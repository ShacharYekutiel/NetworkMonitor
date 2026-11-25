from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Query
from sqlalchemy import case, func, select

from netmon.api.common import csv_list
from netmon.api.deps import DB, RT, Page, Paging, Viewer
from netmon.api.errors import validation_error
from netmon.models import Event
from netmon.services import severity as severity_svc
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
    in_maintenance: bool | None = None,
    severity: str | None = None,
    sort: str | None = None,
    noise: str | None = None,
) -> dict:
    stmt = select(Event)
    # [if-noise] "hide" (default): rows of debounced / flapping changes (and changes still in
    # their debounce hold) are left out; "include": every row; "only": just those rows
    if noise in (None, "hide"):
        stmt = stmt.where(Event.noise.is_(None))
    elif noise == "only":
        stmt = stmt.where(Event.noise.is_not(None))
    levels = severity_svc.parse_levels(csv_list(severity), ["query", "severity"])
    if levels:  # [severity] the stored severity (events written while the feature was ON)
        stmt = stmt.where(Event.severity.in_(levels))
    if in_maintenance is not None:
        stmt = stmt.where(Event.in_maintenance.is_(in_maintenance))
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
    order = [Event.time.desc(), Event.id.desc()]
    if sort == "severity":  # [severity] highest first (null last), then newest first
        order.insert(0, case(severity_svc.RANK, value=Event.severity, else_=0).desc())
    rows = (
        await session.execute(stmt.order_by(*order).offset(pg.offset).limit(pg.page_size))
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
    in_maintenance: bool | None = None,
    severity: str | None = None,
    sort: str | None = Query(default=None, pattern="^(time|severity)$"),
    noise: str | None = Query(default=None, pattern="^(hide|include|only)$"),
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
        in_maintenance=in_maintenance,
        severity=severity,
        sort=sort,
        noise=noise,
    )


@router.get("/api/events/noisy-sensors")
async def noisy_sensors(
    _: Viewer,
    rt: RT,
    session: DB,
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """[if-noise] The sensors with the most status changes in the last `hours` (every change,
    shown or hidden by the debounce / flapping protection), noisiest first."""
    from sqlalchemy import text

    from netmon.services.serialize import iso

    rows = (
        await session.execute(
            text(
                """
                SELECT e.sensor_id, count(*) AS changes,
                       count(*) FILTER (WHERE e.noise IS NULL) AS shown,
                       count(*) FILTER (WHERE e.noise IN ('debounced','flapping')) AS hidden,
                       max(e.time) AS last_change,
                       s.name AS sensor_name, s.type AS sensor_type, s.status,
                       s.noise_debounce, s.noise_flap, s.noise_debounce_polls,
                       s.noise_debounce_min_s,
                       d.id AS device_id, d.name AS device_name,
                       EXISTS (SELECT 1 FROM events f WHERE f.sensor_id = e.sensor_id
                               AND f.kind = 'sensor_flapping' AND f.flap_end IS NULL)
                           AS flapping
                FROM events e
                JOIN sensors s ON s.id = e.sensor_id
                JOIN devices d ON d.id = s.device_id
                WHERE e.kind = 'status_change' AND e.time >= now() - make_interval(hours => :h)
                GROUP BY e.sensor_id, s.id, d.id
                ORDER BY changes DESC, e.sensor_id
                LIMIT :n
                """
            ),
            {"h": hours, "n": limit},
        )
    ).all()
    total = (
        await session.execute(
            text(
                "SELECT count(*) FROM events WHERE kind = 'status_change' "
                "AND time >= now() - make_interval(hours => :h)"
            ),
            {"h": hours},
        )
    ).scalar_one()
    from netmon.services import noise_override as noise_ov

    eff = await noise_ov.effective_for_sensors(
        session, [int(r.sensor_id) for r in rows], noise_ov.glob_of(rt)
    )
    return {
        "hours": hours,
        "total_changes": int(total),
        "items": [
            {
                "sensor_id": r.sensor_id,
                "sensor_name": r.sensor_name,
                "sensor_type": r.sensor_type,
                "status": r.status,
                "device_id": r.device_id,
                "device_name": r.device_name,
                "changes": int(r.changes),
                "shown": int(r.shown),
                "hidden": int(r.hidden),
                "last_change": iso(r.last_change),
                "flapping": bool(r.flapping),
                # [if-noise] the sensor's own overrides and what applies (quick action)
                "event_noise": noise_ov.override_of(r._mapping),
                "effective_event_noise": eff[r.sensor_id].out() if r.sensor_id in eff else None,
            }
            for r in rows
        ],
    }
