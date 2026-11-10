"""[3] Config backup end to end against the asyncssh device simulator (tests/ssh_sim.py):
feature toggle, SSH credentials API, TOFU host keys, versions on change, diff, search, export,
event sensor conditions, syslog trigger, schedule, retention, migration 0007."""

from __future__ import annotations

import io
import secrets
import zipfile
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy import text

from netmon.configbackup.runner import run_retention
from netmon.db import timescale
from netmon.db.engine import make_engine
from netmon.db.migrate import run_migrations
from netmon.runtime import Runtime
from tests.conftest import TEST_DATABASE_URL, _plain, db_url_for, make_device
from tests.conftest import requires_db as _requires_db
from tests.ingest_helpers import send_udp, wait_for
from tests.ssh_sim import CISCO_CONFIG, COMWARE_CONFIG, Sim
from tests.test_2b3_ingest import features

pytestmark = _requires_db


@pytest.fixture
async def sim() -> AsyncIterator[Sim]:
    s = await Sim().start()
    yield s
    s.stop()


async def ssh_device(c: httpx.AsyncClient, sim: Sim, **kw: Any) -> dict[str, Any]:
    return await make_device(
        c,
        name=kw.pop("name", "sw1"),
        host="127.0.0.1",
        ssh_credentials={"username": "netmon", "password": kw.pop("password", "pw"),
                         "port": sim.port},
        config_backup_driver=kw.pop("driver", "hpe_comware"),
        **kw,
    )  # fmt: skip


async def status(c: httpx.AsyncClient, did: int) -> dict[str, Any]:
    r = await c.get(f"/api/devices/{did}/config")
    assert r.status_code == 200, r.text
    return r.json()


async def sensor(c: httpx.AsyncClient, did: int) -> dict[str, Any] | None:
    items = (
        await c.get("/api/sensors", params={"device_id": did, "type": "config_backup"})
    ).json()["items"]
    return items[0] if items else None


