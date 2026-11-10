"""[3] Single sign-on endpoints (api.md § [3] Single sign-on, architecture § 13.5).

Kerberos: `GET /api/auth/sso/kerberos` (gated by `sso_kerberos`) and the admin endpoints
`/api/sso/kerberos*` (work while OFF). OIDC: `/api/auth/oidc/start|callback` (gated by
`sso_oidc`) and `/api/sso/oidc*` (work while OFF)."""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import time
from datetime import datetime
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text

from netmon.api.deps import DB, RT, Admin, Audit, client_ip, feature_on, request_audit_ctx
from netmon.api.errors import ApiError, error_response
from netmon.auth import kerberos_pure, oidc
from netmon.models import User
from netmon.services import audit, settings_p3c
from netmon.services import settings as settings_svc
from netmon.services.audit import AuditCtx
from netmon.services.auth import cookie_secure, session_token, set_session_cookie
from netmon.services.features import require
from netmon.services.serialize import iso, now_utc, user_out

log = logging.getLogger("netmon.api.sso")
router = APIRouter()

NTLM_MESSAGE = (
    "Your browser offered NTLM instead of Kerberos. Open NetworkMonitor by its full name "
    "(netmonitor.<domain>) and make sure the site is in the Local intranet zone "
    "(or in the AuthServerAllowlist policy); the AD admin must register its HTTP/ SPN."
)
SSO_THROTTLE_NAME = "*sso*"


def _secure(request: Request, rt: RT) -> bool:
    return cookie_secure(request, rt.settings.netmon_cookie_secure, rt.settings.netmon_trust_proxy)


def _kerberos(request: Request) -> Any:
    rt = request.app.state.rt
    mgr = getattr(rt, "kerberos", None)
    if mgr is None:
        from netmon.auth.kerberos import KerberosManager

        mgr = rt.kerberos = KerberosManager(rt)
    return mgr


async def _sso_failed(rt: RT, request: Request, reason: str, source: str, name: str = "") -> None:
    await audit.record_now(
        rt,
        AuditCtx.anonymous(rt, name, client_ip(request, rt)),
        "auth.login_failed",
        source=source,
        reason=reason,
    )


# ================================================================ Kerberos
@router.get("/api/auth/sso/kerberos")
async def kerberos_login(request: Request, rt: RT, session: DB) -> Response:
    require(rt, "sso_kerberos")
    from netmon.api import auth as auth_api
    from netmon.auth import ldap_config, ldap_pure
    from netmon.auth.kerberos import KerberosFailure

    ip = client_ip(request, rt)
    limiter = request.app.state.login_limiter
    token = kerberos_pure.negotiate_token(request.headers.get("authorization"))
    if token is None:
        return error_response(
            401,
            "sso_challenge",
            "Kerberos authentication required",
            headers={"WWW-Authenticate": "Negotiate"},
        )
    if limiter.blocked(ip, SSO_THROTTLE_NAME):
        raise ApiError(429, "rate_limited", "Too many failed logins; try again later")
    mgr = _kerberos(request)

    async def failed(reason: str, name: str = "", code: str = "sso_failed") -> ApiError:
        limiter.fail(ip, SSO_THROTTLE_NAME)
        await _sso_failed(rt, request, reason, "ldap", name)
        if code == "sso_ntlm":
            return ApiError(401, "sso_ntlm", NTLM_MESSAGE)
        return ApiError(401, "sso_failed", "Single sign-on failed")

    try:
        accepted = await mgr.accept(token)
    except KerberosFailure as exc:
        if exc.reason == "sso_ntlm":
            raise await failed("sso_ntlm", code="sso_ntlm") from None
        log.info("Kerberos sign-in failed: %s", exc)
        raise await failed("sso_failed") from None
    parts = kerberos_pure.split_principal(accepted.principal)
    if parts is None:
        raise await failed("sso_failed", accepted.principal)
    name, realm = parts
    stored_krb = await settings_p3c.get(session, "kerberos")
    stored_ldap = await settings_svc.get(session, "ldap")
    realms = [str(r).upper() for r in stored_krb.get("realms") or []] or (
        kerberos_pure.default_realms(stored_ldap.get("domain"))
    )
    if realm not in realms:
        raise await failed("sso_realm", accepted.principal)
    if not feature_on(rt, "ldap"):
        raise await failed("ldap_disabled", accepted.principal)
    cfg = ldap_config.to_config(stored_ldap, rt.secret_key)
    if not ldap_pure.valid_sam(name):
        raise await failed("unknown_user", accepted.principal)
    await session.close()  # no DB connection is held while the directory answers
    outcome = await request.app.state.directory.lookup(cfg, name, now=rt.clock.now())
    if outcome.unavailable:
        await _sso_failed(rt, request, "directory_unavailable", "ldap", accepted.principal)
        raise ApiError(
            503, "directory_unavailable", "The directory (Active Directory) is not available"
        )
    if not outcome.ok or outcome.user is None or outcome.role is None:
        raise await failed(outcome.reason or "sso_failed", accepted.principal)
    sam = (outcome.user.sam_account_name or name).lower()
    if not ldap_pure.valid_sam(sam):
        raise await failed("not_permitted", accepted.principal)
    try:
        user, role_changed = await auth_api._upsert_ldap_user(  # noqa: SLF001
            request, rt, session, outcome.user, sam, outcome.role, method="kerberos"
        )
    except auth_api._LoginFailed as exc:  # noqa: SLF001
        await session.rollback()
        raise await failed(exc.reason, accepted.principal) from None
    limiter.reset(ip, SSO_THROTTLE_NAME)
    if role_changed:
        await rt.hub.close_user(user.id, 4401)
    headers = {}
    if accepted.out_token:
        headers["WWW-Authenticate"] = "Negotiate " + base64.b64encode(accepted.out_token).decode()
    resp = JSONResponse({"user": user_out(user)}, headers=headers)
    set_session_cookie(resp, session_token(rt, user, am="kerberos"), _secure(request, rt))
    return resp


