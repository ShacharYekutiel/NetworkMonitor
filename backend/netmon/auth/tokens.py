"""[2b-3] Personal API tokens (architecture § 12.11).

Token text `nmt_` + secrets.token_urlsafe(32) (43 chars); stored as SHA-256 hex (a fast hash is
right for a 256-bit random secret) plus a display prefix (`nmt_` + 8 chars)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.models import ApiToken, User

TOKEN_PREFIX = "nmt_"
DISPLAY_LEN = 8
STAMP_EVERY_S = 60.0
CLEANUP_AFTER_DAYS = 30
# paths a bearer token may never reach (a token cannot mint tokens or change users/passwords)
# [3b] the assistant and the AI features are session-only (questions cost money and belong
# to a person; api.md "[3b] Phase 3b contract")
BARRED_PREFIXES = ("/api/auth/", "/api/users", "/api/tokens", "/api/assistant", "/api/incidents",
                   "/api/aws/")  # fmt: skip
BARRED_PATHS = ("/api/users", "/api/tokens", "/api/device-templates/user")


def generate() -> tuple[str, str, str]:
    """(token text, display prefix, sha256 hex)."""
    secret = secrets.token_urlsafe(32)
    token = TOKEN_PREFIX + secret
    return token, TOKEN_PREFIX + secret[:DISPLAY_LEN], hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_from_header(value: str | None) -> str | None:
    """The token of an `Authorization: Bearer …` header ("" for a malformed bearer header),
    None when the header is absent or another scheme."""
    if not value:
        return None
    scheme, _, rest = value.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return rest.strip()


def is_barred(path: str) -> bool:
    return path.startswith(BARRED_PREFIXES) or path in BARRED_PATHS


def effective_role(token_role: str, owner_role: str) -> str:
    return "admin" if token_role == "admin" and owner_role == "admin" else "viewer"


def state(tok: Any, now: datetime) -> str:
    if tok.revoked_at is not None:
        return "revoked"
    if tok.expires_at <= now:
        return "expired"
    return "active"


def token_out(tok: Any, username: str, now: datetime) -> dict[str, Any]:
    from netmon.services.serialize import iso

    return {
        "id": tok.id,
        "name": tok.name,
        "prefix": tok.prefix,
        "role": tok.role,
        "user_id": tok.user_id,
        "username": username,
        "created_at": iso(tok.created_at),
        "expires_at": iso(tok.expires_at),
        "last_used_at": iso(tok.last_used_at),
        "last_used_ip": tok.last_used_ip,
        "revoked_at": iso(tok.revoked_at),
        "state": state(tok, now),
    }


class InvalidTokenLimiter:
    """20 invalid bearer tokens per 5 minutes per IP → 429."""

    def __init__(self, max_failures: int = 20, window_s: float = 300.0) -> None:
        self.max = max_failures
        self.window = window_s
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, ip: str) -> deque[float]:
        q = self._fails[ip]
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, ip: str) -> bool:
        if ip not in self._fails:
            return False
        return len(self._prune(ip)) >= self.max

    def fail(self, ip: str) -> None:
        self._prune(ip).append(time.monotonic())
        if len(self._fails) > 10000:
            for k in list(self._fails)[:5000]:
                if not self._prune(k):
                    del self._fails[k]

    def clear(self) -> None:
        self._fails.clear()


class TokenFailure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def verify(session: AsyncSession, token: str, now: datetime) -> tuple[User, ApiToken]:
    """The active token and its active owner, else TokenFailure."""
    if not token.startswith(TOKEN_PREFIX) or len(token) > 200:
        raise TokenFailure("token_invalid")
    digest = hash_token(token)
    row = (
        await session.execute(
            select(ApiToken, User)
            .join(User, User.id == ApiToken.user_id)
            .where(ApiToken.token_hash == digest)
        )
    ).first()
    if row is None:
        raise TokenFailure("token_invalid")
    tok, user = row
    if not hmac.compare_digest(tok.token_hash, digest):  # pragma: no cover - defensive
        raise TokenFailure("token_invalid")
    if tok.revoked_at is not None:
        raise TokenFailure("token_revoked")
    if tok.expires_at <= now:
        raise TokenFailure("token_expired")
    if not user.is_active:
        raise TokenFailure("inactive")
    return user, tok


class UseStamper:
    """`last_used_at` / `last_used_ip` written at most once per minute per token."""

    def __init__(self) -> None:
        self._last: dict[int, float] = {}

    def due(self, token_id: int) -> bool:
        last = self._last.get(token_id)
        return last is None or time.monotonic() - last >= STAMP_EVERY_S

    async def stamp(self, rt: Any, token_id: int, ip: str | None) -> None:
        if not self.due(token_id):
            return
        self._last[token_id] = time.monotonic()
        if len(self._last) > 10000:
            self._last.clear()
        async with rt.sessionmaker() as s:
            await s.execute(
                text(
                    "UPDATE api_tokens SET last_used_at = now(), last_used_ip = :ip "
                    "WHERE id = :id AND (last_used_at IS NULL "
                    "OR last_used_at < now() - interval '1 minute')"
                ),
                {"id": token_id, "ip": (ip or None) and ip[:45]},
            )
            await s.commit()

    def clear(self) -> None:
        self._last.clear()


async def cleanup(session: AsyncSession, now: datetime) -> int:
    """Delete tokens expired or revoked more than 30 days ago."""
    cutoff = now - timedelta(days=CLEANUP_AFTER_DAYS)
    res = await session.execute(
        text(
            "DELETE FROM api_tokens WHERE (revoked_at IS NOT NULL AND revoked_at < :c) "
            "OR expires_at < :c"
        ),
        {"c": cutoff},
    )
    return int(res.rowcount or 0)
