"""[1.5.0] TimescaleDB extension update at startup (netmon.db.timescale.update_extension).

A newer timescale/timescaledb image keeps the extension of an existing database at its old
version until `ALTER EXTENSION timescaledb UPDATE` runs as the first statement of a fresh
session. The DB tests install an OLDER extension version in a scratch database (the
Timescale packages and images ship the previous versions' libraries) and check the update,
the no-op cases and the non-owner fallback (warning, app carries on)."""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from netmon.db import timescale
from netmon.db.engine import make_engine
from tests.conftest import TEST_DATABASE_URL, _plain, db_url_for, requires_db


def test_version_tuple() -> None:
    assert timescale._version_tuple("2.31.0") == (2, 31, 0)
    assert timescale._version_tuple("2.32.1") > timescale._version_tuple("2.31.10")[:2]
    assert timescale._version_tuple("2.9.3") < timescale._version_tuple("2.10.0")
    assert timescale._version_tuple("2.18.0-dev") == (2, 18, 0)
    assert timescale._version_tuple(None) == ()


async def _older_version(admin: asyncpg.Connection) -> tuple[str, str] | None:
    row = await admin.fetchrow(
        "SELECT default_version FROM pg_available_extensions WHERE name = 'timescaledb'"
    )
    if row is None:
        return None
    default = row["default_version"]
    versions = [
        r["version"]
        for r in await admin.fetch(
            "SELECT version FROM pg_available_extension_versions WHERE name = 'timescaledb'"
        )
    ]
    older = sorted(
        (v for v in versions if timescale._version_tuple(v) < timescale._version_tuple(default)),
        key=timescale._version_tuple,
    )
    return (older[-1], default) if older else None


@pytest.fixture
async def old_ts_db() -> AsyncIterator[tuple[str, str, str]]:
    """A scratch database with TimescaleDB installed at the newest version OLDER than the
    server's default, a hypertable and a retention policy (a background job)."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    admin = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    name = f"netmon_test_tsupd_{secrets.token_hex(4)}"
    try:
        versions = await _older_version(admin)
        if versions is None:
            pytest.skip("no older TimescaleDB version installable on this server")
        old, new = versions
        # template0, not the default template1: the timescale/timescaledb image's init script
        # (000_install_timescaledb.sh) installs the extension, at the default version, into
        # template1, so a copy of it already has timescaledb and the old version cannot be
        # installed ("extension already exists", CI run 45776086774).
        await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
    finally:
        await admin.close()
    url = db_url_for(name)
    c = await asyncpg.connect(url)
    try:
        await c.execute(f"CREATE EXTENSION timescaledb VERSION '{old}'")
        await c.execute("CREATE TABLE m (time timestamptz NOT NULL, v int)")
        await c.execute("SELECT create_hypertable('m', 'time')")
        await c.execute("SELECT add_retention_policy('m', INTERVAL '7 days')")
        await c.execute("INSERT INTO m VALUES (now(), 1)")
    finally:
        await c.close()
    try:
        yield url, old, new
    finally:
        admin = await asyncpg.connect(_plain(TEST_DATABASE_URL))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


def _async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


async def _ext_version(url: str) -> str:
    c = await asyncpg.connect(url)
    try:
        return await c.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
    finally:
        await c.close()


@requires_db
async def test_update_extension_to_server_version(old_ts_db: tuple[str, str, str]) -> None:
    url, old, new = old_ts_db
    assert await _ext_version(url) == old
    engine = make_engine(_async_url(url))
    try:
        # a pooled session that has the old library loaded (as wait_for_db leaves one)
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT count(*) FROM m")
        assert await timescale.update_extension(engine) == new
        # the engine still works after the pool was disposed (no stale old-version session)
        async with engine.connect() as conn:
            n = (await conn.exec_driver_sql("SELECT count(*) FROM m")).scalar()
            jobs = (
                await conn.exec_driver_sql(
                    "SELECT count(*) FROM timescaledb_information.jobs "
                    "WHERE proc_name = 'policy_retention'"
                )
            ).scalar()
        assert (n, jobs) == (1, 1)
        assert await _ext_version(url) == new
        # second start: nothing to do
        assert await timescale.update_extension(engine) is None
    finally:
        await engine.dispose()


@requires_db
async def test_update_extension_disabled_is_noop(old_ts_db: tuple[str, str, str]) -> None:
    url, old, _new = old_ts_db
    engine = make_engine(_async_url(url))
    try:
        assert await timescale.update_extension(engine, disabled=True) is None
    finally:
        await engine.dispose()
    assert await _ext_version(url) == old


@requires_db
async def test_update_extension_not_owner_warns(
    old_ts_db: tuple[str, str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """Native installs create the extension as the postgres superuser: the app role cannot
    update it, logs the manual command and carries on with the installed version."""
    url, old, new = old_ts_db
    role = f"netmon_test_tsrole_{secrets.token_hex(3)}"
    pw = secrets.token_hex(8)
    c = await asyncpg.connect(url)
    try:
        await c.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{pw}'")
        await c.execute(f"GRANT CONNECT ON DATABASE {make_url(url).database} TO {role}")
    finally:
        await c.close()
    u = make_url(url).set(username=role, password=pw)
    engine = make_engine(_async_url(u.render_as_string(hide_password=False)))
    try:
        with caplog.at_level(logging.WARNING, logger="netmon.db.timescale"):
            assert await timescale.update_extension(engine) is None
        assert any("ALTER EXTENSION timescaledb UPDATE" in r.getMessage() for r in caplog.records)
        assert any(old in r.getMessage() and new in r.getMessage() for r in caplog.records)
        async with engine.connect() as conn:  # still usable
            assert (await conn.exec_driver_sql("SELECT 1")).scalar() == 1
        assert await _ext_version(url) == old
    finally:
        await engine.dispose()
        c = await asyncpg.connect(_plain(TEST_DATABASE_URL or ""))
        try:
            await c.execute(f'DROP DATABASE IF EXISTS "{make_url(url).database}" WITH (FORCE)')
            await c.execute(f"DROP ROLE IF EXISTS {role}")
        finally:
            await c.close()


@requires_db
async def test_runtime_start_updates_and_keeps_lock(
    old_ts_db: tuple[str, str, str], tmp_path: Path
) -> None:
    """Server start on a database with an older extension: updated before the migrations,
    and the single-instance lock is held on a session that survives the update (the server
    restarts sessions that loaded the old library, as the old lock connection did)."""
    from netmon.runtime import Runtime
    from tests.conftest import ingest_ports, make_settings

    url, _old, new = old_ts_db
    # this test is about the Timescale path even when the suite runs in fallback mode
    settings = make_settings(url, tmp_path, NETMON_DISABLE_TIMESCALE="false", **ingest_ports())
    runtime = Runtime(settings)
    await runtime.start(run_poller=False, maintenance=False)
    try:
        assert runtime.timescale_enabled
        assert await _ext_version(url) == new
        assert await runtime._lock_conn.fetchval("SELECT count(*) FROM measurements") == 0
        assert await runtime.check_lock() is True
    finally:
        await runtime.stop()
