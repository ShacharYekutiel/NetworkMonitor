"""[1.3] DB / API tests: sensor impact (API, rollup, backfill), important ports and connected
hosts in the ports API, ports CSV, trunk settings, link-down on trunks, LAG degraded,
migration 0008."""

from __future__ import annotations

import asyncio
import csv
import io
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy import text

from netmon.runtime import Runtime
from tests.conftest import _plain, make_device, make_sensor
from tests.conftest import requires_db as _requires_db
from tests.ingest_helpers import send_udp, src, v2c_trap, wait_for
from tests.test_2b3_ingest import features, ports

pytestmark = _requires_db

LINK_DOWN, LINK_UP = "1.3.6.1.6.3.1.1.5.3", "1.3.6.1.6.3.1.1.5.4"
IFINDEX, ADMIN_ST = "1.3.6.1.2.1.2.2.1.1", "1.3.6.1.2.1.2.2.1.7"

# if_index, name, kind, bridge_port, mode, untagged, tagged, mac_count, lag, oper
LAB = [
    (1, "Gi1/0/1", "physical", 1, "access", [10], [], 1, None, "up"),  # PC
    (2, "Gi1/0/2", "physical", 2, "trunk", [10], [20], 1, None, "up"),  # hybrid + voice
    (3, "Gi1/0/3", "physical", 3, "trunk", [1], [10, 20, 30], 0, None, "up"),  # trunk
    (4, "Gi1/0/4", "physical", None, None, [], [], 0, 11, "up"),  # LAG member
    (5, "Gi1/0/5", "physical", None, None, [], [], 0, 11, "up"),  # LAG member
    (6, "Gi1/0/6", "physical", 6, "access", [10], [], 9, None, "up"),  # uplink (MAC count)
    (7, "Gi1/0/7", "physical", 7, "access", [1], [], 0, None, "up"),  # uplink (neighbour)
    (8, "Gi1/0/8", "physical", 8, "access", [10], [], 0, None, "down"),
    (11, "BAGG1", "lag", 11, "trunk", [1], [10, 20], 0, None, "up"),
]


