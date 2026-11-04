"""Setup, auth and user management."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from netmon.api.common import commit_or_conflict
from netmon.api.deps import DB, RT, Admin, Page, Viewer
from netmon.api.errors import ApiError, not_found
from netmon.models import Group, User
from netmon.schemas import ChangePasswordIn, LoginIn, MePatch, SetupIn, UserCreate, UserPatch
from netmon.services import settings as settings_svc
from netmon.services.auth import (
    burn_verify,
    clear_session_cookie,
    cookie_secure,
    hash_password,
    make_token,
    set_session_cookie,
    verify_password,
)
from netmon.services.serialize import now_utc, page, user_out

router = APIRouter()

WS_SESSION_RENEWED = 4000  # WebSocket close code: session re-issued, reconnect


def _secure(request: Request, rt: RT) -> bool:
    return cookie_secure(request, rt.settings.netmon_cookie_secure, rt.settings.netmon_trust_proxy)


def _client_ip(request: Request, rt: RT) -> str:
    """Peer address used for login throttling.

    uvicorn runs without proxy-header rewriting; when the proxy is trusted we take the
    *rightmost* X-Forwarded-For entry (the one our proxy appended), never the client-supplied
    leftmost one."""
    if rt.settings.netmon_trust_proxy:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            last = fwd.split(",")[-1].strip()
            if last:
                return last
    return request.client.host if request.client else "unknown"


def _login_response(request: Request, rt: RT, user: User, status: int = 200) -> JSONResponse:
    resp = JSONResponse({"user": user_out(user)}, status_code=status)
    token = make_token(rt.secret_key, user.id, user.role, user.token_version)
    set_session_cookie(resp, token, _secure(request, rt))
    return resp


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
    await session.commit()
    await session.refresh(user)
    rt.mark_has_users()
    rt.config_changed("group", 0, "created")
    return _login_response(request, rt, user, 201)


# ---------------------------------------------------------------- auth
@router.post("/api/auth/login")
async def login(body: LoginIn, request: Request, rt: RT, session: DB) -> JSONResponse:
    ip = _client_ip(request, rt)
    limiter = request.app.state.login_limiter
    if limiter.blocked(ip, body.username):
        raise ApiError(429, "rate_limited", "Too many failed logins; try again later")
    user = (
        await session.execute(
            select(User).where(func.lower(User.username) == body.username.lower())
        )
    ).scalar_one_or_none()
    if user is None:
        await burn_verify(body.password)
        ok = False
    else:
        ok = await verify_password(user.password_hash, body.password) and user.is_active
    if not ok or user is None:
        limiter.fail(ip, body.username)
        raise ApiError(401, "invalid_credentials", "Invalid username or password")
    limiter.reset(ip, body.username)
    user.last_login_at = now_utc()
    await session.commit()
    await session.refresh(user)
    return _login_response(request, rt, user)


@router.post("/api/auth/logout", status_code=204)
async def logout(request: Request, rt: RT, user: Viewer) -> Response:
    request.state.renew_token = None
    resp = Response(status_code=204)
    clear_session_cookie(resp, _secure(request, rt))
    return resp


@router.get("/api/auth/me")
async def me(user: Viewer) -> dict:
    return {"user": user_out(user)}


@router.patch("/api/auth/me")
async def patch_me(body: MePatch, user: Viewer, session: DB) -> dict:
    fields = body.model_fields_set
    if "email" in fields:
        user.email = body.email
    if "language" in fields and body.language is not None:
        user.language = body.language
    await session.commit()
    await session.refresh(user)
    return {"user": user_out(user)}


@router.post("/api/auth/change-password", status_code=204)
async def change_password(
    body: ChangePasswordIn, request: Request, rt: RT, user: Viewer, session: DB
) -> Response:
    if not await verify_password(user.password_hash, body.current_password):
        raise ApiError(401, "invalid_credentials", "Current password is wrong")
    user.password_hash = await hash_password(body.new_password)
    user.token_version += 1
    await session.commit()
    # this browser gets a new cookie: its sockets reconnect (4000); other sessions get 4401
    await rt.hub.close_user(user.id, WS_SESSION_RENEWED)
    request.state.renew_token = None
    resp = Response(status_code=204)
    set_session_cookie(
        resp,
        make_token(rt.secret_key, user.id, user.role, user.token_version),
        _secure(request, rt),
    )
    return resp


# ---------------------------------------------------------------- users
async def _active_admins(session: DB) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(User).where(User.role == "admin", User.is_active)
        )
    ).scalar_one()


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
async def create_user(body: UserCreate, _: Admin, session: DB) -> dict:
    dup = (
        await session.execute(
            select(User.id).where(func.lower(User.username) == body.username.lower())
        )
    ).first()
    if dup:
        raise ApiError(409, "conflict", "Username already exists", [{"field": "username"}])
    user = User(
        username=body.username,
        email=body.email,
        password_hash=await hash_password(body.password),
        role=body.role,
        language=body.language,
        is_active=True,
        token_version=0,
    )
    session.add(user)
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
    user_id: int, body: UserPatch, request: Request, rt: RT, me_: Admin, session: DB
) -> JSONResponse:
    user = await session.get(User, user_id)
    if user is None:
        raise not_found("User")
    fields = body.model_fields_set
    bump = False
    demote = "role" in fields and body.role is not None and body.role != user.role
    deactivate = "is_active" in fields and body.is_active is False and user.is_active
    if demote or deactivate:
        if user.id == me_.id:
            raise ApiError(409, "last_admin", "You may not demote or deactivate yourself")
        if user.role == "admin" and user.is_active and await _active_admins(session) <= 1:
            raise ApiError(409, "last_admin", "Cannot demote or deactivate the last admin")
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
    if "password" in fields and body.password:
        user.password_hash = await hash_password(body.password)
        bump = True
    if bump:
        user.token_version += 1
    await session.commit()
    await session.refresh(user)
    if bump:
        await rt.hub.close_user(user.id, WS_SESSION_RENEWED if user.id == me_.id else 4401)
    resp = JSONResponse(user_out(user))
    if bump and user.id == me_.id:
        request.state.renew_token = None
        set_session_cookie(
            resp,
            make_token(rt.secret_key, user.id, user.role, user.token_version),
            _secure(request, rt),
        )
    return resp


@router.delete("/api/users/{user_id}", status_code=204)
async def delete_user(user_id: int, me_: Admin, rt: RT, session: DB) -> Response:
    user = await session.get(User, user_id)
    if user is None:
        raise not_found("User")
    if user.id == me_.id:
        raise ApiError(409, "last_admin", "You may not delete yourself")
    if user.role == "admin" and user.is_active and await _active_admins(session) <= 1:
        raise ApiError(409, "last_admin", "Cannot delete the last admin")
    await session.delete(user)
    await session.commit()
    await rt.hub.close_user(user_id, 4401)
    return Response(status_code=204)
