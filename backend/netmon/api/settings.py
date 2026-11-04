from __future__ import annotations

from fastapi import APIRouter

from netmon.alerts import templates
from netmon.alerts.email import SmtpError, send_email
from netmon.api.common import merge_secret
from netmon.api.deps import DB, RT, Admin
from netmon.api.errors import ApiError
from netmon.db.timescale import reapply_retention
from netmon.schemas import SettingsPut, SmtpTestIn
from netmon.services import settings as settings_svc

router = APIRouter()


@router.get("/api/settings/public")
async def public_settings(session: DB) -> dict:
    return {"default_language": await settings_svc.get(session, "default_language")}


@router.get("/api/settings")
async def get_settings(_: Admin, session: DB) -> dict:
    return settings_svc.settings_out(await settings_svc.get_all(session))


@router.put("/api/settings")
async def put_settings(body: SettingsPut, _: Admin, rt: RT, session: DB) -> dict:
    current = await settings_svc.get_all(session)
    fields = body.model_fields_set
    retention_changed = False
    if "smtp" in fields and body.smtp is not None:
        smtp = dict(current["smtp"])
        incoming = body.smtp
        for key in incoming.model_fields_set:
            if key == "password":
                smtp["password"] = merge_secret(incoming.password, smtp.get("password"))
                continue
            value = getattr(incoming, key)
            if value is None and key in ("host", "port", "security", "from_address", "from_name"):
                continue
            smtp[key] = value if value != "" or key != "username" else None
        await settings_svc.put(session, "smtp", smtp)
    for key in ("retention_days", "event_retention_days", "default_interval_s", "default_language"):
        if key in fields and getattr(body, key) is not None:
            if key == "retention_days" and body.retention_days != current["retention_days"]:
                retention_changed = True
            await settings_svc.put(session, key, getattr(body, key))
    if "public_base_url" in fields:
        await settings_svc.put(session, "public_base_url", body.public_base_url)
    await session.commit()
    values = await settings_svc.get_all(session)
    if retention_changed and rt.timescale_enabled and rt.engine is not None:
        rt.retention_policy_ok = await reapply_retention(rt.engine, int(values["retention_days"]))
    return settings_svc.settings_out(values)


@router.post("/api/settings/smtp/test")
async def smtp_test(body: SmtpTestIn, _: Admin, session: DB) -> dict:
    cfg = await settings_svc.get_all(session)
    await session.close()
    lang = cfg["default_language"]
    payload = {"event": "test", "time": None}
    try:
        await send_email(
            cfg["smtp"],
            [body.to],
            templates.subject(payload, lang),
            templates.text_body(payload, lang),
            templates.html_body(payload, lang),
        )
    except SmtpError as exc:
        raise ApiError(502, "smtp_error", str(exc)) from exc
    return {"ok": True, "message": "sent"}
