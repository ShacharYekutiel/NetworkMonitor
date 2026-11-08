"""[3] Reports: gather `ReportData` (api.md) from the database with short read-only queries.

Each section is omitted with a note (`feature_off` | `no_data`) when its source feature is OFF
or it has no rows."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.reports import compute
from netmon.services.serialize import iso

log = logging.getLogger("netmon.reports.data")

SECTIONS = (
    "availability",
    "top_alerts",
    "flapping_ports",
    "port_errors",
    "config_changes",
    "new_devices",
    "top_talkers",
)
SECTION_FEATURE = {
    "flapping_ports": "flapping",
    "config_changes": "config_backup",
    "new_devices": "new_device_alerts",
    "top_talkers": "flows",
}
MAX_AVAIL_ROWS = 2000
TOP_TALKERS_MAX_DAYS = 31
_MAINT_RE = re.compile(r'^Maintenance "(.*)" (started|ended)$')
# the window name of a maintenance event in SQL (the colon of "(?:" escaped for text())
_NAME = r"""substring(message from '^Maintenance "(.*)" (?\:started|ended)$')"""


async def scope_devices(
    session: AsyncSession, group_ids: list[int], device_ids: list[int]
) -> list[Any]:
    """Devices in scope: groups recursive ∪ devices; both empty = everything."""
    if not group_ids and not device_ids:
        q = "SELECT d.id, d.name, d.group_id, g.name AS group_name FROM devices d "
        q += "JOIN groups g ON g.id = d.group_id ORDER BY lower(d.name), d.id"
        return list((await session.execute(text(q))).all())
    rows = await session.execute(
        text(
            """
            WITH RECURSIVE sub AS (
                SELECT id FROM groups WHERE id = ANY(:gids)
                UNION SELECT g.id FROM groups g JOIN sub ON g.parent_id = sub.id)
            SELECT d.id, d.name, d.group_id, g.name AS group_name FROM devices d
            JOIN groups g ON g.id = d.group_id
            WHERE d.group_id IN (SELECT id FROM sub) OR d.id = ANY(:dids)
            ORDER BY lower(d.name), d.id
            """
        ),
        {"gids": list(group_ids), "dids": list(device_ids)},
    )
    return list(rows.all())


async def scope_label(session: AsyncSession, group_ids: list[int], device_ids: list[int]) -> str:
    if not group_ids and not device_ids:
        return "All devices"
    parts: list[str] = []
    if group_ids:
        names = (
            await session.execute(
                text("SELECT name FROM groups WHERE id = ANY(:i) ORDER BY name"), {"i": group_ids}
            )
        ).scalars()
        parts.extend(names)
    if device_ids:
        parts.append(f"{len(device_ids)} device(s)")
    return ", ".join(parts)[:200]


async def _availability(
    session: AsyncSession, devices: list[Any], start: datetime, end: datetime
) -> dict[str, Any] | None:
    if not devices:
        return None
    ids = [d.id for d in devices]
    sensors = {
        r.device_id: r.id
        for r in await session.execute(
            text(
                """
                SELECT DISTINCT ON (device_id) device_id, id FROM sensors
                WHERE device_id = ANY(:ids) AND enabled AND type IN ('ping', 'snmp_system')
                ORDER BY device_id, (type <> 'ping'), sort_order, id
                """
            ),
            {"ids": ids},
        )
    }
    sids = list(sensors.values())
    initial: dict[int, str | None] = {}
    changes: dict[int, list[tuple[datetime, str | None]]] = defaultdict(list)
    flagged: dict[int, list[compute.Interval]] = defaultdict(list)
    if sids:
        for r in await session.execute(
            text(
                """
                SELECT DISTINCT ON (sensor_id) sensor_id, new_status FROM events
                WHERE kind = 'status_change' AND sensor_id = ANY(:s) AND time < :start
                ORDER BY sensor_id, time DESC, id DESC
                """
            ),
            {"s": sids, "start": start},
        ):
            initial[r.sensor_id] = r.new_status
        rows = (
            await session.execute(
                text(
                    "SELECT sensor_id, time, new_status, in_maintenance FROM events "
                    "WHERE kind = 'status_change' AND sensor_id = ANY(:s) "
                    "AND time >= :start AND time < :end ORDER BY sensor_id, time, id"
                ),
                {"s": sids, "start": start, "end": end},
            )
        ).all()
        by_sensor: dict[int, list[Any]] = defaultdict(list)
        for r in rows:
            changes[r.sensor_id].append((r.time, r.new_status))
            by_sensor[r.sensor_id].append(r)
        # the events' in_maintenance flag: a problem that began during maintenance is excluded
        # until the next change
        for sid, evs in by_sensor.items():
            for i, r in enumerate(evs):
                if r.in_maintenance and r.new_status == "down":
                    nxt = evs[i + 1].time if i + 1 < len(evs) else end
                    flagged[sid].append((r.time, nxt))
    maint = await _maintenance_by_device(session, devices, start, end)
    rows_out: list[dict[str, Any]] = []
    not_measured: list[dict[str, Any]] = []
    per_group: dict[int, list[compute.Availability]] = defaultdict(list)
    group_names: dict[int, str] = {}
    all_results: list[compute.Availability] = []
    for d in devices:
        sid = sensors.get(d.id)
        if sid is None:
            not_measured.append({"device_id": d.id, "device_name": d.name})
            continue
        a = compute.availability(
            initial.get(sid),
            changes.get(sid, []),
            list(maint.get(d.id, [])) + flagged.get(sid, []),
            start,
            end,
        )
        all_results.append(a)
        per_group[d.group_id].append(a)
        group_names[d.group_id] = d.group_name
        rows_out.append(
            {
                "device_id": d.id,
                "device_name": d.name,
                "group_name": d.group_name,
                "sensor_id": sid,
                "availability": a.availability,
                "measured_s": int(a.measured_s),
                "downtime_s": int(a.downtime_s),
                "outages": a.outages,
                "longest_outage_s": int(a.longest_outage_s),
            }
        )
    rows_out.sort(
        key=lambda r: (
            r["availability"] is None,
            r["availability"] if r["availability"] is not None else 0,
            -r["downtime_s"],
            r["device_name"].lower(),
        )
    )
    groups = [
        {
            "group_id": gid,
            "group_name": group_names[gid],
            "availability": compute.group_availability(items),
            "devices": len(items),
        }
        for gid, items in sorted(per_group.items(), key=lambda kv: group_names[kv[0]].lower())
    ]
    return {
        "overall": compute.group_availability(all_results),
        "devices": rows_out[:MAX_AVAIL_ROWS],
        "not_measured": not_measured[:MAX_AVAIL_ROWS],
        "groups": groups,
    }


async def _maintenance_by_device(
    session: AsyncSession, devices: list[Any], start: datetime, end: datetime
) -> dict[int, list[compute.Interval]]:
    """Maintenance coverage from the `maintenance` started/ended events (device, group
    (recursive, current tree) or global windows); the last event before the period tells
    whether a window was already open at the start."""
    rows = (
        await session.execute(
            text(
                f"""
                (SELECT DISTINCT ON (coalesce(device_id, 0), coalesce(group_id, 0),
                                     {_NAME})
                        time, device_id, group_id, device_name, message FROM events
                 WHERE kind = 'maintenance' AND time < :start
                   AND time > :start - interval '400 days'
                 ORDER BY coalesce(device_id, 0), coalesce(group_id, 0),
                          {_NAME},
                          time DESC, id DESC)
                UNION ALL
                (SELECT time, device_id, group_id, device_name, message FROM events
                 WHERE kind = 'maintenance' AND time >= :start AND time < :end)
                """
            ),
            {"start": start, "end": end},
        )
    ).all()
    if not rows:
        return {}
    # last-before rows: only `started` matter (an open window at the period start)
    per_key: dict[tuple[str, int | None], list[tuple[datetime, str, str]]] = defaultdict(list)
    for r in rows:
        m = _MAINT_RE.match(r.message or "")
        if not m:
            continue
        name, verb = m.group(1), m.group(2)
        if r.device_id is None and r.device_name:
            continue  # a device window whose device was deleted since: not a global window
        if re.fullmatch(r"#\d+", name):
            continue  # an unknown / deleted window (written as "#<id>"), scope not known
        if r.time < start:
            if verb != "started":
                continue
            t = start
        else:
            t = r.time
        if r.device_id is not None:
            scope = ("device", r.device_id)
        elif r.group_id is not None:
            scope = ("group", r.group_id)
        else:
            scope = ("global", None)
        per_key[scope].append((t, f"{scope[0]}:{scope[1]}:{name}", verb))
    intervals = {k: compute.maintenance_intervals(v, start, end) for k, v in per_key.items()}
    group_ids = [k[1] for k in intervals if k[0] == "group"]
    descendants: dict[int, set[int]] = {}
    if group_ids:
        for gid in group_ids:
            sub = (
                await session.execute(
                    text(
                        "WITH RECURSIVE sub AS (SELECT id FROM groups WHERE id = :g UNION "
                        "SELECT g.id FROM groups g JOIN sub ON g.parent_id = sub.id) "
                        "SELECT id FROM sub"
                    ),
                    {"g": gid},
                )
            ).scalars()
            descendants[int(gid)] = set(sub)  # type: ignore[arg-type]
    out: dict[int, list[compute.Interval]] = defaultdict(list)
    for d in devices:
        for (kind, ref), ivs in intervals.items():
            if (
                kind == "global"
                or (kind == "device" and ref == d.id)
                or (kind == "group" and d.group_id in descendants.get(int(ref or 0), set()))
            ):
                out[d.id].extend(ivs)
    return out


async def _top_alerts(
    session: AsyncSession, device_ids: list[int], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT e.sensor_id, max(e.sensor_name) AS sensor_name,
                       max(e.device_name) AS device_name, count(*) AS n
                FROM events e
                WHERE e.kind = 'notification_sent' AND e.time >= :start AND e.time < :end
                  AND e.sensor_id IS NOT NULL AND e.device_id = ANY(:d)
                  AND e.new_status IN ('down', 'warning')
                GROUP BY e.sensor_id ORDER BY n DESC, e.sensor_id LIMIT 20
                """
            ),
            {"start": start, "end": end, "d": device_ids},
        )
    ).all()
    if not rows:
        return []
    sids = [r.sensor_id for r in rows]
    problem = await _problem_seconds(session, sids, start, end)
    return [
        {
            "sensor_id": r.sensor_id,
            "sensor_name": r.sensor_name or "",
            "device_name": r.device_name or "",
            "notifications": int(r.n),
            "problem_s": int(problem.get(r.sensor_id, 0)),
        }
        for r in rows
    ]


