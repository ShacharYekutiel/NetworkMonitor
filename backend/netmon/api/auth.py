"""Setup, auth and user management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from netmon.api.common import commit_or_conflict
from netmon.api.deps import (
    DB,
    RT,
    Admin,
    Audit,
    Page,
    Viewer,
    client_ip,
    feature_on,
    request_audit_ctx,
)
from netmon.api.errors import ApiError, not_found
from netmon.models import Group, User
from netmon.schemas import (
    ChangePasswordIn,
    LoginIn,
    LogoutIn,
    MePatch,
    SetupIn,
    UserCreate,
    UserPatch,
)
from netmon.services import audit
from netmon.services import security as security_svc
from netmon.services import settings as settings_svc
from netmon.services.audit import AuditCtx
from netmon.services.auth import (
    PW_CHANGE_THROTTLE_IP,
    burn_verify,
    clear_session_cookie,
    cookie_secure,
    hash_password,
    session_token,
    set_session_cookie,
    verify_password,
)
from netmon.services.serialize import now_utc, page, user_out

router = APIRouter()

WS_SESSION_RENEWED = 4000  # WebSocket close code: session re-issued, reconnect


def _secure(request: Request, rt: RT) -> bool:
    return cookie_secure(request, rt.settings.netmon_cookie_secure, rt.settings.netmon_trust_proxy)


def _client_ip(request: Request, rt: RT) -> str:
    return client_ip(request, rt)


def _login_response(request: Request, rt: RT, user: User, status: int = 200) -> JSONResponse:
    resp = JSONResponse({"user": user_out(user)}, status_code=status)
    set_session_cookie(resp, session_token(rt, user), _secure(request, rt))
    return resp


USER_AUDIT_FIELDS = ("username", "email", "role", "language", "is_active", "source")


def _user_values(u: User) -> dict:
    return {k: getattr(u, k) for k in USER_AUDIT_FIELDS}


def _user_entity(u: User) -> audit.Entity:
    return audit.entity("user", u.id, u.username)


# ---------------------------------------------------------------- setup
@router.get("/api/setup/status")
async def setup_status(rt: RT) -> dict:
    return {"setup_required": not await rt.has_users()}


@router.post("/api/setup", status_code=201)
async def setup(body: SetupIn, request: Request, rt: RT, session: DB) -> JSONResponse:
    await session.execute(text("SELECT pg_advisory_xact_lock(727002)"))
    count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    if count:
        raise ApiError(409, "setup_already_done", "Setup has already been completed")
    user = User(
        username=body.username,
        email=body.email,
        password_hash=await hash_password(body.password),
        role="admin",
        language=body.language,
        is_active=True,
        token_version=0,
        last_login_at=now_utc(),
    )
    session.add(user)
    exists = (
        await session.execute(
            select(Group.id).where(Group.parent_id.is_(None), func.lower(Group.name) == "default")
        )
    ).first()
    if exists is None:
        session.add(Group(name="Default", parent_id=None, description="", sort_order=0))
    await settings_svc.ensure_defaults(session)
    await settings_svc.put(session, "default_language", body.language)
    if body.timezone:
        from netmon.services.maintenance import valid_timezone

        if valid_timezone(body.timezone):
            await settings_svc.put(session, "timezone", body.timezone)
        else:
            # setup never fails over the browser's zone; the default stays
            logging.getLogger("netmon.api.auth").warning(
                "Setup: ignoring unknown time zone %r; using the default", body.timezone[:64]
            )
    await session.commit()
    if body.timezone and rt.maintenance is not None:
        await rt.maintenance.reload()
    await session.refresh(user)
    rt.mark_has_users()
    rt.config_changed("group", 0, "created")
    return _login_response(request, rt, user, 201)


# ---------------------------------------------------------------- auth
class _LoginFailed(Exception):
    def __init__(self, reason: str, status: int = 401) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


async def _login_failed(
    request: Request, rt: RT, username: str, reason: str, source: str | None, ip: str
) -> None:
    await audit.record_now(
        rt,
        AuditCtx.anonymous(rt, username, ip),
        "auth.login_failed",
        source=source,
        reason=reason,
    )


@router.post("/api/auth/login")
async def login(body: LoginIn, request: Request, rt: RT, session: DB) -> JSONResponse:
    ip = _client_ip(request, rt)
    limiter = request.app.state.login_limiter
    if limiter.blocked(ip, body.username):
        await _login_failed(request, rt, body.username, "rate_limited", None, ip)
        raise ApiError(429, "rate_limited", "Too many failed logins; try again later")
    user = (
        await session.execute(
            select(User).where(func.lower(User.username) == body.username.lower())
        )
    ).scalar_one_or_none()
    if user is not None and user.source == "local":
        # a local account always wins; the directory is never contacted
        ok = user.password_hash is not None and await verify_password(
            user.password_hash, body.password
        )
        if not ok or not user.is_active:
            limiter.fail(ip, body.username)
            reason = "bad_password" if not ok else "inactive"
            await _login_failed(request, rt, body.username, reason, "local", ip)
            raise ApiError(401, "invalid_credentials", "Invalid username or password")
        limiter.reset(ip, body.username)
        user.last_login_at = now_utc()
        await audit.record(
            session,
            request_audit_ctx(request, rt).as_user(user.id, user.username),
            "auth.login",
            _user_entity(user),
            source="local",
        )
        await session.commit()
        await session.refresh(user)
        return _login_response(request, rt, user)
    if not feature_on(rt, "ldap"):
        await burn_verify(body.password)
        limiter.fail(ip, body.username)
        reason = "ldap_disabled" if user is not None else "unknown_user"
        await _login_failed(request, rt, body.username, reason, None, ip)
        raise ApiError(401, "invalid_credentials", "Invalid username or password")
    try:
        user = await _ldap_login(request, rt, session, body.username, body.password, ip)
    except _LoginFailed as exc:
        await session.rollback()
        if exc.status == 401:
            limiter.fail(ip, body.username)
        await _login_failed(request, rt, body.username, exc.reason, "ldap", ip)
        if exc.status == 503:
            raise ApiError(
                503, "directory_unavailable", "The directory (Active Directory) is not available"
            ) from None
        if exc.status == 429:
            raise ApiError(429, "rate_limited", "Too many failed logins; try again later") from None
        raise ApiError(401, "invalid_credentials", "Invalid username or password") from None
    limiter.reset(ip, body.username)
    return _login_response(request, rt, user)


async def _ldap_login(
    request: Request, rt: RT, session: DB, username: str, password: str, ip: str
) -> User:
    """§ 12.7: directory checks, then the app user upsert (raises _LoginFailed)."""
    from netmon.auth import ldap_config, ldap_pure

    stored = await settings_svc.get(session, "ldap")
    cfg = ldap_config.to_config(stored, rt.secret_key)
    name = ldap_pure.parse_login(username, cfg.domain, cfg.netbios_domain)
    if name is None:
        await burn_verify(password)
        raise _LoginFailed("unknown_user")
    if password == "":
        raise _LoginFailed("empty_password")
    guard = request.app.state.directory.guard
    guard_key = (name.sam or name.upn or username).lower()
    if guard.blocked(guard_key):
        raise _LoginFailed("rate_limited", 429)
    if cfg.bind_password_unreadable:
        await _note_unreadable_password(rt)
    await session.close()  # no DB connection is held while the directory answers
    outcome = await request.app.state.directory.authenticate(
        cfg, username, password, now=rt.clock.now()
    )
    if outcome.unavailable:
        raise _LoginFailed("directory_unavailable", 503)
    if not outcome.ok or outcome.user is None or outcome.role is None:
        guard.fail(guard_key)
        raise _LoginFailed(outcome.reason or "bad_password")
    du = outcome.user
    sam = (du.sam_account_name or "").lower()
    if not ldap_pure.valid_sam(sam):
        guard.fail(guard_key)
        raise _LoginFailed("not_permitted")
    user, role_changed = await _upsert_ldap_user(request, rt, session, du, sam, outcome.role)
    guard.reset(guard_key)
    if role_changed:
        await rt.hub.close_user(user.id, 4401)  # other sessions of this user end
    return user


async def _upsert_ldap_user(
    request: Request,
    rt: RT,
    session: DB,
    du: object,
    sam: str,
    role: str,
    method: str | None = None,
) -> tuple[User, bool]:
    """[3] `method` (e.g. "kerberos") is recorded on the `auth.login` audit row."""
    from netmon.auth.ldap import DirectoryUser

    assert isinstance(du, DirectoryUser)
    await session.execute(text("SELECT pg_advisory_xact_lock(727003)"))  # concurrent first logins
    user: User | None = None
    if du.guid:
        user = (
            await session.execute(select(User).where(User.external_id == du.guid))
        ).scalar_one_or_none()
    if user is None:
        user = (
            await session.execute(
                select(User).where(func.lower(User.username) == sam, User.source == "ldap")
            )
        ).scalar_one_or_none()
    if user is None:
        clash = (
            await session.execute(select(User.id).where(func.lower(User.username) == sam))
        ).first()
        if clash is not None:
            # a local account owns this name (e.g. a UPN login of a user whose sAMAccountName
            # is also a local user name): the local account wins
            raise _LoginFailed("not_permitted")
    now = now_utc()
    role_changed = False
    ctx = request_audit_ctx(request, rt)
    email = (du.mail or None) and du.mail[:255]
    display = (du.display_name or None) and du.display_name[:256]
    if user is None:
        user = User(
            username=sam,
            email=email,
            password_hash=None,
            role=role,
            language=str(await settings_svc.get(session, "default_language") or "en"),
            is_active=True,
            token_version=0,
            source="ldap",
            external_id=du.guid,
            display_name=display,
            ldap_dn=du.dn,
            ldap_synced_at=now,
            last_login_at=now,
        )
        session.add(user)
        await session.flush()
        actor = ctx.as_user(user.id, user.username)
        await audit.record(
            session,
            actor,
            "user.ldap_create",
            _user_entity(user),
            audit.created({**_user_values(user), "display_name": display}),
            source="ldap",
        )
    else:
        actor = ctx.as_user(user.id, user.username)
        before = {**_user_values(user), "display_name": user.display_name}
        if user.username.lower() != sam:
            taken = (
                await session.execute(
                    select(User.id).where(func.lower(User.username) == sam, User.id != user.id)
                )
            ).first()
            if taken is None:
                user.username = sam
        user.email = email
        user.display_name = display
        user.ldap_dn = du.dn
        if du.guid:
            user.external_id = du.guid
        if user.role != role:
            await audit.record(
                session,
                actor,
                "user.ldap_role_change",
                _user_entity(user),
                {"role": [user.role, role]},
                source="ldap",
            )
            user.role = role
            user.token_version += 1  # other sessions of this user end
            role_changed = True
        changes = audit.diff(before, {**_user_values(user), "display_name": user.display_name})
        changes.pop("role", None)
        if changes:
            await audit.record(
                session, actor, "user.update", _user_entity(user), changes, source="ldap"
            )
        if not user.is_active:
            await session.commit()  # keep the synced attributes, refuse the login
            raise _LoginFailed("inactive")
        user.ldap_synced_at = now
        user.last_login_at = now
    await audit.record(
        session,
        actor,
        "auth.login",
        _user_entity(user),
        {"method": [None, method]} if method else None,
        source="ldap",
    )
    await session.commit()
    await session.refresh(user)
    return user, role_changed


_unreadable_noted = False


async def _note_unreadable_password(rt: RT) -> None:
    """One `system` event per process when the stored bind password cannot be decrypted."""
    global _unreadable_noted
    if _unreadable_noted:
        return
    _unreadable_noted = True
    from netmon.services import events as events_svc

    await events_svc.write_system_event(
        rt,
        "Active Directory: the stored bind password cannot be decrypted (secret.key changed?); "
        "enter it again in Settings",
    )


@router.post("/api/auth/logout", status_code=204)
async def logout(
    request: Request,
    rt: RT,
    user: Viewer,
    session: DB,
    ctx: Audit,
    body: LogoutIn | None = None,
) -> Response:
    """[1.4.1] Ends the session **server-side**: its id (`sid`) is revoked, so a copy of the
    cookie stops working at once, while the user's other browsers stay signed in.
    `{"everywhere": true}` ends every session of the user instead (`token_version`)."""
    request.state.renew_token = None
    auth = getattr(request.state, "auth", None)
    sid = getattr(auth, "sid", None)
    everywhere = bool(body and body.everywhere)
    if everywhere:
        user.token_version += 1
    elif sid:
        await security_svc.revoke_session(rt, session, sid, user.id)
    await audit.record(
        session,
        ctx,
        "auth.logout",
        _user_entity(user),
        {"everywhere": [None, True]} if everywhere else None,
        source=user.source,
    )
    await session.commit()
    if everywhere:
        await rt.hub.close_user(user.id, 4401)
    elif sid:
        await rt.hub.close_session(sid, 4401)
    resp = Response(status_code=204)
    clear_session_cookie(resp, _secure(request, rt))
    return resp


@router.get("/api/auth/me")
async def me(user: Viewer) -> dict:
    return {"user": user_out(user)}


def _ldap_managed(what: str) -> ApiError:
    return ApiError(
        409, "ldap_managed", f"The {what} of an Active Directory user is managed in the directory"
    )


@router.patch("/api/auth/me")
async def patch_me(body: MePatch, user: Viewer, session: DB, ctx: Audit) -> dict:
    fields = body.model_fields_set
    before = _user_values(user)
    if "email" in fields and user.source in ("ldap", "oidc") and body.email != user.email:
        raise _ldap_managed("email address")
    if "email" in fields:
        user.email = body.email
    if "language" in fields and body.language is not None:
        user.language = body.language
    changes = audit.diff(before, _user_values(user))
    if changes:
        await audit.record(session, ctx, "user.update", _user_entity(user), changes)
    await session.commit()
    await session.refresh(user)
    return {"user": user_out(user)}


@router.post("/api/auth/change-password", status_code=204)
async def change_password(
    body: ChangePasswordIn, request: Request, rt: RT, user: Viewer, session: DB, ctx: Audit
) -> Response:
    if user.source in ("ldap", "oidc") or user.password_hash is None:
        raise _ldap_managed("password")
    # [1.4.1] guessing the current password with a stolen session is throttled like a login:
    # 10 failures / 5 min per user (whatever the address) plus the address-wide counter
    ip = _client_ip(request, rt)
    limiter = request.app.state.login_limiter
    guess_key = f"pw-change:{user.id}"
    if limiter.blocked(PW_CHANGE_THROTTLE_IP, guess_key, ip_wide=False) or limiter.ip_blocked(ip):
        raise ApiError(429, "rate_limited", "Too many wrong passwords; try again later")
    if not await verify_password(user.password_hash, body.current_password):
        limiter.fail(PW_CHANGE_THROTTLE_IP, guess_key, ip_wide=False)
        limiter.fail(ip, user.username)
        await audit.record_now(
            rt, ctx, "auth.login_failed", source=user.source, reason="bad_current_password"
        )
        raise ApiError(401, "invalid_credentials", "Current password is wrong")
    limiter.reset(PW_CHANGE_THROTTLE_IP, guess_key)
    user.password_hash = await hash_password(body.new_password)
    user.token_version += 1
    await audit.record(
        session, ctx, "user.update", _user_entity(user), {"password": ["***", "***"]}
    )
    await session.commit()
    # this browser gets a new cookie: its sockets reconnect (4000); other sessions get 4401
    await rt.hub.close_user(user.id, WS_SESSION_RENEWED)
    request.state.renew_token = None
    resp = Response(status_code=204)
    set_session_cookie(resp, session_token(rt, user), _secure(request, rt))
    return resp


# ---------------------------------------------------------------- users
async def _active_local_admins(session: DB) -> int:
    """[2b-3] `last_admin` protects the last active *local* admin (break-glass account)."""
    return (
        await session.execute(
            select(func.count())
            .select_from(User)
            .where(User.role == "admin", User.is_active, User.source == "local")
        )
    ).scalar_one()


def _is_active_local_admin(u: User) -> bool:
    return u.role == "admin" and u.is_active and u.source == "local"


@router.get("/api/users")
async def list_users(_: Admin, session: DB, pg: Page) -> dict:
    total = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    rows = (
        await session.execute(
            select(User).order_by(func.lower(User.username)).offset(pg.offset).limit(pg.page_size)
        )
    ).scalars()
    return page([user_out(u) for u in rows], total, pg.page, pg.page_size)


@router.post("/api/users", status_code=201)
async def create_user(body: UserCreate, _: Admin, session: DB, ctx: Audit) -> dict:
    dup = (
        await session.execute(
            select(User.id).where(func.lower(User.username) == body.username.lower())
        )
    ).first()
    if dup:  # also an LDAP user's name (local users only are created here)
        raise ApiError(409, "conflict", "Username already exists", [{"field": "username"}])
    user = User(
        username=body.username,
        email=body.email,
        password_hash=await hash_password(body.password),
        role=body.role,
        language=body.language,
        is_active=True,
        token_version=0,
        source="local",
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:  # unique race
        await session.rollback()
        raise ApiError(
            409, "conflict", "Username already exists", [{"field": "username"}]
        ) from None
    await audit.record(
        session, ctx, "user.create", _user_entity(user), audit.created(_user_values(user))
    )
    await commit_or_conflict(session, "username")
    await session.refresh(user)
    return user_out(user)


@router.get("/api/users/{user_id}")
async def get_user(user_id: int, _: Admin, session: DB) -> dict:
    user = await session.get(User, user_id)
    if user is None:
        raise not_found("User")
    return user_out(user)


@router.patch("/api/users/{user_id}")
async def patch_user(
    user_id: int, body: UserPatch, request: Request, rt: RT, me_: Admin, session: DB, ctx: Audit
) -> JSONResponse:
    user = await session.get(User, user_id)
    if user is None:
        raise not_found("User")
    fields = body.model_fields_set
    if user.source in ("ldap", "oidc"):  # [3] OIDC users: the same managed fields
        if "password" in fields and body.password:
            raise _ldap_managed("password")
        if "role" in fields and body.role is not None and body.role != user.role:
            raise _ldap_managed("role")
        if "email" in fields and body.email != user.email:
            raise _ldap_managed("email address")
    bump = False
    demote = "role" in fields and body.role is not None and body.role != user.role
    deactivate = "is_active" in fields and body.is_active is False and user.is_active
    if demote or deactivate:
        if user.id == me_.id:
            raise ApiError(409, "last_admin", "You may not demote or deactivate yourself")
        if _is_active_local_admin(user) and await _active_local_admins(session) <= 1:
            raise ApiError(409, "last_admin", "Cannot demote or deactivate the last local admin")
    before = _user_values(user)
    if "email" in fields:
        user.email = body.email
    if "language" in fields and body.language is not None:
        user.language = body.language
    if demote:
        user.role = body.role  # type: ignore[assignment]
        bump = True
    if "is_active" in fields and body.is_active is not None and body.is_active != user.is_active:
        user.is_active = body.is_active
        bump = True
    password_changed = False
    if "password" in fields and body.password:
        user.password_hash = await hash_password(body.password)
        bump = True
        password_changed = True
    if bump:
        user.token_version += 1
    changes = audit.diff(before, _user_values(user))
    if password_changed:
        changes["password"] = ["***", "***"]
    if changes:
        await audit.record(session, ctx, "user.update", _user_entity(user), changes)
    await session.commit()
    await session.refresh(user)
    if bump:
        await rt.hub.close_user(user.id, WS_SESSION_RENEWED if user.id == me_.id else 4401)
    resp = JSONResponse(user_out(user))
    if bump and user.id == me_.id:
        request.state.renew_token = None
        set_session_cookie(resp, session_token(rt, user), _secure(request, rt))
    return resp


@router.delete("/api/users/{user_id}", status_code=204)
async def delete_user(user_id: int, me_: Admin, rt: RT, session: DB, ctx: Audit) -> Response:
    user = await session.get(User, user_id)
    if user is None:
        raise not_found("User")
    if user.id == me_.id:
        raise ApiError(409, "last_admin", "You may not delete yourself")
    if _is_active_local_admin(user) and await _active_local_admins(session) <= 1:
        raise ApiError(409, "last_admin", "Cannot delete the last local admin")
    await audit.record(
        session, ctx, "user.delete", _user_entity(user), audit.deleted(_user_values(user))
    )
    await session.delete(user)
    await session.commit()
    await rt.hub.close_user(user_id, 4401)
    return Response(status_code=204)
