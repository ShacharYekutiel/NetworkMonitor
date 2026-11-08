"""[2a] Discovery: profiles, scans, results, import (all admin)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Query, Response
from sqlalchemy import func, select, text

from netmon.api.common import commit_or_conflict, csv_list
from netmon.api.deps import DB, RT, Admin, Page
from netmon.api.errors import ApiError, conflict, not_found, validation_error
from netmon.discovery import options as opts_mod
from netmon.discovery.importer import import_results
from netmon.discovery.runner import EXISTING_DEVICE_SQL, ScanRunningError
from netmon.discovery.serialize import profile_out, result_out, scan_out
from netmon.discovery.targets import TargetError, TooManyHostsError, validate_targets
from netmon.models import DiscoveryProfile, DiscoveryScan, Group, NotificationChannel, User
from netmon.schemas import (
    DiscoveryProfileCreate,
    DiscoveryProfilePatch,
    DiscoveryScanCreate,
    ImportRequest,
    ScanOptionsIn,
)
from netmon.services.serialize import now_utc, page
from netmon.templates import registry as template_registry

router = APIRouter()


def like_escape(value: str) -> str:
    """Escape LIKE wildcards so `q` matches literally (used with ESCAPE '\\')."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


DEVICE_CLASSES = (
    "router",
    "switch",
    "access_point",
    "printer",
    "nas",
    "server",
    "workstation",
    "unknown",
)


def _check_targets(targets: list[str]) -> None:
    try:
        validate_targets(targets)
    except TargetError as exc:
        raise validation_error(["body", "targets", exc.index], exc.message) from None
    except TooManyHostsError as exc:
        raise ApiError(
            422,
            "too_many_hosts",
            f"The targets expand to {exc.count} hosts (max {exc.max})",
            [{"count": exc.count, "max": exc.max}],
        ) from None


def _options(incoming: ScanOptionsIn | None, base: dict[str, Any] | None) -> dict[str, Any]:
    out = opts_mod.merge_options(incoming.given() if incoming is not None else None, base)
    if not (out["ping"] or out["snmp"] or out["tcp"]):
        raise validation_error(["body", "options"], "at least one of ping, snmp, tcp must be on")
    return out


async def _check_group(session: DB, group_id: int) -> None:
    if await session.get(Group, group_id) is None:
        raise validation_error(["body", "group_id"], "group does not exist")


async def _check_channels(session: DB, ids: list[int]) -> None:
    if not ids:
        return
    found = set(
        (
            await session.execute(
                select(NotificationChannel.id).where(NotificationChannel.id.in_(ids))
            )
        ).scalars()
    )
    missing = [i for i in ids if i not in found]
    if missing:
        raise validation_error(["body", "notify_channel_ids"], f"unknown channel id(s): {missing}")


async def _unique_profile_name(session: DB, name: str, exclude: int | None = None) -> None:
    q = select(DiscoveryProfile.id).where(func.lower(DiscoveryProfile.name) == name.lower())
    if exclude is not None:
        q = q.where(DiscoveryProfile.id != exclude)
    if (await session.execute(q)).first():
        raise conflict("name")


def _scan_running(scan_id: int | None) -> ApiError:
    return ApiError(
        409,
        "scan_running",
        "A discovery scan is already running",
        [{"scan_id": scan_id}] if scan_id is not None else None,
    )


async def _scan_json(session: DB, scan: Any) -> dict:
    username = None
    if scan.created_by is not None:
        username = (
            await session.execute(select(User.username).where(User.id == scan.created_by))
        ).scalar_one_or_none()
    return scan_out(scan, username)


async def _started_json(session: DB, runner: Any, scan_id: int) -> dict:
    """The 202 response: the scan as created (it may already have finished meanwhile)."""
    row = runner.last_started
    if row is not None and row.id == scan_id:
        return await _scan_json(session, row)
    return await _scan_json(session, await _get_scan(session, scan_id))


async def _get_scan(session: DB, scan_id: int) -> DiscoveryScan:
    scan = await session.get(DiscoveryScan, scan_id)
    if scan is None:
        raise not_found("Scan")
    await session.refresh(scan)
    return scan


def _runner(rt: RT) -> Any:
    if getattr(rt, "discovery", None) is None:
        raise ApiError(500, "internal_error", "Discovery is not available")
    return rt.discovery


# ---------------------------------------------------------------- profiles
@router.get("/api/discovery/profiles")
async def list_profiles(_: Admin, session: DB, pg: Page) -> dict:
    total = (await session.execute(select(func.count()).select_from(DiscoveryProfile))).scalar_one()
    rows = (
        await session.execute(
            select(DiscoveryProfile)
            .order_by(func.lower(DiscoveryProfile.name), DiscoveryProfile.id)
            .offset(pg.offset)
            .limit(pg.page_size)
        )
    ).scalars()
    return page([profile_out(p) for p in rows], total, pg.page, pg.page_size)


