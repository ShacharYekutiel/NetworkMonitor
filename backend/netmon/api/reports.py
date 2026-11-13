"""[3] Reports API (api.md § [3] Reports, feature `reports`)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text

from netmon.api.deps import DB, RT, Admin, Audit, Page, Viewer
from netmon.api.errors import ApiError, not_found
from netmon.reports import compute
from netmon.reports.data import SECTIONS
from netmon.reports.scheduler import (
    REPORT_COLUMNS,
    RUN_COLUMNS,
    ReportBusy,
    ReportService,
    report_dict,
    run_out,
)
from netmon.schemas import _email
from netmon.services import audit, settings_p3c
from netmon.services.features import require
from netmon.services.security import DOCUMENT_CSP
from netmon.services.serialize import iso, page


def _feature(request: Request) -> None:
    require(request.app.state.rt, "reports")


router = APIRouter(dependencies=[Depends(_feature)])

ReportSection = Literal[
    "availability",
    "top_alerts",
    "flapping_ports",
    "port_errors",
    "config_changes",
    "new_devices",
    "top_talkers",
]
HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
CHANNEL_TYPES = ("email", "teams", "telegram", "webhook")


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    language: Literal["en", "he"] | None = None
    scope_group_ids: list[int] | None = Field(default=None, max_length=500)
    scope_device_ids: list[int] | None = Field(default=None, max_length=5000)
    sections: list[ReportSection] | None = Field(default=None, min_length=1, max_length=7)
    schedule: Literal["weekly", "monthly", "none"] | None = None
    weekday: int | None = Field(default=None, ge=1, le=7)
    month_day: int | None = Field(default=None, ge=1, le=28)
    send_time: str | None = None
    email_to: list[str] | None = Field(default=None, max_length=50)
    channel_ids: list[int] | None = Field(default=None, max_length=50)
    ai_summary: bool | None = None  # [3b] § 14.8.10

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name is required")
        return v

    @field_validator("sections")
    @classmethod
    def _sections(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(set(v)) != len(v):
            raise ValueError("sections must be distinct")
        return v

    @field_validator("send_time")
    @classmethod
    def _time(cls, v: str | None) -> str | None:
        if v is not None and not HHMM.match(v):
            raise ValueError("HH:MM")
        return v

    @field_validator("email_to")
    @classmethod
    def _emails(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out: list[str] = []
        for e in v:
            e2 = _email(e)
            if e2 and e2.lower() not in {x.lower() for x in out}:
                out.append(e2)
        return out

    @field_validator("scope_group_ids", "scope_device_ids", "channel_ids")
    @classmethod
    def _ids(cls, v: list[int] | None) -> list[int] | None:
        return list(dict.fromkeys(v)) if v is not None else v


def _merge(current: dict[str, Any], body: ReportIn) -> dict[str, Any]:
    out = dict(current)
    for k in body.model_fields_set:
        val = getattr(body, k)
        if val is None and k in (
            "name",
            "enabled",
            "language",
            "sections",
            "schedule",
            "ai_summary",
        ):
            continue
        if val is None and k in ("scope_group_ids", "scope_device_ids", "email_to", "channel_ids"):
            val = []
        out[k] = val
    sched = out.get("schedule") or "none"
    if sched != "weekly":
        out["weekday"] = None
    if sched != "monthly":
        out["month_day"] = None
    if sched == "none":
        out["send_time"] = None
    return out


def _schedule_errors(r: dict[str, Any]) -> list[dict[str, Any]]:
    errs = []
    if not r.get("name"):
        errs.append({"loc": ["body", "name"], "msg": "name is required"})
    if not r.get("sections"):
        errs.append({"loc": ["body", "sections"], "msg": "at least one section"})
    if r["schedule"] == "weekly" and r.get("weekday") is None:
        errs.append({"loc": ["body", "weekday"], "msg": "required for a weekly report"})
    if r["schedule"] == "monthly" and r.get("month_day") is None:
        errs.append({"loc": ["body", "month_day"], "msg": "required for a monthly report"})
    if r["schedule"] in ("weekly", "monthly") and not r.get("send_time"):
        errs.append({"loc": ["body", "send_time"], "msg": "required for a scheduled report"})
    return errs


async def _check_refs(session: DB, r: dict[str, Any]) -> None:
    errs: list[dict[str, Any]] = []
    for key, table in (("scope_group_ids", "groups"), ("scope_device_ids", "devices")):
        ids = r.get(key) or []
        if ids:
            found = set(
                (
                    await session.execute(
                        text(f"SELECT id FROM {table} WHERE id = ANY(:i)"), {"i": ids}
                    )
                ).scalars()
            )
            missing = [i for i in ids if i not in found]
            if missing:
                errs.append({"loc": ["body", key], "msg": f"unknown ids {missing[:10]}"})
    cids = r.get("channel_ids") or []
    if cids:
        rows = {
            c.id: c.type
            for c in await session.execute(
                text("SELECT id, type FROM notification_channels WHERE id = ANY(:i)"), {"i": cids}
            )
        }
        bad = [c for c in cids if rows.get(c) not in CHANNEL_TYPES]
        if bad:
            errs.append({"loc": ["body", "channel_ids"], "msg": f"unknown channels {bad[:10]}"})
    if errs:
        raise ApiError(422, "validation_error", errs[0]["msg"], errs)


async def _last_runs(session: DB, ids: list[int]) -> dict[int, Any]:
    if not ids:
        return {}
    rows = await session.execute(
        text(
            "SELECT r.id AS report_id, rr.id, rr.status, rr.finished_at FROM reports r "
            "JOIN report_runs rr ON rr.id = r.last_run_id WHERE r.id = ANY(:i)"
        ),
        {"i": ids},
    )
    return {
        r.report_id: {"id": r.id, "status": r.status, "finished_at": iso(r.finished_at)}
        for r in rows
    }


def _report_out(r: Any, last: dict[int, Any]) -> dict[str, Any]:
    d = report_dict(r)
    d["next_run_at"] = iso(r.next_run_at)
    d["last_run"] = last.get(r.id)
    d["created_at"] = iso(r.created_at)
    d["updated_at"] = iso(r.updated_at)
    return d


async def _get(session: DB, report_id: int) -> Any:
    r = (
        await session.execute(
            text(f"SELECT {REPORT_COLUMNS} FROM reports WHERE id = :i"), {"i": report_id}
        )
    ).first()
    if r is None:
        raise not_found("Report")
    return r


def _svc(rt: Any) -> ReportService:
    svc = getattr(rt, "reports", None)
    if svc is None:
        svc = rt.reports = ReportService(rt)
    return svc


def _audit_values(r: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "name",
        "enabled",
        "language",
        "scope_group_ids",
        "scope_device_ids",
        "sections",
        "schedule",
        "weekday",
        "month_day",
        "send_time",
        "email_to",
        "channel_ids",
        "ai_summary",
    )
    return {k: r.get(k) for k in keys}


async def _next_run(session: DB, rt: RT, r: dict[str, Any]) -> datetime | None:
    if not r.get("enabled", True) or r.get("schedule") == "none":
        return None
    tz = await settings_p3c.timezone(session)
    return compute.next_run(
        r["schedule"], r.get("weekday"), r.get("month_day"), r.get("send_time"), tz, rt.clock.now()
    )


@router.get("/api/reports")
async def list_reports(_: Viewer, session: DB) -> dict:
    rows = (
        await session.execute(
            text(f"SELECT {REPORT_COLUMNS} FROM reports ORDER BY lower(name), id LIMIT 100")
        )
    ).all()
    last = await _last_runs(session, [r.id for r in rows])
    return {"items": [_report_out(r, last) for r in rows]}


INSERT_SQL = """
INSERT INTO reports (name, enabled, language, scope_group_ids, scope_device_ids, sections,
    schedule, weekday, month_day, send_time, email_to, channel_ids, next_run_at, ai_summary)
