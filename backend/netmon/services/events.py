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
) -> Event:
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
    )
    session.add(ev)
    await session.flush()
    return ev


async def add_event(session: AsyncSession, kind: str, message: str, **fields: Any) -> Event:
    ev = Event(time=fields.pop("time", None) or now_utc(), kind=kind, message=message, **fields)
    session.add(ev)
    await session.flush()
    return ev


def published(rt: Runtime, events: list[Event]) -> None:
    """Call after commit: broadcast events and hand status changes to the alert engine."""
    changed = False
    for ev in events:
        data = event_out(ev)
        rt.hub.broadcast("event.created", data)
        if ev.kind == "status_change":
            changed = True
            if rt.alerts is not None:
                rt.alerts.on_status_change(data)
    if changed:
        rt.request_rollup()


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
