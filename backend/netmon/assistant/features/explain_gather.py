"""[3b] "Explain this alert" input (§ 14.8.1): the pure query plan `gather_plan` and the
collector that runs it (read-only, bounded, compact JSON ≤ 24 KiB). The alert-note feature uses
the reduced plan (§ 14.8.3: 30 min, ≤ 10 rows each, no tools)."""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

log = logging.getLogger("netmon.assistant.explain")

MAX_BYTES = 24 * 1024
NOTE_MAX_BYTES = 8 * 1024


@dataclass(frozen=True)
class GatherPlan:
    window_start: datetime
    window_end: datetime
    previous_changes: int
    related_events: int
    traps: int  # 0 = not gathered (feature OFF)
    syslog: int
    syslog_severity_max: int
    config_changes: bool
    config_diff: bool
    port_flaps: bool
    port_changes: int
    maintenance: bool
    dependencies: bool
    neighbours: bool
    other_problems: bool
    max_bytes: int
    notes: list[str] = field(default_factory=list)


def gather_plan(
    problem_start: datetime,
    now: datetime,
    recovered_at: datetime | None,
    features: dict[str, bool],
    *,
    reduced: bool = False,
    diff_gates_open: bool = False,
) -> GatherPlan:
    """Pure: what to gather for an explanation (full) or an alert note (reduced)."""
    end = recovered_at or now
    if reduced:
        start = problem_start - timedelta(minutes=30)
        n = 10
    else:
        start = problem_start - timedelta(minutes=60)
        n = 50
    return GatherPlan(
        window_start=start,
        window_end=end,
        previous_changes=5,
        related_events=n,
        traps=n if features.get("traps") else 0,
        syslog=n if features.get("syslog") else 0,
        syslog_severity_max=5,
        config_changes=not reduced,
        config_diff=bool(diff_gates_open) and not reduced,
        port_flaps=True,
        port_changes=0 if reduced else n,
        maintenance=True,
        dependencies=True,
        neighbours=not reduced,
        other_problems=True,
        max_bytes=NOTE_MAX_BYTES if reduced else MAX_BYTES,
    )


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat().replace("+00:00", "Z")
    return str(v)


