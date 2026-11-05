"""[2a] Telegram channel: sender against a local Bot API mock, texts, API secret handling."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from netmon.alerts import telegram, templates
from netmon.alerts.telegram import TelegramError, scrub, send_telegram
from tests.conftest import requires_db

VALID = "123456:TEST_TOKEN_valid_0123456789abcdef"
OTHER = "654321:TEST_TOKEN_other_0123456789abcdef"


class TelegramMock:
    """Minimal Bot API: POST /bot<token>/sendMessage (behaviour as tests/sim/telegram_mock.py)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.port = 0
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server
        self._server.close()
        await self._server.wait_closed()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        lines = head.decode().split("\r\n")
        _method, path, _ = lines[0].split(" ", 2)
        headers = {
            k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)
        }
        raw = await reader.readexactly(int(headers.get("content-length", 0)))
        body = json.loads(raw) if raw else {}
        token = path.split("/")[1][3:] if path.startswith("/bot") else ""
        self.requests.append({"path": path, "token": token, "json": body})
        status, resp, ctype = 200, {}, "application/json"
        if token == "html_error":
            status, resp, ctype = 502, "<html>bad gateway</html>", "text/html"
        elif token == "echo":
            status, resp = 400, {"ok": False, "error_code": 400, "description": f"bad {path}"}
        elif token != VALID:
            status, resp = 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        elif body.get("chat_id") in ("-1001", "@netmon_test"):
            resp = {"ok": True, "result": {"message_id": len(self.requests), "text": body["text"]}}
        elif body.get("chat_id") == "429":
            status = 429
            resp = {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 5",
                "parameters": {"retry_after": 5},
            }
        elif body.get("chat_id") == "slow":
            await asyncio.sleep(5)
            resp = {"ok": True, "result": {}}
        else:
            status = 400
            resp = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        data = resp.encode() if isinstance(resp, str) else json.dumps(resp).encode()
        try:
            writer.write(
                f"HTTP/1.1 {status} X\r\nContent-Type: {ctype}\r\nContent-Length: {len(data)}\r\n"
                f"Connection: close\r\n\r\n".encode()
                + data
            )
            await writer.drain()
        except ConnectionError:
            pass
        writer.close()


@pytest.fixture
async def tg() -> AsyncIterator[TelegramMock]:
    m = TelegramMock()
    await m.start()
    old = telegram.api_base()
    telegram.configure(m.base)
    yield m
    telegram.configure(old)
    await m.stop()


def cfg(chat: str = "-1001", token: str = VALID, **kw: Any) -> dict[str, Any]:
    return {"bot_token": token, "chat_id": chat, "parse_mode": "HTML", **kw}


async def test_send_success_and_body(tg: TelegramMock) -> None:
    await send_telegram(cfg(disable_notification=True), "<b>hi</b>", "HTML")
    req = tg.requests[-1]
    assert req["path"] == f"/bot{VALID}/sendMessage"
    assert req["json"] == {
        "chat_id": "-1001",
        "text": "<b>hi</b>",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": True,
    }
    await send_telegram(cfg("@netmon_test"), "plain", None)
    assert "parse_mode" not in tg.requests[-1]["json"]


@pytest.mark.parametrize(
    ("chat", "token", "message"),
    [
        ("404", VALID, "Bad Request: chat not found"),
        ("-1001", OTHER, "Unauthorized"),
        ("429", VALID, "Too Many Requests: retry after 5"),
        ("-1001", "html_error", "Telegram returned HTTP 502"),
    ],
)
async def test_send_errors(tg: TelegramMock, chat: str, token: str, message: str) -> None:
    with pytest.raises(TelegramError) as ei:
        await send_telegram(cfg(chat, token), "x")
    assert str(ei.value) == message


