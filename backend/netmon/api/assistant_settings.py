"""[3b] Assistant connection endpoints (api.md "[3b] Assistant settings, test, certificate",
"[3b] AI features": knowledge + usage). Admin and session only (API tokens are barred on
`/api/assistant*` by the app guard).

Allowed while `assistant` is OFF (preparation): settings, test, models, usage, knowledge
settings. `GET /api/assistant/status` and the KB test need it ON.

[4] PR 4-0a: the authentication moved to the shared AWS connections (`/api/aws/connections*`,
`netmon.api.aws_connections`): the settings carry `connection_id` / `access` instead, a 1.4.x
authentication field in a body is refused ("moved to Settings → AWS"), `/api/assistant/cert*`
are removed."""

from __future__ import annotations

import re
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any

from fastapi import APIRouter, Query, Request

from netmon.api.deps import DB, RT, Admin, Audit, SessionUser
from netmon.api.errors import ApiError
from netmon.assistant import config, gates
from netmon.assistant import testconn as tc
from netmon.assistant.errors import AssistantError
from netmon.aws import connections as conns
from netmon.schemas.assistant import (
    AssistantFieldsIn,
    AssistantSettingsIn,
    AssistantTestIn,
    KnowledgeSettingsIn,
    KnowledgeTestIn,
)
from netmon.services import audit
from netmon.services import settings as settings_svc
from netmon.services.features import feature_error, setting_key
from netmon.services.serialize import iso, now_utc

router = APIRouter()

SECRET_FIELDS = ("secret_access_key_enc", "session_token_enc", "ra_key_enc", "ra_csr_key_enc")
# [4] the legacy (mirrored) authentication keys are not audited here (the connection is), nor
# the ExternalId (audited as `access.external_id_set`)
NOT_AUDITED = ("last_test", *conns.LEGACY_KEYS)
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _svc(rt: Any) -> Any:
    svc = getattr(rt, "assistant", None)
    if svc is None:
        raise ApiError(503, "unavailable", "The assistant is not initialised")
    return svc


def _require_on(rt: Any) -> None:
    if not rt.features.enabled("assistant"):
        raise feature_error("assistant")


def _validation(errors: list[dict[str, Any]], message: str = "Invalid assistant settings") -> None:
    if errors:
        raise ApiError(422, "validation_error", message, errors)


def _audit_view(cfg: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in cfg.items() if k not in NOT_AUDITED}
    access = conns.normalize_access(cfg.get("access"))
    out["access"] = {
        "mode": access["mode"],
        "role_arn": access["role_arn"],
        "external_id_set": bool(access["external_id"]),
    }
    return out


def _moved(fields: AssistantFieldsIn | None) -> None:
    """[4] 1.4.x authentication fields in a body → 422 "moved to Settings → AWS"."""
    if fields is None:
        return
    errs = [{"loc": ["body", f], "msg": config.MOVED_MESSAGE} for f in fields.moved]
    if errs:
        raise ApiError(422, "validation_error", "Authentication " + config.MOVED_MESSAGE, errs)


