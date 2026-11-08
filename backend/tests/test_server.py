"""WebSocket over a real uvicorn server, and SPA serving."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
import websockets

import netmon
from netmon.app import create_app
from tests.conftest import ADMIN, make_settings, requires_db

pytestmark = requires_db


@pytest.fixture
async def server(app: Any, clean: None) -> AsyncIterator[int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws_ping_interval=30, lifespan="off"
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


async def _recv(ws: Any, type_: str, timeout: float = 5.0) -> dict:
    async def _loop() -> dict:
        while True:
            msg = json.loads(await ws.recv())
            if msg["type"] == type_:
                return msg

    return await asyncio.wait_for(_loop(), timeout)


async def test_websocket_flow(server: int) -> None:
    url = f"ws://127.0.0.1:{server}/ws"
    async with websockets.connect(url) as ws:
        with pytest.raises(websockets.ConnectionClosed) as exc:
            await asyncio.wait_for(ws.recv(), 5)
        assert exc.value.rcvd.code == 4403  # setup not done
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server}") as c:
        r = await c.post("/api/setup", json=ADMIN)
        assert r.status_code == 201
        cookie = r.cookies["netmon_session"]
        async with websockets.connect(url) as ws:
            with pytest.raises(websockets.ConnectionClosed) as exc:
                await asyncio.wait_for(ws.recv(), 5)
            assert exc.value.rcvd.code == 4401
        headers = {"Cookie": f"netmon_session={cookie}"}
        async with websockets.connect(url, additional_headers=headers) as ws:
            hello = await _recv(ws, "hello")
            assert hello["data"]["user"]["username"] == "admin"
            assert hello["data"]["version"] == netmon.__version__ and "timescale" in hello["data"]
            assert hello["ts"].endswith("Z")
            await ws.send(json.dumps({"type": "ping"}))
            assert (await _recv(ws, "pong"))["data"] == {}
            await ws.send("garbage")
            r = await c.post("/api/groups", json={"name": "WS group"})
            assert r.status_code == 201
            msg = await _recv(ws, "config.changed")
            assert msg["data"] == {"entity": "group", "id": r.json()["id"], "action": "created"}
        # cross-origin mutating request through the real server
        r = await c.post(
            "/api/groups", json={"name": "x"}, headers={"Origin": "http://attacker.example"}
        )
        assert r.status_code == 403
        r = await c.post(
            "/api/groups", json={"name": "y"}, headers={"Origin": f"http://127.0.0.1:{server}"}
        )
        assert r.status_code == 201


async def test_spa_placeholder(app: Any, clean: None) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        r = await c.get("/")
        assert r.status_code == 200 and "Frontend not built" in r.text
        r = await c.get("/devices/3")
        assert "Frontend not built" in r.text


async def test_spa_serving(rt: Any, tmp_path: Path, clean: None) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>SPA</html>")
    (dist / "assets" / "app-123.js").write_text("console.log(1)")
    (dist / "favicon.svg").write_text("<svg/>")
    settings = make_settings(rt.settings.database_url, tmp_path, NETMON_FRONTEND_DIST=str(dist))
    spa_app = create_app(settings, runtime=rt, manage_runtime=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=spa_app), base_url="http://testserver"
    ) as c:
        r = await c.get("/")
        assert r.text == "<html>SPA</html>" and r.headers["cache-control"] == "no-cache"
        r = await c.get("/sensors/7")
        assert r.text == "<html>SPA</html>"
        r = await c.get("/assets/app-123.js")
        assert r.text == "console.log(1)"
        assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
        r = await c.get("/favicon.svg")
        assert r.text == "<svg/>"
        r = await c.get("/../../etc/passwd")
        assert r.text == "<html>SPA</html>"
        assert (await c.post("/api/setup", json=ADMIN)).status_code == 201
        r = await c.get("/api/unknown")
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
        r = await c.get("/api/health")
        assert r.json()["status"] == "ok"
