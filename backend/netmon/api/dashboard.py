from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import case, select

from netmon.api.deps import DB, Viewer
from netmon.models import Device, Event, Sensor
from netmon.services.serialize import event_out, sensor_out
from netmon.services.status import empty_counts, load_inventory

router = APIRouter()


@router.get("/api/dashboard")
async def dashboard(user: Viewer, session: DB) -> dict:
    inv = await load_inventory(session)
    sensor_counts = empty_counts()
    for s in inv.sensors.values():
        sensor_counts[s["status"]] += 1
        sensor_counts["total"] += 1
    device_counts = empty_counts()
    for did in inv.devices:
        device_counts[inv.device_status[did]] += 1
        device_counts["total"] += 1
    events = (
        await session.execute(
            select(Event)
            .where(Event.kind == "status_change")
            .order_by(Event.time.desc(), Event.id.desc())
            .limit(20)
        )
    ).scalars()
    sev = case({"down": 0, "warning": 1}, value=Sensor.status, else_=2)
    problems = (
        await session.execute(
            select(Sensor, Device)
            .join(Device, Device.id == Sensor.device_id)
            .where(Sensor.status.in_(["down", "warning"]))
            .order_by(sev, Sensor.last_status_change_at.asc().nulls_first(), Sensor.id)
            .limit(20)
        )
    ).all()
    return {
        "sensor_counts": sensor_counts,
        "device_counts": device_counts,
        "group_count": len(inv.groups),
        "recent_events": [event_out(e) for e in events],
        "problem_sensors": [
            sensor_out(s, d.name, d.group_id, reveal_secrets=user.role == "admin")
            for s, d in problems
        ],
    }
