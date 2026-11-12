"""[3b] "Explain this alert" (`ai_explain_alert`, architecture § 14.8.1).

The context is gathered deterministically (`explain_gather`) and handed to the agent loop
(developer B's `netmon.assistant.agent.start`) as the first turn's JSON context; the answer
streams like `ask` and is stored as a conversation of kind `explain`, so the admin can ask
follow-up questions. Same `(sensor, problem event)` within 30 min returns the stored
explanation (the stream sends `start` and `done` at once)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from sqlalchemy import text

from netmon.api.errors import not_found
from netmon.assistant.features import common, explain_gather
from netmon.assistant.features.explain_gather import gather_plan

__all__ = ["gather_plan", "explain_stream", "PURPOSE_BLOCK"]

CACHE_FOR = timedelta(minutes=30)
EXTRA_TOOL_CALLS = 4

PURPOSE_BLOCK = (
    "Task: explain an alert to the administrators. NetworkMonitor gathered the context (the "
    "sensor, its device, preceding status changes, related events, logs, dependencies, "
    "neighbours and maintenance) as JSON in the first message; you may call at most 4 tools to "
    "look further. Answer with these Markdown sections: **What happened** (one line), **What "
    "preceded it** (timeline bullets with links), **Affected** (dependents / neighbours), "
    "**Likely cause** (clearly marked as a hypothesis with confidence low/medium/high) and "
    "**Next checks** (2–3 read-only suggestions; never a write action)."
)


async def _cached(session: Any, user_id: int, sensor_id: int, event_id: int, since: Any) -> Any:
    return (
        await session.execute(
            text(
                "SELECT c.id AS conversation_id, m.id AS message_id FROM assistant_conversations c "
                "JOIN assistant_messages m ON m.conversation_id = c.id AND m.role = 'assistant' "
                "AND m.seq = 2 WHERE c.kind = 'explain' AND c.user_id = :u AND c.ref_sensor_id = "
                ":s "
                "AND c.ref_event_id = :e AND m.status = 'complete' AND m.created_at >= :since "
                "ORDER BY m.created_at DESC LIMIT 1"
            ),
            {"u": user_id, "s": sensor_id, "e": event_id, "since": since},
        )
    ).first()


def _sse(seq: int, event: str, data: Any) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"id: {seq}\nevent: {event}\ndata: {payload}\n\n".encode()


async def _cached_stream(start: dict[str, Any], message: dict[str, Any]) -> AsyncIterator[bytes]:
    yield _sse(1, "start", start)
    yield _sse(2, "done", {"message": message})


async def explain_stream(
    rt: Any, user: Any, ctx: Any, sensor_id: int, event_id: int | None, refresh: bool
) -> AsyncIterator[bytes]:
    """Validate, gather and start; returns the SSE body (pre-stream errors raise ApiError)."""
    from netmon.assistant import agent, conversations

    async with rt.sessionmaker() as session:
        await common.require(rt, "ai_explain_alert", session)
        sensor = (
            await session.execute(
                text(
                    "SELECT s.id, s.name, s.status, d.name AS device_name FROM sensors s JOIN "
                    "devices d ON d.id = s.device_id WHERE s.id = :s"
                ),
                {"s": sensor_id},
            )
        ).first()
        if sensor is None:
            raise not_found("Sensor")
        ev = await explain_gather.problem_event(session, sensor_id, event_id)
        if event_id is not None and ev is None:
            raise not_found("Event")
        ev_id = ev.id if ev is not None else None
        if ev_id is not None and not refresh:
            hit = await _cached(session, user.id, sensor_id, ev_id, rt.clock.now() - CACHE_FOR)
            if hit is not None:
                msg = await conversations.message_full(session, hit.message_id, user.id)
                if msg is not None:
                    lang = (
                        await session.execute(
                            text("SELECT language FROM assistant_conversations WHERE id = :c"),
                            {"c": hit.conversation_id},
                        )
                    ).scalar() or "en"
                    start = {
                        "conversation_id": hit.conversation_id,
                        "message_id": hit.message_id,
                        "user_message_id": hit.message_id - 1,
                        "language": lang,
                        "redaction": False,
                        "cached": True,
                    }
                    return _cached_stream(start, msg)
        diff_open = await common.active(rt, "ai_config_diff", session)
        context = await explain_gather.collect(
            rt, session, sensor_id, ev, reduced=False, diff_gates_open=diff_open
        )
    status = (ev.new_status if ev is not None else sensor.status) or sensor.status
    lang = getattr(user, "language", "en") or "en"
    question = (
        f"הסבר את ההתראה: {sensor.device_name} / {sensor.name} ({status})"
        if lang == "he"
        else f"Explain this alert: {sensor.device_name} / {sensor.name} ({status})"
    )
    opts = agent.AgentOptions(
        purpose="explain",
        kind="explain",
        max_tool_calls=EXTRA_TOOL_CALLS,
        purpose_block=PURPOSE_BLOCK,
        extra_context=context,
        title=question[:80],
        audit_title="הסבר התראה" if lang == "he" else "Explain alert",
        ref_sensor_id=sensor_id,
        ref_event_id=ev_id,
        audit_ctx=ctx,
        audit_action="assistant.explain_alert",
        audit_extra={"sensor_id": sensor_id, "event_id": ev_id, "refresh": bool(refresh)},
        language=lang,
    )
    run = await agent.start(rt, _agent_user(user), question, options=opts)
    return agent.sse_events(run, 0)


def _agent_user(user: Any) -> Any:
    from netmon.assistant import agent

    if isinstance(user, agent.AgentUser):
        return user
    return agent.AgentUser(
        id=user.id,
        username=user.username,
        role=user.role,
        language=getattr(user, "language", "en") or "en",
    )