class KerberosPut(BaseModel):
    realms: list[str] | None = Field(default=None, max_length=8)
    auto_login: bool | None = None

    @field_validator("realms")
    @classmethod
    def _realms(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out: list[str] = []
        for r in v:
            r = r.strip().upper()
            if not r or len(r) > 255 or any(c.isspace() or c in "@/\\" for c in r):
                raise ValueError(f"invalid realm {r!r}")
            if r not in out:
                out.append(r)
        if not out:
            raise ValueError("at least one realm")
        return out


async def _kerberos_out(request: Request, rt: RT, session: DB) -> dict[str, Any]:
    from netmon.auth.kerberos import decrypt_keytab, gssapi_available

    stored = await settings_p3c.get(session, "kerberos")
    ldap = await settings_svc.get(session, "ldap")
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    data = decrypt_keytab(stored, rt.secret_key)
    error = None
    if data is not None:
        try:
            parsed = kerberos_pure.parse_keytab(data)
            entries = [e.out() for e in parsed]
            warnings = kerberos_pure.keytab_warnings(parsed)
        except kerberos_pure.KeytabError as exc:
            error = str(exc)
    elif stored.get("keytab_enc"):
        error = "The stored keytab cannot be decrypted (secret.key changed?); upload it again"
    mgr = _kerberos(request)
    supported = mgr.platform_supported()
    state = mgr.health_state()
    if state != "disabled" and not supported:
        state = "unavailable"
    return {
        "realms": [str(r).upper() for r in stored.get("realms") or []]
        or kerberos_pure.default_realms(ldap.get("domain")),
        "auto_login": bool(stored.get("auto_login", True)),
        "keytab_set": data is not None,
        "keytab_uploaded_at": stored.get("keytab_uploaded_at"),
        "keytab_entries": entries,
        "warnings": warnings,
        "platform_supported": supported,
        "state": state,
        "error": mgr.error if state in ("error", "unavailable") else error,
        "unavailable_reason": None if supported else gssapi_available()[1],
    }


@router.get("/api/sso/kerberos")
async def get_kerberos(request: Request, _: Admin, rt: RT, session: DB) -> dict:
    return await _kerberos_out(request, rt, session)


@router.put("/api/sso/kerberos")
async def put_kerberos(
    body: KerberosPut, request: Request, _: Admin, rt: RT, session: DB, ctx: Audit
) -> dict:
    stored = await settings_p3c.get(session, "kerberos")
    before = {"realms": stored.get("realms"), "auto_login": stored.get("auto_login")}
    if body.realms is not None:
        stored["realms"] = body.realms
    if body.auto_login is not None:
        stored["auto_login"] = body.auto_login
    await settings_p3c.put(session, "kerberos", stored)
    changes = audit.diff(
        before, {"realms": stored.get("realms"), "auto_login": stored.get("auto_login")}
    )
    if changes:
        await audit.record(
            session, ctx, "sso.settings_update", audit.entity("sso", None, "kerberos"), changes
        )
    await session.commit()
    if feature_on(rt, "sso_kerberos"):
        await _kerberos(request).reload()
    return await _kerberos_out(request, rt, session)


async def read_upload(request: Request, field: str, max_bytes: int) -> bytes:
    """Minimal multipart/form-data reader (one file field; no python-multipart dependency)."""
    ctype = request.headers.get("content-type", "")
    if not ctype.lower().startswith("multipart/form-data"):
        raise ApiError(415, "unsupported_media_type", "Content-Type must be multipart/form-data")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes + 16 * 1024:
            raise ApiError(422, "invalid_keytab", "The file is larger than 64 KiB")
    msg = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + ctype.encode("latin-1") + b"\r\n\r\n" + bytes(body)
    )
    if not msg.is_multipart():
        raise ApiError(422, "validation_error", "Malformed multipart body")
    for part in msg.iter_parts():
        if part.get_param("name", header="content-disposition") == field:
            payload = part.get_payload(decode=True)
            data = payload if isinstance(payload, bytes) else b""
            if len(data) > max_bytes:
                raise ApiError(422, "invalid_keytab", "The file is larger than 64 KiB")
            return data
    raise ApiError(
        422, "validation_error", f"Field {field!r} is required", [{"loc": ["body", field]}]
    )


