"""[2b-3] Device timeline (api.md "Device timeline")."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query

from netmon.api.deps import DB, RT, Viewer
from netmon.api.errors import not_found, validation_error
from netmon.ingest.query import decode_cursor, time_range
from netmon.models import Device
from netmon.services import timeline as timeline_svc
from netmon.services.features import feature_disabled
from netmon.services.status import load_inventory

router = APIRouter()


@router.get(
    "/api/devices/{device_id}/timeline", dependencies=[Depends(feature_disabled("timeline"))]
)
async def device_timeline(
    device_id: int,
    user: Viewer,
    rt: RT,
    session: DB,
    kinds: list[str] | None = Query(default=None),
    since: str | None = None,
    until: str | None = None,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    d = await session.get(Device, device_id)
    if d is None:
        raise not_found("Device")
    bad = [k for k in kinds or [] if k not in timeline_svc.KINDS]
    if bad:
        raise validation_error(["query", "kinds"], f"must be in {list(timeline_svc.KINDS)}")
    available = {"status", "notification", "ack", "maintenance", "port"}
    if rt.features.enabled("traps"):
        available.add("trap")
    if rt.features.enabled("syslog"):
        available.add("syslog")
    if user.role == "admin" and rt.features.enabled("audit"):
        available.add("audit")
    wanted = [k for k in (kinds or timeline_svc.KINDS) if k in available]
    s, u = time_range(since, until, rt.clock.now(), timedelta(hours=24))
    cur = None
    if cursor:
        ct, crank, cid = decode_cursor(cursor, 3)
        cur = (ct, int(crank), int(cid))
    inv = await load_inventory(session)
    chain = inv.ancestors(d.group_id)
    return await timeline_svc.timeline(session, device_id, chain, wanted, s, u, cur, limit)