async def _view_for(session: Any, svc: Any, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The connection view `cfg` uses; `connection_id` of a missing connection → 422."""
    cid = cfg.get("connection_id")
    if cid is not None and await conns.get_row(session, int(cid)) is None:
        raise ApiError(
            422, "validation_error", "Unknown AWS connection",
            [{"loc": ["body", "connection_id"], "msg": "no such connection"}],
        )  # fmt: skip
    view: dict[str, Any] | None = await svc.resolve_view(session, cfg)
    return view


def _normalize(fields: dict[str, Any]) -> dict[str, Any]:
    if "access" in fields:
        fields["access"] = conns.normalize_access(fields["access"])
    return fields


async def _set_feature(
    session: Any, ctx: Any, rt: Any, name: str, on: bool, *, before: bool
) -> None:
    await settings_svc.put(session, setting_key(name), on)
    if before != on:
        await audit.record(
            session,
            ctx,
            "feature.enable" if on else "feature.disable",
            audit.entity("feature", None, name),
            {setting_key(name): [before, on]},
        )


# ---------------------------------------------------------------- settings
@router.get("/api/assistant/settings")
async def get_settings(_: Admin, __: SessionUser, rt: RT) -> dict:
    svc = _svc(rt)
    await svc.load()
    return svc.settings_out()


@router.put("/api/assistant/settings")
async def put_settings(
    body: AssistantSettingsIn, user: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit
) -> dict:
    svc = _svc(rt)
    _moved(body)
    current = await config.load(session)
    fields = _normalize(body.changes())
    fields.pop("enabled", None)
    fields.pop("test_id", None)
    merged = config.merge_settings(current, fields)
    _validation(config.settings_field_errors(merged, test_mode=svc.test_mode))
    view = await _view_for(session, svc, merged)
    access = conns.normalize_access(merged.get("access"))
    _validation(conns.access_errors(access, view) if access["mode"] != "direct" else [])
    on_now = rt.features.enabled("assistant")
    want = body.enabled if "enabled" in body.model_fields_set else None
    turning_on = want is True and not on_now
    turning_off = want is False and on_now
    stays_on = on_now and not turning_off
    old_view = await svc.resolve_view(session, current)
    old_hash = svc.connection_hash(current, old_view)
    new_hash = svc.connection_hash(merged, view)
    if turning_on:
        if not rt.features.enabled("audit"):
            raise ApiError(
                422,
                "feature_dependency",
                "The assistant needs the audit log: turn the audit feature on first",
                [{"feature": "assistant", "requires": "audit"}],
            )
        errs = config.settings_required_errors(merged)
        if view is None or not conns.configured(view):
            errs.append(
                {"loc": ["body", "connection_id"], "msg": "configure an AWS connection first"}
            )
        _validation(errs, "The assistant settings are incomplete")
    needs_test = turning_on or (stays_on and old_hash != new_hash)
    if needs_test and not svc.tests.valid("assistant", body.test_id, user.id, new_hash):
        raise ApiError(
            422,
            "assistant_test_required",
            "Run a successful Test connection first",
            [{"feature": "assistant"}],
        )
    await config.save(session, merged)
    changes = audit.diff(_audit_view(current), _audit_view(merged), secret_keys=SECRET_FIELDS)
    if changes:
        await audit.record(
            session,
            ctx,
            "assistant.settings_update",
            audit.entity("assistant", None, "connection"),
            changes,
            secret_keys=SECRET_FIELDS,
        )
    if turning_on or turning_off:
        await _set_feature(session, ctx, rt, "assistant", bool(want), before=on_now)
    await conns.sync_legacy(session)  # [4] mirror again when back on "Default" / direct
    await session.commit()
    if body.test_id and needs_test:
        svc.tests.consume(body.test_id)
    await svc.reload()
    if turning_on or turning_off:
        await rt.features.apply({"assistant": bool(want)})
    return svc.settings_out()


# ---------------------------------------------------------------- test
@router.post("/api/assistant/test")
async def test_connection(
    body: AssistantTestIn, user: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit
) -> dict:
    svc = _svc(rt)
    _moved(body.settings)
    if not svc.tests.allow(user.id):
        raise ApiError(429, "rate_limited", "Too many connection tests; try again in a few minutes")
    stored = await config.load(session)
    fields = _normalize(body.settings.changes() if body.settings is not None else {})
    cfg = config.merge_settings(stored, fields)
    # SSRF: reject disallowed endpoint targets before the connection test connects.
    _validation(config.settings_field_errors(cfg, test_mode=svc.test_mode))
    view = await _view_for(session, svc, cfg)
    await session.close()
    result = await tc.run_test(svc, cfg, user_id=user.id, view=view)
    async with rt.sessionmaker() as s2:
        latest = await config.load(s2)
        latest["last_test"] = {
            "ok": bool(result["ok"]),
            "at": iso(now_utc()),
            "by": user.username,
            "error_code": (result.get("error") or {}).get("code"),
        }
        await config.save(s2, latest)
        await audit.record(
            s2,
            ctx,
            "assistant.test",
            audit.entity("assistant", None, "connection"),
            {
                "ok": [None, bool(result["ok"])],
                "error_code": [None, (result.get("error") or {}).get("code")],
                "steps": [None, [{"step": st["step"], "ok": st["ok"]} for st in result["steps"]]],
            },
        )
        await s2.commit()
    svc.cfg["last_test"] = latest["last_test"]
    return result


# ---------------------------------------------------------------- multipart (certificate uploads)
async def _read_form(request: Request, max_bytes: int) -> dict[str, bytes]:
    ctype = request.headers.get("content-type", "")
    if not ctype.lower().startswith("multipart/form-data"):
        raise ApiError(415, "unsupported_media_type", "Content-Type must be multipart/form-data")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 3 * max_bytes + 16 * 1024:
            raise ApiError(
                422, "invalid_certificate", "The upload is too large", [{"reason": "too_large"}]
            )
    msg = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + ctype.encode("latin-1") + b"\r\n\r\n" + bytes(body)
    )
    if not msg.is_multipart():
        raise ApiError(422, "validation_error", "Malformed multipart body")
    out: dict[str, bytes] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name in ("file", "key", "password"):
            payload = part.get_payload(decode=True)
            out[str(name)] = payload if isinstance(payload, bytes) else b""
    return out


# ---------------------------------------------------------------- models
@router.post("/api/assistant/models")
async def list_models(
    body: AssistantTestIn, _: Admin, __: SessionUser, rt: RT, session: DB
) -> dict:
    svc = _svc(rt)
    _moved(body.settings)
    stored = await config.load(session)
    fields = _normalize(body.settings.changes() if body.settings is not None else {})
    cfg = config.merge_settings(stored, fields)
    # SSRF: reject disallowed endpoint targets before any outbound request is built.
    _validation(config.settings_field_errors(cfg, test_mode=svc.test_mode))
    view = await _view_for(session, svc, cfg)
    await session.close()
    try:
        access = conns.normalize_access(cfg.get("access"))
        bad = conns.access_errors(access, view) if access["mode"] != "direct" else []
        if bad:
            return {"items": [], "error": {"code": "validation_error", "message": "; ".join(
                f"{e['loc'][-1]}: {e['msg']}" for e in bad)}}  # fmt: skip
        client = svc.build_client(
            {**cfg, "model_id": cfg.get("model_id") or "-"}, report=False, view=view
        )
    except AssistantError as err:
        return {"items": [], "error": {"code": err.code, "message": err.message}}
    try:
        items = await client.list_models()
    except AssistantError as err:
        return {"items": [], "error": {"code": err.code, "message": err.message}}
    finally:
        await client.aclose()
    return {"items": items, "error": None}


# ---------------------------------------------------------------- status / usage
@router.get("/api/assistant/status")
async def status(user: Admin, __: SessionUser, rt: RT, session: DB) -> dict:
    svc = _svc(rt)
    _require_on(rt)
    await svc.refresh_flags()
    snapshot = rt.features.snapshot()
    extra: dict[str, Any] = {"tools": [], "status_lines": {}, "running_message_id": None}
    for provider in list(svc.status_providers):
        try:
            part = await provider(user.id)
        except Exception:  # noqa: BLE001 - a broken provider never breaks the status
            continue
        extra["tools"] += list(part.get("tools") or [])
        extra["status_lines"].update(part.get("status_lines") or {})
        if part.get("running_message_id") is not None:
            extra["running_message_id"] = part["running_message_id"]
    features = []
    for name in gates.AI_FEATURES:
        idle = gates.idle_reasons(name, snapshot, svc.cfg)
        features.append(
            {
                "name": name,
                "enabled": bool(snapshot.get(name)),
                "idle_reason": (",".join(idle) if idle else None) if snapshot.get(name) else None,
                "status_line": extra["status_lines"].get(name),
            }
        )
    day, _tz = await svc.usage.today(session)
    sc = await config.scalars(session)
    return {
        "state": svc.state(),
        "state_error": svc.state_error,
        "tools": extra["tools"],
        "features": features,
        "me": {
            "questions_today": await svc.usage.questions_today(session, user.id, day),
            "daily_limit": int(sc["assistant_daily_questions_per_user"]),
            "running_message_id": extra["running_message_id"],
        },
        "month": await svc.usage.month_summary(session=session),
        # [3b owner] the models the chat composer offers (main model first = default)
        "chat_models": config.chat_models_effective(svc.cfg),
        "default_model_id": svc.cfg.get("model_id") or None,
        # [4] the live credential chain of the assistant (architecture § 15.1.5)
        "aws": svc.aws_status(),
    }


@router.get("/api/assistant/usage")
async def usage_report(
    _: Admin, __: SessionUser, rt: RT, month: str | None = Query(default=None)
) -> dict:
    if month is not None and not MONTH_RE.match(month):
        raise ApiError(
            422, "validation_error", "month must be YYYY-MM", [{"loc": ["query", "month"]}]
        )
    return await _svc(rt).usage.report(month)


# ---------------------------------------------------------------- knowledge base
@router.get("/api/assistant/knowledge")
async def get_knowledge(_: Admin, __: SessionUser, rt: RT) -> dict:
    svc = _svc(rt)
    await svc.load()
    return svc.kb_out()


@router.put("/api/assistant/knowledge")
async def put_knowledge(
    body: KnowledgeSettingsIn, user: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit
) -> dict:
    svc = _svc(rt)
    current = await config.load_kb(session)
    fields = body.model_dump(exclude_unset=True)
    fields.pop("enabled", None)
    fields.pop("test_id", None)
    if "knowledge_base_id" in fields and fields["knowledge_base_id"] is None:
        fields["knowledge_base_id"] = ""
    if "description" in fields and fields["description"] is None:
        fields["description"] = ""
    for k in ("results", "min_score"):
        if k in fields and fields[k] is None:
            fields.pop(k)
    merged = config.merge_kb(current, fields)
    _validation(config.kb_field_errors(merged, test_mode=svc.test_mode))
    on_now = rt.features.enabled("ai_knowledge")
    want = body.enabled if "enabled" in body.model_fields_set else None
    turning_on = want is True and not on_now
    turning_off = want is False and on_now
    stays_on = on_now and not turning_off
    conn_hash = svc.connection_hash(svc.cfg)
    old_h = config.kb_connection_hash(current, conn_hash)
    new_h = config.kb_connection_hash(merged, conn_hash)
    if turning_on and not merged.get("knowledge_base_id"):
        _validation([{"loc": ["body", "knowledge_base_id"], "msg": "required"}])
    needs_test = turning_on or (stays_on and old_h != new_h)
    if needs_test and not svc.tests.valid("kb", body.test_id, user.id, new_h):
        raise ApiError(
            422,
            "assistant_test_required",
            "Run a successful Knowledge Base test first",
            [{"feature": "ai_knowledge"}],
        )
    await config.save_kb(session, merged)
    view = lambda d: {k: v for k, v in d.items() if k != "last_test"}  # noqa: E731
    changes = audit.diff(view(current), view(merged))
    if changes:
        await audit.record(
            session,
            ctx,
            "assistant.settings_update",
            audit.entity("assistant", None, "knowledge_base"),
            changes,
        )
    if turning_on or turning_off:
        await _set_feature(session, ctx, rt, "ai_knowledge", bool(want), before=on_now)
    await session.commit()
    if body.test_id and needs_test:
        svc.tests.consume(body.test_id)
    await svc.load()
    if turning_on or turning_off:
        await rt.features.apply({"ai_knowledge": bool(want)})
    return svc.kb_out()


@router.post("/api/assistant/knowledge/test")
async def test_knowledge(
    body: KnowledgeTestIn, user: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit
) -> dict:
    svc = _svc(rt)
    _require_on(rt)
    if not svc.tests.allow(user.id):
        raise ApiError(429, "rate_limited", "Too many connection tests; try again in a few minutes")
    stored = await config.load_kb(session)
    fields = body.settings.model_dump(exclude_unset=True) if body.settings is not None else {}
    if fields.get("knowledge_base_id") is None:
        fields.pop("knowledge_base_id", None)
    kb = config.merge_kb(stored, {k: v for k, v in fields.items() if v is not None or k in (
        "region", "endpoint_url")})  # fmt: skip
    # SSRF: reject disallowed KB endpoint targets before the KB test connects.
    _validation(config.kb_field_errors(kb, test_mode=svc.test_mode))
    await session.close()
    out = await tc.run_kb_test(svc, kb, user_id=user.id)
    async with rt.sessionmaker() as s2:
        latest = await config.load_kb(s2)
        latest["last_test"] = {
            "ok": bool(out["ok"]),
            "at": iso(now_utc()),
            "by": user.username,
            "error_code": (out.get("error") or {}).get("code"),
        }
        await config.save_kb(s2, latest)
        await audit.record(
            s2,
            ctx,
            "assistant.kb_test",
            audit.entity("assistant", None, "knowledge_base"),
            {"ok": [None, out["ok"]], "error_code": [None, (out.get("error") or {}).get("code")]},
        )
        await s2.commit()
    svc.kb["last_test"] = latest["last_test"]
    return out
