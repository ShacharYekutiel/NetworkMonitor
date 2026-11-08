"""[2b-3] Active Directory authentication with ldap3 (architecture § 12.7).

All ldap3 calls run in worker threads (`asyncio` never waits on a socket): at most
`MAX_THREADS` concurrent directory operations, each with hard socket timeouts and an overall
budget of min(3 × timeout_s + 2, 30) s. LDAPS or StartTLS only (never plaintext), certificate
chain + hostname verified against `tlsutil.trust_bundle_pem()` unless `tls_verify` is off.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from netmon.auth import ldap_pure
from netmon.auth.ldap_config import LdapConfig

log = logging.getLogger("netmon.auth.ldap")

MAX_THREADS = 4
LAST_GOOD_S = 600.0
GUARD_MAX = 5
GUARD_WINDOW_S = 900.0


# ---------------------------------------------------------------- results
@dataclass
class Step:
    step: str
    ok: bool
    detail: str
    server: str | None = None

    def out(self) -> dict[str, Any]:
        return {"step": self.step, "ok": self.ok, "detail": self.detail, "server": self.server}


@dataclass
class DirectoryUser:
    dn: str
    sam_account_name: str
    user_principal_name: str | None
    display_name: str | None
    mail: str | None
    guid: str | None
    disabled: bool
    locked: bool

    def out(self) -> dict[str, Any]:
        return {
            "dn": self.dn,
            "sam_account_name": self.sam_account_name,
            "user_principal_name": self.user_principal_name,
            "display_name": self.display_name,
            "mail": self.mail,
            "disabled": self.disabled,
            "locked": self.locked,
        }


@dataclass
class AuthOutcome:
    ok: bool
    reason: str | None = None  # login failure reason (api.md); directory_unavailable → 503
    role: str | None = None
    user: DirectoryUser | None = None
    server: str | None = None
    steps: list[Step] = field(default_factory=list)

    @property
    def unavailable(self) -> bool:
        return self.reason == "directory_unavailable"


class _Unavailable(Exception):
    pass


# ---------------------------------------------------------------- TLS
def _make_tls(cfg: LdapConfig, host: str) -> Any:
    from ldap3 import Tls

    from netmon.tlsutil import trust_bundle_pem

    class NetmonTls(Tls):  # type: ignore[misc]
        """Stdlib SSLContext with check_hostname (ldap3's own matcher is a backport without
        IP-SAN support); remembers the peer certificate and the TLS error for the test page."""

        def __init__(self) -> None:
            super().__init__(validate=ssl.CERT_REQUIRED if cfg.tls_verify else ssl.CERT_NONE)
            self.peer_der: bytes | None = None
            self.error: str | None = None

        def wrap_socket(self, connection: Any, do_handshake: bool = False) -> None:
            if cfg.tls_verify:
                ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
                ctx.load_verify_locations(cadata=trust_bundle_pem() or None)
                ctx.check_hostname = True
                ctx.verify_mode = ssl.CERT_REQUIRED
            else:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            try:
                wrapped = ctx.wrap_socket(
                    connection.socket,
                    server_side=False,
                    do_handshake_on_connect=True,
                    server_hostname=host,
                )
            except ssl.SSLCertVerificationError as exc:
                self.error = f"certificate verify failed: {exc.verify_message or exc}"
                raise
            except (ssl.SSLError, OSError) as exc:
                self.error = f"TLS error: {exc}"
                raise
            with contextlib.suppress(Exception):
                self.peer_der = wrapped.getpeercert(binary_form=True)
            connection.socket = wrapped

    return NetmonTls()


def _cert_info(der: bytes | None, verified: bool) -> dict[str, Any] | None:
    if not der:
        return None
    try:
        from cryptography import x509

        cert = x509.load_der_x509_certificate(der)
        not_after = cert.not_valid_after_utc
        from netmon.services.serialize import iso

        return {
            "subject": cert.subject.rfc4514_string(),
            "issuer": cert.issuer.rfc4514_string(),
            "not_after": iso(not_after),
            "verified": verified,
        }
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- connection helpers
@dataclass
class _Target:
    host: str
    port: int

    @property
    def label(self) -> str:
        return ldap_pure.server_label(self.host, self.port)


def discover_srv(domain: str, timeout_s: float) -> list[tuple[str, int]]:
    """DNS SRV `_ldap._tcp.dc._msdcs.<domain>` → [(host, port)] by priority, weight desc."""
    import dns.exception
    import dns.resolver

    try:
        ans = dns.resolver.resolve(
            f"_ldap._tcp.dc._msdcs.{domain}", "SRV", lifetime=max(1.0, timeout_s)
        )
    except (dns.exception.DNSException, OSError) as exc:
        log.warning("DNS SRV lookup for %s failed: %s", domain, exc)
        return []
    recs = sorted(ans, key=lambda r: (r.priority, -r.weight))
    return [(str(r.target).rstrip("."), int(r.port)) for r in recs]


def resolve_targets(cfg: LdapConfig) -> tuple[list[_Target], bool]:
    """(targets, discovered_via_dns)."""
    if cfg.servers:
        out = []
        for s in cfg.servers:
            try:
                host, port = ldap_pure.parse_server(s, cfg.security)
            except ValueError:
                continue
            out.append(_Target(host, port))
        return out, False
    if not cfg.domain:
        return [], True
    default = 636 if cfg.security == "ldaps" else 389
    seen = set()
    out = []
    for host, _srv_port in discover_srv(cfg.domain, cfg.timeout_s):
        if host in seen:
            continue
        seen.add(host)
        # SRV advertises 389; LDAPS uses 636 on the same DC
        out.append(_Target(host, default))
    return out, True


class _Conn:
    """One ldap3 SYNC connection with TLS established (LDAPS or StartTLS)."""

    def __init__(self, cfg: LdapConfig, target: _Target) -> None:
        self.cfg = cfg
        self.target = target
        self.tls = _make_tls(cfg, target.host)
        self.conn: Any = None
        self.latency_ms: int | None = None

    def open(self) -> None:
        from ldap3 import NONE, SYNC, Connection, Server

        t = float(self.cfg.timeout_s)
        server = Server(
            self.target.host,
            port=self.target.port,
            use_ssl=self.cfg.security == "ldaps",
            tls=self.tls,
            get_info=NONE,
            connect_timeout=t,
        )
        self.conn = Connection(
            server,
            client_strategy=SYNC,
            auto_referrals=False,
            raise_exceptions=False,
            receive_timeout=int(t),  # ldap3 packs it into SO_RCVTIMEO: must be an int
            read_only=True,
            check_names=False,
        )
        start = time.monotonic()
        try:
            self.conn.open()
            if self.cfg.security == "starttls" and not self.conn.start_tls():
                raise _Unavailable("StartTLS failed")
        except _Unavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - ldap3 raises many socket/TLS types
            log.debug("LDAP connect to %s failed", self.target.label, exc_info=True)
            raise _Unavailable(self.tls.error or _short(exc)) from exc
        if self.conn.socket is None or not isinstance(self.conn.socket, ssl.SSLSocket):
            raise _Unavailable("TLS was not established")
        self.latency_ms = int((time.monotonic() - start) * 1000)

    def bind(self, user: str, password: str) -> tuple[bool, int | None, str]:
        """(ok, result code, message). Communication failures raise _Unavailable."""
        from ldap3 import SIMPLE

        self.conn.user = user
        self.conn.password = password
        self.conn.authentication = SIMPLE
        try:
            ok = bool(self.conn.bind())
        except Exception as exc:  # noqa: BLE001
            raise _Unavailable(_short(exc)) from None
        res = self.conn.result or {}
        if not ok and res.get("result") is None:
            raise _Unavailable(self.conn.last_error or "bind failed")
        return ok, res.get("result"), str(res.get("message") or res.get("description") or "")

    def search(
        self,
        base: str,
        filt: str,
        scope: str,
        attrs: list[str],
        size_limit: int = 0,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """(entries with raw attributes, result code)."""
        from ldap3 import BASE, SUBTREE

        try:
            self.conn.search(
                base,
                filt,
                search_scope=BASE if scope == "base" else SUBTREE,
                attributes=attrs,
                size_limit=size_limit,
            )
        except Exception as exc:  # noqa: BLE001
            raise _Unavailable(_short(exc)) from None
        res = self.conn.result or {}
        if res.get("result") is None:
            raise _Unavailable(self.conn.last_error or "search failed")
        entries = [
            {"dn": e.get("dn", ""), "raw": e.get("raw_attributes") or {}}
            for e in (self.conn.response or [])
            if e.get("type") == "searchResEntry"
        ]
        return entries, res.get("result")

    def root_dse_is_ad(self) -> bool:
        try:
            entries, _ = self.search("", "(objectClass=*)", "base", ["supportedCapabilities"])
        except _Unavailable:
            return False
        for e in entries:
            for v in _raw_list(e["raw"], "supportedCapabilities"):
                if v == ldap_pure.AD_CAPABILITY:
                    return True
        return False

    def close(self) -> None:
        if self.conn is not None:
            with contextlib.suppress(Exception):
                self.conn.unbind()


def _short(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text[:300]


def _raw_list(raw: dict[str, Any], name: str) -> list[str]:
    for k, v in raw.items():
        if k.lower() == name.lower():
            out = []
            for item in v or []:
                if isinstance(item, bytes):
                    out.append(item.decode("utf-8", "replace"))
                else:
                    out.append(str(item))
            return out
    return []


def _raw_first_bytes(raw: dict[str, Any], name: str) -> bytes | None:
    for k, v in raw.items():
        if k.lower() == name.lower() and v:
            item = v[0]
            return item if isinstance(item, bytes) else str(item).encode()
    return None


def _raw_first(raw: dict[str, Any], name: str) -> str | None:
    vals = _raw_list(raw, name)
    return vals[0] if vals and vals[0] != "" else None


# ---------------------------------------------------------------- the login sequence (thread)
class _LastGood:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._label: str | None = None
        self._at = 0.0

    def order(self, targets: list[_Target]) -> list[_Target]:
        with self._lock:
            label, at = self._label, self._at
        if label is None or time.monotonic() - at > LAST_GOOD_S:
            return targets
        first = [t for t in targets if t.label == label]
        return first + [t for t in targets if t.label != label]

    def remember(self, label: str) -> None:
        with self._lock:
            self._label, self._at = label, time.monotonic()

    def clear(self) -> None:
        with self._lock:
            self._label = None


def _connect_service(
    cfg: LdapConfig,
    targets: list[_Target],
    deadline: float,
    steps: list[Step],
    last_good: _LastGood | None,
) -> _Conn:
    if not cfg.bind_password:
        detail = (
            "the stored bind password cannot be decrypted"
            if cfg.bind_password_unreadable
            else "no service account password"
        )
        steps.append(Step("service_bind", False, detail))
        raise _Unavailable(detail)
    if not targets:
        steps.append(Step("connect", False, "no directory server configured or discovered"))
        raise _Unavailable("no server")
    ordered = last_good.order(targets) if last_good else targets
    last_error = "no server answered"
    for target in ordered:
        if time.monotonic() >= deadline:
            steps.append(Step("connect", False, "time budget exhausted", target.label))
            break
        c = _Conn(cfg, target)
        try:
            c.open()
        except _Unavailable as exc:
            last_error = str(exc)
            steps.append(Step("connect", False, last_error, target.label))
            c.close()
            continue
        steps.append(
            Step("connect", True, f"{cfg.security.upper()} in {c.latency_ms} ms", target.label)
        )
        try:
            ok, code, msg = c.bind(cfg.bind_dn, cfg.bind_password)
        except _Unavailable as exc:
            last_error = str(exc)
            steps.append(Step("service_bind", False, last_error, target.label))
            c.close()
            continue
        if not ok:
            last_error = f"service bind failed (result {code}): {msg}"[:300]
            steps.append(Step("service_bind", False, last_error, target.label))
            c.close()
            continue
        steps.append(Step("service_bind", True, f"bound as {cfg.bind_dn}", target.label))
        if last_good:
            last_good.remember(target.label)
        return c
    raise _Unavailable(last_error)


def _find_user(
    c: _Conn, cfg: LdapConfig, name: ldap_pure.LoginName, steps: list[Step]
) -> DirectoryUser | str:
    base = cfg.effective_base_dn
    filters = []
    if name.upn:
        filters.append(ldap_pure.user_filter_upn(name.upn))
    if name.sam:
        filters.append(ldap_pure.user_filter_sam(name.sam))
    for filt in filters:
        entries, code = c.search(base, filt, "sub", list(ldap_pure.USER_ATTRS), size_limit=2)
        if len(entries) >= 2 or code == 4 and entries:
            steps.append(
                Step("search_user", False, "more than one account matches", c.target.label)
            )
            return "ambiguous_user"
        if code not in (0, 4) and not entries:
            if code == 32:  # noSuchObject: base DN wrong
                steps.append(
                    Step("search_user", False, f"base DN {base} not found", c.target.label)
                )
                return "unknown_user"
            raise _Unavailable(f"search failed (result {code})")
        if entries:
            e = entries[0]
            raw = e["raw"]
            uac = _raw_first(raw, "userAccountControl")
            computed = _raw_first(raw, "msDS-User-Account-Control-Computed")
            u = ldap_pure._as_int(uac) or 0  # noqa: SLF001
            cu = ldap_pure._as_int(computed) or 0  # noqa: SLF001
            dn = e["dn"] or _raw_first(raw, "distinguishedName") or ""
            user = DirectoryUser(
                dn=dn,
                sam_account_name=_raw_first(raw, "sAMAccountName") or (name.sam or ""),
                user_principal_name=_raw_first(raw, "userPrincipalName"),
                display_name=_raw_first(raw, "displayName"),
                mail=_raw_first(raw, "mail"),
                guid=ldap_pure.guid_hex(_raw_first_bytes(raw, "objectGUID")),
                disabled=bool(u & ldap_pure.UAC_ACCOUNTDISABLE),
                locked=bool((cu | u) & ldap_pure.UAC_LOCKOUT),
            )
            user._uac = uac  # type: ignore[attr-defined]
            user._computed = computed  # type: ignore[attr-defined]
            user._expires = _raw_first(raw, "accountExpires")  # type: ignore[attr-defined]
            steps.append(Step("search_user", True, f"found {dn}", c.target.label))
            return user
    steps.append(Step("search_user", False, "no such user", c.target.label))
    return "unknown_user"


def _in_group(c: _Conn, user_dn: str, group_dn: str) -> bool:
    if not group_dn:
        return False
    entries, code = c.search(user_dn, ldap_pure.group_filter(group_dn), "base", ["1.1"])
    return bool(entries)


def authenticate_sync(
    cfg: LdapConfig,
    username: str,
    password: str,
    deadline: float,
    last_good: _LastGood | None = None,
    now: Callable[[], datetime] | None = None,
) -> AuthOutcome:
    """The full § 12.7 sequence (blocking; call in a thread)."""
    steps: list[Step] = []
    name = ldap_pure.parse_login(username, cfg.domain, cfg.netbios_domain)
    if name is None:
        steps.append(Step("search_user", False, "not a directory user name"))
        return AuthOutcome(False, "unknown_user", steps=steps)
    if password == "":
        steps.append(Step("user_bind", False, "empty password (never sent to the directory)"))
        return AuthOutcome(False, "empty_password", steps=steps)
    targets, _dns = resolve_targets(cfg)
    svc: _Conn | None = None
    ucon: _Conn | None = None
    try:
        try:
            svc = _connect_service(cfg, targets, deadline, steps, last_good)
        except _Unavailable:
            return AuthOutcome(False, "directory_unavailable", steps=steps)
        label = svc.target.label
        try:
            found = _find_user(svc, cfg, name, steps)
        except _Unavailable as exc:
            steps.append(Step("search_user", False, str(exc), label))
            return AuthOutcome(False, "directory_unavailable", server=label, steps=steps)
        if isinstance(found, str):
            return AuthOutcome(False, found, server=label, steps=steps)
        user = found
        reason = ldap_pure.account_state(
            user._uac,  # type: ignore[attr-defined]
            user._computed,  # type: ignore[attr-defined]
            user._expires,  # type: ignore[attr-defined]
            (now or (lambda: datetime.now(UTC)))(),
        )
        if reason:
            steps.append(Step("account_state", False, reason, label))
            return AuthOutcome(False, reason, user=user, server=label, steps=steps)
        steps.append(Step("account_state", True, "account enabled", label))
        # bind as the user on the same server
        ucon = _Conn(cfg, svc.target)
        try:
            ucon.open()
            ok, code, msg = ucon.bind(user.dn, password)
        except _Unavailable as exc:
            steps.append(Step("user_bind", False, str(exc), label))
            return AuthOutcome(False, "directory_unavailable", user=user, server=label, steps=steps)
        if not ok:
            r = ldap_pure.map_bind_error(msg) if code == 49 else "bad_password"
            steps.append(Step("user_bind", False, r, label))
            return AuthOutcome(False, r, user=user, server=label, steps=steps)
        steps.append(Step("user_bind", True, "password accepted", label))
        try:
            role = None
            if _in_group(svc, user.dn, cfg.admin_group_dn):
                role = "admin"
            elif _in_group(svc, user.dn, cfg.viewer_group_dn):
                role = "viewer"
        except _Unavailable as exc:
            steps.append(Step("groups", False, str(exc), label))
            return AuthOutcome(False, "directory_unavailable", user=user, server=label, steps=steps)
        if role is None:
            steps.append(Step("groups", False, "not a member of the admin or viewer group", label))
            return AuthOutcome(False, "not_in_group", user=user, server=label, steps=steps)
        steps.append(Step("groups", True, f"role {role}", label))
        return AuthOutcome(True, None, role=role, user=user, server=label, steps=steps)
    finally:
        if ucon is not None:
            ucon.close()
        if svc is not None:
            svc.close()


def lookup_sync(
    cfg: LdapConfig,
    sam: str,
    deadline: float,
    last_good: _LastGood | None = None,
    now: Callable[[], datetime] | None = None,
) -> AuthOutcome:
    """[3] SSO (Kerberos): the § 12.7 sequence minus the user bind — the identity was already
    proven by the Kerberos ticket. Service bind, find by sAMAccountName, account state, groups
    (blocking; call in a thread)."""
    steps: list[Step] = []
    if not ldap_pure.valid_sam(sam):
        return AuthOutcome(False, "unknown_user", steps=steps)
    targets, _dns = resolve_targets(cfg)
    svc: _Conn | None = None
    try:
        try:
            svc = _connect_service(cfg, targets, deadline, steps, last_good)
        except _Unavailable:
            return AuthOutcome(False, "directory_unavailable", steps=steps)
        label = svc.target.label
        try:
            found = _find_user(svc, cfg, ldap_pure.LoginName(sam=sam, upn=None), steps)
        except _Unavailable as exc:
            steps.append(Step("search_user", False, str(exc), label))
            return AuthOutcome(False, "directory_unavailable", server=label, steps=steps)
        if isinstance(found, str):
            return AuthOutcome(False, found, server=label, steps=steps)
        user = found
        reason = ldap_pure.account_state(
            user._uac,  # type: ignore[attr-defined]
            user._computed,  # type: ignore[attr-defined]
            user._expires,  # type: ignore[attr-defined]
            (now or (lambda: datetime.now(UTC)))(),
        )
        if reason:
            steps.append(Step("account_state", False, reason, label))
            return AuthOutcome(False, reason, user=user, server=label, steps=steps)
        try:
            role = None
            if _in_group(svc, user.dn, cfg.admin_group_dn):
                role = "admin"
            elif _in_group(svc, user.dn, cfg.viewer_group_dn):
                role = "viewer"
        except _Unavailable as exc:
            steps.append(Step("groups", False, str(exc), label))
            return AuthOutcome(False, "directory_unavailable", user=user, server=label, steps=steps)
        if role is None:
            return AuthOutcome(False, "not_in_group", user=user, server=label, steps=steps)
        return AuthOutcome(True, None, role=role, user=user, server=label, steps=steps)
    finally:
        if svc is not None:
            svc.close()


def test_connection_sync(cfg: LdapConfig, deadline: float) -> dict[str, Any]:
    """LdapTestResult (api.md): every server is tried (table), then service bind, base DN and
    groups on the first server that binds."""
    targets, via_dns = resolve_targets(cfg)
    servers: list[dict[str, Any]] = []
    service_bind = {"ok": False, "error": None}
    base_dn = {"ok": False, "error": None}
    groups: dict[str, Any] = {"admin": None, "viewer": None}
    bound: _Conn | None = None
    if not targets:
        service_bind["error"] = "no directory server configured or discovered"
    for target in targets:
        if time.monotonic() >= deadline:
            servers.append(
                {
                    "server": target.label,
                    "ok": False,
                    "security": cfg.security,
                    "latency_ms": None,
                    "tls": None,
                    "active_directory": False,
                    "error": "time budget exhausted",
                }
            )
            continue
        c = _Conn(cfg, target)
        row: dict[str, Any] = {
            "server": target.label,
            "ok": False,
            "security": cfg.security,
            "latency_ms": None,
            "tls": None,
            "active_directory": False,
            "error": None,
        }
        try:
            c.open()
        except _Unavailable as exc:
            row["error"] = str(exc)
            row["tls"] = _cert_info(c.tls.peer_der, False)
            servers.append(row)
            c.close()
            continue
        row["ok"] = True
        row["latency_ms"] = c.latency_ms
        row["tls"] = _cert_info(c.tls.peer_der, cfg.tls_verify)
        if bound is None and cfg.bind_password:
            try:
                ok, code, msg = c.bind(cfg.bind_dn, cfg.bind_password)
            except _Unavailable as exc:
                ok, code, msg = False, None, str(exc)
            if ok:
                bound = c
                service_bind = {"ok": True, "error": None}
            else:
                service_bind = {"ok": False, "error": f"result {code}: {msg}"[:300]}
        row["active_directory"] = c.root_dse_is_ad()
        servers.append(row)
        if c is not bound:
            c.close()
    if not cfg.bind_password and targets:
        service_bind["error"] = (
            "the stored bind password cannot be decrypted; enter it again"
            if cfg.bind_password_unreadable
            else "no service account password"
        )
    if bound is not None:
        try:
            base = cfg.effective_base_dn
            if not base:
                base_dn = {"ok": False, "error": "no base DN"}
            else:
                entries, code = bound.search(base, "(objectClass=*)", "base", ["1.1"])
                base_dn = (
                    {"ok": True, "error": None}
                    if entries
                    else {"ok": False, "error": f"{base} not found (result {code})"}
                )
            for key, dn in (("admin", cfg.admin_group_dn), ("viewer", cfg.viewer_group_dn)):
                if dn:
                    entries, _ = bound.search(dn, "(objectClass=group)", "base", ["1.1"])
                    groups[key] = {"dn": dn, "found": bool(entries)}
        except _Unavailable as exc:
            base_dn = {"ok": False, "error": str(exc)}
        finally:
            bound.close()
    else:
        base_dn["error"] = "not checked (no service bind)"
        for key, dn in (("admin", cfg.admin_group_dn), ("viewer", cfg.viewer_group_dn)):
            if dn:
                groups[key] = {"dn": dn, "found": False}
    any_ok = any(s["ok"] for s in servers)
    ok = (
        any_ok
        and service_bind["ok"]
        and base_dn["ok"]
        and all(g is None or g["found"] for g in groups.values())
    )
    if ok:
        message = "Connection, service bind, base DN and groups OK"
    elif not any_ok:
        message = "No directory server could be reached"
    elif not service_bind["ok"]:
        message = "The service account bind failed"
    elif not base_dn["ok"]:
        message = "The base DN was not found"
    else:
        message = "A group DN was not found"
    return {
        "ok": ok,
        "discovered_via_dns": via_dns,
        "servers": servers,
        "service_bind": service_bind,
        "base_dn": base_dn,
        "admin_group": groups["admin"],
        "viewer_group": groups["viewer"],
        "message": message,
    }


# ---------------------------------------------------------------- async front
class DirectoryGuard:
    """5 failed directory logins per username in 15 min, across all IPs (AD lockout guard)."""

    def __init__(self, max_failures: int = GUARD_MAX, window_s: float = GUARD_WINDOW_S) -> None:
        self.max = max_failures
        self.window = window_s
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[float]:
        q = self._fails[key]
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, username: str) -> bool:
        key = username.lower()
        if key not in self._fails:
            return False
        return len(self._prune(key)) >= self.max

    def fail(self, username: str) -> None:
        self._prune(username.lower()).append(time.monotonic())
        if len(self._fails) > 10000:
            for k in list(self._fails)[:5000]:
                if not self._prune(k):
                    del self._fails[k]

    def reset(self, username: str) -> None:
        self._fails.pop(username.lower(), None)

    def clear(self) -> None:
        self._fails.clear()


class Directory:
    """Process-wide AD client: thread semaphore, last-good DC memory, username guard."""

    def __init__(self, max_threads: int = MAX_THREADS) -> None:
        self._sem = asyncio.Semaphore(max_threads)
        self.last_good = _LastGood()
        self.guard = DirectoryGuard()

    async def _run(self, budget: float, fn: Callable[[float], Any]) -> Any:
        """Run fn(deadline) in a thread; the semaphore slot is released when the thread ends
        (not at the budget), so dead DCs never pile up more than `max_threads` threads."""
        await self._sem.acquire()
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + budget
        try:
            fut = loop.run_in_executor(None, fn, deadline)
        except BaseException:
            self._sem.release()
            raise
        fut.add_done_callback(lambda _f: self._sem.release())
        try:
            return await asyncio.wait_for(asyncio.shield(fut), budget)
        except TimeoutError:
            fut.add_done_callback(_consume)
            raise

    async def authenticate(
        self, cfg: LdapConfig, username: str, password: str, now: datetime | None = None
    ) -> AuthOutcome:
        if password == "":
            return AuthOutcome(
                False, "empty_password", steps=[Step("user_bind", False, "empty password")]
            )
        if cfg.bind_password_unreadable or not cfg.bind_password:
            return AuthOutcome(
                False,
                "directory_unavailable",
                steps=[Step("service_bind", False, "service account password not available")],
            )
        try:
            return await self._run(
                cfg.budget_s,
                lambda dl: authenticate_sync(
                    cfg, username, password, dl, self.last_good, (lambda: now) if now else None
                ),
            )
        except TimeoutError:
            return AuthOutcome(
                False,
                "directory_unavailable",
                steps=[Step("connect", False, f"no answer within {cfg.budget_s:.0f} s")],
            )
        except Exception as exc:  # noqa: BLE001 - never a 500 from the directory path
            log.exception("Directory authentication failed unexpectedly")
            return AuthOutcome(
                False, "directory_unavailable", steps=[Step("connect", False, _short(exc))]
            )

    async def lookup(self, cfg: LdapConfig, sam: str, now: datetime | None = None) -> AuthOutcome:
        """[3] SSO: directory checks of an already authenticated account (no user bind)."""
        if cfg.bind_password_unreadable or not cfg.bind_password:
            return AuthOutcome(
                False,
                "directory_unavailable",
                steps=[Step("service_bind", False, "service account password not available")],
            )
        try:
            return await self._run(
                cfg.budget_s,
                lambda dl: lookup_sync(
                    cfg, sam, dl, self.last_good, (lambda: now) if now else None
                ),
            )
        except TimeoutError:
            return AuthOutcome(
                False,
                "directory_unavailable",
                steps=[Step("connect", False, f"no answer within {cfg.budget_s:.0f} s")],
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Directory lookup failed unexpectedly")
            return AuthOutcome(
                False, "directory_unavailable", steps=[Step("connect", False, _short(exc))]
            )

    async def test_connection(self, cfg: LdapConfig) -> dict[str, Any]:
        budget = float(min(max(len(cfg.servers), 1) * (2 * cfg.timeout_s + 1) + 2, 60))
        try:
            return await self._run(budget, lambda dl: test_connection_sync(cfg, dl))
        except TimeoutError:
            return {
                "ok": False,
                "discovered_via_dns": not cfg.servers,
                "servers": [],
                "service_bind": {"ok": False, "error": "time budget exhausted"},
                "base_dn": {"ok": False, "error": "not checked"},
                "admin_group": None,
                "viewer_group": None,
                "message": f"No answer within {budget:.0f} s",
            }


def _consume(f: asyncio.Future[Any]) -> None:
    with contextlib.suppress(BaseException):
        f.result()
