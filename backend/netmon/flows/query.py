"""[3] Flow queries (api.md "[3] Flows"): exporters, top talkers / applications /
conversations, interfaces and series. Ranges <= 48 h read the 1-minute tables, longer ranges
the hourly objects (max 31 days); in fallback (table) mode the last hours, which the hourly
rollup may not have reached yet, are read from the 1-minute tables (union)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.flows.services import ServiceMap, proto_name
from netmon.ingest import attribution
from netmon.services.serialize import iso

if TYPE_CHECKING:
    from netmon.flows.service import FlowCollector

MINUTE_MAX = timedelta(hours=48)
RANGE_MAX = timedelta(days=31)
CONV_MAX = timedelta(hours=24)
DEFAULT_RANGE = timedelta(hours=1)
TARGET_POINTS = 120
MINUTE_BUCKETS = (60, 300, 900, 1800, 3600)
HOUR_BUCKETS = (3600, 7200, 10800, 21600, 43200, 86400)
UNION_HOURS = 3  # fallback: hours served from the 1-minute tables
SERIES_SPLIT_TOP = 8
FAMILY_KEYS = {
    "if": ("if_index", "direction"),
    "app": ("protocol", "port"),
    "host": ("ip", "direction", "other"),
}


class QueryError(ValueError):
    def __init__(self, loc: str, msg: str) -> None:
        super().__init__(msg)
        self.loc = loc
        self.msg = msg


@dataclass
class Range:
    since: datetime
    until: datetime
    source: str  # minute | hour

    @property
    def seconds(self) -> float:
        return (self.until - self.since).total_seconds()


def parse_time(value: str | None, loc: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise QueryError(loc, "must be an ISO 8601 date-time") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def make_range(
    now: datetime, since: str | None, until: str | None, max_range: timedelta = RANGE_MAX
) -> Range:
    u = parse_time(until, "until") or now
    s = parse_time(since, "since") or (u - DEFAULT_RANGE)
    if s >= u:
        raise QueryError("since", "must be before until")
    if u - s > max_range:
        hours = int(max_range.total_seconds() // 3600)
        raise QueryError("since", f"the range is limited to {hours} hours")
    source = "minute" if u - s <= MINUTE_MAX else "hour"
    if source == "hour":
        s = s.replace(minute=0, second=0, microsecond=0)
    return Range(s, u, source)


def bucket_seconds(rng: Range) -> int:
    want = rng.seconds / TARGET_POINTS
    choices = MINUTE_BUCKETS if rng.source == "minute" else HOUR_BUCKETS
    for c in choices:
        if c >= want:
            return c
    return choices[-1]


def relation(family: str, rng: Range, mode: str | None) -> str:
    """SQL relation (time, exporter_id, keys, bytes, packets, flows) for the range's source."""
    keys = ", ".join(FAMILY_KEYS[family])
    minute = f"SELECT time, exporter_id, {keys}, bytes, packets, flows FROM flow_{family}_1m"
    if rng.source == "minute" or mode is None:
        return f"({minute})"
    if mode == "cagg":
        return f"(SELECT time, exporter_id, {keys}, bytes, packets, flows FROM flow_{family}_1h)"
    return (
        f"(SELECT time, exporter_id, {keys}, bytes, packets, flows FROM flow_{family}_1h "
        "WHERE time < :cut UNION ALL "
        f"SELECT date_bin(INTERVAL '1 hour', time, TIMESTAMPTZ '2000-01-01') AS time, "
        f"exporter_id, {keys}, bytes, packets, flows FROM flow_{family}_1m WHERE time >= :cut)"
    )


def _cut(now: datetime) -> datetime:
    return now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=UNION_HOURS - 1)


