"""[2b-3] Microsoft Teams channel: write-only URL, legacy flag, test send against an httpx mock
(payload shape, 202 success, 400 → 502 teams_error without the URL). Teams is always available
(no feature toggle): a stored legacy `feature_teams_enabled=false` changes nothing."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import asyncpg
import httpx
import pytest

from netmon.alerts import teams
from tests.conftest import _plain, requires_db

pytestmark = requires_db

URL = (
    "https://prod-12.westeurope.logic.azure.com/workflows/abc/triggers/manual/paths/invoke"
    "?api-version=2016-06-01&sig=TOPSECRETSIG"
)


@pytest.fixture
def teams_mock(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"status": 202, "body": "", "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return httpx.Response(state["status"], text=state["body"])

    real = httpx.AsyncClient

    def factory(*args: Any, **kw: Any) -> httpx.AsyncClient:
        state["kwargs"] = kw
        kw.pop("verify", None)
        kw.pop("trust_env", None)
        return real(*args, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(teams.httpx, "AsyncClient", factory)
    return state


async def _create(admin: httpx.AsyncClient, url: str = URL) -> httpx.Response:
    return await admin.post(
        "/api/notification-channels",
        json={"name": "Ops Teams", "type": "teams", "config": {"url": url}},
    )


async def test_always_available_legacy_toggle_ignored(
    admin: httpx.AsyncClient, feats: Any, rt: Any
) -> None:
    """A row left by the removed toggle is ignored; PUT /api/settings ignores the key."""
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        for k in ("feature_teams_enabled", "feature_api_tokens_enabled"):
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ($1, 'false'::jsonb) "
                "ON CONFLICT (key) DO UPDATE SET value = 'false'::jsonb",
                k,
            )
    finally:
        await conn.close()
    s = (await admin.get("/api/settings")).json()
    assert "feature_teams_enabled" not in s and "feature_api_tokens_enabled" not in s
    pub = (await admin.get("/api/settings/public")).json()
    assert "teams" not in pub["features"] and "api_tokens" not in pub["features"]
    r = await admin.put(
        "/api/settings", json={"feature_teams_enabled": True, "feature_api_tokens_enabled": False}
    )
    assert r.status_code == 200, r.text
    assert "feature_teams_enabled" not in r.json()
    r = await _create(admin)
    assert r.status_code == 201, r.text


async def test_channel_crud_secret_url(
    admin: httpx.AsyncClient, feats: Any, teams_mock: dict, rt: Any
) -> None:
    feats(audit=True)
    r = await admin.post(
        "/api/notification-channels", json={"name": "T", "type": "teams", "config": {}}
    )
    assert r.status_code == 422
    r = await _create(admin, "http://plain.example.com/x")
    assert r.status_code == 422
    r = await _create(admin)
    assert r.status_code == 201, r.text
    ch = r.json()
    assert ch["config"] == {
        "url": None,
        "url_set": True,
        "url_host": "prod-12.westeurope.logic.azure.com",
        "legacy": False,
        "ack_link": True,  # [3] chat_ack
    }
    assert "TOPSECRETSIG" not in r.text
    # PATCH without a URL keeps it (echoed response keys are ignored)
    r = await admin.patch(
        f"/api/notification-channels/{ch['id']}",
        json={"name": "Ops Teams 2", "config": ch["config"]},
    )
    assert r.status_code == 200 and r.json()["config"]["url_set"] is True
    # legacy connector URL accepted with the flag
    r = await admin.patch(
        f"/api/notification-channels/{ch['id']}",
        json={"config": {"url": "https://mnidom.webhook.office.com/webhookb2/x/IncomingWebhook/y"}},
    )
    assert r.json()["config"]["legacy"] is True
    r = await admin.patch(f"/api/notification-channels/{ch['id']}", json={"config": {"url": URL}})
    # test send
    r = await admin.post(f"/api/notification-channels/{ch['id']}/test")
    assert r.status_code == 200, r.text
    req = teams_mock["requests"][-1]
    assert str(req.url) == URL and req.method == "POST"
    assert req.headers["content-type"] == "application/json"
    body = json.loads(req.content)
    assert body["type"] == "message"
    content = body["attachments"][0]["content"]
    assert body["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert content["body"][0]["text"] == "NetworkMonitor test message"
    assert teams_mock["kwargs"]["timeout"] == 15.0
    # failure → 502 teams_error with status + body, never the URL
    teams_mock["status"], teams_mock["body"] = 400, "Bad Request: " + URL
    r = await admin.post(f"/api/notification-channels/{ch['id']}/test")
    assert r.status_code == 502 and r.json()["error"]["code"] == "teams_error"
    msg = r.json()["error"]["message"]
    assert msg.startswith("HTTP 400") and "TOPSECRETSIG" not in msg
    # audit never stores the URL
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        rows = await conn.fetch(
            "SELECT changes::text AS c FROM audit_log WHERE entity_type = 'notification_channel'"
        )
    finally:
        await conn.close()
    assert rows and all("TOPSECRETSIG" not in r["c"] and "webhookb2" not in r["c"] for r in rows)


async def test_send_always_delivered(admin: httpx.AsyncClient, feats: Any, rt: Any) -> None:
    calls: list[Any] = []

    async def sender(*a: Any) -> None:
        calls.append(a)

    rt.alerts.sender = sender
    channel = {"id": 1, "name": "Ops Teams", "type": "teams", "config": {"url": URL}}
    r = SimpleNamespace(sensor_id=None, device_id=None, gid=None, sname=None, dname=None)
    ok = await rt.alerts._send_one(  # noqa: SLF001
        channel, {"event": "test"}, {"smtp": {}, "default_language": "en"}, r
    )
    assert ok is True and len(calls) == 1