async def _problem_seconds(
    session: AsyncSession, sids: list[int], start: datetime, end: datetime
) -> dict[int, float]:
    initial = {
        r.sensor_id: r.new_status
        for r in await session.execute(
            text(
                "SELECT DISTINCT ON (sensor_id) sensor_id, new_status FROM events "
                "WHERE kind = 'status_change' AND sensor_id = ANY(:s) AND time < :start "
                "ORDER BY sensor_id, time DESC, id DESC"
            ),
            {"s": sids, "start": start},
        )
    }
    changes: dict[int, list[tuple[datetime, str | None]]] = defaultdict(list)
    for r in await session.execute(
        text(
            "SELECT sensor_id, time, new_status FROM events WHERE kind = 'status_change' "
            "AND sensor_id = ANY(:s) AND time >= :start AND time < :end ORDER BY time, id"
        ),
        {"s": sids, "start": start, "end": end},
    ):
        changes[r.sensor_id].append((r.time, r.new_status))
    out: dict[int, float] = {}
    for sid in sids:
        segs = compute.status_segments(initial.get(sid), changes.get(sid, []), start, end)
        out[sid] = sum((e - s).total_seconds() for s, e, st in segs if st in ("down", "warning"))
    return out


async def _flapping(
    session: AsyncSession, device_ids: list[int], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            """
            SELECT p.device_id, d.name AS device_name, p.if_index, max(p.port_name) AS port_name,
                   count(*) AS episodes, sum(p.transitions) AS transitions
            FROM port_flap_episodes p JOIN devices d ON d.id = p.device_id
            WHERE p.device_id = ANY(:d) AND p.started_at < :end
              AND coalesce(p.ended_at, :end) >= :start
            GROUP BY p.device_id, d.name, p.if_index
            ORDER BY episodes DESC, transitions DESC LIMIT 20
            """
        ),
        {"d": device_ids, "start": start, "end": end},
    )
    return [
        {
            "device_id": r.device_id,
            "device_name": r.device_name,
            "if_index": r.if_index,
            "port_name": r.port_name or "",
            "episodes": int(r.episodes),
            "transitions": int(r.transitions or 0),
        }
        for r in rows
    ]


