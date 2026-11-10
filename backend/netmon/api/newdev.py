"""[3] New-device alerts API (api.md "New-device alerts", feature `new_device_alerts`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import text

from netmon.api.common import commit_or_conflict
from netmon.api.deps import DB, RT, Admin, Audit, Page, Viewer
from netmon.api.errors import ApiError, not_found
from netmon.ingest.query import parse_time
from netmon.newdev import detector
from netmon.newdev.service import known_macs_out
from netmon.ports import oui
from netmon.schemas.phase3 import AllowEntryIn, NewDeviceAckIn
from netmon.services import audit
from netmon.services.features import feature_disabled
from netmon.services.serialize import iso, page
from netmon.services.status import load_inventory

router = APIRouter(dependencies=[Depends(feature_disabled("new_device_alerts"))])
STATES = ("baseline", "new", "known", "allowed")
MAX_ALLOWLIST = 5000


def _detector(rt: Any) -> Any:
    d = getattr(rt, "newdev", None)
    if d is None:
        raise ApiError(409, "feature_disabled", "The feature new_device_alerts is turned off")
    return d


@router.get("/api/new-devices/status")
async def status(_: Viewer, rt: RT, session: DB) -> dict:
    return await _detector(rt).status(session)


@router.get("/api/new-devices")
async def list_new_devices(
    _: Viewer,
    rt: RT,
    session: DB,
    pg: Page,
    state: list[str] | None = Query(default=None),
    device_id: int | None = None,
    group_id: int | None = None,
    q: str | None = Query(default=None, max_length=128),
    since: str | None = None,
) -> dict:
    where: list[str] = []
    params: dict[str, Any] = {}
    states = [s for st in state or [] for s in st.split(",") if s]
    bad = [s for s in states if s not in STATES]
    if bad:
        raise ApiError(
            422,
            "validation_error",
            "Invalid state",
            [{"loc": ["query", "state"], "msg": f"must be in {list(STATES)}"}],
        )
    if states:
        where.append("k.state = ANY(:states)")
        params["states"] = states
    if device_id is not None:
        where.append("(k.last_device_id = :dev OR k.first_device_id = :dev)")
        params["dev"] = device_id
    if group_id is not None:
        inv = await load_inventory(session)
        gids = set(inv.descendants(group_id)) if group_id in inv.groups else set()
        params["gdev"] = sorted(d for d, v in inv.devices.items() if v["group_id"] in gids)
        where.append("(k.last_device_id = ANY(:gdev) OR k.first_device_id = ANY(:gdev))")
    if since:
        params["since"] = parse_time(since, "since")
        where.append("COALESCE(k.detected_at, k.first_seen) >= :since")
    if q:
        term = q.strip().lower()
        hexq = detector._hex(term)  # noqa: SLF001
        params["like"] = f"%{term}%"
        cond = [
            "lower(COALESCE(k.vendor, '')) LIKE :like",
            "COALESCE(k.first_ip, '') LIKE :like",
            "lower(COALESCE(k.first_hostname, '')) LIKE :like",
            "k.mac IN (SELECT a.mac FROM arp_entries a WHERE a.ip LIKE :like)",
            "k.mac IN (SELECT a.mac FROM arp_entries a JOIN dns_cache c ON c.ip = a.ip "
            "WHERE lower(COALESCE(c.name, '')) LIKE :like)",
        ]
        if hexq and all(ch in "0123456789abcdef" for ch in hexq):
            params["macp"] = hexq + "%"
            cond.append("replace(k.mac, ':', '') LIKE :macp")
        where.append("(" + " OR ".join(cond) + ")")
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    total = (
        await session.execute(text(f"SELECT count(*) FROM known_macs k {wsql}"), params)  # noqa: S608
    ).scalar_one()
    rows = (
        (
            await session.execute(
                text(
                    f"SELECT k.* FROM known_macs k {wsql} "  # noqa: S608
                    "ORDER BY (k.state = 'new') DESC, COALESCE(k.detected_at, k.first_seen) DESC, "
                    "k.mac LIMIT :lim OFFSET :off"
                ),
                {**params, "lim": pg.page_size, "off": pg.offset},
            )
        )
        .mappings()
        .all()
    )
    return page(await known_macs_out(session, rows=rows), int(total), pg.page, pg.page_size)


@router.post("/api/new-devices/acknowledge")
async def acknowledge(body: NewDeviceAckIn, user: Admin, rt: RT, ctx: Audit) -> dict:
    n = await _detector(rt).acknowledge(body.macs, body.comment, user.username, ctx)
    return {"acknowledged": n}


@router.post("/api/new-devices/restart-learning")
async def restart_learning(_: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    det = _detector(rt)
    until = await det.restart_learning()
    await audit.record(
        session,
        ctx,
        "newdev.restart_learning",
        None,
        {"learning_until": [None, iso(until)]},
    )
    await session.commit()
    return await det.status(session)


# ------------------------------------------------------------------ allow-list
def allow_out(r: Any) -> dict[str, Any]:
    return {
        "id": int(r["id"]),
        "prefix": r["prefix"],
        "bits": int(r["bits"]),
        "vendor": oui.vendor(r["prefix"]) if int(r["bits"]) >= 24 else None,
        "comment": r["comment"],
        "created_by": r["created_by"],
        "created_at": iso(r["created_at"]),
    }


@router.get("/api/new-devices/allowlist")
async def list_allowlist(_: Viewer, session: DB) -> dict:
    rows = (
        (await session.execute(text("SELECT * FROM mac_allowlist ORDER BY prefix")))
        .mappings()
        .all()
    )
    return {"items": [allow_out(r) for r in rows]}


@router.post("/api/new-devices/allowlist", status_code=201)
async def create_allow(body: AllowEntryIn, user: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    parsed = detector.parse_prefix(body.prefix)
    assert parsed is not None
    prefix, bits = parsed
    n = (await session.execute(text("SELECT count(*) FROM mac_allowlist"))).scalar_one()
    if n >= MAX_ALLOWLIST:
        raise ApiError(
            422,
            "validation_error",
            f"The allow-list holds at most {MAX_ALLOWLIST} entries",
            [{"loc": ["body", "prefix"], "msg": "allow-list full"}],
        )
    exists = (
        await session.execute(text("SELECT 1 FROM mac_allowlist WHERE prefix = :p"), {"p": prefix})
    ).first()
    if exists:
        raise ApiError(
            409, "conflict", "This prefix is already in the allow-list", [{"field": "prefix"}]
        )
    row = (
        (
            await session.execute(
                text(
                    "INSERT INTO mac_allowlist (prefix, bits, comment, created_by, created_at) "
                    "VALUES (:p, :b, :c, :by, :now) RETURNING *"
                ),
                {
                    "p": prefix,
                    "b": bits,
                    "c": body.comment,
                    "by": user.username[:64],
                    "now": rt.clock.now(),
                },
            )
        )
        .mappings()
        .one()
    )
    await audit.record(
        session,
        ctx,
        "newdev.allowlist_create",
        audit.entity("mac_allowlist", int(row["id"]), prefix),
        audit.created({"prefix": prefix, "bits": bits, "comment": body.comment}),
    )
    await commit_or_conflict(session, "prefix")
    rt.config_changed("mac_allowlist", int(row["id"]), "created")
    return allow_out(row)


@router.delete("/api/new-devices/allowlist/{entry_id}", status_code=204)
async def delete_allow(entry_id: int, _: Admin, rt: RT, session: DB, ctx: Audit) -> Response:
    row = (
        (
            await session.execute(
                text("DELETE FROM mac_allowlist WHERE id = :i RETURNING *"), {"i": entry_id}
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Allow-list entry")
    await audit.record(
        session,
        ctx,
        "newdev.allowlist_delete",
        audit.entity("mac_allowlist", entry_id, row["prefix"]),
        audit.deleted({"prefix": row["prefix"], "bits": row["bits"], "comment": row["comment"]}),
    )
    await session.commit()
    rt.config_changed("mac_allowlist", entry_id, "deleted")
    return Response(status_code=204)


@router.delete("/api/new-devices/{mac}", status_code=204)
async def forget(mac: str, _: Admin, rt: RT, session: DB, ctx: Audit) -> Response:
    norm = detector.normalize_mac(mac)
    if norm is None:
        raise not_found("MAC address")
    if not await _detector(rt).forget(norm):
        raise not_found("MAC address")
    await audit.record(session, ctx, "newdev.forget", audit.entity("known_mac", None, norm), {})
    await session.commit()
    return Response(status_code=204)
