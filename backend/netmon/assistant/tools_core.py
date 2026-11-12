"""[3b] Read-only tools: inventory, status, events, maintenance, topology, ports, MAC/IP search,
availability, groups (api.md § [3b] Tools). Each handler returns an explicit projection; every
item carries a ``ref``."""

from __future__ import annotations

from collections import deque
from datetime import timedelta
from typing import Any, Literal

from pydantic import Field, model_validator
from sqlalchemy import text

from netmon.assistant.tools import (
    MESSAGE_CAP,
    Args,
    ToolCtx,
    ToolDef,
    ToolError,
    ToolOutput,
    cap,
    group_scope,
    iso,
    register,
    require_device,
    time_range,
)

Status = Literal["up", "warning", "down", "unreachable", "paused", "unknown"]
PROBLEM = ("down", "warning", "unreachable")


async def _inventory(ctx: ToolCtx) -> Any:
    from netmon.services.status import load_inventory

    return await load_inventory(ctx.session)


def _group_path(inv: Any, group_id: int | None) -> str:
    if group_id is None:
        return ""
    names = [inv.groups[g]["name"] for g in reversed(inv.ancestors(group_id))]
    return cap(" / ".join(names), 200)


def _in_maintenance(ctx: ToolCtx, device_id: int) -> bool:
    m = getattr(ctx.rt, "maintenance", None)
    try:
        return bool(m is not None and m.in_maintenance(device_id))
    except Exception:  # noqa: BLE001
        return False


def _ident(identity: Any, key: str) -> Any:
    return identity.get(key) if isinstance(identity, dict) else None


# --------------------------------------------------------------------------- search_devices
class SearchDevicesArgs(Args):
    query: str | None = Field(None, max_length=100, description="name or host substring")
    group_id: int | None = Field(None, description="group (and its subgroups)")
    status: list[Status] | None = Field(None, description="device statuses")
    vendor: str | None = Field(None, max_length=50)
    limit: int = Field(20, ge=1, le=50)


def project_device(d: Any, inv: Any, ctx: ToolCtx) -> dict[str, Any]:
    did = int(d["id"])
    ident = d.get("identity")
    acked = sum(
        1
        for sid in inv.device_sensors.get(did, [])
        if inv.sensors[sid].get("acknowledged") and inv.sensors[sid]["status"] in PROBLEM
    )
    counts = inv.device_counts.get(did, {})
    return {
        "id": did,
        "ref": f"device/{did}",
        "name": cap(d["name"]),
        "host": cap(d["host"]),
        "group_path": _group_path(inv, d["group_id"]),
        "status": inv.device_status.get(did, "unknown"),
        "status_counts": {k: int(v) for k, v in counts.items()},
        "vendor": cap(_ident(ident, "vendor"), 50),
        "model": cap(_ident(ident, "model"), 100),
        "sys_descr": cap(_ident(ident, "sys_descr"), 200),
        "in_maintenance": _in_maintenance(ctx, did),
        "acknowledged_problems": acked,
    }


async def search_devices(ctx: ToolCtx, a: SearchDevicesArgs) -> ToolOutput:
    inv = await _inventory(ctx)
    conds = ["true"]
    p: dict[str, Any] = {}
    if a.query:
        conds.append("(d.name ILIKE :q OR d.host ILIKE :q)")
        p["q"] = f"%{_like(a.query)}%"
    if a.group_id is not None:
        conds.append("d.id = ANY(:scope)")
        p["scope"] = await group_scope(ctx.session, a.group_id)
    if a.vendor:
        conds.append("(d.identity->>'vendor') ILIKE :v")
        p["v"] = f"%{_like(a.vendor)}%"
    rows = (
        (
            await ctx.session.execute(
                text(
                    "SELECT d.id, d.name, d.host, d.group_id, d.identity FROM devices d "  # noqa: S608
                    f"WHERE {' AND '.join(conds)} ORDER BY lower(d.name), d.id"
                ),
                p,
            )
        )
        .mappings()
        .all()
    )
    items = []
    for r in rows:
        if a.status and inv.device_status.get(int(r["id"]), "unknown") not in a.status:
            continue
        items.append(project_device(dict(r), inv, ctx))
    total = len(items)
    return ToolOutput(
        {"items": items[: a.limit], "total": total}, min(total, a.limit), truncated=total > a.limit
    )


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------- get_device
class GetDeviceArgs(Args):
    device_id: int


