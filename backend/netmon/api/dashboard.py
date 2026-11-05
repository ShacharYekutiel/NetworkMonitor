from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import case, select

from netmon.api.deps import DB, RT, Viewer
from netmon.models import Device, Event, Sensor
from netmon.services.dependencies import effective_parent
from netmon.services.serialize import event_out, sensor_out
from netmon.services.status import empty_counts, load_inventory

router = APIRouter()


@router.get("/api/dashboard")
async def dashboard(user: Viewer, rt: RT, session: DB) -> dict:
    inv = await load_inventory(session)
    maint = rt.maintenance
    reveal = user.role == "admin"
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
            .where(Sensor.status.in_(["down", "warning"]), Sensor.ack_at.is_(None))
            .order_by(sev, Sensor.last_status_change_at.asc().nulls_first(), Sensor.id)
            .limit(20)
        )
    ).all()
    acked = (
        await session.execute(
            select(Sensor, Device)
            .join(Device, Device.id == Sensor.device_id)
            .where(Sensor.status.in_(["down", "warning"]), Sensor.ack_at.isnot(None))
            .order_by(sev, Sensor.ack_at.asc(), Sensor.id)
        )
    ).all()

    def out(s: Sensor, d: Device) -> dict:
        return sensor_out(
            s,
            d.name,
            d.group_id,
            reveal_secrets=reveal,
            in_maintenance=bool(maint is not None and maint.in_maintenance(d.id)),
        )

    unreachable = []
    for did in sorted(
        (d for d in inv.devices if inv.device_status[d] == "unreachable"),
        key=lambda d: (inv.devices[d]["name"].lower(), d),
    )[:50]:
        eff = effective_parent(inv, did)
        unreachable.append(
            {
                "id": did,
                "name": inv.devices[did]["name"],
                "host": inv.devices[did]["host"],
                "parent_device_id": eff.device_id if eff else None,
                "parent_device_name": inv.devices[eff.device_id]["name"] if eff else None,
            }
        )
    return {
        "sensor_counts": sensor_counts,
        "device_counts": device_counts,
        "group_count": len(inv.groups),
        "recent_events": [event_out(e) for e in events],
        "problem_sensors": [out(s, d) for s, d in problems],
        "acknowledged_sensors": [out(s, d) for s, d in acked[:20]],
        "acknowledged_count": len(acked),
        "unreachable_devices": unreachable,
        "maintenance": {
            "active_window_ids": maint.active_window_ids() if maint is not None else [],
            "device_count": len(maint.covered_device_ids()) if maint is not None else 0,
        },
    }
