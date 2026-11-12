"""[2b-3] Traps, syslog, ingest status, unknown sources, SIEM forwarding test, event rules and
event-sensor conditions (api.md "Phase 2b-3")."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import select, text

from netmon.api.deps import DB, RT, Admin, Audit, Viewer, require_admin
from netmon.api.errors import ApiError, not_found, validation_error
from netmon.ingest import query, rules
from netmon.ingest.builtin import BUILTIN_RULES, builtin_values
from netmon.ingest.event_sensors import conditions_out, is_event_type
from netmon.models import Device, Sensor
from netmon.schemas.ingest import AssignSourceIn, EventRuleIn, EventRuleTestIn, ForwardTestIn
from netmon.services import audit
from netmon.services import settings as settings_svc
from netmon.services.features import feature_disabled
from netmon.services.serialize import iso, sensor_out
from netmon.services.status import load_inventory

log = logging.getLogger("netmon.api.ingest")
router = APIRouter()

TRAPS = [Depends(feature_disabled("traps"))]
SYSLOG = [Depends(feature_disabled("syslog"))]
INGEST = [Depends(feature_disabled("traps", "syslog"))]
MAX_RULES = 500
REATTRIBUTE_BATCH = 10000
MAX_SOURCE_ADDRESSES = 16


# ================================================================ traps / syslog
async def _device_ids(session: DB, device_id: int | None, group_id: int | None) -> list[int] | None:
    ids: set[int] | None = None
    if device_id is not None:
        ids = {device_id}
    if group_id is not None:
        inv = await load_inventory(session)
        groups = set(inv.descendants(group_id)) if group_id in inv.groups else set()
        in_group = {d for d, row in inv.devices.items() if row["group_id"] in groups}
        ids = in_group if ids is None else ids & in_group
    return sorted(ids) if ids is not None else None


def _ip_param(value: str | None, loc: str) -> str | None:
    if not value:
        return None
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        raise validation_error(["query", loc], "must be an IP address") from None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return str(addr)


async def _filter(
    kind: str,
    rt: RT,
    session: DB,
    *,
    device_id: int | None,
    group_id: int | None,
    unknown: bool | None,
    source_ip: str | None,
    rule_id: int | None,
    q: str | None,
    since: str | None,
    until: str | None,
    **extra: Any,
) -> query.Filter:
    s, u = query.time_range(since, until, rt.clock.now())
    return query.Filter(
        kind=kind,
        since=s,
        until=u,
        device_ids=await _device_ids(session, device_id, group_id),
        unknown=unknown,
        source_ip=_ip_param(source_ip, "source_ip"),
        rule_id=rule_id,
        q=q or None,
        **extra,
    )


Q = Query(default=None, min_length=1, max_length=200)


@router.get("/api/traps/stats", dependencies=TRAPS)
async def trap_stats(
    _: Viewer,
    rt: RT,
    session: DB,
    device_id: int | None = None,
    group_id: int | None = None,
    unknown: bool | None = None,
    source_ip: str | None = None,
    trap_oid: str | None = Query(default=None, max_length=255),
    rule_id: int | None = None,
    q: str | None = Q,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    flt = await _filter(
        "trap", rt, session, device_id=device_id, group_id=group_id, unknown=unknown,
        source_ip=source_ip, rule_id=rule_id, q=q, since=since, until=until, trap_oid=trap_oid,
    )  # fmt: skip
    return await query.stats(session, flt)


@router.get("/api/traps", dependencies=TRAPS)
async def list_traps(
    _: Viewer,
    rt: RT,
    session: DB,
    device_id: int | None = None,
    group_id: int | None = None,
    unknown: bool | None = None,
    source_ip: str | None = None,
    trap_oid: str | None = Query(default=None, max_length=255),
    rule_id: int | None = None,
    q: str | None = Q,
    since: str | None = None,
    until: str | None = None,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    flt = await _filter(
        "trap", rt, session, device_id=device_id, group_id=group_id, unknown=unknown,
        source_ip=source_ip, rule_id=rule_id, q=q, since=since, until=until, trap_oid=trap_oid,
    )  # fmt: skip
    return await query.list_page(session, flt, limit, cursor)


@router.get("/api/traps/{trap_id}", dependencies=TRAPS)
async def get_trap(trap_id: int, _: Viewer, session: DB) -> dict:
    out = await query.get_one(session, "trap", trap_id)
    if out is None:
        raise not_found("Trap")
    return out


@router.get("/api/syslog/stats", dependencies=SYSLOG)
async def syslog_stats(
    _: Viewer,
    rt: RT,
    session: DB,
    device_id: int | None = None,
    group_id: int | None = None,
    unknown: bool | None = None,
    source_ip: str | None = None,
    severity_max: int | None = Query(default=None, ge=0, le=7),
    facility: list[int] | None = Query(default=None),
    program: list[str] | None = Query(default=None),
    hostname: str | None = Query(default=None, max_length=255),
    rule_id: int | None = None,
    q: str | None = Q,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    flt = await _filter(
        "syslog", rt, session, device_id=device_id, group_id=group_id, unknown=unknown,
        source_ip=source_ip, rule_id=rule_id, q=q, since=since, until=until,
        severity_max=severity_max, facilities=list(facility or []),
        programs=list(program or []), hostname=hostname,
    )  # fmt: skip
    return await query.stats(session, flt)


@router.get("/api/syslog", dependencies=SYSLOG)
async def list_syslog(
    _: Viewer,
    rt: RT,
    session: DB,
    device_id: int | None = None,
    group_id: int | None = None,
    unknown: bool | None = None,
    source_ip: str | None = None,
    severity_max: int | None = Query(default=None, ge=0, le=7),
    facility: list[int] | None = Query(default=None),
    program: list[str] | None = Query(default=None),
    hostname: str | None = Query(default=None, max_length=255),
    rule_id: int | None = None,
    q: str | None = Q,
    since: str | None = None,
    until: str | None = None,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    for f in facility or []:
        if not 0 <= f <= 23:
            raise validation_error(["query", "facility"], "must be 0-23")
    flt = await _filter(
        "syslog", rt, session, device_id=device_id, group_id=group_id, unknown=unknown,
        source_ip=source_ip, rule_id=rule_id, q=q, since=since, until=until,
        severity_max=severity_max, facilities=list(facility or []),
        programs=list(program or []), hostname=hostname,
    )  # fmt: skip
    return await query.list_page(session, flt, limit, cursor)


@router.get("/api/syslog/{message_id}", dependencies=SYSLOG)
async def get_syslog(message_id: int, _: Viewer, session: DB) -> dict:
    out = await query.get_one(session, "syslog", message_id)
    if out is None:
        raise not_found("Syslog message")
    return out


# ================================================================ ingest status / sources
@router.get("/api/ingest/status", dependencies=INGEST)
async def ingest_status(user: Viewer, rt: RT) -> dict:
    return rt.ingest.status(admin=user.role == "admin")


@router.get("/api/ingest/unknown-sources", dependencies=INGEST)
async def unknown_sources(
    _: Viewer, rt: RT, session: DB, days: int = Query(default=7, ge=1, le=30)
) -> dict:
    since = rt.clock.now() - timedelta(days=days)
    rows = (
        await session.execute(
            text(
                """
                SELECT ip, sum(sc) AS syslog_count, sum(tc) AS trap_count, max(last) AS last_seen,
                       (SELECT m.hostname FROM syslog_messages m
                        WHERE m.source_ip = u.ip AND m.device_id IS NULL AND m.time >= :since
                        ORDER BY m.time DESC LIMIT 1) AS last_hostname
                FROM (
                    SELECT source_ip AS ip, count(*) AS sc, 0 AS tc, max(time) AS last
                    FROM syslog_messages WHERE device_id IS NULL AND time >= :since
                    GROUP BY source_ip
                    UNION ALL
                    SELECT source_ip, 0, count(*), max(time)
                    FROM traps WHERE device_id IS NULL AND time >= :since GROUP BY source_ip
                ) u GROUP BY ip ORDER BY last_seen DESC LIMIT 200
                """
            ),
            {"since": since},
        )
    ).all()
    ignored = rt.ingest.ignored
    from netmon.ingest.attribution import in_networks

    return {
        "items": [
            {
                "source_ip": str(r.ip).split("/", 1)[0],
                "syslog_count": int(r.syslog_count or 0),
                "trap_count": int(r.trap_count or 0),
                "last_seen": iso(r.last_seen),
                "last_hostname": r.last_hostname or "",
                "ignored": in_networks(str(r.ip).split("/", 1)[0], ignored),
            }
            for r in rows
        ]
    }


@router.post("/api/ingest/unknown-sources/assign", dependencies=INGEST)
async def assign_source(body: AssignSourceIn, _: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    d = (
        await session.execute(select(Device).where(Device.id == body.device_id).with_for_update())
    ).scalar_one_or_none()
    if d is None:
        raise not_found("Device")
    current = [str(a) for a in d.source_addresses or []]
    if body.source_ip in current:
        raise validation_error(["body", "source_ip"], "already a source address of this device")
    if len(current) >= MAX_SOURCE_ADDRESSES:
        raise validation_error(
            ["body", "source_ip"], f"a device has at most {MAX_SOURCE_ADDRESSES} source addresses"
        )
    new = [*current, body.source_ip]
    d.source_addresses = new
    await audit.record(
        session,
        ctx,
        "ingest.assign_source",
        audit.entity("device", d.id, d.name, device_id=d.id),
        {"source_addresses": [current, new]},
    )
    await session.commit()
    rt.config_changed("device", d.id, "updated")
    await rt.ingest.rebuild_sources()
    rt.spawn(reattribute(rt, body.source_ip, d.id))
    return {"device_id": d.id, "source_addresses": new, "reassigning": True}


async def reattribute(rt: Any, ip: str, device_id: int) -> int:
    """Stored rows of `ip` without a device -> `device_id`, in batches (no rule
    re-evaluation)."""
    total = 0
    for table in ("syslog_messages", "traps"):
        while True:
            async with rt.sessionmaker() as session:
                res = await session.execute(
                    text(
                        f"UPDATE {table} SET device_id = :d WHERE (id, time) IN "  # noqa: S608
                        f"(SELECT id, time FROM {table} WHERE source_ip = CAST(:ip AS inet) "
                        "AND device_id IS NULL LIMIT :n)"
                    ),
                    {"d": device_id, "ip": ip, "n": REATTRIBUTE_BATCH},
                )
                await session.commit()
            n = res.rowcount or 0
            total += n
            if n < REATTRIBUTE_BATCH:
                break
            await asyncio.sleep(0)
    return total


async def detach_device(rt: Any, device_id: int) -> None:
    """Device deleted: its traps/syslog rows become an unknown source (batches)."""
    for table in ("syslog_messages", "traps"):
        while True:
            try:
                async with rt.sessionmaker() as session:
                    res = await session.execute(
                        text(
                            f"UPDATE {table} SET device_id = NULL WHERE (id, time) IN "  # noqa: S608
                            f"(SELECT id, time FROM {table} WHERE device_id = :d LIMIT :n)"
                        ),
                        {"d": device_id, "n": REATTRIBUTE_BATCH},
                    )
                    await session.commit()
            except Exception:  # noqa: BLE001
                log.exception("Detaching the %s rows of device %s failed", table, device_id)
                break
            if (res.rowcount or 0) < REATTRIBUTE_BATCH:
                break
            await asyncio.sleep(0)


@router.post(
    "/api/ingest/forwarders/test",
    # role before feature state: a viewer gets 403, never 409 feature_disabled
    dependencies=[Depends(require_admin), Depends(feature_disabled("siem_forwarding"))],
)
async def forwarder_test(body: ForwardTestIn, _: Admin, rt: RT, session: DB) -> dict:
    from netmon.ingest.forward import Target, send_test

    if body.target is not None:
        target = Target.from_dict(body.target.model_dump())
    elif body.target_id:
        stored = await settings_svc.get(session, "siem_targets")
        found = next((t for t in stored or [] if t.get("id") == body.target_id), None)
        if found is None:
            raise not_found("SIEM target")
        target = Target.from_dict(found)
    else:
        raise validation_error(["body", "target_id"], "target_id or target is required")
    await session.close()
    try:
        await send_test(target, rt.version)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(502, "forward_error", (str(exc) or type(exc).__name__)[:300]) from None
    return {"ok": True, "message": "sent"}


# ================================================================ event rules
RULE_FIELDS = (
    "name",
    "kind",
    "enabled",
    "priority",
    "scope",
    "group_id",
    "device_id",
    "match",
    "action",
    "key_varbind_oid",
    "key_regex",
    "clear_match",
    "clear_after_min",
)


async def _rule_rows(session: DB, rule_id: int | None = None, kind: str | None = None) -> list:
    conds, p = [], {}
    if rule_id is not None:
        conds.append("r.id = :id")
        p["id"] = rule_id
    if kind is not None:
        conds.append("r.kind = :kind")
        p["kind"] = kind
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    return list(
        (
            await session.execute(
                text(
                    "SELECT r.*, COALESCE(g.name, d.name) AS scope_name, "
                    "(SELECT count(*) FROM event_conditions c WHERE c.rule_id = r.id) AS active "
                    "FROM event_rules r LEFT JOIN groups g ON g.id = r.group_id "
                    f"LEFT JOIN devices d ON d.id = r.device_id{where} "
                    "ORDER BY r.priority, r.id"
                ),
                p,
            )
        ).mappings()
    )


def rule_name(r: Any, lang: str | None) -> str:
    """A built-in rule whose name is still one of its default names is shown in the
    requesting user's language (he/en); renamed and user-created rules as typed."""
    names = BUILTIN_RULES.get(r["builtin_key"] or "", {}).get("names")
    if names and r["name"] in names.values():
        return str(names.get(lang or "en") or names["en"])
    return str(r["name"])


