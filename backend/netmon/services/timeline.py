"""[2b-3] Device timeline (architecture section 12.9): one query per kind with the same
(since, until, cursor) bound and LIMIT limit+1, k-way merged by (time DESC, kind rank, id DESC).
No new table."""

from __future__ import annotations

import heapq
import re
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.ingest.query import encode_cursor
from netmon.services.serialize import iso

KINDS = ("status", "notification", "ack", "maintenance", "trap", "syslog", "port", "audit")
RANK = {k: i for i, k in enumerate(KINDS)}
MAX_MESSAGE = 500
_ACK_BY = re.compile(r"^Acknowledge(?:d|ment removed) by ([^:]+)")


def _bound(rank: int, cursor: tuple[datetime, int, int] | None, alias: str) -> tuple[str, dict]:
    """Rows strictly after the cursor in (time DESC, rank ASC, id DESC) order."""
    if cursor is None:
        return "", {}
    ct, crank, cid = cursor
    p = {"ct": ct, "cid": cid}
    if rank > crank:
        return f" AND {alias}.time <= :ct", p
    if rank < crank:
        return f" AND {alias}.time < :ct", p
    return f" AND ({alias}.time < :ct OR ({alias}.time = :ct AND {alias}.id < :cid))", p


def _item(kind: str, id_: int, time: datetime, **kw: Any) -> dict[str, Any]:
    out = {
        "kind": kind,
        "id": int(id_),
        "time": iso(time),
        "title": "",
        "status": None,
        "old_status": None,
        "severity": None,
        "sensor_id": None,
        "sensor_name": None,
        "message": "",
        "ref": {"type": "event", "id": int(id_)},
        "actor": None,
        "_t": time,
    }
    out.update(kw)
    out["message"] = str(out["message"] or "")[:MAX_MESSAGE]
    return out


async def _events(
    session: AsyncSession,
    kind: str,
    device_id: int,
    group_chain: list[int],
    since: datetime,
    until: datetime,
    cursor: Any,
    limit: int,
) -> list[dict[str, Any]]:
    bound, p = _bound(RANK[kind], cursor, "e")
    params: dict[str, Any] = {"d": device_id, "since": since, "until": until, "n": limit, **p}
    if kind == "status":
        where = "e.kind = 'status_change' AND e.device_id = :d"
    elif kind == "notification":
        where = "e.kind IN ('notification_sent', 'notification') AND e.device_id = :d"
    elif kind == "ack":
        where = "e.kind = 'acknowledge' AND e.device_id = :d"
    elif kind == "port":
        where = "e.kind = 'port_change' AND e.device_id = :d"
    else:  # maintenance: device scope, group scope (D's group or ancestors), or global
        where = (
            "e.kind = 'maintenance' AND (e.device_id = :d OR (e.device_id IS NULL AND "
            "(e.group_id IS NULL OR e.group_id = ANY(:groups))))"
        )
        params["groups"] = group_chain
    rows = (
        await session.execute(
            text(
                "SELECT e.id, e.time, e.kind, e.sensor_id, e.sensor_name, e.old_status, "
                f"e.new_status, e.message FROM events e WHERE {where} "  # noqa: S608
                f"AND e.time >= :since AND e.time <= :until{bound} "
                "ORDER BY e.time DESC, e.id DESC LIMIT :n"
            ),
            params,
        )
    ).all()
    out = []
    for r in rows:
        title = r.message
        actor = None
        status = old = None
        if kind == "status":
            title = f"{r.sensor_name or 'Sensor'}: {r.old_status} → {r.new_status}"
            status, old = r.new_status, r.old_status
        elif kind in ("notification", "port"):
            status, old = r.new_status, r.old_status
        elif kind == "ack":
            m = _ACK_BY.match(r.message or "")
            actor = m.group(1).strip() if m else None
            title = "Acknowledged" if "removed" not in (r.message or "") else "Unacknowledged"
            status = r.new_status
        out.append(
            _item(
                kind,
                r.id,
                r.time,
                title=str(title or "")[:200],
                status=status,
                old_status=old,
                sensor_id=r.sensor_id,
                sensor_name=r.sensor_name,
                message=r.message,
                actor=actor,
            )
        )
    return out


