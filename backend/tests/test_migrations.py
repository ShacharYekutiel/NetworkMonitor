"""Migrations + Timescale bootstrap in both modes."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from sqlalchemy import text

from netmon.db import timescale
from netmon.db.engine import make_engine
from netmon.db.migrate import run_migrations
from netmon.runtime import LockHeldError, Runtime
from netmon.services import retention
from tests.conftest import TEST_DATABASE_URL, _plain, db_url_for, make_settings, requires_db
from tests.test_poller import _check_history

pytestmark = requires_db


async def _tables(url: str) -> set[str]:
    conn = await asyncpg.connect(_plain(url))
    try:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {r["tablename"] for r in rows}
    finally:
        await conn.close()


async def test_timescale_mode(rt) -> None:  # type: ignore[no-untyped-def]
    if rt.settings.netmon_disable_timescale:
        pytest.skip("suite runs in forced fallback mode")
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        has_ext = await conn.fetchval(
            "SELECT 1 FROM pg_available_extensions WHERE name='timescaledb'"
        )
        if not has_ext:
            pytest.skip("TimescaleDB not installed on this server")
        assert rt.timescale_enabled is True
        ht = await conn.fetchval(
            "SELECT count(*) FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'measurements'"
        )
        assert ht == 1
        job = await conn.fetchval(
            "SELECT count(*) FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_retention' AND hypertable_name = 'measurements'"
        )
        assert job == 1
    finally:
        await conn.close()
    assert {
        "users",
        "groups",
        "devices",
        "sensors",
        "sensor_channels",
        "measurements",
        "events",
        "notification_channels",
        "alert_rules",
        "alert_rule_channels",
        "settings",
        "alembic_version",
    } <= await _tables(rt.settings.database_url)


async def test_fallback_mode(databases, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(databases["fallback"], tmp_path, NETMON_DISABLE_TIMESCALE="1")
    rt = Runtime(settings)
    await rt.start(run_poller=False, maintenance=False)
    try:
        assert rt.timescale_enabled is False
        async with rt.sessionmaker() as session:
            idx = (
                (
                    await session.execute(
                        text("SELECT indexname FROM pg_indexes WHERE tablename = 'measurements'")
                    )
                )
                .scalars()
                .all()
            )
            assert "ix_measurements_time_brin" in idx
            assert "ix_measurements_sensor_channel_time" in idx
            msgs = (
                (await session.execute(text("SELECT message FROM events WHERE kind = 'system'")))
                .scalars()
                .all()
            )
            assert "TimescaleDB not available — using plain PostgreSQL" in msgs
        # history with date_bin
        await _check_history(rt, None, 4242, timescale=False)
        # retention deletes in batches in fallback mode
        now = datetime.now(UTC)
        async with rt.sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO measurements (time, sensor_id, channel, value) "
                    "SELECT CAST(:old AS timestamptz) - make_interval(secs => g), 1, 'x', g "
                    "FROM generate_series(1, 25) g"
                ),
                {"old": now - timedelta(days=200)},
            )
            await session.execute(
                text(
                    "INSERT INTO measurements (time, sensor_id, channel, value) "
                    "VALUES (:t, 1, 'x', 1)"
                ),
                {"t": now},
            )
            await session.commit()
        removed = await retention.delete_old_measurements(rt, 90, batch=10)
        assert removed == 25
        async with rt.sessionmaker() as session:
            left = (
                await session.execute(text("SELECT count(*) FROM measurements WHERE sensor_id = 1"))
            ).scalar()
        assert left == 1
        # second instance against the same DB cannot take the advisory lock
        rt2 = Runtime(settings)
        with pytest.raises(LockHeldError):
            await rt2.start(run_poller=False, maintenance=False)
        await rt2.stop()
    finally:
        await rt.stop()
    # migrations are idempotent: start again
    rt3 = Runtime(settings)
    await rt3.start(run_poller=False, maintenance=False)
    assert rt3.timescale_enabled is False
    await rt3.stop()


async def test_extension_failure_falls_back(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A role without CREATE on the database cannot CREATE EXTENSION -> fallback, no crash."""
    assert TEST_DATABASE_URL
    tag = secrets.token_hex(3)
    role, db = f"netmon_nosu_{tag}", f"netmon_test_nosu_{tag}"
    admin = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    try:
        await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'pw' NOSUPERUSER")
        # template0: some images (e.g. timescale/timescaledb) preinstall the extension in template1,
        # which would make this database Timescale-enabled before the limited role ever connects.
        await admin.execute(f'CREATE DATABASE "{db}" TEMPLATE template0')
        await admin.execute(f'REVOKE CREATE ON DATABASE "{db}" FROM PUBLIC')
        dbconn = await asyncpg.connect(_plain(db_url_for(db)))
        try:
            await dbconn.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {role}")
        finally:
            await dbconn.close()
        parts = urlsplit(db_url_for(db))
        host = parts.netloc.split("@")[-1]
        limited = urlunsplit((parts.scheme, f"{role}:pw@{host}", parts.path, "", ""))
        engine = make_engine(limited.replace("postgresql://", "postgresql+asyncpg://"))
        try:
            await run_migrations(engine)
            enabled = await timescale.bootstrap(engine, 90)
            assert enabled is False
            async with engine.connect() as conn:
                n = (await conn.execute(text("SELECT count(*) FROM measurements"))).scalar()
                assert n == 0
        finally:
            await engine.dispose()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
        await admin.close()
