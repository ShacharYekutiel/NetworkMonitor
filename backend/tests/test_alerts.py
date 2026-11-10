"""Alert rules, notification channels, email (aiosmtpd) and webhook (local server)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from netmon.alerts.email import SmtpError, send_email
from netmon.alerts.engine import should_fire
from netmon.alerts.webhook import WebhookError, send_webhook
from netmon.poller.processor import persist
from netmon.poller.types import CheckResult
from tests.conftest import make_device, make_sensor, requires_db

UP = CheckResult(True, "", {"latency_ms": 1.0, "packet_loss_pct": 0.0})
DOWN = CheckResult(False, "Host unreachable")
WARN = CheckResult(True, "", {"latency_ms": 150.0, "packet_loss_pct": 0.0})


def test_should_fire() -> None:
    assert should_fire("up", "down") and should_fire("unknown", "down")
    assert should_fire("down", "up") and should_fire("warning", "up")
    # an up is a candidate from any status; it is sent only after a problem notification
    assert should_fire("unknown", "up") and not should_fire("up", "up")
    assert not should_fire("down", "paused") and not should_fire("up", "unknown")


# ------------------------------------------------------------------ local webhook receiver
class Receiver:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.port = 0
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = {
            k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)
        }
        body = await reader.readexactly(int(headers.get("content-length", 0)))
        self.requests.append({"line": lines[0], "headers": headers, "json": json.loads(body)})
        writer.write(
            f"HTTP/1.1 {self.status} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        writer.close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"


@pytest.fixture
async def receiver() -> AsyncIterator[Receiver]:
    r = Receiver()
    await r.start()
    yield r
    await r.stop()


async def test_webhook_sender(receiver: Receiver) -> None:
    await send_webhook(
        {"url": receiver.url, "headers": {"X-Token": "abc"}, "timeout_s": 5}, {"event": "test"}
    )
    req = receiver.requests[0]
    assert req["line"].startswith("POST /hook")
    assert req["headers"]["x-token"] == "abc"
    assert req["headers"]["content-type"] == "application/json"
    assert req["headers"]["user-agent"].startswith("NetworkMonitor/")
    receiver.status = 500
    with pytest.raises(WebhookError):
        await send_webhook({"url": receiver.url}, {"event": "test"})


@pytest.fixture
def smtp_server() -> Any:
    from aiosmtpd.controller import Controller

    class Handler:
        def __init__(self) -> None:
            self.messages: list[Any] = []

        async def handle_DATA(self, server, session, envelope):  # type: ignore[no-untyped-def] # noqa: N802
            self.messages.append(envelope)
            return "250 OK"

    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    handler = Handler()
    ctl = Controller(handler, hostname="127.0.0.1", port=port)
    ctl.start()
    yield ctl, handler
    ctl.stop()


async def test_email_sender(smtp_server: Any) -> None:
    ctl, handler = smtp_server
    smtp = {
        "host": "127.0.0.1",
        "port": ctl.port,
        "security": "none",
        "username": None,
        "password": None,
        "from_address": "netmon@example.com",
        "from_name": "NetworkMonitor",
    }
    await send_email(smtp, ["ops@example.com"], "[NetworkMonitor] DOWN: a / b", "text", "<b>x</b>")
    assert handler.messages[0].rcpt_tos == ["ops@example.com"]
    content = handler.messages[0].content.decode()
    assert "Subject: [NetworkMonitor] DOWN: a / b" in content and "text/html" in content
    with pytest.raises(SmtpError):
        await send_email({**smtp, "host": ""}, ["a@example.com"], "s", "t")


# ------------------------------------------------------------------ engine via the API
@requires_db
async def test_alert_rules_api_and_firing(
    rt, admin: httpx.AsyncClient, receiver: Receiver, smtp_server: Any
) -> None:  # type: ignore[no-untyped-def]
    ctl, smtp_handler = smtp_server
    await admin.put(
        "/api/settings",
        json={
            "smtp": {
                "host": "127.0.0.1",
                "port": ctl.port,
                "security": "none",
                "from_address": "netmon@example.com",
            },
            "public_base_url": "http://netmon.local:8080",
        },
    )
    r = await admin.post(
        "/api/notification-channels",
        json={"name": "Ops hook", "type": "webhook", "config": {"url": receiver.url}},
    )
    assert r.status_code == 201, r.text
    hook = r.json()
    assert hook["config"] == {
        "url": receiver.url,
        "headers": {},
        "verify_tls": True,
        "timeout_s": 10,
    }
    r = await admin.post(
        "/api/notification-channels",
        json={"name": "Ops mail", "type": "email", "config": {"to": ["ops@example.com"]}},
    )
    mail = r.json()
    r = await admin.post(
        "/api/notification-channels",
        json={"name": "ops hook", "type": "webhook", "config": {"url": receiver.url}},
    )
    assert r.status_code == 409
    r = await admin.post(
        "/api/notification-channels", json={"name": "bad", "type": "email", "config": {"to": []}}
    )
    assert r.status_code == 422
    r = await admin.post(
        "/api/notification-channels",
        json={"name": "bad", "type": "webhook", "config": {"url": "x", "extra": 1}},
    )
    assert r.status_code == 422
    assert [c["name"] for c in (await admin.get("/api/notification-channels")).json()["items"]] == [
        "Ops hook",
        "Ops mail",
    ]

    # test sends
    r = await admin.post(f"/api/notification-channels/{hook['id']}/test")
    assert r.json() == {"ok": True, "message": "sent"}
    assert receiver.requests[-1]["json"]["event"] == "test"
    assert receiver.requests[-1]["json"]["sensor"] is None
    r = await admin.post(f"/api/notification-channels/{mail['id']}/test")
    assert r.status_code == 200 and len(smtp_handler.messages) == 1
    r = await admin.post("/api/settings/smtp/test", json={"to": "me@example.com"})
    assert r.json()["ok"] is True and smtp_handler.messages[-1].rcpt_tos == ["me@example.com"]
    receiver.requests.clear()
    smtp_handler.messages.clear()

    dev = await make_device(admin, name="core-sw", host="10.0.0.2")
    s = await make_sensor(admin, dev["id"], "ping", name="Ping")
    other = await make_sensor(admin, dev["id"], "ping", name="Other")
    r = await admin.post(
        "/api/alert-rules",
        json={
            "name": "All down",
            "sensor_id": None,
            "on_statuses": ["down", "up"],
            "delay_s": 0,
            "channel_ids": [hook["id"], mail["id"]],
        },
    )
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule == {
        "id": rule["id"],
        "name": "All down",
        "sensor_id": None,
        "sensor_name": None,
        "on_statuses": ["down", "up"],
        "delay_s": 0,
        "enabled": True,
        "channel_ids": sorted([hook["id"], mail["id"]]),
        "escalation_after_s": None,
        "escalation_channel_ids": [],
        "repeat_every_s": None,
        "created_at": rule["created_at"],
        "updated_at": rule["updated_at"],
    }
    # a sensor-specific rule targeting the same webhook -> still one send per event
    r = await admin.post(
        "/api/alert-rules",
        json={
            "name": "Ping warn",
            "sensor_id": s["id"],
            "on_statuses": ["down", "warning"],
            "channel_ids": [hook["id"]],
        },
    )
    assert r.json()["sensor_name"] == "Ping"
    assert (
        await admin.post(
            "/api/alert-rules", json={"name": "x", "on_statuses": ["down"], "channel_ids": [999]}
        )
    ).status_code == 422
    assert (
        await admin.post(
            "/api/alert-rules",
            json={"name": "x", "on_statuses": ["paused"], "channel_ids": [hook["id"]]},
        )
    ).status_code == 422
    assert (
        await admin.post(
            "/api/alert-rules", json={"name": "x", "on_statuses": ["down"], "channel_ids": []}
        )
    ).status_code == 422
    r = await admin.get("/api/alert-rules", params={"sensor_id": "global"})
    assert [x["name"] for x in r.json()["items"]] == ["All down"]
    r = await admin.get("/api/alert-rules", params={"sensor_id": s["id"]})
    assert [x["name"] for x in r.json()["items"]] == ["Ping warn"]

    async def run(sid: int, raw: CheckResult) -> None:
        await persist(
            rt, sid, raw, 1.0, __import__("datetime").datetime.now(__import__("datetime").UTC)
        )
        await rt.drain()

    await run(s["id"], UP)  # unknown -> up: never fires
    assert receiver.requests == [] and smtp_handler.messages == []
    await run(s["id"], DOWN)  # up -> down
    assert len(receiver.requests) == 1
    p = receiver.requests[0]["json"]
    assert p["event"] == "sensor.status_changed" and p["old_status"] == "up"
    assert p["new_status"] == "down" and p["message"] == "Host unreachable"
    assert p["sensor"] == {"id": s["id"], "name": "Ping", "type": "ping"}
    assert p["device"] == {"id": dev["id"], "name": "core-sw", "host": "10.0.0.2"}
    assert p["group"]["name"] == "Default"
    assert p["rule"]["name"] == "Ping warn"  # sensor-specific rule wins the dedupe
    assert p["url"] == f"http://netmon.local:8080/sensors/{s['id']}"
    assert p["stage"] == "problem" and p["after_maintenance"] is False
    assert p["problem_since"] == p["time"]
    assert set(p) == {
        "event",
        "stage",
        "after_maintenance",
        "after_flapping",  # [2b-3]
        "ack_url",  # [3] chat_ack
        "problem_since",
        "event_id",
        "time",
        "old_status",
        "new_status",
        "message",
        "sensor",
        "device",
        "group",
        "rule",
        "url",
    }
    assert len(smtp_handler.messages) == 1
    assert (
        "Subject: [NetworkMonitor] DOWN: core-sw / Ping"
        in smtp_handler.messages[0].content.decode()
    )
    await run(s["id"], WARN)  # down -> warning: only "Ping warn"
    assert len(receiver.requests) == 2 and len(smtp_handler.messages) == 1
    await run(s["id"], UP)  # warning -> up: "All down" (on up)
    assert len(receiver.requests) == 3 and receiver.requests[-1]["json"]["new_status"] == "up"
    await run(other["id"], UP)
    await run(other["id"], WARN)  # global rule ignores warning
    assert len(receiver.requests) == 3

    # failing channel -> notification event
    receiver.status = 500
    await run(other["id"], DOWN)
    events = (await admin.get("/api/events", params={"kind": "notification"})).json()["items"]
    assert len(events) == 1 and events[0]["message"].startswith("Ops hook:")
    receiver.status = 200
    r = await admin.post(
        "/api/notification-channels",
        json={
            "name": "dead",
            "type": "webhook",
            "config": {"url": "http://127.0.0.1:1/x", "timeout_s": 1},
        },
    )
    r = await admin.post(f"/api/notification-channels/{r.json()['id']}/test")
    assert r.status_code == 502 and r.json()["error"]["code"] == "webhook_error"

    # disabled rule / patch / delete
    r = await admin.patch(
        f"/api/alert-rules/{rule['id']}", json={"enabled": False, "channel_ids": [mail["id"]]}
    )
    assert r.json()["enabled"] is False and r.json()["channel_ids"] == [mail["id"]]
    assert (await admin.delete(f"/api/alert-rules/{rule['id']}")).status_code == 204
    assert (await admin.get(f"/api/alert-rules/{rule['id']}")).status_code == 404


@requires_db
async def test_alert_delay(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    sent: list[dict] = []

    async def fake_sender(channel, payload, smtp, lang):  # type: ignore[no-untyped-def]
        sent.append(payload)

    rt.alerts.sender = fake_sender
    ch = (
        await admin.post(
            "/api/notification-channels",
            json={"name": "h", "type": "webhook", "config": {"url": "http://x.invalid/"}},
        )
    ).json()
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "ping")
    await admin.post(
        "/api/alert-rules",
        json={"name": "delayed", "on_statuses": ["down"], "delay_s": 1, "channel_ids": [ch["id"]]},
    )
    from datetime import UTC, datetime

    async def run(raw: CheckResult) -> None:
        await persist(rt, s["id"], raw, 1.0, datetime.now(UTC))
        await rt.drain()

    await run(UP)
    await run(DOWN)
    async with rt.sessionmaker() as session:
        pending = (
            await session.execute(
                __import__("sqlalchemy").text(
                    "SELECT count(*) FROM alert_state WHERE due_at IS NOT NULL"
                )
            )
        ).scalar()
    assert sent == [] and pending == 1  # [2b-1] the delay is persisted in alert_state
    await run(UP)  # recovered before the delay expired -> no send
    await asyncio.sleep(1.3)
    assert sent == []
    await run(DOWN)
    await asyncio.sleep(1.3)
    assert len(sent) == 1 and sent[0]["new_status"] == "down"
