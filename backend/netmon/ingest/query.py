"""[2b-3] Read side of traps / syslog: serializers, keyset cursors, list and stats queries
(api.md "Cursor pages", "Traps", "Syslog")."""

from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.api.errors import ApiError, validation_error
from netmon.services.serialize import iso

MAX_RANGE = timedelta(days=31)
DEFAULT_RANGE = timedelta(hours=24)
BUCKETS = (60, 300, 900, 3600, 21600, 86400)


# ------------------------------------------------------------------ cursors / time range
def encode_cursor(*parts: Any) -> str:
    raw = json.dumps([iso(p) if isinstance(p, datetime) else p for p in parts])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, n: int) -> list[Any]:
    try:
        pad = "=" * (-len(cursor) % 4)
        parts = json.loads(base64.urlsafe_b64decode(cursor + pad).decode())
        if not isinstance(parts, list) or len(parts) != n:
            raise ValueError
        parts[0] = datetime.fromisoformat(str(parts[0]).replace("Z", "+00:00"))
        for i in range(1, n):
            parts[i] = int(parts[i])
        return parts
    except Exception:  # noqa: BLE001
        raise ApiError(400, "invalid_cursor", "Malformed cursor") from None


def parse_time(value: str | None, loc: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace(" ", "+"))
    except ValueError:
        raise validation_error(["query", loc], "must be an ISO-8601 timestamp") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def time_range(
    since: str | None, until: str | None, now: datetime, default: timedelta = DEFAULT_RANGE
) -> tuple[datetime, datetime]:
    u = parse_time(until, "until") or now
    s = parse_time(since, "since") or (u - default)
    if s > u:
        raise validation_error(["query", "since"], "must be before until")
    if u - s > MAX_RANGE:
        raise validation_error(["query", "since"], "the range is longer than 31 days")
    return s, u


def like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ------------------------------------------------------------------ serializers
def _ip(v: Any) -> str | None:
    return None if v is None else str(v).split("/", 1)[0]


def trap_out(r: Any) -> dict[str, Any]:
    varbinds = r["varbinds"]
    if isinstance(varbinds, str):
        varbinds = json.loads(varbinds)
    return {
        "id": int(r["id"]),
        "time": iso(r["time"]),
        "source_ip": _ip(r["source_ip"]),
        "agent_addr": _ip(r["agent_addr"]),
        "device_id": r["device_id"],
        "device_name": r.get("device_name"),
        "version": r["version"],
        "pdu": r["pdu"],
        "security_name": r["security_name"],
        "trap_oid": r["trap_oid"],
        "trap_name": r["trap_name"],
        "uptime_ticks": r["uptime_ticks"],
        "summary": r["summary"],
        "varbinds": varbinds or [],
        "matched_rule_ids": list(r["matched_rule_ids"] or []),
    }


def syslog_out(r: Any) -> dict[str, Any]:
    return {
        "id": int(r["id"]),
        "time": iso(r["time"]),
        "device_time": iso(r["device_time"]),
        "source_ip": _ip(r["source_ip"]),
        "device_id": r["device_id"],
        "device_name": r.get("device_name"),
        "transport": r["transport"],
        "format": r["format"],
        "facility": int(r["facility"]),
        "severity": int(r["severity"]),
        "hostname": r["hostname"],
        "program": r["program"],
        "pid": r["pid"],
        "msgid": r["msgid"],
        "structured_data": r["structured_data"],
        "message": r["message"],
        "truncated": bool(r["truncated"]),
        "matched_rule_ids": list(r["matched_rule_ids"] or []),
    }


# ------------------------------------------------------------------ filters
@dataclass
class Filter:
    kind: str  # trap | syslog
    since: datetime
    until: datetime
    device_ids: list[int] | None = None  # device_id / group_id (resolved)
    unknown: bool | None = None
    source_ip: str | None = None
    trap_oid: str | None = None
    rule_id: int | None = None
    q: str | None = None
    severity_max: int | None = None
    facilities: list[int] = field(default_factory=list)
    programs: list[str] = field(default_factory=list)
    hostname: str | None = None

    def where(self, alias: str = "x") -> tuple[str, dict[str, Any]]:
        a = alias
        conds = [f"{a}.time >= :since", f"{a}.time <= :until"]
        p: dict[str, Any] = {"since": self.since, "until": self.until}
        if self.device_ids is not None:
            conds.append(f"{a}.device_id = ANY(:dids)")
            p["dids"] = self.device_ids
        if self.unknown is True:
            conds.append(f"{a}.device_id IS NULL")
        elif self.unknown is False:
            conds.append(f"{a}.device_id IS NOT NULL")
        if self.source_ip:
            conds.append(f"{a}.source_ip = CAST(:sip AS inet)")
            p["sip"] = self.source_ip
        if self.rule_id is not None:
            conds.append(f":rid = ANY({a}.matched_rule_ids)")
            p["rid"] = self.rule_id
        col = "summary" if self.kind == "trap" else "message"
        if self.q:
            conds.append(f"{a}.{col} ILIKE :q ESCAPE '\\'")
            p["q"] = "%" + like_escape(self.q) + "%"
        if self.kind == "trap" and self.trap_oid:
            oid = self.trap_oid.strip().lstrip(".")
            conds.append(f"({a}.trap_oid = :oid OR {a}.trap_oid LIKE :oidp ESCAPE '\\')")
            p["oid"] = oid
            p["oidp"] = like_escape(oid) + ".%"
        if self.kind == "syslog":
            if self.severity_max is not None:
                conds.append(f"{a}.severity <= :sevmax")
                p["sevmax"] = self.severity_max
            if self.facilities:
                conds.append(f"{a}.facility = ANY(:facs)")
                p["facs"] = self.facilities
            if self.programs:
                conds.append(f"lower({a}.program) = ANY(:progs)")
                p["progs"] = [x.lower() for x in self.programs]
            if self.hostname:
                conds.append(f"lower({a}.hostname) = :host")
                p["host"] = self.hostname.lower()
        return " AND ".join(conds), p


