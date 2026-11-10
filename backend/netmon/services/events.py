"""EventWriter: insert events, publish them, trigger alerts and rollup updates."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.models import Event
from netmon.services.serialize import event_out, now_utc

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.events")


async def add_status_change(
    session: AsyncSession,
    *,
    sensor: Any,
    device: Any,
    old_status: str | None,
    new_status: str,
    message: str = "",
    time: datetime | None = None,
    in_maintenance: bool = False,
) -> Event:
    """Write a status_change event. [2b-1] Any status change clears the sensor's
    acknowledgement (the publisher then broadcasts `sensor.ack` with null)."""
    ack_cleared = False
    if getattr(sensor, "ack_at", None) is not None and old_status != new_status:
        clear_ack(sensor)
        ack_cleared = True
    ev = Event(
        time=time or now_utc(),
        kind="status_change",
        sensor_id=sensor.id,
        device_id=device.id,
        group_id=device.group_id,
        sensor_name=sensor.name,
        device_name=device.name,
        old_status=old_status,
        new_status=new_status,
        message=message or "",
        in_maintenance=bool(in_maintenance),
    )
    session.add(ev)
    await session.flush()
    if ack_cleared:
        ev.ack_cleared = True  # type: ignore[attr-defined]  # transient, read by published()
    return ev


def clear_ack(sensor: Any) -> None:
    sensor.ack_at = None
    sensor.ack_user_id = None
    sensor.ack_by = None
    sensor.ack_comment = ""
    sensor.ack_status = None


async def add_event(session: AsyncSession, kind: str, message: str, **fields: Any) -> Event:
    ev = Event(time=fields.pop("time", None) or now_utc(), kind=kind, message=message, **fields)
    session.add(ev)
    await session.flush()
    return ev


def published(rt: Runtime, events: list[Event]) -> None:
    """Call after commit: broadcast events and hand status changes to the alert engine."""
    changes: list[Event] = []
    change_data: list[dict[str, Any]] = []
    for ev in events:
        data = event_out(ev)
        rt.hub.broadcast("event.created", data)
        if ev.kind == "status_change":
            changes.append(ev)
            change_data.append(data)
            if getattr(ev, "ack_cleared", False):
                rt.hub.broadcast(
                    "sensor.ack",
                    {"id": ev.sensor_id, "device_id": ev.device_id, "acknowledgement": None},
                )
            if rt.alerts is not None:
                rt.alerts.on_status_change(data, ev.time)
    if changes:
        ai = getattr(rt, "ai_features", None)  # [3b] correlation (§ 14.8.2), never the send path
        if ai is not None:
            try:
                ai.on_events(change_data)
            except Exception:  # noqa: BLE001
                log.exception("AI correlation hook failed")
        rt.request_rollup()
        if getattr(rt, "dependencies_enabled", True):
            rt.cascade.submit(changes)


async def write_system_event(rt: Runtime, message: str, **fields: Any) -> None:
    try:
        async with rt.sessionmaker() as session:
            ev = await add_event(session, "system", message, **fields)
            await session.commit()
        published(rt, [ev])
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot write system event: %s", exc)


async def write_notification_event(rt: Runtime, message: str, **fields: Any) -> None:
    try:
        async with rt.sessionmaker() as session:
            ev = await add_event(session, "notification", message, **fields)
            await session.commit()
        published(rt, [ev])
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot write notification event: %s", exc)


async def delete_old_events(session: AsyncSession, days: int) -> int:
    res = await session.execute(
        text("DELETE FROM events WHERE time < now() - make_interval(days => :d)"), {"d": int(days)}
    )
    return res.rowcount or 0