async def _primary_values(ctx: ToolCtx, sensor_ids: list[int]) -> dict[int, tuple[Any, str]]:
    from netmon.poller.sensor_types import SENSOR_TYPES

    out: dict[int, tuple[Any, str]] = {}
    if not sensor_ids:
        return out
    rows = (
        await ctx.session.execute(
            text(
                "SELECT s.id, s.type, c.key, c.unit, c.last_value FROM sensors s "
                "JOIN sensor_channels c ON c.sensor_id = s.id WHERE s.id = ANY(:ids)"
            ),
            {"ids": sensor_ids},
        )
    ).all()
    for r in rows:
        tdef = SENSOR_TYPES.get(r.type)
        if tdef is not None and r.key == tdef.primary_channel:
            out[int(r.id)] = (r.last_value, r.unit)
        elif int(r.id) not in out:
            out[int(r.id)] = (r.last_value, r.unit)
    return out


async def get_device(ctx: ToolCtx, a: GetDeviceArgs) -> ToolOutput:
    from netmon.services.dependencies import effective_parent

    inv = await _inventory(ctx)
    row = (
        (
            await ctx.session.execute(
                text("SELECT id, name, host, group_id, identity FROM devices WHERE id = :id"),
                {"id": a.device_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ToolError("not_found", f"device {a.device_id} not found")
    out = project_device(dict(row), inv, ctx)
    srows = (
        (
            await ctx.session.execute(
                text(
                    "SELECT id, name, type, status, status_message, last_check_at, ack_at, ack_by, "
                    "ack_comment, last_status_change_at FROM sensors WHERE device_id = :d "
                    "ORDER BY sort_order, id LIMIT 101"
                ),
                {"d": a.device_id},
            )
        )
        .mappings()
        .all()
    )
    truncated = len(srows) > 100
    srows = srows[:100]
    values = await _primary_values(ctx, [int(s["id"]) for s in srows])
    sensors = []
    problems = []
    for s in srows:
        val, unit = values.get(int(s["id"]), (None, ""))
        sensors.append(
            {
                "id": int(s["id"]),
                "ref": f"sensor/{s['id']}",
                "name": cap(s["name"]),
                "type": s["type"],
                "status": s["status"],
                "message": cap(s["status_message"], MESSAGE_CAP),
                "last_check_at": iso(s["last_check_at"]),
                "primary_value": val,
                "unit": unit,
            }
        )
        if s["status"] in PROBLEM:
            problems.append(
                {
                    "sensor_id": int(s["id"]),
                    "ref": f"sensor/{s['id']}",
                    "sensor_name": cap(s["name"]),
                    "status": s["status"],
                    "message": cap(s["status_message"], MESSAGE_CAP),
                    "since": iso(s["last_status_change_at"]),
                    "acknowledged": s["ack_at"] is not None,
                }
            )
    depends_on = []
    eff = effective_parent(inv, a.device_id)
    seen: set[int] = set()
    while eff is not None and eff.device_id not in seen and len(depends_on) < 16:
        seen.add(eff.device_id)
        pid = eff.device_id
        depends_on.append(
            {
                "id": pid,
                "ref": f"device/{pid}",
                "name": cap(inv.devices[pid]["name"]),
                "status": inv.device_status.get(pid, "unknown"),
            }
        )
        eff = effective_parent(inv, pid)
    dependents = sum(
        1
        for did in inv.devices
        if (e := effective_parent(inv, did)) is not None and e.device_id == a.device_id
    )
    maint = None
    m = getattr(ctx.rt, "maintenance", None)
    if m is not None and _in_maintenance(ctx, a.device_id):
        ref = m.maintenance_ref(a.device_id)
        if ref:
            maint = {
                "window_id": ref.get("window_id"),
                "ref": f"maintenance/{ref.get('window_id')}",
                "name": cap(ref.get("name")),
                "ends_at": ref.get("ends_at"),
            }
    out.update(
        sensors=sensors,
        problems=problems,
        depends_on=depends_on,
        dependents_count=dependents,
        maintenance_now=maint,
    )
    return ToolOutput(out, len(sensors), truncated=truncated)


# --------------------------------------------------------------------------- list_problems
class ListProblemsArgs(Args):
    group_id: int | None = None
    include_acknowledged: bool = True
    statuses: list[Literal["down", "warning", "unreachable"]] | None = None
    limit: int = Field(20, ge=1, le=100)


async def list_problems(ctx: ToolCtx, a: ListProblemsArgs) -> ToolOutput:
    statuses = list(a.statuses or PROBLEM)
    conds = ["s.status = ANY(:st)", "s.enabled", "d.enabled"]
    p: dict[str, Any] = {"st": statuses}
    if a.group_id is not None:
        conds.append("d.id = ANY(:scope)")
        p["scope"] = await group_scope(ctx.session, a.group_id)
    if not a.include_acknowledged:
        conds.append("s.ack_at IS NULL")
    rows = (
        (
            await ctx.session.execute(
                text(
                    "SELECT s.id, s.name, s.status, s.status_message, s.last_status_change_at, "  # noqa: S608
                    "s.ack_at, s.ack_comment, d.id AS device_id, d.name AS device_name "
                    "FROM sensors s JOIN devices d ON d.id = s.device_id "
                    f"WHERE {' AND '.join(conds)} "
                    "ORDER BY CASE s.status WHEN 'down' THEN 0 WHEN 'unreachable' THEN 1 "
                    "ELSE 2 END, "
                    "s.last_status_change_at DESC NULLS LAST, s.id LIMIT :lim"
                ),
                {**p, "lim": a.limit + 1},
            )
        )
        .mappings()
        .all()
    )
    items = [
        {
            "sensor_id": int(r["id"]),
            "ref": f"sensor/{r['id']}",
            "sensor_name": cap(r["name"]),
            "device_id": int(r["device_id"]),
            "device_ref": f"device/{r['device_id']}",
            "device_name": cap(r["device_name"]),
            "status": r["status"],
            "message": cap(r["status_message"], MESSAGE_CAP),
            "since": iso(r["last_status_change_at"]),
            "acknowledged": r["ack_at"] is not None,
            "ack_comment": cap(r["ack_comment"] or "", 200) or None,
        }
        for r in rows[: a.limit]
    ]
    return ToolOutput({"items": items}, len(items), truncated=len(rows) > a.limit)


# --------------------------------------------------------------------------- search_sensors
class SearchSensorsArgs(Args):
    query: str | None = Field(None, max_length=100)
    device_id: int | None = None
    group_id: int | None = None
    type: str | None = Field(None, max_length=64)
    status: list[Status] | None = None
    limit: int = Field(20, ge=1, le=100)


async def search_sensors(ctx: ToolCtx, a: SearchSensorsArgs) -> ToolOutput:
    conds = ["true"]
    p: dict[str, Any] = {}
    if a.query:
        conds.append("s.name ILIKE :q")
        p["q"] = f"%{_like(a.query)}%"
    if a.device_id is not None:
        conds.append("s.device_id = :did")
        p["did"] = a.device_id
    if a.group_id is not None:
        conds.append("s.device_id = ANY(:scope)")
        p["scope"] = await group_scope(ctx.session, a.group_id)
    if a.type:
        conds.append("s.type = :t")
        p["t"] = a.type
    if a.status:
        conds.append("s.status = ANY(:st)")
        p["st"] = list(a.status)
    rows = (
        (
            await ctx.session.execute(
                text(
                    "SELECT s.id, s.name, s.type, s.status, s.status_message, s.last_check_at, "  # noqa: S608
                    "d.name AS device_name, d.id AS device_id FROM sensors s "
                    f"JOIN devices d ON d.id = s.device_id WHERE {' AND '.join(conds)} "
                    "ORDER BY lower(d.name), s.sort_order, s.id LIMIT :lim"
                ),
                {**p, "lim": a.limit + 1},
            )
        )
        .mappings()
        .all()
    )
    items = [
        {
            "id": int(r["id"]),
            "ref": f"sensor/{r['id']}",
            "name": cap(r["name"]),
            "type": r["type"],
            "device_name": cap(r["device_name"]),
            "device_ref": f"device/{r['device_id']}",
            "status": r["status"],
            "message": cap(r["status_message"], MESSAGE_CAP),
            "last_check_at": iso(r["last_check_at"]),
        }
        for r in rows[: a.limit]
    ]
    return ToolOutput({"items": items}, len(items), truncated=len(rows) > a.limit)


# --------------------------------------------------------------------------- get_sensor_history
class SensorHistoryArgs(Args):
    sensor_id: int
    since: str | None = None
    until: str | None = None
    channel: str | None = Field(None, max_length=64)


async def get_sensor_history(ctx: ToolCtx, a: SensorHistoryArgs) -> ToolOutput:
    start, end = time_range(a.since, a.until, ctx.now, max_days=31)
    srow = (
        await ctx.session.execute(
            text("SELECT id, name, device_id FROM sensors WHERE id = :id"), {"id": a.sensor_id}
        )
    ).first()
    if srow is None:
        raise ToolError("not_found", f"sensor {a.sensor_id} not found")
    chans = (
        await ctx.session.execute(
            text(
                "SELECT key, unit, last_value FROM sensor_channels WHERE sensor_id = :s ORDER BY id"
            ),
            {"s": a.sensor_id},
        )
    ).all()
    if a.channel:
        chans = [c for c in chans if c.key == a.channel]
        if not chans:
            raise ToolError("not_found", f"channel {a.channel} not found")
    span = max(1.0, (end - start).total_seconds())
    bucket = max(60, int(-(-span // 48)))
    timescale = bool(getattr(ctx.rt, "timescale_enabled", False))
    width = "(CAST(:bucket AS integer) * INTERVAL '1 second')"
    origin = "TIMESTAMPTZ '2000-01-01 00:00:00+00'"
    expr = (
        f"time_bucket({width}, time, origin => {origin})"
        if timescale
        else f"date_bin({width}, time, {origin})"
    )
    out = []
    for c in chans:
        agg = (
            await ctx.session.execute(
                text(
                    "SELECT min(value) AS mn, avg(value) AS av, max(value) AS mx, count(*) AS n "
                    "FROM measurements WHERE sensor_id = :s AND channel = :c "
                    "AND time >= :a AND time <= :b"
                ),
                {"s": a.sensor_id, "c": c.key, "a": start, "b": end},
            )
        ).first()
        buckets = (
            await ctx.session.execute(
                text(
                    f"SELECT {expr} AS bk, avg(value) AS av FROM measurements "  # noqa: S608
                    "WHERE sensor_id = :s AND channel = :c AND time >= :a AND time <= :b "
                    "GROUP BY 1 ORDER BY 1"
                ),
                {"s": a.sensor_id, "c": c.key, "a": start, "b": end, "bucket": bucket},
            )
        ).all()
        out.append(
            {
                "key": c.key,
                "unit": c.unit,
                "min": _num(agg.mn) if agg else None,
                "avg": _num(agg.av) if agg else None,
                "max": _num(agg.mx) if agg else None,
                "last": _num(c.last_value),
                "samples": int(agg.n) if agg else 0,
                "buckets": [[iso(b.bk), _num(b.av)] for b in buckets[-48:]],
            }
        )
    data = {
        "sensor_id": a.sensor_id,
        "ref": f"sensor/{a.sensor_id}",
        "name": cap(srow.name),
        "device_ref": f"device/{srow.device_id}",
        "since": iso(start),
        "until": iso(end),
        "bucket_s": bucket,
        "channels": out,
    }
    return ToolOutput(data, len(out))


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- list_events
class ListEventsArgs(Args):
    device_id: int | None = None
    sensor_id: int | None = None
    group_id: int | None = None
    kinds: list[str] | None = Field(None, max_length=20)
    status: str | None = Field(None, max_length=20, description="new status")
    since: str | None = None
    until: str | None = None
    limit: int = Field(20, ge=1, le=200)


async def list_events(ctx: ToolCtx, a: ListEventsArgs) -> ToolOutput:
    start, end = time_range(a.since, a.until, ctx.now, max_days=31)
    conds = ["e.time >= :a", "e.time <= :b"]
    p: dict[str, Any] = {"a": start, "b": end}
    if a.device_id is not None:
        conds.append("e.device_id = :did")
        p["did"] = a.device_id
    if a.sensor_id is not None:
        conds.append("e.sensor_id = :sid")
        p["sid"] = a.sensor_id
    if a.group_id is not None:
        conds.append("e.device_id = ANY(:scope)")
        p["scope"] = await group_scope(ctx.session, a.group_id)
    if a.kinds:
        conds.append("e.kind = ANY(:k)")
        p["k"] = [k[:40] for k in a.kinds]
    if a.status:
        conds.append("e.new_status = :st")
        p["st"] = a.status
    rows = (
        (
            await ctx.session.execute(
                text(
                    "SELECT e.id, e.time, e.kind, e.device_id, e.sensor_id, e.device_name, "  # noqa: S608
                    "e.sensor_name, e.old_status, e.new_status, e.message FROM events e "
                    f"WHERE {' AND '.join(conds)} ORDER BY e.time DESC, e.id DESC LIMIT :lim"
                ),
                {**p, "lim": a.limit + 1},
            )
        )
        .mappings()
        .all()
    )
    items = [
        {
            "id": int(r["id"]),
            "ref": f"event/{r['id']}",
            "time": iso(r["time"]),
            "kind": r["kind"],
            "device_name": cap(r["device_name"]),
            "device_ref": f"device/{r['device_id']}" if r["device_id"] else None,
            "sensor_name": cap(r["sensor_name"]),
            "sensor_ref": f"sensor/{r['sensor_id']}" if r["sensor_id"] else None,
            "old_status": r["old_status"],
            "new_status": r["new_status"],
            "message": cap(r["message"], MESSAGE_CAP),
        }
        for r in rows[: a.limit]
    ]
    return ToolOutput(
        {"since": iso(start), "until": iso(end), "items": items},
        len(items),
        truncated=len(rows) > a.limit,
    )


# --------------------------------------------------------------------------- list_maintenance
class ListMaintenanceArgs(Args):
    device_id: int | None = None
    group_id: int | None = None
    active_only: bool = False
    since: str | None = None
    until: str | None = None


async def list_maintenance(ctx: ToolCtx, a: ListMaintenanceArgs) -> ToolOutput:
    from netmon.services import maintenance as msvc

    if a.since is None and a.until is None:
        start, end = ctx.now - timedelta(days=7), ctx.now + timedelta(days=8)
    else:
        start, end = time_range(a.since, a.until, ctx.now, max_days=92, default=timedelta(days=7))
    rows = (
        (
            await ctx.session.execute(
                text(
                    "SELECT w.*, d.name AS device_name, g.name AS group_name "
                    "FROM maintenance_windows w LEFT JOIN devices d ON d.id = w.device_id "
                    "LEFT JOIN groups g ON g.id = w.group_id ORDER BY w.id"
                )
            )
        )
        .mappings()
        .all()
    )
    chain: list[int] = []
    if a.device_id is not None:
        dev = await require_device(ctx.session, a.device_id)
        inv = await _inventory(ctx)
        chain = inv.ancestors(dev.group_id)
    scope_groups: set[int] = set()
    scope_devices: set[int] = set()
    if a.group_id is not None:
        scope_devices = set(await group_scope(ctx.session, a.group_id))
        inv = await _inventory(ctx)
        if a.group_id not in inv.groups:
            raise ToolError("not_found", f"group {a.group_id} not found")
        scope_groups = set(inv.descendants(a.group_id)) | set(inv.ancestors(a.group_id))
    items = []
    for w in rows:
        if a.device_id is not None and not msvc.window_covers_device(w, a.device_id, chain):
            continue
        if a.group_id is not None and not (
            w["scope"] == "global"
            or (w["scope"] == "group" and w["group_id"] in scope_groups)
            or (w["scope"] == "device" and w["device_id"] in scope_devices)
        ):
            continue
        active = msvc.covers(w, ctx.timezone, ctx.now)
        if a.active_only and not active:
            continue
        occ = msvc.occurrences(w, ctx.timezone, start, end)
        cur = msvc.current_or_next(w, ctx.timezone, ctx.now)
        if not occ and not active:
            continue
        recurrence = (
            msvc.describe_weekly(w["weekdays"] or [], w["start_time"], w["duration_min"] or 0)
            if w["kind"] == "weekly" and w["start_time"] is not None
            else "once"
        )
        target = (
            w["device_name"]
            or w["group_name"]
            or ("everything" if w["scope"] == "global" else None)
        )
        items.append(
            {
                "id": int(w["id"]),
                "ref": f"maintenance/{w['id']}",
                "name": cap(w["name"]),
                "enabled": bool(w["enabled"]),
                "scope": w["scope"],
                "target_name": cap(target),
                "target_ref": (
                    f"device/{w['device_id']}"
                    if w["device_id"]
                    else f"group/{w['group_id']}"
                    if w["group_id"]
                    else None
                ),
                "occurrence": {"start": iso(cur[0]), "end": iso(cur[1])} if cur else None,
                "occurrences_in_range": len(occ),
                "recurrence": recurrence,
                "active": active,
                "comment": cap(w["comment"] or "", 200),
            }
        )
        if len(items) >= 50:
            break
    return ToolOutput(
        {"since": iso(start), "until": iso(end), "items": items},
        len(items),
        truncated=len(items) >= 50,
    )


# --------------------------------------------------------------------------- topology
class NeighborsArgs(Args):
    device_id: int
    include_stale: bool = False


async def _links(ctx: ToolCtx, include_stale: bool) -> list[Any]:
    return list(
        (
            await ctx.session.execute(
                text(
                    "SELECT l.*, n.sys_name AS nb_sys_name, n.chassis_id AS nb_chassis "
                    "FROM topology_links l "
                    "LEFT JOIN topology_neighbors n ON n.id = l.b_neighbor_id "
                    + ("" if include_stale else "WHERE NOT l.stale ")
                    + "ORDER BY l.id"
                )
            )
        )
        .mappings()
        .all()
    )


async def topology_neighbors(ctx: ToolCtx, a: NeighborsArgs) -> ToolOutput:
    await require_device(ctx.session, a.device_id)
    inv = await _inventory(ctx)
    items = []
    for ln in await _links(ctx, a.include_stale):
        if ln["a_device_id"] == a.device_id:
            local, remote = ln["a_port"], ln["b_port"]
            nid = ln["b_device_id"]
            local_if = ln["a_if_index"]
        elif ln["b_device_id"] == a.device_id:
            local, remote = ln["b_port"], ln["a_port"]
            nid = ln["a_device_id"]
            local_if = ln["b_if_index"]
        else:
            continue
        if nid is not None and nid in inv.devices:
            name = inv.devices[nid]["name"]
            status = inv.device_status.get(nid, "unknown")
            nref = f"device/{nid}"
        else:
            name = ln["nb_sys_name"] or ln["nb_chassis"] or "unknown neighbour"
            status = None
            nref = None
        items.append(
            {
                "local_port": cap(local or (str(local_if) if local_if else "")),
                "port_ref": f"port/{a.device_id}/{local_if}" if local_if else None,
                "neighbor_device_id": nid,
                "neighbor_ref": nref,
                "neighbor_name": cap(name),
                "neighbor_status": status,
                "remote_port": cap(remote or ""),
                "source": ln["source"],
                "stale": bool(ln["stale"]),
                "last_seen": iso(ln["last_seen"]),
            }
        )
    items = items[:100]
    return ToolOutput(
        {
            "device_ref": f"device/{a.device_id}",
            "topology_ref": f"topology/{a.device_id}",
            "items": items,
        },
        len(items),
    )


class PathArgs(Args):
    from_device_id: int
    to_device_id: int


async def topology_path(ctx: ToolCtx, a: PathArgs) -> ToolOutput:
    await require_device(ctx.session, a.from_device_id)
    await require_device(ctx.session, a.to_device_id)
    inv = await _inventory(ctx)
    adj: dict[int, list[tuple[int, str, str]]] = {}
    for ln in await _links(ctx, False):
        x, y = ln["a_device_id"], ln["b_device_id"]
        if x is None or y is None:
            continue
        adj.setdefault(x, []).append((y, ln["a_port"] or "", ln["b_port"] or ""))
        adj.setdefault(y, []).append((x, ln["b_port"] or "", ln["a_port"] or ""))
    for v in adj.values():
        v.sort()
    prev: dict[int, tuple[int, str, str] | None] = {a.from_device_id: None}
    q = deque([(a.from_device_id, 0)])
    while q:
        cur, depth = q.popleft()
        if cur == a.to_device_id or depth >= 16:
            continue
        for nxt, out_p, in_p in adj.get(cur, []):
            if nxt not in prev:
                prev[nxt] = (cur, out_p, in_p)
                q.append((nxt, depth + 1))
    if a.to_device_id not in prev:
        return ToolOutput({"found": False, "hops": []}, 0, note="no path within 16 hops")
    chain: list[int] = []
    ports: dict[int, tuple[str, str]] = {}
    cur: int | None = a.to_device_id
    while cur is not None:
        chain.append(cur)
        p = prev[cur]
        if p is None:
            break
        ports[p[0]] = (p[1], ports.get(p[0], ("", ""))[1])
        ports[cur] = (ports.get(cur, ("", ""))[0], p[2])
        cur = p[0]
    chain.reverse()
    hops = [
        {
            "device_id": did,
            "ref": f"device/{did}",
            "name": cap(inv.devices[did]["name"]) if did in inv.devices else str(did),
            "status": inv.device_status.get(did, "unknown"),
            "out_port": cap(ports.get(did, ("", ""))[0]) or None,
            "in_port": cap(ports.get(did, ("", ""))[1]) or None,
        }
        for did in chain
    ]
    return ToolOutput({"found": True, "hops": hops}, len(hops))


# --------------------------------------------------------------------------- ports
class PortsArgs(Args):
    device_id: int
    only_down: bool = False
    query: str | None = Field(None, max_length=50)
    vlan: int | None = Field(None, ge=1, le=4094)
    limit: int = Field(20, ge=1, le=200)


async def get_device_ports(ctx: ToolCtx, a: PortsArgs) -> ToolOutput:
    await require_device(ctx.session, a.device_id)
    conds = ["p.device_id = :d"]
    p: dict[str, Any] = {"d": a.device_id}
    if a.only_down:
        conds.append("p.oper_status <> 'up' AND p.admin_status = 'up'")
    if a.query:
        conds.append("(p.name ILIKE :q OR p.descr ILIKE :q OR p.alias ILIKE :q)")
        p["q"] = f"%{_like(a.query)}%"
    if a.vlan is not None:
        conds.append(
            ":vlan = ANY(p.untagged_vlans) OR :vlan = ANY(p.tagged_vlans) OR p.pvid = :vlan"
        )
        conds[-1] = f"({conds[-1]})"
        p["vlan"] = a.vlan
    rows = (
        (
            await ctx.session.execute(
                text(
                    "SELECT p.*, (SELECT count(*) FROM port_flap_episodes f "  # noqa: S608
                    "WHERE f.device_id = p.device_id AND f.if_index = p.if_index) AS flaps "
                    f"FROM switch_ports p WHERE {' AND '.join(conds)} "
                    "ORDER BY p.if_index LIMIT :lim"
                ),
                {**p, "lim": a.limit + 1},
            )
        )
        .mappings()
        .all()
    )
    nb: dict[int, str] = {}
    for ln in await _links(ctx, False):
        for side, other in (("a", "b"), ("b", "a")):
            if ln[f"{side}_device_id"] == a.device_id and ln[f"{side}_if_index"] is not None:
                oid = ln[f"{other}_device_id"]
                label = f"device {oid}" if oid else (ln["nb_sys_name"] or ln["nb_chassis"] or "")
                if oid:
                    label = await _device_name(ctx, oid) or label
                nb[int(ln[f"{side}_if_index"])] = cap(label)
    items = []
    for r in rows[: a.limit]:
        errs = None
        if r["in_errors_delta"] is not None or r["out_errors_delta"] is not None:
            errs = int(r["in_errors_delta"] or 0) + int(r["out_errors_delta"] or 0)
        items.append(
            {
                "if_index": int(r["if_index"]),
                "ref": f"port/{a.device_id}/{r['if_index']}",
                "name": cap(r["name"] or r["descr"] or str(r["if_index"])),
                "alias": cap(r["alias"] or "", 200),
                "kind": r["kind"],
                "oper": r["oper_status"],
                "admin": r["admin_status"],
                "speed_mbps": int(r["speed_bps"] // 1_000_000) if r["speed_bps"] else None,
                "vlan_untagged": (
                    list(r["untagged_vlans"] or []) or [r["pvid"]]
                    if r["pvid"]
                    else list(r["untagged_vlans"] or [])
                ),
                "vlans_tagged": list(r["tagged_vlans"] or [])[:20],
                "lag": r.get("lag_if_index"),
                "neighbor": nb.get(int(r["if_index"])),
                "mac_count": int(r["mac_count"] or 0),
                "errors_24h": errs,
                "flap_episodes": int(r["flaps"] or 0),
                "last_change": iso(r["last_change_at"]),
            }
        )
    return ToolOutput(
        {"device_ref": f"device/{a.device_id}", "items": items},
        len(items),
        truncated=len(rows) > a.limit,
    )


async def _device_name(ctx: ToolCtx, device_id: int) -> str | None:
    row = (
        await ctx.session.execute(text("SELECT name FROM devices WHERE id = :i"), {"i": device_id})
    ).first()
    return row.name if row else None


class MacIpArgs(Args):
    mac: str | None = Field(None, max_length=32)
    ip: str | None = Field(None, max_length=45)

    @model_validator(mode="after")
    def _one(self) -> MacIpArgs:
        if (self.mac is None) == (self.ip is None):
            raise ValueError("exactly one of mac and ip is required")
        return self


async def find_mac_or_ip(ctx: ToolCtx, a: MacIpArgs) -> ToolOutput:
    from netmon.api.errors import ApiError
    from netmon.ports.search import search_mac

    try:
        res = await search_mac(ctx.session, a.mac, a.ip, lag_on=ctx.gate.on("lag"))
    except ApiError as exc:
        raise ToolError("invalid_arguments", exc.message) from None
    items = []
    for i in (res.get("items") or [])[:50]:
        items.append(
            {
                "mac": i.get("mac"),
                "vendor": cap(i.get("vendor"), 64),
                "ip": i.get("ip"),
                "hostname": cap(i.get("hostname"), 200),
                "device_ref": f"device/{i['device_id']}",
                "device_name": cap(i.get("device_name")),
                "port_ref": f"port/{i['device_id']}/{i['if_index']}",
                "port_name": cap(i.get("port")),
                "vlan": i.get("vlan"),
                "last_seen": i.get("last_seen"),
                "present": i.get("present"),
                "is_uplink": i.get("is_uplink"),
            }
        )
    arp = [
        {
            "ip": r.get("ip"),
            "mac": r.get("mac"),
            "hostname": cap(r.get("hostname"), 200),
            "seen_by_ref": f"device/{r['source_device_id']}",
            "seen_by": cap(r.get("source_device_name")),
            "present": r.get("present"),
        }
        for r in (res.get("arp") or [])[:20]
    ]
    return ToolOutput({"items": items, "arp": arp}, len(items))


# --------------------------------------------------------------------------- availability
class AvailabilityArgs(Args):
    device_ids: list[int] | None = Field(None, max_length=100)
    group_id: int | None = None
    since: str
    until: str


async def availability(ctx: ToolCtx, a: AvailabilityArgs) -> ToolOutput:
    from netmon.reports import data as rdata

    start, end = time_range(a.since, a.until, ctx.now, max_days=92)
    devices = await rdata.scope_devices(
        ctx.session, [a.group_id] if a.group_id is not None else [], list(a.device_ids or [])
    )
    if a.group_id is not None:
        await group_scope(ctx.session, a.group_id)  # not_found check
    if not devices:
        return ToolOutput({"since": iso(start), "until": iso(end), "items": []}, 0)
    truncated = len(devices) > 100
    devices = devices[:100]
    res = await rdata._availability(ctx.session, devices, start, end)  # noqa: SLF001
    items = [
        {
            "device_id": r["device_id"],
            "ref": f"device/{r['device_id']}",
            "device_name": cap(r["device_name"]),
            "group_name": cap(r["group_name"]),
            "sensor_ref": f"sensor/{r['sensor_id']}",
            "availability": r["availability"],
            "measured_s": r["measured_s"],
            "downtime_s": r["downtime_s"],
            "outages": r["outages"],
            "longest_outage_s": r["longest_outage_s"],
        }
        for r in (res or {}).get("devices", [])
    ]
    data = {
        "since": iso(start),
        "until": iso(end),
        "overall": (res or {}).get("overall"),
        "items": items,
        "not_measured": [
            {"ref": f"device/{n['device_id']}", "device_name": cap(n["device_name"])}
            for n in (res or {}).get("not_measured", [])[:100]
        ],
    }
    return ToolOutput(data, len(items), truncated=truncated)


# --------------------------------------------------------------------------- groups
class GroupsArgs(Args):
    parent_id: int | None = None


async def list_groups(ctx: ToolCtx, a: GroupsArgs) -> ToolOutput:
    inv = await _inventory(ctx)
    if a.parent_id is not None and a.parent_id not in inv.groups:
        raise ToolError("not_found", f"group {a.parent_id} not found")
    ids = inv.descendants(a.parent_id)[1:] if a.parent_id is not None else list(inv.groups)
    ids.sort(key=lambda g: (inv.groups[g]["sort_order"], inv.groups[g]["name"].lower(), g))
    items = [
        {
            "id": gid,
            "ref": f"group/{gid}",
            "name": cap(inv.groups[gid]["name"]),
            "parent_id": inv.groups[gid]["parent_id"],
            "status": inv.group_status.get(gid, "unknown"),
            "device_count": sum(len(inv.group_devices.get(x, [])) for x in inv.descendants(gid)),
        }
        for gid in ids[:500]
    ]
    return ToolOutput({"items": items}, len(items), truncated=len(ids) > 500)


# --------------------------------------------------------------------------- registry
_UNTRUSTED_DEV = ("sys_descr", "name", "host")

register(
    ToolDef(
        "search_devices",
        "Find monitored devices by name/host substring, group, status or vendor. Returns status, "
        "sensor status counts, vendor/model and maintenance state.",
        SearchDevicesArgs,
        search_devices,
        untrusted=_UNTRUSTED_DEV,
    )
)
register(
    ToolDef(
        "get_device",
        "One device in detail: identity, status, its sensors (current value, message), active "
        "problems, the dependency parent chain, number of dependents and current maintenance.",
        GetDeviceArgs,
        get_device,
        untrusted=(*_UNTRUSTED_DEV, "message"),
    )
)
register(
    ToolDef(
        "list_problems",
        "Current problems: sensors that are down, warning or unreachable, with acknowledgement.",
        ListProblemsArgs,
        list_problems,
        untrusted=("message", "ack_comment"),
    )
)
register(
    ToolDef(
        "search_sensors",
        "Find sensors by name, device, group, type or status.",
        SearchSensorsArgs,
        search_sensors,
        untrusted=("message",),
    )
)
register(
    ToolDef(
        "get_sensor_history",
        "Measurement history of a sensor: min/avg/max/last per channel and up to 48 time buckets "
        "(range up to 31 days, default last 24 h).",
        SensorHistoryArgs,
        get_sensor_history,
    )
)
register(
    ToolDef(
        "list_events",
        "Events (status changes, maintenance, config changes, port changes…) in a time range "
        "(up to 31 days, default last 24 h), newest first.",
        ListEventsArgs,
        list_events,
        untrusted=("message",),
    )
)
register(
    ToolDef(
        "list_maintenance",
        "Maintenance windows that are active, upcoming or past in a range, with their scope.",
        ListMaintenanceArgs,
        list_maintenance,
        untrusted=("comment",),
    )
)
register(
    ToolDef(
        "topology_neighbors",
        "Topology links of a device: local port, neighbour (device or LLDP/CDP name), remote port, "
        "source and staleness.",
        NeighborsArgs,
        topology_neighbors,
        untrusted=("neighbor_name", "remote_port", "local_port"),
    )
)
register(
    ToolDef(
        "topology_path",
        "Shortest path between two devices over the topology links (up to 16 hops).",
        PathArgs,
        topology_path,
    )
)
register(
    ToolDef(
        "get_device_ports",
        "Switch port inventory of a device: oper/admin state, speed, VLANs, LAG, neighbour, MAC "
        "count, errors and flaps.",
        PortsArgs,
        get_device_ports,
        untrusted=("alias", "neighbor", "name"),
    )
)
register(
    ToolDef(
        "find_mac_or_ip",
        "Where is a MAC or IPv4 address connected: switch, port, VLAN, last seen (exactly one of "
        "mac / ip).",
        MacIpArgs,
        find_mac_or_ip,
        untrusted=("hostname",),
    )
)
register(
    ToolDef(
        "availability",
        "Availability percentage per device (or a group) in a time range up to 92 days, with "
        "downtime and outages.",
        AvailabilityArgs,
        availability,
    )
)
register(
    ToolDef(
        "list_groups",
        "The group tree: id, name, parent, status and device count.",
        GroupsArgs,
        list_groups,
    )
)