@router.post("/api/sso/kerberos/keytab")
async def upload_keytab(request: Request, _: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    from netmon.auth.kerberos import encrypt_keytab

    data = await read_upload(request, "file", kerberos_pure.MAX_KEYTAB_BYTES)
    try:
        entries = kerberos_pure.parse_keytab(data)
    except kerberos_pure.KeytabError as exc:
        raise ApiError(422, "invalid_keytab", str(exc)) from None
    stored = await settings_p3c.get(session, "kerberos")
    stored["keytab_enc"] = encrypt_keytab(data, rt.secret_key)
    stored["keytab_uploaded_at"] = iso(now_utc())
    await settings_p3c.put(session, "kerberos", stored)
    await audit.record(
        session,
        ctx,
        "sso.keytab_upload",
        audit.entity("sso", None, "kerberos"),
        {"keytab_entries": [None, len(entries)]},
    )
    await session.commit()
    if feature_on(rt, "sso_kerberos"):
        await _kerberos(request).reload()
    return await _kerberos_out(request, rt, session)


@router.delete("/api/sso/kerberos/keytab")
async def delete_keytab(request: Request, _: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    if feature_on(rt, "sso_kerberos"):
        raise ApiError(409, "conflict", "Turn Windows sign-in (Kerberos) off first")
    stored = await settings_p3c.get(session, "kerberos")
    had = bool(stored.get("keytab_enc"))
    stored["keytab_enc"] = None
    stored["keytab_uploaded_at"] = None
    await settings_p3c.put(session, "kerberos", stored)
    if had:
        await audit.record(session, ctx, "sso.keytab_delete", audit.entity("sso", None, "kerberos"))
    await session.commit()
    return await _kerberos_out(request, rt, session)


async def kerberos_enable_errors(rt: Any, session: Any, ldap_on: bool) -> list[dict[str, Any]]:
    """Turning `sso_kerberos` ON requires a keytab with ≥ 1 entry and `ldap` ON (422)."""
    from netmon.auth.kerberos import decrypt_keytab

    errors: list[dict[str, Any]] = []
    stored = await settings_p3c.get(session, "kerberos")
    data = decrypt_keytab(stored, rt.secret_key)
    ok = False
    if data is not None:
        try:
            ok = bool(kerberos_pure.parse_keytab(data))
        except kerberos_pure.KeytabError:
            ok = False
    if not ok:
        errors.append({"loc": ["body", "kerberos", "keytab"], "msg": "upload a keytab"})
    if not ldap_on:
        errors.append(
            {"loc": ["body", "feature_ldap_enabled"], "msg": "Active Directory login must be on"}
        )
    return errors


# ================================================================ OIDC
def _redirect_uri(request: Request, base: str | None) -> str:
    if base:
        return f"{base}/api/auth/oidc/callback"
    rt = request.app.state.rt
    scheme = "https" if cookie_secure(request, "auto", rt.settings.netmon_trust_proxy) else "http"
    host = request.headers.get("host", "localhost")
    if rt.settings.netmon_trust_proxy and request.headers.get("x-forwarded-host"):
        host = request.headers["x-forwarded-host"].split(",")[0].strip()
    return f"{scheme}://{host}/api/auth/oidc/callback"


def _oidc_client(request: Request) -> oidc.OidcClient:
    rt = request.app.state.rt
    c = getattr(rt, "oidc", None)
    if c is None:
        c = rt.oidc = oidc.OidcClient()
    return c


def _login_redirect(code: str) -> RedirectResponse:
    resp = RedirectResponse(f"/login?{urlencode({'sso_error': code})}", status_code=302)
    resp.delete_cookie(oidc.COOKIE_NAME, path=oidc.COOKIE_PATH)
    return resp


@router.get("/api/auth/oidc/start")
async def oidc_start(request: Request, rt: RT, session: DB, next: str | None = None) -> Response:  # noqa: A002
    require(rt, "sso_oidc")
    stored = await settings_p3c.get(session, "oidc")
    base = await settings_p3c.public_base_url(session)
    client = _oidc_client(request)
    tenant = str(stored.get("tenant_id") or "").lower()
    try:
        disc = await client.discovery(tenant)
    except oidc.OidcError:
        return _login_redirect("oidc_error")
    state = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    nonce = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
    verifier = oidc.pkce_verifier()
    redirect_uri = _redirect_uri(request, base)
    cookie = oidc.seal_cookie(
        rt.secret_key,
        {
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "next": oidc.safe_next(next),
            "redirect_uri": redirect_uri,
            "exp": time.time() + oidc.COOKIE_TTL_S,
        },
    )
    params = {
        "client_id": stored.get("client_id"),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": oidc.SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": oidc.pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    resp = RedirectResponse(f"{disc['authorization_endpoint']}?{urlencode(params)}", 302)
    resp.set_cookie(
        oidc.COOKIE_NAME,
        cookie,
        max_age=oidc.COOKIE_TTL_S,
        path=oidc.COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=_secure(request, rt),
    )
    return resp


SUCCESS_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>NetworkMonitor</title>
<meta http-equiv="refresh" content="0;url={url}"></head>
<body><p>Signing in&hellip; <a href="{url}">continue</a></p>
<script>window.location.replace({js});</script></body></html>
"""


@router.get("/api/auth/oidc/callback")
async def oidc_callback(request: Request, rt: RT, session: DB) -> Response:
    if not feature_on(rt, "sso_oidc"):
        return _login_redirect("feature_disabled")
    q = request.query_params
    data = oidc.open_cookie(rt.secret_key, request.cookies.get(oidc.COOKIE_NAME), time.time())

    async def fail(code: str, reason: str | None = None, name: str = "") -> Response:
        await _sso_failed(rt, request, reason or code, "oidc", name)
        return _login_redirect(code)

    if q.get("error"):
        return await fail(oidc.ERROR_MAP.get(q.get("error", ""), "sso_failed"))
    state = q.get("state") or ""
    code = q.get("code") or ""
    if (
        data is None
        or not code
        or not oidc.secrets.compare_digest(state.encode(), str(data.get("state", "")).encode())
    ):
        return await fail("sso_failed", "sso_failed")
    stored = await settings_p3c.get(session, "oidc")
    secret = oidc.decrypt_secret(stored, rt.secret_key)
    tenant = str(stored.get("tenant_id") or "").lower()
    client = _oidc_client(request)
    await session.close()
    try:
        if secret is None:
            raise oidc.OidcError("oidc_error", "no client secret")
        tokens = await client.exchange_code(
            tenant,
            str(stored.get("client_id")),
            secret,
            code,
            str(data.get("verifier")),
            str(data.get("redirect_uri")),
        )
        claims = await client.verify_id_token(tenant, str(tokens["id_token"]))
    except oidc.OidcError as exc:
        log.info("OIDC sign-in failed: %s", exc.detail or exc.code)
        return await fail(exc.code if exc.code != "sso_failed" else "sso_failed")
    res = oidc.validate_claims(claims, stored, data.get("nonce"), rt.clock.now().timestamp())
    uname = str(claims.get("preferred_username") or "")[:256]
    if not res.ok or res.role is None:
        return await fail(res.error or "sso_failed", res.error, uname)
    oid = str(claims.get("oid") or "").lower()
    username = uname.lower()
    if not oidc.is_guid(oid) or not username or len(username) > 64:
        return await fail("sso_failed", "sso_failed", uname)
    try:
        user = await _upsert_oidc_user(request, rt, session, oid, username, claims, res.role)
    except _OidcRefused as exc:
        await session.rollback()
        return await fail(exc.code, exc.code, uname)
    nxt = oidc.safe_next(data.get("next"))
    page = SUCCESS_PAGE.format(url=html.escape(nxt, quote=True), js=json.dumps(nxt))
    resp = HTMLResponse(page, headers={"Cache-Control": "no-store"})
    set_session_cookie(resp, session_token(rt, user, am="oidc"), _secure(request, rt))
    resp.delete_cookie(oidc.COOKIE_NAME, path=oidc.COOKIE_PATH)
    return resp


class _OidcRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def _upsert_oidc_user(
    request: Request,
    rt: RT,
    session: DB,
    oid: str,
    username: str,
    claims: dict[str, Any],
    role: str,
) -> User:
    await session.execute(text("SELECT pg_advisory_xact_lock(727003)"))
    user = (
        await session.execute(select(User).where(User.external_id == oid, User.source == "oidc"))
    ).scalar_one_or_none()
    clash = (
        await session.execute(
            select(User).where(
                func.lower(User.username) == username,
                *([User.id != user.id] if user is not None else []),
            )
        )
    ).scalar_one_or_none()
    if clash is not None and (user is None or clash.source != "oidc"):
        if user is None:
            raise _OidcRefused("sso_conflict")
    now = now_utc()
    ctx = request_audit_ctx(request, rt)
    email = str(claims.get("email") or "")[:255] or None
    display = str(claims.get("name") or "")[:256] or None
    if user is None:
        user = User(
            username=username,
            email=email,
            password_hash=None,
            role=role,
            language=str(await settings_svc.get(session, "default_language") or "en"),
            is_active=True,
            token_version=0,
            source="oidc",
            external_id=oid,
            display_name=display,
            last_login_at=now,
        )
        session.add(user)
        await session.flush()
        actor = ctx.as_user(user.id, user.username)
        await audit.record(
            session,
            actor,
            "user.oidc_create",
            audit.entity("user", user.id, user.username),
            audit.created({"username": username, "role": role, "email": email}),
            source="oidc",
        )
    else:
        actor = ctx.as_user(user.id, user.username)
        if clash is None and user.username.lower() != username:
            user.username = username
        user.email = email
        user.display_name = display
        if user.role != role:
            await audit.record(
                session,
                actor,
                "user.oidc_role_change",
                audit.entity("user", user.id, user.username),
                {"role": [user.role, role]},
                source="oidc",
            )
            user.role = role
            user.token_version += 1
        if not user.is_active:
            await session.commit()
            raise _OidcRefused("inactive")
        user.last_login_at = now
    await audit.record(
        session,
        actor,
        "auth.login",
        audit.entity("user", user.id, user.username),
        {"method": [None, "oidc"]},
        source="oidc",
    )
    await session.commit()
    await session.refresh(user)
    return user


class OidcPut(BaseModel):
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = Field(default=None, max_length=1024)
    client_secret_expires_at: str | None = None
    role_source: str | None = None
    admin_values: list[str] | None = Field(default=None, max_length=16)
    viewer_values: list[str] | None = Field(default=None, max_length=16)
    button_label: str | None = Field(default=None, max_length=64)

    @field_validator("tenant_id", "client_id")
    @classmethod
    def _guid(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        v = v.strip().lower()
        if not oidc.is_guid(v):
            raise ValueError("must be a GUID")
        return v

    @field_validator("role_source")
    @classmethod
    def _rs(cls, v: str | None) -> str | None:
        if v is not None and v not in ("app_roles", "groups"):
            raise ValueError("app_roles or groups")
        return v

    @field_validator("client_secret_expires_at")
    @classmethod
    def _date(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        try:
            datetime.fromisoformat(v)
        except ValueError:
            raise ValueError("a date (YYYY-MM-DD)") from None
        return v

    @field_validator("admin_values", "viewer_values")
    @classmethod
    def _vals(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out = []
        for x in v:
            x = x.strip()
            if not x or len(x) > 128:
                raise ValueError("values are 1–128 characters")
            if x not in out:
                out.append(x)
        return out


async def _oidc_out(request: Request, rt: RT, session: DB) -> dict[str, Any]:
    stored = await settings_p3c.get(session, "oidc")
    base = await settings_p3c.public_base_url(session)
    return {
        "tenant_id": stored.get("tenant_id") or "",
        "client_id": stored.get("client_id") or "",
        "client_secret": None,
        "client_secret_set": oidc.decrypt_secret(stored, rt.secret_key) is not None,
        "client_secret_expires_at": stored.get("client_secret_expires_at"),
        "role_source": stored.get("role_source") or "app_roles",
        "admin_values": list(stored.get("admin_values") or []),
        "viewer_values": list(stored.get("viewer_values") or []),
        "button_label": stored.get("button_label"),
        "redirect_uri": _redirect_uri(request, base),
    }


@router.get("/api/sso/oidc")
async def get_oidc(request: Request, _: Admin, rt: RT, session: DB) -> dict:
    return await _oidc_out(request, rt, session)


@router.put("/api/sso/oidc")
async def put_oidc(
    body: OidcPut, request: Request, _: Admin, rt: RT, session: DB, ctx: Audit
) -> dict:
    stored = await settings_p3c.get(session, "oidc")
    before = dict(stored)
    for key in body.model_fields_set:
        value = getattr(body, key)
        if key == "client_secret":
            if value is None:
                continue
            stored["client_secret_enc"] = (
                oidc.encrypt_secret(value, rt.secret_key) if value else None
            )
            continue
        if value is None and key in ("tenant_id", "client_id", "role_source"):
            continue
        stored[key] = value
    if feature_on(rt, "sso_oidc"):
        errors = oidc.oidc_config_errors(stored, rt.secret_key)
        if errors:
            raise ApiError(422, "validation_error", "The OIDC settings are incomplete", errors)
    await settings_p3c.put(session, "oidc", stored)
    b = {k: v for k, v in before.items() if k != "client_secret_enc"}
    a = {k: v for k, v in stored.items() if k != "client_secret_enc"}
    changes = audit.diff(b, a)
    if before.get("client_secret_enc") != stored.get("client_secret_enc"):
        changes["client_secret"] = ["***", "***"]
    if changes:
        await audit.record(
            session, ctx, "sso.settings_update", audit.entity("sso", None, "oidc"), changes
        )
    await session.commit()
    if before.get("tenant_id") != stored.get("tenant_id"):
        _oidc_client(request).reset()
    return await _oidc_out(request, rt, session)


@router.post("/api/sso/oidc/test")
async def test_oidc(request: Request, _: Admin, rt: RT, session: DB) -> dict:
    stored = await settings_p3c.get(session, "oidc")
    tenant = str(stored.get("tenant_id") or "").lower()
    if not oidc.is_guid(tenant):
        raise ApiError(502, "oidc_error", "No tenant id configured")
    await session.close()
    client = _oidc_client(request)
    t0 = time.monotonic()
    try:
        disc = await client.discovery(tenant, force=True)
        keys = await client.jwks(tenant, force=False)
    except oidc.OidcError as exc:
        raise ApiError(502, "oidc_error", exc.detail or exc.code) from None
    return {
        "ok": True,
        "issuer": disc.get("issuer"),
        "jwks_keys": len(keys),
        "latency_ms": int((time.monotonic() - t0) * 1000),
    }


async def sso_public(rt: Any, session: Any) -> dict[str, Any]:
    """`GET /api/settings/public` → `sso` (null parts while the features are OFF)."""
    out: dict[str, Any] = {"kerberos": None, "oidc": None}
    if feature_on(rt, "sso_kerberos"):
        k = await settings_p3c.get(session, "kerberos")
        out["kerberos"] = {"auto_login": bool(k.get("auto_login", True))}
    if feature_on(rt, "sso_oidc"):
        o = await settings_p3c.get(session, "oidc")
        out["oidc"] = {"button_label": o.get("button_label") or oidc.DEFAULT_BUTTON}
    return out
