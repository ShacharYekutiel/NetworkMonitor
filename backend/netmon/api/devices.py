"""Devices and SNMP interface enumeration."""

from __future__ import annotations

from fastapi import APIRouter, Query, Response
from sqlalchemy import or_, select

from netmon.api.common import commit_or_conflict, csv_list, merge_snmp
from netmon.api.deps import DB, RT, Admin, Page, Viewer
from netmon.api.errors import ApiError, not_found, validation_error
from netmon.models import Device, Group, Sensor
from netmon.poller.sensor_types import AUTO_SENSOR_TYPES, default_sensor_name, validate_config
from netmon.poller.snmp_client import SnmpError, as_text, snmp_walk, walk_to_index
from netmon.schemas import DeviceCreate, DevicePatch
from netmon.services import events as events_svc
from netmon.services import settings as settings_svc
from netmon.services.retention import delete_sensor_measurements
from netmon.services.sensors import (
    build_channels,
    effective_credentials,
    sync_device_sensors_status,
)
from netmon.services.serialize import device_out, page
from netmon.services.status import load_inventory

router = APIRouter()


@router.get("/api/devices")
async def list_devices(
    _: Viewer,
    session: DB,
    pg: Page,
    group_id: int | None = None,
    status: str | None = None,
    q: str | None = Query(default=None, max_length=255),
) -> dict:
    stmt = select(Device)
    if group_id is not None:
        stmt = stmt.where(Device.group_id == group_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Device.name.ilike(like), Device.host.ilike(like)))
    rows = (await session.execute(stmt.order_by(Device.name, Device.id))).scalars().all()
    inv = await load_inventory(session)
    statuses = csv_list(status)
    if statuses:
        rows = [d for d in rows if inv.device_status.get(d.id) in statuses]
    total = len(rows)
    sliced = rows[pg.offset : pg.offset + pg.page_size]
    return page([device_out(d, inv) for d in sliced], total, pg.page, pg.page_size)


@router.post("/api/devices", status_code=201)
async def create_device(body: DeviceCreate, _: Admin, rt: RT, session: DB) -> dict:
    if await session.get(Group, body.group_id) is None:
        raise validation_error(["body", "group_id"], "group does not exist")
    bad = [t for t in body.auto_sensors if t not in AUTO_SENSOR_TYPES]
    if bad:
        raise validation_error(
            ["body", "auto_sensors"],
            f"only {sorted(AUTO_SENSOR_TYPES)} can be created automatically",
        )
    d = Device(
        group_id=body.group_id,
        name=body.name.strip(),
        host=body.host,
        description=body.description,
        enabled=body.enabled,
        snmp_credentials=merge_snmp(body.snmp_credentials, None) if body.snmp_credentials else None,
        sort_order=body.sort_order,
    )
    session.add(d)
    await session.flush()
    interval = int(await settings_svc.get(session, "default_interval_s"))
    created: list[Sensor] = []
    for i, type_ in enumerate(dict.fromkeys(body.auto_sensors)):
        cfg = validate_config(type_, {})
        s = Sensor(
            device_id=d.id,
            name=default_sensor_name(type_, cfg),
            type=type_,
            config=cfg,
            interval_s=interval,
            enabled=True,
            status="unknown" if d.enabled else "paused",
            status_message="",
            state={},
            sort_order=i,
            channels=build_channels(type_, {}),
        )
        session.add(s)
        created.append(s)
    await commit_or_conflict(session)
    await session.refresh(d)
    if rt.scheduler:
        for s in created:
            await rt.scheduler.upsert(s.id)
    rt.config_changed("device", d.id, "created")
    for s in created:
        rt.config_changed("sensor", s.id, "created")
    return device_out(d, await load_inventory(session))


@router.get("/api/devices/{device_id}")
async def get_device(device_id: int, _: Viewer, session: DB) -> dict:
    d = await session.get(Device, device_id)
    if d is None:
        raise not_found("Device")
    return device_out(d, await load_inventory(session))


@router.patch("/api/devices/{device_id}")
async def patch_device(device_id: int, body: DevicePatch, _: Admin, rt: RT, session: DB) -> dict:
    d = await session.get(Device, device_id)
    if d is None:
        raise not_found("Device")
    fields = body.model_fields_set
    if "group_id" in fields and body.group_id is not None and body.group_id != d.group_id:
        if await session.get(Group, body.group_id) is None:
            raise validation_error(["body", "group_id"], "group does not exist")
        d.group_id = body.group_id
    if "name" in fields and body.name:
        d.name = body.name.strip()
    if "host" in fields and body.host:
        d.host = body.host
    if "description" in fields and body.description is not None:
        d.description = body.description
    if "sort_order" in fields and body.sort_order is not None:
        d.sort_order = body.sort_order
    if "snmp_credentials" in fields:
        d.snmp_credentials = (
            merge_snmp(body.snmp_credentials, d.snmp_credentials)
            if body.snmp_credentials is not None
            else None
        )
    evs = []
    enabled_changed = "enabled" in fields and body.enabled is not None and body.enabled != d.enabled
    if enabled_changed:
        d.enabled = bool(body.enabled)
        await session.flush()
        evs = await sync_device_sensors_status(session, d)
    await commit_or_conflict(session)
    await session.refresh(d)
    events_svc.published(rt, evs)
    if rt.scheduler:
        await rt.scheduler.reload_device(d.id)
    rt.config_changed("device", d.id, "updated")
    return device_out(d, await load_inventory(session))


