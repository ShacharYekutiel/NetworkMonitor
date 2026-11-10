"""[3] Flow collector API (api.md "[3] Flows"; feature `flows`)."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from netmon.api.deps import DB, RT, Admin, Audit, Viewer
from netmon.api.errors import ApiError, not_found, validation_error
from netmon.flows import query
from netmon.flows import storage as flow_storage
from netmon.services import audit
from netmon.services.features import feature_disabled

log = logging.getLogger("netmon.api.flows")
router = APIRouter(dependencies=[Depends(feature_disabled("flows"))])


class ExporterPatch(BaseModel):
    model_config = {"extra": "forbid"}

    state: Literal["accepted", "ignored"] | None = None
    device_id: int | None = None
    sampling_override: int | None = Field(default=None, ge=1, le=1000000)


def _collector(rt: Any) -> Any:
    return getattr(rt, "flows", None)


def _mode(rt: Any) -> str | None:
    return getattr(rt, "flows_rollup", None)


def _range(rt: Any, since: str | None, until: str | None, conv: bool = False) -> query.Range:
    try:
        return query.make_range(
            rt.clock.now(), since, until, query.CONV_MAX if conv else query.RANGE_MAX
        )
    except query.QueryError as exc:
        raise validation_error(["query", exc.loc], exc.msg) from None


async def _ids(session: DB, exporter_id: int | None, device_id: int | None) -> list[int]:
    return await query.exporter_ids(session, exporter_id, device_id)


def _services(rt: Any) -> Any:
    c = _collector(rt)
    if c is not None:
        return c.services()
    from netmon.flows.services import default_map

    return default_map()


# ================================================================ status / exporters
@router.get("/api/flows/status")
async def flows_status(user: Viewer, rt: RT) -> dict:
    c = _collector(rt)
    if c is None:
        raise ApiError(503, "unavailable", "The flow collector is not running")
    return await c.status(admin=user.role == "admin")


@router.get("/api/flows/exporters")
async def list_exporters(
    _: Viewer,
    rt: RT,
    session: DB,
    state: Literal["accepted", "pending", "ignored"] | None = None,
) -> dict:
    return {"items": await query.exporters_out(_collector(rt), session, state=state)}


@router.patch("/api/flows/exporters/{exporter_id}")
async def patch_exporter(
    exporter_id: int, body: ExporterPatch, _: Admin, rt: RT, session: DB, ctx: Audit
) -> dict:
    row = (
        (
            await session.execute(
                text(
                    "SELECT id, host(address) AS address, state, device_id, sampling_override "
                    "FROM flow_exporters WHERE id = :id FOR UPDATE"
                ),
                {"id": exporter_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Exporter")
    fields = body.model_fields_set
    new = {
        "state": row["state"],
        "device_id": row["device_id"],
        "sampling_override": row["sampling_override"],
    }
    if "state" in fields:
        if body.state is None:
            raise validation_error(["body", "state"], "must be accepted or ignored")
        new["state"] = body.state
    if "device_id" in fields:
        if body.device_id is not None:
            exists = (
                await session.execute(
                    text("SELECT name FROM devices WHERE id = :d"), {"d": body.device_id}
                )
            ).first()
            if exists is None:
                raise validation_error(["body", "device_id"], "device not found")
        new["device_id"] = body.device_id
    if "sampling_override" in fields:
        new["sampling_override"] = body.sampling_override
    old = {k: row[k] for k in new}
    if new != old:
        await session.execute(
            text(
                "UPDATE flow_exporters SET state = :state, device_id = :device_id, "
                "sampling_override = :sampling_override WHERE id = :id"
            ),
            {**new, "id": exporter_id},
        )
        await audit.record(
            session,
            ctx,
            "flows.exporter_update",
            audit.entity(
                "flow_exporter", exporter_id, str(row["address"]), device_id=new["device_id"]
            ),
            audit.diff(old, new),
        )
    await session.commit()
    c = _collector(rt)
    if c is not None:
        from netmon.ingest.attribution import normalize_ip

        addr = normalize_ip(str(row["address"])) or str(row["address"])
        c.exporter_updated(addr, new["state"], new["device_id"], new["sampling_override"])
    items = await query.exporters_out(c, session, ids=[exporter_id])
    if new != old:
        rt.config_changed("flow_exporter", exporter_id, "updated")
        if new["state"] != old["state"]:
            rt.hub.broadcast("flows.exporter", {"exporter": items[0]}, admin_only=True)
    return items[0]


@router.delete("/api/flows/exporters/{exporter_id}", status_code=204)
async def delete_exporter(exporter_id: int, _: Admin, rt: RT, session: DB, ctx: Audit) -> Response:
    row = (
        (
            await session.execute(
                text(
                    "DELETE FROM flow_exporters WHERE id = :id "
                    "RETURNING host(address) AS address, device_id, state"
                ),
                {"id": exporter_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Exporter")
    await audit.record(
        session,
        ctx,
        "flows.exporter_delete",
        audit.entity("flow_exporter", exporter_id, str(row["address"]), device_id=row["device_id"]),
        audit.deleted({"address": str(row["address"]), "state": row["state"]}),
    )
    await session.commit()
    c = _collector(rt)
    if c is not None:
        from netmon.ingest.attribution import normalize_ip

        c.exporter_deleted(normalize_ip(str(row["address"])) or str(row["address"]))
    rt.config_changed("flow_exporter", exporter_id, "deleted")
    rt.spawn(flow_storage.delete_exporter_rows(rt, exporter_id))
    return Response(status_code=204)


# ================================================================ tops
LimitQ = Query(default=10, ge=1, le=100)


@router.get("/api/flows/top/talkers")
async def talkers(
    _: Viewer,
    rt: RT,
    session: DB,
    exporter_id: int | None = None,
    device_id: int | None = None,
    if_index: int | None = Query(default=None, ge=0),
    direction: Literal["in", "out"] | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = LimitQ,
    side: Literal["src", "dst", "both"] = "both",
) -> dict:
    rng = _range(rt, since, until)
    _iface_needs_exporter(if_index, exporter_id, device_id)
    ids = await _ids(session, exporter_id, device_id)
    return await query.top_talkers(
        session,
        rng,
        ids,
        mode=_mode(rt),
        now=rt.clock.now(),
        side=side,
        limit=limit,
        if_index=if_index,
        direction=direction,
    )


@router.get("/api/flows/top/applications")
async def applications(
    _: Viewer,
    rt: RT,
    session: DB,
    exporter_id: int | None = None,
    device_id: int | None = None,
    if_index: int | None = Query(default=None, ge=0),
    direction: Literal["in", "out"] | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = LimitQ,
) -> dict:
    rng = _range(rt, since, until)
    _iface_needs_exporter(if_index, exporter_id, device_id)
    ids = await _ids(session, exporter_id, device_id)
    return await query.top_applications(
        session,
        rng,
        ids,
        mode=_mode(rt),
        now=rt.clock.now(),
        limit=limit,
        services=_services(rt),
        if_index=if_index,
        direction=direction,
    )


@router.get("/api/flows/top/conversations")
async def conversations(
    _: Viewer,
    rt: RT,
    session: DB,
    exporter_id: int | None = None,
    device_id: int | None = None,
    if_index: int | None = Query(default=None, ge=0),
    direction: Literal["in", "out"] | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = LimitQ,
) -> dict:
    rng = _range(rt, since, until, conv=True)
    _iface_needs_exporter(if_index, exporter_id, device_id)
    ids = await _ids(session, exporter_id, device_id)
    rng.source = "minute"
    return await query.top_conversations(
        session,
        rng,
        ids,
        limit=limit,
        services=_services(rt),
        if_index=if_index,
        direction=direction,
    )


@router.get("/api/flows/interfaces")
async def interfaces(
    _: Viewer,
    rt: RT,
    session: DB,
    exporter_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    rng = _range(rt, since, until)
    ids = await _ids(session, exporter_id, None)
    return {"items": await query.interfaces(session, rng, ids, mode=_mode(rt), now=rt.clock.now())}


@router.get("/api/flows/series")
async def flow_series(
    _: Viewer,
    rt: RT,
    session: DB,
    exporter_id: int | None = None,
    device_id: int | None = None,
    if_index: int | None = Query(default=None, ge=0),
    direction: Literal["in", "out"] | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = LimitQ,
    split: Literal["none", "app", "host"] = "none",
) -> dict:
    rng = _range(rt, since, until)
    _iface_needs_exporter(if_index, exporter_id, device_id)
    ids = await _ids(session, exporter_id, device_id)
    eid = exporter_id if exporter_id is not None else (ids[0] if len(ids) == 1 else None)
    return await query.series(
        session,
        rng,
        ids,
        mode=_mode(rt),
        now=rt.clock.now(),
        split=split,
        services=_services(rt),
        exporter_id=eid,
        if_index=if_index,
        direction=direction,
    )


def _iface_needs_exporter(
    if_index: int | None, exporter_id: int | None, device_id: int | None
) -> None:
    if if_index is not None and exporter_id is None and device_id is None:
        raise validation_error(
            ["query", "if_index"], "interface-level queries need exporter_id or device_id"
        )
