from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from sqlalchemy import func, select

from netmon.api.common import csv_list
from netmon.api.deps import DB, Page, Paging, Viewer
from netmon.api.errors import validation_error
from netmon.models import Event
from netmon.services.serialize import event_out, page
from netmon.services.status import load_inventory

router = APIRouter()


def _dt(value: str, loc: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "+"))
    except ValueError as exc:
        raise validation_error(["query", loc], "must be an ISO-8601 timestamp") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def list_events_impl(
    session: DB,
    pg: Paging,
    *,
    sensor_id: int | None = None,
    device_id: int | None = None,
    group_id: int | None = None,
    kind: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    stmt = select(Event)
    if sensor_id is not None:
        stmt = stmt.where(Event.sensor_id == sensor_id)
    if device_id is not None:
        stmt = stmt.where(Event.device_id == device_id)
    if group_id is not None:
        inv = await load_inventory(session)
        gids = inv.descendants(group_id) if group_id in inv.groups else [group_id]
        stmt = stmt.where(Event.group_id.in_(gids))
    kinds = csv_list(kind)
    if kinds:
        stmt = stmt.where(Event.kind.in_(kinds))
    statuses = csv_list(status)
    if statuses:
        stmt = stmt.where(Event.new_status.in_(statuses))
    if since:
        stmt = stmt.where(Event.time >= _dt(since, "since"))
    if until:
        stmt = stmt.where(Event.time <= _dt(until, "until"))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await session.execute(
            stmt.order_by(Event.time.desc(), Event.id.desc()).offset(pg.offset).limit(pg.page_size)
        )
    ).scalars()
    return page([event_out(e) for e in rows], total, pg.page, pg.page_size)


@router.get("/api/events")
async def list_events(
    _: Viewer,
    session: DB,
    pg: Page,
    sensor_id: int | None = None,
    device_id: int | None = None,
    group_id: int | None = None,
    kind: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    return await list_events_impl(
        session,
        pg,
        sensor_id=sensor_id,
        device_id=device_id,
        group_id=group_id,
        kind=kind,
        status=status,
        since=since,
        until=until,
    )