def rule_out(r: Any, lang: str | None = None) -> dict[str, Any]:
    return {
        "id": int(r["id"]),
        "name": rule_name(r, lang),
        "kind": r["kind"],
        "enabled": bool(r["enabled"]),
        "builtin": r["builtin_key"],
        "priority": int(r["priority"]),
        "scope": r["scope"],
        "group_id": r["group_id"],
        "device_id": r["device_id"],
        "scope_name": r["scope_name"],
        "match": r["match"],
        "action": r["action"],
        "key_varbind_oid": r["key_varbind_oid"],
        "key_regex": r["key_regex"],
        "clear_match": r["clear_match"],
        "clear_after_min": r["clear_after_min"],
        "active_conditions": int(r["active"] or 0),
        "last_matched_at": iso(r["last_matched_at"]),
        "created_at": iso(r["created_at"]),
        "updated_at": iso(r["updated_at"]),
    }


def _audit_values(v: dict[str, Any]) -> dict[str, Any]:
    return {k: v.get(k) for k in RULE_FIELDS}


async def _check_refs(session: DB, v: dict[str, Any]) -> None:
    if (
        v["group_id"] is not None
        and (
            await session.execute(text("SELECT 1 FROM groups WHERE id = :i"), {"i": v["group_id"]})
        ).first()
        is None
    ):
        raise validation_error(["body", "group_id"], "group does not exist")
    if v["device_id"] is not None and await session.get(Device, v["device_id"]) is None:
        raise validation_error(["body", "device_id"], "device does not exist")


