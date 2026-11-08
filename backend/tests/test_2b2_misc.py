"""[2b-2] Port job load (48 ports / 2000 MACs: bulk walks, shared semaphore), the scheduler pass
(class detection, due devices), snmp_walk_count and percent_base end to end, migration 0005
up/down/up in both modes."""

from __future__ import annotations

import secrets
import socket
import time
from collections.abc import Iterator
from typing import Any

import asyncpg
import httpx
import pytest

from netmon.db import timescale
from netmon.db.engine import make_engine
from netmon.db.migrate import run_migrations
from netmon.ports import collect
from tests import ports_sims as sims
from tests.conftest import TEST_DATABASE_URL, _plain, db_url_for, make_device, requires_db
from tests.snmpsim_agents import Record, start_agent, system
from tests.test_migrations import _alembic, _tables

pytestmark = requires_db

BIG_IP, SMALL_IP, CUSTOM_IP = "127.0.7.90", "127.0.7.91", "127.0.7.92"


def _free_udp_port(ip: str) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((ip, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def custom_records() -> list[Record]:
    pool = "1.3.6.1.4.1.9.9.48.1.1.1"
    return system("Cisco IOS", "1.3.6.1.4.1.9.1.1", "r1") + [
        (f"{pool}.5.1", 66, "300"),  # used
        (f"{pool}.6.1", 66, "100"),  # free
        (f"{pool}.5.2", 66, "0"),
        (f"{pool}.6.2", 66, "0"),
        ("1.3.6.1.4.1.9.99.1.1", 2, "1"),
        ("1.3.6.1.4.1.9.99.1.2", 2, "2"),
        ("1.3.6.1.4.1.9.99.1.3", 4, "1"),
    ]


@pytest.fixture(scope="module")
def agents() -> Iterator[dict[str, Any]]:
    out: dict[str, Any] = {}
    for key, ip, recs in (
        ("big", BIG_IP, sims.big_switch_records(48, 2000)),
        ("small", SMALL_IP, sims.arubacx_records()),
        ("custom", CUSTOM_IP, custom_records()),
    ):
        a = start_agent(ip, _free_udp_port(ip), {"public": recs})
        if a is None:
            for x in out.values():
                x.stop()
            pytest.skip("snmpsim not available")
        out[key] = a
    yield out
    for a in out.values():
        a.stop()


def _creds(a: Any) -> dict[str, Any]:
    return {"port": a.port, "timeout_s": 2, "retries": 0}


async def test_big_switch_bulk_walks_and_shared_semaphore(rt, admin, agents, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: dict[str, int] = {"walk": 0, "get": 0, "unguarded": 0}
    initial = rt.semaphore._value  # noqa: SLF001
    real_walk, real_get = collect.snmp_walk, collect.snmp_get

    async def walk(*a: Any, **kw: Any) -> Any:
        calls["walk"] += 1
        if rt.semaphore._value >= initial:  # noqa: SLF001 - the shared slot must be held
            calls["unguarded"] += 1
        return await real_walk(*a, **kw)

    async def get(*a: Any, **kw: Any) -> Any:
        calls["get"] += 1
        if rt.semaphore._value >= initial:  # noqa: SLF001
            calls["unguarded"] += 1
        return await real_get(*a, **kw)

    monkeypatch.setattr(collect, "snmp_walk", walk)
    monkeypatch.setattr(collect, "snmp_get", get)
    dev = await make_device(
        admin,
        name="big",
        host=BIG_IP,
        snmp_credentials=_creds(agents["big"]),
        auto_sensors=["ping", "snmp_system"],
    )
    t0 = time.monotonic()
    r = await admin.post(f"/api/devices/{dev['id']}/ports/refresh")
    assert r.status_code == 202
    await rt.ports.idle()
    elapsed = time.monotonic() - t0
    body = (await admin.get(f"/api/devices/{dev['id']}/ports")).json()
    assert body["status"]["error"] is None and body["status"]["ports"] == 48
    assert body["status"]["macs"] == 2000
    assert sum(p["mac_count"] for p in body["ports"]) == 2000
    # one bulk walk per column, independent of the number of ports / MACs; one GET (sysUpTime)
    assert calls["get"] == 1 and calls["walk"] <= 30, calls
    assert calls["unguarded"] == 0
    assert elapsed < 60
    # the second run upserts the same 2000 MACs (first_seen kept)
    await rt.ports.refresh(dev["id"])
    await rt.ports.idle()
    body = (await admin.get(f"/api/devices/{dev['id']}/ports")).json()
    assert body["status"]["macs"] == 2000


async def test_scheduler_pass(rt, admin, agents) -> None:  # type: ignore[no-untyped-def]
    dev = await make_device(
        admin,
        name="cx",
        host=SMALL_IP,
        snmp_credentials=_creds(agents["small"]),
        auto_sensors=["ping"],
    )
    # no SNMP sensor -> no detection, nothing due
    assert await rt.ports.pass_once() == []
    await admin.post("/api/sensors", json={"device_id": dev["id"], "type": "snmp_system"})
    due = await rt.ports.pass_once()  # class detection, then the due device
    assert due == [dev["id"]]
    await rt.ports.idle()
    d = (await admin.get(f"/api/devices/{dev['id']}")).json()
    assert d["effective_class"] == "switch" and d["ports_effective"] is True
    assert (await rt.ports.pass_once()) == []  # ran just now
    # a flag change makes it due at once
    await admin.patch(f"/api/devices/{dev['id']}", json={"arp_collect": "on"})
    assert (await rt.ports.pass_once()) == [dev["id"]]
    await rt.ports.idle()
    body = (await admin.get(f"/api/devices/{dev['id']}/ports")).json()
    assert body["arp_effective"] is True and body["status"]["sources"]["arp"] == "empty"
    # interval 0 -> off
    await admin.put("/api/settings", json={"port_inventory_min": 0})
    await admin.patch(f"/api/devices/{dev['id']}", json={"arp_collect": "off"})
    assert (await rt.ports.pass_once()) == []
    await admin.put("/api/settings", json={"port_inventory_min": 10})
    # the device is disabled -> not effective
    await admin.patch(f"/api/devices/{dev['id']}", json={"enabled": False})
    d = (await admin.get(f"/api/devices/{dev['id']}")).json()
    assert d["ports_effective"] is False
    assert (await rt.ports.pass_once()) == []


async def test_walk_count_and_percent_base(admin: httpx.AsyncClient, agents) -> None:
    a = agents["custom"]
    dev = await make_device(admin, name="r1", host=CUSTOM_IP, snmp_credentials=_creds(a))

    async def run(type_: str, config: dict[str, Any], **kw: Any) -> dict[str, Any]:
        r = await admin.post(
            "/api/sensors", json={"device_id": dev["id"], "type": type_, "config": config, **kw}
        )
        assert r.status_code == 201, r.text
        s = r.json()
        return {"sensor": s, **(await admin.post(f"/api/sensors/{s['id']}/run")).json()}

    res = await run(
        "snmp_walk_count", {"oid": "1.3.6.1.4.1.9.99.1", "count_values": [1], "label": "Up"}
    )
    assert res["sensor"]["name"] == "Count 1.3.6.1.4.1.9.99.1"
    assert [c["unit"] for c in res["sensor"]["channels"]] == ["#", "#", "#"]
    vals = {c["key"]: c["value"] for c in res["channels"]}
    assert vals == {"rows": 3, "matched": 1, "unmatched": 2}  # "1" (a string) does not match
    assert res["status"] == "up" and res["message"] == "Up: 1 of 3"
    res = await run("snmp_walk_count", {"oid": "1.3.6.1.4.1.9.99.1"})
    assert {c["key"]: c["value"] for c in res["channels"]}["matched"] == 3
    res = await run("snmp_walk_count", {"oid": "1.3.6.1.4.1.9.98"})  # no rows -> ok, zeros
    assert res["status"] == "up" and {c["value"] for c in res["channels"]} == {0}
    # percent_base sum: used / (used + free)
    pool = "1.3.6.1.4.1.9.9.48.1.1.1"
    res = await run(
        "snmp_custom",
        {"oid": f"{pool}.5.1", "percent_of_oid": f"{pool}.6.1", "percent_base": "sum", "unit": "%"},
    )
    assert res["channels"][0]["value"] == 75.0
    res = await run(
        "snmp_custom",
        {"oid": f"{pool}.5.2", "percent_of_oid": f"{pool}.6.2", "percent_base": "sum"},
    )
    assert res["status"] == "up" and res["channels"][0]["value"] is None
    # PATCH percent_base resets the state; total keeps 2a behaviour (300/100 -> 300 %)
    sid = res["sensor"]["id"]
    r = await admin.patch(
        f"/api/sensors/{sid}",
        json={
            "config": {
                "oid": f"{pool}.5.1",
                "percent_of_oid": f"{pool}.6.1",
                "percent_base": "total",
            }
        },
    )
    assert r.status_code == 200
    out = (await admin.post(f"/api/sensors/{sid}/run")).json()
    assert out["channels"][0]["value"] == 300.0
    r = await admin.post(
        "/api/sensors",
        json={
            "device_id": dev["id"],
            "type": "snmp_custom",
            "config": {"oid": "1.3", "percent_base": "sum"},
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["loc"] == ["body", "config", "percent_base"]
    types = {t["type"]: t for t in (await admin.get("/api/sensor-types")).json()["items"]}
    wc = types["snmp_walk_count"]
    assert wc["primary_channel"] == "matched" and wc["requires_snmp"] is True
    assert [f["key"] for f in wc["config_fields"]] == ["oid", "count_values", "label", "max_rows"]
    pb = next(f for f in types["snmp_custom"]["config_fields"] if f["key"] == "percent_base")
    assert pb["type"] == "enum" and pb["options"] == ["total", "sum"] and pb["default"] == "total"


# ------------------------------------------------------------------ migration 0005
NEW_TABLES = {"switch_ports", "port_macs", "arp_entries", "dns_cache", "port_inventory_status"}


@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_0005_up_down_up(mode: str, rt) -> None:  # type: ignore[no-untyped-def]
    assert TEST_DATABASE_URL
    if mode == "timescale" and rt.settings.netmon_disable_timescale:
        pytest.skip("suite runs in forced fallback mode")
    name = f"netmon_test_m5_{secrets.token_hex(3)}"
    admin = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = make_engine(db_url_for(name).replace("postgresql://", "postgresql+asyncpg://"))
    url = db_url_for(name)
    try:
        await run_migrations(engine)
        enabled = await timescale.bootstrap(engine, 90, disabled=(mode == "fallback"))
        if mode == "timescale" and not enabled:
            pytest.skip("TimescaleDB not available")
        assert NEW_TABLES <= await _tables(url)
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0005"
            gid = await conn.fetchval("INSERT INTO groups (name) VALUES ('g') RETURNING id")
            did = await conn.fetchval(
                "INSERT INTO devices (group_id, name, host) VALUES ($1, 'd', 'h') RETURNING id", gid
            )
            assert (
                await conn.fetchval("SELECT port_inventory || '/' || arp_collect FROM devices")
                == "auto/auto"
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE devices SET port_inventory = 'yes'")
            sid = await conn.fetchval(
                "INSERT INTO sensors (device_id, name, type, config) VALUES "
                "($1, 'c', 'snmp_walk_count', '{}') RETURNING id",
                did,
            )
            await conn.execute(
                "INSERT INTO sensor_channels (sensor_id, key, unit) VALUES ($1, 'rows', '#')", sid
            )
            await conn.execute(
                "INSERT INTO switch_ports (device_id, if_index, kind, admin_status, oper_status, "
                "untagged_vlans, in_octets, counter_bits, collected_at) VALUES ($1, 1, "
                "'physical', 'up', 'up', '{1,10}', 18446744073709551615, 64, now())",
                did,
            )
            for bad in (
                "kind = 'x'",
                "duplex = 'x'",
                "mode = 'x'",
                "counter_bits = 16",
                "poe_class = 5",
                "poe_detection = 'x'",
            ):
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(f"UPDATE switch_ports SET {bad}")
            await conn.execute(
                "INSERT INTO port_macs (device_id, if_index, mac, vlan, first_seen, last_seen) "
                "VALUES ($1, 1, 'aa:bb:cc:dd:ee:ff', NULL, now(), now())",
                did,
            )
            with pytest.raises(asyncpg.UniqueViolationError):  # COALESCE(vlan, -1) key
                await conn.execute(
                    "INSERT INTO port_macs (device_id, if_index, mac, vlan, first_seen, "
                    "last_seen) VALUES ($1, 2, 'aa:bb:cc:dd:ee:ff', NULL, now(), now())",
                    did,
                )
            await conn.execute(
                "INSERT INTO arp_entries (source_device_id, ip, mac, first_seen, last_seen) "
                "VALUES ($1, '10.0.0.1', 'aa:bb:cc:dd:ee:ff', now(), now())",
                did,
            )
            await conn.execute("INSERT INTO dns_cache VALUES ('10.0.0.1', 'x', now())")
            await conn.execute(
                "INSERT INTO port_inventory_status (device_id, detected_class) "
                "VALUES ($1, 'switch')",
                did,
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE port_inventory_status SET detected_class = 'firewall'")
            idx = {
                r["indexname"]
                for r in await conn.fetch(
                    "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
                )
            }
            assert {
                "ux_port_macs_key",
                "ix_port_macs_mac",
                "ix_port_macs_device_if",
                "ix_arp_entries_mac",
                "ix_arp_entries_ip",
            } <= idx
            await conn.execute(
                "INSERT INTO measurements (time, sensor_id, channel, value) "
                "VALUES (now(), $1, 'rows', 1)",
                sid,
            )
        finally:
            await conn.close()
        async with engine.connect() as c:  # 0005 -> 0004
            await c.run_sync(lambda sc: _alembic(sc, "downgrade", "0004"))
            await c.commit()
        assert not (NEW_TABLES & await _tables(url))
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0004"
            cols = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'devices'"
                )
            }
            assert not cols & {"port_inventory", "arp_collect"}
            assert await conn.fetchval("SELECT count(*) FROM sensors") == 0
            assert await conn.fetchval("SELECT count(*) FROM sensor_channels") == 0
            assert await conn.fetchval("SELECT count(*) FROM measurements") == 1  # untouched
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO sensors (device_id, name, type, config) VALUES "
                    "($1, 'c', 'snmp_walk_count', '{}')",
                    did,
                )
            await conn.execute(
                "INSERT INTO sensors (device_id, name, type, config) "
                "VALUES ($1, 'p', 'ping', '{}')",
                did,
            )
        finally:
            await conn.close()
        await run_migrations(engine)  # and up again
        assert NEW_TABLES <= await _tables(url)
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0005"
            assert await conn.fetchval("SELECT port_inventory FROM devices") == "auto"
            await conn.execute(
                "INSERT INTO sensors (device_id, name, type, config) VALUES "
                "($1, 'c', 'snmp_walk_count', '{}')",
                did,
            )
        finally:
            await conn.close()
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
