"""[3] NOC kiosk against the database: screen management (token shown once, hashed),
the guard (wrong token / wrong IP / disabled / feature OFF all → 404, 429 after 30 failures),
the curated view (no addresses unless allowed, no data outside the scope), /ws/kiosk over a
real uvicorn server (token in the first message, kiosk.refresh, 4401 / 4403)."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
import uvicorn
import websockets

from netmon import kiosk
from tests.conftest import _plain, make_device, make_sensor, requires_db

pytestmark = requires_db

LOCAL = ["127.0.0.0/8"]


@pytest.fixture
async def noc(rt: Any, admin: httpx.AsyncClient, feats: Any, app: Any) -> Any:
    feats(noc=True, audit=True)
    if getattr(app.state, "kiosk_limiter", None) is not None:
        app.state.kiosk_limiter.clear()
    await rt.kiosk.feature_changed("noc", True)
    yield rt.kiosk
    await rt.kiosk.feature_changed("noc", False)


async def _screen(admin: httpx.AsyncClient, **kw: Any) -> dict[str, Any]:
    body = {
        "name": kw.pop("name", "Wall"),
        "allowed_sources": kw.pop("allowed_sources", LOCAL),
        "widgets": kw.pop(
            "widgets",
            ["summary", "down_devices", "problems", "recent_alerts", "map", "group_tiles"],
        ),
        **kw,
    }
    r = await admin.post("/api/kiosk-screens", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _kiosk_get(app: Any, token: str | None, headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        h = dict(headers or {})
        if token is not None:
            h["X-Netmon-Kiosk"] = token
        return await c.get("/api/kiosk/view", headers=h)


async def test_management_and_validation(admin: httpx.AsyncClient, feats: Any, rt: Any) -> None:
    r = await admin.get("/api/kiosk-screens")
    assert r.status_code == 409
    feats(noc=True)
    for bad in (
        {"name": "x", "allowed_sources": []},
        {"name": "x", "allowed_sources": ["not-an-ip"]},
        {"name": "x", "allowed_sources": LOCAL, "widgets": ["summary", "summary"]},
        {"name": "x", "allowed_sources": LOCAL, "refresh_s": 5},
        {"name": "x"},
    ):
        assert (await admin.post("/api/kiosk-screens", json=bad)).status_code == 422, bad
    out = await _screen(admin, allowed_sources=["10.1.2.7/24", "127.0.0.1"])
    tok = out["token"]
    assert tok.startswith("nmk_") and len(tok) == 47
    assert out["url"] is None  # no public_base_url
    item = out["item"]
    assert item["token_prefix"] == tok[:12] and item["allowed_sources"] == [
        "10.1.2.0/24",
        "127.0.0.1",
    ]
    assert "token" not in item and "token_hash" not in item
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        stored = await conn.fetchval("SELECT token_hash FROM kiosk_screens")
    finally:
        await conn.close()
    assert stored == kiosk.token_hash(tok)
    lst = (await admin.get("/api/kiosk-screens")).json()["items"]
    assert len(lst) == 1 and "token" not in lst[0]
    assert (
        await admin.post("/api/kiosk-screens", json={"name": "Wall", "allowed_sources": LOCAL})
    ).status_code == 409
    await admin.put("/api/settings", json={"public_base_url": "https://nm.example"})
    r = await admin.post(f"/api/kiosk-screens/{item['id']}/regenerate")
    assert r.status_code == 200
    assert r.json()["url"] == f"https://nm.example/noc#{r.json()['token']}"
    assert r.json()["token"] != tok
    r = await admin.patch(
        f"/api/kiosk-screens/{item['id']}", json={"refresh_s": 60, "enabled": False}
    )
    assert r.status_code == 200 and r.json()["refresh_s"] == 60 and r.json()["enabled"] is False
    assert (await admin.delete(f"/api/kiosk-screens/{item['id']}")).status_code == 204


async def test_guard_404_everywhere(
    app: Any, admin: httpx.AsyncClient, noc: Any, feats: Any
) -> None:
    good = await _screen(admin)
    tok = good["token"]
    far = await _screen(admin, name="Far", allowed_sources=["10.99.0.0/16"])
    r = await _kiosk_get(app, tok)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    cases = [
        None,
        "",
        "nmk_" + "x" * 43,
        far["token"],  # wrong IP
    ]
    bodies = set()
    for t in cases:
        r = await _kiosk_get(app, t)
        assert r.status_code == 404, (t, r.text)
        bodies.add(r.text)
    # disabled screen and feature OFF look the same
    await admin.patch(f"/api/kiosk-screens/{good['item']['id']}", json={"enabled": False})
    r = await _kiosk_get(app, tok)
    assert r.status_code == 404
    bodies.add(r.text)
    await admin.patch(f"/api/kiosk-screens/{good['item']['id']}", json={"enabled": True})
    feats(noc=False)
    r = await _kiosk_get(app, tok)
    assert r.status_code == 404
    bodies.add(r.text)
    assert len(bodies) == 1  # no oracle
    feats(noc=True)
    # a kiosk token is no session: other API routes ignore it
    r = await _kiosk_get(app, tok)
    assert r.status_code == 200
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.get("/api/devices", headers={"X-Netmon-Kiosk": tok})
        assert r.status_code == 401


async def test_throttle(app: Any, admin: httpx.AsyncClient, noc: Any) -> None:
    good = await _screen(admin)
    for _ in range(30):
        assert (await _kiosk_get(app, "nmk_" + "y" * 43)).status_code == 404
    r = await _kiosk_get(app, good["token"])
    assert r.status_code == 429
    app.state.kiosk_limiter.clear()


async def test_view_content_scope_and_addresses(
    app: Any, admin: httpx.AsyncClient, noc: Any, rt: Any
) -> None:
    r = await admin.post("/api/groups", json={"name": "Site A"})
    ga = r.json()["id"]
    r = await admin.post("/api/groups", json={"name": "Site B"})
    gb = r.json()["id"]
    da = await make_device(admin, name="a-sw", host="10.0.0.1", group_id=ga)
    db = await make_device(admin, name="b-sw", host="10.0.0.2", group_id=gb)
    sa = await make_sensor(admin, da["id"], "ping", {})
    await make_sensor(admin, db["id"], "ping", {})
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        await conn.execute(
            "UPDATE sensors SET status = 'down', status_message = 'timeout', "
            "last_status_change_at = now() WHERE device_id = ANY($1)",
            [da["id"], db["id"]],
        )
    finally:
        await conn.close()
    scoped = await _screen(admin, name="A only", scope_group_id=ga)
    r = await _kiosk_get(app, scoped["token"])
    v = r.json()
    assert v["screen"]["name"] == "A only" and v["timezone"]
    assert [d["name"] for d in v["down_devices"]] == ["a-sw"]
    assert v["down_devices"][0]["host"] is None  # show_addresses false
    assert [p["sensor_id"] for p in v["problems"]] == [sa["id"]]
    assert v["summary"]["device_counts"]["total"] == 1
    assert {n["name"] for n in v["map"]["nodes"]} == {"a-sw"}
    assert v["group_tiles"][0]["id"] == ga
    text = json.dumps(v)
    for secret in ("b-sw", "10.0.0.", "community", "password", "admin"):
        assert secret not in text, secret
    widgets = await _screen(admin, name="Few", widgets=["down_devices"], show_addresses=True)
    v = (await _kiosk_get(app, widgets["token"])).json()
    assert v["summary"] is None and v["map"] is None and v["problems"] is None
    assert {d["host"] for d in v["down_devices"]} == {"10.0.0.1", "10.0.0.2"}
    lst = (await admin.get("/api/kiosk-screens")).json()["items"]
    seen = {i["name"]: i for i in lst}
    assert seen["Few"]["last_seen_ip"] == "127.0.0.1" and seen["Few"]["last_seen_at"]


@pytest.fixture
async def server(app: Any) -> AsyncIterator[int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    from netmon.cli import H11_MAX_HEADER_BYTES

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
        http="h11",
        h11_max_incomplete_event_size=H11_MAX_HEADER_BYTES,
    )
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    for _ in range(100):
        if srv.started:
            break
        await asyncio.sleep(0.05)
    yield port
    srv.should_exit = True
    await task


async def _closed_code(ws: Any) -> int:
    with pytest.raises(websockets.ConnectionClosed) as exc:
        while True:
            await asyncio.wait_for(ws.recv(), 8)
    return exc.value.rcvd.code


async def test_kiosk_websocket(admin: httpx.AsyncClient, noc: Any, server: int) -> None:
    noc.debounce_s = 0.05
    out = await _screen(admin)
    url = f"ws://127.0.0.1:{server}/ws/kiosk"
    async with websockets.connect(url) as ws:  # wrong token
        await ws.send(json.dumps({"type": "kiosk_auth", "token": "nmk_" + "z" * 43}))
        assert await _closed_code(ws) == 4401
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "kiosk_auth", "token": out["token"]}))
        assert json.loads(await asyncio.wait_for(ws.recv(), 5)) == {"type": "kiosk.ready"}
        # a status-relevant broadcast triggers a debounced refresh
        noc.rt.hub.broadcast("maintenance.changed", {"active_window_ids": []})
        assert json.loads(await asyncio.wait_for(ws.recv(), 5)) == {"type": "kiosk.refresh"}
        # regenerating the token closes the socket with 4403
        r = await admin.post(f"/api/kiosk-screens/{out['item']['id']}/regenerate")
        assert r.status_code == 200
        assert await _closed_code(ws) == 4403


async def test_kiosk_ws_auth_timeout(
    noc: Any, server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from netmon.api import kiosk as kiosk_api

    monkeypatch.setattr(kiosk_api, "AUTH_TIMEOUT_S", 0.3)
    async with websockets.connect(f"ws://127.0.0.1:{server}/ws/kiosk") as ws:
        assert await _closed_code(ws) == 4401


async def test_large_negotiate_header_accepted(server: int, admin: httpx.AsyncClient) -> None:
    """§ 13.5.1: users in many groups send 10–40 KB Negotiate headers; QA sends 48 KB."""
    big = "Negotiate " + "A" * (48 * 1024)
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server}") as c:
        r = await c.get("/api/health", headers={"Authorization": big})
        assert r.status_code == 200