async def _port_errors(
    session: AsyncSession, device_ids: list[int], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in await session.execute(
        text(
            """
            SELECT s.device_id, d.name AS device_name, s.name AS sensor_name,
                   (s.config->>'if_index')::int AS if_index,
                   sum(m.value * s.interval_s) AS errors
            FROM measurements m JOIN sensors s ON s.id = m.sensor_id
            JOIN devices d ON d.id = s.device_id
            WHERE s.type = 'snmp_interface' AND s.device_id = ANY(:d)
              AND m.channel IN ('in_errors_ps', 'out_errors_ps')
              AND m.time >= :start AND m.time < :end
            GROUP BY s.device_id, d.name, s.name, s.config->>'if_index'
            HAVING sum(m.value * s.interval_s) >= 1
            ORDER BY errors DESC LIMIT 20
            """
        ),
        {"d": device_ids, "start": start, "end": end},
    ):
        out.append(
            {
                "device_id": r.device_id,
                "device_name": r.device_name,
                "if_index": r.if_index,
                "port_name": r.sensor_name,
                "errors": int(round(float(r.errors))),
                "source": "sensor",
            }
        )
    for r in await session.execute(
        text(
            """
            SELECT p.device_id, d.name AS device_name, p.if_index, p.name,
                   coalesce(p.in_errors_delta, 0) + coalesce(p.out_errors_delta, 0) AS errors
            FROM switch_ports p JOIN devices d ON d.id = p.device_id
            WHERE p.device_id = ANY(:d)
              AND coalesce(p.in_errors_delta, 0) + coalesce(p.out_errors_delta, 0) > 0
            ORDER BY errors DESC LIMIT 20
            """
        ),
        {"d": device_ids},
    ):
        out.append(
            {
                "device_id": r.device_id,
                "device_name": r.device_name,
                "if_index": r.if_index,
                "port_name": r.name or "",
                "errors": int(r.errors),
                "source": "snapshot",
            }
        )
    return out


async def _config_changes(
    session: AsyncSession, device_ids: list[int], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            """
            SELECT v.device_id, d.name AS device_name, count(*) AS changes,
                   max(v.collected_at) AS last_change_at, sum(v.added) AS added,
                   sum(v.removed) AS removed
            FROM config_versions v JOIN devices d ON d.id = v.device_id
            WHERE NOT v.initial AND v.device_id = ANY(:d)
              AND v.collected_at >= :start AND v.collected_at < :end
            GROUP BY v.device_id, d.name ORDER BY changes DESC, lower(d.name)
            """
        ),
        {"d": device_ids, "start": start, "end": end},
    )
    return [
        {
            "device_id": r.device_id,
            "device_name": r.device_name,
            "changes": int(r.changes),
            "last_change_at": iso(r.last_change_at),
            "added": int(r.added or 0),
            "removed": int(r.removed or 0),
        }
        for r in rows
    ]


