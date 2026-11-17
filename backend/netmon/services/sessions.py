"""[premium] Registry of signed-in browsers (`user_sessions`, migration 0014).

The session cookie stays the credential (1.4.1 rules: signature, `token_version`, `sid` not
revoked, absolute lifetime). This module only *records* each session so that a user can see
their browsers and end one (`GET` / `DELETE /api/auth/sessions`):

* a row is written the first time a session is seen (also a session issued before migration
  0014: nobody is signed out by the upgrade) and its `last_seen_at`, IP address and browser
  at most once per `TOUCH_EVERY_S` per session (an in-memory throttle, plus the same rule in
  the SQL so a second writer cannot write more often);
* a row is listed only while its session can still be alive: same `token_version` as the
  user, not revoked, within the absolute lifetime and seen within the cookie's lifetime;
* the API exposes `public_id`, a random value unrelated to the cookie and its `sid`.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from netmon.services.auth import TOKEN_TTL_S
from netmon.services.security import REVOCATION_KEEP, policy_of

log = logging.getLogger("netmon.sessions")

TOUCH_EVERY_S = 60.0
UA_MAX = 256
IP_MAX = 64
# a cookie expires at most TOKEN_TTL_S after the last request (sliding renewal); the last
# request is at most one throttle interval after `last_seen_at`
IDLE_MAX = timedelta(seconds=TOKEN_TTL_S + 2 * TOUCH_EVERY_S)


def new_public_id() -> str:
    return secrets.token_urlsafe(12)


def clean_user_agent(value: str | None) -> str | None:
    if not value:
        return None
    out = "".join(ch for ch in value if ch.isprintable()).strip()[:UA_MAX]
    return out or None


def clean_ip(value: str | None) -> str | None:
    if not value or value == "unknown":
        return None
    return value[:IP_MAX]


class TouchThrottle:
    """At most one registry write per session id per `every_s` (per process; the server is
    one process). Bounded: entries older than the interval are dropped when it grows."""

    def __init__(
        self, every_s: float = TOUCH_EVERY_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.every_s = every_s
        self.clock = clock
        self._last: dict[str, float] = {}

    def due(self, sid: str) -> bool:
        last = self._last.get(sid)
        return last is None or self.clock() - last >= self.every_s

    def done(self, sid: str) -> None:
        now = self.clock()
        self._last[sid] = now
        if len(self._last) > 20000:
            cutoff = now - self.every_s
            self._last = {k: v for k, v in self._last.items() if v >= cutoff}

    def forget(self, sid: str) -> None:
        self._last.pop(sid, None)

    def clear(self) -> None:
        self._last.clear()


def throttle_of(rt: Any) -> TouchThrottle:
    th = getattr(rt, "session_touch", None)
    if not isinstance(th, TouchThrottle):
        th = rt.session_touch = TouchThrottle()
    return th


@dataclass(frozen=True)
class Seen:
    sid: str
    user_id: int
    token_version: int
    auth_at: float  # `at` claim (epoch seconds, app clock)
    ip: str | None
    user_agent: str | None


_UPSERT = text(
    """
    INSERT INTO user_sessions (public_id, sid, user_id, token_version, created_at,
                               last_seen_at, ip, user_agent)
    SELECT CAST(:pid AS varchar), CAST(:sid AS varchar), CAST(:uid AS integer),
           CAST(:tv AS integer), CAST(:created AS timestamptz), CAST(:now AS timestamptz),
           CAST(:ip AS varchar), CAST(:ua AS varchar)
    WHERE NOT EXISTS (SELECT 1 FROM revoked_sessions r WHERE r.sid = CAST(:sid AS varchar))
    ON CONFLICT (sid) DO UPDATE
       SET last_seen_at = EXCLUDED.last_seen_at, ip = EXCLUDED.ip,
           user_agent = EXCLUDED.user_agent
     WHERE user_sessions.last_seen_at IS NULL
        OR user_sessions.last_seen_at <= CAST(:stale AS timestamptz)
    RETURNING (xmax = 0) AS inserted
    """
)


async def touch(rt: Any, seen: Seen, *, source: str | None = None) -> bool:
    """Record that the session was used now, unless it was recorded less than a minute ago.
    Returns True when the database was written. Never raises (the request goes on)."""
    th = throttle_of(rt)
    if not th.due(seen.sid):
        return False
    now = rt.clock.now()
    try:
        created = datetime.fromtimestamp(seen.auth_at, UTC)
    except (OverflowError, OSError, ValueError):
        created = now
    try:
        async with rt.sessionmaker() as session:
            row = (
                await session.execute(
                    _UPSERT,
                    {
                        "pid": new_public_id(),
                        "sid": seen.sid,
                        "uid": seen.user_id,
                        "tv": seen.token_version,
                        "created": created,
                        "now": now,
                        "ip": clean_ip(seen.ip),
                        "ua": clean_user_agent(seen.user_agent),
                        "stale": now - timedelta(seconds=TOUCH_EVERY_S),
                    },
                )
            ).first()
            if row is not None and row[0]:
                # a new session of this user: drop the rows of sessions that cannot be alive
                await prune_user(rt, session, seen.user_id, source)
            await session.commit()
    except Exception:  # noqa: BLE001
        log.exception("Could not record the session's activity")
        return False
    th.done(seen.sid)
    return row is not None


def _cutoffs(rt: Any, source: str | None) -> tuple[datetime, datetime]:
    now = rt.clock.now()
    return now - timedelta(seconds=policy_of(rt).session_max_s(source)), now - IDLE_MAX


_ALIVE = """
    s.user_id = :uid AND s.token_version = u.token_version
    AND s.created_at > :abs_cutoff
    AND (s.last_seen_at > :idle_cutoff OR s.sid = :current)
    AND NOT EXISTS (SELECT 1 FROM revoked_sessions r WHERE r.sid = s.sid)
