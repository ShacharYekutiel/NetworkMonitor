"""[3] NOC kiosk: the curated read-only view (`KioskView`, api.md § NOC kiosk).

Only what a wall screen shows for its scope: status counts, down/unreachable devices,
problem sensors, recent status changes, group tiles, the topology map. No addresses unless
`show_addresses`; no configuration, users, logs or settings."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.services.serialize import iso
from netmon.services.status import empty_counts, load_inventory, severity_key

MAX_DOWN = 100
MAX_PROBLEMS = 50
MAX_ALERTS = 20


async def build_view(rt: Any, session: AsyncSession, screen: Any) -> dict[str, Any]:
    from netmon.services import settings as settings_svc

    widgets = list(screen.widgets or [])
    inv = await load_inventory(session)
    maint = rt.maintenance
    scope_gid = screen.scope_group_id if screen.scope_group_id in inv.groups else None
    if scope_gid is not None:
        groups = set(inv.descendants(scope_gid))
        devices = {d for d, info in inv.devices.items() if info["group_id"] in groups}
    else:
        groups = set(inv.groups)
        devices = set(inv.devices)
    show = bool(screen.show_addresses)
    out: dict[str, Any] = {
        "screen": {
            "name": screen.name,
            "language": screen.language,
            "widgets": widgets,
            "refresh_s": screen.refresh_s,
        },
        "generated_at": iso(rt.clock.now()),
        "timezone": str(await settings_svc.get(session, "timezone") or "UTC"),
        "summary": None,
        "down_devices": None,
        "problems": None,
        "recent_alerts": None,
        "group_tiles": None,
        "map": None,
    }
    sensors_in = {sid: s for sid, s in inv.sensors.items() if s["device_id"] in devices}
    group_names = {gid: g["name"] for gid, g in inv.groups.items()}
    if "summary" in widgets:
        dc, sc = empty_counts(), empty_counts()
        for did in devices:
            dc[inv.device_status[did]] += 1
            dc["total"] += 1
        acked = 0
        for s in sensors_in.values():
            sc[s["status"]] += 1
            sc["total"] += 1
            if s["acknowledged"] and s["status"] in ("down", "warning"):
                acked += 1
        in_maint = sum(1 for d in devices if maint is not None and maint.in_maintenance(d))
        out["summary"] = {
            "device_counts": dc,
            "sensor_counts": sc,
            "acknowledged": acked,
            "in_maintenance": in_maint,
        }
    since: dict[int, Any] = {}
    if "down_devices" in widgets or "problems" in widgets:
        ids = [sid for sid, s in sensors_in.items() if s["status"] in ("down", "warning")]
        if ids:
            for r in await session.execute(
                text(
                    "SELECT id, last_status_change_at, status_message FROM sensors "
                    "WHERE id = ANY(:i)"
                ),
                {"i": ids},
            ):
                since[r.id] = (r.last_status_change_at, r.status_message or "")
    if "down_devices" in widgets:
        rows = []
        for did in devices:
            st = inv.device_status[did]
            if st not in ("down", "unreachable"):
                continue
            info = inv.devices[did]
            problem_sensors = [sid for sid in inv.device_sensors.get(did, []) if sid in since]
            times = [since[s][0] for s in problem_sensors if since[s][0] is not None]
            acked = bool(problem_sensors) and all(
                inv.sensors[s]["acknowledged"] for s in problem_sensors
            )
            rows.append(
                {
                    "id": did,
                    "name": info["name"],
                    "group": group_names.get(info["group_id"], ""),
                    "host": info["host"] if show else None,
                    "status": st,
                    "since": iso(min(times)) if times else None,
                    "acknowledged": acked,
                }
            )
        rows.sort(key=lambda r: (r["status"] != "down", r["since"] or "", r["name"].lower()))
        out["down_devices"] = rows[:MAX_DOWN]
    if "problems" in widgets:
        probs = []
        for sid, s in sensors_in.items():
            if s["status"] not in ("down", "warning"):
                continue
            t, msg = since.get(sid, (None, ""))
            probs.append(
                {
                    "sensor_id": sid,
                    "sensor_name": s["name"],
                    "device_name": inv.devices[s["device_id"]]["name"],
                    "status": s["status"],
                    "message": msg[:300],
                    "since": iso(t),
                    "acknowledged": bool(s["acknowledged"]),
                }
            )
        probs.sort(
            key=lambda p: (
                p["acknowledged"],
                -severity_key(p["status"]),
                p["since"] or "",
                p["sensor_id"],
            )
        )
        out["problems"] = probs[:MAX_PROBLEMS]
    if "recent_alerts" in widgets:
        rows = await session.execute(
            text(
                "SELECT time, device_name, sensor_name, old_status, new_status, message "
                "FROM events WHERE kind = 'status_change' AND device_id = ANY(:d) "
                "ORDER BY time DESC, id DESC LIMIT :n"
            ),
            {"d": list(devices), "n": MAX_ALERTS},
        )
        out["recent_alerts"] = [
            {
                "time": iso(r.time),
                "device_name": r.device_name or "",
                "sensor_name": r.sensor_name or "",
                "old_status": r.old_status,
                "new_status": r.new_status,
                "message": (r.message or "")[:300],
            }
            for r in rows
        ]
    if "group_tiles" in widgets:
        top = (
            inv.children.get(scope_gid, []) if scope_gid is not None else inv.children.get(None, [])
        )
        if scope_gid is not None and not top:
            top = [scope_gid]
        tiles = [
            {
                "id": gid,
                "name": group_names[gid],
                "status": inv.group_status[gid],
                "counts": inv.group_counts[gid],
            }
            for gid in top
            if gid in groups
        ]
        tiles.sort(key=lambda t: (inv.groups[t["id"]]["sort_order"], t["name"].lower()))
        out["group_tiles"] = tiles
    if "map" in widgets:
        layout = {
            r.node_key: (r.x, r.y)
            for r in await session.execute(text("SELECT node_key, x, y FROM topology_layout"))
        }
        links_rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT LEAST(a_device_id, b_device_id) AS a, "
                    "GREATEST(a_device_id, b_device_id) AS b FROM topology_links "
                    "WHERE b_device_id IS NOT NULL AND NOT stale"
                )
            )
        ).all()
        nodes = []
        for did in sorted(devices, key=lambda d: (inv.devices[d]["name"].lower(), d)):
            pos = layout.get(f"d:{did}")
            nodes.append(
                {
                    "id": did,
                    "name": inv.devices[did]["name"],
                    "status": inv.device_status[did],
                    "x": pos[0] if pos else None,
                    "y": pos[1] if pos else None,
                    "kind": "device",
                }
            )
        links = []
        for r in links_rows:
            if r.a not in devices or r.b not in devices or r.a == r.b:
                continue
            sa, sb = inv.device_status[r.a], inv.device_status[r.b]
            if "down" in (sa, sb) or "unreachable" in (sa, sb):
                st = "down"
            elif sa in ("up", "warning") and sb in ("up", "warning"):
                st = "up"
            else:
                st = "unknown"
            links.append({"a": r.a, "b": r.b, "status": st})
        out["map"] = {"nodes": nodes, "links": links}
    return out