@router.post("/api/discovery/profiles", status_code=201)
async def create_profile(body: DiscoveryProfileCreate, _: Admin, rt: RT, session: DB) -> dict:
    _check_targets(body.targets)
    await _check_group(session, body.group_id)
    channel_ids = list(dict.fromkeys(body.notify_channel_ids))
    await _check_channels(session, channel_ids)
    options = _options(body.options, None)
    name = body.name.strip()
    await _unique_profile_name(session, name)
    p = DiscoveryProfile(
        name=name,
        targets=[t.strip() for t in body.targets],
        group_id=body.group_id,
        options=options,
        schedule_enabled=body.schedule_enabled,
        schedule_every_hours=body.schedule_every_hours,
        notify_new_hosts=body.notify_new_hosts,
        notify_channel_ids=channel_ids,
        next_run_at=now_utc() + timedelta(minutes=1) if body.schedule_enabled else None,
    )
    session.add(p)
    await commit_or_conflict(session)
    await session.refresh(p)
    rt.config_changed("discovery_profile", p.id, "created")
    return profile_out(p)


@router.get("/api/discovery/profiles/{profile_id}")
async def get_profile(profile_id: int, _: Admin, session: DB) -> dict:
    p = await session.get(DiscoveryProfile, profile_id)
    if p is None:
        raise not_found("Discovery profile")
    return profile_out(p)


@router.patch("/api/discovery/profiles/{profile_id}")
async def patch_profile(
    profile_id: int, body: DiscoveryProfilePatch, _: Admin, rt: RT, session: DB
) -> dict:
    p = await session.get(DiscoveryProfile, profile_id)
    if p is None:
        raise not_found("Discovery profile")
    fields = body.model_fields_set
    if "name" in fields and body.name:
        name = body.name.strip()
        await _unique_profile_name(session, name, p.id)
        p.name = name
    if "targets" in fields and body.targets is not None:
        _check_targets(body.targets)
        p.targets = [t.strip() for t in body.targets]
    if "group_id" in fields and body.group_id is not None:
        await _check_group(session, body.group_id)
        p.group_id = body.group_id
    if "notify_channel_ids" in fields and body.notify_channel_ids is not None:
        ids = list(dict.fromkeys(body.notify_channel_ids))
        await _check_channels(session, ids)
        p.notify_channel_ids = ids
    if "notify_new_hosts" in fields and body.notify_new_hosts is not None:
        p.notify_new_hosts = body.notify_new_hosts
    if "options" in fields and body.options is not None:
        p.options = _options(body.options, dict(p.options or {}))
    was_enabled, old_every = p.schedule_enabled, p.schedule_every_hours
    if "schedule_every_hours" in fields and body.schedule_every_hours is not None:
        p.schedule_every_hours = body.schedule_every_hours
    if "schedule_enabled" in fields and body.schedule_enabled is not None:
        p.schedule_enabled = body.schedule_enabled
    if not p.schedule_enabled:
        p.next_run_at = None
    elif not was_enabled or p.schedule_every_hours != old_every or p.next_run_at is None:
        p.next_run_at = now_utc() + timedelta(minutes=1)
    await commit_or_conflict(session)
    await session.refresh(p)
    rt.config_changed("discovery_profile", p.id, "updated")
    return profile_out(p)


@router.delete("/api/discovery/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: int, _: Admin, rt: RT, session: DB) -> Response:
    p = await session.get(DiscoveryProfile, profile_id)
    if p is None:
        raise not_found("Discovery profile")
    await session.delete(p)
    await session.commit()
    rt.config_changed("discovery_profile", profile_id, "deleted")
    return Response(status_code=204)


@router.post("/api/discovery/profiles/{profile_id}/run", status_code=202)
async def run_profile(profile_id: int, user: Admin, rt: RT, session: DB) -> dict:
    p = await session.get(DiscoveryProfile, profile_id)
    if p is None:
        raise not_found("Discovery profile")
    runner = _runner(rt)
    targets, group_id, options, name = list(p.targets), p.group_id, dict(p.options), p.name
    await session.commit()
    try:
        scan_id = await runner.start(
            targets=targets,
            group_id=group_id,
            options=options,
            trigger="manual",
            profile_id=profile_id,
            profile_name=name,
            created_by=user.id,
        )
    except ScanRunningError as exc:
        raise _scan_running(exc.scan_id) from None
    except TargetError as exc:
        raise validation_error(["body", "targets", exc.index], exc.message) from None
    except TooManyHostsError as exc:
        raise ApiError(
            422, "too_many_hosts", str(exc), [{"count": exc.count, "max": exc.max}]
        ) from None
    return await _started_json(session, runner, scan_id)


# ---------------------------------------------------------------- scans
@router.post("/api/discovery/scans", status_code=202)
async def create_scan(body: DiscoveryScanCreate, user: Admin, rt: RT, session: DB) -> dict:
    runner = _runner(rt)
    if runner.busy:
        raise _scan_running(runner.running_scan_id)
    _check_targets(body.targets)
    await _check_group(session, body.group_id)
    options = _options(body.options, None)
    await session.commit()
    try:
        scan_id = await runner.start(
            targets=[t.strip() for t in body.targets],
            group_id=body.group_id,
            options=options,
            trigger="manual",
            created_by=user.id,
        )
    except ScanRunningError as exc:
        raise _scan_running(exc.scan_id) from None
    return await _started_json(session, runner, scan_id)


@router.get("/api/discovery/scans")
async def list_scans(_: Admin, session: DB, pg: Page, profile_id: int | None = None) -> dict:
    stmt = select(DiscoveryScan)
    if profile_id is not None:
        stmt = stmt.where(DiscoveryScan.profile_id == profile_id)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(DiscoveryScan.id.desc()).offset(pg.offset).limit(pg.page_size)
            )
        )
        .scalars()
        .all()
    )
    return page([await _scan_json(session, s) for s in rows], total, pg.page, pg.page_size)