async def _get_rule(session: DB, rule_id: int) -> dict[str, Any]:
    rows = await _rule_rows(session, rule_id)
    if not rows:
        raise not_found("Event rule")
    return dict(rows[0])


_RULE_SET = (
    "name = :name, kind = :kind, enabled = :enabled, priority = :priority, scope = :scope, "
    "group_id = :group_id, device_id = :device_id, match = CAST(:match AS jsonb), "
    "action = :action, key_varbind_oid = :key_varbind_oid, key_regex = :key_regex, "
    "clear_match = CAST(:clear_match AS jsonb), clear_after_min = :clear_after_min"
)


def _rule_params(v: dict[str, Any]) -> dict[str, Any]:
    p = dict(v)
    p["match"] = json.dumps(v["match"])
    p["clear_match"] = json.dumps(v["clear_match"]) if v["clear_match"] is not None else None
    return p


async def _rules_changed(rt: RT, rule_id: int, action: str, drop: bool) -> None:
    if drop:
        await rt.event_sensors.remove_rule_conditions([rule_id])
    await rt.event_sensors.reload()
    rt.config_changed("event_rule", rule_id, action)


@router.get("/api/event-rules", dependencies=INGEST)
async def list_rules(
    user: Viewer, session: DB, kind: str | None = Query(default=None, pattern="^(trap|syslog)$")
) -> dict:
    lang = user.language
    return {"items": [rule_out(r, lang) for r in await _rule_rows(session, kind=kind)]}


