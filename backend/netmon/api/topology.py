"""[2b-1] Topology map: graph, refresh, links, layout, dependency suggestions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Response
from sqlalchemy import text

from netmon.api.deps import DB, RT, Admin, Page, Viewer
from netmon.api.errors import ApiError, not_found, validation_error
from netmon.models import Device, TopologyLink, TopologyNeighbor
from netmon.ports.read import port_names
from netmon.ports.view import display_port
from netmon.schemas import LayoutIn, TopologyLinkIn
from netmon.services.dependencies import dependency_out, effective_parent
from netmon.services.serialize import iso, page
from netmon.services.status import load_inventory
from netmon.topology.refresh import RefreshRunningError, refresh_out
from netmon.topology.suggest import suggest_dependencies

router = APIRouter()


def _running(refresh_id: int | None) -> ApiError:
    return ApiError(
        409,
        "topology_refresh_running",
        "A topology refresh is already running",
        [{"refresh_id": refresh_id}] if refresh_id is not None else None,
    )


async def _iface_data(session: DB) -> dict[tuple[int, int], dict[str, Any]]:
    """(device_id, if_index) -> the snmp_interface sensor and its last channel values."""
    out: dict[tuple[int, int], dict[str, Any]] = {}
    rows = await session.execute(
        text(
            "SELECT s.id, s.device_id, s.status, s.config, "
            "jsonb_object_agg(c.key, c.last_value) FILTER (WHERE c.key IS NOT NULL) AS vals "
            "FROM sensors s LEFT JOIN sensor_channels c ON c.sensor_id = s.id "
            "WHERE s.type = 'snmp_interface' GROUP BY s.id ORDER BY s.id"
        )
    )
    for r in rows:
        try:
            idx = int((r.config or {}).get("if_index"))
        except (TypeError, ValueError):
            continue
        out.setdefault((r.device_id, idx), {"id": r.id, "status": r.status, "vals": r.vals or {}})
    return out


def _end(
    device_id: int | None,
    neighbor_id: int | None,
    if_index: int | None,
    port: str,
    ifaces: dict[tuple[int, int], dict[str, Any]],
) -> dict[str, Any]:
    sensor = ifaces.get((device_id, if_index)) if device_id and if_index is not None else None
    vals = sensor["vals"] if sensor else {}

    def v(key: str) -> Any:
        return vals.get(key) if sensor else None

    oper = v("oper_status")
    return {
        "node": f"d:{device_id}" if device_id is not None else f"n:{neighbor_id}",
        "device_id": device_id,
        "neighbor_id": neighbor_id,
        "if_index": if_index,
        "port": port,
        "sensor_id": sensor["id"] if sensor else None,
        "sensor_status": sensor["status"] if sensor else None,
        "oper_status": int(oper) if oper is not None else None,
        "in_bps": v("in_bps"),
        "out_bps": v("out_bps"),
        "in_util_pct": v("in_util_pct"),
        "out_util_pct": v("out_util_pct"),
    }


def link_out(
    ln: Any,
    ifaces: dict[tuple[int, int], dict[str, Any]],
    names: dict[tuple[int, int], str] | None = None,
) -> dict[str, Any]:
    """[2b-2] `names`: current port names (netmon.ports.read.port_names); an end whose stored
    port is empty or the bare index shows the current name (the stored value is kept)."""
    names = names or {}

    def port(dev: int | None, idx: int | None, stored: str) -> str:
        name = names.get((dev, idx)) if dev is not None and idx is not None else None
        return display_port(stored, idx, name)

    return {
        "id": ln.id,
        "source": ln.source,
        "a": _end(
            ln.a_device_id,
            None,
            ln.a_if_index,
            port(ln.a_device_id, ln.a_if_index, ln.a_port),
            ifaces,
        ),
        "b": _end(
            ln.b_device_id,
            ln.b_neighbor_id,
            ln.b_if_index,
            port(ln.b_device_id, ln.b_if_index, ln.b_port),
            ifaces,
        ),
        "stale": ln.stale,
        "missed_refreshes": ln.missed_refreshes,
        "first_seen": iso(ln.first_seen),
        "last_seen": iso(ln.last_seen),
    }


def _ends_key(ln: Any) -> tuple[Any, ...]:
    return (ln.a_device_id, ln.a_port, ln.b_device_id, ln.b_neighbor_id, ln.b_port)


async def _last_refresh(session: DB) -> dict[str, Any] | None:
    row = (
        await session.execute(text("SELECT * FROM topology_refreshes ORDER BY id DESC LIMIT 1"))
    ).first()
    return refresh_out(row) if row is not None else None


@router.get("/api/topology")
async def graph(
    _: Viewer,
    rt: RT,
    session: DB,
    group_id: int | None = None,
    include_stale: bool = True,
) -> dict:
    inv = await load_inventory(session)
    maint = rt.maintenance
    layout = {
        r.node_key: (r.x, r.y)
        for r in await session.execute(text("SELECT node_key, x, y FROM topology_layout"))
    }
    links = list((await session.execute(text("SELECT * FROM topology_links ORDER BY id"))).all())
    manual = {_ends_key(ln) for ln in links if ln.source == "manual"}
    links = [
        ln
        for ln in links
        if (ln.source == "manual" or _ends_key(ln) not in manual)
        and (include_stale or not ln.stale)
    ]
    in_filter: set[int] = set(inv.devices)
    if group_id is not None:
        groups = set(inv.descendants(group_id)) if group_id in inv.groups else set()
        in_filter = {d for d, info in inv.devices.items() if info["group_id"] in groups}
        links = [ln for ln in links if ln.a_device_id in in_filter or ln.b_device_id in in_filter]
    device_ids = set(in_filter)
    neighbor_ids: set[int] = set()
    for ln in links:
        device_ids.add(ln.a_device_id)
        if ln.b_device_id is not None:
            device_ids.add(ln.b_device_id)
        if ln.b_neighbor_id is not None:
            neighbor_ids.add(ln.b_neighbor_id)
    classes = {
        r.id: (r.identity or {}).get("device_class")
        for r in await session.execute(text("SELECT id, identity FROM devices"))
    }
    nodes: list[dict[str, Any]] = []
    for did in sorted(device_ids, key=lambda d: (inv.devices[d]["name"].lower(), d)):
        info = inv.devices[did]
        pos = layout.get(f"d:{did}")
        nodes.append(
            {
                "key": f"d:{did}",
                "kind": "device",
                "device_id": did,
                "name": info["name"],
                "host": info["host"],
                "group_id": info["group_id"],
                "status": inv.device_status[did],
                "in_maintenance": bool(maint is not None and maint.in_maintenance(did)),
                "device_class": classes.get(did),
                "x": pos[0] if pos else None,
                "y": pos[1] if pos else None,
                "in_filter": did in in_filter,
            }
        )
    if neighbor_ids:
        for nb in await session.execute(
            text("SELECT * FROM topology_neighbors WHERE id = ANY(:ids) ORDER BY id"),
            {"ids": sorted(neighbor_ids)},
        ):
            pos = layout.get(f"n:{nb.id}")
            nodes.append(
                {
                    "key": f"n:{nb.id}",
                    "kind": "neighbor",
                    "neighbor_id": nb.id,
                    "name": nb.sys_name or nb.chassis_id,
                    "chassis_id": nb.chassis_id,
                    "mgmt_address": nb.mgmt_address,
                    "sys_descr": nb.sys_descr,
                    "x": pos[0] if pos else None,
                    "y": pos[1] if pos else None,
                    "in_filter": True,
                }
            )
    ifaces = await _iface_data(session)
    names = await port_names(session)
    return {
        "nodes": nodes,
        "links": [link_out(ln, ifaces, names) for ln in links],
        "last_refresh": await _last_refresh(session),
        "refresh_running": rt.topology is not None and rt.topology.running_id is not None,
    }


@router.post("/api/topology/refresh", status_code=202)
async def start_refresh(user: Admin, rt: RT, session: DB) -> dict:
    await session.close()
    if rt.topology is None:
        raise ApiError(500, "internal_error", "Topology is not available")
    try:
        return await rt.topology.start(trigger="manual", created_by=user.id)
    except RefreshRunningError as exc:
        raise _running(exc.refresh_id) from None


@router.get("/api/topology/links")
async def list_links(
    _: Admin,
    session: DB,
    pg: Page,
    device_id: int | None = None,
    source: str | None = None,
    stale: bool | None = None,
) -> dict:
    where, params = ["true"], {}
    if device_id is not None:
        where.append("(a_device_id = :d OR b_device_id = :d)")
        params["d"] = device_id
    if source is not None:
        if source not in ("lldp", "cdp", "manual"):
            raise validation_error(["query", "source"], "must be lldp, cdp or manual")
        where.append("source = :s")
        params["s"] = source
    if stale is not None:
        where.append("stale = :st")
        params["st"] = stale
    cond = " AND ".join(where)
    total = (
        await session.execute(text(f"SELECT count(*) FROM topology_links WHERE {cond}"), params)  # noqa: S608
    ).scalar_one()
    rows = (
        await session.execute(
            text(
                f"SELECT * FROM topology_links WHERE {cond} ORDER BY id "  # noqa: S608
                "OFFSET :off LIMIT :lim"
            ),
            {**params, "off": pg.offset, "lim": pg.page_size},
        )
    ).all()
    ifaces = await _iface_data(session)
    names = await port_names(session)
    return page([link_out(r, ifaces, names) for r in rows], total, pg.page, pg.page_size)


async def _default_port(session: DB, device_id: int, if_index: int | None) -> str:
    """ifName from the 2b-2 port inventory, else the snmp_interface sensor's if_name, else the
    index."""
    if if_index is None:
        return ""
    port = (
        await session.execute(
            text("SELECT name FROM switch_ports WHERE device_id = :d AND if_index = :i"),
            {"d": device_id, "i": if_index},
        )
    ).scalar()
    if port:
        return str(port)
    name = (
        await session.execute(
            text(
                "SELECT config->>'if_name' FROM sensors WHERE device_id = :d "
                "AND type = 'snmp_interface' AND (config->>'if_index')::int = :i "
                "ORDER BY id LIMIT 1"
            ),
            {"d": device_id, "i": if_index},
        )
    ).scalar()
    return str(name) if name else str(if_index)


async def _link_fields(session: DB, data: dict[str, Any]) -> dict[str, Any]:
    if data.get("a_device_id") is None:
        raise validation_error(["body", "a_device_id"], "field required")
    if await session.get(Device, data["a_device_id"]) is None:
        raise validation_error(["body", "a_device_id"], "device does not exist")
    b_dev, b_nb = data.get("b_device_id"), data.get("b_neighbor_id")
    if (b_dev is None) == (b_nb is None):
        raise validation_error(
            ["body", "b_device_id"], "exactly one of b_device_id and b_neighbor_id is required"
        )
    if b_dev is not None:
        if await session.get(Device, b_dev) is None:
            raise validation_error(["body", "b_device_id"], "device does not exist")
        if b_dev == data["a_device_id"]:
            raise validation_error(["body", "b_device_id"], "a link needs two different devices")
    if b_nb is not None and await session.get(TopologyNeighbor, b_nb) is None:
        raise validation_error(["body", "b_neighbor_id"], "neighbor does not exist")
    a_port = data.get("a_port")
    if a_port is None:
        a_port = await _default_port(session, data["a_device_id"], data.get("a_if_index"))
    b_port = data.get("b_port")
    if b_port is None:
        b_port = (
            await _default_port(session, b_dev, data.get("b_if_index")) if b_dev is not None else ""
        )
    out = {
        "a_device_id": data["a_device_id"],
        "a_if_index": data.get("a_if_index"),
        "a_port": a_port,
        "b_device_id": b_dev,
        "b_neighbor_id": b_nb,
        "b_if_index": data.get("b_if_index"),
        "b_port": b_port,
    }
    if b_dev is not None and b_dev < out["a_device_id"]:  # canonical order
        out.update(
            a_device_id=b_dev,
            a_if_index=out["b_if_index"],
            a_port=out["b_port"],
            b_device_id=out["a_device_id"],
            b_if_index=out["a_if_index"],
            b_port=out["a_port"],
        )
    return out


async def _link_json(session: DB, link_id: int) -> dict:
    row = (
        await session.execute(text("SELECT * FROM topology_links WHERE id = :id"), {"id": link_id})
    ).first()
    assert row is not None
    return link_out(row, await _iface_data(session), await port_names(session))


def _changed(rt: RT, link_id: int, action: str) -> None:
    rt.hub.broadcast("config.changed", {"entity": "topology_link", "id": link_id, "action": action})
    rt.hub.broadcast("topology.updated", {"reason": "link", "refresh_id": None})


@router.post("/api/topology/links", status_code=201)
async def create_link(body: TopologyLinkIn, user: Admin, rt: RT, session: DB) -> dict:
    fields = await _link_fields(session, body.model_dump())
    ln = TopologyLink(source="manual", created_by=user.id, **fields)
    session.add(ln)
    await _commit_link(session)
    _changed(rt, ln.id, "created")
    return await _link_json(session, ln.id)


async def _commit_link(session: DB) -> None:
    from sqlalchemy.exc import IntegrityError

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(
            409, "conflict", "A link with these endpoints already exists", [{"field": "link"}]
        ) from exc


@router.patch("/api/topology/links/{link_id}")
async def patch_link(link_id: int, body: TopologyLinkIn, _: Admin, rt: RT, session: DB) -> dict:
    ln = await session.get(TopologyLink, link_id)
    if ln is None:
        raise not_found("Link")
    if ln.source != "manual":
        raise ApiError(400, "link_not_manual", "Only manual links can be edited")
    data = {
        "a_device_id": ln.a_device_id,
        "a_if_index": ln.a_if_index,
        "a_port": ln.a_port,
        "b_device_id": ln.b_device_id,
        "b_neighbor_id": ln.b_neighbor_id,
        "b_if_index": ln.b_if_index,
        "b_port": ln.b_port,
    }
    given = body.model_fields_set
    for key in given:
        data[key] = getattr(body, key)
    if "b_device_id" in given and body.b_device_id is not None and "b_neighbor_id" not in given:
        data["b_neighbor_id"] = None
    if "b_neighbor_id" in given and body.b_neighbor_id is not None and "b_device_id" not in given:
        data["b_device_id"] = None
    if "a_if_index" in given and "a_port" not in given:
        data["a_port"] = None
    if "b_if_index" in given and "b_port" not in given:
        data["b_port"] = None
    for key, value in (await _link_fields(session, data)).items():
        setattr(ln, key, value)
    from sqlalchemy import func

    ln.last_seen = func.now()
    await _commit_link(session)
    _changed(rt, link_id, "updated")
    return await _link_json(session, link_id)


@router.delete("/api/topology/links/{link_id}", status_code=204)
async def delete_link(link_id: int, _: Admin, rt: RT, session: DB) -> Response:
    ln = await session.get(TopologyLink, link_id)
    if ln is None:
        raise not_found("Link")
    await session.delete(ln)
    await session.commit()
    _changed(rt, link_id, "deleted")
    return Response(status_code=204)


@router.delete("/api/topology/links")
async def delete_stale_links(
    _: Admin, rt: RT, session: DB, stale: bool | None = Query(default=None)
) -> dict:
    if stale is not True:
        raise validation_error(["query", "stale"], "stale=true is required")
    n = (await session.execute(text("DELETE FROM topology_links WHERE stale RETURNING id"))).all()
    await session.execute(
        text(
            "DELETE FROM topology_neighbors n WHERE NOT EXISTS "
            "(SELECT 1 FROM topology_links l WHERE l.b_neighbor_id = n.id)"
        )
    )
    await session.commit()
    if n:
        rt.hub.broadcast("topology.updated", {"reason": "link", "refresh_id": None})
    return {"deleted": len(n)}


@router.put("/api/topology/layout", status_code=204)
async def put_layout(body: LayoutIn, _: Admin, rt: RT, session: DB) -> Response:
    devices = set((await session.execute(text("SELECT 'd:' || id FROM devices"))).scalars())
    nbs = set((await session.execute(text("SELECT 'n:' || id FROM topology_neighbors"))).scalars())
    known = devices | nbs
    rows = [p for p in body.positions if p.node in known]
    for p in rows:
        await session.execute(
            text(
                "INSERT INTO topology_layout (node_key, x, y) VALUES (:k, :x, :y) "
                "ON CONFLICT (node_key) DO UPDATE SET x = EXCLUDED.x, y = EXCLUDED.y, "
                "updated_at = now()"
            ),
            {"k": p.node, "x": p.x, "y": p.y},
        )
    await session.commit()
    rt.hub.broadcast("topology.updated", {"reason": "layout", "refresh_id": None})
    return Response(status_code=204)


@router.delete("/api/topology/layout", status_code=204)
async def delete_layout(_: Admin, rt: RT, session: DB) -> Response:
    await session.execute(text("DELETE FROM topology_layout"))
    await session.commit()
    rt.hub.broadcast("topology.updated", {"reason": "layout", "refresh_id": None})
    return Response(status_code=204)


@router.get("/api/topology/dependency-suggestions")
async def dependency_suggestions(_: Admin, session: DB, root_device_id: int | None = None) -> dict:
    inv = await load_inventory(session)
    if root_device_id is not None and root_device_id not in inv.devices:
        raise validation_error(["query", "root_device_id"], "device does not exist")
    edges = [
        (r.a_device_id, r.b_device_id)
        for r in await session.execute(
            text(
                "SELECT a_device_id, b_device_id FROM topology_links "
                "WHERE b_device_id IS NOT NULL AND NOT stale"
            )
        )
    ]
    root, result = suggest_dependencies(edges, root_device_id)
    items = []
    for did, (parent, hops) in result.items():
        if did == root or did not in inv.devices:
            continue
        eff = effective_parent(inv, did)
        items.append(
            {
                "device_id": did,
                "device_name": inv.devices[did]["name"],
                "suggested_parent_id": parent,
                "suggested_parent_name": inv.devices[parent]["name"]
                if parent in inv.devices
                else None,
                "current": dependency_out(inv, inv.devices[did]),
                "effective_parent_id": eff.device_id if eff else None,
                "hops": hops,
            }
        )
    items.sort(key=lambda i: (i["hops"], i["device_name"].lower(), i["device_id"]))
    return {"root_device_id": root, "items": items}
