"""[3b] Assistant connection endpoints (api.md "[3b] Assistant settings, test, certificate",
"[3b] AI features": knowledge + usage). Admin and session only (API tokens are barred on
`/api/assistant*` by the app guard).

Allowed while `assistant` is OFF (preparation): settings, test, certificate (+ CSR), models,
usage, knowledge settings. `GET /api/assistant/status` and the KB test need it ON."""

from __future__ import annotations

import re
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from netmon.api.deps import DB, RT, Admin, Audit, SessionUser
from netmon.api.errors import ApiError
from netmon.assistant import certs, config, gates
from netmon.assistant import testconn as tc
from netmon.assistant.errors import AssistantError
from netmon.schemas.assistant import (
    AssistantSettingsIn,
    AssistantTestIn,
    CsrIn,
    KnowledgeSettingsIn,
    KnowledgeTestIn,
)
from netmon.services import audit, crypto
from netmon.services import settings as settings_svc
from netmon.services.features import feature_error, setting_key
from netmon.services.serialize import iso, now_utc

router = APIRouter()

SECRET_FIELDS = ("secret_access_key_enc", "session_token_enc", "ra_key_enc", "ra_csr_key_enc")
NOT_AUDITED = ("last_test", "ra_cert_pem", "ra_chain_pem", "ra_csr_pem")
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
    return {k: v for k, v in cfg.items() if k not in NOT_AUDITED}


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
    current = await config.load(session)
    fields = body.changes()
    fields.pop("enabled", None)
    fields.pop("test_id", None)
    merged = config.merge(current, fields, rt.secret_key)
    _validation(config.field_errors(merged, test_mode=svc.test_mode))
    on_now = rt.features.enabled("assistant")
    want = body.enabled if "enabled" in body.model_fields_set else None
    turning_on = want is True and not on_now
    turning_off = want is False and on_now
    stays_on = on_now and not turning_off
    old_hash, new_hash = svc.connection_hash(current), svc.connection_hash(merged)
    if turning_on:
        if not rt.features.enabled("audit"):
            raise ApiError(
                422,
                "feature_dependency",
                "The assistant needs the audit log: turn the audit feature on first",
                [{"feature": "assistant", "requires": "audit"}],
            )
        has_cert, has_key = svc.has_cert_and_key(merged)
        _validation(
            config.required_errors(merged, rt.secret_key, has_cert=has_cert, has_key=has_key),
            "The assistant settings are incomplete",
        )
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
    if not svc.tests.allow(user.id):
        raise ApiError(429, "rate_limited", "Too many connection tests; try again in a few minutes")
    stored = await config.load(session)
    fields = body.settings.changes() if body.settings is not None else {}
    cfg = config.merge(stored, fields, rt.secret_key)
    await session.close()
    result = await tc.run_test(svc, cfg, user_id=user.id)
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


# ---------------------------------------------------------------- certificate
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


@router.post("/api/assistant/cert")
async def upload_cert(
    request: Request, _: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit
) -> dict:
    svc = _svc(rt)
    if config.host_files() is not None:
        raise ApiError(
            409, "conflict", "The certificate comes from host files (NETMON_AWS_RA_CERT_FILE)"
        )
    form = await _read_form(request, certs.MAX_UPLOAD)
    if not form.get("file"):
        raise ApiError(
            422, "validation_error", "Field 'file' is required", [{"loc": ["body", "file"]}]
        )
    stored = await config.load(session)
    pending = None
    try:
        pending = config.decrypt_opt(
            rt.secret_key, stored.get("ra_csr_key_enc"), config.AAD_RA_CSR_KEY
        )
    except config.Unreadable:
        pending = None
    password = form.get("password", b"").decode("utf-8", "replace") or None
    try:
        mat = certs.parse_upload(
            form["file"], form.get("key") or None, password, pending_key_pem=pending
        )
    except certs.CertError as exc:
        details = [{"reason": exc.reason}] if exc.code == "invalid_certificate" else None
        raise ApiError(422, exc.code, exc.reason, details) from None
    before_meta = None
    if stored.get("ra_cert_pem"):
        found = certs.load_pem_certs(stored["ra_cert_pem"].encode())
        before_meta = certs.cert_info(found[0], [], source="uploaded", uploaded_at=None,
                                      key_unreadable=False) if found else None  # fmt: skip
    stored["ra_cert_pem"] = mat.cert_pem
    stored["ra_chain_pem"] = mat.chain_pem
    stored["ra_key_enc"] = crypto.encrypt(rt.secret_key, mat.key_pem or "", config.AAD_RA_KEY)
    stored["ra_cert_uploaded_at"] = iso(now_utc())
    used_csr_key = pending is not None and mat.key_pem == certs.key_to_pkcs8_pem(
        certs.key_from_pkcs8_pem(pending)
    )
    if used_csr_key:
        for k in ("ra_csr_pem", "ra_csr_key_enc", "ra_csr_subject", "ra_csr_created_at"):
            stored[k] = None
    await config.save(session, stored)
    info = certs.cert_info(
        mat.cert,
        mat.chain,
        source="uploaded",
        uploaded_at=stored["ra_cert_uploaded_at"],
        key_unreadable=False,
    )
    await audit.record(
        session,
        ctx,
        "assistant.cert_upload",
        audit.entity("assistant", None, "certificate"),
        {
            "subject": [before_meta and before_meta["subject"], info["subject"]],
            "serial_hex": [before_meta and before_meta["serial_hex"], info["serial_hex"]],
            "not_after": [before_meta and before_meta["not_after"], info["not_after"]],
            "sha256": [before_meta and before_meta["sha256"], info["sha256"]],
            "key_from_csr": [None, used_csr_key],
        },
    )
    await session.commit()
    await svc.reload()
    return info