@router.post("/api/event-rules", status_code=201, dependencies=INGEST)
async def create_rule(body: EventRuleIn, user: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    data = body.model_dump(exclude_unset=True)
    data.setdefault("enabled", True)
    data.setdefault("priority", 100)
    data.setdefault("scope", "global")
    v = rules.validate_rule(data)
    await _check_refs(session, v)
    n = (await session.execute(text("SELECT count(*) FROM event_rules"))).scalar_one()
    if n >= MAX_RULES:
        raise validation_error(["body"], f"at most {MAX_RULES} event rules")
    rid = (
        await session.execute(
            text(
                "INSERT INTO event_rules (name, kind, enabled, priority, scope, group_id, "
                "device_id, match, action, key_varbind_oid, key_regex, clear_match, "
                "clear_after_min) VALUES (:name, :kind, :enabled, :priority, :scope, :group_id, "
                ":device_id, CAST(:match AS jsonb), :action, :key_varbind_oid, :key_regex, "
                "CAST(:clear_match AS jsonb), :clear_after_min) RETURNING id"
            ),
            _rule_params(v),
        )
    ).scalar_one()
    await audit.record(
        session,
        ctx,
        "event_rule.create",
        audit.entity("event_rule", rid, v["name"]),
        audit.created(_audit_values(v)),
    )
    await session.commit()
    await _rules_changed(rt, rid, "created", drop=False)
    return rule_out(await _get_rule(session, rid), user.language)


@router.get("/api/event-rules/{rule_id}", dependencies=INGEST)
async def get_rule(rule_id: int, user: Viewer, session: DB) -> dict:
    return rule_out(await _get_rule(session, rule_id), user.language)


@router.patch("/api/event-rules/{rule_id}", dependencies=INGEST)
async def patch_rule(
    rule_id: int, body: EventRuleIn, user: Admin, rt: RT, session: DB, ctx: Audit
) -> dict:
    old = await _get_rule(session, rule_id)
    data = body.model_dump(exclude_unset=True)
    if "kind" in data and data["kind"] != old["kind"]:
        raise validation_error(["body", "kind"], "the kind of a rule cannot be changed")
    merged = {k: old[k] for k in RULE_FIELDS}
    merged.update(data)
    v = rules.validate_rule(merged)
    await _check_refs(session, v)
    await session.execute(
        text(f"UPDATE event_rules SET {_RULE_SET}, updated_at = now() WHERE id = :id"),
        {**_rule_params(v), "id": rule_id},
    )
    await audit.record(
        session,
        ctx,
        "event_rule.update",
        audit.entity("event_rule", rule_id, v["name"]),
        audit.diff(_audit_values(old), _audit_values(v)),
    )
    await session.commit()
    await _rules_changed(rt, rule_id, "updated", drop=True)
    return rule_out(await _get_rule(session, rule_id), user.language)


@router.delete("/api/event-rules/{rule_id}", status_code=204, dependencies=INGEST)
async def delete_rule(rule_id: int, _: Admin, rt: RT, session: DB, ctx: Audit) -> Response:
    old = await _get_rule(session, rule_id)
    if old["builtin_key"]:
        raise ApiError(409, "builtin_rule", "Built-in rules cannot be deleted (disable them)")
    await session.close()
    await rt.event_sensors.remove_rule_conditions([rule_id])
    async with rt.sessionmaker() as s2:
        await s2.execute(text("DELETE FROM event_rules WHERE id = :id"), {"id": rule_id})
        await audit.record(
            s2,
            ctx,
            "event_rule.delete",
            audit.entity("event_rule", rule_id, old["name"]),
            audit.deleted(_audit_values(old)),
        )
        await s2.commit()
    await _rules_changed(rt, rule_id, "deleted", drop=False)
    return Response(status_code=204)


@router.post("/api/event-rules/{rule_id}/reset", dependencies=INGEST)
async def reset_rule(rule_id: int, user: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    old = await _get_rule(session, rule_id)
    key = old["builtin_key"]
    if not key or key not in BUILTIN_RULES:
        raise not_found("Built-in rule")
    lang = await settings_svc.get(session, "default_language")
    v = {**builtin_values(key, str(lang)), "enabled": bool(old["enabled"])}
    await session.execute(
        text(f"UPDATE event_rules SET {_RULE_SET}, updated_at = now() WHERE id = :id"),
        {**_rule_params(v), "id": rule_id},
    )
    await audit.record(
        session,
        ctx,
        "event_rule.update",
        audit.entity("event_rule", rule_id, v["name"]),
        audit.diff(_audit_values(old), _audit_values(v)),
    )
    await session.commit()
    await _rules_changed(rt, rule_id, "updated", drop=True)
    return rule_out(await _get_rule(session, rule_id), user.language)


@router.post("/api/event-rules/test", dependencies=INGEST)
async def test_rule(body: EventRuleTestIn, _: Admin, rt: RT, session: DB) -> dict:
    data = body.rule.model_dump(exclude_unset=True)
    data.setdefault("scope", "global")
    data.setdefault("name", "test")
    v = rules.validate_rule(data)
    rule = rules.Rule(id=0, **{k: v[k] for k in RULE_FIELDS if k not in ("enabled",)})
    since = rt.clock.now() - timedelta(hours=body.hours)
    inv = await load_inventory(session)
    tbl = "traps" if v["kind"] == "trap" else "syslog_messages"
    rows = (
        (
            await session.execute(
                text(
                    f"SELECT x.*, d.name AS device_name FROM {tbl} x "  # noqa: S608
                    "JOIN devices d ON d.id = x.device_id WHERE x.time >= :since "
                    "ORDER BY x.time DESC, x.id DESC LIMIT 5000"
                ),
                {"since": since},
            )
        )
        .mappings()
        .all()
    )
    await session.close()
    scanned = matched = cleared = 0
    items: list[dict[str, Any]] = []
    for r in rows:
        did = int(r["device_id"])
        dev = inv.devices.get(did)
        chain = inv.ancestors(dev["group_id"]) if dev else []
        if not rules.in_scope(rule, did, chain):
            continue
        scanned += 1
        rec = query.trap_out(r) if v["kind"] == "trap" else query.syslog_out(r)
        info = await rt.ingest.port_info(did) if v["kind"] == "trap" else None
        ctx = rules.Context(important=rt.ingest._important_fn(info))  # noqa: SLF001
        out = rules.evaluate(rec, [rule], ctx)
        result = key = None
        if rule.action == rules.BACKUP_ACTION:  # [3] a hit queues a config backup
            if rules.backup_triggers(rec, [rule]):
                matched += 1
                result, key = "raise", ""
        elif out.raised is not None or out.suppressed is not None:
            matched += 1
            result, key = "raise", (out.raised or out.suppressed)[1]  # type: ignore[index]
        elif out.ignored is not None:
            matched += 1
            result, key = "ignore", ""
        elif out.clears:
            cleared += 1
            result, key = "clear", out.clears[0][1]
        if result is not None and len(items) < 50:
            items.append(
                {
                    "kind": v["kind"],
                    "id": rec["id"],
                    "time": rec["time"],
                    "device_name": r["device_name"],
                    "text": rec["summary"] if v["kind"] == "trap" else rec["message"],
                    "result": result,
                    "key": key,
                }
            )
    return {"scanned": scanned, "matched": matched, "cleared": cleared, "items": items}


# ================================================================ event sensors
async def _event_sensor(session: DB, sensor_id: int) -> Sensor:
    s = await session.get(Sensor, sensor_id)
    if s is None:
        raise not_found("Sensor")
    if not is_event_type(s.type):
        raise ApiError(400, "not_event_sensor", "The sensor is not an event sensor")
    return s


@router.get("/api/sensors/{sensor_id}/conditions")
async def sensor_conditions(sensor_id: int, _: Viewer, session: DB) -> dict:
    await _event_sensor(session, sensor_id)
    return {"items": await conditions_out(session, sensor_id)}


@router.post("/api/sensors/{sensor_id}/clear")
async def clear_sensor(sensor_id: int, _: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    s = await _event_sensor(session, sensor_id)
    d = await session.get(Device, s.device_id)
    assert d is not None
    n = (
        await session.execute(
            text("SELECT count(*) FROM event_conditions WHERE sensor_id = :s"), {"s": sensor_id}
        )
    ).scalar_one()
    await audit.record(
        session,
        ctx,
        "sensor.clear_events",
        audit.entity("sensor", s.id, s.name, device_id=d.id),
        {"conditions": [int(n), 0]},
    )
    await session.commit()
    await rt.event_sensors.clear_sensor(sensor_id)
    async with rt.sessionmaker() as s2:
        s = await s2.get(Sensor, sensor_id)
        d = await s2.get(Device, s.device_id) if s else None
        if s is None or d is None:
            raise not_found("Sensor")
        return sensor_out(
            s,
            d.name,
            d.group_id,
            in_maintenance=bool(rt.maintenance and rt.maintenance.in_maintenance(d.id)),
        )
