"""Devices and SNMP interface enumeration."""

from __future__ import annotations

from fastapi import APIRouter, Query, Response
from sqlalchemy import or_, select, text, update

from netmon.api.common import commit_or_conflict, csv_list, merge_snmp
from netmon.api.deps import DB, RT, Admin, Page, Viewer
from netmon.api.errors import ApiError, not_found, validation_error
from netmon.models import Device, Group, Sensor
from netmon.poller.sensor_types import AUTO_SENSOR_TYPES, default_sensor_name, validate_config
from netmon.poller.snmp_client import SnmpError
from netmon.schemas import DependencyBulkIn, DeviceCreate, DevicePatch
from netmon.services import dependencies as deps
from netmon.services import events as events_svc
from netmon.services import settings as settings_svc
from netmon.services.retention import delete_sensor_measurements
from netmon.services.sensors import (
    build_channels,
    effective_credentials,
    sync_device_sensors_status,
)
from netmon.services.serialize import device_out, page
from netmon.services.snmp_enum import enumerate_interfaces, enumerate_storage
from netmon.services.status import load_inventory

router = APIRouter()


@router.get("/api/devices")
async def list_devices(
    _: Viewer,
    rt: RT,
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
    return page([device_out(d, inv, rt.maintenance) for d in sliced], total, pg.page, pg.page_size)


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
    dep = ("inherit", None, None)
    if body.dependency is not None:
        dep = await deps.validated_dependency(session, body.dependency, ["body", "dependency"])
    d = Device(
        group_id=body.group_id,
        name=body.name.strip(),
        host=body.host,
        description=body.description,
        enabled=body.enabled,
        snmp_credentials=merge_snmp(body.snmp_credentials, None) if body.snmp_credentials else None,
        sort_order=body.sort_order,
        port_inventory=body.port_inventory,
        arp_collect=body.arp_collect,
    )
    deps.apply_dependency(d, dep)
    session.add(d)
    await session.flush()
    await deps.check_no_cycle(session)
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
    return device_out(d, await load_inventory(session), rt.maintenance)


@router.get("/api/devices/{device_id}")
async def get_device(device_id: int, _: Viewer, rt: RT, session: DB) -> dict:
    d = await session.get(Device, device_id)
    if d is None:
        raise not_found("Device")
    return device_out(d, await load_inventory(session), rt.maintenance)


@router.patch("/api/devices/{device_id}")
async def patch_device(device_id: int, body: DevicePatch, _: Admin, rt: RT, session: DB) -> dict:
    d = await session.get(Device, device_id)
    if d is None:
        raise not_found("Device")
    fields = body.model_fields_set
    check_cycle = False
    identity_changed = False  # [2b-2] host / credentials: class detection must run again
    flags_changed = False
    if "group_id" in fields and body.group_id is not None and body.group_id != d.group_id:
        if await session.get(Group, body.group_id) is None:
            raise validation_error(["body", "group_id"], "group does not exist")
        d.group_id = body.group_id
        check_cycle = True
    if "dependency" in fields and body.dependency is not None:
        deps.apply_dependency(
            d, await deps.validated_dependency(session, body.dependency, ["body", "dependency"])
        )
        check_cycle = True
    if "name" in fields and body.name:
        d.name = body.name.strip()
    if "host" in fields and body.host and body.host != d.host:
        d.host = body.host
        identity_changed = True
        # another host: stored interface counters are no longer comparable
        await session.execute(
            update(Sensor)
            .where(Sensor.device_id == d.id, Sensor.type == "snmp_interface")
            .values(state={})
        )
    if "description" in fields and body.description is not None:
        d.description = body.description
    if "sort_order" in fields and body.sort_order is not None:
        d.sort_order = body.sort_order
    if "snmp_credentials" in fields:
        new_creds = (
            merge_snmp(body.snmp_credentials, d.snmp_credentials)
            if body.snmp_credentials is not None
            else None
        )
        if new_creds != d.snmp_credentials:  # unchanged credentials re-sent by a form: no-op
            d.snmp_credentials = new_creds
            identity_changed = True
    for key in ("port_inventory", "arp_collect"):  # [2b-2]
        value = getattr(body, key)
        if key in fields and value is not None and value != getattr(d, key):
            setattr(d, key, value)
            flags_changed = True
    if identity_changed:
        await session.execute(
            text("UPDATE port_inventory_status SET detected_at = NULL WHERE device_id = :d"),
            {"d": d.id},
        )
    if check_cycle:
        await deps.check_no_cycle(session)
    evs = []
    enabled_changed = "enabled" in fields and body.enabled is not None and body.enabled != d.enabled
    if enabled_changed:
        d.enabled = bool(body.enabled)
        await session.flush()
        evs = await sync_device_sensors_status(
            session,
            d,
            in_maintenance=rt.maintenance is not None and rt.maintenance.in_maintenance(d.id),
        )
    await commit_or_conflict(session)
    await session.refresh(d)
    events_svc.published(rt, evs)
    if rt.scheduler:
        await rt.scheduler.reload_device(d.id)
    if (flags_changed or identity_changed or enabled_changed) and rt.ports is not None:
        rt.ports.mark_due(d.id)
    rt.config_changed("device", d.id, "updated")
    return device_out(d, await load_inventory(session), rt.maintenance)


@router.post("/api/devices/dependencies")
async def bulk_dependencies(body: DependencyBulkIn, _: Admin, rt: RT, session: DB) -> dict:
    """[2b-1] Atomic bulk apply; cycles are checked on the final state."""
    updated: list[int] = []
    for i, item in enumerate(body.items):
        d = await session.get(Device, item.device_id)
        if d is None:
            raise validation_error(["body", "items", i, "device_id"], "device does not exist")
        values = await deps.validated_dependency(
            session, item.dependency, ["body", "items", i, "dependency"]
        )
        if (d.dependency_mode, d.dependency_device_id, d.dependency_sensor_id) != values:
            deps.apply_dependency(d, values)
            updated.append(d.id)
    await deps.check_no_cycle(session)
    await commit_or_conflict(session)
    for did in updated:
        rt.config_changed("device", did, "updated")
    return {"updated": updated}


@router.post("/api/devices/{device_id}/identify")
async def identify(device_id: int, _: Admin, rt: RT, session: DB) -> dict:
    """[2b-1] Re-identify: probe, fingerprint, classify, suggest a template; stored."""
    from netmon.discovery.identify import identify_device

    d = await session.get(Device, device_id)
    if d is None:
        raise not_found("Device")
    creds = await effective_credentials(session, d)
    host = d.host
    await session.close()
    identity = await identify_device(rt, host, creds)
    async with rt.sessionmaker() as s2:
        dev = await s2.get(Device, device_id)
        if dev is None:
            raise not_found("Device")
        dev.identity = identity
        await s2.commit()
    rt.config_changed("device", device_id, "updated")
    return identity


@router.delete("/api/devices/{device_id}", status_code=204)
async def delete_device(device_id: int, _: Admin, rt: RT, session: DB) -> Response:
    d = await session.get(Device, device_id)
    if d is None:
        raise not_found("Device")
    sensor_ids = list(
        (await session.execute(select(Sensor.id).where(Sensor.device_id == device_id))).scalars()
    )
    dependents = await deps.reset_dependents(session, device_id)
    await session.delete(d)
    await session.commit()
    if rt.scheduler:
        rt.scheduler.remove_many(sensor_ids)
    rt.spawn(delete_sensor_measurements(rt, sensor_ids))
    rt.config_changed("device", device_id, "deleted")
    for did in dependents:
        rt.config_changed("device", did, "updated")
    return Response(status_code=204)


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


@router.get("/api/devices/{device_id}/snmp/storage")
async def snmp_storage(device_id: int, _: Admin, rt: RT, session: DB) -> dict:
    d = await session.get(Device, device_id)
    if d is None:
        raise not_found("Device")
    creds = await effective_credentials(session, d)
    existing: dict[str, int] = {}
    for s in (
        await session.execute(
            select(Sensor)
            .where(Sensor.device_id == device_id, Sensor.type == "snmp_disk")
            .order_by(Sensor.id)
        )
    ).scalars():
        descr = s.config.get("storage_descr")
        if isinstance(descr, str):
            existing.setdefault(descr, s.id)
    await session.close()
    try:
        async with rt.semaphore:
            items = await enumerate_storage(d.host, creds)
    except SnmpError as exc:
        raise ApiError(502, "snmp_error", f"SNMP enumeration failed: {exc}") from exc
    for it in items:
        it["existing_sensor_id"] = existing.get(it["descr"])
    return {"items": items}
