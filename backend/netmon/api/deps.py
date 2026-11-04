"""FastAPI dependencies: runtime, DB session, auth, pagination."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.api.errors import ApiError
from netmon.models import User
from netmon.runtime import Runtime
from netmon.services.auth import COOKIE_NAME, RENEW_BELOW_S, decode_token, make_token


def get_rt(request: Request) -> Runtime:
    return request.app.state.rt  # type: ignore[no-any-return]


async def get_session(rt: Annotated[Runtime, Depends(get_rt)]) -> AsyncIterator[AsyncSession]:
    async with rt.sessionmaker() as session:
        yield session


RT = Annotated[Runtime, Depends(get_rt)]
DB = Annotated[AsyncSession, Depends(get_session)]


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
    user = await session.get(User, uid)
    if user is None or not user.is_active or claims.get("tv") != user.token_version:
        raise ApiError(401, "unauthenticated", "Session invalid or expired")
    return user, claims


async def current_user(request: Request, rt: RT, session: DB) -> User:
    user, claims = await authenticate(rt, session, request.cookies.get(COOKIE_NAME))
    if int(claims["exp"]) - time.time() < RENEW_BELOW_S:
        request.state.renew_token = make_token(
            rt.secret_key, user.id, user.role, user.token_version
        )
    return user


async def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != "admin":
        raise ApiError(403, "forbidden", "Administrator role required")
    return user


Viewer = Annotated[User, Depends(current_user)]
Admin = Annotated[User, Depends(require_admin)]


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