# ================================================================ helpers
async def exporter_ids(
    session: AsyncSession, exporter_id: int | None, device_id: int | None
) -> list[int]:
    where = ["state = 'accepted'"]
    params: dict[str, Any] = {}
    if exporter_id is not None:
        where = ["id = :e"]
        params["e"] = exporter_id
    if device_id is not None:
        where.append("device_id = :d")
        params["d"] = device_id
    rows = await session.execute(
        text(f"SELECT id FROM flow_exporters WHERE {' AND '.join(where)}"),
        params,  # noqa: S608
    )
    return [int(r[0]) for r in rows]


async def _names(session: AsyncSession, ips: list[str]) -> tuple[dict[str, str], dict[str, int]]:
    """Reverse-DNS names from `dns_cache` (cache only) and device ids of the IPs."""
    if not ips:
        return {}, {}
    names = {
        str(r.ip): str(r.name)
        for r in await session.execute(
            text("SELECT ip, name FROM dns_cache WHERE ip = ANY(:ips) AND name IS NOT NULL"),
            {"ips": ips},
        )
    }
    devices = [
        {
            "id": r.id,
            "name": r.name,
            "host": r.host,
            "enabled": r.enabled,
            "source_addresses": [str(a) for a in r.source_addresses or []],
        }
        for r in await session.execute(
            text("SELECT id, name, host, enabled, source_addresses FROM devices")
        )
    ]
    smap = attribution.build(devices, {})
    ids = {ip: smap.by_ip[ip] for ip in ips if ip in smap.by_ip}
    return names, ids


def _ip(v: Any) -> str:
    return str(v).split("/", 1)[0]


def _iface_filter(if_index: int | None, direction: str | None) -> str:
    if if_index is None:
        return ""
    if direction == "in":
        return " AND in_if = :ifx"
    if direction == "out":
        return " AND out_if = :ifx"
    return " AND (in_if = :ifx OR out_if = :ifx)"


