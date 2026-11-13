"""[3b] AI feature endpoints (api.md § [3b] AI features): explain (SSE), incidents, config-diff
explanation and flags, syslog digests, rule drafts, template walks and drafts, and the
always-available `POST /api/device-templates/user`.

Every endpoint is admin **and** session only (API tokens → 403). The AI endpoints answer
`409 feature_disabled` (`assistant` first, then the feature's own name) and `409 feature_idle`
while a dependency is missing (`netmon.assistant.features.common.require`)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from netmon.api.deps import DB, RT, Page, request_audit_ctx, require_admin
from netmon.api.errors import ApiError, not_found
from netmon.assistant.features import common
from netmon.models import User

SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
router = APIRouter()


async def _admin_session(request: Request, user: Annotated[User, Depends(require_admin)]) -> User:
    auth = getattr(request.state, "auth", None)
    if auth is not None and auth.kind == "token":
        raise ApiError(403, "forbidden", "API tokens cannot be used here")
    return user


AdminSession = Annotated[User, Depends(_admin_session)]


def _ai(rt: Any) -> Any:
    ai = getattr(rt, "ai_features", None)
    if ai is None:
        raise ApiError(
            409, "feature_disabled", "The AI features are not available", [{"feature": "assistant"}]
        )
    return ai


# ---------------------------------------------------------------- explain (§ 14.8.1)
class ExplainIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sensor_id: int
    event_id: int | None = None
    refresh: bool = False


@router.post("/api/assistant/explain")
async def explain(
    body: ExplainIn, user: AdminSession, request: Request, rt: RT
) -> StreamingResponse:
    from netmon.assistant.features import explain as explain_mod

    stream = await explain_mod.explain_stream(
        rt, user, request_audit_ctx(request, rt), body.sensor_id, body.event_id, body.refresh
    )
    return StreamingResponse(
        stream, media_type="text/event-stream; charset=utf-8", headers=SSE_HEADERS
    )


# ---------------------------------------------------------------- incidents (§ 14.8.2)
@router.get("/api/incidents")
async def list_incidents(
    _: AdminSession,
    rt: RT,
    session: DB,
    pg: Page,
    state: Annotated[list[Literal["open", "resolved", "stale"]] | None, Query()] = None,
    since: datetime | None = None,
) -> dict:
    from netmon.assistant.features.correlate import (
        INC_COLS,
        device_names,
        incident_counts,
        incident_out,
    )
    from netmon.services.serialize import page

    await common.require(rt, "ai_correlation", session)
    where = ["true"]
    params: dict[str, Any] = {}
    if state:
        where.append("state = ANY(:st)")
        params["st"] = list(state)
    if since is not None:
        where.append("last_change_at >= :since")
        params["since"] = since
    w = " AND ".join(where)
    total = (
        await session.execute(text(f"SELECT count(*) FROM assistant_incidents WHERE {w}"), params)
    ).scalar()
    rows = (
        await session.execute(
            text(
                f"SELECT {INC_COLS} FROM assistant_incidents WHERE {w} ORDER BY opened_at DESC, "
                "id DESC "
                "OFFSET :o LIMIT :l"
            ),
            {**params, "o": pg.offset, "l": pg.page_size},
        )
    ).all()
    ids = [r.id for r in rows]
    names = await device_names(
        session, {d for r in rows for d in [r.root_device_id, *(r.root_candidates or [])]}
    )
    counts = await incident_counts(session, ids)
    gens = {
        int(r.k.split(":")[1]): r.at
        for r in await session.execute(
            text(
                "SELECT key AS k, created_at AS at FROM assistant_artifacts WHERE kind = "
                "'incident_summary' AND status = 'ok' AND key = ANY(:keys)"
            ),
            {"keys": [f"incident:{r.id}:{r.ai_summaries}" for r in rows]},
        )
    }
    items = [incident_out(r, names, counts.get(r.id, {}), gens.get(r.id)) for r in rows]
    return page(items, int(total or 0), pg.page, pg.page_size)


@router.get("/api/incidents/{incident_id}")
async def get_incident(incident_id: int, _: AdminSession, rt: RT, session: DB) -> dict:
    from netmon.assistant.features.correlate import member_out

    await common.require(rt, "ai_correlation", session)
    inc = await _ai(rt).correlation.load(session, incident_id)
    if inc is None:
        raise not_found("Incident")
    rows = (
        await session.execute(
            text(
                "SELECT m.event_id, m.sensor_id, m.device_id, m.status, m.time, m.recovered_at, "
                "s.name AS sensor_name, d.name AS device_name FROM assistant_incident_members m "
                "LEFT JOIN sensors s ON s.id = m.sensor_id LEFT JOIN devices d ON d.id = "
                "m.device_id "
                "WHERE m.incident_id = :i ORDER BY m.time, m.event_id"
            ),
            {"i": incident_id},
        )
    ).all()
    root = (inc.get("root_device") or {}).get("id")
    return {**inc, "members": [member_out(m, root) for m in rows]}


@router.post("/api/incidents/{incident_id}/summarize", status_code=202)
async def summarize_incident(incident_id: int, _: AdminSession, rt: RT, session: DB) -> dict:
    await common.require(rt, "ai_correlation", session)
    ai = _ai(rt)
    row = (
        await session.execute(
            text("SELECT ai_summaries FROM assistant_incidents WHERE id = :i"), {"i": incident_id}
        )
    ).first()
    if row is None:
        raise not_found("Incident")
    if row.ai_summaries >= 5:
        raise ApiError(
            429,
            "assistant_cap_reached",
            "At most 5 AI summaries per incident",
            [
                {
                    "cap": "purpose_daily",
                    "limit": 5,
                    "used": int(row.ai_summaries),
                    "resets_at": None,
                }
            ],
        )
    cap = await common.int_setting(session, "correlation_daily_summaries")
    used = await common.purpose_count_today(rt, session, "incident_summary", status=None)
    if used >= cap:
        raise ApiError(
            429,
            "assistant_cap_reached",
            "The daily limit of incident summaries is reached",
            [{"cap": "purpose_daily", "limit": cap, "used": used, "resets_at": None}],
        )
    await session.close()
    ai.correlation._maybe_summary(incident_id, 0, force=True)  # noqa: SLF001
    async with rt.sessionmaker() as s2:
        inc = await ai.correlation.load(s2, incident_id)
    return inc or {}


# ---------------------------------------------------------------- config diff (§ 14.8.4)
class DiffExplainIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    device_id: int
    from_: int = Field(alias="from")
    to: int
    refresh: bool = False


@router.post("/api/assistant/config-diff/explain")
async def diff_explain(body: DiffExplainIn, user: AdminSession, request: Request, rt: RT) -> dict:
    from netmon.assistant.features import diffexplain

    return await diffexplain.explain(
        rt, user, request_audit_ctx(request, rt), body.device_id, body.from_, body.to, body.refresh
    )


@router.get("/api/assistant/config-diff/flags")
async def diff_flags(
    _: AdminSession,
    rt: RT,
    session: DB,
    device_id: int,
    to: int,
    from_: Annotated[int, Query(alias="from")],
) -> dict:
    from netmon.assistant.features import diffexplain

    await common.require(rt, "ai_config_diff", session)
    return {"flags": await diffexplain.flags_for(rt, session, device_id, from_, to)}


# ---------------------------------------------------------------- digests (§ 14.8.5)
def _day(day: str) -> date:
    try:
        return date.fromisoformat(day)
    except ValueError:
        raise ApiError(
            422,
            "validation_error",
            "day must be YYYY-MM-DD",
            [{"loc": ["path", "day"], "msg": "YYYY-MM-DD"}],
        ) from None


@router.get("/api/assistant/digests")
async def list_digests(_: AdminSession, rt: RT, session: DB, pg: Page) -> dict:
    from netmon.assistant.features.digest import summary_out
    from netmon.services.serialize import page

    await common.require(rt, "ai_syslog_digest", session)
    total = (
        await session.execute(
            text("SELECT count(*) FROM assistant_artifacts WHERE kind = 'digest'")
        )
    ).scalar()
    rows = (
        await session.execute(
            text(
                f"SELECT {common.ARTIFACT_COLS} FROM assistant_artifacts WHERE kind = 'digest' "
                "ORDER BY key DESC OFFSET :o LIMIT :l"
            ),
            {"o": pg.offset, "l": pg.page_size},
        )
    ).all()
    return page(
        [summary_out(r.key.split(":", 1)[1], r) for r in rows],
        int(total or 0),
        pg.page,
        pg.page_size,
    )


@router.get("/api/assistant/digests/{day}")
async def get_digest(day: str, _: AdminSession, rt: RT, session: DB) -> dict:
    from netmon.assistant.features.digest import digest_out

    d = _day(day)
    await common.require(rt, "ai_syslog_digest", session)
    art = await common.get_artifact(session, "digest", f"digest:{d.isoformat()}")
    if art is None:
        raise not_found("Digest")
    return digest_out(d.isoformat(), art)


class DigestRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    send: bool = False


@router.post("/api/assistant/digests/{day}/run", status_code=202)
async def run_digest(
    day: str, user: AdminSession, request: Request, rt: RT, body: DigestRunIn | None = None
) -> dict:
    d = _day(day)
    async with rt.sessionmaker() as session:
        await common.require(rt, "ai_syslog_digest", session)
    return await _ai(rt).digest.request_run(
        d, bool(body.send) if body else False, user, request_audit_ctx(request, rt)
    )


# ---------------------------------------------------------------- rule drafts (§ 14.8.6)
class RuleDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["alert_rule", "event_rule"]
    description: str = Field(min_length=5, max_length=1000)


@router.post("/api/assistant/rule-drafts")
async def rule_draft(body: RuleDraftIn, user: AdminSession, request: Request, rt: RT) -> dict:
    from netmon.assistant.features import drafts

    return await drafts.run_draft(
        rt, user, request_audit_ctx(request, rt), body.kind, body.description
    )


# ---------------------------------------------------------------- template drafts (§ 14.8.7)
class WalkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: int
    presets: list[
        Literal["system", "entity", "host_resources", "enterprise", "if_sample", "entity_sensor"]
    ] = Field(
        default_factory=lambda: ["system", "entity", "host_resources", "enterprise"], max_length=6
    )
    oids: list[str] = Field(default_factory=list, max_length=8)


@router.post("/api/assistant/template-drafts/walks", status_code=202)
async def start_walk(body: WalkIn, user: AdminSession, request: Request, rt: RT) -> dict:
    return await _ai(rt).templates.start_walk(
        user, request_audit_ctx(request, rt), body.device_id, list(body.presets), list(body.oids)
    )


@router.get("/api/assistant/template-drafts/walks/{walk_id}")
async def get_walk(walk_id: int, _: AdminSession, rt: RT) -> dict:
    return await _ai(rt).templates.get_walk(walk_id)


@router.post("/api/assistant/template-drafts/walks/{walk_id}/cancel")
async def cancel_walk(walk_id: int, _: AdminSession, rt: RT) -> dict:
    return await _ai(rt).templates.cancel_walk(walk_id)


class TemplateDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    walk_id: int
    template_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,31}$")
    notes: str | None = Field(default=None, max_length=1000)


@router.post("/api/assistant/template-drafts")
async def template_draft(
    body: TemplateDraftIn, user: AdminSession, request: Request, rt: RT
) -> dict:
    return await _ai(rt).templates.draft(
        user, request_audit_ctx(request, rt), body.walk_id, body.template_id, body.notes
    )


# ---------------------------------------------------------------- user templates (always on)
class UserTemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    yaml: str = Field(min_length=1, max_length=256 * 1024)
    overwrite: bool = False


@router.post("/api/device-templates/user", status_code=201)
async def create_user_template(
    body: UserTemplateIn, _: AdminSession, request: Request, rt: RT
) -> dict:
    from netmon.assistant.features.templates import save_user_template

    return await save_user_template(rt, request_audit_ctx(request, rt), body.yaml, body.overwrite)
