"""[3] Config backup API (api.md "Config backup", feature `config_backup`).

Viewers see status and version metadata; content, diff, search and export are admin-only and
audited. `POST /api/devices/{id}/ssh/test` stays available while the feature is OFF."""

from __future__ import annotations

import asyncio
import io
import re
import time
import zipfile
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from netmon.api.deps import DB, RT, Admin, Audit, Page, Viewer
from netmon.api.errors import ApiError, not_found, validation_error
from netmon.configbackup import credentials, drivers, normalize, service, sysoid
from netmon.configbackup.session import HostKeyInfo, SessionError, run_session
from netmon.ingest.query import time_range
from netmon.models import Device
from netmon.schemas.phase3 import BackupRunIn, HostKeyAcceptIn, SshTestIn
from netmon.services import audit
from netmon.services import settings as settings_svc
from netmon.services.features import feature_disabled
from netmon.services.serialize import page
from netmon.services.status import load_inventory

router = APIRouter()
FEATURE = [Depends(feature_disabled("config_backup"))]
VIEW_AUDIT_EVERY_S = 600.0
SEARCH_MAX_DEVICES = 200
SEARCH_MAX_LINES = 20
SEARCH_MAX_DAYS = 31
EXPORT_MAX_DEVICES = 2000
PREVIEW_LINES = 20
_view_audited: dict[tuple[int, int], float] = {}


def _runner(rt: Any) -> Any:
    return getattr(rt, "configbackup", None)


async def _ctx(rt: RT, session: DB, device_id: int) -> service.DeviceCtx:
    ctxs = await service.load_contexts(session, rt.secret_key, [device_id])
    if not ctxs:
        raise not_found("Device")
    return ctxs[0]


def _running(rt: Any, device_id: int) -> bool:
    r = _runner(rt)
    return bool(r is not None and r.is_running(device_id))


def _device_entity(c: service.DeviceCtx) -> audit.Entity:
    return audit.entity("device", c.id, c.name, device_id=c.id)


def _local_stamp(when: Any, tz: str | None) -> str:
    try:
        zone = ZoneInfo(tz or "UTC")
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("UTC")
    return when.astimezone(zone).strftime("%Y%m%d-%H%M")