async def test_timeout_and_scrubbing(
    tg: TelegramMock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(telegram, "TIMEOUT_S", 0.5)
    with pytest.raises(TelegramError) as ei:
        await send_telegram(cfg("slow"), "x")
    assert str(ei.value) == "Telegram request timed out"
    # Telegram's description echoing the URL -> token replaced
    with pytest.raises(TelegramError) as ei:
        await send_telegram(cfg(token="echo"), "x")
    assert "echo" not in str(ei.value) and "/bot***/sendMessage" in str(ei.value)
    # transport error: closed port
    telegram.configure("http://127.0.0.1:1")
    with pytest.raises(TelegramError) as ei:
        await send_telegram(cfg(), "x")
    assert VALID not in str(ei.value)
    assert VALID not in caplog.text
    assert scrub(f"https://x/bot{VALID}/send", VALID) == "https://x/bot***/send"
    assert logging.getLogger("httpx").level == logging.WARNING


def test_telegram_texts() -> None:
    payload = {
        "event": "sensor.status_changed",
        "event_id": 1,
        "time": "2026-11-04T12:00:00Z",
        "old_status": "up",
        "new_status": "down",
        "message": "a < b & c",
        "sensor": {"id": 7, "name": "Ping", "type": "ping"},
        "device": {"id": 3, "name": "core<sw>", "host": "10.0.0.2"},
        "group": {"id": 1, "name": "Default"},
        "rule": {"id": 2, "name": "All down"},
        "url": "http://netmon.local:8080/sensors/7?a=1&b=2",
    }
    t = templates.telegram_text(payload, "en", "HTML")
    lines = t.split("\n")
    assert lines[0] == "<b>[NetworkMonitor] DOWN: core&lt;sw&gt; / Ping</b>"
    assert "<b>Message:</b> a &lt; b &amp; c" in lines
    assert lines[-1] == (
        '<a href="http://netmon.local:8080/sensors/7?a=1&amp;b=2">Open in NetworkMonitor</a>'
    )
    plain = templates.telegram_text(payload, "he", None)
    assert plain.startswith("[NetworkMonitor] תקלה: core<sw> / Ping\n\n")
    test = templates.telegram_text(
        {"event": "test", "time": "x", "message": "Test notification", "url": None}, "en", "HTML"
    )
    assert test == (
        "<b>[NetworkMonitor] Test notification</b>\n"
        "This is a test notification from NetworkMonitor."
    )
    long_payload = {**payload, "message": ("x" * 100 + "\n") * 100}
    long = templates.telegram_text(long_payload, "en", None)
    assert len(long) <= 4096 and long.endswith("…")
    # discovery payload
    disc = {
        "event": "discovery.new_hosts",
        "message": 'Discovery "LAN": 52 new hosts',
        "profile": {"id": 2, "name": "LAN"},
        "hosts": [
            {
                "ip": "10.0.0.5",
                "hostname": "brn.lan",
                "device_class": "printer",
                "vendor": "brother",
            },
            {"ip": "10.0.0.9", "hostname": None, "device_class": "unknown", "vendor": None},
        ],
        "url": None,
    }
    t = templates.telegram_text(disc, "he", "HTML").split("\n")
    assert t[0] == "<b>[NetworkMonitor] התגלו מארחים חדשים: LAN (52)</b>"
    assert t[1] == "10.0.0.5 — brn.lan — מדפסת — brother"
    assert t[2] == "10.0.0.9 — לא ידוע"
    assert t[3] == "ועוד 50"
    assert templates.subject(disc, "en") == "[NetworkMonitor] New hosts discovered: LAN (52)"
    assert "+50 more" in templates.text_body(disc, "en")


@requires_db
async def test_telegram_channel_api(admin: httpx.AsyncClient, tg: TelegramMock) -> None:
    body = {"name": "TG", "type": "telegram", "config": {"chat_id": "-1001"}}
    r = await admin.post("/api/notification-channels", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["loc"] == ["body", "config", "bot_token"]
    body["config"]["bot_token"] = ""
    assert (await admin.post("/api/notification-channels", json=body)).status_code == 422
    for bad in ({"chat_id": -1001}, {"chat_id": "bad id"}, {"parse_mode": "Markdown"}, {"x": 1}):
        r = await admin.post(
            "/api/notification-channels",
            json={**body, "config": {"bot_token": VALID, "chat_id": "-1001", **bad}},
        )
        assert r.status_code == 422, bad
    r = await admin.post(
        "/api/notification-channels",
        json={**body, "config": {"bot_token": "nope", "chat_id": "-1001"}},
    )
    assert r.status_code == 422
    body["config"] = {"bot_token": VALID, "chat_id": "-1001", "bot_token_set": False}
    r = await admin.post("/api/notification-channels", json=body)
    assert r.status_code == 201, r.text
    ch = r.json()
    assert ch["config"] == {
        "bot_token": None,
        "bot_token_set": True,
        "chat_id": "-1001",
        "parse_mode": "HTML",
        "disable_notification": False,
    }
    assert VALID not in r.text
    r = await admin.post(f"/api/notification-channels/{ch['id']}/test")
    assert r.status_code == 200 and r.json() == {"ok": True, "message": "sent"}
    sent = tg.requests[-1]["json"]
    assert sent["text"].startswith("<b>[NetworkMonitor] Test notification</b>")
    # PATCH without the token keeps it; a new chat id
    r = await admin.patch(
        f"/api/notification-channels/{ch['id']}",
        json={"config": {"chat_id": "404", "parse_mode": None}},
    )
    assert r.status_code == 200 and r.json()["config"]["bot_token_set"] is True
    assert r.json()["config"]["parse_mode"] is None
    r = await admin.post(f"/api/notification-channels/{ch['id']}/test")
    assert r.status_code == 502
    assert r.json()["error"] == {
        "code": "telegram_error",
        "message": "Bad Request: chat not found",
        "details": None,
    }
    assert "parse_mode" not in tg.requests[-1]["json"]
    r = await admin.patch(
        f"/api/notification-channels/{ch['id']}",
        json={"config": {"chat_id": "-1001", "bot_token": ""}},
    )
    assert r.status_code == 422
    r = await admin.patch(
        f"/api/notification-channels/{ch['id']}",
        json={"config": {"chat_id": "-1001", "bot_token": OTHER}},
    )
    assert r.status_code == 200
    r = await admin.post(f"/api/notification-channels/{ch['id']}/test")
    assert r.status_code == 502 and r.json()["error"]["message"] == "Unauthorized"
    assert tg.requests[-1]["token"] == OTHER
    r = await admin.get("/api/notification-channels")
    assert VALID not in r.text and OTHER not in r.text