def _cut(s: Any, n: int = 200) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def shrink(data: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Drop list items from the end of the largest lists until the JSON fits."""

    def size() -> int:
        return len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))

    guard = 0
    while size() > max_bytes and guard < 5000:
        guard += 1
        lists = [(k, v) for k, v in data.items() if isinstance(v, list) and v]
        if not lists:
            break
        k, v = max(lists, key=lambda kv: len(json.dumps(kv[1], default=str)))
        v.pop()
        data.setdefault("truncated", [])
        if k not in data["truncated"]:
            data["truncated"].append(k)
    return data


async def problem_event(session: Any, sensor_id: int, event_id: int | None) -> Any:
    """The explained status_change event: the given one, else the sensor's current problem
    event, else its latest status change."""
    if event_id is not None:
        return (
            await session.execute(
                text(
                    "SELECT id, time, old_status, new_status, message, device_id, sensor_id "
                    "FROM events WHERE id = :e AND sensor_id = :s"
                ),
                {"e": event_id, "s": sensor_id},
            )
        ).first()
    st = (
        await session.execute(text("SELECT status FROM sensors WHERE id = :s"), {"s": sensor_id})
    ).scalar()
    if st in ("down", "warning", "unreachable"):
        row = (
            await session.execute(
                text(
                    "SELECT id, time, old_status, new_status, message, device_id, sensor_id "
                    "FROM events WHERE sensor_id = :s AND kind = 'status_change' "
                    "AND new_status = :st ORDER BY time DESC, id DESC LIMIT 1"
                ),
                {"s": sensor_id, "st": st},
            )
        ).first()
        if row is not None:
            return row
    return (
        await session.execute(
            text(
                "SELECT id, time, old_status, new_status, message, device_id, sensor_id FROM "
                "events "
                "WHERE sensor_id = :s AND kind = 'status_change' ORDER BY time DESC LIMIT 1"
            ),
            {"s": sensor_id},
        )
    ).first()


async def collect(
    rt: Any,
    session: Any,
    sensor_id: int,
    event: Any,
    *,
    reduced: bool = False,
    diff_gates_open: bool = False,
) -> dict[str, Any]:
    """Run the plan for `sensor_id` / `event` (a row from `problem_event`). Read-only."""
    sensor = (
        await session.execute(
            text(
                "SELECT s.id, s.name, s.type, s.status, s.status_message AS message, "
                "s.last_check_at, "
                "s.device_id, "
                "d.name AS device_name, d.host, d.group_id FROM sensors s JOIN devices d "
                "ON d.id = s.device_id WHERE s.id = :s"
            ),
            {"s": sensor_id},
        )
    ).first()
    if sensor is None:
        return {}
    now = rt.clock.now()
    problem_start = event.time if event is not None else now
    recovered = None
    if event is not None:
        recovered = (
            await session.execute(
                text(
                    "SELECT min(time) FROM events WHERE sensor_id = :s AND kind = 'status_change' "
                    "AND time > :t AND new_status = 'up'"
                ),
                {"s": sensor_id, "t": event.time},
            )
        ).scalar()
    feats = rt.features.snapshot() if getattr(rt, "features", None) else {}
    plan = gather_plan(
        problem_start, now, recovered, feats, reduced=reduced, diff_gates_open=diff_gates_open
    )
    did = sensor.device_id
    out: dict[str, Any] = {
        "sensor": {
            "id": sensor.id,
            "ref": f"sensor/{sensor.id}",
            "name": sensor.name,
            "type": sensor.type,
            "status": sensor.status,
            "message": _cut(sensor.message),
            "last_check_at": _iso(sensor.last_check_at),
        },
        "device": {
            "id": did,
            "ref": f"device/{did}",
            "name": sensor.device_name,
            "host": sensor.host,
        },
        "window": {"start": _iso(plan.window_start), "end": _iso(plan.window_end)},
    }
    # group path
    with contextlib.suppress(Exception):
        path = []
        gid = sensor.group_id
        for _ in range(20):
            if gid is None:
                break
            g = (
                await session.execute(
                    text("SELECT id, name, parent_id FROM groups WHERE id = :g"), {"g": gid}
                )
            ).first()
            if g is None:
                break
            path.insert(0, g.name)
            gid = g.parent_id
        out["device"]["group_path"] = " / ".join(path)
    if event is not None:
        out["event"] = {
            "id": event.id,
            "ref": f"event/{event.id}",
            "time": _iso(event.time),
            "old_status": event.old_status,
            "new_status": event.new_status,
            "message": _cut(event.message),
            "recovered_at": _iso(recovered),
        }
    out["previous_changes"] = [
        {
            "id": r.id,
            "ref": f"event/{r.id}",
            "time": _iso(r.time),
            "old_status": r.old_status,
            "new_status": r.new_status,
            "message": _cut(r.message),
        }
        for r in await session.execute(
            text(
                "SELECT id, time, old_status, new_status, message FROM events WHERE sensor_id = :s "
                "AND kind = 'status_change' AND time < :t ORDER BY time DESC LIMIT :n"
            ),
            {"s": sensor_id, "t": problem_start, "n": plan.previous_changes},
        )
    ]
    # dependencies
    parent_chain: list[int] = []
    children: list[int] = []
    if plan.dependencies:
        with contextlib.suppress(Exception):
            from netmon.services import dependencies
            from netmon.services.status import load_inventory

            inv = await load_inventory(session)
            cur = did
            for _ in range(16):
                eff = dependencies.effective_parent(inv, cur)
                if eff is None or eff.device_id in parent_chain:
                    break
                parent_chain.append(eff.device_id)
                cur = eff.device_id
            for other in inv.devices:
                eff = dependencies.effective_parent(inv, other)
                if eff is not None and eff.device_id == did:
                    children.append(other)
            ids = parent_chain + children
            st = {}
            if ids:
                st = {
                    r.device_id: r.st
                    for r in await session.execute(
                        text(
                            "SELECT device_id, (array_agg(status ORDER BY CASE status WHEN 'down' "
                            "THEN 0 WHEN 'unreachable' THEN 1 WHEN 'warning' THEN 2 ELSE 3 "
                            "END))[1] "
                            "AS st FROM sensors WHERE device_id = ANY(:d) GROUP BY device_id"
                        ),
                        {"d": ids},
                    )
                }
            out["dependencies"] = {
                "parents": [
                    {
                        "id": p,
                        "ref": f"device/{p}",
                        "name": inv.devices[p]["name"],
                        "status": st.get(p),
                    }
                    for p in parent_chain
                ],
                "dependents_count": len(children),
                "unreachable_dependents": sum(1 for c in children if st.get(c) == "unreachable"),
            }
    related = [did, *parent_chain[:1], *children[:50]]
    out["related_events"] = [
        {
            "id": r.id,
            "ref": f"event/{r.id}",
            "time": _iso(r.time),
            "kind": r.kind,
            "device": r.device_name,
            "sensor": r.sensor_name,
            "old_status": r.old_status,
            "new_status": r.new_status,
            "message": _cut(r.message),
        }
        for r in await session.execute(
            text(
                "SELECT id, time, kind, device_name, sensor_name, old_status, new_status, message "
                "FROM events WHERE device_id = ANY(:d) AND time BETWEEN :a AND :b "
                "AND kind IN ('status_change','acknowledge','maintenance','port_change',"
                "'config_change','system') AND (sensor_id IS DISTINCT FROM :s OR time <> :t) "
                "ORDER BY time DESC LIMIT :n"
            ),
            {
                "d": related,
                "a": plan.window_start,
                "b": plan.window_end,
                "s": sensor_id,
                "t": problem_start,
                "n": plan.related_events,
            },
        )
    ]
    if plan.traps:
        with contextlib.suppress(Exception):
            out["traps"] = [
                {
                    "id": r.id,
                    "ref": f"trap/{r.id}",
                    "time": _iso(r.time),
                    "trap_name": r.trap_name,
                    "trap_oid": r.trap_oid,
                    "summary": _cut(r.summary, 300),
                }
                for r in await session.execute(
                    text(
                        "SELECT id, time, trap_name, trap_oid, summary FROM traps WHERE "
                        "device_id = :d "
                        "AND time BETWEEN :a AND :b ORDER BY time DESC LIMIT :n"
                    ),
                    {"d": did, "a": plan.window_start, "b": plan.window_end, "n": plan.traps},
                )
            ]
    if plan.syslog:
        with contextlib.suppress(Exception):
            out["syslog"] = [
                {
                    "id": r.id,
                    "ref": f"syslog/{r.id}",
                    "time": _iso(r.time),
                    "severity": r.severity,
                    "program": r.program,
                    "message": _cut(r.message, 500),
                }
                for r in await session.execute(
                    text(
                        "SELECT id, time, severity, program, message FROM syslog_messages "
                        "WHERE device_id = :d AND time BETWEEN :a AND :b AND severity <= :sev "
                        "ORDER BY severity, time DESC LIMIT :n"
                    ),
                    {
                        "d": did,
                        "a": plan.window_start,
                        "b": plan.window_end,
                        "sev": plan.syslog_severity_max,
                        "n": plan.syslog,
                    },
                )
            ]
    if plan.config_changes:
        with contextlib.suppress(Exception):
            out["config_changes"] = [
                {
                    "version_id": r.id,
                    "ref": f"config/{did}/{r.id}",
                    "time": _iso(r.collected_at),
                    "added": r.added,
                    "removed": r.removed,
                }
                for r in await session.execute(
                    text(
                        "SELECT id, collected_at, added, removed FROM config_versions WHERE "
                        "device_id = :d AND NOT initial AND collected_at BETWEEN :a AND :b "
                        "ORDER BY collected_at DESC LIMIT 10"
                    ),
                    {"d": did, "a": plan.window_start - timedelta(hours=23), "b": plan.window_end},
                )
            ]
    if plan.port_flaps:
        with contextlib.suppress(Exception):
            out["port_flaps"] = [
                {
                    "ref": f"port/{did}/{r.if_index}",
                    "port": r.port_name,
                    "started_at": _iso(r.started_at),
                    "ended_at": _iso(r.ended_at),
                    "transitions": r.transitions,
                }
                for r in await session.execute(
                    text(
                        "SELECT if_index, port_name, started_at, ended_at, transitions FROM "
                        "port_flap_episodes WHERE device_id = :d AND started_at <= :b AND "
                        "(ended_at IS NULL OR ended_at >= :a) ORDER BY started_at DESC LIMIT 10"
                    ),
                    {"d": did, "a": plan.window_start, "b": plan.window_end},
                )
            ]
    if plan.maintenance:
        with contextlib.suppress(Exception):
            from netmon.services import maintenance as maint

            tz = "UTC"
            with contextlib.suppress(Exception):
                from netmon.services import settings_p3c

                tz = await settings_p3c.timezone(session)
            chain = []
            gid = sensor.group_id
            for _ in range(20):
                if gid is None:
                    break
                chain.append(gid)
                gid = (
                    await session.execute(
                        text("SELECT parent_id FROM groups WHERE id = :g"), {"g": gid}
                    )
                ).scalar()
            wins = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM maintenance_windows WHERE enabled AND (scope = "
                            "'global' OR "
                            "device_id = :d OR group_id = ANY(:g))"
                        ),
                        {"d": did, "g": chain},
                    )
                )
                .mappings()
                .all()
            )
            out["maintenance"] = [
                {"id": w["id"], "ref": f"maintenance/{w['id']}", "name": w["name"]}
                for w in wins
                if maint.covers(w, tz, problem_start) or maint.covers(w, tz, plan.window_end)
            ][:10]
    if plan.neighbours:
        with contextlib.suppress(Exception):
            out["neighbours"] = [
                {"id": r.nid, "ref": f"device/{r.nid}", "name": r.name, "status": r.st}
                for r in await session.execute(
                    text(
                        "SELECT n.nid, d.name, (SELECT (array_agg(s.status ORDER BY CASE s.status "
                        "WHEN 'down' THEN 0 WHEN 'unreachable' THEN 1 WHEN 'warning' THEN 2 ELSE 3 "
                        "END))[1] FROM sensors s WHERE s.device_id = n.nid) AS st FROM (SELECT "
                        "CASE WHEN a_device_id = :d THEN b_device_id ELSE a_device_id END AS nid "
                        "FROM topology_links WHERE NOT stale AND b_device_id IS NOT NULL AND "
                        "(a_device_id = :d OR b_device_id = :d)) n JOIN devices d ON d.id = n.nid "
                        "LIMIT 30"
                    ),
                    {"d": did},
                )
            ]
    if plan.other_problems:
        out["other_problems"] = [
            {
                "id": r.id,
                "ref": f"sensor/{r.id}",
                "name": r.name,
                "status": r.status,
                "message": _cut(r.message),
            }
            for r in await session.execute(
                text(
                    "SELECT id, name, status, status_message AS message FROM sensors WHERE "
                    "device_id = :d "
                    "AND id <> :s "
                    "AND status IN ('down','unreachable','warning') ORDER BY id LIMIT 20"
                ),
                {"d": did, "s": sensor_id},
            )
        ]
    return shrink(out, plan.max_bytes)


def refs_of(data: Any) -> list[str]:
    """Every `ref` value in the gathered data (the conversation's known refs)."""
    out: list[str] = []

    def visit(v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "ref" and isinstance(x, str):
                    out.append(x)
                else:
                    visit(x)
        elif isinstance(v, list):
            for x in v:
                visit(x)

    visit(data)
    seen: list[str] = []
    for r in out:
        if r not in seen:
            seen.append(r)
    return seen
