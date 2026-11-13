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

# [2b-3] shared fixtures of the auth / audit / tokens / Teams / AD tests
pytest_plugins = ["tests.fixtures_2b3_auth", "tests.fixtures_3b_agent"]  # [3b] agent / mock

# [2b-1] coalescing window of the dependency cascade (0.5 s in production)
__import__("netmon.services.dependencies", fromlist=["x"]).CASCADE_DEBOUNCE_S = 0.02


def migration_head() -> str:
    """[4] The migration head of this tree (phase-4 revision numbers are re-assigned at merge
    time, so no test writes a number down)."""
    from alembic.script import ScriptDirectory

    from netmon.db.migrate import _alembic_config

    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    assert head is not None
    return str(head)


def revision_of(placeholder: str) -> tuple[str, str]:
    """[4] `(revision, down_revision)` of the phase-4 migration whose module declares
    `PLACEHOLDER = placeholder` (e.g. "P4-1")."""
    from alembic.script import ScriptDirectory

    from netmon.db.migrate import _alembic_config

    for rev in ScriptDirectory.from_config(_alembic_config()).walk_revisions():
        if getattr(rev.module, "PLACEHOLDER", None) == placeholder:
            return str(rev.revision), str(rev.down_revision)
    raise LookupError(placeholder)


requires_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

TABLES = (
    "discovery_results, discovery_scans, discovery_profiles, discovery_known_hosts, "
    "alert_state, alert_rule_channels, alert_rules, notification_channels, events, "
    "measurements, sensor_channels, sensors, devices, groups, users, settings, "
    "maintenance_windows, topology_links, topology_neighbors, topology_layout, "
    "topology_refreshes, dns_cache, "
    # [2b-3] (TRUNCATE does not fire the audit_log row trigger)
    "audit_log, api_tokens, "
    # [2b-3] ingestion (event_rules is re-seeded with the built-in rules in `clean`)
    "traps, syslog_messages, event_conditions, event_rules, "
    # [3] flow collector (hourly fallback tables / continuous aggregates are not truncated
    # here: flow tests clean them themselves)
    "flow_exporters, flow_if_1m, flow_app_1m, flow_host_1m, flow_conv_1m, "
    # [3] config backup (ssh_host_keys, config_backup_status, config_versions cascade with
    # devices), new-device alerts, reports, kiosk, chat ack, flapping episodes
    "ssh_host_keys, config_backup_status, config_versions, known_macs, mac_allowlist, "
    "port_flap_episodes, reports, report_runs, kiosk_screens, chat_identities, "
    "chat_link_codes, telegram_poll_state, "
    # [3b] assistant and AI features (migration 0008)
    "assistant_conversations, assistant_messages, assistant_tool_calls, assistant_usage, "
    "assistant_artifacts, assistant_incidents, assistant_incident_members, "
    "assistant_note_targets, assistant_syslog_signatures, assistant_snmp_walks, "
    "revoked_sessions, "  # [1.4.1]
    "aws_connections"  # [4] PR 4-0a
)


def _plain(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


def db_url_for(name: str) -> str:
    assert TEST_DATABASE_URL
    parts = urlsplit(_plain(TEST_DATABASE_URL))
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, ""))


@pytest.fixture(autouse=True)
def _loopback_targets_allowed() -> None:
    """[1.4.1] The suite's local test servers (webhook receivers, HTTP sensor targets) live on
    127.0.0.1: loopback targets are allowed by default; tests/test_141_*.py switch the SSRF
    egress filter back to its production default for their cases."""
    from netmon.util import egress

    egress.override_loopback(True)


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


def free_port(kind: int) -> int:
    import socket

    s = socket.socket(socket.AF_INET, kind)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def ingest_ports() -> dict[str, str]:
    """[2b-3] trap / syslog listeners on free loopback ports (UDP and TCP free for syslog)."""
    import socket

    for _ in range(50):
        sp = free_port(socket.SOCK_DGRAM)
        t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            t.bind(("127.0.0.1", sp))
        except OSError:
            continue
        finally:
            t.close()
        return {
            "NETMON_TRAP_PORT": str(free_port(socket.SOCK_DGRAM)),
            "NETMON_SYSLOG_PORT": str(sp),
            "NETMON_INGEST_HOST": "127.0.0.1",
            # [3] flow sockets on free loopback ports
            "NETMON_NETFLOW_PORT": str(free_port(socket.SOCK_DGRAM)),
            "NETMON_IPFIX_PORT": str(free_port(socket.SOCK_DGRAM)),
            "NETMON_SFLOW_PORT": str(free_port(socket.SOCK_DGRAM)),
            "NETMON_FLOW_HOST": "127.0.0.1",
        }
    raise RuntimeError("no free port")


@pytest.fixture(scope="session")
async def rt(
    databases: dict[str, str], tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[Runtime]:
    data_dir = tmp_path_factory.mktemp("data")
    settings = make_settings(databases["main"], data_dir, **ingest_ports())
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
    app.state.login_limiter.clear()
    # [1.4.1] default security policy, no revoked sessions
    from netmon.services import security as security_svc

    security_svc.apply(rt, {})
    rt.revoked_sessions.clear()
    # [2b-3] API tokens and Active Directory throttles / memory
    app.state.token_limiter.clear()
    app.state.token_stamper.clear()
    app.state.directory.guard.clear()
    app.state.directory.last_good.clear()
    rt.hub.listeners.clear()
    # [2b-3] every feature OFF (fresh install), built-in event rules back, caches empty
    await rt.features.reset()
    async with rt.sessionmaker() as session:
        from netmon.ingest.builtin import ensure_builtin_rules

        await ensure_builtin_rules(session)
        await session.commit()
    if getattr(rt, "flows", None) is not None:
        rt.flows.reset_state()  # [3]
    if rt.ingest is not None:
        rt.ingest.reset_state()
        await rt.ingest.rebuild_sources()
        await rt.event_sensors.reload()
    if rt.alerts is not None:
        rt.alerts.sender = __import__("netmon.alerts.engine", fromlist=["x"]).send_to_channel
        rt.alerts._sent.clear()  # noqa: SLF001
    rt.cascade.invalidate()
    rt.aws.invalidate()  # [4] credential chains of the removed connections
    rt.aws.tests.clear()
    if rt.assistant is not None:
        rt.assistant.view = None
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
