"""[severity] The severity mapping (api.md "[severity] Alert severity"): read by every role,
changed by admins. Editable while the feature is OFF (no effect until it is ON); a change
re-resolves the active problems while it is ON."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from netmon.api.deps import DB, RT, Admin, Audit, Viewer
from netmon.schemas import Severity
from netmon.services import audit
from netmon.services import settings as settings_svc
from netmon.services import severity as severity_svc

router = APIRouter()


class SeverityMappingIn(BaseModel):
    """Omitted keys keep their current value; unknown keys -> 422."""

    model_config = ConfigDict(extra="forbid")
    down_critical: Severity | None = None
    down_warning: Severity | None = None
    down_none: Severity | None = None
    warning_critical: Severity | None = None
    warning_warning: Severity | None = None
    warning_none: Severity | None = None
    info: Severity | None = None
    # the admin option (not a mapping row): email subjects name the severity
    severity_in_subject: bool | None = None


async def _subject_option(session: DB) -> bool:
    return bool(await settings_svc.get(session, severity_svc.SUBJECT_KEY))


def _out(mapping: dict[str, str], in_subject: bool) -> dict:
    return {
        "mapping": mapping,
        "defaults": dict(severity_svc.DEFAULT_MAPPING),
        "levels": list(severity_svc.LEVELS),
        "severity_in_subject": in_subject,
    }


async def _store(
    rt: RT, session: DB, ctx: Audit, new: dict[str, str], in_subject: bool | None = None
) -> dict:
    current = await severity_svc.load_mapping(session)
    subj_old = await _subject_option(session)
    subj_new = subj_old if in_subject is None else in_subject
    before: dict = {}
    after: dict = {}
    if new != current:
        await settings_svc.put(session, severity_svc.KEY, new)
        before[severity_svc.KEY], after[severity_svc.KEY] = current, new
    if subj_new != subj_old:
        await settings_svc.put(session, severity_svc.SUBJECT_KEY, subj_new)
        before[severity_svc.SUBJECT_KEY], after[severity_svc.SUBJECT_KEY] = subj_old, subj_new
    if after:
        await audit.record_settings(session, ctx, before, after)
    await session.commit()
    if new != current:
        await severity_svc.reresolve_now(rt, ctx, reason="mapping")
    return _out(new, subj_new)


@router.get("/api/settings/severity-mapping")
async def get_mapping(_: Viewer, session: DB) -> dict:
    return _out(await severity_svc.load_mapping(session), await _subject_option(session))


@router.put("/api/settings/severity-mapping")
async def put_mapping(body: SeverityMappingIn, _: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    new = await severity_svc.load_mapping(session)
    for key in body.model_fields_set:
        value = getattr(body, key)
        if key != severity_svc.SUBJECT_KEY and value is not None:
            new[key] = value
    return await _store(rt, session, ctx, severity_svc.normalize(new), body.severity_in_subject)


@router.post("/api/settings/severity-mapping/reset")
async def reset_mapping(_: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    """Puts the default mapping back; the subject option is not a mapping row and stays."""
    return await _store(rt, session, ctx, dict(severity_svc.DEFAULT_MAPPING))
