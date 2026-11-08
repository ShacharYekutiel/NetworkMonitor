"""[2b-3] Audit log (admin; feature `audit`): list with filters, streamed CSV export."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from netmon.api.deps import DB, RT, Admin, Page
from netmon.api.errors import validation_error
from netmon.services import audit as audit_svc
from netmon.services.features import feature_disabled
from netmon.services.serialize import page

router = APIRouter(dependencies=[Depends(feature_disabled("audit"))])


def _filters(
    since: datetime | None = None,
    until: datetime | None = None,
    actor: Annotated[str | None, Query(max_length=256)] = None,
    actor_user_id: int | None = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    entity_type: Annotated[str | None, Query(max_length=32)] = None,
    entity_id: int | None = None,
    device_id: int | None = None,
    ip: Annotated[str | None, Query(max_length=45)] = None,
) -> audit_svc.AuditFilter:
    if since is not None and until is not None and since > until:
        raise validation_error(["query", "since"], "must be before until")
    return audit_svc.AuditFilter(
        since=since,
        until=until,
        actor=actor or None,
        actor_user_id=actor_user_id,
        action=action or None,
        entity_type=entity_type or None,
        entity_id=entity_id,
        device_id=device_id,
        ip=ip or None,
    )


Filters = Annotated[audit_svc.AuditFilter, Depends(_filters)]


@router.get("/api/audit")
async def list_audit(_: Admin, session: DB, pg: Page, flt: Filters) -> dict:
    items, total = await audit_svc.list_page(session, flt, pg.offset, pg.page_size)
    return page(items, total, pg.page, pg.page_size)


def _stamp(dt: datetime | None, fallback: str) -> str:
    return dt.strftime("%Y%m%d") if dt is not None else fallback


@router.get("/api/audit/export.csv")
async def export_audit(_: Admin, rt: RT, session: DB, flt: Filters) -> StreamingResponse:
    limit = audit_svc.EXPORT_MAX_ROWS
    truncated = await audit_svc.count_matching(session, flt, limit + 1) > limit
    await session.close()
    from netmon.services.serialize import now_utc

    today = now_utc()
    first = flt.since
    if first is None:
        from sqlalchemy import text

        where, p = flt.where()
        async with rt.sessionmaker() as s:
            first = (await s.execute(text(f"SELECT min(time) FROM audit_log{where}"), p)).scalar()
    name = (
        f"netmon-audit-{_stamp(first, today.strftime('%Y%m%d'))}-{_stamp(flt.until or today, '')}"
    )
    headers = {"Content-Disposition": f'attachment; filename="{name}.csv"'}
    if truncated:
        headers["X-Netmon-Truncated"] = "true"
    return StreamingResponse(
        audit_svc.export_rows(rt, flt, limit),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )
