"""[3b] "Ask the network": ask (SSE), re-attach, cancel, feedback, conversations
(api.md § [3b] Ask, stream, conversations).

Every endpoint is admin **and** session only (API tokens → 403) and needs the `assistant`
feature (409 `feature_disabled`). The run is a background task (`netmon.assistant.agent`);
the SSE response only subscribes to it, so closing the tab does not cancel the paid answer."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from netmon.api.deps import DB, RT, Audit, Page, request_audit_ctx, require_admin
from netmon.api.errors import ApiError, not_found
from netmon.assistant import agent, config, conversations
from netmon.models import User
from netmon.services import audit
from netmon.services.features import feature_disabled

SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


async def require_admin_session(
    request: Request, user: Annotated[User, Depends(require_admin)]
) -> User:
    """Admin role and an interactive session (API tokens are refused, § 14.0)."""
    auth = getattr(request.state, "auth", None)
    if auth is not None and auth.kind == "token":
        raise ApiError(403, "forbidden", "API tokens cannot be used here")
    return user


AdminSession = Annotated[User, Depends(require_admin_session)]
router = APIRouter(dependencies=[Depends(feature_disabled("assistant"))])


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: int | None = None
    question: str = Field(min_length=1, max_length=2000)
    # [3b owner] model chosen in the composer; must be one of the allowed chat models
    model_id: str | None = Field(None, max_length=256)


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: Literal[1, -1, 0]
    comment: str | None = Field(None, max_length=1000)


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)


def agent_user(user: User) -> agent.AgentUser:
    return agent.AgentUser(
        id=user.id,
        username=user.username,
        role=user.role,
        language=getattr(user, "language", "en") or "en",
    )


def sse_response(body: Any) -> StreamingResponse:
    return StreamingResponse(
        body, media_type="text/event-stream; charset=utf-8", headers=SSE_HEADERS
    )


async def chosen_model(rt: Any, model_id: str | None) -> str | None:
    """Validate the composer's model against the allowed chat models (`422
    model_not_allowed`). None / blank = the main model."""
    mid = (model_id or "").strip()
    if not mid:
        return None
    async with rt.sessionmaker() as session:
        cfg = await config.load(session)
    allowed = [m["id"] for m in config.chat_models_effective(cfg)]
    if mid not in allowed:
        raise ApiError(
            422,
            "model_not_allowed",
            "This model is not offered for the chat; choose one of the allowed models",
            [{"loc": ["body", "model_id"], "model_id": mid, "allowed": allowed}],
        )
    return mid


@router.post("/api/assistant/ask")
async def ask(body: AskIn, user: AdminSession, request: Request, rt: RT) -> StreamingResponse:
    model_id = await chosen_model(rt, body.model_id)
    run = await agent.start(
        rt,
        agent_user(user),
        body.question,
        conversation_id=body.conversation_id,
        options=agent.AgentOptions(audit_ctx=request_audit_ctx(request, rt), model_id=model_id),
    )
    return sse_response(agent.sse_events(run, 0))


@router.get("/api/assistant/messages/{message_id}/stream")
async def stream(
    message_id: int,
    user: AdminSession,
    rt: RT,
    session: DB,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    run = agent.runs_of(rt).get(message_id)
    if run is not None and run.user_id == user.id:
        try:
            last = int(last_event_id or 0)
        except ValueError:
            last = 0
        return sse_response(agent.sse_events(run, last))
    msg = await conversations.message_full(session, message_id, user.id)
    if msg is None or msg["role"] != "assistant":
        raise not_found("Message")
    await session.close()
    return sse_response(agent.done_only(msg))


@router.post("/api/assistant/messages/{message_id}/cancel")
async def cancel(message_id: int, user: AdminSession, rt: RT) -> dict:
    status = await agent.cancel(rt, user.id, message_id)
    if status is None:
        raise not_found("Message")
    return {"status": status}


@router.post("/api/assistant/messages/{message_id}/feedback")
async def feedback(
    message_id: int, body: FeedbackIn, user: AdminSession, session: DB, ctx: Audit
) -> dict:
    uid = user.id
    row = await conversations.get_message_row(session, message_id, uid)
    if row is None or row["role"] != "assistant":
        raise not_found("Message")
    await conversations.set_feedback(session, message_id, body.rating, body.comment)
    await audit.record(
        session,
        ctx,
        "assistant.feedback",
        audit.entity("assistant_message", message_id),
        {"message_id": message_id, "rating": body.rating},
    )
    await session.commit()
    out = await conversations.message_full(session, message_id, uid)
    assert out is not None
    return out


@router.get("/api/assistant/conversations")
async def list_conversations(
    user: AdminSession,
    session: DB,
    pg: Page,
    q: Annotated[str | None, Query(max_length=200)] = None,
    kind: Literal["chat", "explain", "rule_draft", "template_draft"] | None = None,
) -> dict:
    return await conversations.list_conversations(
        session, user.id, q=q, kind=kind, page=pg.page, page_size=pg.page_size
    )


@router.get("/api/assistant/conversations/{conversation_id}")
async def get_conversation(conversation_id: int, user: AdminSession, session: DB) -> dict:
    out = await conversations.conversation_full(session, conversation_id, user.id)
    if out is None:
        raise not_found("Conversation")
    return out


@router.patch("/api/assistant/conversations/{conversation_id}")
async def patch_conversation(
    conversation_id: int, body: ConversationPatch, user: AdminSession, session: DB
) -> dict:
    uid = user.id
    conv = await conversations.get_conversation(session, conversation_id, uid)
    if conv is None:
        raise not_found("Conversation")
    title = " ".join(body.title.split())
    if not title:
        raise ApiError(
            422,
            "validation_error",
            "Invalid title",
            [{"loc": ["body", "title"], "msg": "must not be blank"}],
        )
    await conversations.rename(session, conversation_id, title)
    await session.commit()
    conv = await conversations.get_conversation(session, conversation_id, uid)
    assert conv is not None
    return conversations.conversation_out(conv)


async def _cancel_running(rt: Any, user_id: int, conversation_id: int | None) -> None:
    runs = agent.runs_of(rt)
    for r in list(runs.runs.values()):
        if r.user_id != user_id or r.finished:
            continue
        if conversation_id is None or r.conversation_id == conversation_id:
            await agent.cancel(rt, user_id, r.message_id)


@router.delete("/api/assistant/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: int, user: AdminSession, rt: RT, session: DB, ctx: Audit
) -> Response:
    uid = user.id
    conv = await conversations.get_conversation(session, conversation_id, uid)
    if conv is None:
        raise not_found("Conversation")
    await session.rollback()
    await _cancel_running(rt, uid, conversation_id)
    await conversations.delete_conversations(session, uid, conversation_id)
    await audit.record(
        session,
        ctx,
        "assistant.conversation_delete",
        audit.entity("assistant_conversation", conversation_id),
        {"conversation_id": conversation_id},
    )
    await session.commit()
    return Response(status_code=204)


@router.delete("/api/assistant/conversations")
async def delete_all(user: AdminSession, rt: RT, session: DB, ctx: Audit) -> dict:
    uid = user.id
    await _cancel_running(rt, uid, None)
    n = await conversations.delete_conversations(session, uid)
    await audit.record(
        session,
        ctx,
        "assistant.conversation_delete",
        audit.entity("assistant_conversation", None),
        {"all": n},
    )
    await session.commit()
    return {"deleted": n}
