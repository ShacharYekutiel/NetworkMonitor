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
HEAD = "0009"  # [3b] head revision after run_migrations() (0007 -> 0008 v1.3 -> 0009 3b)


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


# ------------------------------------------------------------------ [2a] migration 0003
DISCOVERY_TABLES = {
    "discovery_profiles",
    "discovery_scans",
    "discovery_results",
    "discovery_known_hosts",
}


def _alembic(engine_conn, action: str, rev: str) -> None:  # type: ignore[no-untyped-def]
    from alembic import command

    from netmon.db.migrate import _alembic_config

    cfg = _alembic_config()
    cfg.attributes["connection"] = engine_conn
    getattr(command, action)(cfg, rev)


@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_0003_up_down(mode: str, rt) -> None:  # type: ignore[no-untyped-def]
    assert TEST_DATABASE_URL
    if mode == "timescale" and rt.settings.netmon_disable_timescale:
        pytest.skip("suite runs in forced fallback mode")
    name = f"netmon_test_m3_{secrets.token_hex(3)}"
    admin = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = make_engine(db_url_for(name).replace("postgresql://", "postgresql+asyncpg://"))
    try:
        await run_migrations(engine)
        enabled = await timescale.bootstrap(engine, 90, disabled=(mode == "fallback"))
        if mode == "timescale" and not enabled:
            pytest.skip("TimescaleDB not available")
        assert enabled is (mode == "timescale")
        url = db_url_for(name)
        assert DISCOVERY_TABLES <= await _tables(url)
        conn = await asyncpg.connect(_plain(url))
        try:
            idx = {
                r["indexname"]
                for r in await conn.fetch(
                    "SELECT indexname FROM pg_indexes WHERE tablename LIKE 'discovery_%'"
                )
            }
            assert {
                "ux_discovery_profiles_name_lower",
                "ix_discovery_scans_started_at",
                "ux_discovery_scans_running",
                "ix_discovery_results_scan_id",
            } <= idx
            gid = await conn.fetchval("INSERT INTO groups (name) VALUES ('g') RETURNING id")
            did = await conn.fetchval(
                "INSERT INTO devices (group_id, name, host) VALUES ($1, 'd', 'h') RETURNING id", gid
            )
            for t in ("snmp_custom", "snmp_disk", "ping"):
                await conn.execute(
                    "INSERT INTO sensors (device_id, name, type, config) VALUES ($1, $2, $3, '{}')",
                    did,
                    t,
                    t,
                )
            await conn.execute(
                "INSERT INTO notification_channels (name, type, config) "
                "VALUES ('t', 'telegram', '{}'), ('w', 'webhook', '{}')"
            )
            await conn.execute(
                "INSERT INTO events (kind, message) VALUES ('discovery', 'x'), ('system', 'y')"
            )
            await conn.execute(
                "INSERT INTO measurements (time, sensor_id, channel, value) "
                "VALUES (now(), 1, 'value', 1)"
            )
        finally:
            await conn.close()
        async with engine.connect() as c:
            await c.run_sync(lambda sc: _alembic(sc, "downgrade", "0002"))
            await c.commit()
        assert not (DISCOVERY_TABLES & await _tables(url))
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0002"
            assert [r["type"] for r in await conn.fetch("SELECT type FROM sensors")] == ["ping"]
            assert await conn.fetchval("SELECT count(*) FROM notification_channels") == 1
            assert await conn.fetchval("SELECT count(*) FROM events") == 1
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO sensors (device_id, name, type, config) "
                    "VALUES ($1, 'x', 'snmp_custom', '{}')",
                    did,
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("INSERT INTO events (kind) VALUES ('discovery')")
            # measurements (a hypertable in Timescale mode) are untouched
            assert await conn.fetchval("SELECT count(*) FROM measurements") == 1
        finally:
            await conn.close()
        await run_migrations(engine)  # upgrade again
        assert DISCOVERY_TABLES <= await _tables(url)
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == HEAD
            await conn.execute(
                "INSERT INTO sensors (device_id, name, type, config) "
                "VALUES ($1, 'x', 'snmp_disk', '{}')",
                did,
            )
        finally:
            await conn.close()
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