"""


async def prune_user(rt: Any, session: Any, user_id: int, source: str | None) -> None:
    """Delete the rows of `user_id` whose session cannot be alive any more (caller commits)."""
    abs_cutoff, idle_cutoff = _cutoffs(rt, source)
    await session.execute(
        text(
            "DELETE FROM user_sessions s USING users u "
            f"WHERE u.id = s.user_id AND s.user_id = :uid AND NOT ({_ALIVE})"
        ),
        {"uid": user_id, "abs_cutoff": abs_cutoff, "idle_cutoff": idle_cutoff, "current": ""},
    )


async def prune_all(rt: Any) -> None:
    """Startup: drop rows no session can still use (older than any session can live)."""
    async with rt.sessionmaker() as session:
        await session.execute(
            text("DELETE FROM user_sessions WHERE coalesce(last_seen_at, created_at) < :cut"),
            {"cut": rt.clock.now() - REVOCATION_KEEP},
        )
        await session.execute(
            text(
                "DELETE FROM user_sessions s USING users u "
                "WHERE u.id = s.user_id AND s.token_version <> u.token_version"
            )
        )
        await session.commit()


async def list_alive(
    rt: Any, session: Any, user_id: int, source: str | None, current_sid: str | None
) -> list[Any]:
    """The user's sessions that can still be alive, newest activity first."""
    abs_cutoff, idle_cutoff = _cutoffs(rt, source)
    rows = await session.execute(
        text(
            "SELECT s.public_id, s.sid, s.created_at, s.last_seen_at, s.ip, s.user_agent "
            f"FROM user_sessions s JOIN users u ON u.id = s.user_id WHERE {_ALIVE} "
            "ORDER BY coalesce(s.last_seen_at, s.created_at) DESC, s.id DESC"
        ),
        {
            "uid": user_id,
            "abs_cutoff": abs_cutoff,
            "idle_cutoff": idle_cutoff,
            "current": current_sid or "",
        },
    )
    return list(rows.all())


async def find_alive(
    rt: Any,
    session: Any,
    user_id: int,
    source: str | None,
    public_id: str,
    current_sid: str | None,
) -> Any | None:
    """The caller's live session `public_id` (None for an unknown, dead or foreign id)."""
    abs_cutoff, idle_cutoff = _cutoffs(rt, source)
    return (
        await session.execute(
            text(
                "SELECT s.public_id, s.sid FROM user_sessions s JOIN users u ON u.id = s.user_id "
                f"WHERE s.public_id = :pid AND {_ALIVE}"
            ),
            {
                "pid": public_id,
                "uid": user_id,
                "abs_cutoff": abs_cutoff,
                "idle_cutoff": idle_cutoff,
                "current": current_sid or "",
            },
        )
    ).first()


async def forget(rt: Any, session: Any, sid: str) -> None:
    """The session ended (logout / revoked): drop its row (caller commits)."""
    await session.execute(text("DELETE FROM user_sessions WHERE sid = :sid"), {"sid": sid})
    throttle_of(rt).forget(sid)


async def forget_user(rt: Any, session: Any, user_id: int) -> None:
    """Every session of the user ended (logout everywhere): drop their rows (caller commits)."""
    sids = await session.execute(
        text("DELETE FROM user_sessions WHERE user_id = :uid RETURNING sid"), {"uid": user_id}
    )
    th = throttle_of(rt)
    for (sid,) in sids.all():
        th.forget(sid)