@router.delete("/api/devices/{device_id}", status_code=204)
async def delete_device(device_id: int, _: Admin, rt: RT, session: DB) -> Response:
    d = await session.get(Device, device_id)
    if d is None:
        raise not_found("Device")
    sensor_ids = list(
        (await session.execute(select(Sensor.id).where(Sensor.device_id == device_id))).scalars()
    )
    await session.delete(d)
    await session.commit()
    if rt.scheduler:
        rt.scheduler.remove_many(sensor_ids)
    rt.spawn(delete_sensor_measurements(rt, sensor_ids))
    rt.config_changed("device", device_id, "deleted")
    return Response(status_code=204)


IF_STATUS = {
    1: "up",
    2: "down",
    3: "testing",
    4: "unknown",
    5: "dormant",
    6: "notPresent",
    7: "lowerLayerDown",
}
IF_TABLE = "1.3.6.1.2.1.2.2.1"
IF_XTABLE = "1.3.6.1.2.1.31.1.1.1"


def _mac(v: object) -> str | None:
    if isinstance(v, bytes) and v:
        return ":".join(f"{b:02x}" for b in v)
    return None


async def enumerate_interfaces(host: str, creds: dict) -> list[dict]:
    async def col(base: str, n: int) -> dict:
        oid = f"{base}.{n}"
        return walk_to_index(await snmp_walk(host, creds, oid), oid)

    descr = await col(IF_TABLE, 2)
    if not descr:
        raise SnmpError("ifTable not available")
    types, speeds, macs = await col(IF_TABLE, 3), await col(IF_TABLE, 5), await col(IF_TABLE, 6)
    admin, oper = await col(IF_TABLE, 7), await col(IF_TABLE, 8)
    names: dict = {}
    high: dict = {}
    alias: dict = {}
    if creds.get("version") != "v1":
        try:
            names = await col(IF_XTABLE, 1)
            high = await col(IF_XTABLE, 15)
            alias = await col(IF_XTABLE, 18)
        except SnmpError:
            pass
    items = []
    for idx in sorted(descr, key=lambda x: int(x) if x.isdigit() else 0):
        if not idx.isdigit():
            continue
        hs = high.get(idx)
        speed = (
            int(hs) * 1_000_000
            if isinstance(hs, int) and hs > 0
            else (speeds.get(idx) if isinstance(speeds.get(idx), int) else None)
        )
        d_text = as_text(descr.get(idx))
        items.append(
            {
                "if_index": int(idx),
                "name": as_text(names.get(idx)) or d_text,
                "descr": d_text,
                "alias": as_text(alias.get(idx)),
                "type": types.get(idx) if isinstance(types.get(idx), int) else None,
                "speed_bps": speed,
                "admin_status": IF_STATUS.get(admin.get(idx), "unknown"),  # type: ignore[arg-type]
                "oper_status": IF_STATUS.get(oper.get(idx), "unknown"),  # type: ignore[arg-type]
                "mac": _mac(macs.get(idx)),
            }
        )
    return items


@router.get("/api/devices/{device_id}/snmp/interfaces")
async def snmp_interfaces(device_id: int, _: Admin, rt: RT, session: DB) -> dict:
    d = await session.get(Device, device_id)
    if d is None:
        raise not_found("Device")
    creds = await effective_credentials(session, d)
    existing: dict[int, int] = {}
    for s in (
        await session.execute(
            select(Sensor).where(Sensor.device_id == device_id, Sensor.type == "snmp_interface")
        )
    ).scalars():
        try:
            existing.setdefault(int(s.config.get("if_index")), s.id)
        except (TypeError, ValueError):
            pass
    await session.close()
    try:
        async with rt.semaphore:
            items = await enumerate_interfaces(d.host, creds)
    except SnmpError as exc:
        raise ApiError(502, "snmp_error", f"SNMP enumeration failed: {exc}") from exc
    for it in items:
        it["existing_sensor_id"] = existing.get(it["if_index"])
    return {"items": items}
