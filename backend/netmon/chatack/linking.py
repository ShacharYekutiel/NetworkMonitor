"""[3] Telegram account linking with one-time codes (architecture § 13.8.2).

A code is `NM-` + 8 base32 characters, valid 10 minutes, single use, stored as the SHA-256 of
the normalised code (upper-case, no `NM-`). At most 3 unexpired codes per user (the oldest is
replaced); rows are deleted 1 day after expiry."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
CODE_TTL = timedelta(minutes=10)
MAX_ACTIVE = 3
_CODE_RE = re.compile(r"^(?:NM-)?([A-Z2-7]{8})$")
_LINK_RE = re.compile(r"^/link(?:@(\w+))?\s+(\S+)\s*$", re.IGNORECASE)
_UNLINK_RE = re.compile(r"^/unlink(?:@(\w+))?\s*$", re.IGNORECASE)


def new_code() -> str:
    return "NM-" + "".join(secrets.choice(ALPHABET) for _ in range(8))


def normalize(code: str) -> str | None:
    m = _CODE_RE.match((code or "").strip().upper())
    return m.group(1) if m else None


def code_hash(code: str) -> str | None:
    n = normalize(code)
    return hashlib.sha256(n.encode("ascii")).hexdigest() if n else None


def parse_command(text_: str, bot_username: str | None) -> tuple[str, str | None] | None:
    """`/link NM-XXXX` | `/link@Bot NM-XXXX` → ("link", code); `/unlink` → ("unlink", None);
    a command addressed to another bot or anything else → None."""
    t = (text_ or "").strip()
    m = _LINK_RE.match(t)
    if m:
        if m.group(1) and bot_username and m.group(1).lower() != bot_username.lower():
            return None
        return "link", m.group(2)
    m = _UNLINK_RE.match(t)
    if m:
        if m.group(1) and bot_username and m.group(1).lower() != bot_username.lower():
            return None
        return "unlink", None
    return None


async def create_code(
    session: AsyncSession, user_id: int, now: datetime, provider: str = "telegram"
) -> tuple[str, datetime]:
    await session.execute(
        text("DELETE FROM chat_link_codes WHERE expires_at < :cut"),
        {"cut": now - timedelta(days=1)},
    )
    active = (
        (
            await session.execute(
                text(
                    "SELECT code_hash FROM chat_link_codes WHERE user_id = :u AND used_at IS NULL "
                    "AND expires_at > :now ORDER BY expires_at"
                ),
                {"u": user_id, "now": now},
            )
        )
        .scalars()
        .all()
    )
    for h in active[: max(0, len(active) - (MAX_ACTIVE - 1))]:
        await session.execute(text("DELETE FROM chat_link_codes WHERE code_hash = :h"), {"h": h})
    code = new_code()
    expires = now + CODE_TTL
    await session.execute(
        text(
            "INSERT INTO chat_link_codes (code_hash, user_id, provider, expires_at) "
            "VALUES (:h, :u, :p, :e)"
        ),
        {"h": code_hash(code), "u": user_id, "p": provider, "e": expires},
    )
    return code, expires


async def consume(
    session: AsyncSession, code: str, external_id: str, display: str, now: datetime
) -> Any | None:
    """Use a code: link `(user, telegram, external_id)`. Returns the user row or None. The
    caller commits."""
    h = code_hash(code)
    if h is None:
        return None
    row = (
        await session.execute(
            text(
                "UPDATE chat_link_codes SET used_at = :now WHERE code_hash = :h "
                "AND used_at IS NULL AND expires_at > :now RETURNING user_id, provider"
            ),
            {"h": h, "now": now},
        )
    ).first()
    if row is None:
        return None
    user = (
        await session.execute(
            text("SELECT id, username, is_active, role, language FROM users WHERE id = :u"),
            {"u": row.user_id},
        )
    ).first()
    if user is None or not user.is_active:
        return None
    await session.execute(
        text(
            "INSERT INTO chat_identities (user_id, provider, external_id, display, linked_at) "
            "VALUES (:u, :p, :x, :d, :now) ON CONFLICT (provider, external_id) DO UPDATE SET "
            "user_id = EXCLUDED.user_id, display = EXCLUDED.display, linked_at = EXCLUDED.linked_at"
        ),
        {"u": user.id, "p": row.provider, "x": external_id[:64], "d": display[:128], "now": now},
    )
    return user


async def unlink_external(session: AsyncSession, external_id: str) -> Any | None:
    return (
        await session.execute(
            text(
                "DELETE FROM chat_identities WHERE provider = 'telegram' AND external_id = :x "
                "RETURNING id, user_id"
            ),
            {"x": external_id},
        )
    ).first()


def display_of(sender: dict[str, Any]) -> str:
    if sender.get("username"):
        return "@" + str(sender["username"])
    name = " ".join(str(sender.get(k) or "") for k in ("first_name", "last_name")).strip()
    return name or str(sender.get("id") or "")
