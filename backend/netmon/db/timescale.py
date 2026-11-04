"""TimescaleDB bootstrap with automatic plain-PostgreSQL fallback (architecture.md section 6)."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

log = logging.getLogger("netmon.db.timescale")


async def bootstrap(engine: AsyncEngine, retention_days: int, disabled: bool = False) -> bool:
    """Returns True when TimescaleDB is enabled for `measurements`."""
    async with engine.connect() as conn:
        enabled = await _try_enable(conn, retention_days, disabled)
        if not enabled:
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_measurements_time_brin "
                    "ON measurements USING brin (time)"
                )
            )
        await conn.commit()
    return enabled


async def _try_enable(conn: AsyncConnection, retention_days: int, disabled: bool) -> bool:
    if disabled:
        log.info("TimescaleDB disabled by NETMON_DISABLE_TIMESCALE; using plain PostgreSQL")
        return False
    row = await conn.execute(
        text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
    )
    if row.first() is None:
        log.warning("TimescaleDB extension not available; using plain PostgreSQL")
        return False
    try:
        async with conn.begin_nested():
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot enable TimescaleDB (%s); using plain PostgreSQL", _short(exc))
        return False
    try:
        async with conn.begin_nested():
            await conn.execute(
                text(
                    "SELECT create_hypertable('measurements', 'time', "
                    "chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE, "
                    "migrate_data => TRUE)"
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("create_hypertable failed (%s); using plain PostgreSQL", _short(exc))
        return False
    try:
        async with conn.begin_nested():
            await apply_retention_policy(conn, retention_days)
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot set Timescale retention policy: %s", _short(exc))
    try:
        async with conn.begin_nested():
            await conn.execute(
                text(
                    "ALTER TABLE measurements SET (timescaledb.compress, "
                    "timescaledb.compress_segmentby = 'sensor_id, channel')"
                )
            )
            await conn.execute(
                text(
                    "SELECT add_compression_policy('measurements', INTERVAL '7 days', "
                    "if_not_exists => TRUE)"
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.info("Timescale compression not enabled: %s", _short(exc))
    log.info("TimescaleDB enabled for measurements")
    return True


async def apply_retention_policy(conn: AsyncConnection, retention_days: int) -> None:
    days = int(retention_days)
    await conn.execute(text("SELECT remove_retention_policy('measurements', if_exists => TRUE)"))
    await conn.execute(text(f"SELECT add_retention_policy('measurements', INTERVAL '{days} days')"))


async def reapply_retention(engine: AsyncEngine, retention_days: int) -> None:
    async with engine.connect() as conn:
        try:
            await apply_retention_policy(conn, retention_days)
            await conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("Cannot re-apply Timescale retention policy: %s", _short(exc))


def _short(exc: Exception) -> str:
    return str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
