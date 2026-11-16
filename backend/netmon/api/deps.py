"""FastAPI dependencies: runtime, DB session, auth, pagination."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from netmon.api.errors import ApiError
from netmon.models import User
from netmon.runtime import Runtime
from netmon.services.audit import AuditCtx, audit_enabled
from netmon.services.auth import (
    COOKIE_NAME,
    RENEW_BELOW_S,
    decode_token,
    session_token,
)
from netmon.services.security import is_revoked, policy_of


def get_rt(request: Request) -> Runtime:
    return request.app.state.rt  # type: ignore[no-any-return]


async def get_session(rt: Annotated[Runtime, Depends(get_rt)]) -> AsyncIterator[AsyncSession]:
    async with rt.sessionmaker() as session:
        yield session


RT = Annotated[Runtime, Depends(get_rt)]
DB = Annotated[AsyncSession, Depends(get_session)]


def feature_on(rt: Any, name: str) -> bool:
    """[2b-3] `rt.features.enabled(name)`; False before the feature manager exists."""
    features = getattr(rt, "features", None)
    if features is None:
        return False
    return bool(features.enabled(name))


def client_ip(conn: HTTPConnection, rt: Runtime) -> str:
    """Peer address (throttling, audit).

    uvicorn runs without proxy-header rewriting; when the proxy is trusted we take the
    *rightmost* X-Forwarded-For entry (the one our proxy appended), never the client-supplied
    leftmost one."""
    if rt.settings.netmon_trust_proxy:
        fwd = conn.headers.get("x-forwarded-for")
        if fwd:
            last = fwd.split(",")[-1].strip()
            if last:
                return last[:45]
    return conn.client.host if conn.client else "unknown"


@dataclass(frozen=True)
class AuthInfo:
    """[2b-3] How the current request is authenticated (request.state.auth)."""

    kind: str  # "session" | "token"
    user_id: int
    username: str
    role: str  # effective role (a token: min(token role, owner role))
    token_id: int | None = None
    source: str = "local"  # the user's source (local | ldap)
    sid: str | None = None  # [1.4.1] session id of a cookie session (revoked by logout)


def session_expired(rt: Runtime, user: User, claims: dict[str, Any]) -> bool:
    """[1.4.1] Absolute session lifetime, judged on the app clock from `at` (auth time):
    `session_max_age_h` for local accounts; AD / OIDC users re-authenticate at least every
    12 h ([2b-3], [3]) or sooner when the setting is lower."""
    at = claims.get("at", claims.get("iat"))
    try:
        at_f = float(at)
    except (TypeError, ValueError):
        return True
    max_s = policy_of(rt).session_max_s(getattr(user, "source", "local"))
    return rt.clock.now().timestamp() >= at_f + max_s


async def authenticate(rt: Runtime, session: AsyncSession, token: str | None) -> tuple[User, Any]:
    if not token:
        raise ApiError(401, "unauthenticated", "Not logged in")
    claims = decode_token(rt.secret_key, token)
    if claims is None:
        raise ApiError(401, "unauthenticated", "Session invalid or expired")
    try:
        uid = int(claims["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(401, "unauthenticated", "Session invalid") from exc
    # [1.4.1] a session without an id cannot be revoked: cookies issued before 1.4.1 are
    # refused (one sign-in after the upgrade); a revoked id is refused for good
    sid = claims.get("sid")
    if not isinstance(sid, str) or not sid or is_revoked(rt, sid):
        raise ApiError(401, "unauthenticated", "Session invalid or expired")
    user = await session.get(User, uid)
    if user is None or not user.is_active or claims.get("tv") != user.token_version:
        raise ApiError(401, "unauthenticated", "Session invalid or expired")
    if session_expired(rt, user, claims):
        raise ApiError(401, "unauthenticated", "Session expired; log in again")
    if user.source == "oidc" and not feature_on(rt, "sso_oidc"):
        # [3] `src = oidc` sessions are refused while the feature is OFF
        raise ApiError(401, "unauthenticated", "Microsoft sign-in is turned off")
    return user, claims


async def _bearer_user(request: Request, rt: Runtime, session: AsyncSession, token: str) -> User:
    from netmon.auth import tokens as tokens_svc
    from netmon.services import audit

    ip = client_ip(request, rt)
    limiter = request.app.state.token_limiter
    if limiter.blocked(ip):
        raise ApiError(429, "rate_limited", "Too many invalid API tokens; try again later")
    try:
        user, tok = await tokens_svc.verify(session, token, rt.clock.now())
    except tokens_svc.TokenFailure as exc:
        limiter.fail(ip)
        await audit.record_now(
            rt,
            AuditCtx.anonymous(rt, token[:12] if token else "", ip),
            "auth.login_failed",
            source="token",
            reason="token_invalid",
        )
        raise ApiError(401, "unauthenticated", "API token invalid, expired or revoked") from exc
    role = tokens_svc.effective_role(tok.role, user.role)
    # the request sees the effective role; the instance is detached so it is never flushed
    session.expunge(user)
    user.role = role
    request.state.auth = AuthInfo(
        "token", user.id, user.username, role, tok.id, getattr(user, "source", "local")
    )
    await request.app.state.token_stamper.stamp(rt, tok.id, ip)
    return user


async def current_user(request: Request, rt: RT, session: DB) -> User:
    from netmon.auth.tokens import bearer_from_header

    bearer = bearer_from_header(request.headers.get("authorization"))
    if bearer is not None:  # [2b-3] the cookie is ignored when a bearer header is present
        return await _bearer_user(request, rt, session, bearer)
    user, claims = await authenticate(rt, session, request.cookies.get(COOKIE_NAME))
    request.state.auth = AuthInfo(
        "session", user.id, user.username, user.role, None, user.source or "local", claims["sid"]
    )
    now = time.time()
    am = claims.get("am") if isinstance(claims.get("am"), str) else None  # [3] kept on renewal
    # sliding renewal keeps `at` and `sid`: it never extends past at + max (app clock)
    at = float(claims.get("at", claims.get("iat")))
    clock_now = rt.clock.now().timestamp()
    max_s = policy_of(rt).session_max_s(user.source)
    if int(claims["exp"]) - now < RENEW_BELOW_S and clock_now < at + max_s:
        request.state.renew_token = session_token(
            rt, user, at=at, clock_now=clock_now, am=am, sid=claims["sid"]
        )
    return user


async def require_admin(request: Request, user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != "admin":
        raise ApiError(403, "forbidden", "Administrator role required")
    return user


async def require_session(request: Request, user: Annotated[User, Depends(current_user)]) -> User:
    """[2b-3] Endpoints that a bearer token may not use (also enforced by the app guard)."""
    auth = getattr(request.state, "auth", None)
    if auth is not None and auth.kind == "token":
        raise ApiError(403, "forbidden", "API tokens cannot be used here")
    return user


Viewer = Annotated[User, Depends(current_user)]
Admin = Annotated[User, Depends(require_admin)]
SessionUser = Annotated[User, Depends(require_session)]


def request_audit_ctx(request: Request, rt: Runtime) -> AuditCtx:
    """[2b-3] Audit context of a request (anonymous until authenticated)."""
    enabled = audit_enabled(rt)
    ip = client_ip(request, rt)
    auth: AuthInfo | None = getattr(request.state, "auth", None)
    if auth is None:
        return AuditCtx(actor_type="anonymous", ip=ip, enabled=enabled)
    if auth.kind == "token":
        return AuditCtx(
            actor_type="token",
            actor_user_id=auth.user_id,
            actor_name=auth.username,
            token_id=auth.token_id,
            ip=ip,
            enabled=enabled,
        )
    return AuditCtx(
        actor_type="user",
        actor_user_id=auth.user_id,
        actor_name=auth.username,
        ip=ip,
        enabled=enabled,
    )


async def audit_ctx(
    request: Request, rt: RT, _user: Annotated[User, Depends(current_user)]
) -> AuditCtx:
    """Authenticated request's audit context (the auth dependency is shared, not re-run)."""
    return request_audit_ctx(request, rt)


Audit = Annotated[AuditCtx, Depends(audit_ctx)]


@dataclass
class Paging:
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def paging(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> Paging:
    return Paging(page, page_size)


Page = Annotated[Paging, Depends(paging)]
