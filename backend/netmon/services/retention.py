"""Maintenance: measurement retention (fallback mode) and event retention."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy import text

from netmon.db import timescale
from netmon.services import settings as settings_svc
from netmon.services.events import delete_old_events

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.retention")
BATCH = 50000


async def delete_old_measurements(rt: Runtime, days: int, batch: int = BATCH) -> int:
    total = 0
    while True:
        async with rt.sessionmaker() as session:
            res = await session.execute(
                text(
                    """
                    -- (tableoid, ctid): ctid alone is not unique across hypertable chunks
                    DELETE FROM measurements
                    WHERE time < now() - make_interval(days => :d)
                      AND (tableoid, ctid) IN (
                        SELECT tableoid, ctid FROM measurements
                        WHERE time < now() - make_interval(days => :d) LIMIT :n)
                    """
                ),
                {"d": int(days), "n": batch},
            )
            await session.commit()
        n = res.rowcount or 0
        total += n
        if n < batch:
            return total
        await asyncio.sleep(0)


async def run_once(rt: Runtime) -> dict[str, int]:
    async with rt.sessionmaker() as session:
        cfg = await settings_svc.get_all(session)
    removed_m = 0
    if not rt.timescale_enabled:
        removed_m = await delete_old_measurements(rt, cfg["retention_days"])
    elif not rt.retention_policy_ok:
        # Timescale without the retention-policy API (Apache-only build): prune manually
        try:
            assert rt.engine is not None
            await timescale.drop_old_chunks(rt.engine, cfg["retention_days"])
        except Exception as exc:  # noqa: BLE001
            log.warning("drop_chunks failed (%s); deleting old rows instead", exc)
        # rows in chunks that straddle the cutoff (and everything when drop_chunks failed)
        removed_m = await delete_old_measurements(rt, cfg["retention_days"])
    async with rt.sessionmaker() as session:
        removed_e = await delete_old_events(session, cfg["event_retention_days"])
        await session.commit()
    if removed_m or removed_e:
        log.info("Retention removed %d measurements and %d events", removed_m, removed_e)
    out = {"measurements": removed_m, "events": removed_e}
    out.update(await _run_2b3_auth(rt))
    out.update(await _run_2b3_ingest(rt, cfg))
    return out


async def delete_old_ingest(rt: Runtime, table: str, days: int, batch: int = BATCH) -> int:
    """[2b-3] traps / syslog_messages older than `days` (fallback mode or no Timescale policy),
    in batches; runs whether or not the feature is on."""
    total = 0
    while True:
        async with rt.sessionmaker() as session:
            res = await session.execute(
                text(
                    f"DELETE FROM {table} WHERE (id, time) IN (SELECT id, time FROM {table} "  # noqa: S608
                    "WHERE time < now() - make_interval(days => :d) LIMIT :n)"
                ),
                {"d": int(days), "n": batch},
            )
            await session.commit()
        n = res.rowcount or 0
        total += n
        if n < batch:
            return total
        await asyncio.sleep(0)


async def _run_2b3_ingest(rt: Runtime, cfg: dict) -> dict[str, int]:
    out = {"traps": 0, "syslog": 0}
    if rt.timescale_enabled and getattr(rt, "ingest_retention_policy_ok", False):
        return out  # Timescale retention policies drop old chunks
    try:
        out["traps"] = await delete_old_ingest(rt, "traps", int(cfg["trap_retention_days"]))
        out["syslog"] = await delete_old_ingest(
            rt, "syslog_messages", int(cfg["syslog_retention_days"])
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Trap/syslog retention failed: %s", exc)
    if out["traps"] or out["syslog"]:
        log.info("Retention removed %d traps and %d syslog messages", out["traps"], out["syslog"])
    return out


async def _run_2b3_auth(rt: Runtime) -> dict[str, int]:
    """[2b-3] Audit retention (runs whether or not the audit log is on) and the cleanup of
    API tokens expired/revoked more than 30 days ago.

    The retention period is read on its own, not from the full settings dict (which also
    holds the SMTP password), so nothing derived from secrets reaches the log line below."""
    from netmon.auth import tokens
    from netmon.services import audit

    out = {"audit": 0, "api_tokens": 0}
    try:
        async with rt.sessionmaker() as session:
            days = int(await settings_svc.get(session, "audit_retention_days") or 365)
        out["audit"] = await audit.purge_old(rt, days)
    except Exception as exc:  # noqa: BLE001
        log.warning("Audit retention failed: %s", exc)
    try:
        async with rt.sessionmaker() as session:
            out["api_tokens"] = await tokens.cleanup(session, rt.clock.now())
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("API token cleanup failed: %s", exc)
    if out["audit"] or out["api_tokens"]:
        log.info(
            "Retention removed %d audit entries and %d API tokens", out["audit"], out["api_tokens"]
        )
    return out


async def delete_sensor_measurements(rt: Runtime, sensor_ids: list[int]) -> None:
    if not sensor_ids:
        return
    try:
        async with rt.sessionmaker() as session:
            await session.execute(
                text("DELETE FROM measurements WHERE sensor_id = ANY(:ids)"), {"ids": sensor_ids}
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot delete measurements of removed sensors: %s", exc)


async def loop(rt: Runtime, interval_s: float = 3600.0) -> None:
    await asyncio.sleep(60)
    while True:
        try:
            await run_once(rt)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Retention run failed")
        await asyncio.sleep(interval_s)
