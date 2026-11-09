"""[2b-1] Maintenance windows."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Query, Response
from sqlalchemy import select

from netmon.api.common import commit_or_conflict, flush_or_conflict
from netmon.api.deps import DB, RT, Admin, Audit, Page, Viewer
from netmon.api.errors import ApiError, not_found, validation_error
from netmon.models import Device, Group, MaintenanceWindow, User
from netmon.schemas import MaintenanceWindowIn
from netmon.services import audit
from netmon.services import maintenance as msvc
from netmon.services import settings as settings_svc
from netmon.services.serialize import iso, page

router = APIRouter()

ONCE_FIELDS = ("starts_at", "ends_at")
WEEKLY_FIELDS = ("weekdays", "start_time", "duration_min")
MAX_SPAN = timedelta(days=366)


def _hhmm(v: Any) -> str | None:
    if v is None:
        return None
    return f"{v.hour:02d}:{v.minute:02d}"


async def _names(session: DB, windows: list[MaintenanceWindow]) -> dict[str, dict[int, str]]:
    gids = {w.group_id for w in windows if w.group_id is not None}
    dids = {w.device_id for w in windows if w.device_id is not None}
    uids = {w.created_by for w in windows if w.created_by is not None}
    out: dict[str, dict[int, str]] = {"g": {}, "d": {}, "u": {}}
    if gids:
        out["g"] = dict(
            (await session.execute(select(Group.id, Group.name).where(Group.id.in_(gids)))).all()
        )  # type: ignore[arg-type]
    if dids:
        out["d"] = dict(
            (await session.execute(select(Device.id, Device.name).where(Device.id.in_(dids)))).all()
        )  # type: ignore[arg-type]
    if uids:
        out["u"] = dict(
            (await session.execute(select(User.id, User.username).where(User.id.in_(uids)))).all()
        )  # type: ignore[arg-type]
    return out


def window_out(w: MaintenanceWindow, tz: str, now: Any, names: dict[str, dict[int, str]]) -> dict:
    active = msvc.covers(w, tz, now)
    occ = msvc.current_or_next(w, tz, now)
    return {
        "id": w.id,
        "name": w.name,
        "enabled": w.enabled,
        "scope": w.scope,
        "group_id": w.group_id,
        "group_name": names["g"].get(w.group_id) if w.group_id is not None else None,
        "device_id": w.device_id,
        "device_name": names["d"].get(w.device_id) if w.device_id is not None else None,
        "kind": w.kind,
        "starts_at": iso(w.starts_at),
        "ends_at": iso(w.ends_at),
        "weekdays": sorted(w.weekdays) if w.weekdays is not None else None,
        "start_time": _hhmm(w.start_time),
        "duration_min": w.duration_min,
        "timezone": tz,
        "comment": w.comment,
        "active_now": active,
        "occurrence_start": iso(occ[0]) if occ else None,
        "occurrence_end": iso(occ[1]) if occ else None,
        "created_by": names["u"].get(w.created_by) if w.created_by is not None else None,
        "created_at": iso(w.created_at),
        "updated_at": iso(w.updated_at),
    }


async def _tz(session: DB) -> str:
    return str(await settings_svc.get(session, "timezone"))


def _sort_key(item: dict) -> tuple:
    return (
        0 if item["active_now"] else 1,
        0 if item["occurrence_start"] is not None else 1,
        item["occurrence_start"] or "",
        item["id"],
    )


@router.get("/api/maintenance-windows")
async def list_windows(
    _: Viewer,
    rt: RT,
    session: DB,
    pg: Page,
    scope: str | None = None,
    group_id: int | None = None,
    device_id: int | None = None,
    active: bool | None = None,
    include_past: bool = True,
) -> dict:
    stmt = select(MaintenanceWindow)
    if scope is not None:
        if scope not in ("global", "group", "device"):
            raise validation_error(["query", "scope"], "must be global, group or device")
        stmt = stmt.where(MaintenanceWindow.scope == scope)
    if group_id is not None:
        stmt = stmt.where(MaintenanceWindow.group_id == group_id)
    if device_id is not None:
        stmt = stmt.where(MaintenanceWindow.device_id == device_id)
    rows = list((await session.execute(stmt)).scalars())
    tz = await _tz(session)
    now = rt.clock.now()
    names = await _names(session, rows)
    items = [window_out(w, tz, now, names) for w in rows]
    if active is not None:
        items = [i for i in items if i["active_now"] is active]
    if not include_past:
        items = [i for i in items if i["occurrence_start"] is not None]
    items.sort(key=_sort_key)
    return page(items[pg.offset : pg.offset + pg.page_size], len(items), pg.page, pg.page_size)


@router.get("/api/maintenance/active")
async def active_windows(_: Viewer, rt: RT, session: DB) -> dict:
    tz = await _tz(session)
    now = rt.clock.now()
    rows = list((await session.execute(select(MaintenanceWindow))).scalars())
    rows = [w for w in rows if msvc.covers(w, tz, now)]
    names = await _names(session, rows)
    items = sorted((window_out(w, tz, now, names) for w in rows), key=_sort_key)
    covered = rt.maintenance.covered_device_ids() if rt.maintenance is not None else []
    return {"now": iso(now), "windows": items, "device_ids": covered}


async def _validate(session: DB, data: dict[str, Any]) -> None:
    """Cross-field rules on the merged state (422 with loc)."""
    if not data.get("name"):
        raise validation_error(["body", "name"], "field required")
    scope = data.get("scope")
    if scope is None:
        raise validation_error(["body", "scope"], "field required")
    for s, key in (("group", "group_id"), ("device", "device_id")):
        if scope == s:
            if data.get(key) is None:
                raise validation_error(["body", key], f"required for scope {s}")
        elif data.get(key) is not None:
            raise validation_error(["body", key], f"must be null for scope {scope}")
    if scope == "group" and await session.get(Group, data["group_id"]) is None:
        raise validation_error(["body", "group_id"], "group does not exist")
    if scope == "device" and await session.get(Device, data["device_id"]) is None:
        raise validation_error(["body", "device_id"], "device does not exist")
    kind = data.get("kind")
    if kind is None:
        raise validation_error(["body", "kind"], "field required")
    own, other = (ONCE_FIELDS, WEEKLY_FIELDS) if kind == "once" else (WEEKLY_FIELDS, ONCE_FIELDS)
    for key in own:
        if data.get(key) is None:
            raise validation_error(["body", key], f"required for kind {kind}")
    for key in other:
        if data.get(key) is not None:
            raise validation_error(["body", key], f"must be null for kind {kind}")
    if kind == "once":
        if not data["starts_at"] < data["ends_at"]:
            raise validation_error(["body", "ends_at"], "must be after starts_at")
        if data["ends_at"] - data["starts_at"] > MAX_SPAN:
            raise validation_error(["body", "ends_at"], "a window spans at most 366 days")


def _merge(body: MaintenanceWindowIn, stored: dict[str, Any] | None) -> dict[str, Any]:
    given = body.model_fields_set
    data: dict[str, Any] = dict(stored or {})
    for key in given:
        data[key] = getattr(body, key)
    if stored is not None:
        if "scope" in given and body.scope != stored.get("scope"):
            for key in ("group_id", "device_id"):
                if key not in given:
                    data[key] = None
        if "kind" in given and body.kind != stored.get("kind"):
            for key in ONCE_FIELDS + WEEKLY_FIELDS:
                if key not in given:
                    data[key] = None
    if isinstance(data.get("start_time"), str):
        from datetime import time

        hh, mm = data["start_time"].split(":")
        data["start_time"] = time(int(hh), int(mm))
    return data


def _assign(w: MaintenanceWindow, data: dict[str, Any]) -> None:
    w.name = str(data["name"]).strip()
    w.enabled = bool(data.get("enabled", True) if data.get("enabled") is not None else True)
    w.scope = data["scope"]
    w.group_id = data.get("group_id")
    w.device_id = data.get("device_id")
    w.kind = data["kind"]
    w.starts_at = data.get("starts_at")
    w.ends_at = data.get("ends_at")
    w.weekdays = sorted(data["weekdays"]) if data.get("weekdays") is not None else None
    w.start_time = data.get("start_time")
    w.duration_min = data.get("duration_min")
    w.comment = data.get("comment") or ""


def _stored(w: MaintenanceWindow) -> dict[str, Any]:
    return {
        "name": w.name,
        "enabled": w.enabled,
        "scope": w.scope,
        "group_id": w.group_id,
        "device_id": w.device_id,
        "kind": w.kind,
        "starts_at": w.starts_at,
        "ends_at": w.ends_at,
        "weekdays": list(w.weekdays) if w.weekdays is not None else None,
        "start_time": w.start_time,
        "duration_min": w.duration_min,
        "comment": w.comment,
    }


async def _out_one(rt: RT, session: DB, w: MaintenanceWindow) -> dict:
    return window_out(w, await _tz(session), rt.clock.now(), await _names(session, [w]))


def _entity(w: MaintenanceWindow) -> audit.Entity:
    return audit.entity("maintenance_window", w.id, w.name, device_id=w.device_id)


@router.post("/api/maintenance-windows", status_code=201)
async def create_window(
    body: MaintenanceWindowIn, user: Admin, rt: RT, session: DB, ctx: Audit
) -> dict:
    data = _merge(body, None)
    await _validate(session, data)
    w = MaintenanceWindow(created_by=user.id)
    _assign(w, data)
    session.add(w)
    await flush_or_conflict(session)
    await audit.record(
        session, ctx, "maintenance_window.create", _entity(w), audit.created(_stored(w))
    )
    await commit_or_conflict(session)
    await session.refresh(w)
    await _after_change(rt, w.id, "created")
    return await _out_one(rt, session, w)


@router.get("/api/maintenance-windows/{window_id}")
async def get_window(window_id: int, _: Viewer, rt: RT, session: DB) -> dict:
    w = await session.get(MaintenanceWindow, window_id)
    if w is None:
        raise not_found("Maintenance window")
    return await _out_one(rt, session, w)


@router.patch("/api/maintenance-windows/{window_id}")
async def patch_window(
    window_id: int, body: MaintenanceWindowIn, _: Admin, rt: RT, session: DB, ctx: Audit
) -> dict:
    w = await session.get(MaintenanceWindow, window_id)
    if w is None:
        raise not_found("Maintenance window")
    if "name" in body.model_fields_set and body.name is None:
        raise validation_error(["body", "name"], "must not be null")
    before = _stored(w)
    data = _merge(body, before)
    await _validate(session, data)
    _assign(w, data)
    changes = audit.diff(before, _stored(w))
    if changes:
        await audit.record(session, ctx, "maintenance_window.update", _entity(w), changes)
    await commit_or_conflict(session)
    await session.refresh(w)
    await _after_change(rt, w.id, "updated")
    return await _out_one(rt, session, w)


@router.delete("/api/maintenance-windows/{window_id}", status_code=204)
async def delete_window(window_id: int, _: Admin, rt: RT, session: DB, ctx: Audit) -> Response:
    w = await session.get(MaintenanceWindow, window_id)
    if w is None:
        raise not_found("Maintenance window")
    await audit.record(
        session, ctx, "maintenance_window.delete", _entity(w), audit.deleted(_stored(w))
    )
    await session.delete(w)
    await session.commit()
    await _after_change(rt, window_id, "deleted")
    return Response(status_code=204)


@router.get("/api/maintenance-windows/{window_id}/occurrences")
async def window_occurrences(
    window_id: int,
    _: Viewer,
    rt: RT,
    session: DB,
    days: int = Query(default=14, ge=1, le=90),
) -> dict:
    w = await session.get(MaintenanceWindow, window_id)
    if w is None:
        raise not_found("Maintenance window")
    now = rt.clock.now()
    occ = msvc.occurrences(w, await _tz(session), now, now + timedelta(days=days))
    return {"items": [{"start": iso(s), "end": iso(e)} for s, e in occ[:100]]}


async def _after_change(rt: RT, window_id: int, action: str) -> None:
    rt.hub.broadcast(
        "config.changed", {"entity": "maintenance_window", "id": window_id, "action": action}
    )
    if rt.maintenance is not None:
        try:
            await rt.maintenance.reload()
        except Exception as exc:  # noqa: BLE001
            raise ApiError(500, "internal_error", "Maintenance re-evaluation failed") from exc
    rt.request_rollup()
