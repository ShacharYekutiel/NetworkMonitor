"""Migrations + Timescale bootstrap in both modes."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from sqlalchemy import text

from netmon.db import timescale
from netmon.db.engine import make_engine
from netmon.db.migrate import migration_head, run_migrations
from netmon.runtime import LockHeldError, Runtime
from netmon.services import retention
from tests.conftest import TEST_DATABASE_URL, _plain, db_url_for, make_settings, requires_db
from tests.test_poller import _check_history

pytestmark = requires_db
HEAD = migration_head()  # this version's head (the migration scripts), never hard-coded


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


@requires_db
async def test_database_newer_than_this_version_starts_without_migrating(  # type: ignore[no-untyped-def]
    tmp_path, caplog
) -> None:
    """[1.4.1] Rollback safety: the previous image is started on a database that a newer
    release already migrated. It must start (and say so) instead of failing with
    "Can't locate revision"."""
    import logging

    name = f"netmon_test_{secrets.token_hex(4)}_ahead"
    admin = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        engine = make_engine(db_url_for(name).replace("postgresql://", "postgresql+asyncpg://"))
        try:
            await run_migrations(engine)
            async with engine.begin() as conn:
                assert (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one() == HEAD
                await conn.execute(text("UPDATE alembic_version SET version_num = '9999'"))
            with caplog.at_level(logging.WARNING, logger="netmon.db.migrate"):
                await run_migrations(engine)  # must not raise
            assert "9999" in caplog.text and "newer release" in caplog.text
            async with engine.begin() as conn:
                # untouched: still the newer revision, nothing was downgraded or re-run
                assert (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one() == "9999"
        finally:
            await engine.dispose()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


# ------------------------------------------------------------------ [premium] connected kind
def _premium_revision() -> tuple[str, str]:
    """(revision, down_revision) of the migration that adds `port_kind_overrides` (found by
    content: its number may change when branches merge)."""
    from alembic.script import ScriptDirectory

    from netmon.db.migrate import _alembic_config

    for sc in ScriptDirectory.from_config(_alembic_config()).walk_revisions():
        if "port_kind_overrides" in " ".join(getattr(sc.module, "UPGRADE", [])):
            return sc.revision, str(sc.down_revision)
    raise AssertionError("migration not found")


@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_premium_up_down_up(mode: str, rt) -> None:  # type: ignore[no-untyped-def]
    assert TEST_DATABASE_URL
    if mode == "timescale" and rt.settings.netmon_disable_timescale:
        pytest.skip("suite runs in forced fallback mode")
    _, down = _premium_revision()
    name = f"netmon_test_mp_{secrets.token_hex(3)}"
    admin = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = make_engine(db_url_for(name).replace("postgresql://", "postgresql+asyncpg://"))
    url = db_url_for(name)

    async def columns(conn: asyncpg.Connection) -> set[str]:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'topology_neighbors'"
        )
        return {r["column_name"] for r in rows}

    try:
        await run_migrations(engine)
        enabled = await timescale.bootstrap(engine, 90, disabled=(mode == "fallback"))
        if mode == "timescale" and not enabled:
            pytest.skip("TimescaleDB not available")
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == HEAD
            assert "port_kind_overrides" in await _tables(url)
            assert "capabilities" in await columns(conn)
            gid = await conn.fetchval("INSERT INTO groups (name) VALUES ('g') RETURNING id")
            did = await conn.fetchval(
                "INSERT INTO devices (group_id, name, host) VALUES ($1, 'd', 'h') RETURNING id", gid
            )
            await conn.execute(
                "INSERT INTO port_kind_overrides (device_id, if_index, kind) VALUES ($1, 3, "
                "'printer')",
                did,
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO port_kind_overrides (device_id, if_index, kind) "
                    "VALUES ($1, 4, 'toaster')",
                    did,
                )
            await conn.execute(
                "INSERT INTO topology_neighbors (chassis_id, capabilities) "
                "VALUES ('aa:bb:cc:dd:ee:ff', '{bridge,telephone}')"
            )
            await conn.execute(
                "INSERT INTO measurements (time, sensor_id, channel, value) "
                "VALUES (now(), 1, 'value', 1)"
            )
        finally:
            await conn.close()
        async with engine.connect() as c:
            await c.run_sync(lambda sc: _alembic(sc, "downgrade", down))
            await c.commit()
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == down
            assert "port_kind_overrides" not in await _tables(url)
            assert "capabilities" not in await columns(conn)
            assert await conn.fetchval("SELECT count(*) FROM topology_neighbors") == 1
            assert await conn.fetchval("SELECT count(*) FROM measurements") == 1
        finally:
            await conn.close()
        await run_migrations(engine)  # up again
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == HEAD
            assert await conn.fetchval("SELECT count(*) FROM port_kind_overrides") == 0
            assert "capabilities" in await columns(conn)
        finally:
            await conn.close()
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


# ------------------------------------------------------------------ [profiles] 0019
@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_0019_connection_profiles(mode: str, rt) -> None:  # type: ignore[no-untyped-def]
    """Upgrade changes no effective credential; downgrade materialises every reference into
    the node's column (overwriting dormant manual data), removes the discovery option keys,
    and stops on an unreadable profile secret without changing anything."""
    import json

    from netmon.configbackup import credentials as ssh_credentials
    from netmon.poller.snmp_client import resolve_credentials
    from netmon.services import crypto, secretbox

    assert TEST_DATABASE_URL
    if mode == "timescale" and rt.settings.netmon_disable_timescale:
        pytest.skip("suite runs in forced fallback mode")
    key = secrets.token_urlsafe(48)
    name = f"netmon_test_m21_{secrets.token_hex(3)}"
    admin = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = make_engine(db_url_for(name).replace("postgresql://", "postgresql+asyncpg://"))
    url = db_url_for(name)
    saved = secretbox.state()
    secretbox.configure(key, plaintext_copy=True)
    try:
        await run_migrations(engine, secret_key=key, target="0018")
        enabled = await timescale.bootstrap(engine, 90, disabled=(mode == "fallback"))
        if mode == "timescale" and not enabled:
            pytest.skip("TimescaleDB not available")
        conn = await asyncpg.connect(_plain(url))
        try:
            g = await conn.fetchval(
                "INSERT INTO groups (name, snmp_credentials) VALUES ('g', NULL) RETURNING id"
            )
            await conn.execute(
                "UPDATE groups SET snmp_credentials = $1::jsonb WHERE id = $2",
                json.dumps(secretbox.seal_snmp({"version": "v2c", "community": "gc"}, "group", g)),
                g,
            )
            d1 = await conn.fetchval(
                "INSERT INTO devices (group_id, name, host) VALUES ($1, 'd1', 'h1') RETURNING id", g
            )
            d2 = await conn.fetchval(
                "INSERT INTO devices (group_id, name, host) VALUES ($1, 'd2', 'h2') RETURNING id", g
            )
            await conn.execute(
                "UPDATE devices SET snmp_credentials = $1::jsonb WHERE id = $2",
                json.dumps(
                    secretbox.seal_snmp({"version": "v2c", "community": "own"}, "device", d2)
                ),
                d2,
            )
            dp = await conn.fetchval(
                "INSERT INTO discovery_profiles (name, targets, group_id, options) "
                "VALUES ('dp', '{h}', $1, '{\"ping\": true}') RETURNING id",
                g,
            )

            async def snapshot() -> list[Any]:
                return [
                    tuple(r)
                    for r in await conn.fetch(
                        "SELECT 'g', id, snmp_credentials::text, ssh_credentials::text FROM groups "
                        "UNION ALL SELECT 'd', id, snmp_credentials::text, ssh_credentials::text "
                        "FROM devices ORDER BY 1, 2"
                    )
                ]

            def effective(rows: list[Any], did: int) -> dict[str, Any]:
                by = {(t, i): s for t, i, s, _ in rows}
                dev = by[("d", did)]
                grp = by[("g", g)]
                return resolve_credentials(
                    json.loads(dev) if dev else None, [(g, json.loads(grp) if grp else None)], did
                )[0]

            before = await snapshot()
        finally:
            await conn.close()
        await run_migrations(engine, secret_key=key)  # 0018 -> head
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == HEAD
            after = await snapshot()
            assert after == before  # no data changed: every effective credential identical
            assert effective(after, d1) == effective(before, d1)
            assert effective(after, d1)["community"] == "gc"
            assert effective(after, d2)["community"] == "own"
            # profiles: d1 references an SNMP profile; d2 keeps its manual data dormant behind
            # one; the group references an SSH profile; the discovery profile lists both
            sp = await conn.fetchval(
                "INSERT INTO connection_profiles (kind, name, data) VALUES ('snmp', 'p', '{}') "
                "RETURNING id"
            )
            await conn.execute(
                "UPDATE connection_profiles SET data = $1::jsonb WHERE id = $2",
                json.dumps(
                    secretbox.seal_with(
                        key, {"version": "v2c", "community": "pc", "port": 1161},
                        secretbox.SNMP_FIELDS, secretbox.conn_profile_owner(sp), False,
                    )
                ),
                sp,
            )  # fmt: skip
            hp = await conn.fetchval(
                "INSERT INTO connection_profiles (kind, name, data) VALUES ('ssh', 'h', $1::jsonb) "
                "RETURNING id",
                json.dumps(
                    {"username": "su", "port": 22,
                     "password_enc": crypto.encrypt(key, "spw", b"ssh.password")}
                ),
            )  # fmt: skip
            refs = [("snmp", sp, "device_id", d1), ("snmp", sp, "device_id", d2),
                    ("ssh", hp, "group_id", g)]  # fmt: skip
            for kind, pid, col, node in refs:
                await conn.execute(
                    f"INSERT INTO connection_profile_assignments (kind, profile_id, {col}) "
                    "VALUES ($1, $2, $3)",
                    kind,
                    pid,
                    node,
                )
            await conn.execute(
                "UPDATE discovery_profiles SET options = options || $1::jsonb WHERE id = $2",
                json.dumps({"snmp_profile_ids": [sp], "ssh_profile_ids": [hp],
                            "ssh_credentials": {"username": "t"}}),
                dp,
            )  # fmt: skip
            with_refs = await snapshot()
        finally:
            await conn.close()
        # an unreadable profile secret stops the downgrade, nothing changed
        conn = await asyncpg.connect(_plain(url))
        try:
            good = await conn.fetchval("SELECT data FROM connection_profiles WHERE id = $1", sp)
            await conn.execute(
                "UPDATE connection_profiles SET data = $1::jsonb WHERE id = $2",
                json.dumps(secretbox.seal_with(
                    secrets.token_urlsafe(48), {"community": "x"}, secretbox.SNMP_FIELDS,
                    secretbox.conn_profile_owner(sp), False,
                )),
                sp,
            )  # fmt: skip
        finally:
            await conn.close()
        with pytest.raises(RuntimeError, match="cannot be decrypted"):
            await run_migrations(engine, secret_key=key, target="0018")
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == HEAD
            assert await snapshot() == with_refs
            assert await conn.fetchval("SELECT count(*) FROM connection_profile_assignments") == 3
            await conn.execute(
                "UPDATE connection_profiles SET data = $1::jsonb WHERE id = $2", good, sp
            )
        finally:
            await conn.close()
        # downgrade: materialised into the node columns (the profile value wins over dormant)
        await run_migrations(engine, secret_key=key, target="0018")
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0018"
            tables = await _tables(url)
            assert "connection_profiles" not in tables
            assert "connection_profile_assignments" not in tables
            cols = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name IN ('discovery_results', 'discovery_scans')"
                )
            }
            assert not cols & {"snmp_profile_id", "ssh_ok", "ssh_attempted", "ssh_candidates"}
            opts = json.loads(
                await conn.fetchval("SELECT options FROM discovery_profiles WHERE id = $1", dp)
            )
            assert opts == {"ping": True}
            rows = await snapshot()
            for did in (d1, d2):  # 1.5 polls (the 0018 resolver) use the profile's values
                creds = effective(rows, did)
                assert (creds["community"], creds["port"]) == ("pc", 1161)
            stored = json.loads(
                await conn.fetchval("SELECT ssh_credentials FROM groups WHERE id = $1", g)
            )
            assert ssh_credentials.decrypt(stored, key).password == "spw"
        finally:
            await conn.close()
        await run_migrations(engine, secret_key=key)  # up again
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == HEAD
            assert await conn.fetchval("SELECT count(*) FROM connection_profiles") == 0
        finally:
            await conn.close()
    finally:
        secretbox.restore(saved)
        await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