@router.delete("/api/assistant/cert", status_code=204)
async def delete_cert(_: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit) -> Response:
    svc = _svc(rt)
    stored = await config.load(session)
    if rt.features.enabled("assistant") and stored.get("auth_mode") == "roles_anywhere":
        raise ApiError(
            409, "conflict", "Turn the assistant off (or change the auth mode) before deleting"
        )
    had = stored.get("ra_cert_pem")
    for k in ("ra_cert_pem", "ra_chain_pem", "ra_key_enc", "ra_cert_uploaded_at"):
        stored[k] = None
    await config.save(session, stored)
    if had:
        await audit.record(
            session, ctx, "assistant.cert_delete", audit.entity("assistant", None, "certificate")
        )
    await session.commit()
    await svc.reload()
    return Response(status_code=204)


def _csr_out(stored: dict[str, Any]) -> dict[str, Any]:
    return {
        "subject": stored.get("ra_csr_subject"),
        "created_at": stored.get("ra_csr_created_at"),
        "pem": stored.get("ra_csr_pem"),
    }


@router.post("/api/assistant/cert/csr")
async def make_csr(body: CsrIn, _: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit) -> dict:
    """Owner decision (3b): generate a key on the server (kept encrypted) and a CSR for the
    internal CA; the issued certificate is then uploaded without a key."""
    import asyncio

    svc = _svc(rt)
    if config.host_files() is not None:
        raise ApiError(
            409, "conflict", "The certificate comes from host files (NETMON_AWS_RA_CERT_FILE)"
        )
    err = certs.validate_cn(body.common_name)
    if err:
        raise ApiError(
            422, "validation_error", f"common_name: {err}", [{"loc": ["body", "common_name"]}]
        )
    size = body.key_size or (2048 if body.key_type == "rsa" else 256)
    try:
        key_pem, csr_pem = await asyncio.to_thread(
            certs.generate_csr,
            body.common_name,
            body.key_type,
            size,
            body.organization,
            body.organizational_unit,
        )
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc), [{"loc": ["body", "key_size"]}]) from None
    stored = await config.load(session)
    stored["ra_csr_pem"] = csr_pem
    stored["ra_csr_key_enc"] = crypto.encrypt(rt.secret_key, key_pem, config.AAD_RA_CSR_KEY)
    stored["ra_csr_subject"] = f"CN={body.common_name.strip()}"
    stored["ra_csr_created_at"] = iso(now_utc())
    await config.save(session, stored)
    await audit.record(
        session,
        ctx,
        "assistant.csr_create",
        audit.entity("assistant", None, "certificate"),
        {
            "subject": [None, stored["ra_csr_subject"]],
            "key_type": [None, f"{body.key_type}-{size}"],
        },
    )
    await session.commit()
    await svc.reload()
    return _csr_out(stored)


@router.get("/api/assistant/cert/csr")
async def download_csr(_: Admin, __: SessionUser, rt: RT, session: DB) -> Response:
    stored = await config.load(session)
    if not stored.get("ra_csr_pem"):
        raise ApiError(404, "not_found", "No CSR has been generated")
    return Response(
        stored["ra_csr_pem"],
        media_type="application/pkcs10",
        headers={"Content-Disposition": 'attachment; filename="netmonitor-bedrock.csr"'},
    )


@router.delete("/api/assistant/cert/csr", status_code=204)
async def delete_csr(_: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit) -> Response:
    stored = await config.load(session)
    if stored.get("ra_csr_pem"):
        for k in ("ra_csr_pem", "ra_csr_key_enc", "ra_csr_subject", "ra_csr_created_at"):
            stored[k] = None
        await config.save(session, stored)
        await audit.record(
            session, ctx, "assistant.csr_delete", audit.entity("assistant", None, "certificate")
        )
        await session.commit()
        await _svc(rt).reload()
    return Response(status_code=204)


# ---------------------------------------------------------------- models
@router.post("/api/assistant/models")
async def list_models(
    body: AssistantTestIn, _: Admin, __: SessionUser, rt: RT, session: DB
) -> dict:
    svc = _svc(rt)
    stored = await config.load(session)
    fields = body.settings.changes() if body.settings is not None else {}
    cfg = config.merge(stored, fields, rt.secret_key)
    await session.close()
    try:
        client = svc.build_client({**cfg, "model_id": cfg.get("model_id") or "-"}, report=False)
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