async def test_feature_off_and_ssh_api(rt: Runtime, admin: httpx.AsyncClient, sim: Sim) -> None:
    s = (await admin.get("/api/settings")).json()
    for k in ("config_backup", "flows", "new_device_alerts", "sso_kerberos", "sso_oidc",
              "reports", "noc", "chat_ack"):  # fmt: skip
        assert s[f"feature_{k}_enabled"] is False
    assert s["config_backup_time"] == "02:47" and s["config_backup_mask_secrets"] is True
    d = await ssh_device(admin, sim)
    assert d["ssh_credentials"]["password"] is None and d["ssh_credentials"]["password_set"]
    assert d["effective_ssh_source"] == "device" and d["config_backup"] == "auto"
    assert d["config_backup_driver"] == "hpe_comware" and d["newdev_excluded_ports"] == []
    for path in (
        "/api/config-backups",
        "/api/config-backups/status",
        f"/api/devices/{d['id']}/config",
        f"/api/devices/{d['id']}/config/versions",
    ):
        r = await admin.get(path)  # fmt: skip
        assert r.status_code == 409 and r.json()["error"]["code"] == "feature_disabled", path
    # the SSH test works while OFF; the key is only reported (no TOFU pin)
    r = await admin.post(f"/api/devices/{d['id']}/ssh/test", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["driver"] == "hpe_comware"
    assert [st["step"] for st in body["steps"]] == [
        "connect", "host_key", "auth", "prompt", "setup", "command"
    ]  # fmt: skip
    assert "<secret removed>" in body["output_preview"]
    async with rt.sessionmaker() as session:
        assert (await session.execute(text("SELECT count(*) FROM ssh_host_keys"))).scalar() == 0
    r = await admin.post(
        f"/api/devices/{d['id']}/ssh/test", json={"ssh_credentials": {"password": "bad"}}
    )
    assert r.status_code == 502
    err = r.json()["error"]
    assert err["code"] == "ssh_error" and err["details"][0]["error"] == "auth_failed"
    assert err["details"][0]["step"] == "auth" and "bad" not in r.text
    # unknown driver override -> 422; group inheritance
    r = await admin.patch(f"/api/devices/{d['id']}", json={"config_backup_driver": "nope"})
    assert r.status_code == 422
    r = await admin.patch(f"/api/devices/{d['id']}", json={"ssh_credentials": None})
    assert r.json()["effective_ssh_source"] == "none"
    g = (await admin.get("/api/groups", params={"parent_id": "root"})).json()["items"][0]
    r = await admin.patch(
        f"/api/groups/{g['id']}", json={"ssh_credentials": {"username": "u", "password": "p"}}
    )
    assert r.status_code == 200 and r.json()["ssh_credentials"]["password_set"]
    dev = (await admin.get(f"/api/devices/{d['id']}")).json()
    assert dev["effective_ssh_source"] == "group" and dev["effective_ssh_group_id"] == g["id"]
    r = await admin.patch(f"/api/groups/{g['id']}", json={"ssh_credentials": {"username": "u"}})
    assert r.status_code == 200  # secrets kept
    r = await admin.post(
        "/api/devices",
        json={"group_id": g["id"], "name": "x", "host": "h",
              "ssh_credentials": {"username": "u", "private_key": "garbage"}},
    )  # fmt: skip
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_private_key"


async def test_backup_versions_diff_search_export(
    rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient, sim: Sim
) -> None:
    await features(admin, config_backup=True, audit=True)
    d = await ssh_device(admin, sim)
    did = d["id"]
    st = await status(admin, did)
    assert st["eligible"] and st["driver"] == "hpe_comware" and st["driver_source"] == "override"
    await wait_for(lambda: sensor(admin, did), 20)  # auto-created for eligible devices
    r = await admin.post(f"/api/devices/{did}/config/backup")
    assert r.status_code == 202 and r.json() == {"queued": 1}
    await wait_for(lambda: _result(admin, did, "ok"), 20)
    st = await status(admin, did)
    assert st["versions"] == 1 and st["last_trigger"] == "manual"
    assert st["host_key"]["state"] == "pinned"
    assert st["host_key"]["fingerprint"].startswith("SHA256:")
    assert sim.commands == ["screen-length disable", "display current-configuration", "exit"]
    # unchanged
    await rt.configbackup.attempt(did, "manual")
    st = await status(admin, did)
    assert st["last_result"] == "unchanged" and st["versions"] == 1
    # changed -> new version, config_change event
    sim.config = COMWARE_CONFIG.replace("sysname sw1", "sysname sw1-new\n vlan 20")
    await rt.configbackup.attempt(did, "manual")
    st = await status(admin, did)
    assert st["last_result"] == "ok" and st["versions"] == 2 and st["last_change_at"]
    vers = (await viewer.get(f"/api/devices/{did}/config/versions")).json()
    assert vers["total"] == 2 and [v["version_no"] for v in vers["items"]] == [2, 1]
    new, old = vers["items"]
    assert new["is_latest"] and not new["initial"] and old["initial"]
    assert (new["added"], new["removed"]) == (2, 1) and new["masked"]
    assert "content" not in new
    evs = (await admin.get("/api/events", params={"kind": "config_change"})).json()["items"]
    assert [e["message"] for e in evs] == ["Configuration changed (+2 −1 lines), version 2"]
    # content / diff / search / export: admin only
    assert (await viewer.get(f"/api/devices/{did}/config/versions/{new['id']}")).status_code == 403
    v = (await admin.get(f"/api/devices/{did}/config/versions/{new['id']}")).json()
    assert "sysname sw1-new" in v["content"] and "$c$3$xyz" not in v["content"]
    diff = (await admin.get(f"/api/devices/{did}/config/diff", params={"from": old["id"]})).json()
    assert diff["added"] == 2 and diff["removed"] == 1 and "+ sysname sw1-new" in diff["diff"]
    r = await admin.get(f"/api/devices/{did}/config/versions/{old['id']}/download")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert 'filename="sw1_' in r.headers["content-disposition"]
    found = (await admin.get("/api/config-backups/search", params={"q": "vlan 20"})).json()
    assert found["truncated"] is False
    assert found["items"][0]["matches"] == [{"line": 5, "text": " vlan 20"}]
    rx = (
        await admin.get("/api/config-backups/search", params={"q": "sysname .*new", "regex": True})
    ).json()
    assert rx["items"][0]["device_id"] == did
    # QA phase 3: `^`/`$` anchor per line (PostgreSQL `(?n)`), with and without a leading `(?i)`
    for q, n in (
        ("^ sysname sw1-new$", 1), ("^ (vlan|sysname) (20|sw1-new)$", 1), ("(?i)^ VLAN 20$", 1),
        ("^sysname", 0), ("sw1-new$\\n vlan", 0),
    ):  # fmt: skip
        r = await admin.get("/api/config-backups/search", params={"q": q, "regex": True})
        assert r.status_code == 200, (q, r.text)
        assert len(r.json()["items"]) == n, (q, r.json())
    anch = (
        await admin.get("/api/config-backups/search", params={"q": "^ vlan 20$", "regex": True})
    ).json()
    assert anch["items"][0]["matches"] == [{"line": 5, "text": " vlan 20"}]
    r = await admin.get("/api/config-backups/search", params={"q": "(", "regex": True})
    assert r.status_code == 422
    allv = (
        await admin.get(
            "/api/config-backups/search", params={"q": "sysname sw1", "all_versions": True}
        )
    ).json()
    assert allv["items"][0]["version_id"] == new["id"]  # newest matching version
    r = await admin.get("/api/config-backups/export.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert names == ["sw1.cfg"]
    lst = (await viewer.get("/api/config-backups")).json()
    assert lst["total"] == 1 and lst["items"][0]["versions"] == 2
    rs = (await viewer.get("/api/config-backups/status")).json()
    assert rs["eligible_devices"] == 1 and len(rs["drivers"]) == 7 and rs["driver_errors"] == []
    assert rs["next_schedule_at"] is not None
    actions = [
        a["action"] for a in (await admin.get("/api/audit", params={"limit": 200})).json()["items"]
    ]
    assert "config.view" in actions and "config.export" in actions
    await features(admin, timeline=True)
    tl = (await viewer.get(f"/api/devices/{did}/timeline", params={"kinds": ["config"]})).json()[
        "items"
    ]
    assert [i["kind"] for i in tl] == ["config"] and tl[0]["message"].startswith("Configuration")
    # change alert (off by default: event only)
    s = await sensor(admin, did)
    assert s is not None and s["status"] == "up"


async def _result(c: httpx.AsyncClient, did: int, want: str) -> bool:
    st = await status(c, did)
    return st["last_result"] == want and not st["running"]


async def test_host_key_change_and_failures(
    rt: Runtime, admin: httpx.AsyncClient, sim: Sim
) -> None:
    await features(admin, config_backup=True, audit=True)
    d = await ssh_device(admin, sim)
    did = d["id"]
    await rt.configbackup.attempt(did, "manual")
    pinned = (await status(admin, did))["host_key"]["fingerprint"]
    sim.new_host_key()
    sim.commands.clear()
    await rt.configbackup.attempt(did, "manual")
    st = await status(admin, did)
    assert st["last_result"] == "failed" and st["last_error"] == "host_key_changed"
    assert st["eligible"] is False and st["reason"] == "host_key_changed"
    hk = st["host_key"]
    assert hk["state"] == "changed" and hk["fingerprint"] == pinned
    assert hk["presented_fingerprint"] != pinned
    assert sim.commands == []  # nothing sent to the impostor
    s = await sensor(admin, did)
    assert s["status"] == "down"
    sysev = (await admin.get("/api/events", params={"kind": "system"})).json()["items"]
    assert any("host key of sw1 changed" in e["message"] for e in sysev)
    r = await admin.post(f"/api/devices/{did}/config/backup")
    assert r.status_code == 400 and r.json()["error"]["details"] == [{"reason": "host_key_changed"}]
    r = await admin.post(f"/api/devices/{did}/config/host-key/accept", json={"fingerprint": pinned})
    assert r.status_code == 409 and r.json()["error"]["code"] == "host_key_mismatch"
    r = await admin.post(
        f"/api/devices/{did}/config/host-key/accept",
        json={"fingerprint": hk["presented_fingerprint"]},
    )
    assert r.status_code == 200 and r.json()["state"] == "pinned"
    assert r.json()["accepted_by"] == "admin"
    assert (await sensor(admin, did))["status"] == "up"
    r = await admin.post(f"/api/devices/{did}/config/host-key/accept", json={"fingerprint": pinned})
    assert r.status_code == 400 and r.json()["error"]["code"] == "no_host_key_change"
    await rt.configbackup.attempt(did, "manual")
    assert (await status(admin, did))["last_result"] == "unchanged"
    assert (await admin.delete(f"/api/devices/{did}/config/host-key")).status_code == 204
    assert (await status(admin, did))["host_key"] is None
    # consecutive failures -> backup_failed condition (threshold 2), removed by a success
    await admin.patch(f"/api/devices/{did}", json={"ssh_credentials": {"password": "bad"}})
    await rt.configbackup.attempt(did, "manual")
    assert (await sensor(admin, did))["status"] == "up"
    await rt.configbackup.attempt(did, "manual")
    st = await status(admin, did)
    assert st["consecutive_failures"] == 2 and st["last_error"] == "auth_failed"
    s = await sensor(admin, did)
    assert s["status"] == "warning" and "auth_failed" in s["status_message"]
    await admin.patch(f"/api/devices/{did}", json={"ssh_credentials": {"password": "pw"}})
    await rt.configbackup.attempt(did, "manual")
    assert (await sensor(admin, did))["status"] == "up"
    # config change alert
    await admin.put("/api/settings", json={"config_change_alert": True})
    sim.config = COMWARE_CONFIG + "\n#"
    await rt.configbackup.attempt(did, "manual")
    s = await sensor(admin, did)
    assert s["status"] == "warning" and s["status_message"].startswith("Configuration changed")
    # OFF: conditions removed, sensor up with the OFF message; data kept
    await features(admin, config_backup=False)
    s = await sensor(admin, did)
    assert s["status"] == "up" and s["status_message"] == "Configuration backup is turned off"
    async with rt.sessionmaker() as session:
        assert (await session.execute(text("SELECT count(*) FROM config_versions"))).scalar() == 2
    actions = {
        a["action"] for a in (await admin.get("/api/audit", params={"limit": 200})).json()["items"]
    }
    assert {"config.host_key_accept", "config.host_key_reset", "device.update"} <= actions
    rows = (await admin.get("/api/audit", params={"action": "device.update"})).json()["items"]
    assert "bad" not in str(rows) and "pw" not in str([r["changes"] for r in rows])


async def test_cisco_enable_and_exec_driver(rt: Runtime, admin: httpx.AsyncClient) -> None:
    await features(admin, config_backup=True)
    cisco = await Sim(mode="cisco", config=CISCO_CONFIG).start()
    exec_sim = await Sim(mode="exec", config="/ip address\nadd address=10.0.0.1/24").start()
    try:
        c = await make_device(
            admin, name="r1", host="127.0.0.1", config_backup_driver="cisco_ios",
            ssh_credentials={"username": "netmon", "password": "pw", "enable_password": "en",
                             "port": cisco.port},
        )  # fmt: skip
        m = await make_device(
            admin, name="mt1", host="127.0.0.1", config_backup_driver="mikrotik_routeros",
            ssh_credentials={"username": "netmon", "password": "pw", "port": exec_sim.port},
        )  # fmt: skip
        await rt.configbackup.attempt(c["id"], "manual")
        await rt.configbackup.attempt(m["id"], "manual")
        assert (await status(admin, c["id"]))["last_result"] == "ok"
        assert cisco.commands[0] == "enable" and cisco.commands[-2:] == [
            "show running-config",
            "exit",
        ]
        v = (await admin.get(f"/api/devices/{c['id']}/config/versions")).json()["items"][0]
        content = (await admin.get(f"/api/devices/{c['id']}/config/versions/{v['id']}")).json()
        assert content["content"] == "!\nhostname r1\nenable secret 9 <secret removed>\n!\nend\n"
        assert exec_sim.commands == ["/export"]
        assert (await status(admin, m["id"]))["last_result"] == "ok"
    finally:
        cisco.stop()
        exec_sim.stop()


async def test_no_credentials_and_unreachable(rt: Runtime, admin: httpx.AsyncClient) -> None:
    await features(admin, config_backup=True)
    d = await make_device(admin, name="nocred", host="127.0.0.1", config_backup_driver="aruba_cx")
    st = await status(admin, d["id"])
    assert not st["eligible"] and st["reason"] == "no_credentials"
    r = await admin.post(f"/api/devices/{d['id']}/config/backup")
    assert r.status_code == 400 and r.json()["error"]["details"] == [{"reason": "no_credentials"}]
    nd = await make_device(
        admin, name="nodrv", host="127.0.0.1",
        ssh_credentials={"username": "u", "password": "p"},
    )  # fmt: skip
    assert (await status(admin, nd["id"]))["reason"] == "no_driver"
    off = await make_device(admin, name="off", host="127.0.0.1", config_backup="off")
    assert (await status(admin, off["id"]))["reason"] == "mode_off"
    # template / sysObjectID resolution
    async with rt.sessionmaker() as session:
        await session.execute(
            text("UPDATE devices SET identity = CAST(:i AS jsonb) WHERE id = :d"),
            {"i": '{"sys_object_id": "1.3.6.1.4.1.2620.1.6.123"}', "d": nd["id"]},
        )
        await session.commit()
    st = await status(admin, nd["id"])
    assert st["driver"] == "checkpoint_gaia" and st["driver_source"] == "sys_object_id"
    # connect refused (nothing listens on port 1) -> scheduled attempts retry once after 15 min
    r = await make_device(
        admin, name="dead", host="127.0.0.1", config_backup_driver="hpe_comware",
        ssh_credentials={"username": "u", "password": "p", "port": 1},
    )  # fmt: skip
    await rt.configbackup.attempt(r["id"], "schedule")
    st = await status(admin, r["id"])
    assert st["last_error"] == "connect_refused" and st["next_due_at"] is not None
    assert rt.configbackup._sched_due[r["id"]][1] == "retry"  # noqa: SLF001
    lst = (await admin.get("/api/config-backups", params={"result": "failed"})).json()
    assert [i["device_name"] for i in lst["items"]] == ["dead"]
    assert all(i["device_name"] != "off" for i in (await admin.get("/api/config-backups")).json()[
        "items"])  # fmt: skip


async def test_syslog_trigger_and_schedule(rt: Runtime, admin: httpx.AsyncClient, sim: Sim) -> None:
    await features(admin, config_backup=True, syslog=True)
    d = await ssh_device(admin, sim, name="r9")
    did = d["id"]
    rules = (await admin.get("/api/event-rules")).json()["items"]
    cisco = next(r for r in rules if r["builtin"] == "syslog_cisco_config_i")
    assert cisco["action"] == "config_backup" and cisco["enabled"] is False
    r = await admin.patch(f"/api/event-rules/{cisco['id']}", json={"enabled": True})
    assert r.status_code == 200, r.text
    port = rt.ingest.syslog_udp.port
    await rt.ingest.rebuild_sources()
    send_udp(port, b"<189>r9: %SYS-5-CONFIG_I: Configured from console", "127.0.0.1")
    runner = rt.configbackup
    await wait_for(lambda: _async(did in runner._event_due), 10)  # noqa: SLF001
    due = runner._event_due[did]  # noqa: SLF001
    assert 100 < (due - rt.clock.now()).total_seconds() <= 120  # debounce 120 s

    # QA phase 3: the debounced due time is persisted (next_due_at), not only kept in memory
    async def due_shown() -> bool:
        return (await status(admin, did))["next_due_at"] is not None

    await wait_for(due_shown, 10)
    shown = datetime.fromisoformat((await status(admin, did))["next_due_at"].replace("Z", "+00:00"))
    assert abs((shown - due).total_seconds()) < 1

    async def matched() -> bool:
        items = (await admin.get("/api/syslog")).json()["items"]
        return bool(items) and cisco["id"] in items[0]["matched_rule_ids"]

    await wait_for(matched, 10)
    runner._event_due[did] = rt.clock.now()  # noqa: SLF001
    runner._wake.set()  # noqa: SLF001
    await wait_for(lambda: _result(admin, did, "ok"), 20)
    st = await status(admin, did)
    assert st["last_trigger"] == "event" and st["next_due_at"] is None  # cleared after the run
    # a second trigger inside 10 min schedules exactly one run at the window end
    runner.trigger(did)
    assert (runner._event_due[did] - rt.clock.now()).total_seconds() > 400  # noqa: SLF001
    runner._event_due.clear()  # noqa: SLF001
    # daily schedule: every eligible device, staggered (window 0 = all at once)
    await admin.put("/api/settings", json={"config_backup_window_min": 0})
    n = await runner.schedule_pass(rt.clock.now())
    assert n == 1
    await wait_for(lambda: _trigger(admin, did, "schedule"), 20)
    assert runner.last_schedule_at is not None


async def _async(v: Any) -> Any:
    return v


async def _trigger(c: httpx.AsyncClient, did: int, want: str) -> bool:
    st = await status(c, did)
    return st["last_trigger"] == want and not st["running"]


async def test_retention(rt: Runtime, admin: httpx.AsyncClient) -> None:
    d = await make_device(admin, name="old", host="127.0.0.1")
    now = rt.clock.now()
    await admin.put(
        "/api/settings",
        json={"config_backup_max_versions": 5, "config_backup_min_versions": 2,
              "config_backup_retention_days": 30},
    )  # fmt: skip
    r = await admin.put(
        "/api/settings",
        json={"config_backup_min_versions": 10, "config_backup_max_versions": 5},
    )
    assert r.status_code == 422
    async with rt.sessionmaker() as session:
        for i in range(8):
            await session.execute(
                text(
                    "INSERT INTO config_versions (device_id, collected_at, trigger, driver, "
                    "sha256, content, masked, size_bytes, lines, initial, is_latest) VALUES "
                    "(:d, :at, 'schedule', 'x', :sha, 'c', true, 1, 1, :ini, :latest)"
                ),
                {"d": d["id"], "at": now - timedelta(days=i * 10), "sha": f"{i:064d}",
                 "ini": i == 7, "latest": i == 0},
            )  # fmt: skip
        await session.commit()
    assert await run_retention(rt) == 5  # keep latest + 1 (min 2); 3 days > 30 d also go
    async with rt.sessionmaker() as session:
        left = (await session.execute(text("SELECT count(*) FROM config_versions"))).scalar()
    assert left == 3  # ages 0, 10, 20 d


# ------------------------------------------------------------------ migration 0007
PHASE3_TABLES = {
    "ssh_host_keys", "config_backup_status", "config_versions", "flow_exporters", "flow_if_1m",
    "flow_app_1m", "flow_host_1m", "flow_conv_1m", "known_macs", "mac_allowlist",
    "port_flap_episodes", "reports", "report_runs", "kiosk_screens", "chat_identities",
    "chat_link_codes", "telegram_poll_state",
}  # fmt: skip


@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_0007_up_down_up(mode: str, rt: Runtime) -> None:
    from alembic import command

    from netmon.db.migrate import _alembic_config

    assert TEST_DATABASE_URL
    if mode == "timescale" and rt.settings.netmon_disable_timescale:
        pytest.skip("suite runs in forced fallback mode")
    name = f"netmon_test_m7_{secrets.token_hex(3)}"
    adm = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await adm.execute(f'CREATE DATABASE "{name}"')
    url = db_url_for(name)
    engine = make_engine(url.replace("postgresql://", "postgresql+asyncpg://"))

    def downgrade(sync_conn: Any, rev: str) -> None:
        cfg = _alembic_config()
        cfg.attributes["connection"] = sync_conn
        command.downgrade(cfg, rev)

    try:
        await run_migrations(engine)
        enabled = await timescale.bootstrap(engine, 90, disabled=(mode == "fallback"))
        if mode == "timescale" and not enabled:
            pytest.skip("TimescaleDB not available")
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0007"
            tables = {r["tablename"] for r in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}  # fmt: skip
            assert PHASE3_TABLES <= tables
            keys = {r["builtin_key"]: r["action"] for r in await conn.fetch(
                "SELECT builtin_key, action FROM event_rules WHERE NOT enabled")}  # fmt: skip
            assert keys["syslog_cisco_config_i"] == "config_backup" and len(keys) == 7
            if mode == "timescale":  # hourly objects as the flow bootstrap would create them
                await conn.execute(
                    "SELECT create_hypertable('flow_if_1m', 'time', "
                    "if_not_exists => TRUE, create_default_indexes => FALSE)"
                )
                await conn.execute(
                    "CREATE MATERIALIZED VIEW flow_if_1h WITH (timescaledb.continuous) AS "
                    "SELECT time_bucket(INTERVAL '1 hour', time) AS time, exporter_id, "
                    "sum(bytes)::bigint AS bytes FROM flow_if_1m GROUP BY 1, 2 WITH NO DATA"
                )  # fmt: skip
            else:
                await conn.execute("CREATE TABLE flow_if_1h (time timestamptz, x int)")
            gid = await conn.fetchval("INSERT INTO groups (name) VALUES ('g') RETURNING id")
            did = await conn.fetchval(
                "INSERT INTO devices (group_id, name, host, ssh_credentials) VALUES "
                "($1, 'd', 'h', '{\"username\": \"u\"}') RETURNING id",
                gid,
            )
            assert await conn.fetchval("SELECT config_backup FROM devices") == "auto"
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE devices SET config_backup = 'maybe'")
            sid = await conn.fetchval(
                "INSERT INTO sensors (device_id, name, type, config, status) VALUES "
                "($1, 'Configuration backup', 'config_backup', '{}', 'up') RETURNING id",
                did,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO sensors (device_id, name, type, config) VALUES "
                    "($1, 'x', 'config_backup', '{}')",
                    did,
                )
            await conn.execute(
                "INSERT INTO event_conditions (sensor_id, source, key, status, first_at, "
                "last_at, message) VALUES ($1, 'host_key', '', 'down', now(), now(), 'm')",
                sid,
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO event_rules (name, kind, match, action, clear_after_min) "
                    "VALUES ('x', 'syslog', '{}', 'config_backup', 5)"
                )
            await conn.execute(
                "INSERT INTO config_versions (device_id, collected_at, trigger, driver, sha256, "
                "content, masked, size_bytes, lines, initial, is_latest) VALUES ($1, now(), "
                "'manual', 'x', repeat('a', 64), 'c', true, 1, 1, true, true)",
                did,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO config_versions (device_id, collected_at, trigger, driver, "
                    "sha256, content, masked, size_bytes, lines, initial, is_latest) VALUES "
                    "($1, now(), 'manual', 'x', repeat('b', 64), 'c', true, 1, 1, false, true)",
                    did,
                )
            await conn.execute(
                "INSERT INTO users (username, source, display_name) VALUES "
                "('jane@mnidom.net', 'oidc', 'J')"
            )
            await conn.execute(
                "INSERT INTO audit_log (actor_type, action, source) VALUES "
                "('user', 'auth.login', 'oidc')"
            )
            await conn.execute(
                "INSERT INTO events (kind, message) VALUES ('config_change', 'm'), "
                "('new_device', 'n')"
            )
            await conn.execute(
                "INSERT INTO known_macs (mac, state, first_seen, last_seen) VALUES "
                "('00:1b:21:aa:00:01', 'new', now(), now())"
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("INSERT INTO mac_allowlist (prefix, bits) VALUES ('00:1b', 16)")
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO reports (name, language, sections, schedule) VALUES "
                    "('r', 'en', '{availability}', 'weekly')"
                )
            await conn.execute(
                "INSERT INTO reports (name, language, sections, schedule, weekday, send_time) "
                "VALUES ('r', 'en', '{availability}', 'weekly', 1, '07:47')"
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO kiosk_screens (name, token_hash, token_prefix, allowed_sources, "
                    "widgets, language) VALUES ('k', repeat('a', 64), 'nmk_x', '{}', "
                    "'{summary}', 'en')"
                )
        finally:
            await conn.close()
        async with engine.connect() as c:
            await c.run_sync(lambda sc: downgrade(sc, "0006"))
            await c.commit()
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0006"
            tables = {r["tablename"] for r in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}  # fmt: skip
            assert not (PHASE3_TABLES & tables) and "flow_if_1h" not in tables
            assert await conn.fetchval("SELECT count(*) FROM sensors") == 0
            assert await conn.fetchval("SELECT count(*) FROM users") == 0
            assert await conn.fetchval("SELECT count(*) FROM events") == 0
            assert await conn.fetchval("SELECT count(*) FROM event_rules") == 4
            cols = {r["column_name"] for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name IN ('devices', 'groups', 'port_inventory_status')")}  # fmt: skip
            assert not cols & {"ssh_credentials", "config_backup", "config_backup_driver",
                               "newdev_excluded_ports", "newdev_baselined_at"}  # fmt: skip
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("INSERT INTO events (kind) VALUES ('new_device')")
            assert await conn.fetch("SELECT source FROM audit_log") == [(None,)]
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO audit_log (actor_type, action, source) VALUES "
                    "('user', 'x', 'oidc')"
                )
            with pytest.raises(asyncpg.PostgresError):  # append-only trigger back on
                await conn.execute("UPDATE audit_log SET action = 'y'")
        finally:
            await conn.close()
        await run_migrations(engine)
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0007"
            assert await conn.fetchval("SELECT count(*) FROM event_rules") == 7
        finally:
            await conn.close()
    finally:
        await engine.dispose()
        await adm.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await adm.close()


async def test_sensor_sync_due_soon_after_boot(rt: Runtime, admin: httpx.AsyncClient) -> None:
    """CI regression: time.monotonic() counts from boot. With `_last_sensor_sync = 0.0`, a host
    up for less than SENSOR_SYNC_S (1 h) never ran the sensor sync after mark_dirty, so a new
    eligible device got no config_backup sensor. "Sync now" must not depend on uptime."""
    from netmon.configbackup.runner import SENSOR_SYNC_S

    await features(admin, config_backup=True)
    cb = rt.configbackup
    cb.mark_dirty()
    assert 5.0 - cb._last_sensor_sync > SENSOR_SYNC_S  # e.g. 5 s after boot  # noqa: SLF001


async def test_automatic_driver_reads_sysobjectid(
    rt: Runtime, admin: httpx.AsyncClient, sim: Sim, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device added by hand has no stored identity: "Automatic" reads the sysObjectID over
    SNMP (owner's office test: Comware switch -> no_driver unless hpe_comware was chosen)."""
    from netmon.configbackup import sysoid
    from netmon.poller import snmp_client

    calls: list[str] = []

    async def fake_get(host: str, creds: dict[str, Any], oids: list[str]) -> dict[str, Any]:
        calls.append(host)
        return {sysoid.SYS_OBJECT_ID: "1.3.6.1.4.1.25506.11.1.240"}

    monkeypatch.setattr(snmp_client, "snmp_get", fake_get)
    sysoid.forget()
    await features(admin, config_backup=True)
    try:
        d = await ssh_device(admin, sim, name="auto", driver=None)
        assert (await status(admin, d["id"]))["reason"] == "no_driver"
        r = await admin.post(f"/api/devices/{d['id']}/ssh/test", json={})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] and r.json()["driver"] == "hpe_comware"
        st = await status(admin, d["id"])
        assert st["driver"] == "hpe_comware" and st["driver_source"] == "sys_object_id"
        # cached: the runner does not read it again
        await rt.configbackup.attempt(d["id"], "manual")
        assert calls == ["127.0.0.1"]
        # unreadable -> still no_driver, retried only after the short TTL
        sysoid.forget()

        async def dead_get(host: str, creds: dict[str, Any], oids: list[str]) -> dict[str, Any]:
            raise snmp_client.SnmpError("timeout")

        monkeypatch.setattr(snmp_client, "snmp_get", dead_get)
        await rt.configbackup.attempt(d["id"], "manual")
        assert (await status(admin, d["id"]))["reason"] == "no_driver"
    finally:
        sysoid.forget()