def table(kind: str) -> str:
    return "traps" if kind == "trap" else "syslog_messages"


async def list_page(
    session: AsyncSession, flt: Filter, limit: int, cursor: str | None
) -> dict[str, Any]:
    where, p = flt.where()
    if cursor:
        ct, cid = decode_cursor(cursor, 2)
        where += " AND (x.time, x.id) < (:ct, :cid)"
        p.update({"ct": ct, "cid": cid})
    rows = (
        (
            await session.execute(
                text(
                    f"SELECT x.*, d.name AS device_name FROM {table(flt.kind)} x "  # noqa: S608
                    f"LEFT JOIN devices d ON d.id = x.device_id WHERE {where} "
                    "ORDER BY x.time DESC, x.id DESC LIMIT :lim"
                ),
                {**p, "lim": limit + 1},
            )
        )
        .mappings()
        .all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    out = trap_out if flt.kind == "trap" else syslog_out
    nxt = encode_cursor(rows[-1]["time"], int(rows[-1]["id"])) if more and rows else None
    return {"items": [out(r) for r in rows], "next_cursor": nxt, "limit": limit}


async def get_one(session: AsyncSession, kind: str, id_: int) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text(
                    f"SELECT x.*, d.name AS device_name FROM {table(kind)} x "  # noqa: S608
                    "LEFT JOIN devices d ON d.id = x.device_id WHERE x.id = :id LIMIT 1"
                ),
                {"id": id_},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return trap_out(row) if kind == "trap" else syslog_out(row)


def pick_bucket(span_s: float) -> int:
    best = BUCKETS[0]
    best_score = math.inf
    for b in BUCKETS:
        n = max(span_s / b, 1e-9)
        score = abs(math.log(n / 60))
        if score < best_score:
            best, best_score = b, score
    return best


async def stats(session: AsyncSession, flt: Filter) -> dict[str, Any]:
    where, p = flt.where()
    tbl = table(flt.kind)
    span = (flt.until - flt.since).total_seconds()
    b = pick_bucket(span)
    p["b"] = b
    bucket_sql = "floor(extract(epoch FROM x.time) / :b) * :b"
    start = math.floor(flt.since.timestamp() / b) * b
    end = math.floor(flt.until.timestamp() / b) * b
    keys = list(range(int(start), int(end) + 1, b))
    if flt.kind == "trap":
        counts = {
            int(r.bk): int(r.cnt)
            for r in await session.execute(
                text(
                    f"SELECT {bucket_sql} AS bk, count(*) AS cnt FROM {tbl} x "  # noqa: S608
                    f"WHERE {where} GROUP BY 1"
                ),
                p,
            )
        }
        buckets = [
            {"t": iso(datetime.fromtimestamp(k, UTC)), "count": counts.get(k, 0)} for k in keys
        ]
        total = sum(counts.values())
    else:
        sev: dict[int, list[int]] = {}
        for r in await session.execute(
            text(
                f"SELECT {bucket_sql} AS bk, x.severity AS sev, count(*) AS cnt "  # noqa: S608
                f"FROM {tbl} x WHERE {where} GROUP BY 1, 2"
            ),
            p,
        ):
            sev.setdefault(int(r.bk), [0] * 8)[int(r.sev)] += int(r.cnt)
        buckets = [
            {"t": iso(datetime.fromtimestamp(k, UTC)), "counts": sev.get(k, [0] * 8)} for k in keys
        ]
        by_severity = [sum(v[i] for v in sev.values()) for i in range(8)]
        total = sum(by_severity)
    top_sources = [
        {
            "device_id": r.device_id,
            "device_name": r.device_name,
            "source_ip": _ip(r.source_ip),
            "count": int(r.n),
        }
        for r in await session.execute(
            text(
                f"SELECT x.device_id, d.name AS device_name, x.source_ip, count(*) AS n "  # noqa: S608
                f"FROM {tbl} x LEFT JOIN devices d ON d.id = x.device_id WHERE {where} "
                "GROUP BY x.device_id, d.name, x.source_ip ORDER BY n DESC, x.source_ip LIMIT 10"
            ),
            p,
        )
    ]
    out: dict[str, Any] = {
        "since": iso(flt.since),
        "until": iso(flt.until),
        "bucket_s": b,
        "buckets": buckets,
        "total": total,
    }
    if flt.kind == "trap":
        out["top_sources"] = top_sources
        out["top_traps"] = [
            {"trap_oid": r.trap_oid, "trap_name": r.trap_name, "count": int(r.n)}
            for r in await session.execute(
                text(
                    f"SELECT x.trap_oid, max(x.trap_name) AS trap_name, count(*) AS n "  # noqa: S608
                    f"FROM {tbl} x WHERE {where} GROUP BY x.trap_oid "
                    "ORDER BY n DESC, x.trap_oid LIMIT 10"
                ),
                p,
            )
        ]
    else:
        out["by_severity"] = by_severity
        out["top_sources"] = top_sources
        out["top_programs"] = [
            {"program": r.program, "count": int(r.n)}
            for r in await session.execute(
                text(
                    f"SELECT x.program, count(*) AS n FROM {tbl} x "  # noqa: S608
                    f"WHERE {where} GROUP BY x.program ORDER BY n DESC, x.program LIMIT 10"
                ),
                p,
            )
        ]
    return out