# ------------------------------------------------------------------ [2b-1] migration 0004
PHASE2B_TABLES = {
    "alert_state",
    "maintenance_windows",
    "topology_neighbors",
    "topology_links",
    "topology_layout",
    "topology_refreshes",
}


@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_0004_up_down_up(mode: str, rt) -> None:  # type: ignore[no-untyped-def]
    assert TEST_DATABASE_URL
    if mode == "timescale" and rt.settings.netmon_disable_timescale:
        pytest.skip("suite runs in forced fallback mode")
    name = f"netmon_test_m4_{secrets.token_hex(3)}"
    admin = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = make_engine(db_url_for(name).replace("postgresql://", "postgresql+asyncpg://"))
    url = db_url_for(name)
    try:
        await run_migrations(engine)
        enabled = await timescale.bootstrap(engine, 90, disabled=(mode == "fallback"))
        if mode == "timescale" and not enabled:
            pytest.skip("TimescaleDB not available")
        # back to 0003 and create Phase 2a state with notified problems
        async with engine.connect() as c:
            await c.run_sync(lambda sc: _alembic(sc, "downgrade", "0003"))
            await c.commit()
        assert not (PHASE2B_TABLES & await _tables(url))
        assert "alert_notified" in await _tables(url)
        conn = await asyncpg.connect(_plain(url))
        try:
            gid = await conn.fetchval("INSERT INTO groups (name) VALUES ('g') RETURNING id")
            did = await conn.fetchval(
                "INSERT INTO devices (group_id, name, host) VALUES ($1, 'd', 'h') RETURNING id", gid
            )
            s1, s2 = [
                await conn.fetchval(
                    "INSERT INTO sensors (device_id, name, type, config, status) "
                    "VALUES ($1, $2, 'ping', '{}', 'down') RETURNING id",
                    did,
                    n,
                )
                for n in ("a", "b")
            ]
            c1, c2 = [
                await conn.fetchval(
                    "INSERT INTO notification_channels (name, type, config) "
                    "VALUES ($1, 'webhook', '{}') RETURNING id",
                    n,
                )
                for n in ("c1", "c2")
            ]
            rule = await conn.fetchval(
                "INSERT INTO alert_rules (name, on_statuses) VALUES ('r', '{down,up}') RETURNING id"
            )
            await conn.execute(
                "INSERT INTO alert_rule_channels VALUES ($1, $2), ($1, $3)", rule, c2, c1
            )
            await conn.execute(
                "INSERT INTO alert_notified (rule_id, sensor_id, status, time) VALUES "
                "($1, $2, 'down', '2026-11-01T10:00:00Z'), ($1, $3, 'warning', now())",
                rule,
                s1,
                s2,
            )
            await conn.execute(
                "INSERT INTO discovery_scans (trigger, status, targets, total_hosts) "
                "VALUES ('manual', 'completed', '{x}', 1)"
            )
            await conn.execute(
                "INSERT INTO discovery_results (scan_id, ip, ping_ok, snmp_ok, device_class, "
                "suggested_template, is_new) VALUES (1, '10.0.0.1', true, true, 'switch', "
                "'generic_snmp', true), (1, '10.0.0.2', true, false, 'unknown', "
                "'generic_ping', true)"
            )
        finally:
            await conn.close()
        await run_migrations(engine)  # 0003 -> 0004
        assert PHASE2B_TABLES <= await _tables(url)
        assert "alert_notified" not in await _tables(url)
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == HEAD
            rows = await conn.fetch("SELECT * FROM alert_state ORDER BY sensor_id")
            assert [(r["sensor_id"], r["status"]) for r in rows] == [(s1, "down"), (s2, "warning")]
            assert rows[0]["notified_channel_ids"] == sorted([c1, c2])
            assert rows[0]["problem_since"] == rows[0]["primary_sent_at"]
            assert rows[0]["problem_since"].isoformat().startswith("2026-11-01T10:00:00")
            assert rows[0]["due_at"] is None and rows[0]["reminders_sent"] == 0
            dr = await conn.fetch(
                "SELECT classified_by, confidence FROM discovery_results ORDER BY id"
            )
            assert [tuple(r) for r in dr] == [("snmp", "medium"), ("ports", "low")]
            idx = {
                r["indexname"]
                for r in await conn.fetch(
                    "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
                )
            }
            assert {
                "ix_alert_state_sensor_id",
                "ix_devices_dependency_device_id",
                "ix_maintenance_windows_group_id",
                "ix_maintenance_windows_device_id",
                "ux_topology_links_ends",
                "ix_topology_links_b_device_id",
                "ix_topology_links_b_neighbor_id",
                "ux_topology_refreshes_running",
            } <= idx
            # new values are accepted now
            await conn.execute("UPDATE sensors SET status = 'unreachable' WHERE id = $1", s2)
            await conn.execute(
                "INSERT INTO events (kind, old_status, new_status, in_maintenance) VALUES "
                "('status_change', 'down', 'unreachable', true), ('acknowledge', 'down', 'down', "
                "false), ('maintenance', NULL, NULL, false), ('system', NULL, NULL, false)"
            )
            await conn.execute(
                "UPDATE alert_state SET status = 'suspended' WHERE sensor_id = $1", s2
            )
            await conn.execute(
                "UPDATE devices SET dependency_mode = 'device', dependency_device_id = $1", did
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO maintenance_windows (name, scope, kind) VALUES ('x', 'global', "
                    "'once')"
                )
            await conn.execute(
                "INSERT INTO maintenance_windows (name, scope, kind, weekdays, start_time, "
                "duration_min) VALUES ('w', 'global', 'weekly', '{7}', '02:00', 90)"
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO topology_refreshes (trigger, status) VALUES "
                    "('manual', 'running'), ('manual', 'running')"
                )
            await conn.execute(
                "INSERT INTO measurements (time, sensor_id, channel, value) "
                "VALUES (now(), 1, 'value', 1)"
            )
        finally:
            await conn.close()
        async with engine.connect() as c:  # 0004 -> 0003
            await c.run_sync(lambda sc: _alembic(sc, "downgrade", "0003"))
            await c.commit()
        assert not (PHASE2B_TABLES & await _tables(url))
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0003"
            back = await conn.fetch("SELECT rule_id, sensor_id, status FROM alert_notified")
            # every row that notified someone (L8): the suspended one too (as down)
            assert sorted((r["sensor_id"], r["status"]) for r in back) == [
                (s1, "down"),
                (s2, "down"),
            ]
            assert await conn.fetchval("SELECT status FROM sensors WHERE id = $1", s2) == "down"
            kinds = sorted(r["kind"] for r in await conn.fetch("SELECT kind FROM events"))
            assert kinds == ["status_change", "system"]
            assert (
                await conn.fetchval("SELECT new_status FROM events WHERE kind = 'status_change'")
                == "down"
            )
            cols = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_name IN "
                    "('devices', 'sensors', 'events', 'alert_rules', 'groups', "
                    "'discovery_results')"
                )
            }
            assert not cols & {
                "dependency_mode",
                "ack_at",
                "in_maintenance",
                "escalation_after_s",
                "identity",
                "fingerprint",
                "lldp_chassis_id",
            }
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE sensors SET status = 'unreachable' WHERE id = $1", s1)
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("INSERT INTO events (kind) VALUES ('acknowledge')")
            assert await conn.fetchval("SELECT count(*) FROM measurements") == 1
        finally:
            await conn.close()
        await run_migrations(engine)  # and up again
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == HEAD
            rows = await conn.fetch("SELECT sensor_id, status FROM alert_state ORDER BY 1")
            assert [(r["sensor_id"], r["status"]) for r in rows] == [(s1, "down"), (s2, "down")]
        finally:
            await conn.close()
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
