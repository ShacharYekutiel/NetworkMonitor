"""[syslog-protection] Syslog filter rules (admin), the cleanup of stored rows they match
(offered, never automatic) and the "top talkers" of the last 24 h (api.md
"[syslog-protection]")."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import text

from netmon.api.deps import DB, RT, Admin, Audit, Viewer, require_admin
from netmon.api.errors import ApiError, not_found, validation_error
from netmon.ingest import query, syslog_cleanup
from netmon.schemas.ingest import SyslogCleanupIn, SyslogFilterIn, SyslogFilterOrderIn
from netmon.services import audit
from netmon.services.features import feature_disabled
from netmon.services.serialize import iso

router = APIRouter()

# role before feature state: a viewer gets 403, never 409 feature_disabled
ADMIN_SYSLOG = [Depends(require_admin), Depends(feature_disabled("syslog"))]
SYSLOG = [Depends(feature_disabled("syslog"))]
MAX_FILTERS = 200
FIELDS = (
    "name",
    "enabled",
    "sources",
    "device_ids",
    "program",
    "program_match",
    "msgid",
    "severity_min",
    "message_contains",
    "action",
    "keep_n",
)
MATCH_FIELDS = ("sources", "device_ids", "program", "program_match", "msgid", "severity_min",
                "message_contains")  # fmt: skip
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "sources": [],
    "device_ids": [],
    "program": "",
    "program_match": "exact",
    "msgid": "",
    "severity_min": None,
    "message_contains": "",
    "action": "drop",
    "keep_n": 100,
}


def _has_criterion(v: dict[str, Any]) -> bool:
    return bool(
        v["sources"]
        or v["device_ids"]
        or v["program"]
        or v["msgid"]
        or v["message_contains"]
        or v["severity_min"] is not None
    )


async def _rows(session: DB, rule_id: int | None = None) -> list[dict[str, Any]]:
    where = "WHERE f.id = :id" if rule_id is not None else ""
    return [
        dict(r)
        for r in (
            await session.execute(
                text(
                    "SELECT f.*, coalesce((SELECT json_agg(json_build_object('id', d.id, "
                    "'name', d.name) ORDER BY d.name) FROM devices d "
                    "WHERE d.id = ANY(f.device_ids)), '[]') AS devices "
                    f"FROM syslog_filters f {where} ORDER BY f.position, f.id"
                ),
                {"id": rule_id} if rule_id is not None else {},
            )
        ).mappings()
    ]


def filter_out(r: dict[str, Any], rt: Any) -> dict[str, Any]:
    pending = rt.ingest.protect.pending_hits.get(int(r["id"]), (0, None))[0]
    return {
        "id": int(r["id"]),
        "name": r["name"],
        "enabled": bool(r["enabled"]),
        "position": int(r["position"]),
        "sources": [str(s) for s in r["sources"] or []],
        "device_ids": [int(d) for d in r["device_ids"] or []],
        "devices": list(r.get("devices") or []),
        "program": r["program"],
        "program_match": r["program_match"],
        "msgid": r["msgid"],
        "severity_min": r["severity_min"],
        "message_contains": r["message_contains"],
        "action": r["action"],
        "keep_n": int(r["keep_n"]),
        "hits": int(r["hits"]) + int(pending),
        "last_hit_at": iso(r["last_hit_at"]),
        "hits_since": iso(r["hits_since"]),
        "created_at": iso(r["created_at"]),
        "updated_at": iso(r["updated_at"]),
    }


async def _get(session: DB, rule_id: int) -> dict[str, Any]:
    rows = await _rows(session, rule_id)
    if not rows:
        raise not_found("Syslog filter rule")
    return rows[0]


def _values(r: dict[str, Any]) -> dict[str, Any]:
    out = {k: r[k] for k in FIELDS}
    out["sources"] = [str(s) for s in out["sources"] or []]
    out["device_ids"] = [int(d) for d in out["device_ids"] or []]
    return out


async def _check(session: DB, v: dict[str, Any]) -> None:
    if not _has_criterion(v):
        raise validation_error(
            ["body"], "a rule needs at least one condition (source, program, msgid, severity "
            "or message text)"
        )  # fmt: skip
    if v["device_ids"]:
        found = {
            int(r[0])
            for r in await session.execute(
                text("SELECT id FROM devices WHERE id = ANY(:ids)"), {"ids": v["device_ids"]}
            )
        }
        missing = [d for d in v["device_ids"] if d not in found]
        if missing:
            raise validation_error(["body", "device_ids"], f"devices do not exist: {missing}")


async def _changed(rt: RT) -> None:
    await rt.ingest.reload_syslog_filters()
    rt.config_changed("syslog_filter", 0, "updated")


_SET = (
    "name = :name, enabled = :enabled, sources = CAST(:sources AS text[]), "
    "device_ids = CAST(:device_ids AS integer[]), program = :program, "
    "program_match = :program_match, msgid = :msgid, severity_min = :severity_min, "
    "message_contains = :message_contains, action = :action, keep_n = :keep_n"
)


@router.get("/api/syslog-filters", dependencies=ADMIN_SYSLOG)
async def list_filters(_: Admin, rt: RT, session: DB) -> dict:
    return {
        "items": [filter_out(r, rt) for r in await _rows(session)],
        "cleanup": rt.ingest.cleanup,
    }


@router.post("/api/syslog-filters", status_code=201, dependencies=ADMIN_SYSLOG)
async def create_filter(body: SyslogFilterIn, _: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    data = body.model_dump(exclude_unset=True)
    if not data.get("name"):
        raise validation_error(["body", "name"], "required")
    v = {**DEFAULTS, **{k: x for k, x in data.items() if x is not None}}
    if "severity_min" in data:
        v["severity_min"] = data["severity_min"]
    await _check(session, v)
    n, pos = (
        await session.execute(
            text("SELECT count(*), coalesce(max(position), 0) FROM syslog_filters")
        )
    ).one()
    if n >= MAX_FILTERS:
        raise validation_error(["body"], f"at most {MAX_FILTERS} syslog filter rules")
    rid = (
        await session.execute(
            text(
                "INSERT INTO syslog_filters (name, enabled, position, sources, device_ids, "
                "program, program_match, msgid, severity_min, message_contains, action, keep_n) "
                "VALUES (:name, :enabled, :position, CAST(:sources AS text[]), "
                "CAST(:device_ids AS integer[]), :program, :program_match, :msgid, "
                ":severity_min, :message_contains, :action, :keep_n) RETURNING id"
            ),
            {**v, "position": int(pos) + 1},
        )
    ).scalar_one()
    await audit.record(
        session,
        ctx,
        "syslog_filter.create",
        audit.entity("syslog_filter", rid, v["name"]),
        audit.created(_values(v)),
    )
    await session.commit()
    await _changed(rt)
    return filter_out(await _get(session, rid), rt)


@router.patch("/api/syslog-filters/{rule_id}", dependencies=ADMIN_SYSLOG)
async def patch_filter(
    rule_id: int, body: SyslogFilterIn, _: Admin, rt: RT, session: DB, ctx: Audit
) -> dict:
    old = await _get(session, rule_id)
    data = body.model_dump(exclude_unset=True)
    if "name" in data and not data["name"]:
        raise validation_error(["body", "name"], "required")
    v = _values(old)
    for k, x in data.items():
        if x is not None or k == "severity_min":
            v[k] = x
    await _check(session, v)
    match_changed = any(v[k] != _values(old)[k] for k in (*MATCH_FIELDS, "action"))
    reset = ", hits = 0, last_hit_at = NULL, hits_since = now()" if match_changed else ""
    await session.execute(
        text(f"UPDATE syslog_filters SET {_SET}, updated_at = now(){reset} WHERE id = :id"),
        {**v, "id": rule_id},
    )
    if match_changed:
        # rows kept under the old definition no longer count towards the new window
        await session.execute(
            text("UPDATE syslog_messages SET filter_rule_id = NULL WHERE filter_rule_id = :r"),
            {"r": rule_id},
        )
        rt.ingest.protect.pending_hits.pop(rule_id, None)
    await audit.record(
        session,
        ctx,
        "syslog_filter.update",
        audit.entity("syslog_filter", rule_id, v["name"]),
        audit.diff(_values(old), v),
    )
    await session.commit()
    await _changed(rt)
    return filter_out(await _get(session, rule_id), rt)


@router.delete("/api/syslog-filters/{rule_id}", status_code=204, dependencies=ADMIN_SYSLOG)
async def delete_filter(rule_id: int, _: Admin, rt: RT, session: DB, ctx: Audit) -> Response:
    old = await _get(session, rule_id)
    await session.execute(text("DELETE FROM syslog_filters WHERE id = :id"), {"id": rule_id})
    await audit.record(
        session,
        ctx,
        "syslog_filter.delete",
        audit.entity("syslog_filter", rule_id, old["name"]),
        audit.deleted(_values(old)),
    )
    await session.commit()
    rt.ingest.protect.pending_hits.pop(rule_id, None)
    await _changed(rt)
    return Response(status_code=204)


@router.put("/api/syslog-filters/order", dependencies=ADMIN_SYSLOG)
async def order_filters(
    body: SyslogFilterOrderIn, _: Admin, rt: RT, session: DB, ctx: Audit
) -> dict:
    rows = await _rows(session)
    current = [int(r["id"]) for r in rows]
    if sorted(body.ids) != sorted(current) or len(set(body.ids)) != len(body.ids):
        raise validation_error(["body", "ids"], "must list every syslog filter rule once")
    for pos, rid in enumerate(body.ids, start=1):
        await session.execute(
            text("UPDATE syslog_filters SET position = :p WHERE id = :id"), {"p": pos, "id": rid}
        )
    await audit.record(
        session,
        ctx,
        "syslog_filter.reorder",
        audit.entity("syslog_filter", None, None),
        {"order": [current, list(body.ids)]},
    )
    await session.commit()
    await _changed(rt)
    return {"items": [filter_out(r, rt) for r in await _rows(session)]}


# ---------------------------------------------------------------- cleanup of stored rows
@router.post("/api/syslog-filters/{rule_id}/cleanup-preview", dependencies=ADMIN_SYSLOG)
async def cleanup_preview(rule_id: int, _: Admin, rt: RT, session: DB) -> dict:
    r = await _get(session, rule_id)
    until = rt.clock.now()
    n = await syslog_cleanup.preview(session, r, until)
    return {
        "rule_id": rule_id,
        "action": r["action"],
        "keep_n": int(r["keep_n"]),
        "count": n,
        "until": until.isoformat(),
        "running": bool(rt.ingest.cleanup and rt.ingest.cleanup["state"] == "running"),
    }


@router.post("/api/syslog-filters/{rule_id}/cleanup", status_code=202, dependencies=ADMIN_SYSLOG)
async def cleanup_run(
    rule_id: int, body: SyslogCleanupIn, _: Admin, rt: RT, session: DB, ctx: Audit
) -> dict:
    r = await _get(session, rule_id)
    until = query.parse_time(body.until, "until")
    assert until is not None
    if until > rt.clock.now() + timedelta(seconds=5):
        raise validation_error(["body", "until"], "must not be in the future")
    job = rt.ingest.cleanup
    if job is not None and job["state"] == "running":
        raise ApiError(409, "cleanup_running", "A syslog cleanup is already running")
    job = {
        "rule_id": rule_id,
        "rule_name": r["name"],
        "action": r["action"],
        "keep_n": int(r["keep_n"]),
        "state": "running",
        "expected": body.expected,
        "deleted": 0,
        "until": iso(until),
        "started_at": iso(rt.clock.now()),
        "finished_at": None,
        "error": None,
    }
    await audit.record(
        session,
        ctx,
        "syslog_filter.cleanup",
        audit.entity("syslog_filter", rule_id, r["name"]),
        {"rows": [None, body.expected], "until": [None, iso(until)]},
    )
    await session.commit()
    rt.ingest.cleanup = job
    rt.spawn(syslog_cleanup.run(rt, r, until, job))
    return job


@router.get("/api/syslog-filters/cleanup", dependencies=ADMIN_SYSLOG)
async def cleanup_status(_: Admin, rt: RT) -> dict:
    return {"cleanup": rt.ingest.cleanup}


# ---------------------------------------------------------------- top talkers
@router.get("/api/syslog/top-talkers", dependencies=SYSLOG)
async def top_talkers(
    _: Viewer,
    rt: RT,
    session: DB,
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """The busiest (source, program, severity) of the last `hours`: stored rows (what the
    database holds) and messages (rows + folded repeats)."""
    since = rt.clock.now() - timedelta(hours=hours)
    total = (
        await session.execute(
            text(
                "SELECT count(*), coalesce(sum(repeat_count), 0) FROM syslog_messages "
                "WHERE time >= :since"
            ),  # fmt: skip
            {"since": since},
        )
    ).one()
    rows = (
        await session.execute(
            text(
                "SELECT x.source_ip, x.device_id, d.name AS device_name, x.program, x.severity, "
                "count(*) AS nrows, sum(x.repeat_count) AS messages, max(x.time) AS last "
                "FROM syslog_messages x LEFT JOIN devices d ON d.id = x.device_id "
                "WHERE x.time >= :since GROUP BY x.source_ip, x.device_id, d.name, x.program, "
                "x.severity ORDER BY nrows DESC, x.source_ip, x.program LIMIT :lim"
            ),
            {"since": since, "lim": limit},
        )
    ).all()
    return {
        "since": iso(since),
        "hours": hours,
        "total_rows": int(total[0]),
        "total_messages": int(total[1]),
        "items": [
            {
                "source_ip": str(r.source_ip).split("/", 1)[0],
                "device_id": r.device_id,
                "device_name": r.device_name,
                "program": r.program,
                "severity": int(r.severity),
                "rows": int(r.nrows),
                "messages": int(r.messages),
                "last_seen": iso(r.last),
            }
            for r in rows
        ],
    }
