"""[3] Acknowledge from chat API (api.md § Acknowledge from chat, feature `chat_ack`).

API tokens are refused here (`403 forbidden`): every endpoint needs a web session."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from netmon.api.deps import DB, RT, Audit, SessionUser, require_session
from netmon.api.errors import ApiError, not_found
from netmon.chatack import linking, service, tokens
from netmon.chatack.telegram_poller import TelegramApiError, TelegramPollers
from netmon.models import User
from netmon.services import audit
from netmon.services.features import require
from netmon.services.serialize import acknowledgement_out, iso, sensor_out


def _feature(request: Request) -> None:
    require(request.app.state.rt, "chat_ack")


router = APIRouter(dependencies=[Depends(_feature)])


async def _session_admin(user: Annotated[User, Depends(require_session)]) -> User:
    if user.role != "admin":
        raise ApiError(403, "forbidden", "Administrator role required")
    return user


SessionAdmin = Annotated[User, Depends(_session_admin)]


def pollers(rt: Any) -> TelegramPollers:
    p = getattr(rt, "telegram_pollers", None)
    if p is None:
        p = rt.telegram_pollers = TelegramPollers(rt)
    return p


class TokenIn(BaseModel):
    token: str = Field(max_length=64)


class AckIn(TokenIn):
    comment: str = Field(default="", max_length=1000)


def _decode(rt: Any, token: str) -> tuple[int, int]:
    try:
        sid, eid, _ = tokens.decode(service.key_of(rt), token, service.now_min(rt))
    except tokens.AckTokenError as exc:
        if exc.reason == "expired":
            raise ApiError(410, "ack_expired", "This link has expired") from None
        raise ApiError(400, "ack_invalid", "This link is not valid") from None
    return sid, eid


@router.post("/api/chat-ack/resolve")
async def resolve(body: TokenIn, user: SessionUser, rt: RT, session: DB) -> dict:
    sid, eid = _decode(rt, body.token)
    problem = await service.problem_of(session, sid, eid)
    if problem is None:
        raise ApiError(400, "ack_invalid", "The sensor of this link no longer exists")
    s, d = problem.sensor, problem.device
    maint = rt.maintenance
    return {
        "sensor": sensor_out(
            s,
            d.name,
            d.group_id,
            reveal_secrets=user.role == "admin",
            in_maintenance=bool(maint is not None and maint.in_maintenance(d.id)),
        ),
        "device_name": d.name,
        "problem_since": iso(problem.problem_since),
        "event_id": eid,
        "active": problem.active,
        "acknowledgement": acknowledgement_out(s),
        "can_acknowledge": user.role == "admin" and problem.active and s.ack_at is None,
    }


@router.post("/api/chat-ack/acknowledge")
async def acknowledge(body: AckIn, user: SessionAdmin, rt: RT, session: DB, ctx: Audit) -> dict:
    sid, eid = _decode(rt, body.token)
    problem = await service.problem_of(session, sid, eid, lock=True)
    if problem is None:
        raise ApiError(400, "ack_invalid", "The sensor of this link no longer exists")
    s, d = problem.sensor, problem.device
    if s.ack_at is None:
        if s.status not in ("down", "warning"):
            raise ApiError(409, "ack_stale", "This problem is no longer active")
        if not problem.active:
            raise ApiError(409, "ack_stale", "This problem is no longer active")
        await service.acknowledge(
            rt, session, problem, user.id, user.username, body.comment, ctx, "link"
        )
        await session.refresh(s)
    maint = rt.maintenance
    return sensor_out(
        s,
        d.name,
        d.group_id,
        reveal_secrets=True,
        in_maintenance=bool(maint is not None and maint.in_maintenance(d.id)),
    )


@router.get("/api/chat-links")
async def list_links(user: SessionUser, session: DB, all: bool = False) -> dict:  # noqa: A002
    if all and user.role != "admin":
        raise ApiError(403, "forbidden", "Administrator role required")
    where = "" if all else "WHERE ci.user_id = :u"
    rows = await session.execute(
        text(
            "SELECT ci.id, ci.user_id, u.username, ci.provider, ci.display, ci.linked_at, "
            f"ci.last_used_at FROM chat_identities ci JOIN users u ON u.id = ci.user_id {where} "
            "ORDER BY lower(u.username), ci.id"
        ),
        {"u": user.id},
    )
    return {
        "items": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "username": r.username,
                "provider": r.provider,
                "display": r.display,
                "linked_at": iso(r.linked_at),
                "last_used_at": iso(r.last_used_at),
            }
            for r in rows
        ]
    }


class CodeIn(BaseModel):
    provider: Literal["telegram"] = "telegram"


@router.post("/api/chat-links/codes", status_code=201)
async def create_code(user: SessionUser, rt: RT, session: DB, body: CodeIn | None = None) -> dict:
    code, expires = await linking.create_code(session, user.id, rt.clock.now())
    await session.commit()
    p = pollers(rt)
    bots = sorted({b.username for b in p.bots.values() if b.username})
    if not bots:
        bots = sorted(
            {
                r
                for r in (
                    await session.execute(
                        text(
                            "SELECT bot_username FROM telegram_poll_state "
                            "WHERE bot_username IS NOT NULL"
                        )
                    )
                ).scalars()
            }
        )
    return {
        "code": code,
        "expires_at": iso(expires),
        "bot_usernames": bots,
        "instructions": f"/link {code}",
    }


@router.delete("/api/chat-links/{link_id}", status_code=204)
async def delete_link(link_id: int, user: SessionUser, session: DB, ctx: Audit) -> Response:
    row = (
        await session.execute(
            text(
                "SELECT ci.id, ci.user_id, ci.display, u.username FROM chat_identities ci "
                "JOIN users u ON u.id = ci.user_id WHERE ci.id = :i"
            ),
            {"i": link_id},
        )
    ).first()
    if row is None or (row.user_id != user.id and user.role != "admin"):
        raise not_found("Chat account link")
    await session.execute(text("DELETE FROM chat_identities WHERE id = :i"), {"i": link_id})
    await audit.record(
        session,
        ctx,
        "chatack.unlink",
        audit.entity("chat_identity", link_id, row.display),
        {"user": [row.username, None]},
    )
    await session.commit()
    return Response(status_code=204)


@router.get("/api/chat-ack/telegram/status")
async def telegram_status(_: SessionAdmin, rt: RT) -> dict:
    return {"pollers": await pollers(rt).status()}


@router.post("/api/notification-channels/{channel_id}/telegram/take-over")
async def take_over(channel_id: int, _: SessionAdmin, rt: RT, session: DB, ctx: Audit) -> dict:
    ch = (
        await session.execute(
            text("SELECT id, name, type, config FROM notification_channels WHERE id = :i"),
            {"i": channel_id},
        )
    ).first()
    if ch is None or ch.type != "telegram":
        raise not_found("Telegram channel")
    token = str((ch.config or {}).get("bot_token") or "")
    if not token:
        raise ApiError(502, "telegram_error", "No bot token configured")
    await session.close()
    try:
        await pollers(rt).take_over(token)
    except TelegramApiError as exc:
        raise ApiError(502, "telegram_error", str(exc)) from None
    async with rt.sessionmaker() as s2:
        await audit.record(
            s2, ctx, "channel.telegram_take_over", audit.entity("channel", ch.id, ch.name)
        )
        await s2.commit()
    return {"ok": True}