async def _traps(
    session: AsyncSession, device_id: int, since: datetime, until: datetime, cursor: Any, n: int
) -> list[dict[str, Any]]:
    bound, p = _bound(RANK["trap"], cursor, "x")
    rows = (
        await session.execute(
            text(
                "SELECT x.id, x.time, x.trap_oid, x.trap_name, x.summary FROM traps x "
                "WHERE x.device_id = :d AND x.time >= :since AND x.time <= :until"
                f"{bound} ORDER BY x.time DESC, x.id DESC LIMIT :n"
            ),
            {"d": device_id, "since": since, "until": until, "n": n, **p},
        )
    ).all()
    return [
        _item(
            "trap",
            r.id,
            r.time,
            title=r.trap_name or r.trap_oid,
            message=r.summary,
            ref={"type": "trap", "id": int(r.id)},
        )
        for r in rows
    ]


async def _syslog(
    session: AsyncSession, device_id: int, since: datetime, until: datetime, cursor: Any, n: int
) -> list[dict[str, Any]]:
    bound, p = _bound(RANK["syslog"], cursor, "x")
    rows = (
        await session.execute(
            text(
                "SELECT x.id, x.time, x.severity, x.program, x.message FROM syslog_messages x "
                "WHERE x.device_id = :d AND x.time >= :since AND x.time <= :until"
                f"{bound} ORDER BY x.time DESC, x.id DESC LIMIT :n"
            ),
            {"d": device_id, "since": since, "until": until, "n": n, **p},
        )
    ).all()
    return [
        _item(
            "syslog",
            r.id,
            r.time,
            title=r.program or "syslog",
            severity=int(r.severity),
            message=r.message,
            ref={"type": "syslog", "id": int(r.id)},
        )
        for r in rows
    ]


async def _audit(
    session: AsyncSession, device_id: int, since: datetime, until: datetime, cursor: Any, n: int
) -> list[dict[str, Any]]:
    bound, p = _bound(RANK["audit"], cursor, "a")
    rows = (
        await session.execute(
            text(
                "SELECT a.id, a.time, a.action, a.actor_name, a.entity_type, a.entity_id, "
                "a.entity_name FROM audit_log a WHERE a.device_id = :d AND a.time >= :since "
                f"AND a.time <= :until{bound} ORDER BY a.time DESC, a.id DESC LIMIT :n"
            ),
            {"d": device_id, "since": since, "until": until, "n": n, **p},
        )
    ).all()
    out = []
    for r in rows:
        is_sensor = r.entity_type == "sensor"
        out.append(
            _item(
                "audit",
                r.id,
                r.time,
                title=f"{r.action} {r.entity_name or ''}".strip(),
                message=r.action,
                sensor_id=int(r.entity_id) if is_sensor and r.entity_id is not None else None,
                sensor_name=r.entity_name if is_sensor else None,
                ref={"type": "audit", "id": int(r.id)},
                actor=r.actor_name or None,
            )
        )
    return out


async def timeline(
    session: AsyncSession,
    device_id: int,
    group_chain: list[int],
    kinds: list[str],
    since: datetime,
    until: datetime,
    cursor: tuple[datetime, int, int] | None,
    limit: int,
) -> dict[str, Any]:
    per_kind: list[list[dict[str, Any]]] = []
    n = limit + 1
    for kind in KINDS:
        if kind not in kinds:
            continue
        if kind == "trap":
            per_kind.append(await _traps(session, device_id, since, until, cursor, n))
        elif kind == "syslog":
            per_kind.append(await _syslog(session, device_id, since, until, cursor, n))
        elif kind == "audit":
            per_kind.append(await _audit(session, device_id, since, until, cursor, n))
        else:
            per_kind.append(
                await _events(session, kind, device_id, group_chain, since, until, cursor, n)
            )

    def key(it: dict[str, Any]) -> tuple[float, int, int]:
        return (-it["_t"].timestamp(), RANK[it["kind"]], -it["id"])

    merged = list(heapq.merge(*per_kind, key=key))
    more = len(merged) > limit
    page = merged[:limit]
    nxt = None
    if more and page:
        last = page[-1]
        nxt = encode_cursor(last["_t"], RANK[last["kind"]], last["id"])
    return {
        "items": [{k: v for k, v in it.items() if k != "_t"} for it in page],
        "next_cursor": nxt,
        "limit": limit,
    }