async def _new_devices(
    session: AsyncSession, device_ids: list[int], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            """
            SELECT k.mac, k.vendor, k.first_ip, k.first_hostname, d.name AS switch_name,
                   k.first_port_name, k.detected_at, k.ack_at
            FROM known_macs k LEFT JOIN devices d ON d.id = k.first_device_id
            WHERE k.detected_at >= :start AND k.detected_at < :end
              AND (k.first_device_id IS NULL OR k.first_device_id = ANY(:d))
            ORDER BY k.detected_at DESC LIMIT 500
            """
        ),
        {"d": device_ids, "start": start, "end": end},
    )
    return [
        {
            "mac": r.mac,
            "vendor": r.vendor,
            "ip": r.first_ip,
            "hostname": r.first_hostname,
            "switch_name": r.switch_name,
            "port_name": r.first_port_name,
            "detected_at": iso(r.detected_at),
            "acknowledged": r.ack_at is not None,
        }
        for r in rows
    ]


async def _top_talkers(
    session: AsyncSession, start: datetime, end: datetime, rt: Any = None
) -> dict[str, Any] | None:
    """Top 10 hosts and applications by bytes (netmon.flows.query, hourly objects)."""

    from netmon.flows import query as fq

    rng = fq.Range(start.replace(minute=0, second=0, microsecond=0), end, "hour")
    ids = await fq.exporter_ids(session, None, None)
    if not ids:
        return None
    mode = getattr(rt, "flows_rollup", None) or "table"
    now = rt.clock.now() if rt is not None else end
    flows = getattr(rt, "flows", None)
    if flows is not None and hasattr(flows, "services"):
        services = flows.services()
    else:
        from netmon.flows.services import ServiceMap

        services = ServiceMap(overrides=[])
    hosts = await fq.top_talkers(session, rng, ids, mode=mode, now=now, side="both", limit=10)
    apps = await fq.top_applications(
        session, rng, ids, mode=mode, now=now, limit=10, services=services
    )
    if not hosts.get("items") and not apps.get("items"):
        return None
    return {"hosts": list(hosts.get("items") or []), "applications": list(apps.get("items") or [])}


