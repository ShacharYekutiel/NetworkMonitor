"""[3b] "Ask the network" against the QA Bedrock mock (developer B): the agent loop, SSE,
re-attach, cancel, limits, caps, concurrency, conversations, feedback, audit (title only),
redaction and retention (architecture § 14.3, § 14.6, § 14.7; api.md § [3b] Ask)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from netmon.assistant import agent
from tests.conftest import requires_db
from tests.fixtures_3b_agent import (
    ask,
    configure,
    done_message,
    parse_sse,
    seed,
)

pytestmark = requires_db


async def put_setting(rt: Any, key: str, value: Any) -> None:
    from netmon.services import settings as settings_svc

    async with rt.sessionmaker() as session:
        await settings_svc.put(session, key, value)
        await session.commit()


async def audit_rows(rt: Any, prefix: str = "assistant.") -> list[dict[str, Any]]:
    async with rt.sessionmaker() as session:
        rows = (
            await session.execute(
                text("SELECT action, changes, actor_name FROM audit_log WHERE action LIKE :p "
                     "ORDER BY id"),
                {"p": prefix + "%"},
            )
        ).mappings().all()  # fmt: skip
    return [dict(r) for r in rows]


def events_of(events: list[Any], name: str) -> list[Any]:
    return [d for _, e, d in events if e == name]


# --------------------------------------------------------------------------- the happy path
async def test_ask_one_tool_round_links_audit_ws(rt: Any, admin: httpx.AsyncClient, asst: Any):
    inv = await seed(admin, rt)
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    rt.hub.listeners.append(q)
    question = "Which devices are down? my password=Hunter2"
    status, events = await ask(admin, question)
    assert status == 200
    names = [e for _, e, _ in events]
    assert names[0] == "start" and names[-1] == "done"
    assert [i for i, _, _ in events] == list(range(1, len(events) + 1))
    start = events[0][2]
    # [3b Finding B] internal-IP masking defaults ON (the assistant row sets no override)
    assert start["language"] == "en" and start["redaction"] is True
    steps = events_of(events, "step")
    assert [s["state"] for s in steps] == ["running", "done"]
    assert steps[1]["tool"] == "list_problems" and steps[1]["ok"] is True and steps[1]["rows"] == 1
    sid, did = inv["core_ping"]["id"], inv["core"]["id"]
    assert f"sensor/{sid}" in steps[1]["refs"] and f"device/{did}" in steps[1]["refs"]
    assert steps[1]["label_key"] == "assistant.tool.list_problems"
    assert "".join(d["text"] for d in events_of(events, "delta"))
    msg = done_message(events)
    assert msg["status"] == "complete" and msg["role"] == "assistant"
    assert f"(nm:sensor/{sid})" in msg["content"] and f"(nm:device/{did})" in msg["content"]
    assert {s["ref"] for s in msg["sources"]} == {f"sensor/{sid}", f"device/{did}"}
    assert msg["model_calls"] == 2 and msg["tool_calls"] == 1
    assert msg["usage"]["input_tokens"] > 0 and msg["model_id"]
    assert msg["steps"][0]["tool"] == "list_problems" and msg["steps"][0]["state"] == "done"

    # what left the server: purpose marker, json tool results, cache points, no secret
    reqs = asst.requests("converse_stream")
    assert len(reqs) == 2 and all(r["sig"]["ok"] for r in reqs)
    assert reqs[0]["purpose"] == "ask" and reqs[0]["has_cache_point"]
    body = reqs[1]["body"]
    tr = body["messages"][-1]["content"][0]["toolResult"]
    assert tr["status"] == "success" and tr["content"][0]["json"]["tool"] == "list_problems"
    assert tr["content"][0]["json"]["untrusted_fields"] == ["message", "ack_comment"]
    assert "Hunter2" not in json.dumps(body)
    assert "(Answer in English.)" in body["messages"][0]["content"][0]["text"]
    assert set(reqs[0]["offered_tools"]) >= {"list_problems", "get_device"}
    assert "search_syslog" not in reqs[0]["offered_tools"]

    # stored conversation (question as typed), listing, WS to the user
    r = await admin.get("/api/assistant/conversations")
    conv = r.json()["items"][0]
    assert conv["message_count"] == 2 and conv["title"].startswith("Which devices are down?")
    assert "Hunter2" not in conv["title"]
    r = await admin.get(f"/api/assistant/conversations/{conv['id']}")
    full = r.json()
    assert [m["role"] for m in full["messages"]] == ["user", "assistant"]
    assert full["messages"][0]["content"] == question
    seen = []
    while not q.empty():
        seen.append(q.get_nowait())
    rt.hub.listeners.remove(q)
    ws = [m for m in seen if m["type"] == "assistant.message"]
    assert ws and ws[0]["data"]["status"] == "complete" and ws[0].get("user_id")

    # audit: question / tool call / answer — owner rule: never the question text, nor the
    # conversation title derived from it; a neutral title by the light model (purpose title)
    rows = await audit_rows(rt)
    acts = [r["action"] for r in rows]
    assert acts == ["assistant.question", "assistant.tool_call", "assistant.answer"]
    assert "Which devices" not in json.dumps(rows, ensure_ascii=False)
    assert "Hunter2" not in json.dumps(rows)
    qch = rows[0]["changes"]
    assert "question" not in qch and qch["title"][1] == "Assistant question"  # while generating
    async with rt.sessionmaker() as session:
        ename = (await session.execute(text(
            "SELECT entity_name FROM audit_log WHERE action = 'assistant.question'"
        ))).scalar()  # fmt: skip
    assert ename == "Assistant question"
    tc = rows[1]["changes"]
    assert tc["tool"][1] == "list_problems" and tc["rows"][1] == 1 and "arguments" not in tc
    assert tc["duration_ms"][1] is not None and tc["result_bytes"][1] > 0
    ans = rows[2]["changes"]
    assert ans["status"][1] == "complete" and ans["tools"][1] == ["list_problems"]
    assert ans["title"][1] == "Network status question"  # the mock's light-model title
    assert ans["input_tokens"][1] > 0 and ans["model_calls"][1] == 2
    assert ans["usage_estimated"][1] is False
    treq = [r for r in asst.requests("converse") if r["purpose"] == "title"]
    assert len(treq) == 1 and "toolConfig" not in treq[0]["body"]
    assert "Hunter2" not in json.dumps(treq[0]["body"])
    assert "Never include IP addresses, hostnames" in treq[0]["body"]["system"][0]["text"]

    # usage counters
    async with rt.sessionmaker() as session:
        u = (await session.execute(text(
            "SELECT sum(questions) q, sum(model_calls) m, sum(tool_calls) tc, "
            "sum(input_tokens) i FROM assistant_usage WHERE purpose = 'ask'"))).one()  # fmt: skip
    assert (u.q, u.m, u.tc) == (1, 2, 1) and u.i > 0
    async with rt.sessionmaker() as session:
        t = (await session.execute(text(
            "SELECT questions, model_calls, input_tokens, output_tokens FROM assistant_usage "
            "WHERE purpose = 'title'"))).one()  # fmt: skip
    assert (t.questions, t.model_calls, t.input_tokens, t.output_tokens) == (0, 1, 90, 5)


async def test_three_rounds_notes_and_followup(rt: Any, admin: httpx.AsyncClient, asst: Any):
    await seed(admin, rt)
    status, events = await ask(admin, "[qa:3rounds] investigate")
    msg = done_message(events)
    assert msg["status"] == "complete" and msg["model_calls"] == 4 and msg["tool_calls"] == 3
    assert [n["round"] for n in msg["notes"]] == [1, 2]
    assert msg["notes"][0]["text"].startswith("Let me check")
    assert [e["round"] for e in events_of(events, "round_end")] == [1, 2, 3]
    assert "core-sw" in msg["content"]
    # follow-up in the same conversation sees the earlier exchange
    asst.reset()
    status, events2 = await ask(admin, "and the other problems?", msg["conversation_id"])
    assert done_message(events2)["status"] == "complete"
    first = asst.requests("converse_stream")[0]["body"]["messages"]
    texts = [b.get("text") for m in first for b in m["content"] if "text" in b]
    assert any("investigate" in (t or "") for t in texts)
    assert any("toolResult" in b for m in first for b in m["content"])


async def test_parallel_tools_one_result_message(rt: Any, admin: httpx.AsyncClient, asst: Any):
    await seed(admin, rt)
    _, events = await ask(admin, "[qa:parallel] overview")
    msg = done_message(events)
    assert msg["tool_calls"] == 3 and len(msg["steps"]) == 3
    body = asst.requests("converse_stream")[1]["body"]
    results = body["messages"][-1]["content"]
    assert len(results) == 3 and all("toolResult" in b for b in results)
    ids = [b["toolUse"]["toolUseId"] for b in body["messages"][-2]["content"] if "toolUse" in b]
    assert [b["toolResult"]["toolUseId"] for b in results] == ids


async def test_tool_error_then_retry(rt: Any, admin: httpx.AsyncClient, asst: Any):
    await seed(admin, rt)
    _, events = await ask(admin, "[qa:retry] check")
    msg = done_message(events)
    assert msg["status"] == "complete"
    assert [s["ok"] for s in msg["steps"]] == [False, True]
    assert msg["steps"][0]["error"] == "invalid_arguments"
    assert "invalid_arguments" in msg["content"]
    tr = asst.requests("converse_stream")[1]["body"]["messages"][-1]["content"][0]["toolResult"]
    assert tr["status"] == "error" and tr["content"][0]["json"]["ok"] is False


async def test_unknown_tool_is_not_run(rt: Any, admin: httpx.AsyncClient, asst: Any):
    asst.add_scenario({"name": "b_unknown", "purpose": ["ask"], "match": r"\[b:unknown\]",
                       "turns": [{"tool_use": [{"name": "update_device", "input": {"id": 1}}]},
                                 {"text": "{{ $.error }}"}]})  # fmt: skip
    _, events = await ask(admin, "[b:unknown] go")
    msg = done_message(events)
    assert msg["content"] == "unknown_tool" and msg["steps"][0]["error"] == "unknown_tool"


# --------------------------------------------------------------------------- limits
async def test_tool_call_limit_final_call_without_tools(
    rt: Any, admin: httpx.AsyncClient, asst: Any
):
    await seed(admin, rt)
    await put_setting(rt, "assistant_max_tool_calls", 3)
    _, events = await ask(admin, "[qa:loop] forever")
    msg = done_message(events)
    assert msg["status"] == "limit_reached" and "limit_reached" in msg["notices"]
    assert msg["tool_calls"] == 3 and msg["content"].startswith("I stopped after")
    notices = events_of(events, "notice")
    assert notices[-1]["code"] == "limit_reached"
    reqs = asst.requests("converse_stream")
    last = reqs[-1]
    assert last["final_call"] is True and "toolChoice" not in last["body"]["toolConfig"]
    assert len(reqs) == 4  # 3 tool rounds + the final call


async def test_model_call_limit(rt: Any, admin: httpx.AsyncClient, asst: Any):
    await put_setting(rt, "assistant_max_model_calls", 2)
    _, events = await ask(admin, "[qa:loop] forever")
    msg = done_message(events)
    assert msg["status"] == "limit_reached" and msg["model_calls"] == 3


async def test_max_tokens_is_a_limit(rt: Any, admin: httpx.AsyncClient, asst: Any):
    _, events = await ask(admin, "[qa:maxtokens] long")
    msg = done_message(events)
    assert msg["status"] == "limit_reached"
    assert msg["notes"][0]["text"].startswith("This answer is cut")


async def test_timeout(rt: Any, admin: httpx.AsyncClient, asst: Any, monkeypatch: Any):
    monkeypatch.setitem(agent.RANGES, "assistant_question_timeout_s", (1, 300))
    await put_setting(rt, "assistant_question_timeout_s", 1)
    _, events = await ask(admin, "[qa:long] text")
    assert events_of(events, "error")[0]["code"] == "assistant_timeout"
    msg = done_message(events)
    assert msg["status"] == "error" and msg["error"]["code"] == "assistant_timeout"


# --------------------------------------------------------------------------- cancel / re-attach
async def _start_long(rt: Any, username: str = "admin") -> agent.Run:
    """Start a ~1 minute streamed answer directly (httpx's ASGI transport buffers a streamed
    body until it ends, so the HTTP stream cannot be read live in-process)."""
    from netmon.services.audit import AuditCtx

    async with rt.sessionmaker() as session:
        uid = (await session.execute(text("SELECT id FROM users WHERE username = :u"),
                                     {"u": username})).scalar()  # fmt: skip
    ctx = AuditCtx(actor_type="user", actor_user_id=uid, actor_name=username, enabled=True)
    run = await agent.start(rt, agent.AgentUser(id=uid, username=username), "[qa:long] stop me",
                            options=agent.AgentOptions(audit_ctx=ctx))  # fmt: skip
    for _ in range(200):
        if any(e[1] == "delta" for e in run.events):
            break
        await asyncio.sleep(0.05)
    assert not run.finished
    return run


async def test_cancel_mid_stream(rt: Any, admin: httpx.AsyncClient, asst: Any):
    run = await _start_long(rt)
    mid = run.message_id
    # a second question of the same user is refused while running
    r = await admin.post("/api/assistant/ask", json={"question": "Which devices are down?"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "assistant_busy"
    assert r.json()["error"]["details"] == [{"message_id": mid}]
    t0 = time.monotonic()
    r = await admin.post(f"/api/assistant/messages/{mid}/cancel")
    assert r.status_code == 200 and r.json() == {"status": "cancelled"}
    assert time.monotonic() - t0 < 2.0
    assert run.finished
    r = await admin.post(f"/api/assistant/messages/{mid}/cancel")  # idempotent
    assert r.json() == {"status": "cancelled"}
    conv = (await admin.get(f"/api/assistant/conversations/{run.conversation_id}")).json()
    msg = conv["messages"][-1]
    assert msg["status"] == "cancelled" and msg["content"].startswith("This")
    assert len(msg["content"]) < 300
    for _ in range(40):
        req = asst.requests("converse_stream")[-1]
        if req.get("closed_by_client"):
            break
        await asyncio.sleep(0.05)
    assert req.get("closed_by_client") and not req.get("complete")
    assert "assistant.cancel" in [a["action"] for a in await audit_rows(rt)]
    # § 14.3.3: no metadata event on a stopped stream → usage estimated, counted, marked
    assert msg["usage"]["input_tokens"] > 0 and msg["usage"]["output_tokens"] > 0
    assert "usage_estimated" in msg["notices"]
    async with rt.sessionmaker() as session:
        u = (await session.execute(text(
            "SELECT sum(input_tokens) i, sum(output_tokens) o FROM assistant_usage "
            "WHERE purpose = 'ask'"))).one()  # fmt: skip
    assert u.i >= msg["usage"]["input_tokens"] and u.o >= msg["usage"]["output_tokens"]
    ans = [a for a in await audit_rows(rt) if a["action"] == "assistant.answer"][-1]["changes"]
    assert ans["usage_estimated"][1] is True and ans["title"][1] == "Assistant question"
    assert not [r for r in asst.requests("converse") if r["purpose"] == "title"]  # no title call


async def test_reattach_with_last_event_id(rt: Any, admin: httpx.AsyncClient, asst: Any):
    run = await _start_long(rt)
    mid = run.message_id
    await asyncio.sleep(0.3)
    last = run.seq
    assert last >= 2

    async def reattach() -> str:
        r = await admin.get(f"/api/assistant/messages/{mid}/stream",
                            headers={"Last-Event-ID": str(last)}, timeout=60)  # fmt: skip
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        return r.text

    rtask = asyncio.create_task(reattach())
    await asyncio.sleep(0.5)
    await admin.post(f"/api/assistant/messages/{mid}/cancel")
    events = parse_sse(await asyncio.wait_for(rtask, 10))
    assert events[0][0] == last + 1  # replay continues after the last seen id
    assert events[-1][1] == "done" and events[-1][2]["message"]["status"] == "cancelled"
    # live subscriber from the start sees every event once, in order
    replay = [chunk async for chunk in agent.sse_events(run, 0)]
    ids = [e[0] for e in parse_sse(b"".join(replay).decode())]
    assert ids == list(range(1, run.seq + 1))
    # a finished message: `done` at once (also after the run left the registry)
    agent.runs_of(rt).runs.clear()
    r = await admin.get(f"/api/assistant/messages/{mid}/stream")
    ev = parse_sse(r.text)
    assert [e for _, e, _ in ev] == ["done"] and ev[0][2]["message"]["status"] == "cancelled"
    r = await admin.get("/api/assistant/messages/999999/stream")
    assert r.status_code == 404


# --------------------------------------------------------------------------- refusals / caps
async def test_global_concurrency_and_caps(rt: Any, admin: httpx.AsyncClient, asst: Any, app: Any):
    await put_setting(rt, "assistant_max_concurrent", 1)
    run = await _start_long(rt)
    start = {"message_id": run.message_id, "conversation_id": run.conversation_id}
    r = await admin.post("/api/users", json={"username": "admin2", "password": "Adm1n-pass-2",
                                             "role": "admin"})  # fmt: skip
    assert r.status_code == 201
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c2:
        await c2.post("/api/auth/login", json={"username": "admin2", "password": "Adm1n-pass-2"})
        r = await c2.post("/api/assistant/ask", json={"question": "Which devices are down?"})
        assert r.status_code == 429 and r.json()["error"]["code"] == "assistant_busy"
        await admin.post(f"/api/assistant/messages/{start['message_id']}/cancel")
        # not own: 404 / cancel unknown
        r = await c2.get(f"/api/assistant/conversations/{start['conversation_id']}")
        assert r.status_code == 404
        r = await c2.post(f"/api/assistant/messages/{start['message_id']}/cancel")
        assert r.status_code == 404
        r = await c2.post(
            "/api/assistant/ask",
            json={"question": "x", "conversation_id": start["conversation_id"]},
        )
        assert r.status_code == 404
    # daily question cap (the cancelled question counted)
    await put_setting(rt, "assistant_daily_questions_per_user", 1)
    r = await admin.post("/api/assistant/ask", json={"question": "Which devices are down?"})
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["code"] == "assistant_cap_reached" and err["details"][0]["cap"] == "user_daily"


async def test_access_and_feature_rules(
    rt: Any, admin: httpx.AsyncClient, viewer: httpx.AsyncClient, asst: Any
):
    r = await viewer.post("/api/assistant/ask", json={"question": "hi"})
    assert r.status_code == 403
    r = await viewer.get("/api/assistant/conversations")
    assert r.status_code == 403
    r = await admin.post("/api/tokens", json={"name": "t", "role": "admin", "expires_days": 30})
    assert r.status_code == 201, r.text
    tok = r.json()["token"]
    r = await admin.get("/api/assistant/conversations",
                        headers={"Authorization": f"Bearer {tok}"})  # fmt: skip
    assert r.status_code == 403
    r = await admin.post("/api/assistant/ask", json={"question": ""})
    assert r.status_code == 422
    r = await admin.post("/api/assistant/ask", json={"question": "x" * 2001})
    assert r.status_code == 422
    r = await admin.post("/api/assistant/ask", json={"question": "hi", "extra": 1})
    assert r.status_code == 422
    await rt.features.apply({"assistant": False})
    r = await admin.post("/api/assistant/ask", json={"question": "hi"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "feature_disabled"
    r = await admin.get("/api/assistant/conversations")
    assert r.status_code == 409


async def test_feature_off_cancels_running(rt: Any, admin: httpx.AsyncClient, asst: Any):
    run = await _start_long(rt)
    await rt.features.apply({"assistant": False})
    assert run.finished
    async with rt.sessionmaker() as session:
        st = (await session.execute(text("SELECT status FROM assistant_messages WHERE id = :i"),
                                    {"i": run.message_id})).scalar()  # fmt: skip
    assert st == "cancelled"


# --------------------------------------------------------------------------- errors
async def test_throttle_retried_then_success(rt: Any, admin: httpx.AsyncClient, asst: Any):
    asst.mode("throttle:1")
    _, events = await ask(admin, "[qa:notools] what is a vlan")
    assert events_of(events, "notice")[0]["code"] == "retrying"
    assert done_message(events)["status"] == "complete"


@pytest.mark.parametrize(
    ("mode", "code"),
    [("access_denied", "bedrock_access_denied"), ("model_access", "bedrock_model_access"),
     ("on_demand_unsupported", "bedrock_model_invalid")],
)  # fmt: skip
async def test_error_modes(rt: Any, admin: httpx.AsyncClient, asst: Any, mode: str, code: str):
    asst.mode(mode)
    _, events = await ask(admin, "[qa:notools] what is a vlan")
    err = events_of(events, "error")
    assert err and err[0]["code"] == code
    msg = done_message(events)
    assert msg["status"] == "error" and msg["error"]["code"] == code
    async with rt.sessionmaker() as session:
        n = (await session.execute(text(
            "SELECT sum(errors) FROM assistant_usage WHERE purpose = 'ask'"))).scalar()  # fmt: skip
    assert n == 1


# --------------------------------------------------------------------------- language, redaction
async def test_hebrew_question(rt: Any, admin: httpx.AsyncClient, asst: Any):
    await seed(admin, rt)
    _, events = await ask(admin, "אילו מכשירים למטה?")
    assert events[0][2]["language"] == "he"
    body = asst.requests("converse_stream")[0]["body"]
    assert "(Answer in Hebrew.)" in body["messages"][0]["content"][0]["text"]
    conv = (await admin.get("/api/assistant/conversations")).json()["items"][0]
    assert conv["language"] == "he"


async def test_internal_ip_redaction(rt: Any, admin: httpx.AsyncClient, asst: Any):
    inv = await seed(admin, rt)
    await configure(rt, {"redact_internal_ips": True})
    _, events = await ask(admin, "אילו מכשירים למטה? 10.0.0.9")
    assert events[0][2]["redaction"] is True
    msg = done_message(events)
    blob = json.dumps(asst.requests("converse_stream"))
    assert "10.0.0.9" not in blob and "10.0.0.10" not in blob and "IP-1" in blob
    assert "10.0.0.9" in msg["content"]  # the admin sees real addresses
    assert "".join(d["text"] for d in events_of(events, "delta")).count("IP-") == 0
    async with rt.sessionmaker() as session:
        m = (await session.execute(text(
            "SELECT redaction_map FROM assistant_conversations WHERE id = :c"),
            {"c": msg["conversation_id"]})).scalar()  # fmt: skip
    assert "10.0.0.9" in m.values()
    # run_agent for Telegram keeps the placeholders in text_redacted
    res = await agent.run_agent(
        rt,
        agent.AgentUser(id=1, username="admin"),
        "אילו מכשירים למטה?",
        options=agent.AgentOptions(purpose="chat_ask", channel="telegram", redact_ips=True),
    )
    assert res.status == "complete" and "IP-" in res.text_redacted and "IP-" not in res.text
    assert inv["core"]["host"] in res.text


# --------------------------------------------------------------------------- conversations
async def test_conversation_crud_feedback(rt: Any, admin: httpx.AsyncClient, asst: Any):
    _, events = await ask(admin, "[qa:notools] what is a vlan")
    msg = done_message(events)
    cid, mid = msg["conversation_id"], msg["id"]
    r = await admin.post(f"/api/assistant/messages/{mid}/feedback",
                         json={"rating": 1, "comment": "good"})  # fmt: skip
    assert r.status_code == 200 and r.json()["feedback"] == 1
    assert r.json()["feedback_comment"] == "good"
    r = await admin.post(f"/api/assistant/messages/{mid}/feedback", json={"rating": 0})
    assert r.json()["feedback"] is None
    r = await admin.post(f"/api/assistant/messages/{mid}/feedback", json={"rating": 2})
    assert r.status_code == 422
    r = await admin.patch(f"/api/assistant/conversations/{cid}", json={"title": "VLANs"})
    assert r.status_code == 200 and r.json()["title"] == "VLANs"
    r = await admin.get("/api/assistant/conversations", params={"q": "vla"})
    assert r.json()["total"] == 1
    r = await admin.get("/api/assistant/conversations", params={"kind": "explain"})
    assert r.json()["total"] == 0
    r = await admin.delete(f"/api/assistant/conversations/{cid}")
    assert r.status_code == 204
    r = await admin.get(f"/api/assistant/conversations/{cid}")
    assert r.status_code == 404
    _, _ = await ask(admin, "[qa:notools] what is lldp")
    _, _ = await ask(admin, "[qa:notools] what is lacp")
    r = await admin.delete("/api/assistant/conversations")
    assert r.json() == {"deleted": 2}
    acts = [a for a in await audit_rows(rt) if a["action"] in
            ("assistant.feedback", "assistant.conversation_delete")]  # fmt: skip
    assert [a["action"] for a in acts] == ["assistant.feedback", "assistant.feedback",
                                           "assistant.conversation_delete",
                                           "assistant.conversation_delete"]  # fmt: skip
    assert "comment" not in acts[0]["changes"] and acts[-1]["changes"]["all"][1] == 2


async def test_conversation_full(rt: Any, admin: httpx.AsyncClient, asst: Any):
    _, events = await ask(admin, "[qa:notools] what is a vlan")
    cid = done_message(events)["conversation_id"]
    async with rt.sessionmaker() as session:
        await session.execute(text("UPDATE assistant_conversations SET message_count = 80 "
                                   "WHERE id = :c"), {"c": cid})  # fmt: skip
        await session.commit()
    r = await admin.post("/api/assistant/ask", json={"question": "more", "conversation_id": cid})
    assert r.status_code == 409 and r.json()["error"]["code"] == "conversation_full"


async def test_retention_and_interrupted(rt: Any, admin: httpx.AsyncClient, asst: Any):
    _, events = await ask(admin, "[qa:notools] what is a vlan")
    msg = done_message(events)
    async with rt.sessionmaker() as session:
        await session.execute(text("UPDATE assistant_messages SET status = 'running' "
                                   "WHERE id = :m"), {"m": msg["id"]})  # fmt: skip
        await session.commit()
    assert await agent.mark_interrupted(rt) == 1
    conv = (await admin.get(f"/api/assistant/conversations/{msg['conversation_id']}")).json()
    last = conv["messages"][-1]
    assert last["status"] == "error" and last["error"]["code"] == "interrupted"
    assert await agent.purge_expired(rt) == 0
    async with rt.sessionmaker() as session:
        await session.execute(text("UPDATE assistant_conversations SET last_message_at = "
                                   "now() - interval '91 days'"))  # fmt: skip
        await session.commit()
    assert await agent.purge_expired(rt) == 1
    # audit rows survive the deletion
    assert any(a["action"] == "assistant.answer" for a in await audit_rows(rt))


async def test_status_provider_lists_tools(rt: Any, admin: httpx.AsyncClient, asst: Any):
    parts = [await p(1) for p in rt.assistant.status_providers]
    part = next(p for p in parts if "tools" in p)
    tools_ = {t["name"]: t for t in part["tools"]}
    assert len(tools_) == 25 and tools_["search_syslog"]["reason"] == "feature_off:syslog"
    assert part["running_message_id"] is None
