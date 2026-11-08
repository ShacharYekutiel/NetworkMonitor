"""TimescaleDB bootstrap with automatic plain-PostgreSQL fallback (architecture.md section 6)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

log = logging.getLogger("netmon.db.timescale")


@dataclass
class TimescaleStatus:
    enabled: bool = False
    # False when add_retention_policy is unavailable (e.g. Apache-only TimescaleDB builds):
    # the maintenance loop then prunes with drop_chunks / DELETE.
    retention_policy: bool = False


async def bootstrap(engine: AsyncEngine, retention_days: int, disabled: bool = False) -> bool:
    """Returns True when TimescaleDB is enabled for `measurements`."""
    return (await bootstrap_status(engine, retention_days, disabled)).enabled


async def bootstrap_status(
    engine: AsyncEngine, retention_days: int, disabled: bool = False
) -> TimescaleStatus:
    status = TimescaleStatus()
    async with engine.connect() as conn:
        enabled = await _try_enable(conn, retention_days, disabled, status)
        status.enabled = enabled
        if not enabled:
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_measurements_time_brin "
                    "ON measurements USING brin (time)"
                )
            )
        await conn.commit()
    return status


async def _try_enable(
    conn: AsyncConnection, retention_days: int, disabled: bool, status: TimescaleStatus
) -> bool:
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
        status.retention_policy = True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Cannot set Timescale retention policy (%s); the maintenance loop will prune "
            "old chunks instead",
            _short(exc),
        )
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


async def reapply_retention(engine: AsyncEngine, retention_days: int) -> bool:
    """Returns True when the retention policy is in place."""
    async with engine.connect() as conn:
        try:
            await apply_retention_policy(conn, retention_days)
            await conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Cannot re-apply Timescale retention policy: %s", _short(exc))
            return False


async def drop_old_chunks(engine: AsyncEngine, retention_days: int) -> None:
    """Manual retention for Timescale without the policy API (drop_chunks is Apache-licensed)."""
    days = int(retention_days)
    async with engine.connect() as conn:
        await conn.execute(
            text(f"SELECT drop_chunks('measurements', older_than => INTERVAL '{days} days')")
        )
        await conn.commit()


def _short(exc: Exception) -> str:
    return str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__


# ------------------------------------------------------------------ [2b-3] traps / syslog
INGEST_TABLES = {"traps": "7 days", "syslog_messages": "1 day"}


async def bootstrap_ingest(
    engine: AsyncEngine, enabled: bool, trap_days: int, syslog_days: int
) -> bool:
    """Timescale mode: `traps` (7-day chunks) and `syslog_messages` (1-day chunks) hypertables
    with retention policies (no compression: rows are updated by "assign" / device delete).
    Fallback: BRIN indexes on time. Returns True when both retention policies are in place
    (else the hourly retention loop deletes old rows)."""
    async with engine.connect() as conn:
        if not enabled:
            for table in INGEST_TABLES:
                await conn.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS ix_{table}_time_brin ON {table} "
                        "USING brin (time)"
                    )
                )
            await conn.commit()
            return False
        ok = True
        for table, chunk in INGEST_TABLES.items():
            try:
                async with conn.begin_nested():
                    await conn.execute(
                        text(
                            f"SELECT create_hypertable('{table}', 'time', "
                            f"chunk_time_interval => INTERVAL '{chunk}', if_not_exists => TRUE, "
                            "migrate_data => TRUE, create_default_indexes => FALSE)"
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("create_hypertable(%s) failed: %s", table, _short(exc))
                ok = False
        await conn.commit()
    if not ok:
        return False
    return await reapply_ingest_retention(engine, trap_days, syslog_days)


async def reapply_ingest_retention(engine: AsyncEngine, trap_days: int, syslog_days: int) -> bool:
    ok = True
    async with engine.connect() as conn:
        for table, days in (("traps", trap_days), ("syslog_messages", syslog_days)):
            try:
                async with conn.begin_nested():
                    await conn.execute(
                        text(f"SELECT remove_retention_policy('{table}', if_exists => TRUE)")
                    )
                    await conn.execute(
                        text(f"SELECT add_retention_policy('{table}', INTERVAL '{int(days)} days')")
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("Cannot set the retention policy of %s: %s", table, _short(exc))
                ok = False
        await conn.commit()
    return ok
