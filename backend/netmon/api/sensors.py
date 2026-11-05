"""Sensors, sensor types, run/test, history."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import func, select

from netmon.api.common import csv_list, merge_snmp
from netmon.api.deps import DB, RT, Admin, Page, Viewer
from netmon.api.errors import ApiError, not_found, validation_error
from netmon.api.events import list_events_impl
from netmon.models import Device, Sensor
from netmon.poller.processor import execute_check, persist
from netmon.poller.sensor_types import (
    SENSOR_TYPES,
    SENSOR_TYPES_ORDER,
    default_sensor_name,
    get_type,
    validate_channel_overrides,
    validate_config,
)
from netmon.poller.snmp_client import DEFAULT_SNMP
from netmon.poller.types import SEVERITY, CheckContext, evaluate
from netmon.schemas import AcknowledgeIn, SensorCreate, SensorPatch, SensorTestIn
from netmon.services import events as events_svc
from netmon.services import history as history_svc
from netmon.services import settings as settings_svc
from netmon.services.retention import delete_sensor_measurements
from netmon.services.sensors import (
    apply_channel_overrides,
    build_channels,
    build_context,
    default_thresholds,
    effective_credentials,
    set_sensor_status,
    sync_channel_units,
)
from netmon.services.serialize import check_result_out, page, sensor_out
from netmon.services.status import load_inventory

router = APIRouter()


@router.get("/api/sensor-types")
async def sensor_types(_: Viewer) -> dict:
    return {"items": [SENSOR_TYPES[t].to_api() for t in SENSOR_TYPES_ORDER]}


async def _sensor_full(session: DB, sensor_id: int) -> tuple[Sensor, Device]:
    s = await session.get(Sensor, sensor_id)
    if s is None:
        raise not_found("Sensor")
    d = await session.get(Device, s.device_id)
    assert d is not None
    return s, d


def _out(s: Sensor, d: Device, reveal: bool = True, maint: Any = None) -> dict:
    return sensor_out(
        s,
        d.name,
        d.group_id,
        reveal_secrets=reveal,
        in_maintenance=bool(maint is not None and maint.in_maintenance(d.id)),
    )


@router.get("/api/sensors")
async def list_sensors(
    user: Viewer,
    rt: RT,
    session: DB,
    pg: Page,
    device_id: int | None = None,
    group_id: int | None = None,
    status: str | None = None,
    type: str | None = None,  # noqa: A002
    q: str | None = Query(default=None, max_length=255),
    acknowledged: bool | None = None,
) -> dict:
    return await sensors_page(
        session,
        pg.page,
        pg.page_size,
        device_id=device_id,
        group_id=group_id,
        status=csv_list(status),
        types=csv_list(type),
        q=q,
        reveal=user.role == "admin",
        acknowledged=acknowledged,
        maint=rt.maintenance,
    )


async def sensors_page(
    session: DB,
    page_no: int,
    page_size: int,
    *,
    device_id: int | None = None,
    group_id: int | None = None,
    status: list[str] | None = None,
    types: list[str] | None = None,
    q: str | None = None,
    reveal: bool = True,
    acknowledged: bool | None = None,
    maint: Any = None,
) -> dict:
    stmt = select(Sensor, Device).join(Device, Device.id == Sensor.device_id)
    if acknowledged is not None:
        stmt = stmt.where(Sensor.ack_at.isnot(None) if acknowledged else Sensor.ack_at.is_(None))
    if device_id is not None:
        stmt = stmt.where(Sensor.device_id == device_id)
    if group_id is not None:
        inv = await load_inventory(session)
        if group_id not in inv.groups:
            return page([], 0, page_no, page_size)
        stmt = stmt.where(Device.group_id.in_(inv.descendants(group_id)))
    if status:
        stmt = stmt.where(Sensor.status.in_(status))
    if types:
        stmt = stmt.where(Sensor.type.in_(types))
    if q:
        stmt = stmt.where(Sensor.name.ilike(f"%{q}%"))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    if status:
        from sqlalchemy import case

        sev = case(SEVERITY, value=Sensor.status, else_=0)
        stmt = stmt.order_by(
            sev.desc(), Sensor.last_status_change_at.asc().nulls_first(), Device.name, Sensor.name
        )
    else:
        stmt = stmt.order_by(Device.name, Sensor.sort_order, Sensor.name, Sensor.id)
    rows = (await session.execute(stmt.offset((page_no - 1) * page_size).limit(page_size))).all()
    return page([_out(s, d, reveal, maint) for s, d in rows], total, page_no, page_size)


@router.post("/api/sensors", status_code=201)
async def create_sensor(body: SensorCreate, _: Admin, rt: RT, session: DB) -> dict:
    tdef = get_type(body.type)
    device = await session.get(Device, body.device_id)
    if device is None:
        raise validation_error(["body", "device_id"], "device does not exist")
    cfg = validate_config(tdef.type, body.config)
    overrides = validate_channel_overrides(tdef.type, body.channels)
    interval = body.interval_s or int(await settings_svc.get(session, "default_interval_s"))
    s = Sensor(
        device_id=device.id,
        name=(body.name or "").strip() or default_sensor_name(tdef.type, cfg),
        type=tdef.type,
        config=cfg,
        interval_s=interval,
        enabled=body.enabled,
        status="unknown" if (body.enabled and device.enabled) else "paused",
        status_message="",
        state={},
        sort_order=body.sort_order,
        channels=build_channels(tdef.type, overrides, cfg),
    )
    session.add(s)
    await session.commit()
    s, device = await _sensor_full(session, s.id)
    if rt.scheduler:
        await rt.scheduler.upsert(s.id)
    rt.config_changed("sensor", s.id, "created")
    return _out(s, device, maint=rt.maintenance)


@router.get("/api/sensors/{sensor_id}")
async def get_sensor(sensor_id: int, user: Viewer, rt: RT, session: DB) -> dict:
    s, d = await _sensor_full(session, sensor_id)
    return _out(s, d, user.role == "admin", rt.maintenance)


@router.patch("/api/sensors/{sensor_id}")
async def patch_sensor(sensor_id: int, body: SensorPatch, _: Admin, rt: RT, session: DB) -> dict:
    s, d = await _sensor_full(session, sensor_id)
    fields = body.model_fields_set
    if "type" in fields and body.type is not None and body.type != s.type:
        raise ApiError(400, "invalid_sensor_type", "The sensor type cannot be changed")
    if "config" in fields and body.config is not None:
        old = dict(s.config)
        s.config = validate_config(s.type, body.config, base=dict(s.config))
        if s.type == "snmp_interface" and s.config.get("if_index") != old.get("if_index"):
            s.state = {}  # different interface: counters are no longer comparable
        if s.type == "snmp_custom":
            if any(s.config.get(k) != old.get(k) for k in ("oid", "value_type", "percent_of_oid")):
                s.state = {}
            sync_channel_units(s)
    if "channels" in fields and body.channels is not None:
        apply_channel_overrides(s, validate_channel_overrides(s.type, body.channels))
    if "name" in fields and body.name:
        s.name = body.name.strip()
    if "interval_s" in fields and body.interval_s is not None:
        s.interval_s = body.interval_s
    if "sort_order" in fields and body.sort_order is not None:
        s.sort_order = body.sort_order
    evs = []
    if "enabled" in fields and body.enabled is not None and body.enabled != s.enabled:
        s.enabled = body.enabled
        target = "paused" if not (s.enabled and d.enabled) else "unknown"
        ev = await set_sensor_status(
            session,
            s,
            d,
            target,
            in_maintenance=rt.maintenance is not None and rt.maintenance.in_maintenance(d.id),
        )
        if ev is not None:
            evs.append(ev)
    await session.commit()
    events_svc.published(rt, evs)
    s, d = await _sensor_full(session, sensor_id)
    if rt.scheduler:
        await rt.scheduler.upsert(s.id)
    rt.config_changed("sensor", s.id, "updated")
    return _out(s, d, maint=rt.maintenance)


# ---------------------------------------------------------------- [2b-1] acknowledge
def _ack_ws(rt: RT, s: Sensor) -> None:
    from netmon.services.serialize import acknowledgement_out

    rt.hub.broadcast(
        "sensor.ack",
        {"id": s.id, "device_id": s.device_id, "acknowledgement": acknowledgement_out(s)},
    )


@router.post("/api/sensors/{sensor_id}/acknowledge")
async def acknowledge(
    sensor_id: int, user: Admin, rt: RT, session: DB, body: AcknowledgeIn | None = None
) -> dict:
    body = body or AcknowledgeIn()
    s = (
        await session.execute(select(Sensor).where(Sensor.id == sensor_id).with_for_update())
    ).scalar_one_or_none()
    if s is None:
        raise not_found("Sensor")
    d = await session.get(Device, s.device_id)
    assert d is not None
    if s.status not in ("down", "warning"):
        raise ApiError(
            409,
            "not_acknowledgeable",
            f"Only down or warning sensors can be acknowledged (status is {s.status})",
        )
    comment = body.comment.strip()
    s.ack_at = rt.clock.now()
    s.ack_user_id = user.id
    s.ack_by = user.username
    s.ack_comment = comment
    s.ack_status = s.status
    ev = await events_svc.add_event(
        session,
        "acknowledge",
        f"Acknowledged by {user.username}" + (f": {comment}" if comment else ""),
        sensor_id=s.id,
        device_id=d.id,
        group_id=d.group_id,
        sensor_name=s.name,
        device_name=d.name,
        old_status=s.status,
        new_status=s.status,
        in_maintenance=rt.maintenance is not None and rt.maintenance.in_maintenance(d.id),
    )
    await session.commit()
    events_svc.published(rt, [ev])
    _ack_ws(rt, s)
    if rt.alerts is not None:
        await rt.alerts.acknowledged(s.id)
    rt.request_rollup()
    return _out(s, d, maint=rt.maintenance)


@router.delete("/api/sensors/{sensor_id}/acknowledge")
async def unacknowledge(sensor_id: int, user: Admin, rt: RT, session: DB) -> dict:
    s = (
        await session.execute(select(Sensor).where(Sensor.id == sensor_id).with_for_update())
    ).scalar_one_or_none()
    if s is None:
        raise not_found("Sensor")
    d = await session.get(Device, s.device_id)
    assert d is not None
    if s.ack_at is None:
        return _out(s, d, maint=rt.maintenance)
    events_svc.clear_ack(s)
    ev = await events_svc.add_event(
        session,
        "acknowledge",
        f"Acknowledgement removed by {user.username}",
        sensor_id=s.id,
        device_id=d.id,
        group_id=d.group_id,
        sensor_name=s.name,
        device_name=d.name,
        old_status=s.status,
        new_status=s.status,
        in_maintenance=rt.maintenance is not None and rt.maintenance.in_maintenance(d.id),
    )
    await session.commit()
    events_svc.published(rt, [ev])
    _ack_ws(rt, s)
    if rt.alerts is not None:
        await rt.alerts.unacknowledged(s.id)
    rt.request_rollup()
    return _out(s, d, maint=rt.maintenance)


@router.delete("/api/sensors/{sensor_id}", status_code=204)
async def delete_sensor(sensor_id: int, _: Admin, rt: RT, session: DB) -> Response:
    s = await session.get(Sensor, sensor_id)
    if s is None:
        raise not_found("Sensor")
    await session.delete(s)
    await session.commit()
    if rt.scheduler:
        rt.scheduler.remove(sensor_id)
    rt.spawn(delete_sensor_measurements(rt, [sensor_id]))
    rt.config_changed("sensor", sensor_id, "deleted")
    return Response(status_code=204)


@router.post("/api/sensors/test")
async def test_sensor(body: SensorTestIn, _: Admin, rt: RT, session: DB) -> dict:
    tdef = get_type(body.type)
    cfg = validate_config(tdef.type, body.config)
    overrides = validate_channel_overrides(tdef.type, body.channels)
    creds: dict[str, Any] = {}
    if body.device_id is not None:
        device = await session.get(Device, body.device_id)
        if device is None:
            raise validation_error(["body", "device_id"], "device does not exist")
        host = device.host
        if tdef.requires_snmp:
            if body.snmp_credentials is not None:
                creds = {
                    **DEFAULT_SNMP,
                    **merge_snmp(body.snmp_credentials, device.snmp_credentials),
                }
            else:
                creds = await effective_credentials(session, device)
    elif body.host:
        host = body.host
        if tdef.requires_snmp:
            if body.snmp_credentials is not None:
                creds = {**DEFAULT_SNMP, **merge_snmp(body.snmp_credentials, None)}
            else:
                creds = dict(DEFAULT_SNMP)
    else:
        raise validation_error(["body", "device_id"], "device_id or host is required")
    await session.close()
    ctx = CheckContext(None, tdef.type, cfg, host, creds, {}, timeout_s=60.0)
    raw, duration, checked_at = await execute_check(ctx, rt.semaphore)
    ev = evaluate(raw, default_thresholds(tdef.type, overrides, cfg), duration, checked_at)
    return check_result_out(ev)


@router.post("/api/sensors/{sensor_id}/run")
async def run_sensor(sensor_id: int, _: Admin, rt: RT, session: DB) -> dict:
    s, d = await _sensor_full(session, sensor_id)
    if not (s.enabled and d.enabled):
        raise ApiError(
            409, "sensor_paused", "The sensor or its device is paused; resume it to run a check"
        )
    ctx = await build_context(session, s, d)
    await session.close()
    raw, duration, checked_at = await execute_check(ctx, rt.semaphore)
    ev = await persist(rt, sensor_id, raw, duration, checked_at)
    if ev is None:
        raise not_found("Sensor")
    return check_result_out(ev)


def _parse_dt(value: str, loc: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "+"))
    except ValueError as exc:
        raise validation_error(["query", loc], "must be an ISO-8601 timestamp") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


@router.get("/api/sensors/{sensor_id}/history")
async def sensor_history(
    sensor_id: int,
    request: Request,
    _: Viewer,
    rt: RT,
    session: DB,
    range: str | None = None,  # noqa: A002
    channels: str | None = None,
) -> dict:
    s, _d = await _sensor_full(session, sensor_id)
    q_from, q_to = request.query_params.get("from"), request.query_params.get("to")
    if q_from or q_to:
        if not (q_from and q_to):
            raise validation_error(["query", "from"], "from and to must be given together")
        start, end = _parse_dt(q_from, "from"), _parse_dt(q_to, "to")
        if end <= start:
            raise validation_error(["query", "to"], "must be after from")
    else:
        r = range or "24h"
        if r not in history_svc.RANGES:
            raise validation_error(["query", "range"], "must be one of 1h, 24h, 7d, 30d")
        start, end = history_svc.range_bounds(r, datetime.now(UTC))
    tdef = SENSOR_TYPES[s.type]
    units = {c.key: c.unit for c in s.channels}
    wanted = csv_list(channels)
    chan_list = [(c.key, units.get(c.key, c.unit)) for c in tdef.channels]
    if wanted:
        unknown = [w for w in wanted if w not in units]
        if unknown:
            raise validation_error(["query", "channels"], f"unknown channel(s): {unknown}")
        chan_list = [c for c in chan_list if c[0] in wanted]
    return await history_svc.query_history(
        session,
        timescale=rt.timescale_enabled,
        sensor_id=sensor_id,
        channels=chan_list,
        start=start,
        end=end,
    )


@router.get("/api/sensors/{sensor_id}/events")
async def sensor_events(
    sensor_id: int,
    _: Viewer,
    session: DB,
    pg: Page,
    kind: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    if await session.get(Sensor, sensor_id) is None:
        raise not_found("Sensor")
    return await list_events_impl(
        session, pg, sensor_id=sensor_id, kind=kind, status=status, since=since, until=until
    )
