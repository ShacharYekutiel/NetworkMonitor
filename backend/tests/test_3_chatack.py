"""[3] Chat acknowledge against the database: the ack page API (resolve / acknowledge,
idempotent, stale / expired / invalid, admin only, API tokens refused), account linking with
one-time codes through the Telegram poller, Telegram presses (linked admin, not linked, not
admin, stale, already acknowledged, noop, rate limit) with a mocked Bot API, the long-poll loop
with a persisted offset and the 409 conflict state, and the senders' ack buttons."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest

from netmon.chatack import service
from netmon.chatack.telegram_poller import TelegramPollers, _Bot
from tests.conftest import _plain, make_device, make_sensor, requires_db

pytestmark = requires_db

BOT_TOKEN = "123456:TEST_TOKEN_valid_0123456789abcdef"


async def _db(rt: Any) -> asyncpg.Connection:
    return await asyncpg.connect(_plain(rt.settings.database_url))


async def _problem(admin: httpx.AsyncClient, rt: Any, name: str = "core") -> tuple[int, int]:
    d = await make_device(admin, name=name)
    s = await make_sensor(admin, d["id"], "ping", {})
    conn = await _db(rt)
    try:
        await conn.execute("UPDATE sensors SET status = 'down' WHERE id = $1", s["id"])
        eid = await conn.fetchval(
            "INSERT INTO events (time, kind, sensor_id, device_id, sensor_name, device_name, "
            "old_status, new_status, message) VALUES (now(), 'status_change', $1, $2, 'ping', "
            "$3, 'up', 'down', 'timeout') RETURNING id",
            s["id"],
            d["id"],
            name,
        )
    finally:
        await conn.close()
    return s["id"], int(eid)


@pytest.fixture
async def ack(rt: Any, admin: httpx.AsyncClient, feats: Any) -> Any:
    feats(chat_ack=True, audit=True)
    return rt


async def test_gating_and_tokens_refused(admin: httpx.AsyncClient, feats: Any, rt: Any) -> None:
    r = await admin.post("/api/chat-ack/resolve", json={"token": "a1.x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "feature_disabled"
    assert (await admin.get("/api/chat-links")).status_code == 409
    feats(chat_ack=True)
    r = await admin.post("/api/tokens", json={"name": "t", "role": "admin", "expires_days": 30})
    assert r.status_code == 201, r.text
    tok = r.json()["token"]
    transport = httpx.ASGITransport(app=admin._transport.app)  # type: ignore[attr-defined]
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.get("/api/chat-links", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403


async def test_ack_page_flow(
    admin: httpx.AsyncClient, viewer: httpx.AsyncClient, ack: Any, rt: Any
) -> None:
    sid, eid = await _problem(admin, rt)
    token = service.make_token(rt, sid, eid, 1)
    r = await viewer.post("/api/chat-ack/resolve", json={"token": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["active"] is True and body["event_id"] == eid
    assert body["can_acknowledge"] is False  # viewer
    assert body["device_name"] == "core" and body["sensor"]["id"] == sid
    assert (
        await viewer.post("/api/chat-ack/acknowledge", json={"token": token})
    ).status_code == 403
    r = await admin.post("/api/chat-ack/resolve", json={"token": token})
    assert r.json()["can_acknowledge"] is True
    r = await admin.post("/api/chat-ack/acknowledge", json={"token": token, "comment": "On it"})
    assert r.status_code == 200, r.text
    assert r.json()["acknowledgement"]["by"] == "admin"
    assert r.json()["acknowledgement"]["comment"] == "On it"
    r = await admin.post("/api/chat-ack/acknowledge", json={"token": token})
    assert r.status_code == 200  # idempotent
    r = await admin.post("/api/chat-ack/resolve", json={"token": token})
    assert r.json()["acknowledgement"]["by"] == "admin" and r.json()["can_acknowledge"] is False
    conn = await _db(rt)
    try:
        rows = await conn.fetch("SELECT changes FROM audit_log WHERE action = 'sensor.acknowledge'")
        n_ev = await conn.fetchval(
            "SELECT count(*) FROM events WHERE kind = 'acknowledge' AND sensor_id = $1", sid
        )
    finally:
        await conn.close()
    assert len(rows) == 1 and json.loads(rows[0]["changes"])["via"] == [None, "link"]
    assert n_ev == 1
    # invalid / expired / stale
    assert (
        await admin.post("/api/chat-ack/resolve", json={"token": token[:-1] + "x"})
    ).status_code == 400
    expired = service.tokens.encode(service.key_of(rt), sid, eid, service.now_min(rt) - 1)
    r = await admin.post("/api/chat-ack/resolve", json={"token": expired})
    assert r.status_code == 410 and r.json()["error"]["code"] == "ack_expired"
    sid2, eid2 = await _problem(admin, rt, "edge")
    stale = service.make_token(rt, sid2, eid2 + 1000)
    r = await admin.post("/api/chat-ack/resolve", json={"token": stale})
    assert r.status_code == 200 and r.json()["active"] is False
    r = await admin.post("/api/chat-ack/acknowledge", json={"token": stale})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ack_stale"


class FakeBotApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []
        self.conflict = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "NetMonOpsBot"}})
        if method == "getUpdates":
            if self.conflict:
                return httpx.Response(
                    409, json={"ok": False, "description": "Conflict: can't use getUpdates"}
                )
            ups = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]
            return httpx.Response(200, json={"ok": True, "result": ups})
        return httpx.Response(200, json={"ok": True, "result": True})

    def of(self, method: str) -> list[dict[str, Any]]:
        return [b for m, b in self.calls if m == method]


@pytest.fixture
async def bot(rt: Any, ack: Any) -> AsyncIterator[tuple[TelegramPollers, _Bot, FakeBotApi]]:
    api = FakeBotApi()
    p = TelegramPollers(rt)
    p.transport = httpx.MockTransport(api.handler)
    b = _Bot(BOT_TOKEN, [1])
    b.username = "NetMonOpsBot"
    yield p, b, api
    await p.stop()


def _msg(text: str, uid: int = 555, username: str = "jdoe") -> dict[str, Any]:
    return {
        "message_id": 1,
        "from": {"id": uid, "username": username},
        "chat": {"id": uid, "type": "private"},
        "text": text,
    }


def _press(data: str, uid: int = 555) -> dict[str, Any]:
    return {
        "id": "cq1",
        "from": {"id": uid, "username": "jdoe"},
        "data": data,
        "message": {"message_id": 77, "chat": {"id": -100}},
    }


async def test_linking_and_presses(
    admin: httpx.AsyncClient, viewer: httpx.AsyncClient, rt: Any, bot: Any
) -> None:
    p, b, api = bot
    sid, eid = await _problem(admin, rt)
    token = service.make_token(rt, sid, eid)
    # not linked
    await p.handle_press(b, _press(token))
    ans = api.of("answerCallbackQuery")[-1]
    assert ans["show_alert"] is True and "not linked" in ans["text"]
    # viewer links → not admin
    r = await viewer.post("/api/chat-links/codes", json={"provider": "telegram"})
    assert r.status_code == 201, r.text
    code = r.json()["code"]
    assert code.startswith("NM-") and r.json()["instructions"] == f"/link {code}"
    await p.handle_message(b, _msg(f"/link@NetMonOpsBot {code}", uid=777, username="viewer"))
    assert "Linked to NetworkMonitor user viewer" in api.of("sendMessage")[-1]["text"]
    await p.handle_message(b, _msg(f"/link {code}", uid=778))  # single use
    assert "invalid or expired" in api.of("sendMessage")[-1]["text"]
    await p.handle_press(b, _press(token, uid=777))
    assert api.of("answerCallbackQuery")[-1]["text"] == "Only administrators can acknowledge"
    links = (await viewer.get("/api/chat-links")).json()["items"]
    assert [(x["username"], x["display"]) for x in links] == [("viewer", "@viewer")]
    assert (await viewer.get("/api/chat-links", params={"all": "true"})).status_code == 403
    # admin links and acknowledges
    code = (await admin.post("/api/chat-links/codes", json={})).json()["code"]
    await p.handle_message(b, _msg(f"/link {code.lower()}"))
    await p.handle_message(b, _msg("hello there"))  # ignored, no reply
    n_sent = len(api.of("sendMessage"))
    await p.handle_press(b, _press(token))
    ans = api.of("answerCallbackQuery")[-1]
    assert ans == {"callback_query_id": "cq1", "text": "Acknowledged", "show_alert": False}
    edit = api.of("editMessageReplyMarkup")[-1]
    btn = edit["reply_markup"]["inline_keyboard"][0][0]
    assert btn["callback_data"] == "noop" and btn["text"].startswith("✓ admin ")
    assert edit["chat_id"] == -100 and edit["message_id"] == 77
    assert len(api.of("sendMessage")) == n_sent
    s = (await admin.get(f"/api/sensors/{sid}")).json()
    assert s["acknowledgement"]["by"] == "admin"
    assert s["acknowledgement"]["comment"] == "Acknowledged from Telegram"
    # already acknowledged / stale / invalid / noop
    await p.handle_press(b, _press(token))
    assert api.of("answerCallbackQuery")[-1]["text"] == "Already acknowledged by admin"
    await p.handle_press(b, _press(service.make_token(rt, sid, eid + 1000)))
    assert api.of("answerCallbackQuery")[-1]["text"] == "This problem is no longer active"
    await p.handle_press(b, _press("a1.garbage"))
    assert api.of("answerCallbackQuery")[-1]["text"] == "This button is not valid"
    await p.handle_press(b, _press("noop"))
    assert api.of("answerCallbackQuery")[-1] == {"callback_query_id": "cq1"}
    conn = await _db(rt)
    try:
        presses = await conn.fetch(
            "SELECT reason FROM audit_log WHERE action = 'chatack.press' ORDER BY id"
        )
        acks = await conn.fetch("SELECT changes FROM audit_log WHERE action = 'sensor.acknowledge'")
        links_n = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'chatack.link'"
        )
    finally:
        await conn.close()
    reasons = [x["reason"] for x in presses]
    assert reasons == ["not_linked", "not_admin", "ok", "already", "stale", "invalid"]
    assert json.loads(acks[0]["changes"])["via"] == [None, "telegram"]
    assert links_n == 2
    # rate limit: 10 presses per user per minute
    for _ in range(11):
        await p.handle_press(b, _press("a1.garbage", uid=999))
    assert "Too many" in api.of("answerCallbackQuery")[-1]["text"]
    # /unlink and admin removal
    await p.handle_message(b, _msg("/unlink"))
    assert "no longer linked" in api.of("sendMessage")[-1]["text"]
    all_links = (await admin.get("/api/chat-links", params={"all": "true"})).json()["items"]
    assert [x["username"] for x in all_links] == ["viewer"]
    r = await admin.delete(f"/api/chat-links/{all_links[0]['id']}")
    assert r.status_code == 204
    assert (await admin.get("/api/chat-links", params={"all": "true"})).json()["items"] == []


async def test_poll_loop_offset_and_conflict(admin: httpx.AsyncClient, rt: Any, bot: Any) -> None:
    p, _, api = bot
    saved = rt.telegram_pollers
    rt.telegram_pollers = p
    try:
        await _poll_loop(admin, rt, p, api)
    finally:
        rt.telegram_pollers = saved


async def _poll_loop(admin: httpx.AsyncClient, rt: Any, p: Any, api: FakeBotApi) -> None:
    r = await admin.post(
        "/api/notification-channels",
        json={
            "name": "tg",
            "type": "telegram",
            "config": {"bot_token": BOT_TOKEN, "chat_id": "-100", "ack_buttons": True},
        },
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    code = (await admin.post("/api/chat-links/codes", json={})).json()["code"]
    api.updates = [
        {"update_id": 41, "message": _msg(f"/link {code}")},
        {"update_id": 42, "message": _msg("noise")},
    ]
    await p.feature_changed("chat_ack", True)
    assert list(p.bots) and p.bots[next(iter(p.bots))].channel_ids == [cid]
    for _ in range(100):
        polls = api.of("getUpdates")
        if any(x.get("offset") == 43 for x in polls):
            break
        await asyncio.sleep(0.05)
    polls = api.of("getUpdates")
    assert polls[0]["allowed_updates"] == ["callback_query", "message"]
    assert polls[0]["timeout"] == 50 and any(x["offset"] == 43 for x in polls)
    conn = await _db(rt)
    try:
        row = await conn.fetchrow("SELECT * FROM telegram_poll_state")
        n_ident = await conn.fetchval("SELECT count(*) FROM chat_identities")
    finally:
        await conn.close()
    assert row["next_offset"] == 43 and row["bot_username"] == "NetMonOpsBot"
    assert BOT_TOKEN not in json.dumps(dict(row), default=str)
    assert n_ident == 1
    st = (await admin.get("/api/chat-ack/telegram/status")).json()["pollers"]
    assert st[0]["bot_username"] == "NetMonOpsBot" and st[0]["channel_ids"] == [cid]
    r = (await admin.post("/api/chat-links/codes", json={})).json()
    assert r["bot_usernames"] == ["NetMonOpsBot"]
    # a webhook / second consumer → state error, never deleteWebhook on its own
    api.conflict = True
    for _ in range(100):
        st = (await admin.get("/api/chat-ack/telegram/status")).json()["pollers"]
        if st[0]["state"] == "error":
            break
        await asyncio.sleep(0.05)
    assert st[0]["state"] == "error" and "dedicated bot" in st[0]["last_error"]
    assert not api.of("deleteWebhook")
    # explicit take-over (admin action)
    api.conflict = False
    r = await admin.post(f"/api/notification-channels/{cid}/telegram/take-over")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert api.of("deleteWebhook")
    await p.feature_changed("chat_ack", False)
    assert not p.bots


async def test_senders_ack_buttons(rt: Any, ack: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from netmon.alerts import engine

    sent: dict[str, Any] = {}

    async def fake_tg(
        cfg: Any, text: str, parse_mode: Any = None, reply_markup: Any = None
    ) -> None:
        sent["tg"] = (text, reply_markup)

    async def fake_hook(cfg: Any, payload: Any) -> None:
        sent["hook"] = payload

    monkeypatch.setattr(engine, "send_telegram", fake_tg)
    monkeypatch.setattr(engine, "send_webhook", fake_hook)
    payload = engine.build_payload(
        {
            "id": 991,
            "time": "2026-11-08T10:00:00Z",
            "sensor_id": 7,
            "old_status": "up",
            "new_status": "down",
            "message": "m",
        },
        {"id": 7, "name": "ping", "type": "ping"},
        {"id": 1, "name": "core", "host": "10.0.0.1"},
        {"id": 1, "name": "g"},
        {"id": 1, "name": "r"},
        "https://nm.example",
        stage="problem",
    )
    payload = service.decorate_payload(rt, payload, {"public_base_url": "https://nm.example"})
    await engine.send_to_channel(
        {"type": "telegram", "config": {"bot_token": BOT_TOKEN, "ack_buttons": True}},
        payload,
        {},
        "en",
    )
    text, markup = sent["tg"]
    data = markup["inline_keyboard"][0][0]["callback_data"]
    assert data == payload["ack_token"] and len(data) == 41
    await engine.send_to_channel({"type": "webhook", "config": {"url": "x"}}, payload, {}, "en")
    assert "ack_token" not in sent["hook"]
    assert sent["hook"]["ack_url"] == f"https://nm.example/ack#{payload['ack_token']}"
    rec = engine.build_payload(
        {
            "id": 992,
            "time": "2026-11-08T10:05:00Z",
            "sensor_id": 7,
            "old_status": "down",
            "new_status": "up",
            "message": "m",
        },
        None,
        None,
        None,
        None,
        "https://nm.example",
        stage="recovery",
    )
    rec = service.decorate_payload(rt, rec, {"public_base_url": "https://nm.example"})
    await engine.send_to_channel(
        {"type": "telegram", "config": {"bot_token": BOT_TOKEN, "ack_buttons": True}},
        rec,
        {},
        "en",
    )
    assert sent["tg"][1] is None