VALUES (:name, :enabled, :language, :scope_group_ids, :scope_device_ids, :sections, :schedule,
    :weekday, :month_day, :send_time, :email_to, :channel_ids, :next_run_at, :ai_summary)
RETURNING id
"""


async def _name_taken(session: DB, name: str, exclude: int | None = None) -> bool:
    row = (
        await session.execute(
            text("SELECT id FROM reports WHERE lower(name) = lower(:n) AND id <> :x"),
            {"n": name, "x": exclude or 0},
        )
    ).first()
    return row is not None


@router.post("/api/reports", status_code=201)
async def create_report(body: ReportIn, _: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    defaults = {
        "name": None,
        "enabled": True,
        "language": "en",
        "scope_group_ids": [],
        "scope_device_ids": [],
        "sections": None,
        "schedule": "none",
        "weekday": None,
        "month_day": None,
        "send_time": None,
        "email_to": [],
        "channel_ids": [],
        "ai_summary": False,
    }
    r = _merge(defaults, body)
    errs = _schedule_errors(r)
    if errs:
        raise ApiError(422, "validation_error", errs[0]["msg"], errs)
    await _check_refs(session, r)
    if await _name_taken(session, r["name"]):
        raise ApiError(
            409, "conflict", "A report with this name already exists", [{"field": "name"}]
        )
    r["next_run_at"] = await _next_run(session, rt, r)
    rid = (await session.execute(text(INSERT_SQL), r)).scalar_one()
    await audit.record(
        session,
        ctx,
        "report.create",
        audit.entity("report", rid, r["name"]),
        audit.created(_audit_values(r)),
    )
    await session.commit()
    rt.config_changed("report", rid, "created")
    _svc(rt).wake()
    row = await _get(session, rid)
    return _report_out(row, {})


@router.get("/api/reports/runs")
async def list_runs(
    _: Viewer,
    session: DB,
    pg: Page,
    report_id: int | None = Query(default=None),
) -> dict:
    where = "WHERE report_id = :r" if report_id is not None else ""
    params: dict[str, Any] = {"r": report_id, "lim": pg.page_size, "off": pg.offset}
    total = (
        await session.execute(text(f"SELECT count(*) FROM report_runs {where}"), params)
    ).scalar_one()
    rows = (
        await session.execute(
            text(
                f"SELECT {RUN_COLUMNS} FROM report_runs {where} "
                "ORDER BY started_at DESC, id DESC LIMIT :lim OFFSET :off"
            ),
            params,
        )
    ).all()
    return page([run_out(r) for r in rows], total, pg.page, pg.page_size)


async def _run_row(session: DB, run_id: int, extra: str = "") -> Any:
    r = (
        await session.execute(
            text(f"SELECT {RUN_COLUMNS}{extra} FROM report_runs WHERE id = :i"), {"i": run_id}
        )
    ).first()
    if r is None:
        raise not_found("Report run")
    return r


@router.get("/api/reports/runs/{run_id}")
async def get_run(run_id: int, _: Viewer, session: DB) -> dict:
    r = await _run_row(session, run_id, ", data")
    out = run_out(r)
    out["data"] = r.data if isinstance(r.data, dict) else json.loads(r.data or "{}")
    return out


@router.get("/api/reports/runs/{run_id}/html")
async def get_run_html(run_id: int, _: Viewer, session: DB) -> Response:
    r = await _run_row(session, run_id, ", html")
    if not r.html:
        raise ApiError(404, "not_found", "The report has not been rendered (yet)")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", r.report_name).strip("-")[:60] or "report"
    day = r.period_start.astimezone(UTC).strftime("%Y%m%d")
    fname = f"netmon-report-{safe}-{day}.html"
    return HTMLResponse(
        r.html,
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"; '
            f"filename*=UTF-8''{quote(fname)}",
            "Cache-Control": "no-store",
            # [1.4.1] a stored document: inline styles only, never scripts
            "Content-Security-Policy": DOCUMENT_CSP,
        },
    )


class ResendIn(BaseModel):
    targets: list[str] | None = Field(default=None, max_length=100)


@router.post("/api/reports/runs/{run_id}/resend", status_code=202)
async def resend(
    run_id: int, _: Admin, rt: RT, session: DB, ctx: Audit, body: ResendIn | None = None
) -> dict:
    r = await _run_row(session, run_id, ", html")
    if r.status == "running":
        raise ApiError(409, "report_running", "This report is still running")
    deliveries = r.deliveries if isinstance(r.deliveries, list) else json.loads(r.deliveries)
    targets = (body.targets if body and body.targets is not None else None) or [
        d["target"] for d in deliveries if not d.get("ok")
    ]
    for t in targets:
        if not re.match(r"^(email:.+@.+|channel:\d+)$", t):
            raise ApiError(422, "validation_error", f"invalid target {t!r}")
    if not targets:
        raise ApiError(422, "validation_error", "Nothing to resend (no failed deliveries)")
    await audit.record(
        session,
        ctx,
        "report.resend",
        audit.entity("report", r.report_id, r.report_name),
        {"targets": [None, targets]},
    )
    await session.commit()
    await _svc(rt).resend(run_id, targets)
    return run_out(r)


@router.get("/api/reports/{report_id}")
async def get_report(report_id: int, _: Viewer, session: DB) -> dict:
    r = await _get(session, report_id)
    return _report_out(r, await _last_runs(session, [report_id]))


@router.patch("/api/reports/{report_id}")
async def patch_report(
    report_id: int, body: ReportIn, _: Admin, rt: RT, session: DB, ctx: Audit
) -> dict:
    row = await _get(session, report_id)
    current = report_dict(row)
    r = _merge(current, body)
    errs = _schedule_errors(r)
    if errs:
        raise ApiError(422, "validation_error", errs[0]["msg"], errs)
    await _check_refs(session, r)
    if await _name_taken(session, r["name"], report_id):
        raise ApiError(
            409, "conflict", "A report with this name already exists", [{"field": "name"}]
        )
    r["next_run_at"] = await _next_run(session, rt, r)
    r["id"] = report_id
    await session.execute(
        text(
            "UPDATE reports SET name = :name, enabled = :enabled, language = :language, "
            "scope_group_ids = :scope_group_ids, scope_device_ids = :scope_device_ids, "
            "sections = :sections, schedule = :schedule, weekday = :weekday, "
            "month_day = :month_day, send_time = :send_time, email_to = :email_to, "
            "channel_ids = :channel_ids, next_run_at = :next_run_at, ai_summary = :ai_summary, "
            "updated_at = now() "
            "WHERE id = :id"
        ),
        r,
    )
    changes = audit.diff(_audit_values(current), _audit_values(r))
    if changes:
        await audit.record(
            session, ctx, "report.update", audit.entity("report", report_id, r["name"]), changes
        )
    await session.commit()
    rt.config_changed("report", report_id, "updated")
    _svc(rt).wake()
    return _report_out(await _get(session, report_id), await _last_runs(session, [report_id]))


@router.delete("/api/reports/{report_id}", status_code=204)
async def delete_report(report_id: int, _: Admin, rt: RT, session: DB, ctx: Audit) -> Response:
    row = await _get(session, report_id)
    await audit.record(
        session,
        ctx,
        "report.delete",
        audit.entity("report", report_id, row.name),
        audit.deleted(_audit_values(report_dict(row))),
    )
    await session.execute(text("DELETE FROM reports WHERE id = :i"), {"i": report_id})
    await session.commit()
    rt.config_changed("report", report_id, "deleted")
    return Response(status_code=204)


class RunIn(BaseModel):
    period_start: datetime | None = None
    period_end: datetime | None = None
    send: bool = False

    @model_validator(mode="after")
    def _both(self) -> RunIn:
        if (self.period_start is None) != (self.period_end is None):
            raise ValueError("period_start and period_end go together")
        for v in (self.period_start, self.period_end):
            if v is not None and v.tzinfo is None:
                raise ValueError("timestamps need a time zone (…Z)")
        return self


async def _period(session: DB, rt: RT, schedule: str, body: RunIn) -> tuple[datetime, datetime]:
    if body.period_start is not None and body.period_end is not None:
        s, e = body.period_start, body.period_end
        if e <= s:
            raise ApiError(422, "validation_error", "period_end must be after period_start")
        if e - s > timedelta(days=compute.MAX_ON_DEMAND_DAYS):
            raise ApiError(422, "validation_error", "The period is longer than 92 days")
        return s.astimezone(UTC), e.astimezone(UTC)
    tz = await settings_p3c.timezone(session)
    return compute.period_for(schedule, rt.clock.now(), tz)


@router.post("/api/reports/{report_id}/run", status_code=202)
async def run_report(
    report_id: int, user: Admin, rt: RT, session: DB, ctx: Audit, body: RunIn | None = None
) -> dict:
    body = body or RunIn()
    row = await _get(session, report_id)
    rep = report_dict(row)
    start, end = await _period(session, rt, rep["schedule"], body)
    await audit.record(
        session,
        ctx,
        "report.run",
        audit.entity("report", report_id, rep["name"]),
        {"send": [None, body.send]},
    )
    await session.commit()
    try:
        return await _svc(rt).start_run(rep, start, end, "manual", body.send, user.username)
    except ReportBusy:
        raise ApiError(409, "report_running", "This report is already running") from None


class PreviewIn(BaseModel):
    report: ReportIn
    period_start: datetime | None = None
    period_end: datetime | None = None


@router.post("/api/reports/preview", status_code=202)
async def preview(body: PreviewIn, user: Admin, rt: RT, session: DB) -> dict:
    base = {
        "id": None,
        "name": "Preview",
        "enabled": True,
        "language": "en",
        "scope_group_ids": [],
        "scope_device_ids": [],
        "sections": list(SECTIONS),
        "schedule": "none",
        "weekday": None,
        "month_day": None,
        "send_time": None,
        "email_to": [],
        "channel_ids": [],
    }
    r = _merge(base, body.report)
    r["id"] = None
    await _check_refs(session, r)
    start, end = await _period(
        session,
        rt,
        r["schedule"],
        RunIn(period_start=body.period_start, period_end=body.period_end),
    )
    await session.commit()
    return await _svc(rt).start_run(r, start, end, "manual", False, user.username)
