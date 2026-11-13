"""Groups and the tree."""

from __future__ import annotations

from fastapi import APIRouter, Query, Response
from sqlalchemy import func, select

from netmon.api.common import commit_or_conflict, flush_or_conflict, merge_snmp
from netmon.api.deps import DB, RT, Admin, Audit, Page, Viewer
from netmon.api.errors import ApiError, conflict, not_found, validation_error
from netmon.configbackup.credentials import merge as ssh_merge
from netmon.models import Group, Sensor
from netmon.poller.sensor_types import SENSOR_TYPES
from netmon.schemas import GroupCreate, GroupPatch
from netmon.services import audit
from netmon.services import dependencies as deps
from netmon.services.retention import delete_sensor_measurements
from netmon.services.serialize import group_out, iso, page
from netmon.services.status import load_inventory

router = APIRouter()

GROUP_AUDIT_FIELDS = (
    "name",
    "parent_id",
    "description",
    "sort_order",
    "snmp_credentials",
    "dependency_mode",
    "dependency_device_id",
    "dependency_sensor_id",
    "ssh_credentials",  # [3] (encrypted fields are redacted by the audit)
)


def _values(g: Group) -> dict:
    return {k: getattr(g, k) for k in GROUP_AUDIT_FIELDS if hasattr(g, k)}


def _entity(g: Group) -> audit.Entity:
    return audit.entity("group", g.id, g.name)


async def _check_unique(session: DB, parent_id: int | None, name: str, exclude: int | None) -> None:
    q = select(Group.id).where(
        func.coalesce(Group.parent_id, 0) == (parent_id or 0),
        func.lower(Group.name) == name.lower(),
    )
    if exclude is not None:
        q = q.where(Group.id != exclude)
    if (await session.execute(q)).first():
        raise conflict("name")


@router.get("/api/groups")
async def list_groups(
    _: Viewer, session: DB, pg: Page, parent_id: str | None = Query(default=None)
) -> dict:
    q = select(Group)
    if parent_id is not None:
        if parent_id == "root":
            q = q.where(Group.parent_id.is_(None))
        else:
            try:
                q = q.where(Group.parent_id == int(parent_id))
            except ValueError as exc:
                raise validation_error(
                    ["query", "parent_id"], "must be an integer or 'root'"
                ) from exc
    total = (await session.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                q.order_by(Group.sort_order, Group.name).offset(pg.offset).limit(pg.page_size)
            )
        )
        .scalars()
        .all()
    )
    inv = await load_inventory(session)
    return page([group_out(g, inv) for g in rows], total, pg.page, pg.page_size)


