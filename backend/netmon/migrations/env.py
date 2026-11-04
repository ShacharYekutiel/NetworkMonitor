"""Alembic environment. Works both programmatically (connection passed in config attributes)
and from the alembic CLI (DATABASE_URL env)."""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from netmon.config import normalize_db_url

config = context.config


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=None, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    url = normalize_db_url(
        os.environ.get("DATABASE_URL", "postgresql://netmon:netmon@localhost:5432/netmon"),
        os.environ.get("NETMON_DB_PASSWORD_FILE"),
    )
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


connection = config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    asyncio.run(_run_async())