def _top_out(rng: Range, total: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    shown = sum(i["bytes"] for i in items)
    for i in items:
        i["share"] = round(i["bytes"] / total, 4) if total else 0.0
    return {
        "since": iso(rng.since),
        "until": iso(rng.until),
        "source": rng.source,
        "total_bytes": int(total),
        "items": items,
        "other_bytes": max(0, int(total) - shown),
    }


# ================================================================ exporters
async def exporters_out(
    collector: FlowCollector | None,
    session: AsyncSession,
    *,
    ids: list[int] | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    where, params = [], {}
    if ids is not None:
        where.append("e.id = ANY(:ids)")
        params["ids"] = ids
    if state:
        where.append("e.state = :s")
        params["s"] = state
    sql = (
        "SELECT e.id, host(e.address) AS address, e.device_id, d.name AS device_name, e.state, "
        "e.protocols, e.sampling_override, e.sampling_seen, e.first_seen, e.last_seen "
        "FROM flow_exporters e LEFT JOIN devices d ON d.id = e.device_id"
        + (f" WHERE {' AND '.join(where)}" if where else "")
        + " ORDER BY e.state, e.address"
    )
    out = []
    for r in (await session.execute(text(sql), params)).mappings():
        addr = attribution.normalize_ip(str(r["address"])) or str(r["address"])
        mem = collector.exporters.get(addr) if collector is not None else None
        flags = (
            collector.exporter_flags(mem)
            if collector is not None and mem is not None
            else {
                "records_per_s": 0.0,
                "both_directions_warning": False,
                "unsampled_warning": False,
            }
        )
        protocols = set(r["protocols"] or []) | (mem.protocols if mem is not None else set())
        last_seen = r["last_seen"]
        if (
            mem is not None
            and mem.last_seen is not None
            and (last_seen is None or mem.last_seen > last_seen)
        ):
            last_seen = mem.last_seen
        sampling_seen = r["sampling_seen"]
        if mem is not None and mem.sampling_seen is not None:
            sampling_seen = mem.sampling_seen
        out.append(
            {
                "id": int(r["id"]),
                "address": addr,
                "device_id": r["device_id"],
                "device_name": r["device_name"],
                "state": r["state"],
                "protocols": sorted(protocols),
                "sampling_override": r["sampling_override"],
                "sampling_seen": sampling_seen,
                "first_seen": iso(r["first_seen"]),
                "last_seen": iso(last_seen),
                **flags,
            }
        )
    return out


# ================================================================ tops
async def top_talkers(
    session: AsyncSession,
    rng: Range,
    ids: list[int],
    *,
    mode: str | None,
    now: datetime,
    side: str,
    limit: int,
    if_index: int | None = None,
    direction: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "ids": ids,
        "s": rng.since,
        "u": rng.until,
        "lim": limit,
        "cut": _cut(now),
        "ifx": if_index,
    }
    if if_index is not None:
        # interface-level talkers come from the conversation table (top entries + other)
        cols = {"src": ["src_ip"], "dst": ["dst_ip"], "both": ["src_ip", "dst_ip"]}[side]
        parts = " UNION ALL ".join(
            f"SELECT {c} AS ip, other, bytes, packets, flows FROM flow_conv_1m "
            f"WHERE exporter_id = ANY(:ids) AND time >= :s AND time < :u"
            + _iface_filter(if_index, direction)
            for c in cols
        )
        rel = f"({parts})"
        flt = ""
        rng = Range(rng.since, rng.until, "minute")
    else:
        rel = relation("host", rng, mode)
        dirs = ["src", "dst"] if side == "both" else [side]
        params["dirs"] = dirs
        flt = (
            " WHERE exporter_id = ANY(:ids) AND time >= :s AND time < :u AND direction = ANY(:dirs)"
        )
    total = (
        await session.execute(text(f"SELECT coalesce(sum(bytes), 0) FROM {rel} h{flt}"), params)
    ).scalar()
    rows = (
        await session.execute(
            text(
                f"SELECT ip, sum(bytes) AS b, sum(packets) AS p, sum(flows) AS f FROM {rel} h"
                + (flt + " AND NOT other" if flt else " WHERE NOT other")
                + " GROUP BY ip ORDER BY b DESC, p DESC, ip LIMIT :lim"
            ),
            params,
        )
    ).all()
    ips = [_ip(r.ip) for r in rows]
    names, devices = await _names(session, ips)
    items = []
    for r, ip in zip(rows, ips, strict=True):
        host = names.get(ip)
        items.append(
            {
                "key": ip,
                "label": f"{ip} ({host})" if host else ip,
                "ip": ip,
                "hostname": host,
                "device_id": devices.get(ip),
                "bytes": int(r.b),
                "packets": int(r.p),
                "flows": int(r.f),
            }
        )
    return _top_out(rng, int(total or 0), items)


async def top_applications(
    session: AsyncSession,
    rng: Range,
    ids: list[int],
    *,
    mode: str | None,
    now: datetime,
    limit: int,
    services: ServiceMap,
    if_index: int | None = None,
    direction: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "ids": ids,
        "s": rng.since,
        "u": rng.until,
        "lim": limit,
        "cut": _cut(now),
        "ifx": if_index,
    }
    if if_index is not None:
        rel = (
            "(SELECT protocol, CASE WHEN other THEN -1 ELSE src_port + dst_port END AS port, "
            "bytes, packets, flows FROM flow_conv_1m WHERE exporter_id = ANY(:ids) AND "
            "time >= :s AND time < :u" + _iface_filter(if_index, direction) + ")"
        )
        flt = ""
        rng = Range(rng.since, rng.until, "minute")
    else:
        rel = relation("app", rng, mode)
        flt = " WHERE exporter_id = ANY(:ids) AND time >= :s AND time < :u"
    total = (
        await session.execute(text(f"SELECT coalesce(sum(bytes), 0) FROM {rel} a{flt}"), params)
    ).scalar()
    rows = (
        await session.execute(
            text(
                f"SELECT protocol, port, sum(bytes) AS b, sum(packets) AS p, sum(flows) AS f "
                f"FROM {rel} a"
                + (flt + " AND port >= 0" if flt else " WHERE port >= 0")
                + " GROUP BY protocol, port ORDER BY b DESC, p DESC, protocol, port LIMIT :lim"
            ),
            params,
        )
    ).all()
    items = [
        {
            "key": services.key(int(r.protocol), int(r.port)),
            "label": services.label(int(r.protocol), int(r.port)),
            "protocol": int(r.protocol),
            "port": int(r.port),
            "app": services.app_name(int(r.protocol), int(r.port)),
            "bytes": int(r.b),
            "packets": int(r.p),
            "flows": int(r.f),
        }
        for r in rows
    ]
    return _top_out(rng, int(total or 0), items)


async def top_conversations(
    session: AsyncSession,
    rng: Range,
    ids: list[int],
    *,
    limit: int,
    services: ServiceMap,
    if_index: int | None = None,
    direction: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "ids": ids,
        "s": rng.since,
        "u": rng.until,
        "lim": limit,
        "ifx": if_index,
    }
    flt = " WHERE exporter_id = ANY(:ids) AND time >= :s AND time < :u" + _iface_filter(
        if_index, direction
    )
    total = (
        await session.execute(
            text(f"SELECT coalesce(sum(bytes), 0) FROM flow_conv_1m{flt}"), params
        )
    ).scalar()
    rows = (
        await session.execute(
            text(
                "SELECT in_if, out_if, src_ip, dst_ip, protocol, src_port, dst_port, "
                "sum(bytes) AS b, sum(packets) AS p, sum(flows) AS f FROM flow_conv_1m"
                + flt
                + " AND NOT other GROUP BY in_if, out_if, src_ip, dst_ip, protocol, src_port, "
                "dst_port ORDER BY b DESC, p DESC, src_ip, dst_ip LIMIT :lim"
            ),
            params,
        )
    ).all()
    ips = sorted({_ip(r.src_ip) for r in rows} | {_ip(r.dst_ip) for r in rows})
    names, _ = await _names(session, ips)
    items = []
    for r in rows:
        src, dst = _ip(r.src_ip), _ip(r.dst_ip)
        port = int(r.src_port) or int(r.dst_port)
        svc = (
            services.label(int(r.protocol), port)
            if port or r.protocol not in (6, 17, 132)
            else proto_name(int(r.protocol))
        )
        s_label = f"{src} ({names[src]})" if src in names else src
        d_label = f"{dst} ({names[dst]})" if dst in names else dst
        items.append(
            {
                "key": f"{r.in_if}|{r.out_if}|{src}|{dst}|{r.protocol}|{r.src_port}|{r.dst_port}",
                "label": f"{s_label} → {d_label} {svc}",
                "src_ip": src,
                "dst_ip": dst,
                "src_port": int(r.src_port),
                "dst_port": int(r.dst_port),
                "protocol": int(r.protocol),
                "in_if": int(r.in_if),
                "out_if": int(r.out_if),
                "bytes": int(r.b),
                "packets": int(r.p),
                "flows": int(r.f),
            }
        )
    return _top_out(rng, int(total or 0), items)


# ================================================================ interfaces
async def interface_names(
    session: AsyncSession, exporter_ids_: list[int]
) -> dict[tuple[int, int], tuple[str, int | None]]:
    """(exporter_id, if_index) -> (name, speed_bps): `switch_ports.name`, else the
    `snmp_interface` sensor's if_name; speed from `switch_ports.speed_bps`."""
    out: dict[tuple[int, int], tuple[str, int | None]] = {}
    if not exporter_ids_:
        return out
    rows = await session.execute(
        text(
            "SELECT e.id AS eid, p.if_index, p.name, p.speed_bps FROM flow_exporters e "
            "JOIN switch_ports p ON p.device_id = e.device_id WHERE e.id = ANY(:ids)"
        ),
        {"ids": exporter_ids_},
    )
    for r in rows:
        out[(int(r.eid), int(r.if_index))] = (str(r.name or ""), r.speed_bps)
    rows = await session.execute(
        text(
            "SELECT e.id AS eid, s.config FROM flow_exporters e JOIN sensors s "
            "ON s.device_id = e.device_id AND s.type = 'snmp_interface' WHERE e.id = ANY(:ids)"
        ),
        {"ids": exporter_ids_},
    )
    for r in rows:
        cfg = r.config or {}
        try:
            idx = int(cfg.get("if_index"))
        except (TypeError, ValueError):
            continue
        key = (int(r.eid), idx)
        name, speed = out.get(key, ("", None))
        if not name and cfg.get("if_name"):
            out[key] = (str(cfg["if_name"]), speed)
    return out


def if_label(names: dict[tuple[int, int], tuple[str, int | None]], eid: int, idx: int) -> str:
    if idx == 0:
        return "local / none"
    name = names.get((eid, idx), ("", None))[0]
    return name or f"ifIndex {idx}"


async def interfaces(
    session: AsyncSession, rng: Range, ids: list[int], *, mode: str | None, now: datetime
) -> list[dict[str, Any]]:
    rel = relation("if", rng, mode)
    rows = (
        await session.execute(
            text(
                f"SELECT exporter_id, if_index, direction, sum(bytes) AS b FROM {rel} i "
                "WHERE exporter_id = ANY(:ids) AND time >= :s AND time < :u "
                "GROUP BY exporter_id, if_index, direction"
            ),
            {"ids": ids, "s": rng.since, "u": rng.until, "cut": _cut(now)},
        )
    ).all()
    acc: dict[tuple[int, int], dict[str, int]] = {}
    for r in rows:
        v = acc.setdefault((int(r.exporter_id), int(r.if_index)), {"in": 0, "out": 0})
        v[str(r.direction)] += int(r.b)
    names = await interface_names(session, sorted({k[0] for k in acc}))
    items = [
        {
            "exporter_id": eid,
            "if_index": idx,
            "name": if_label(names, eid, idx),
            "in_bytes": v["in"],
            "out_bytes": v["out"],
            "speed_bps": names.get((eid, idx), ("", None))[1],
        }
        for (eid, idx), v in acc.items()
    ]
    items.sort(key=lambda i: (-(i["in_bytes"] + i["out_bytes"]), i["exporter_id"], i["if_index"]))
    return items


# ================================================================ series
def _buckets(rng: Range, bs: int) -> list[datetime]:
    epoch = datetime(2000, 1, 1, tzinfo=UTC)
    start = epoch + timedelta(seconds=((rng.since - epoch).total_seconds() // bs) * bs)
    out = []
    t = start
    while t < rng.until:
        out.append(t)
        t += timedelta(seconds=bs)
    return out


def _points(buckets: list[datetime], values: dict[datetime, int], bs: int) -> list[list[Any]]:
    return [[iso(b), round(values.get(b, 0) * 8 / bs, 3)] for b in buckets]


async def series(
    session: AsyncSession,
    rng: Range,
    ids: list[int],
    *,
    mode: str | None,
    now: datetime,
    split: str,
    services: ServiceMap,
    exporter_id: int | None,
    if_index: int | None,
    direction: str | None,
) -> dict[str, Any]:
    bs = bucket_seconds(rng)
    buckets = _buckets(rng, bs)
    params: dict[str, Any] = {
        "ids": ids,
        "s": rng.since,
        "u": rng.until,
        "cut": _cut(now),
        "bs": bs,
        "ifx": if_index,
    }
    bucket_sql = "date_bin(make_interval(secs => :bs), time, TIMESTAMPTZ '2000-01-01')"
    out_series: list[dict[str, Any]] = []
    iface = None
    if if_index is not None and exporter_id is not None:
        names = await interface_names(session, [exporter_id])
        iface = {
            "exporter_id": exporter_id,
            "if_index": if_index,
            "name": if_label(names, exporter_id, if_index),
            "speed_bps": names.get((exporter_id, if_index), ("", None))[1],
        }
    source = rng.source
    if split == "none":
        if if_index is not None:
            rel = relation("if", rng, mode)
            dirs = [direction] if direction else ["in", "out"]
            params["dirs"] = dirs
            rows = await session.execute(
                text(
                    f"SELECT {bucket_sql} AS b, direction, sum(bytes) AS v FROM {rel} i "
                    "WHERE exporter_id = ANY(:ids) AND time >= :s AND time < :u AND "
                    "if_index = :ifx AND direction = ANY(:dirs) GROUP BY 1, 2"
                ),
                params,
            )
            vals: dict[str, dict[datetime, int]] = {d: {} for d in dirs}
            for r in rows:
                vals[str(r.direction)][r.b] = int(r.v)
            for d in dirs:
                out_series.append(
                    {"key": d, "label": d, "direction": d, "points": _points(buckets, vals[d], bs)}
                )
        else:
            rel = relation("app", rng, mode)
            rows = await session.execute(
                text(
                    f"SELECT {bucket_sql} AS b, sum(bytes) AS v FROM {rel} a "
                    "WHERE exporter_id = ANY(:ids) AND time >= :s AND time < :u GROUP BY 1"
                ),
                params,
            )
            v1 = {r.b: int(r.v) for r in rows}
            out_series.append(
                {
                    "key": "total",
                    "label": "total",
                    "direction": direction or "in",
                    "points": _points(buckets, v1, bs),
                }
            )
    else:
        d = direction or "in"
        if if_index is not None:
            source = "minute"
            key_sql = (
                "protocol || '/' || CASE WHEN other THEN -1 ELSE src_port + dst_port END"
                if split == "app"
                else "CASE WHEN other THEN NULL ELSE host(src_ip) END"
            )
            rel = (
                f"(SELECT time, exporter_id, {key_sql} AS k, bytes FROM flow_conv_1m "
                "WHERE exporter_id = ANY(:ids) AND time >= :s AND time < :u"
                + _iface_filter(if_index, d)
                + ")"
            )
        elif split == "app":
            rel = (
                "(SELECT time, exporter_id, protocol || '/' || port AS k, bytes FROM "
                f"{relation('app', rng, mode)} a0)"
            )
        else:
            rel = (
                "(SELECT time, exporter_id, CASE WHEN other THEN NULL ELSE host(ip) END AS k, "
                f"bytes FROM {relation('host', rng, mode)} h0 WHERE direction = 'src')"
            )
        flt = "exporter_id = ANY(:ids) AND time >= :s AND time < :u"
        top = [
            r.k
            for r in await session.execute(
                text(
                    f"SELECT k, sum(bytes) AS v FROM {rel} x WHERE {flt} AND k IS NOT NULL "
                    "AND k NOT LIKE '%/-1' GROUP BY k ORDER BY v DESC, k LIMIT :n"
                ),
                {**params, "n": SERIES_SPLIT_TOP},
            )
        ]
        params["top"] = top
        rows = await session.execute(
            text(
                f"SELECT {bucket_sql} AS b, CASE WHEN k = ANY(:top) THEN k ELSE NULL END AS kk, "
                f"sum(bytes) AS v FROM {rel} x WHERE {flt} GROUP BY 1, 2"
            ),
            params,
        )
        vals2: dict[str | None, dict[datetime, int]] = {}
        for r in rows:
            vals2.setdefault(r.kk, {})[r.b] = int(r.v)
        for k in [*top, None]:
            if k is None and None not in vals2:
                continue
            if split == "app" and k is not None:
                proto, port = (int(x) for x in k.split("/"))
                key, label = services.key(proto, port), services.label(proto, port)
            else:
                key = label = k if k is not None else "other"
            out_series.append(
                {
                    "key": key,
                    "label": label,
                    "direction": d,
                    "points": _points(buckets, vals2.get(k, {}), bs),
                }
            )
    return {
        "since": iso(rng.since),
        "until": iso(rng.until),
        "bucket_s": bs,
        "source": source,
        "interface": iface,
        "series": out_series,
    }