async def gather(
    session: AsyncSession,
    report: dict[str, Any],
    start: datetime,
    end: datetime,
    tz: str,
    features: dict[str, bool],
    now: datetime,
    rt: Any = None,
) -> dict[str, Any]:
    sections = [s for s in report.get("sections") or [] if s in SECTIONS]
    gids = list(report.get("scope_group_ids") or [])
    dids = list(report.get("scope_device_ids") or [])
    devices = await scope_devices(session, gids, dids)
    device_ids = [d.id for d in devices]
    data: dict[str, Any] = {
        "generated_at": iso(now),
        "period_start": iso(start),
        "period_end": iso(end),
        "timezone": tz,
        "scope_label": await scope_label(session, gids, dids),
        "sections": sections,
        "notes": [],
    }
    for sec in sections:
        feat = SECTION_FEATURE.get(sec)
        if feat and not features.get(feat, False):
            data["notes"].append({"section": sec, "note": "feature_off"})
            continue
        try:
            value: Any
            if sec == "availability":
                value = await _availability(session, devices, start, end)
            elif sec == "top_alerts":
                value = await _top_alerts(session, device_ids, start, end)
            elif sec == "flapping_ports":
                value = await _flapping(session, device_ids, start, end)
            elif sec == "port_errors":
                value = await _port_errors(session, device_ids, start, end)
            elif sec == "config_changes":
                value = await _config_changes(session, device_ids, start, end)
            elif sec == "new_devices":
                value = await _new_devices(session, device_ids, start, end)
            else:
                if (end - start).total_seconds() > TOP_TALKERS_MAX_DAYS * 86400:
                    value = None
                else:
                    value = await _top_talkers(session, start, end, rt)
        except Exception:  # noqa: BLE001 - a missing table/feature never fails the report
            log.warning("Report section %s failed", sec, exc_info=True)
            await session.rollback()
            value = None
        if not value:
            data["notes"].append({"section": sec, "note": "no_data"})
            continue
        data[sec] = value
    return data
