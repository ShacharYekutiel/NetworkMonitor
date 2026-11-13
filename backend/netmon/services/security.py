"""[1.4.1] Security hardening settings and helpers (pentest findings 3, 5, 6, 7, 8).

Settings keys (data-model "[1.4.1] Security settings"), cached on `rt.security` so the hot
paths (every request, every HTTP check) never read the settings table:

* ``egress_allow_loopback`` (bool, default false): webhook / Teams / HTTP-sensor targets on
  this server itself (loopback) are allowed. Link-local / cloud metadata stay blocked always.
* ``session_max_age_h`` (int, default 168): absolute lifetime of a login session. Directory
  accounts (AD / Kerberos / Microsoft sign-in) end at ``min(12 h, this)``.
* ``login_ip_max_failures`` (int, default 20, 0 = off): failed password logins per client
  address in 5 minutes, across all user names (password spraying).
* ``frame_ancestors`` (list of origins, default []): sites that may embed this application in
  a frame, in addition to the application itself (``'self'``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.security")

DEFAULTS: dict[str, Any] = {
    "egress_allow_loopback": False,
    "session_max_age_h": 168,
    "login_ip_max_failures": 20,
    "frame_ancestors": [],
}
KEYS = tuple(DEFAULTS)
SESSION_MAX_AGE_H_MAX = 720
DIRECTORY_SESSION_MAX_S = 12 * 3600  # AD / OIDC users log in again at least every 12 h
# a revoked session id is remembered longer than any session can live
REVOCATION_KEEP = timedelta(hours=SESSION_MAX_AGE_H_MAX + 24)

HSTS = "max-age=31536000"
REFERRER_POLICY = "same-origin"


@dataclass(frozen=True)
class SecurityPolicy:
    allow_loopback: bool = False
    session_max_age_s: int = 168 * 3600
    login_ip_max_failures: int = 20
    frame_ancestors: tuple[str, ...] = ()

    def session_max_s(self, source: str | None) -> int:
        """Absolute session lifetime of a user of `source` (local | ldap | oidc)."""
        if (source or "local") in ("ldap", "oidc"):
            return min(DIRECTORY_SESSION_MAX_S, self.session_max_age_s)
        return self.session_max_age_s


DEFAULT_POLICY = SecurityPolicy()


def policy_of(rt: Any) -> SecurityPolicy:
    p = getattr(rt, "security", None)
    return p if isinstance(p, SecurityPolicy) else DEFAULT_POLICY


def policy_from(values: dict[str, Any]) -> SecurityPolicy:
    def _int(key: str) -> int:
        try:
            return int(values.get(key, DEFAULTS[key]))
        except (TypeError, ValueError):
            return int(DEFAULTS[key])

    ancestors = values.get("frame_ancestors") or []
    return SecurityPolicy(
        allow_loopback=bool(values.get("egress_allow_loopback", False)),
        session_max_age_s=max(1, min(SESSION_MAX_AGE_H_MAX, _int("session_max_age_h"))) * 3600,
        login_ip_max_failures=max(0, _int("login_ip_max_failures")),
        frame_ancestors=tuple(a for a in ancestors if isinstance(a, str) and valid_ancestor(a)),
    )


def apply(rt: Any, values: dict[str, Any]) -> SecurityPolicy:
    """Cache the policy on the runtime and configure the process-wide egress filter.
    Loopback targets are also allowed in test mode (the E2E simulators live on 127.0.0.x)."""
    from netmon.util import egress

    pol = policy_from(values)
    rt.security = pol
    test_mode = bool(getattr(getattr(rt, "settings", None), "netmon_test_mode", False))
    egress.configure(allow_loopback=pol.allow_loopback or test_mode)
    return pol


async def load(rt: Runtime) -> SecurityPolicy:
    from netmon.services import settings as settings_svc

    async with rt.sessionmaker() as session:
        values = {k: await settings_svc.get(session, k) for k in KEYS}
    return apply(rt, values)


# ---------------------------------------------------------------- frame ancestors / CSP
# scheme://host[:port], host may start with "*." (CSP host-source); nothing else, so a value
# can never inject another directive into the header
_ANCESTOR_RE = re.compile(
    r"^https?://(\*\.)?[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*(:\d{1,5})?$"
)


def valid_ancestor(value: str) -> bool:
    return bool(_ANCESTOR_RE.match(value)) and len(value) <= 255


def inline_script_hashes(html: str) -> tuple[str, ...]:
    """CSP hash sources of the inline <script> blocks of a page (the SPA's index.html has one:
    the theme bootstrap). Scripts with a `src` are covered by 'self'."""
    out: list[str] = []
    for m in re.finditer(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>", html, re.S | re.I):
        if re.search(r"\bsrc\s*=", m.group("attrs"), re.I) or not m.group("body").strip():
            continue
        out.append(script_hash(m.group("body")))
    return tuple(dict.fromkeys(out))


def script_hash(body: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def frame_ancestors_value(pol: SecurityPolicy) -> str:
    return " ".join(("'self'", *pol.frame_ancestors))


def build_csp(pol: SecurityPolicy, script_hashes: tuple[str, ...] = ()) -> str:
    """The application's Content-Security-Policy.

    * scripts: same origin only (plus the hashes of the page's own inline bootstrap); no
      ``unsafe-inline``, no ``unsafe-eval``;
    * styles: the bundled stylesheet; inline ``style`` *attributes* are allowed (the chart
      library writes them in its tooltips), inline ``<style>`` elements are not;
    * images / fonts: same origin and ``data:`` / ``blob:`` (inline SVG icons, chart export);
    * connections: same origin (in CSP 3 ``'self'`` covers the same-origin WebSocket);
    * no plugins, no foreign ``<base>``, forms post to the application only;
    * framing: the application itself plus the ``frame_ancestors`` setting.
    """
    scripts = " ".join(("'self'", *script_hashes))
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src {scripts}",
            "style-src 'self'",
            "style-src-attr 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "worker-src 'self' blob:",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            f"frame-ancestors {frame_ancestors_value(pol)}",
        )
    )


# the interactive API documentation (/api/docs) loads Swagger UI from a CDN with an inline
# bootstrap script; it gets its own policy instead of weakening the application's
DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
)
# a stored report document (inline styles only, no scripts), served as a download
DOCUMENT_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox"
)


def security_headers(
    pol: SecurityPolicy, *, csp: str, https: bool, path: str = "/"
) -> dict[str, str]:
    """Response headers added by the app to every response (finding 8). A header the handler
    set itself is kept (the caller uses setdefault)."""
    headers = {
        "Content-Security-Policy": DOCS_CSP if path.startswith("/api/docs") else csp,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": REFERRER_POLICY,
    }
    # X-Frame-Options cannot express an allow-list: sent only while nothing but the app
    # itself may frame it (otherwise it would contradict frame-ancestors in older browsers)
    if not pol.frame_ancestors or path.startswith("/api/docs"):
        headers["X-Frame-Options"] = "DENY" if path.startswith("/api/docs") else "SAMEORIGIN"
    if https:
        headers["Strict-Transport-Security"] = HSTS
    return headers


# ---------------------------------------------------------------- revoked sessions
async def load_revoked(rt: Runtime) -> set[str]:
    """Session ids revoked by a logout and not yet past their keep time (startup)."""
    async with rt.sessionmaker() as session:
        await session.execute(text("DELETE FROM revoked_sessions WHERE expires_at < now()"))
        rows = await session.execute(text("SELECT sid FROM revoked_sessions"))
        sids = {str(r[0]) for r in rows}
        await session.commit()
    rt.revoked_sessions = sids
    return sids


async def revoke_session(rt: Any, session: Any, sid: str, user_id: int) -> None:
    """Remember `sid` as revoked (the caller commits); effective at once in this process."""
    expires = datetime.now(UTC) + REVOCATION_KEEP
    await session.execute(
        text(
            "INSERT INTO revoked_sessions (sid, user_id, revoked_at, expires_at) "
            "VALUES (:sid, :uid, now(), :exp) ON CONFLICT (sid) DO NOTHING"
        ),
        {"sid": sid, "uid": user_id, "exp": expires},
    )
    await session.execute(text("DELETE FROM revoked_sessions WHERE expires_at < now()"))
    revoked = getattr(rt, "revoked_sessions", None)
    if revoked is None:
        revoked = rt.revoked_sessions = set()
    revoked.add(sid)


def is_revoked(rt: Any, sid: str) -> bool:
    return sid in (getattr(rt, "revoked_sessions", None) or ())


# ---------------------------------------------------------------- startup report
async def warn_blocked_targets(rt: Runtime) -> list[str]:
    """Existing HTTP sensors / webhook / Teams channels whose target the egress filter now
    refuses: one WARNING at startup (and a `system` event) naming them. They keep existing
    and fail with a clear status message until the target or the setting changes."""
    from netmon.util import egress

    found: list[str] = []
    async with rt.sessionmaker() as session:
        sensors = (
            await session.execute(
                text(
                    "SELECT s.id, s.name, s.config->>'url' AS url, d.name AS device, d.host "
                    "FROM sensors s JOIN devices d ON d.id = s.device_id "
                    "WHERE s.type = 'http' AND s.config ? 'url' ORDER BY s.id"
                )
            )
        ).all()
        channels = (
            await session.execute(
                text(
                    "SELECT id, name, type, config->>'url' AS url FROM notification_channels "
                    "WHERE type IN ('webhook', 'teams') AND config ? 'url' ORDER BY id"
                )
            )
        ).all()
    from netmon.poller.checks.http import host_for_url

    sem = asyncio.Semaphore(10)

    async def blocked(url: str) -> bool:
        async with sem:
            try:  # a slow resolver must not hold the report up
                return bool(await asyncio.wait_for(egress.url_block_reason(url), 5.0))
            except TimeoutError:
                return False

    sensor_hits = await asyncio.gather(
        *(
            blocked(str(s.url or "").replace("{host}", host_for_url(str(s.host or ""))))
            for s in sensors
        )
    )
    channel_hits = await asyncio.gather(*(blocked(str(c.url or "")) for c in channels))
    for s, hit in zip(sensors, sensor_hits, strict=True):
        if hit:
            found.append(f"HTTP sensor #{s.id} '{s.name}' on device '{s.device}'")
    for c, hit in zip(channels, channel_hits, strict=True):
        if hit:
            found.append(f"{c.type} channel #{c.id} '{c.name}'")
    if found:
        log.warning(
            "%d monitoring / notification target(s) are blocked by the egress filter "
            "(loopback / link-local / cloud metadata) and will fail until changed%s: %s",
            len(found),
            ""
            if egress.loopback_allowed()
            else " (loopback targets: Settings > General > Security, "
            '"Allow monitoring and notification targets on this server")',
            "; ".join(found),
        )
    return found
