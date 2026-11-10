"""[3b] Read-only flow tools (`flows` feature): top talkers / applications / conversations and
an interface traffic series, through the phase-3 `netmon.flows.query` functions (§ 13.3
ranges: tops ≤ 31 days, conversations ≤ 24 h)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from netmon.assistant import links
from netmon.assistant.tools import (
    Args,
    ToolCtx,
    ToolDef,
    ToolError,
    ToolOutput,
    cap,
    feature_gate,
    register,
    require_device,
)


def _services(ctx: ToolCtx) -> Any:
    c = getattr(ctx.rt, "flows", None)
    if c is not None:
        try:
            return c.services()
        except Exception:  # noqa: BLE001
            pass
    from netmon.flows.services import default_map

    return default_map()


def _range(ctx: ToolCtx, since: str | None, until: str | None, conv: bool) -> Any:
    from netmon.flows import query

    try:
        return query.make_range(ctx.now, since, until, query.CONV_MAX if conv else query.RANGE_MAX)
    except query.QueryError as exc:
        code = "range_too_large" if "limited" in exc.msg else "invalid_arguments"
        raise ToolError(code, f"{exc.loc}: {exc.msg}") from None


def _item(raw: dict[str, Any], kind: str, base_query: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (
        "key",
        "label",
        "ip",
        "hostname",
        "protocol",
        "port",
        "service",
        "src_ip",
        "dst_ip",
        "src_port",
        "dst_port",
        "src_hostname",
        "dst_hostname",
        "bytes",
        "packets",
        "flows",
        "share",
    ):
        if key in raw:
            v = raw[key]
            out[key] = cap(v, 200) if isinstance(v, str) else v
    if raw.get("device_id"):
        out["device_ref"] = f"device/{raw['device_id']}"
    q = dict(base_query)
    if kind == "talkers" and raw.get("ip"):
        q["host"] = raw["ip"]
    out["ref"] = links.flows_ref(q)
    return out


class FlowTopArgs(Args):
    kind: Literal["talkers", "applications", "conversations"]
    device_id: int | None = None
    exporter_id: int | None = None
    if_index: int | None = Field(None, ge=0)
    direction: Literal["in", "out"] | None = None
    side: Literal["src", "dst", "both"] = "both"
    since: str | None = None
    until: str | None = None
    limit: int = Field(10, ge=1, le=25)


async def flow_top(ctx: ToolCtx, a: FlowTopArgs) -> ToolOutput:
    from netmon.flows import query

    if a.if_index is not None and a.device_id is None and a.exporter_id is None:
        raise ToolError("invalid_arguments", "if_index needs device_id or exporter_id")
    if a.device_id is not None:
        await require_device(ctx.session, a.device_id)
    rng = _range(ctx, a.since, a.until, conv=a.kind == "conversations")
    ids = await query.exporter_ids(ctx.session, a.exporter_id, a.device_id)
    mode = getattr(ctx.rt, "flows_rollup", None)
    if a.kind == "talkers":
        res = await query.top_talkers(
            ctx.session,
            rng,
            ids,
            mode=mode,
            now=ctx.now,
            side=a.side,
            limit=a.limit,
            if_index=a.if_index,
            direction=a.direction,
        )
    elif a.kind == "applications":
        res = await query.top_applications(
            ctx.session,
            rng,
            ids,
            mode=mode,
            now=ctx.now,
            limit=a.limit,
            services=_services(ctx),
            if_index=a.if_index,
            direction=a.direction,
        )
    else:
        rng.source = "minute"
        res = await query.top_conversations(
            ctx.session,
            rng,
            ids,
            limit=a.limit,
            services=_services(ctx),
            if_index=a.if_index,
            direction=a.direction,
        )
    base = {
        "tab": a.kind,
        "device_id": a.device_id,
        "exporter_id": a.exporter_id,
        "if_index": a.if_index,
        "since": res.get("since"),
        "until": res.get("until"),
    }
    items = [_item(i, a.kind, base) for i in (res.get("items") or [])[: a.limit]]
    data = {
        "kind": a.kind,
        "since": res.get("since"),
        "until": res.get("until"),
        "total_bytes": res.get("total_bytes"),
        "other_bytes": res.get("other_bytes"),
        "ref": links.flows_ref(base),
        "items": items,
    }
    return ToolOutput(data, len(items))


class FlowSeriesArgs(Args):
    device_id: int
    if_index: int = Field(ge=0)
    since: str | None = None
    until: str | None = None
    split: Literal["none", "app", "host"] = "none"


def _downsample(points: list[list[Any]], n: int = 60) -> list[list[Any]]:
    if len(points) <= n:
        return points
    step = len(points) / n
    out = []
    for i in range(n):
        chunk = points[int(i * step) : int((i + 1) * step)] or [points[-1]]
        vals = [p[1] for p in chunk if isinstance(p[1], int | float)]
        out.append([chunk[0][0], int(sum(vals) / len(vals)) if vals else None])
    return out


async def flow_interface_series(ctx: ToolCtx, a: FlowSeriesArgs) -> ToolOutput:
    from netmon.flows import query

    await require_device(ctx.session, a.device_id)
    rng = _range(ctx, a.since, a.until, conv=False)
    ids = await query.exporter_ids(ctx.session, None, a.device_id)
    if not ids:
        return ToolOutput(
            {"interface": None, "series": []}, 0, note="the device does not export flows"
        )
    eid = ids[0] if len(ids) == 1 else None
    res = await query.series(
        ctx.session,
        rng,
        ids,
        mode=getattr(ctx.rt, "flows_rollup", None),
        now=ctx.now,
        split=a.split,
        services=_services(ctx),
        exporter_id=eid,
        if_index=a.if_index,
        direction=None,
    )
    series = []
    for s in (res.get("series") or [])[:9]:
        pts = s.get("points") or []  # [time, bits per second]
        conv = [
            [p[0], int(p[1]) if isinstance(p[1], int | float) else None]
            for p in pts
            if isinstance(p, list | tuple) and len(p) >= 2
        ]
        series.append(
            {
                "label": cap(str(s.get("label") or s.get("key") or ""), 200),
                "direction": s.get("direction"),
                "points": _downsample(conv),
            }
        )
    iface = res.get("interface") or {}
    data = {
        "interface": cap(iface.get("name"), 200) if iface else None,
        "ref": links.flows_ref(
            {
                "device_id": a.device_id,
                "if_index": a.if_index,
                "since": res.get("since"),
                "until": res.get("until"),
            }
        ),
        "port_ref": f"port/{a.device_id}/{a.if_index}",
        "since": res.get("since"),
        "until": res.get("until"),
        "series": series,
    }
    return ToolOutput(data, len(series))


register(
    ToolDef(
        "flow_top",
        "Flow statistics: top talkers (hosts), applications (protocol/port) or conversations "
        "(range up to 31 days; conversations up to 24 h; default last hour), optionally for a "
        "device/exporter and interface.",
        FlowTopArgs,
        flow_top,
        gate=feature_gate("flows"),
        untrusted=("hostname", "src_hostname", "dst_hostname", "label"),
        source_key="flows",
    )
)
register(
    ToolDef(
        "flow_interface_series",
        "Traffic of one interface over time from flows (bits per second, ≤ 60 points per series), "
        "optionally split by application or host.",
        FlowSeriesArgs,
        flow_interface_series,
        gate=feature_gate("flows"),
        untrusted=("label",),
        source_key="flows",
    )
)