async def _lab(rt: Runtime, admin: httpx.AsyncClient, host: str = "10.13.0.1") -> tuple[int, int]:
    a = await make_device(admin, name="sw13", host=host)
    b = await make_device(admin, name="core13", host="10.13.0.2")
    async with rt.sessionmaker() as s:
        for idx, name, kind, bp, mode, unt, tag, macs, lag, oper in LAB:
            await s.execute(
                text(
                    "INSERT INTO switch_ports (device_id, if_index, name, descr, alias, kind, "
                    "admin_status, oper_status, bridge_port, pvid, mode, untagged_vlans, "
                    "tagged_vlans, mac_count, lag_if_index, lag_attached, collected_at) VALUES "
                    "(:d, :i, :n, :n, :al, :k, 'up', :o, :bp, :pv, :m, :u, :t, :mc, :l, :a, now())"
                ),
                {"d": a["id"], "i": idx, "n": name, "al": f"=port {idx}" if idx == 1 else "",
                 "k": kind, "o": oper, "bp": bp, "pv": unt[0] if unt else None, "m": mode,
                 "u": unt, "t": tag, "mc": macs, "l": lag, "a": True if lag else None},
            )  # fmt: skip
        for mac, idx in (("00:50:56:13:00:01", 1), ("00:50:56:13:00:02", 1)):
            await s.execute(
                text(
                    "INSERT INTO port_macs (device_id, if_index, mac, vlan, first_seen, "
                    "last_seen) VALUES (:d, :i, :m, 10, now(), now())"
                ),
                {"d": a["id"], "i": idx, "m": mac},
            )
        await s.execute(
            text(
                "INSERT INTO arp_entries (source_device_id, ip, mac, if_index, present, "
                "first_seen, last_seen) VALUES (:d, '10.13.10.5', '00:50:56:13:00:01', 1, true, "
                "now(), now())"
            ),
            {"d": a["id"]},
        )
        for did in (a["id"], b["id"]):
            await s.execute(
                text(
                    "INSERT INTO port_inventory_status (device_id, detected_class, last_run_at, "
                    "last_ok_at, ports) VALUES (:d, 'switch', now(), now(), 9)"
                ),
                {"d": did},
            )
        await s.commit()
    r = await admin.post(
        "/api/topology/links",
        json={"a_device_id": b["id"], "a_if_index": 49, "a_port": "49",
              "b_device_id": a["id"], "b_if_index": 7, "b_port": "Gi1/0/7"},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    await admin.patch(f"/api/devices/{b['id']}", json={"port_inventory": "on"})
    return a["id"], b["id"]


async def _status(c: httpx.AsyncClient, device_id: int) -> str:
    return str((await c.get(f"/api/devices/{device_id}")).json()["status"])


async def _set_status(rt: Runtime, sensor_id: int, status: str) -> None:
    async with rt.sessionmaker() as s:
        await s.execute(
            text("UPDATE sensors SET status = :st WHERE id = :i"), {"st": status, "i": sensor_id}
        )
        await s.commit()


# ------------------------------------------------------------------ impact API + rollup
async def test_sensor_impact_api_and_rollup(
    rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient
) -> None:
    a, _ = await _lab(rt, admin)
    ping = await make_sensor(admin, a, "ping")
    assert ping["impact"] == "critical"
    # new interface sensors: important port -> critical, other -> warning; explicit wins
    trunk = await make_sensor(admin, a, "snmp_interface", {"if_index": 3, "if_name": "Gi1/0/3"})
    pc = await make_sensor(admin, a, "snmp_interface", {"if_index": 1, "if_name": "Gi1/0/1"})
    explicit = await make_sensor(
        admin, a, "snmp_interface", {"if_index": 8, "if_name": "Gi1/0/8"}, impact="none"
    )
    assert (trunk["impact"], pc["impact"], explicit["impact"]) == ("critical", "warning", "none")
    # a device without port inventory: critical (1.2 behaviour)
    other = await make_device(admin, name="srv", host="10.13.0.9")
    eth = await make_sensor(admin, other["id"], "snmp_interface", {"if_index": 1})
    assert eth["impact"] == "critical"
    # rollup
    for sid, st in ((ping["id"], "up"), (trunk["id"], "up"), (explicit["id"], "up")):
        await _set_status(rt, sid, st)
    await _set_status(rt, pc["id"], "down")
    assert await _status(admin, a) == "warning"
    tree = (await viewer.get("/api/tree")).json()
    node = next(d for g in tree["groups"] for d in g["devices"] if d["id"] == a)
    assert node["status"] == "warning" and node["status_counts"]["down"] == 1
    assert {s["id"]: s["impact"] for s in node["sensors"]}[pc["id"]] == "warning"
    dash = (await viewer.get("/api/dashboard")).json()
    assert dash["device_counts"]["warning"] >= 1 and dash["sensor_counts"]["down"] == 1
    r = await admin.patch(f"/api/sensors/{pc['id']}", json={"impact": "none"})
    assert r.status_code == 200 and r.json()["impact"] == "none"
    assert await _status(admin, a) == "up"
    groups = (await viewer.get("/api/groups")).json()["items"]
    assert all(g["status"] != "warning" or g["status_counts"]["warning"] for g in groups)
    await admin.patch(f"/api/sensors/{pc['id']}", json={"impact": "critical"})
    assert await _status(admin, a) == "down"
    assert (await admin.patch(f"/api/sensors/{pc['id']}", json={"impact": "x"})).status_code == 422
    assert (
        await viewer.patch(f"/api/sensors/{pc['id']}", json={"impact": "none"})
    ).status_code == 403


async def test_bulk_impact(
    rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient
) -> None:
    await features(admin, audit=True)
    d = await make_device(admin, name="bulk", host="10.13.1.1")
    ids = [(await make_sensor(admin, d["id"], "ping"))["id"] for _ in range(3)]
    await _set_status(rt, ids[0], "down")
    assert await _status(admin, d["id"]) == "down"
    r = await viewer.post("/api/sensors/bulk-impact", json={"sensor_ids": ids, "impact": "none"})
    assert r.status_code == 403
    r = await admin.post(
        "/api/sensors/bulk-impact", json={"sensor_ids": [*ids, 99999], "impact": "none"}
    )
    assert r.status_code == 422
    r = await admin.post("/api/sensors/bulk-impact", json={"sensor_ids": ids, "impact": "warning"})
    assert r.status_code == 200 and r.json() == {"updated": 3}
    assert await _status(admin, d["id"]) == "warning"
    r = await admin.post(
        "/api/sensors/bulk-impact", json={"sensor_ids": ids[:1], "impact": "warning"}
    )
    assert r.json() == {"updated": 0}
    for body in ({"sensor_ids": [], "impact": "none"}, {"sensor_ids": ids, "impact": "low"}):
        assert (await admin.post("/api/sensors/bulk-impact", json=body)).status_code == 422
    audit = (await admin.get("/api/audit", params={"action": "sensor.update"})).json()["items"]
    changes = [a["changes"] for a in audit if "impact" in (a.get("changes") or {})]
    assert len(changes) == 3 and changes[0]["impact"] == ["critical", "warning"]


async def test_impact_backfill(rt: Runtime, admin: httpx.AsyncClient) -> None:
    from netmon.services import impact
    from netmon.services import settings as settings_svc

    a, _ = await _lab(rt, admin)
    srv = await make_device(admin, name="srv", host="10.13.0.9")
    sids = {}
    for idx in (1, 3, 4, 6, 8):
        s = await make_sensor(admin, a, "snmp_interface", {"if_index": idx}, impact="critical")
        sids[idx] = s["id"]
    lone = await make_sensor(admin, srv["id"], "snmp_interface", {"if_index": 1})
    async with rt.sessionmaker() as s:
        await settings_svc.put(s, impact.BACKFILL_KEY, "")
        await s.commit()
    assert await impact.backfill(rt) == 2
    got = {
        idx: (await admin.get(f"/api/sensors/{sid}")).json()["impact"] for idx, sid in sids.items()
    }
    assert got == {1: "warning", 3: "critical", 4: "critical", 6: "critical", 8: "warning"}
    assert (await admin.get(f"/api/sensors/{lone['id']}")).json()["impact"] == "critical"
    # once only
    await admin.patch(f"/api/sensors/{sids[1]}", json={"impact": "critical"})
    assert await impact.backfill(rt) == 0
    assert (await admin.get(f"/api/sensors/{sids[1]}")).json()["impact"] == "critical"


# ------------------------------------------------------------------ ports API
async def test_ports_important_connected_and_csv(
    rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient
) -> None:
    a, b = await _lab(rt, admin)
    body = (await viewer.get(f"/api/devices/{a}/ports")).json()
    by = {p["if_index"]: p for p in body["ports"]}
    assert {i: p["important_reasons"] for i, p in by.items()} == {
        1: [],
        2: [],
        3: ["trunk"],
        4: ["lag", "trunk"],
        5: ["lag", "trunk"],
        6: ["uplink"],
        7: ["uplink"],
        8: [],
        11: ["lag", "trunk"],
    }
    assert by[3]["important"] and not by[1]["important"]
    c = by[1]["connected"]
    assert c["count"] == 2
    assert c["first"] == {
        "mac": "00:50:56:13:00:01",
        "ip": "10.13.10.5",
        "hostname": None,
        "vendor": "VMware, Inc.",
        "device_id": None,
        "device_name": None,
    }
    assert by[8]["connected"] == {"count": 0, "first": None}
    assert by[6]["connected"] == {"count": 9, "first": None}  # uplink: count only
    assert by[7]["neighbor_name"] == "core13" and by[7]["neighbor_mgmt_address"] == "10.13.0.2"
    assert by[1]["neighbor_name"] is None and by[1]["neighbor_mgmt_address"] is None
    det = (await viewer.get(f"/api/devices/{a}/ports/7")).json()
    assert det["important_reasons"] == ["uplink"] and det["neighbor_name"] == "core13"
    # trunk_min_tagged_vlans = 1: the hybrid port is a trunk too
    r = await admin.put("/api/settings", json={"trunk_min_tagged_vlans": 1})
    assert r.status_code == 200 and r.json()["trunk_min_tagged_vlans"] == 1
    by = {p["if_index"]: p for p in (await viewer.get(f"/api/devices/{a}/ports")).json()["ports"]}
    assert by[2]["important_reasons"] == ["trunk"]
    # CSV
    r = await viewer.get(f"/api/devices/{a}/ports.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="ports-sw13.csv"' in r.headers["content-disposition"]
    raw = r.content.decode("utf-8")
    assert raw.startswith("﻿")
    rows = list(csv.reader(io.StringIO(raw[1:])))
    assert rows[0] == [
        "port", "alias", "status", "vlan", "mode", "mac", "ip", "hostname", "vendor", "neighbor"
    ]  # fmt: skip
    pc = [x for x in rows[1:] if x[0] == "Gi1/0/1"]
    assert len(pc) == 2 and pc[0][1] == "'=port 1"  # formula guard
    assert pc[0][2:9] == [
        "up",
        "10",
        "access",
        "00:50:56:13:00:01",
        "10.13.10.5",
        "",
        "VMware, Inc.",
    ]
    up7 = [x for x in rows[1:] if x[0] == "Gi1/0/7"]
    assert len(up7) == 1 and up7[0][5] == "" and up7[0][9] == "core13 (10.13.0.2)"
    assert len(rows) == 1 + 2 + 8  # 2 MACs on port 1, one row for each other port
    assert (await viewer.get("/api/devices/99999/ports.csv")).status_code == 404


async def test_trunk_settings(rt: Runtime, admin: httpx.AsyncClient) -> None:
    s = (await admin.get("/api/settings")).json()
    assert s["trunk_min_tagged_vlans"] == 2 and s["event_linkdown_include_trunks"] is True
    for bad in ({"trunk_min_tagged_vlans": 0}, {"trunk_min_tagged_vlans": 101}):
        assert (await admin.put("/api/settings", json=bad)).status_code == 422
    r = await admin.put(
        "/api/settings",
        json={"trunk_min_tagged_vlans": 3, "event_linkdown_include_trunks": False},
    )
    assert r.status_code == 200
    assert rt.ingest.include_trunks is False


async def test_port_info_trunks_and_critical_sensors(rt: Runtime, admin: httpx.AsyncClient) -> None:
    from netmon.ingest.pipeline import load_port_info

    a, _ = await _lab(rt, admin)
    info = await load_port_info(rt, a, include_trunks=True)
    assert info.important == {3, 4, 5, 6, 7, 11}
    info = await load_port_info(rt, a, include_trunks=False)
    assert info.important == {4, 5, 6, 7, 11}
    await make_sensor(admin, a, "snmp_interface", {"if_index": 8}, impact="critical")
    await make_sensor(admin, a, "snmp_interface", {"if_index": 1})  # warning: not important
    info = await load_port_info(rt, a, include_trunks=False)
    assert info.important == {4, 5, 6, 7, 8, 11}
    srv = await make_device(admin, name="srv", host="10.13.0.9")
    assert (await load_port_info(rt, srv["id"])).important is None


def link(oid: str, idx: int) -> bytes:
    return v2c_trap(oid, [(f"{IFINDEX}.{idx}", "i", idx), (f"{ADMIN_ST}.{idx}", "i", 1)])


async def _trap_sensor(c: httpx.AsyncClient, device_id: int) -> dict[str, Any] | None:
    items = (await c.get("/api/sensors", params={"device_id": device_id, "type": "trap"})).json()[
        "items"
    ]
    return items[0] if items else None


async def _conds(c: httpx.AsyncClient, device_id: int) -> list[dict[str, Any]]:
    ts = await _trap_sensor(c, device_id)
    if ts is None:
        return []
    return list((await c.get(f"/api/sensors/{ts['id']}/conditions")).json()["items"])


async def _enable_linkdown(admin: httpx.AsyncClient) -> int:
    await features(admin, traps=True, event_alerts=True)
    items = (await admin.get("/api/event-rules")).json()["items"]
    rid = next(r["id"] for r in items if r["builtin"] == "trap_link_down")
    await admin.patch(f"/api/event-rules/{rid}", json={"enabled": True})
    return int(rid)


async def test_linkdown_on_trunk_ports(rt: Runtime, admin: httpx.AsyncClient) -> None:
    tp, _ = ports(rt)
    a, _ = await _lab(rt, admin, host=src("127.0.0.83"))
    await _enable_linkdown(admin)
    send_udp(tp, link(LINK_DOWN, 1), "127.0.0.83")  # PC port: nothing
    send_udp(tp, link(LINK_DOWN, 3), "127.0.0.83")  # trunk: link-down condition
    await wait_for(lambda: _has_keys(admin, a, {("rule", "3")}))
    send_udp(tp, link(LINK_UP, 3), "127.0.0.83")
    await wait_for(lambda: _has_keys(admin, a, set()))
    # include_trunks off: a trunk is not important any more
    await admin.put("/api/settings", json={"event_linkdown_include_trunks": False})
    send_udp(tp, link(LINK_DOWN, 3), "127.0.0.83")
    await wait_for(lambda: _trap_count(admin, 4))
    await rt.drain()
    await asyncio.sleep(0.3)
    assert await _conds(admin, a) == []


async def _trap_count(c: httpx.AsyncClient, n: int) -> bool:
    return len((await c.get("/api/traps")).json()["items"]) >= n


async def _has_keys(c: httpx.AsyncClient, device_id: int, keys: set[tuple[str, str]]) -> bool:
    got = {(x["source"], x["key"]) for x in await _conds(c, device_id)}
    return got == keys


# ------------------------------------------------------------------ LAG degraded
async def test_lag_degraded_traps_and_reconcile(rt: Runtime, admin: httpx.AsyncClient) -> None:
    tp, _ = ports(rt)
    a, _ = await _lab(rt, admin, host=src("127.0.0.84"))
    rid = await _enable_linkdown(admin)
    send_udp(tp, link(LINK_DOWN, 4), "127.0.0.84")  # member down, LAG still up
    await wait_for(lambda: _has_keys(admin, a, {("rule", "4"), ("lag_degraded", "11")}))
    c = next(x for x in await _conds(admin, a) if x["source"] == "lag_degraded")
    assert c["message"] == "LAG BAGG1 degraded (1 of 2 members up)" and c["status"] == "warning"
    assert c["expires_at"] is not None  # like the link-down rule (24 h)
    ts = await _trap_sensor(admin, a)
    assert ts is not None and ts["status"] == "warning"
    # second member down too: fully down = the existing link-down alerts only
    send_udp(tp, link(LINK_DOWN, 5), "127.0.0.84")
    await wait_for(lambda: _has_keys(admin, a, {("rule", "4"), ("rule", "5")}))
    send_udp(tp, link(LINK_UP, 5), "127.0.0.84")
    await wait_for(lambda: _has_keys(admin, a, {("rule", "4"), ("lag_degraded", "11")}))
    send_udp(tp, link(LINK_UP, 4), "127.0.0.84")
    await wait_for(lambda: _has_keys(admin, a, set()))
    # reconcile from the stored ifOperStatus (port inventory run)
    async with rt.sessionmaker() as s:
        await s.execute(
            text(
                "UPDATE switch_ports SET oper_status = 'down' WHERE device_id = :d AND if_index = 5"
            ),
            {"d": a},
        )
        await s.commit()
    await rt.ingest.lags.reconcile(a)
    assert await _has_keys(admin, a, {("lag_degraded", "11")})
    # disabling the built-in rule removes the LAG condition as well
    await admin.patch(f"/api/event-rules/{rid}", json={"enabled": False})
    await wait_for(lambda: _has_keys(admin, a, set()))
    await rt.ingest.lags.reconcile(a)  # gate closed: nothing raised
    assert await _conds(admin, a) == []
    # event_alerts OFF clears it too
    await admin.patch(f"/api/event-rules/{rid}", json={"enabled": True})
    await rt.ingest.lags.reconcile(a)
    assert await _has_keys(admin, a, {("lag_degraded", "11")})
    await features(admin, event_alerts=False)
    await wait_for(lambda: _has_keys(admin, a, set()))


async def test_lag_degraded_status_change_and_ack(rt: Runtime, admin: httpx.AsyncClient) -> None:
    a, _ = await _lab(rt, admin)
    await _enable_linkdown(admin)
    async with rt.sessionmaker() as s:
        await s.execute(
            text(
                "UPDATE switch_ports SET oper_status = 'down' WHERE device_id = :d AND if_index = 4"
            ),
            {"d": a},
        )
        await s.commit()
    await rt.ingest.lags.reconcile(a)
    ts = await _trap_sensor(admin, a)
    assert ts is not None and ts["status"] == "warning"
    # an ordinary problem sensor: acknowledge works and is cleared on recovery
    r = await admin.post(f"/api/sensors/{ts['id']}/acknowledge", json={"comment": "known"})
    assert r.status_code == 200, r.text
    ev = (
        await admin.get("/api/events", params={"sensor_id": ts["id"], "kind": "status_change"})
    ).json()["items"]
    assert ev and ev[0]["new_status"] == "warning" and ev[0]["in_maintenance"] is False


# ------------------------------------------------------------------ migration 0008
@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_0008_up_down_up(mode: str, rt: Runtime) -> None:
    import secrets

    from alembic import command

    from netmon.db import timescale
    from netmon.db.engine import make_engine
    from netmon.db.migrate import _alembic_config, run_migrations
    from tests.conftest import TEST_DATABASE_URL, db_url_for

    assert TEST_DATABASE_URL
    name = f"netmon_test_m8_{secrets.token_hex(3)}"
    conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await conn.execute(f'CREATE DATABASE "{name}"')
    await conn.close()
    url = db_url_for(name)
    engine = make_engine(url.replace("postgresql://", "postgresql+asyncpg://"))
    try:
        await run_migrations(engine)
        await timescale.bootstrap_status(engine, 90, disabled=mode == "fallback")
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == "0009"
            gid = await c.fetchval("INSERT INTO groups (name) VALUES ('g') RETURNING id")
            did = await c.fetchval(
                "INSERT INTO devices (group_id, name, host) VALUES ($1, 'd', '10.0.0.1') "
                "RETURNING id",
                gid,
            )
            sid = await c.fetchval(
                "INSERT INTO sensors (device_id, name, type, config) "
                "VALUES ($1, 's', 'ping', '{}') RETURNING id",
                did,
            )
            assert await c.fetchval("SELECT impact FROM sensors WHERE id = $1", sid) == "critical"
            with pytest.raises(asyncpg.CheckViolationError):
                await c.execute("UPDATE sensors SET impact = 'high' WHERE id = $1", sid)
            await c.execute(
                "INSERT INTO event_conditions (sensor_id, source, key, status, first_at, "
                "last_at, message) VALUES ($1, 'lag_degraded', '11', 'warning', now(), now(), "
                "'LAG')",
                sid,
            )
        finally:
            await c.close()

        def down(sync_conn: Any) -> None:
            cfg = _alembic_config()
            cfg.attributes["connection"] = sync_conn
            command.downgrade(cfg, "0007")

        async with engine.connect() as ac:
            await ac.run_sync(down)
            await ac.commit()
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == "0007"
            cols = await c.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'sensors'"
            )
            assert "impact" not in {r["column_name"] for r in cols}
            assert await c.fetchval("SELECT count(*) FROM event_conditions") == 0
        finally:
            await c.close()
        await run_migrations(engine)
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == "0009"
            assert await c.fetchval("SELECT impact FROM sensors LIMIT 1") == "critical"
        finally:
            await c.close()
    finally:
        await engine.dispose()
        conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.close()
