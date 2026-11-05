"""ResultProcessor: thresholds, measurements, sensor/channel updates, status-change events."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import insert, select

from netmon.models import Device, Event, Sensor
from netmon.poller.checks import PLUGINS
from netmon.poller.types import CheckContext, CheckResult, EvaluatedResult, evaluate
from netmon.services import dependencies
from netmon.services import events as events_svc
from netmon.services.sensors import channel_thresholds, effective_paused
from netmon.services.serialize import iso, now_utc

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.poller.processor")

measurements_table = None


def _measurements():  # type: ignore[no-untyped-def]
    global measurements_table
    if measurements_table is None:
        from sqlalchemy import Column, DateTime, Float, Integer, MetaData, String, Table

        measurements_table = Table(
            "measurements",
            MetaData(),
            Column("time", DateTime(timezone=True)),
            Column("sensor_id", Integer),
            Column("channel", String(64)),
            Column("value", Float),
        )
    return measurements_table


async def execute_check(
    ctx: CheckContext, semaphore: asyncio.Semaphore | None = None
) -> tuple[CheckResult, float, datetime]:
    """Run the plugin with the hard timeout. Returns (raw result, duration_ms, checked_at)."""
    plugin = PLUGINS[ctx.type]

    async def _run() -> CheckResult:
        try:
            return await asyncio.wait_for(plugin(ctx), timeout=ctx.timeout_s)
        except TimeoutError:
            return CheckResult(False, f"Check timed out after {ctx.timeout_s:g} s")
        except Exception as exc:  # noqa: BLE001
            log.exception("Check plugin %s crashed", ctx.type)
            return CheckResult(False, f"Internal check error: {exc}")

    checked_at = now_utc()
    start = time.perf_counter()
    if semaphore is not None:
        async with semaphore:
            checked_at = now_utc()
            start = time.perf_counter()
            raw = await _run()
    else:
        raw = await _run()
    return raw, (time.perf_counter() - start) * 1000.0, checked_at


async def persist(
    rt: Runtime, sensor_id: int, raw: CheckResult, duration_ms: float, checked_at: datetime
) -> EvaluatedResult | None:
    """Evaluate against the stored thresholds and persist in one short transaction."""
    event: Event | None = None
    recheck_parent: list[int] = []
    recheck_self = False
    async with rt.sessionmaker() as session:
        sensor = (
            await session.execute(select(Sensor).where(Sensor.id == sensor_id).with_for_update())
        ).scalar_one_or_none()
        if sensor is None:
            return None
        device = await session.get(Device, sensor.device_id)
        assert device is not None
        ev = evaluate(raw, channel_thresholds(sensor), duration_ms, checked_at)
        if effective_paused(sensor, device):
            # paused while the check was running: discard
            return ev
        if ev.status == "down":
            # [2b-1] a failing child of a down parent is unreachable, not down. A parent that
            # recovered at or after this check started counts as down for this check (the
            # failure may be the outage); the child is then re-checked.
            ps = await dependencies.parent_state(session, device)
            recent = (
                ps is not None
                and not ps.down
                and ps.last_change is not None
                and ps.last_change >= checked_at
                # a change stamped in the future (clock skew) never counts: it would make
                # every re-check "recent" again and loop
                and ps.last_change <= now_utc()
            )
            if ps is not None and (ps.down or recent):
                ev.status = "unreachable"
                ev.message = dependencies.unreachable_message(ps.name, ev.message)
                recheck_self = recent
            elif ps is not None and sensor.status != "down":
                # a new child problem while the parent looks up: re-check the parent now (the
                # child's alert is held until then, see AlertEngine)
                recheck_parent = ps.check_ids
        if ev.ok:
            rows = [
                {"time": checked_at, "sensor_id": sensor.id, "channel": c.key, "value": c.value}
                for c in ev.channels
                if c.value is not None
            ]
            if rows:
                await session.execute(insert(_measurements()), rows)
        by_key = {c.key: c for c in sensor.channels}
        for c in ev.channels:
            row = by_key.get(c.key)
            if row is not None:
                row.last_value = c.value
                row.status = c.status
        old_status = sensor.status
        sensor.status = ev.status
        sensor.status_message = ev.message
        sensor.last_check_at = checked_at
        if ev.new_state is not None:
            sensor.state = ev.new_state
        if ev.config_update:
            sensor.config = {**sensor.config, **ev.config_update}
        if old_status != ev.status:
            sensor.last_status_change_at = checked_at
            event = await events_svc.add_status_change(
                session,
                sensor=sensor,
                device=device,
                old_status=old_status,
                new_status=ev.status,
                message=ev.message,
                time=checked_at,
                in_maintenance=rt.maintenance is not None
                and rt.maintenance.in_maintenance(device.id),
            )
        await session.commit()
        update = {
            "id": sensor.id,
            "device_id": sensor.device_id,
            "status": sensor.status,
            "status_message": sensor.status_message,
            "last_check_at": iso(sensor.last_check_at),
            "status_changed": event is not None,
            "acknowledged": sensor.ack_at is not None,
            "channels": [{"key": c.key, "value": c.value, "status": c.status} for c in ev.channels],
        }
    rt.hub.broadcast("sensor.update", update)
    if event is not None:
        events_svc.published(rt, [event])
    if rt.scheduler is not None:
        if recheck_parent:
            rt.scheduler.run_soon(recheck_parent, jitter_s=0)
        if recheck_self:
            rt.scheduler.run_soon([sensor_id], jitter_s=dependencies.RECHECK_JITTER_S)
    return ev
