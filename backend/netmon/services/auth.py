"""Password hashing, JWT session cookie, login throttling."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from starlette.requests import HTTPConnection
from starlette.responses import Response

COOKIE_NAME = "netmon_session"
TOKEN_TTL_S = 43200
RENEW_BELOW_S = 6 * 3600
LDAP_SESSION_MAX_S = 12 * 3600  # [2b-3] AD users log in again at least every 12 h
PW_CHANGE_THROTTLE_IP = "*"  # [1.4.1] change-password guesses: per user, whatever the address

_hasher = PasswordHasher()


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


def hash_password_sync(password: str) -> str:
    return _hasher.hash(password)


async def verify_password(password_hash: str, password: str) -> bool:
    def _verify() -> bool:
        try:
            return _hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    return await asyncio.to_thread(_verify)


_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


async def burn_verify(password: str) -> None:
    """Constant-ish time for unknown users."""
    await verify_password(_DUMMY_HASH, password)


def make_token(
    secret: str,
    user_id: int,
    role: str,
    token_version: int,
    *,
    src: str = "local",
    at: float | None = None,
    exp_cap: float | None = None,
    am: str | None = None,
    sid: str | None = None,
) -> str:
    """[2b-3] claims gain `src` (local|ldap) and `at` (auth time); `exp_cap` (epoch seconds)
    limits the expiry (AD sessions end at `at + 12 h`). [3] `src` may be `oidc`; `am` is the
    authentication method (`password` | `kerberos` | `oidc`; absent = password).
    [1.4.1] `sid` = session id (random per login, kept across renewals): the handle a logout
    revokes server-side."""
    now = int(time.time())
    exp = now + TOKEN_TTL_S
    if exp_cap is not None:
        exp = max(now + 1, min(exp, int(exp_cap)))
    claims = {
        "sub": str(user_id),
        "role": role,
        "tv": token_version,
        "iat": now,
        "exp": exp,
        "src": src,
        "at": int(at if at is not None else now),
        "sid": sid or new_session_id(),
    }
    if am:
        claims["am"] = am
    return jwt.encode(claims, secret, algorithm="HS256")


HARD_LIMIT_SOURCES = ("ldap", "oidc")  # [3] OIDC sessions follow the AD 12 h rule


def new_session_id() -> str:
    return secrets.token_urlsafe(18)


def session_token(
    rt: Any,
    user: Any,
    *,
    at: float | None = None,
    clock_now: float | None = None,
    am: str | None = None,
    sid: str | None = None,
) -> str:
    """Session JWT for `user`. `at` = authentication time (default: now on the app clock).
    [1.4.1] Every session has an absolute lifetime: the cookie expires at the latest at
    `at + max` (real time, shifted like the app clock), where `max` is the setting
    `session_max_age_h` and, for AD / OIDC users, at most 12 h. A renewal passes the original
    `at` and `sid`, so it can never extend the session. [3] `am` = authentication method claim
    (kept across renewals by the caller)."""
    from netmon.services.security import policy_of

    src = getattr(user, "source", "local") or "local"
    clock_now = clock_now if clock_now is not None else rt.clock.now().timestamp()
    at = at if at is not None else clock_now
    remaining = at + policy_of(rt).session_max_s(src) - clock_now
    return make_token(
        rt.secret_key,
        user.id,
        user.role,
        user.token_version,
        src=src,
        at=at,
        exp_cap=time.time() + max(1.0, remaining),
        am=am,
        sid=sid,
    )


def decode_token(secret: str, token: str) -> dict[str, Any] | None:
    try:
        claims = jwt.decode(
            token, secret, algorithms=["HS256"], options={"require": ["sub", "exp", "iat"]}
        )
    except jwt.PyJWTError:
        return None
    return claims


def request_is_https(conn: HTTPConnection, trust_proxy: bool) -> bool:
    if trust_proxy:
        proto = conn.headers.get("x-forwarded-proto")
        if proto:
            return proto.split(",")[0].strip().lower() == "https"
    return conn.url.scheme in ("https", "wss")


def cookie_secure(conn: HTTPConnection, setting: str, trust_proxy: bool) -> bool:
    v = (setting or "auto").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return request_is_https(conn, trust_proxy)


def set_session_cookie(response: Response, token: str, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=TOKEN_TTL_S,
        path="/",
        httponly=True,
        samesite="strict",
        secure=secure,
    )


def clear_session_cookie(response: Response, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME, "", max_age=0, path="/", httponly=True, samesite="strict", secure=secure
    )


class LoginLimiter:
    """Failed-login throttle (in memory, per process).

    * 10 failures per 5 minutes per (client address, user name), as before;
    * [1.4.1] `ip_max()` failures per 5 minutes per client address across **all** user names
      (password spraying). Only failures count, a success does not reset the address counter
      (one valid account must not buy unlimited guesses), and existing sessions are never
      affected: a busy NAT address can at worst not start *new* password logins for the rest
      of the window. `ip_max` is the setting `login_ip_max_failures` (0 = off).

    `ip_wide=False` keeps a caller (Kerberos SSO, whose automatic attempts fail routinely on
    non-domain machines) out of the address-wide counter."""

    def __init__(
        self,
        max_failures: int = 10,
        window_s: float = 300.0,
        ip_max: Callable[[], int] | None = None,
    ) -> None:
        self.max = max_failures
        self.window = window_s
        self.ip_max = ip_max or (lambda: 0)
        self._fails: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._ip_fails: dict[str, deque[float]] = defaultdict(deque)

    def _trim(self, q: deque[float]) -> deque[float]:
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def _prune(self, key: tuple[str, str]) -> deque[float]:
        return self._trim(self._fails[key])

    def ip_blocked(self, ip: str) -> bool:
        cap = self.ip_max()
        if cap <= 0 or ip not in self._ip_fails:
            return False
        return len(self._trim(self._ip_fails[ip])) >= cap

    def blocked(self, ip: str, username: str, *, ip_wide: bool = True) -> bool:
        if ip_wide and self.ip_blocked(ip):
            return True
        key = (ip, username.lower())
        if key not in self._fails:
            return False
        return len(self._prune(key)) >= self.max

    def fail(self, ip: str, username: str, *, ip_wide: bool = True) -> None:
        key = (ip, username.lower())
        now = time.monotonic()
        self._prune(key).append(now)
        if ip_wide:
            self._trim(self._ip_fails[ip]).append(now)
        if len(self._fails) > 10000:
            for k in list(self._fails)[:5000]:
                if not self._prune(k):
                    del self._fails[k]
        if len(self._ip_fails) > 10000:
            for ip_ in list(self._ip_fails)[:5000]:
                if not self._trim(self._ip_fails[ip_]):
                    del self._ip_fails[ip_]

    def reset(self, ip: str, username: str) -> None:
        self._fails.pop((ip, username.lower()), None)

    def clear(self) -> None:
        self._fails.clear()
        self._ip_fails.clear()
