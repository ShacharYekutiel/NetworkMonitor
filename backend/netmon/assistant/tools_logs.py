"""[3b] Read-only tools over the device timeline, syslog and traps (api.md § [3b] Tools)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field
from sqlalchemy import text

from netmon.assistant.tools import (
    Args,
    ToolCtx,
    ToolDef,
    ToolOutput,
    cap,
    feature_gate,
    group_scope,
    iso,
    register,
    require_device,
    time_range,
)

TIMELINE_KINDS = (
    "status",
    "notification",
    "ack",
    "maintenance",
    "trap",
    "syslog",
    "port",
    "config",
    "new_mac",
)  # never "audit": the audit log is not projected


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _device_filter(ctx: ToolCtx, device_id: int | None, group_id: int | None) -> Any:
    if device_id is not None:
        await require_device(ctx.session, device_id)
        return [device_id]
    if group_id is not None:
        return await group_scope(ctx.session, group_id)
    return None


# --------------------------------------------------------------------------- get_timeline
class TimelineArgs(Args):
    device_id: int
    kinds: list[Literal[TIMELINE_KINDS]] | None = None  # type: ignore[valid-type]
    since: str | None = None
    until: str | None = None
    limit: int = Field(20, ge=1, le=100)


def _timeline_ref(ref: Any) -> str | None:
    if not isinstance(ref, dict):
        return None
    t, i = ref.get("type"), ref.get("id")
    if t in ("event", "trap", "syslog") and isinstance(i, int):
        return f"{t}/{i}"
    return None


async def get_timeline(ctx: ToolCtx, a: TimelineArgs) -> ToolOutput:
    from netmon.services.timeline import timeline

    start, end = time_range(a.since, a.until, ctx.now, max_days=7)
    dev = await require_device(ctx.session, a.device_id)
    from netmon.services.status import load_inventory

    inv = await load_inventory(ctx.session)
    kinds = list(a.kinds or TIMELINE_KINDS)
    if not ctx.gate.on("traps"):
        kinds = [k for k in kinds if k != "trap"]
    if not ctx.gate.on("syslog"):
        kinds = [k for k in kinds if k != "syslog"]
    res = await timeline(
        ctx.session, a.device_id, inv.ancestors(dev.group_id), kinds, start, end, None, a.limit
    )
    items = []
    for it in res.get("items", []):
        items.append(
            {
                "kind": it.get("kind"),
                "time": it.get("time"),
                "summary": cap(
                    " — ".join(x for x in (it.get("title") or "", it.get("message") or "") if x),
                    200,
                ),
                "status": it.get("status"),
                "sensor_name": cap(it.get("sensor_name")),
                "ref": _timeline_ref(it.get("ref")),
            }
        )
    return ToolOutput(
        {
            "device_ref": f"device/{a.device_id}",
            "since": iso(start),
            "until": iso(end),
            "items": items,
        },
        len(items),
        truncated=bool(res.get("next_cursor")),
    )


# --------------------------------------------------------------------------- search_syslog
class SyslogArgs(Args):
    device_id: int | None = None
    group_id: int | None = None
    severity_max: int | None = Field(None, ge=0, le=7)
    program: str | None = Field(None, max_length=50)
    text: str | None = Field(None, max_length=100)
    since: str | None = None
    until: str | None = None
    limit: int = Field(20, ge=1, le=100)


async def search_syslog(ctx: ToolCtx, a: SyslogArgs) -> ToolOutput:
    start, end = time_range(a.since, a.until, ctx.now, max_days=7)
    ids = await _device_filter(ctx, a.device_id, a.group_id)
    conds = ["x.time >= :a", "x.time <= :b"]
    p: dict[str, Any] = {"a": start, "b": end}
    if ids is not None:
        conds.append("x.device_id = ANY(:ids)")
        p["ids"] = ids
    if a.severity_max is not None:
        conds.append("x.severity <= :sev")
        p["sev"] = a.severity_max
    if a.program:
        conds.append("lower(x.program) = :prog")
        p["prog"] = a.program.lower()
    if a.text:
        conds.append("x.message ILIKE :q ESCAPE '\\'")
        p["q"] = f"%{_like(a.text)}%"
    rows = (
        (
            await ctx.session.execute(
                text(
                    "SELECT x.id, x.time, x.device_id, d.name AS device_name, x.hostname, "  # noqa: S608
                    "x.severity, x.facility, x.program, x.message FROM syslog_messages x "
                    "LEFT JOIN devices d ON d.id = x.device_id "
                    f"WHERE {' AND '.join(conds)} ORDER BY x.time DESC, x.id DESC LIMIT :lim"
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
            "ref": f"syslog/{r['id']}",
            "time": iso(r["time"]),
            "device_name": cap(r["device_name"]),
            "device_ref": f"device/{r['device_id']}" if r["device_id"] else None,
            "host": cap(r["hostname"] or "", 200),
            "severity": int(r["severity"]),
            "facility": int(r["facility"]),
            "program": cap(r["program"] or "", 64),
            "message": cap(r["message"] or "", 500),
        }
        for r in rows[: a.limit]
    ]
    return ToolOutput(
        {"since": iso(start), "until": iso(end), "items": items},
        len(items),
        truncated=len(rows) > a.limit,
    )


# --------------------------------------------------------------------------- syslog_stats
class SyslogStatsArgs(Args):
    device_id: int | None = None
    group_id: int | None = None
    since: str | None = None
    until: str | None = None
    by: Literal["severity", "program", "device", "hour"]


async def syslog_stats(ctx: ToolCtx, a: SyslogStatsArgs) -> ToolOutput:
    start, end = time_range(a.since, a.until, ctx.now, max_days=31)
    ids = await _device_filter(ctx, a.device_id, a.group_id)
    conds = ["x.time >= :a", "x.time <= :b"]
    p: dict[str, Any] = {"a": start, "b": end}
    if ids is not None:
        conds.append("x.device_id = ANY(:ids)")
        p["ids"] = ids
    key_sql = {
        "severity": "x.severity::text",
        "program": "x.program",
        "device": "coalesce(d.name, host(x.source_ip))",
        "hour": "to_char(date_trunc('hour', x.time AT TIME ZONE 'UTC'), "
        '\'YYYY-MM-DD"T"HH24:00:00"Z"\')',
    }[a.by]
    order = "1" if a.by == "hour" else "n DESC, 1"
    rows = (
        await ctx.session.execute(
            text(
                f"SELECT {key_sql} AS k, min(x.device_id) AS did, count(*) AS n "  # noqa: S608
                "FROM syslog_messages x LEFT JOIN devices d ON d.id = x.device_id "
                f"WHERE {' AND '.join(conds)} GROUP BY 1 ORDER BY {order} LIMIT 51"
            ),
            p,
        )
    ).all()
    items = []
    for r in rows[:50]:
        it: dict[str, Any] = {
            "key": cap(str(r.k) if r.k is not None else "", 200),
            "count": int(r.n),
        }
        if a.by == "device" and r.did is not None:
            it["ref"] = f"device/{r.did}"
        items.append(it)
    return ToolOutput(
        {
            "since": iso(start),
            "until": iso(end),
            "by": a.by,
            "items": items,
            "total": sum(int(r.n) for r in rows),
        },
        len(items),
        truncated=len(rows) > 50,
    )


# --------------------------------------------------------------------------- search_traps
class TrapsArgs(Args):
    device_id: int | None = None
    trap_oid: str | None = Field(None, max_length=255)
    text: str | None = Field(None, max_length=100)
    since: str | None = None
    until: str | None = None
    limit: int = Field(20, ge=1, le=100)


async def search_traps(ctx: ToolCtx, a: TrapsArgs) -> ToolOutput:
    start, end = time_range(a.since, a.until, ctx.now, max_days=7)
    conds = ["x.time >= :a", "x.time <= :b"]
    p: dict[str, Any] = {"a": start, "b": end}
    if a.device_id is not None:
        await require_device(ctx.session, a.device_id)
        conds.append("x.device_id = :did")
        p["did"] = a.device_id
    if a.trap_oid:
        oid = a.trap_oid.strip().lstrip(".")
        conds.append("(x.trap_oid = :oid OR x.trap_oid LIKE :oidp ESCAPE '\\')")
        p["oid"] = oid
        p["oidp"] = _like(oid) + ".%"
    if a.text:
        conds.append("x.summary ILIKE :q ESCAPE '\\'")
        p["q"] = f"%{_like(a.text)}%"
    rows = (
        (
            await ctx.session.execute(
                text(
                    "SELECT x.id, x.time, x.device_id, d.name AS device_name, x.trap_name, "  # noqa: S608
                    "x.trap_oid, x.summary FROM traps x LEFT JOIN devices d ON d.id = x.device_id "
                    f"WHERE {' AND '.join(conds)} ORDER BY x.time DESC, x.id DESC LIMIT :lim"
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
            "ref": f"trap/{r['id']}",
            "time": iso(r["time"]),
            "device_name": cap(r["device_name"]),
            "device_ref": f"device/{r['device_id']}" if r["device_id"] else None,
            "trap_name": cap(r["trap_name"]),
            "trap_oid": cap(r["trap_oid"], 255),
            "summary": cap(r["summary"] or "", 300),
        }
        for r in rows[: a.limit]
    ]
    return ToolOutput(
        {"since": iso(start), "until": iso(end), "items": items},
        len(items),
        truncated=len(rows) > a.limit,
    )


register(
    ToolDef(
        "get_timeline",
        "Timeline of one device (status changes, notifications, acknowledgements, maintenance, "
        "traps, syslog, port and config changes) in a range up to 7 days, newest first.",
        TimelineArgs,
        get_timeline,
        gate=feature_gate("timeline"),
        untrusted=("summary",),
        source_key="timeline",
    )
)
register(
    ToolDef(
        "search_syslog",
        "Search received syslog messages (device or group, severity ≤ N, program, literal text) in "
        "a range up to 7 days, newest first.",
        SyslogArgs,
        search_syslog,
        gate=feature_gate("syslog"),
        untrusted=("message", "host", "program"),
        source_key="syslog",
    )
)
register(
    ToolDef(
        "syslog_stats",
        "Syslog message counts grouped by severity, program, device or hour (range up to 31 days).",
        SyslogStatsArgs,
        syslog_stats,
        gate=feature_gate("syslog"),
        untrusted=("key",),
        source_key="syslog",
    )
)
register(
    ToolDef(
        "search_traps",
        "Search received SNMP traps (device, trap OID prefix, literal text in the summary) in a "
        "range up to 7 days, newest first.",
        TrapsArgs,
        search_traps,
        gate=feature_gate("traps"),
        untrusted=("summary", "trap_name"),
        source_key="traps",
    )
)
