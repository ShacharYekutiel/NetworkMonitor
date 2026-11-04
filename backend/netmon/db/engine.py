"""Async engine / session helpers."""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

log = logging.getLogger("netmon.db")


def make_engine(url: str) -> AsyncEngine:
    # hide_parameters: SQL errors never echo bound values (communities, keys) into messages/logs
    return create_async_engine(
        url, pool_size=10, max_overflow=20, pool_pre_ping=True, hide_parameters=True
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def wait_for_db(engine: AsyncEngine, timeout_s: float = 60.0) -> None:
    """Retry connecting for up to timeout_s with exponential backoff."""
    deadline = time.monotonic() + timeout_s
    delay = 0.5
    while True:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # noqa: BLE001
            if time.monotonic() + delay > deadline:
                raise
            log.warning("Database not reachable yet (%s); retrying in %.1fs", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)
