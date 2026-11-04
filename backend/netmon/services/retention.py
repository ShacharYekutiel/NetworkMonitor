"""Maintenance: measurement retention (fallback mode) and event retention."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy import text

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
                    DELETE FROM measurements WHERE ctid IN (
                        SELECT ctid FROM measurements
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
    async with rt.sessionmaker() as session:
        removed_e = await delete_old_events(session, cfg["event_retention_days"])
        await session.commit()
    if removed_m or removed_e:
        log.info("Retention removed %d measurements and %d events", removed_m, removed_e)
    return {"measurements": removed_m, "events": removed_e}


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
