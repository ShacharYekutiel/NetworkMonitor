"""[2b-3] API tokens (feature `api_tokens`; session only — a token cannot manage tokens)."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from netmon.api.deps import DB, RT, Audit, SessionUser
from netmon.api.errors import ApiError, conflict, not_found, validation_error
from netmon.auth import tokens as tokens_svc
from netmon.models import ApiToken, User
from netmon.services import audit
from netmon.services.features import feature_disabled

router = APIRouter(dependencies=[Depends(feature_disabled("api_tokens"))])


class ApiTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    role: Literal["admin", "viewer"] = "viewer"
    expires_days: int = Field(default=90, ge=1, le=365)


def _entity(tok: ApiToken) -> audit.Entity:
    return audit.entity("api_token", tok.id, tok.name)


@router.get("/api/tokens")
async def list_tokens(user: SessionUser, rt: RT, session: DB, all: bool = False) -> dict:
    stmt = select(ApiToken, User.username).join(User, User.id == ApiToken.user_id)
    if all:
        if user.role != "admin":
            raise ApiError(403, "forbidden", "Administrator role required")
    else:
        stmt = stmt.where(ApiToken.user_id == user.id)
    rows = (
        await session.execute(stmt.order_by(ApiToken.created_at.desc(), ApiToken.id.desc()))
    ).all()
    now = rt.clock.now()
    return {"items": [tokens_svc.token_out(t, name, now) for t, name in rows]}


@router.post("/api/tokens", status_code=201)
async def create_token(
    body: ApiTokenIn, user: SessionUser, rt: RT, session: DB, ctx: Audit
) -> dict:
    name = body.name.strip()
    if not name:
        raise validation_error(["body", "name"], "must not be empty")
    if body.role == "admin" and user.role != "admin":
        raise validation_error(["body", "role"], "cannot be above your own role")
    dup = (
        await session.execute(
            select(ApiToken.id).where(
                ApiToken.user_id == user.id, func.lower(ApiToken.name) == name.lower()
            )
        )
    ).first()
    if dup:
        raise conflict("name")
    text, prefix, digest = tokens_svc.generate()
    now = rt.clock.now()
    tok = ApiToken(
        user_id=user.id,
        name=name,
        prefix=prefix,
        token_hash=digest,
        role=body.role,
        created_at=now,
        expires_at=now + timedelta(days=body.expires_days),
    )
    session.add(tok)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise conflict("name") from None
    await audit.record(
        session,
        ctx,
        "api_token.create",
        _entity(tok),
        audit.created(
            {"name": name, "role": body.role, "prefix": prefix, "expires_days": body.expires_days}
        ),
    )
    await session.commit()
    return {"token": text, "item": tokens_svc.token_out(tok, user.username, now)}


@router.delete("/api/tokens/{token_id}", status_code=204)
async def revoke_token(
    token_id: int, user: SessionUser, rt: RT, session: DB, ctx: Audit
) -> Response:
    tok = await session.get(ApiToken, token_id)
    if tok is None or (tok.user_id != user.id and user.role != "admin"):
        raise not_found("API token")
    if tok.revoked_at is None:
        tok.revoked_at = rt.clock.now()
        await audit.record(
            session, ctx, "api_token.revoke", _entity(tok), {"revoked": [False, True]}
        )
        await session.commit()
    return Response(status_code=204)