@router.post("/api/groups", status_code=201)
async def create_group(body: GroupCreate, _: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    if body.parent_id is not None and await session.get(Group, body.parent_id) is None:
        raise validation_error(["body", "parent_id"], "parent group does not exist")
    await _check_unique(session, body.parent_id, body.name, None)
    dep = ("inherit", None, None)
    if body.dependency is not None:
        dep = await deps.validated_dependency(session, body.dependency, ["body", "dependency"])
    g = Group(
        name=body.name.strip(),
        parent_id=body.parent_id,
        description=body.description,
        snmp_credentials=merge_snmp(body.snmp_credentials, None) if body.snmp_credentials else None,
        sort_order=body.sort_order,
        ssh_credentials=ssh_merge(body.ssh_credentials, None, rt.secret_key)  # [3]
        if body.ssh_credentials is not None
        else None,
    )
    deps.apply_dependency(g, dep)
    session.add(g)
    await deps.check_no_cycle(session)
    await flush_or_conflict(session)
    await audit.record(session, ctx, "group.create", _entity(g), audit.created(_values(g)))
    await commit_or_conflict(session)
    await session.refresh(g)
    rt.config_changed("group", g.id, "created")
    return group_out(g, await load_inventory(session))


@router.get("/api/groups/{group_id}")
async def get_group(group_id: int, _: Viewer, session: DB) -> dict:
    g = await session.get(Group, group_id)
    if g is None:
        raise not_found("Group")
    return group_out(g, await load_inventory(session))


@router.patch("/api/groups/{group_id}")
async def patch_group(
    group_id: int, body: GroupPatch, _: Admin, rt: RT, session: DB, ctx: Audit
) -> dict:
    g = await session.get(Group, group_id)
    if g is None:
        raise not_found("Group")
    before = _values(g)
    fields = body.model_fields_set
    new_parent = body.parent_id if "parent_id" in fields else g.parent_id
    new_name = body.name.strip() if "name" in fields and body.name else g.name
    if "parent_id" in fields and new_parent is not None:
        if await session.get(Group, new_parent) is None:
            raise validation_error(["body", "parent_id"], "parent group does not exist")
        inv = await load_inventory(session)
        if group_id in inv.ancestors(new_parent):
            raise ApiError(400, "group_cycle", "A group cannot be moved below itself")
    if new_parent != g.parent_id or new_name.lower() != g.name.lower():
        await _check_unique(session, new_parent, new_name, g.id)
    g.parent_id = new_parent
    g.name = new_name
    if "description" in fields and body.description is not None:
        g.description = body.description
    if "sort_order" in fields and body.sort_order is not None:
        g.sort_order = body.sort_order
    if "snmp_credentials" in fields:
        g.snmp_credentials = (
            merge_snmp(body.snmp_credentials, g.snmp_credentials)
            if body.snmp_credentials is not None
            else None
        )
    if "ssh_credentials" in fields:  # [3] null = inherit
        new_ssh = (
            ssh_merge(body.ssh_credentials, g.ssh_credentials, rt.secret_key)
            if body.ssh_credentials is not None
            else None
        )
        if new_ssh != g.ssh_credentials:
            g.ssh_credentials = new_ssh
    check_cycle = "parent_id" in fields  # a move changes what devices inherit
    if "dependency" in fields and body.dependency is not None:
        deps.apply_dependency(
            g, await deps.validated_dependency(session, body.dependency, ["body", "dependency"])
        )
        check_cycle = True
    if check_cycle:
        await deps.check_no_cycle(session)
    changes = audit.diff(before, _values(g))
    if changes:
        await audit.record(session, ctx, "group.update", _entity(g), changes)
    await commit_or_conflict(session)
    await session.refresh(g)
    rt.config_changed("group", g.id, "updated")
    return group_out(g, await load_inventory(session))


@router.delete("/api/groups/{group_id}", status_code=204)
async def delete_group(group_id: int, _: Admin, rt: RT, session: DB, ctx: Audit) -> Response:
    g = await session.get(Group, group_id)
    if g is None:
        raise not_found("Group")
    await audit.record(session, ctx, "group.delete", _entity(g), audit.deleted(_values(g)))
    inv = await load_inventory(session)
    gids = set(inv.descendants(group_id))
    sensor_ids = [
        sid for sid, s in inv.sensors.items() if inv.devices[s["device_id"]]["group_id"] in gids
    ]
    await session.delete(g)
    await session.commit()
    if rt.scheduler:
        rt.scheduler.remove_many(sensor_ids)
    rt.spawn(delete_sensor_measurements(rt, sensor_ids))
    rt.config_changed("group", group_id, "deleted")
    return Response(status_code=204)


@router.get("/api/tree")
async def tree(_: Viewer, rt: RT, session: DB) -> dict:
    inv = await load_inventory(session)
    maint = rt.maintenance
    from netmon.models import Device

    devices = {d.id: d for d in (await session.execute(select(Device))).scalars()}
    sensors = (await session.execute(select(Sensor))).scalars().all()
    by_device: dict[int, list[Sensor]] = {}
    for s in sensors:
        by_device.setdefault(s.device_id, []).append(s)

    def sensor_node(s: Sensor) -> dict:
        tdef = SENSOR_TYPES.get(s.type)
        primary = tdef.primary_channel if tdef else ""
        ch = next((c for c in s.channels if c.key == primary), None)
        return {
            "id": s.id,
            "name": s.name,
            "type": s.type,
            "enabled": s.enabled,
            "status": s.status,
            "status_message": s.status_message,
            "primary_channel": primary,
            "primary_value": ch.last_value if ch else None,
            "primary_unit": ch.unit if ch else None,
            "last_check_at": iso(s.last_check_at),
            "acknowledged": s.ack_at is not None,
            "impact": s.impact,  # [1.3]
        }

    def device_node(did: int) -> dict:
        d = devices[did]
        ss = sorted(by_device.get(did, []), key=lambda s: (s.sort_order, s.name.lower()))
        return {
            "id": d.id,
            "name": d.name,
            "host": d.host,
            "enabled": d.enabled,
            "status": inv.device_status[did],
            "status_counts": dict(inv.device_counts[did]),
            "in_maintenance": bool(maint is not None and maint.in_maintenance(did)),
            "parent_device_id": eff.device_id if (eff := deps.effective_parent(inv, did)) else None,
            "sensors": [sensor_node(s) for s in ss],
        }

    def group_node(gid: int, depth: int = 0) -> dict:
        g = inv.groups[gid]
        kids = sorted(
            inv.children.get(gid, []),
            key=lambda c: (inv.groups[c]["sort_order"], inv.groups[c]["name"].lower()),
        )
        devs = sorted(
            inv.group_devices.get(gid, []),
            key=lambda x: (devices[x].sort_order, devices[x].name.lower()),
        )
        return {
            "id": gid,
            "name": g["name"],
            "parent_id": g["parent_id"],
            "status": inv.group_status[gid],
            "status_counts": dict(inv.group_counts[gid]),
            "children": [group_node(c, depth + 1) for c in kids] if depth < 64 else [],
            "devices": [device_node(x) for x in devs],
        }

    roots = sorted(
        inv.children.get(None, []),
        key=lambda c: (inv.groups[c]["sort_order"], inv.groups[c]["name"].lower()),
    )
    return {"groups": [group_node(r) for r in roots]}
