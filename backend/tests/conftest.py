"""Test fixtures.

DB tests need TEST_DATABASE_URL (a PostgreSQL URL with rights to CREATE DATABASE); they are
skipped when it is unset. Each session creates fresh databases `netmon_test_<random>` (main,
Timescale used when available unless NETMON_DISABLE_TIMESCALE=1) and `netmon_test_<random>_fb`
(always fallback mode) and drops them at the end.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest

from netmon.app import create_app
from netmon.config import load_settings
from netmon.runtime import Runtime

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

# [2b-1] coalescing window of the dependency cascade (0.5 s in production)
__import__("netmon.services.dependencies", fromlist=["x"]).CASCADE_DEBOUNCE_S = 0.02
requires_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

TABLES = (
    "discovery_results, discovery_scans, discovery_profiles, discovery_known_hosts, "
    "alert_state, alert_rule_channels, alert_rules, notification_channels, events, "
    "measurements, sensor_channels, sensors, devices, groups, users, settings, "
    "maintenance_windows, topology_links, topology_neighbors, topology_layout, "
    "topology_refreshes"
)


def _plain(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


def db_url_for(name: str) -> str:
    assert TEST_DATABASE_URL
    parts = urlsplit(_plain(TEST_DATABASE_URL))
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, ""))


@pytest.fixture(scope="session")
def db_names() -> dict[str, str]:
    tag = secrets.token_hex(4)
    return {"main": f"netmon_test_{tag}", "fallback": f"netmon_test_{tag}_fb"}


@pytest.fixture(scope="session")
async def databases(db_names: dict[str, str]) -> AsyncIterator[dict[str, str]]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    try:
        for name in db_names.values():
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    yield {k: db_url_for(v) for k, v in db_names.items()}
    conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    try:
        for name in db_names.values():
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


def make_settings(db_url: str, data_dir: Path, **extra: Any) -> Any:
    overrides = {
        "DATABASE_URL": db_url,
        "NETMON_DATA_DIR": str(data_dir),
        "NETMON_FRONTEND_DIST": str(data_dir / "no-dist"),
        **extra,
    }
    s = load_settings(**overrides)
    s.data_dir = data_dir
    return s


@pytest.fixture(scope="session")
async def rt(
    databases: dict[str, str], tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[Runtime]:
    data_dir = tmp_path_factory.mktemp("data")
    settings = make_settings(databases["main"], data_dir)
    runtime = Runtime(settings)
    await runtime.start(run_poller=False, maintenance=False)
    yield runtime
    await runtime.stop()


@pytest.fixture(scope="session")
async def app(rt: Runtime) -> Any:
    return create_app(rt.settings, runtime=rt, manage_runtime=False)


@pytest.fixture
async def clean(rt: Runtime, app: Any) -> AsyncIterator[None]:
    await rt.drain()
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        for attempt in range(5):
            try:
                await conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
                break
            except asyncpg.exceptions.DeadlockDetectedError:
                if attempt == 4:
                    raise
                await asyncio.sleep(0.2)
    finally:
        await conn.close()
    rt._has_users = False  # noqa: SLF001
    rt._rollup_snapshot = None  # noqa: SLF001
    app.state.login_limiter._fails.clear()  # noqa: SLF001
    rt.hub.listeners.clear()
    if rt.alerts is not None:
        rt.alerts.sender = __import__("netmon.alerts.engine", fromlist=["x"]).send_to_channel
        rt.alerts._sent.clear()  # noqa: SLF001
    rt.cascade.invalidate()
    rt.clock.set_offset(0)
    if rt.maintenance is not None:
        rt.maintenance.reset_state()
        await rt.maintenance.reload()
    yield


@pytest.fixture
async def client(app: Any, clean: None) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


ADMIN = {"username": "admin", "password": "S3cret-pass", "email": None, "language": "en"}


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> httpx.AsyncClient:
    r = await client.post("/api/setup", json=ADMIN)
    assert r.status_code == 201, r.text
    return client


@pytest.fixture
async def viewer(app: Any, admin: httpx.AsyncClient) -> AsyncIterator[httpx.AsyncClient]:
    r = await admin.post(
        "/api/users", json={"username": "viewer", "password": "viewer-pass", "role": "viewer"}
    )
    assert r.status_code == 201, r.text
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.post("/api/auth/login", json={"username": "viewer", "password": "viewer-pass"})
        assert r.status_code == 200
        yield c


async def default_group_id(c: httpx.AsyncClient) -> int:
    r = await c.get("/api/groups", params={"parent_id": "root"})
    return int(r.json()["items"][0]["id"])


async def make_device(c: httpx.AsyncClient, **kw: Any) -> dict[str, Any]:
    body = {
        "group_id": kw.pop("group_id", None) or await default_group_id(c),
        "name": kw.pop("name", "dev1"),
        "host": kw.pop("host", "127.0.0.1"),
        **kw,
    }
    r = await c.post("/api/devices", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def make_sensor(
    c: httpx.AsyncClient, device_id: int, type_: str, config: dict | None = None, **kw: Any
) -> dict[str, Any]:
    r = await c.post(
        "/api/sensors", json={"device_id": device_id, "type": type_, "config": config or {}, **kw}
    )
    assert r.status_code == 201, r.text
    return r.json()