@router.get("/api/discovery/scans/{scan_id}")
async def get_scan(scan_id: int, _: Admin, session: DB) -> dict:
    return await _scan_json(session, await _get_scan(session, scan_id))


@router.post("/api/discovery/scans/{scan_id}/cancel")
async def cancel_scan(scan_id: int, _: Admin, rt: RT, session: DB) -> dict:
    scan = await _get_scan(session, scan_id)
    runner = _runner(rt)
    if scan.status != "running":
        raise ApiError(409, "scan_not_running", "The scan is not running")
    await session.commit()
    if not await runner.cancel(scan_id):
        raise ApiError(409, "scan_not_running", "The scan is not running")
    return await _scan_json(session, await _get_scan(session, scan_id))


@router.delete("/api/discovery/scans/{scan_id}", status_code=204)
async def delete_scan(scan_id: int, _: Admin, session: DB) -> Response:
    scan = await _get_scan(session, scan_id)
    if scan.status == "running":
        raise _scan_running(scan.id)
    await session.delete(scan)
    await session.commit()
    return Response(status_code=204)


@router.get("/api/discovery/scans/{scan_id}/results")
async def list_results(
    scan_id: int,
    _: Admin,
    session: DB,
    pg: Page,
    only_new: bool = False,
    hide_existing: bool = False,
    device_class: str | None = None,
    q: str | None = Query(default=None, max_length=255),
) -> dict:
    await _get_scan(session, scan_id)
    existing = EXISTING_DEVICE_SQL.format(ip="r.ip", hn="r.hostname")
    where = ["r.scan_id = :sid"]
    params: dict[str, Any] = {"sid": scan_id}
    if only_new:
        where.append("r.is_new")
    if hide_existing:
        where.append(f"{existing} IS NULL")
    classes = csv_list(device_class)
    if classes:
        bad = [c for c in classes if c not in DEVICE_CLASSES]
        if bad:
            raise validation_error(["query", "device_class"], f"unknown device class(es): {bad}")
        where.append("r.device_class = ANY(:classes)")
        params["classes"] = classes
    if q:
        where.append(
            "(host(r.ip) ILIKE :q ESCAPE '\\' OR r.hostname ILIKE :q ESCAPE '\\' "
            "OR r.sys_name ILIKE :q ESCAPE '\\')"
        )
        params["q"] = f"%{like_escape(q)}%"
    cond = " AND ".join(where)
    total = (
        await session.execute(
            text(f"SELECT count(*) FROM discovery_results r WHERE {cond}"), params
        )
    ).scalar_one()
    rows = (
        await session.execute(
            text(
                f"SELECT r.*, {existing} AS existing_device_id FROM discovery_results r "
                f"WHERE {cond} ORDER BY r.ip LIMIT :limit OFFSET :offset"
            ),
            {**params, "limit": pg.page_size, "offset": pg.offset},
        )
    ).all()
    return page([result_out(r) for r in rows], total, pg.page, pg.page_size)


@router.post("/api/discovery/scans/{scan_id}/import")
async def import_scan(scan_id: int, body: ImportRequest, user: Admin, rt: RT, session: DB) -> dict:
    scan = await _get_scan(session, scan_id)
    if scan.status == "running":
        raise _scan_running(scan.id)
    bad = [
        {"loc": ["body", "items", i, "template_id"], "msg": "unknown template"}
        for i, it in enumerate(body.items)
        if it.template_id not in template_registry.current().by_id
    ]
    if bad:
        raise ApiError(422, "validation_error", "Unknown template", bad)
    group_id = body.group_id if body.group_id is not None else scan.group_id
    if group_id is None or await session.get(Group, group_id) is None:
        raise validation_error(["body", "group_id"], "group does not exist")
    lang = user.language
    await session.commit()
    items = [i.model_dump() for i in body.items]
    return await import_results(rt, scan, group_id, items, lang)
