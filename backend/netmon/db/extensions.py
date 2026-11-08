"""[2b-3] Optional PostgreSQL extensions: pg_trgm for trap/syslog text search
(data-model "Text search: trigram, not tsvector")."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger("netmon.db.extensions")

TRGM_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_traps_summary_trgm ON traps USING gin (summary gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS ix_syslog_message_trgm ON syslog_messages "
    "USING gin (message gin_trgm_ops)",
)


async def ensure_trgm(engine: AsyncEngine) -> bool:
    """CREATE EXTENSION pg_trgm + the two GIN indexes, in a savepoint. False (WARNING) when
    the extension is unavailable: text search then scans the time range sequentially."""
    async with engine.connect() as conn:
        try:
            async with conn.begin_nested():
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                for stmt in TRGM_INDEXES:
                    await conn.execute(text(stmt))
            await conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "pg_trgm is not available (%s); trap/syslog text search uses sequential scans",
                str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__,
            )
            await conn.rollback()
            return False
