"""Password hashing, JWT session cookie, login throttling."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import defaultdict, deque
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from starlette.requests import HTTPConnection
from starlette.responses import Response

COOKIE_NAME = "netmon_session"
TOKEN_TTL_S = 43200
RENEW_BELOW_S = 6 * 3600

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


def make_token(secret: str, user_id: int, role: str, token_version: int) -> str:
    now = int(time.time())
    claims = {
        "sub": str(user_id),
        "role": role,
        "tv": token_version,
        "iat": now,
        "exp": now + TOKEN_TTL_S,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


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
    """10 failures per 5 minutes per (ip, username)."""

    def __init__(self, max_failures: int = 10, window_s: float = 300.0) -> None:
        self.max = max_failures
        self.window = window_s
        self._fails: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def _prune(self, key: tuple[str, str]) -> deque[float]:
        q = self._fails[key]
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, ip: str, username: str) -> bool:
        key = (ip, username.lower())
        if key not in self._fails:
            return False
        return len(self._prune(key)) >= self.max

    def fail(self, ip: str, username: str) -> None:
        key = (ip, username.lower())
        self._prune(key).append(time.monotonic())
        if len(self._fails) > 10000:
            for k in list(self._fails)[:5000]:
                if not self._prune(k):
                    del self._fails[k]

    def reset(self, ip: str, username: str) -> None:
        self._fails.pop((ip, username.lower()), None)