def safe_name(name: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "device"
    return out[:100]


# ================================================================ lists / status
@router.get("/api/config-backups", dependencies=FEATURE)
async def list_backups(
    _: Viewer,
    rt: RT,
    session: DB,
    pg: Page,
    group_id: int | None = None,
    result: str | None = Query(default=None, max_length=16),
    q: str | None = Query(default=None, max_length=255),
) -> dict:
    ctxs = await service.load_contexts(session, rt.secret_key)
    if group_id is not None:
        inv = await load_inventory(session)
        groups = set(inv.descendants(group_id)) if group_id in inv.groups else set()
        ctxs = [c for c in ctxs if c.group_id in groups]
    ctxs = [c for c in ctxs if c.mode != "off" or c.versions > 0]
    if result:
        ctxs = [c for c in ctxs if (c.status or {}).get("last_result") == result]
    if q:
        ql = q.casefold()
        ctxs = [c for c in ctxs if ql in c.name.casefold()]
    total = len(ctxs)
    sliced = ctxs[pg.offset : pg.offset + pg.page_size]
    return page(
        [service.status_out(c, _running(rt, c.id)) for c in sliced], total, pg.page, pg.page_size
    )


@router.get("/api/config-backups/status", dependencies=FEATURE)
async def runner_status(_: Viewer, rt: RT, session: DB) -> dict:
    lib = drivers.library()
    ctxs = await service.load_contexts(session, rt.secret_key, with_status=False)
    r = _runner(rt)
    cfg = await settings_svc.get_all(session)
    return {
        "running": r.running_count if r else 0,
        "queued": r.queued_count if r else 0,
        "concurrency": int(cfg["config_backup_concurrency"]),
        "next_schedule_at": _iso(r.next_schedule_at) if r and r.enabled else None,
        "last_schedule_at": _iso(r.last_schedule_at) if r else None,
        "eligible_devices": sum(1 for c in ctxs if c.eligible[0]),
        "drivers": [d.out() for d in drivers.ordered(lib.drivers)],
        "driver_errors": list(lib.errors),
    }


def _iso(v: Any) -> str | None:
    from netmon.services.serialize import iso

    return iso(v)


@router.post("/api/config-backups/run", dependencies=FEATURE, status_code=202)
async def run_backups(body: BackupRunIn, _: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    ctxs = await service.load_contexts(session, rt.secret_key, body.device_ids, with_status=False)
    ids = [c.id for c in ctxs if c.eligible[0]]
    r = _runner(rt)
    queued, merged = r.run_now(ids, "manual") if r is not None else (0, [])
    await audit.record(
        session,
        ctx,
        "config.backup_all",
        None,
        {"device_ids": [None, body.device_ids], "queued": [None, queued + len(merged)]},
    )
    await session.commit()
    return {"queued": queued + len(merged)}


# ================================================================ search / export
def _match_lines(content: str, rx: re.Pattern[str]) -> tuple[list[dict[str, Any]], bool]:
    out: list[dict[str, Any]] = []
    for i, line in enumerate(content.split("\n"), 1):
        if rx.search(line):
            if len(out) >= SEARCH_MAX_LINES:
                return out, True
            out.append({"line": i, "text": line[:500]})
    return out, False


_LEADING_FLAGS = re.compile(r"^\(\?([a-zA-Z]+)\)")


def pg_line_regex(q: str) -> str:
    """PostgreSQL ARE for a per-line search: the whole config is one text value, so `^`/`$` must
    match at line breaks (and `.`/`[^x]` must not cross them) like the per-line Python matching:
    embedded option `n` (newline-sensitive). ARE options are only valid at the very start, so a
    leading `(?flags)` of the user's pattern is merged (user flags last, they win)."""
    m = _LEADING_FLAGS.match(q)
    if m:
        return f"(?n{m.group(1)}){q[m.end() :]}"
    return f"(?n){q}"


def like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/api/config-backups/search", dependencies=FEATURE)
async def search(
    _: Admin,
    rt: RT,
    session: DB,
    q: str = Query(min_length=2, max_length=200),
    regex: bool = False,
    group_id: int | None = None,
    all_versions: bool = False,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    if regex:
        try:
            rx = re.compile(q, re.IGNORECASE | re.MULTILINE)
        except re.error as exc:
            raise validation_error(["query", "q"], f"invalid regular expression: {exc}") from None
        cond, param = "v.content ~* :q", pg_line_regex(q)
    else:
        rx = re.compile(re.escape(q), re.IGNORECASE)
        cond, param = "v.content ILIKE :q ESCAPE '\\'", f"%{like_escape(q)}%"
    params: dict[str, Any] = {"q": param, "lim": SEARCH_MAX_DEVICES + 1}
    where = [cond]
    if group_id is not None:
        inv = await load_inventory(session)
        params["gids"] = inv.descendants(group_id) if group_id in inv.groups else []
        where.append("d.group_id = ANY(:gids)")
    if all_versions:
        s, u = time_range(since, until, rt.clock.now(), timedelta(days=SEARCH_MAX_DAYS))
        if u - s > timedelta(days=SEARCH_MAX_DAYS, seconds=1):
            raise validation_error(["query", "since"], f"range at most {SEARCH_MAX_DAYS} days")
        params.update(s=s, u=u)
        where.append("v.collected_at >= :s AND v.collected_at < :u")
        sql = (
            "SELECT DISTINCT ON (v.device_id) v.id, v.device_id, v.collected_at, v.content, "
            "d.name FROM config_versions v JOIN devices d ON d.id = v.device_id "
            f"WHERE {' AND '.join(where)} ORDER BY v.device_id, v.collected_at DESC LIMIT :lim"
        )
    else:
        where.append("v.is_latest")
        sql = (
            "SELECT v.id, v.device_id, v.collected_at, v.content, d.name FROM config_versions v "
            f"JOIN devices d ON d.id = v.device_id WHERE {' AND '.join(where)} "
            "ORDER BY d.name, d.id LIMIT :lim"
        )
    try:
        rows = (await session.execute(text(sql), params)).all()  # noqa: S608
    except Exception as exc:  # noqa: BLE001 - PostgreSQL rejects the regex
        await session.rollback()
        raise validation_error(["query", "q"], "invalid regular expression") from exc
    await session.close()
    truncated = len(rows) > SEARCH_MAX_DEVICES
    rows = sorted(rows[:SEARCH_MAX_DEVICES], key=lambda r: (r.name, r.device_id))

    def build() -> list[dict[str, Any]]:
        items = []
        for r in rows:
            matches, more = _match_lines(r.content, rx)
            if not matches:  # matched the whole text only (e.g. an explicit `\n`): no line
                continue
            items.append(
                {
                    "device_id": r.device_id,
                    "device_name": r.name,
                    "version_id": int(r.id),
                    "collected_at": _iso(r.collected_at),
                    "matches": matches,
                    "more": more,
                }
            )
        return items

    return {"items": await asyncio.to_thread(build), "truncated": truncated}


@router.get("/api/config-backups/export.zip", dependencies=FEATURE)
async def export_zip(
    _: Admin, rt: RT, session: DB, ctx: Audit, group_id: int | None = None
) -> StreamingResponse:
    params: dict[str, Any] = {"lim": EXPORT_MAX_DEVICES}
    where = "v.is_latest"
    if group_id is not None:
        inv = await load_inventory(session)
        params["gids"] = inv.descendants(group_id) if group_id in inv.groups else []
        where += " AND d.group_id = ANY(:gids)"
    rows = (
        await session.execute(
            text(
                "SELECT d.id, d.name, v.content FROM config_versions v JOIN devices d "
                f"ON d.id = v.device_id WHERE {where} ORDER BY d.name, d.id LIMIT :lim"  # noqa: S608
            ),
            params,
        )
    ).all()
    tz = await settings_svc.get(session, "timezone")
    await audit.record(
        session,
        ctx,
        "config.export",
        audit.entity("config_backup", None, "all"),
        {"devices": [None, len(rows)], "group_id": [None, group_id]},
    )
    await session.commit()

    def build() -> bytes:
        buf = io.BytesIO()
        used: set[str] = set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for r in rows:
                name = safe_name(r.name)
                if name.lower() in used:
                    name = f"{name}-{r.id}"
                used.add(name.lower())
                zf.writestr(f"{name}.cfg", r.content)
        return buf.getvalue()

    data = await asyncio.to_thread(build)
    stamp = _local_stamp(rt.clock.now(), tz)

    async def chunks() -> Any:
        for i in range(0, len(data), 65536):
            yield data[i : i + 65536]

    return StreamingResponse(
        chunks(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="netmon-configs-{stamp}.zip"'},
    )


# ================================================================ per device
@router.get("/api/devices/{device_id}/config", dependencies=FEATURE)
async def device_status(device_id: int, _: Viewer, rt: RT, session: DB) -> dict:
    c = await _ctx(rt, session, device_id)
    return service.status_out(c, _running(rt, c.id))


@router.post("/api/devices/{device_id}/config/backup", dependencies=FEATURE, status_code=202)
async def backup_now(device_id: int, _: Admin, rt: RT, session: DB) -> dict:
    c = await _ctx(rt, session, device_id)
    ok, reason = c.eligible
    if not ok:
        raise ApiError(
            400,
            "not_eligible",
            f"This device cannot be backed up ({reason})",
            [{"reason": reason}],
        )
    r = _runner(rt)
    if r is None:
        raise ApiError(409, "feature_disabled", "The feature config_backup is turned off")
    queued, merged = r.run_now([c.id], "manual")
    if merged:
        raise ApiError(
            409,
            "backup_running",
            "A backup of this device is already running; another one follows",
            [{"device_id": c.id}],
        )
    return {"queued": queued}


@router.get("/api/devices/{device_id}/config/versions", dependencies=FEATURE)
async def list_versions(device_id: int, _: Viewer, session: DB, pg: Page) -> dict:
    if await session.get(Device, device_id) is None:
        raise not_found("Device")
    nums = await service.version_numbers(session, device_id)
    rows = (
        (
            await session.execute(
                text(
                    f"SELECT {service.VERSION_COLUMNS} FROM config_versions "  # noqa: S608
                    "WHERE device_id = :d ORDER BY collected_at DESC, id DESC "
                    "LIMIT :lim OFFSET :off"
                ),
                {"d": device_id, "lim": pg.page_size, "off": pg.offset},
            )
        )
        .mappings()
        .all()
    )
    return page(
        [service.version_out(dict(r), nums.get(int(r["id"]), 0)) for r in rows],
        len(nums),
        pg.page,
        pg.page_size,
    )


async def _version(session: DB, device_id: int, version_id: int) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text(
                    f"SELECT {service.VERSION_COLUMNS}, content FROM config_versions "  # noqa: S608
                    "WHERE id = :v AND device_id = :d"
                ),
                {"v": version_id, "d": device_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Configuration version")
    return dict(row)


@router.get("/api/devices/{device_id}/config/versions/{version_id}", dependencies=FEATURE)
async def get_version(
    device_id: int, version_id: int, user: Admin, rt: RT, session: DB, ctx: Audit
) -> dict:
    d = await session.get(Device, device_id)
    if d is None:
        raise not_found("Device")
    row = await _version(session, device_id, version_id)
    nums = await service.version_numbers(session, device_id)
    key = (int(user.id), device_id)
    now = time.monotonic()
    if now - _view_audited.get(key, -1e9) >= VIEW_AUDIT_EVERY_S:
        _view_audited[key] = now
        if len(_view_audited) > 10000:
            _view_audited.clear()
        await audit.record(
            session,
            ctx,
            "config.view",
            audit.entity("device", d.id, d.name, device_id=d.id),
            {"version_id": [None, version_id]},
        )
        await session.commit()
    out = service.version_out(row, nums.get(version_id, 0))
    out["content"] = row["content"]
    return out


@router.get("/api/devices/{device_id}/config/versions/{version_id}/download", dependencies=FEATURE)
async def download_version(
    device_id: int, version_id: int, _: Admin, rt: RT, session: DB, ctx: Audit
) -> Response:
    d = await session.get(Device, device_id)
    if d is None:
        raise not_found("Device")
    row = await _version(session, device_id, version_id)
    tz = await settings_svc.get(session, "timezone")
    await audit.record(
        session,
        ctx,
        "config.export",
        audit.entity("device", d.id, d.name, device_id=d.id),
        {"version_id": [None, version_id]},
    )
    await session.commit()
    fname = f"{safe_name(d.name)}_{_local_stamp(row['collected_at'], tz)}.cfg"
    return Response(
        content=row["content"].encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/api/devices/{device_id}/config/diff", dependencies=FEATURE)
async def diff_versions(
    device_id: int,
    _: Admin,
    session: DB,
    from_: int = Query(alias="from"),
    to: int | None = None,
) -> dict:
    if await session.get(Device, device_id) is None:
        raise not_found("Device")
    a = await _version(session, device_id, from_)
    if to is None:
        latest = (
            await session.execute(
                text("SELECT id FROM config_versions WHERE device_id = :d AND is_latest"),
                {"d": device_id},
            )
        ).scalar()
        if latest is None:
            raise not_found("Configuration version")
        to = int(latest)
    b = await _version(session, device_id, to)
    nums = await service.version_numbers(session, device_id)
    await session.close()
    va, vb = service.version_out(a, nums.get(from_, 0)), service.version_out(b, nums.get(to, 0))
    text_, added, removed, truncated = await asyncio.to_thread(
        normalize.diff,
        a["content"],
        b["content"],
        from_name=f"version {va['version_no']}",
        to_name=f"version {vb['version_no']}",
    )
    return {
        "from": va,
        "to": vb,
        "diff": text_,
        "added": added,
        "removed": removed,
        "truncated": truncated,
    }


@router.post("/api/devices/{device_id}/config/host-key/accept", dependencies=FEATURE)
async def accept_host_key(
    device_id: int, body: HostKeyAcceptIn, user: Admin, rt: RT, session: DB, ctx: Audit
) -> dict:
    d = await session.get(Device, device_id)
    if d is None:
        raise not_found("Device")
    row = (
        (
            await session.execute(
                text("SELECT * FROM ssh_host_keys WHERE device_id = :d FOR UPDATE"),
                {"d": device_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None or row["state"] != "changed" or not row["presented_fingerprint"]:
        raise ApiError(400, "no_host_key_change", "The host key of this device did not change")
    if body.fingerprint.strip() != row["presented_fingerprint"]:
        raise ApiError(
            409,
            "host_key_mismatch",
            "The presented host key changed again; check the new fingerprint",
            [{"presented_fingerprint": row["presented_fingerprint"]}],
        )
    now = rt.clock.now()
    new = (
        (
            await session.execute(
                text(
                    "UPDATE ssh_host_keys SET algorithm = presented_algorithm, "
                    "fingerprint = presented_fingerprint, public_key = presented_public_key, "
                    "state = 'pinned', presented_algorithm = NULL, presented_fingerprint = NULL, "
                    "presented_public_key = NULL, pinned_at = :now, changed_at = NULL, "
                    "accepted_by = :by WHERE device_id = :d RETURNING *"
                ),
                {"d": device_id, "now": now, "by": user.username[:64]},
            )
        )
        .mappings()
        .one()
    )
    await audit.record(
        session,
        ctx,
        "config.host_key_accept",
        audit.entity("device", d.id, d.name, device_id=d.id),
        {"fingerprint": [row["fingerprint"], row["presented_fingerprint"]]},
    )
    await session.commit()
    r = _runner(rt)
    if r is not None:
        await r.host_key_resolved(device_id)
    rt.config_changed("config_backup", device_id, "updated")
    return service.host_key_out(dict(new)) or {}


@router.delete("/api/devices/{device_id}/config/host-key", dependencies=FEATURE, status_code=204)
async def reset_host_key(device_id: int, _: Admin, rt: RT, session: DB, ctx: Audit) -> Response:
    d = await session.get(Device, device_id)
    if d is None:
        raise not_found("Device")
    old = (
        await session.execute(
            text("DELETE FROM ssh_host_keys WHERE device_id = :d RETURNING fingerprint"),
            {"d": device_id},
        )
    ).scalar()
    await audit.record(
        session,
        ctx,
        "config.host_key_reset",
        audit.entity("device", d.id, d.name, device_id=d.id),
        {"fingerprint": [old, None]},
    )
    await session.commit()
    r = _runner(rt)
    if r is not None:
        await r.host_key_resolved(device_id)
    rt.config_changed("config_backup", device_id, "updated")
    return Response(status_code=204)


# ================================================================ SSH test (also while OFF)
def _ssh_error(code: str, message: str, step: str, steps: list[dict] | None = None) -> ApiError:
    return ApiError(
        502,
        "ssh_error",
        f"{code}: {message}"[:500],
        [{"error": code, "step": step, "steps": steps or []}],
    )


@router.post("/api/devices/{device_id}/ssh/test")
async def ssh_test(device_id: int, body: SshTestIn, _: Admin, rt: RT, session: DB) -> dict:
    c = await _ctx(rt, session, device_id)
    if c.driver is None and not c.driver_override and body.driver is None:
        # "Automatic" without a stored identity: read the sysObjectID over SNMP, then re-match
        await sysoid.probe(rt, [device_id])
        c = await _ctx(rt, session, device_id)
    stored = c.ssh
    if body.ssh_credentials is not None:
        stored = credentials.merge(body.ssh_credentials, stored, rt.secret_key)
    if stored is None:
        raise _ssh_error("no_credentials", "No SSH credentials for this device", "auth")
    try:
        creds = credentials.decrypt(stored, rt.secret_key)
    except credentials.CredentialsUnreadable:
        raise _ssh_error(
            "no_credentials", "The SSH credentials cannot be decrypted", "auth"
        ) from None
    driver_id = body.driver or c.driver
    if body.driver is not None and drivers.get(body.driver) is None:
        raise validation_error(["body", "driver"], "unknown driver")
    driver = drivers.get(driver_id)
    if driver is None:
        raise _ssh_error("no_driver", "No config backup driver matches this device", "prompt")
    feature_on = rt.features.enabled("config_backup")
    cfg_timeout = float(await settings_svc.get(session, "config_backup_timeout_s"))
    await session.close()
    pin: dict[str, HostKeyInfo] = {}

    def check(info: HostKeyInfo) -> bool:
        decision = normalize.host_key_decision(c.host_key, info.fingerprint)
        if decision == "pin":
            pin["key"] = info
            return True
        return decision == "accept"

    try:
        result = await run_session(c.host, creds, driver, check, total_timeout=cfg_timeout)
    except SessionError as exc:
        raise _ssh_error(exc.code, exc.message, exc.step, exc.steps) from None
    if "key" in pin and feature_on:
        r = _runner(rt)
        if r is not None:
            await r._pin(c.id, pin["key"])  # noqa: SLF001
    mask = True
    norm = await asyncio.to_thread(normalize.normalize, result.outputs, driver, mask)
    preview = "\n".join(norm.text.split("\n")[:PREVIEW_LINES])
    return {
        "ok": True,
        "steps": result.steps,
        "driver": driver.id,
        "output_preview": preview,
    }
