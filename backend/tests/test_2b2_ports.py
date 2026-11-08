"""[2b-2] Switch ports view against snmpsim: ArubaOS-Switch (Q-BRIDGE, bridge port = ifIndex),
Comware (dot1dBasePortIfIndex, fdbId != VLAN), Aruba CX (dot1qVlanCurrentTable and dot1d FDB
fallbacks), an ARP router and a legacy ARP host; MAC/IP search, presence, retention, reverse DNS
cache, refresh errors, ports.updated.

Agents on 127.0.7.61-65 (QA uses 127.0.0.x)."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import asyncpg
import httpx
import pytest

from tests import ports_sims as sims
from tests.conftest import _plain, make_device, make_sensor, requires_db
from tests.snmpsim_agents import start_agent

pytestmark = requires_db

IPS = {
    "aruba": "127.0.7.61",
    "comware": "127.0.7.62",
    "cx": "127.0.7.63",
    "router": "127.0.7.64",
    "legacy": "127.0.7.65",
}


def _free_udp_port(ip: str) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((ip, 0))
    port = s.getsockname()[1]
    s.close()
    return port


RECORDS = {
    "aruba": sims.arubaos_records,
    "comware": sims.comware_records,
    "cx": sims.arubacx_records,
    "router": sims.arp_router_records,
    "legacy": sims.arp_legacy_records,
}


@pytest.fixture(scope="module")
def agents() -> Iterator[dict[str, Any]]:
    started: dict[str, Any] = {}
    for key, ip in IPS.items():
        port = _free_udp_port(ip)
        a = start_agent(ip, port, {"public": RECORDS[key]()})
        if a is None:
            for x in started.values():
                x.stop()
            pytest.skip("snmpsim not available")
        started[key] = a
    yield started
    for a in started.values():
        a.stop()


def restart(agents: dict[str, Any], key: str, records: list[Any]) -> None:
    """Replace an agent's recording (same ip/port)."""
    old = agents[key]
    old.stop()
    a = start_agent(old.ip, old.port, {"public": records})
    assert a is not None
    agents[key] = a


async def _device(admin: httpx.AsyncClient, agents: dict[str, Any], key: str, **kw: Any) -> dict:
    a = agents[key]
    return await make_device(
        admin,
        name=key,
        host=a.ip,
        snmp_credentials={"port": a.port, "timeout_s": 1, "retries": 0},
        auto_sensors=["ping", "snmp_system"],
        **kw,
    )


async def _refresh(rt, admin: httpx.AsyncClient, did: int) -> None:  # type: ignore[no-untyped-def]
    r = await admin.post(f"/api/devices/{did}/ports/refresh")
    assert r.status_code == 202, r.text
    assert r.json() == {"device_id": did, "queued": True}
    await rt.ports.idle()


def _ports(body: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {p["if_index"]: p for p in body["ports"]}


@pytest.fixture
def ptr_file(rt, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    path = tmp_path / "ptr.json"
    path.write_text(json.dumps({"10.0.0.11": "PC-Reception.lan.", "10.0.0.12": "phone-12.lan"}))
    monkeypatch.setenv("NETMON_TEST_PTR_FILE", str(path))
    monkeypatch.setattr(rt.settings, "netmon_test_mode", True)
    return path


async def _lab(rt, admin: httpx.AsyncClient, agents: dict[str, Any]) -> dict[str, dict]:  # type: ignore[no-untyped-def]
    devs = {k: await _device(admin, agents, k) for k in IPS}
    # manual link .62 ifIndex 49 <-> .61 ifIndex 10, a sensor on .61 port 1
    r = await admin.post(
        "/api/topology/links",
        json={
            "a_device_id": devs["comware"]["id"],
            "a_if_index": 49,
            "a_port": "Ten-GigabitEthernet1/0/49",
            "b_device_id": devs["aruba"]["id"],
            "b_if_index": 10,
            "b_port": "10",
        },
    )
    assert r.status_code == 201, r.text
    await make_sensor(admin, devs["aruba"]["id"], "snmp_interface", {"if_index": 1, "if_name": "1"})
    return devs


async def test_ports_lab(rt, admin: httpx.AsyncClient, agents, ptr_file) -> None:  # type: ignore[no-untyped-def]
    devs = await _lab(rt, admin, agents)
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    rt.hub.listeners.append(q)
    try:
        # .65 is a server: ARP auto -> not effective -> 409 ports_disabled
        r = await admin.post(f"/api/devices/{devs['legacy']['id']}/ports/refresh")
        assert r.status_code == 409 and r.json()["error"]["code"] == "ports_disabled"
        assert r.json()["error"]["details"] == [
            {"port_inventory": "auto", "arp_collect": "auto", "effective_class": "server"}
        ]
        r = await admin.patch(f"/api/devices/{devs['legacy']['id']}", json={"arp_collect": "on"})
        assert r.status_code == 200 and r.json()["arp_effective"] is True
        for key in ("router", "legacy", "aruba", "comware", "cx"):
            await _refresh(rt, admin, devs[key]["id"])
    finally:
        rt.hub.listeners.remove(q)
    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    updated = [m["data"] for m in msgs if m["type"] == "ports.updated"]
    assert [u["device_id"] for u in updated] == [
        devs[k]["id"] for k in ("router", "legacy", "aruba", "comware", "cx")
    ]
    assert all(u["ok"] for u in updated) and all(u["collected_at"] for u in updated)
    # arp_source: true for the ARP router and the legacy ARP host, false for the switches
    assert [u["arp_source"] for u in updated] == [True, True, False, False, False]

    # device flags in GET /api/devices/{id}
    d = (await admin.get(f"/api/devices/{devs['aruba']['id']}")).json()
    assert (d["port_inventory"], d["arp_collect"], d["effective_class"]) == (
        "auto",
        "auto",
        "switch",
    )
    assert d["ports_effective"] is True and d["arp_effective"] is False
    d = (await admin.get(f"/api/devices/{devs['router']['id']}")).json()
    assert d["effective_class"] == "router" and d["arp_effective"] is True

    # ---------------------------------------------------------------- .61 ArubaOS
    body = (await admin.get(f"/api/devices/{devs['aruba']['id']}/ports")).json()
    st = body["status"]
    assert st["sources"] == {
        "if_xtable": "ok",
        "duplex": "ok",
        "bridge": "empty",
        "vlans": "ok",
        "fdb": "ok",
        "poe": "ok",
        "arp": "skipped",
        "lag": "skipped",  # [2b-3] feature `lag` OFF
    }
    assert (st["fdb_mib"], st["vlan_mib"]) == ("q_bridge", "static")
    assert st["error"] is None and st["running"] is False and st["arp_sources"] == 2
    assert st["ports"] == 12 and st["macs"] == 14 and st["next_run_at"] is not None
    assert body["settings"] == {"interval_min": 10, "uplink_mac_threshold": 5}
    p = _ports(body)
    assert sorted(p) == list(range(1, 13))
    assert (p[11]["kind"], p[12]["kind"]) == ("lag", "vlan")
    p1 = p[1]
    assert (p1["state"], p1["speed_bps"], p1["duplex"], p1["alias"]) == (
        "up",
        1000000000,
        "full",
        "Reception PC",
    )
    assert (p1["mode"], p1["pvid"], p1["untagged_vlans"], p1["tagged_vlans"]) == (
        "access",
        1,
        [1],
        [],
    )
    assert p1["poe"] == {
        "admin_enabled": True,
        "detection": "delivering",
        "power_class": 3,
        "power_w": None,
    }
    assert p1["mac_count"] == 1 and p1["connected_kind"] == "client" and not p1["alert"]
    m = p1["first_mac"]
    assert m["mac"] == "00:50:56:01:00:01" and m["ip"] == "10.0.0.11"
    assert m["hostname"] == "pc-reception.lan" and m["vendor"] == "VMware, Inc."
    assert m["locally_administered"] is False and m["vlan"] == 1 and p1["macs"] == [m]
    assert p1["sensor_id"] is not None
    assert p1["in_bps"] is None  # first run
    assert p1["last_change_at"] is not None
    p2 = p[2]
    assert p2["mac_count"] == 2 and p2["poe"]["detection"] == "searching"
    assert p2["poe"]["power_class"] is None
    assert p2["first_mac"]["mac"] == "00:00:0c:00:00:03"  # it has an IP
    assert p2["first_mac"]["hostname"] == "phone-12.lan"
    assert p2["first_mac"]["vendor"].startswith("Cisco")
    assert [x["mac"] for x in p2["macs"]] == ["00:00:0c:00:00:03", "3c:52:82:00:00:02"]
    assert (p[3]["speed_bps"], p[3]["duplex"], p[3]["mac_count"]) == (100000000, "half", 0)
    m4 = p[4]["first_mac"]
    assert m4["mac"] == "da:a1:19:00:00:04" and m4["locally_administered"] is True
    assert m4["vendor"] is None and m4["ip"] == "10.0.0.14"  # from the legacy ARP host
    assert p[4]["poe"]["power_class"] == 4
    assert (p[5]["state"], p[5]["connected_kind"]) == ("admin_down", "none")
    assert p[5]["poe"]["admin_enabled"] is False and p[5]["poe"]["detection"] == "disabled"
    assert (p[6]["state"], p[6]["speed_bps"], p[6]["duplex"]) == ("down", None, "unknown")
    assert (p[7]["mode"], p[7]["pvid"], p[7]["untagged_vlans"]) == ("access", 10, [10])
    assert p[7]["first_mac"]["ip"] == "10.0.0.17" and p[7]["first_mac"]["hostname"] is None
    assert p[7]["poe"]["power_class"] == 0
    assert (p[8]["state"], p[8]["alert"], p[8]["alert_reasons"]) == ("down", True, ["poe_fault"])
    p9 = p[9]
    assert (p9["mode"], p9["pvid"], p9["untagged_vlans"], p9["tagged_vlans"]) == (
        "trunk",
        1,
        [1],
        [10, 20],
    )
    assert (p9["is_uplink"], p9["uplink_reason"], p9["mac_count"]) == (True, "mac_count", 7)
    assert p9["macs"] == [] and p9["first_mac"] is None and p9["connected_kind"] == "client"
    assert p9["speed_bps"] == 10_000_000_000 and p9["poe"] is None
    p10 = p[10]
    assert (p10["is_uplink"], p10["uplink_reason"], p10["connected_kind"], p10["trunk"]) == (
        True,
        "neighbor",
        "switch",
        True,
    )
    assert (p10["pvid"], p10["untagged_vlans"], p10["tagged_vlans"]) == (1, [], [10, 20])
    assert p10["neighbor"]["device_id"] == devs["comware"]["id"]
    assert p10["neighbor"]["kind"] == "device" and p10["neighbor"]["source"] == "manual"
    assert p10["neighbor"]["device_class"] == "switch"
    assert p10["neighbor"]["port"] == "Ten-GigabitEthernet1/0/49"
    # kind filter
    r = await admin.get(f"/api/devices/{devs['aruba']['id']}/ports", params={"kind": ["lag"]})
    assert [x["if_index"] for x in r.json()["ports"]] == [11]
    assert (
        await admin.get(f"/api/devices/{devs['aruba']['id']}/ports", params={"kind": "x"})
    ).status_code == 422

    # port detail: uplink MACs hidden unless asked
    r = await admin.get(f"/api/devices/{devs['aruba']['id']}/ports/9")
    det = r.json()
    assert det["macs_hidden"] is True and det["macs"] == [] and det["neighbors"] == []
    r = await admin.get(
        f"/api/devices/{devs['aruba']['id']}/ports/9", params={"include_uplink_macs": "true"}
    )
    det = r.json()
    assert det["macs_hidden"] is False and len(det["macs"]) == 7
    assert [x["vlan"] for x in det["macs"]] == [1, 1, 1, 10, 10, 20, 20]
    det1 = (await admin.get(f"/api/devices/{devs['aruba']['id']}/ports/1")).json()
    assert det1["sensor"]["id"] == p1["sensor_id"] and det1["sensor"]["type"] == "snmp_interface"
    det10 = (await admin.get(f"/api/devices/{devs['aruba']['id']}/ports/10")).json()
    assert len(det10["neighbors"]) == 1
    assert (await admin.get(f"/api/devices/{devs['aruba']['id']}/ports/99")).status_code == 404

    # ---------------------------------------------------------------- .62 Comware
    body = (await admin.get(f"/api/devices/{devs['comware']['id']}/ports")).json()
    assert body["status"]["sources"]["bridge"] == "ok"
    assert body["status"]["fdb_mib"] == "q_bridge" and body["status"]["vlan_mib"] == "static"
    p = _ports(body)
    kinds = {i: x["kind"] for i, x in p.items()}
    assert kinds == {
        1: "physical",
        2: "physical",
        3: "physical",
        4: "physical",
        49: "physical",
        50: "physical",
        101: "physical",
        102: "physical",
        200: "vlan",
        300: "other",
        301: "other",
        400: "mgmt",
    }
    assert (p[4]["mode"], p[4]["pvid"]) == ("access", 10)
    assert p[4]["macs"][0]["mac"] == "00:0c:29:00:01:04" and p[4]["macs"][0]["vlan"] == 10
    assert p[49]["mode"] == "trunk" and p[49]["tagged_vlans"] == [10, 20]
    assert p[49]["untagged_vlans"] == [1]
    assert (p[49]["is_uplink"], p[49]["uplink_reason"], p[49]["connected_kind"]) == (
        True,
        "neighbor",
        "switch",
    )
    assert p[49]["neighbor"]["device_id"] == devs["aruba"]["id"]
    assert p[50]["mode"] == "trunk" and p[50]["tagged_vlans"] == [1, 10, 20]
    assert p[1]["poe"]["detection"] == "delivering" and p[1]["poe"]["power_class"] == 2
    assert p[101]["macs"][0]["mac"] == "00:0c:29:00:01:07"  # bridge port 7 -> ifIndex 101
    assert p[3]["macs"][0]["mac"] == "02:00:00:00:09:01"
    assert p[101]["poe"] is None

    # ---------------------------------------------------------------- .63 Aruba CX
    body = (await admin.get(f"/api/devices/{devs['cx']['id']}/ports")).json()
    st = body["status"]
    assert (st["fdb_mib"], st["vlan_mib"]) == ("bridge", "current")
    p = _ports(body)
    assert p[1]["macs"][0]["mac"] == "00:00:0c:00:cc:01" and p[1]["macs"][0]["vlan"] is None
    assert all((p[i]["mode"], p[i]["pvid"]) == ("access", 1) for i in range(1, 7))
    assert p[2000]["kind"] == "mgmt" and p[1000]["kind"] == "vlan"
    assert p[49]["speed_bps"] == 10_000_000_000 and p[49]["mode"] is None
    assert all(p[i]["poe"]["detection"] == "searching" for i in range(1, 7))

    # ---------------------------------------------------------------- ARP-only devices
    body = (await admin.get(f"/api/devices/{devs['router']['id']}/ports")).json()
    assert body["ports_effective"] is False and body["arp_effective"] is True
    assert (
        body["status"]["sources"]["arp"] == "ok" and body["status"]["sources"]["fdb"] == "skipped"
    )
    assert body["status"]["arp_entries"] == 4 and body["ports"] == []
    body = (await admin.get(f"/api/devices/{devs['legacy']['id']}/ports")).json()
    assert body["status"]["arp_entries"] == 1

    # ---------------------------------------------------------------- search
    r = await admin.get("/api/search/mac", params={"q": "00:50:56:01:00:07"})
    res = r.json()
    assert res["query"] == {"mac": "00:50:56:01:00:07", "mac_prefix": None, "ip": None}
    assert len(res["items"]) == 1
    it = res["items"][0]
    assert (it["device_id"], it["if_index"], it["vlan"], it["ip"], it["best"]) == (
        devs["aruba"]["id"],
        7,
        10,
        "10.0.0.17",
        True,
    )
    assert it["port"] == "7" and it["device_host"] == agents["aruba"].ip
    res = (await admin.get("/api/search/mac", params={"q": "02-00-00-00-09-01"})).json()
    assert [(i["device_id"], i["if_index"], i["best"], i["is_uplink"]) for i in res["items"]] == [
        (devs["comware"]["id"], 3, True, False),
        (devs["aruba"]["id"], 9, False, True),
    ]
    assert all(i["ip"] == "10.0.0.99" for i in res["items"])
    assert [(a["ip"], a["source_device_id"]) for a in res["arp"]] == [
        ("10.0.0.99", devs["router"]["id"])
    ]
    res = (await admin.get("/api/search/mac", params={"ip": "10.0.0.12"})).json()
    assert res["query"]["ip"] == "10.0.0.12"
    assert [(i["device_id"], i["if_index"]) for i in res["items"]] == [(devs["aruba"]["id"], 2)]
    assert [(a["ip"], a["source_device_id"], a["present"]) for a in res["arp"]] == [
        ("10.0.0.12", devs["router"]["id"], True)
    ]
    assert res["arp"][0]["hostname"] == "phone-12.lan"
    res = (await admin.get("/api/search/mac", params={"q": "005056"})).json()
    assert res["query"]["mac_prefix"] == "005056"
    assert sorted({i["mac"] for i in res["items"]}) == ["00:50:56:01:00:01", "00:50:56:01:00:07"]
    res = (await admin.get("/api/search/mac", params={"q": "0050.5601.0007"})).json()
    assert res["query"]["mac"] == "00:50:56:01:00:07"
    res = (await admin.get("/api/search/mac", params={"ip": "10.0.0.50"})).json()
    assert res["items"] == [] and res["arp"] == []  # invalid(2) entry dropped
    res = (await admin.get("/api/search/mac", params={"ip": "10.0.0.1"})).json()
    assert res["items"] == []  # local(5) entry dropped
    for params in ({"q": "zz"}, {"q": "00:50", "ip": "10.0.0.1"}, {}, {"ip": "10.0.0"}):
        r = await admin.get("/api/search/mac", params=params)
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error", params

    # ---------------------------------------------------------------- second run: rates
    await _refresh(rt, admin, devs["aruba"]["id"])
    p = _ports((await admin.get(f"/api/devices/{devs['aruba']['id']}/ports")).json())
    assert p[1]["in_bps"] == 0.0 and p[1]["in_errors_delta"] == 0
    assert p[1]["first_mac"]["first_seen"] == m["first_seen"]

    # ---------------------------------------------------------------- MAC disappears
    before = (await admin.get("/api/search/mac", params={"q": "00:50:56:01:00:07"})).json()
    await asyncio.to_thread(restart, agents, "aruba", sims.arubaos_records(drop_port7_mac=True))
    try:
        await _refresh(rt, admin, devs["aruba"]["id"])
        p = _ports((await admin.get(f"/api/devices/{devs['aruba']['id']}/ports")).json())
        assert p[7]["mac_count"] == 0 and p[7]["macs"] == [] and p[7]["first_mac"] is None
        after = (await admin.get("/api/search/mac", params={"q": "00:50:56:01:00:07"})).json()
        assert len(after["items"]) == 1
        it = after["items"][0]
        assert it["present"] is False and it["best"] is False
        assert it["last_seen"] == before["items"][0]["last_seen"]
    finally:
        await asyncio.to_thread(restart, agents, "aruba", sims.arubaos_records())

    # ---------------------------------------------------------------- viewer / errors
    assert (await admin.get("/api/devices/9999/ports")).status_code == 404
    assert (await admin.post("/api/devices/9999/ports/refresh")).status_code == 404

    # ---------------------------------------------------------------- cascade on delete
    assert (await admin.delete(f"/api/devices/{devs['aruba']['id']}")).status_code == 204
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        for table in ("switch_ports", "port_macs", "port_inventory_status"):
            n = await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE device_id = $1", devs["aruba"]["id"]
            )
            assert n == 0, table
        await admin.delete(f"/api/devices/{devs['router']['id']}")
        n = await conn.fetchval(
            "SELECT count(*) FROM arp_entries WHERE source_device_id = $1", devs["router"]["id"]
        )
        assert n == 0
    finally:
        await conn.close()


async def test_refresh_permissions_and_running(rt, admin, viewer, agents) -> None:  # type: ignore[no-untyped-def]
    dev = await _device(admin, agents, "cx")
    r = await viewer.post(f"/api/devices/{dev['id']}/ports/refresh")
    assert r.status_code == 403
    # viewers read the (empty) snapshot of a never-run device
    body = (await viewer.get(f"/api/devices/{dev['id']}/ports")).json()
    st = body["status"]
    # not detected yet -> nothing effective -> no next run
    assert st["last_run_at"] is None and st["ports"] == 0 and st["next_run_at"] is None
    assert set(st["sources"].values()) == {"skipped"}
    assert body["ports"] == [] and body["health"] == []
    # a second refresh while one is queued/running -> 409
    gate = asyncio.Event()
    orig = rt.ports.max_jobs
    rt.ports.max_jobs = 0  # nothing starts: the job stays queued
    try:
        r = await admin.post(f"/api/devices/{dev['id']}/ports/refresh")
        assert r.status_code == 202
        status = (await admin.get(f"/api/devices/{dev['id']}/ports")).json()["status"]
        assert status["running"] is True
        r = await admin.post(f"/api/devices/{dev['id']}/ports/refresh")
        assert r.status_code == 409 and r.json()["error"]["code"] == "ports_refresh_running"
        assert r.json()["error"]["details"] == [{"device_id": dev["id"]}]
    finally:
        gate.set()
        rt.ports.max_jobs = orig
        rt.ports._pump()  # noqa: SLF001
        await rt.ports.idle()
    # port_inventory off -> 409 ports_disabled
    r = await admin.patch(f"/api/devices/{dev['id']}", json={"port_inventory": "off"})
    assert r.json()["ports_effective"] is False
    r = await admin.post(f"/api/devices/{dev['id']}/ports/refresh")
    assert r.status_code == 409 and r.json()["error"]["code"] == "ports_disabled"
    # invalid mode -> 422
    r = await admin.patch(f"/api/devices/{dev['id']}", json={"arp_collect": "yes"})
    assert r.status_code == 422


async def test_failed_run_keeps_snapshot(rt, admin, agents) -> None:  # type: ignore[no-untyped-def]
    # "on": a credential change re-runs class detection, which fails -> unknown -> with "auto"
    # the device would no longer be eligible (409 ports_disabled)
    dev = await _device(admin, agents, "cx", port_inventory="on")
    await _refresh(rt, admin, dev["id"])
    body = (await admin.get(f"/api/devices/{dev['id']}/ports")).json()
    assert len(body["ports"]) == 10
    ok_at = body["status"]["last_ok_at"]
    # wrong community: the run fails, the snapshot stays
    await admin.patch(f"/api/devices/{dev['id']}", json={"snmp_credentials": {"community": "zz"}})
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    rt.hub.listeners.append(q)
    try:
        await _refresh(rt, admin, dev["id"])
    finally:
        rt.hub.listeners.remove(q)
    msgs = [q.get_nowait() for _ in range(q.qsize())]
    assert {"device_id": dev["id"], "ok": False, "arp_source": False} == {
        k: v
        for k, v in next(m["data"] for m in msgs if m["type"] == "ports.updated").items()
        if k != "collected_at"
    }
    body = (await admin.get(f"/api/devices/{dev['id']}/ports")).json()
    st = body["status"]
    assert st["error"] and st["last_ok_at"] == ok_at and len(body["ports"]) == 10


async def test_retention_and_dns_cache(rt, admin, agents, ptr_file) -> None:  # type: ignore[no-untyped-def]
    from netmon.ports import dns, runner

    dev = await _device(admin, agents, "router")
    await _refresh(rt, admin, dev["id"])
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        rows = {r["ip"]: r["name"] for r in await conn.fetch("SELECT ip, name FROM dns_cache")}
        # trailing dot stripped, lower-cased; missing PTR -> NULL
        assert rows == {
            "10.0.0.11": "pc-reception.lan",
            "10.0.0.12": "phone-12.lan",
            "10.0.0.17": None,
            "10.0.0.99": None,
        }
        # nothing is due again right away
        assert await dns.refresh(rt, dev["id"]) == 0
        # an expired "no name" row (> 1 h) is looked up again; a fresh name row (< 24 h) is not
        await conn.execute(
            "UPDATE dns_cache SET resolved_at = now() - interval '2 hours' "
            "WHERE ip IN ('10.0.0.17', '10.0.0.11')"
        )
        assert await dns.refresh(rt, dev["id"]) == 1
        # reverse_dns off: no lookups, hostnames hidden
        await admin.put("/api/settings", json={"reverse_dns": False})
        await conn.execute("UPDATE dns_cache SET resolved_at = now() - interval '3 days'")
        assert await dns.refresh(rt, dev["id"]) == 0
        await admin.put("/api/settings", json={"reverse_dns": True})

        # retention: non-present rows older than port_mac_retention_days are deleted
        did = dev["id"]
        await conn.execute(
            "INSERT INTO port_macs (device_id, if_index, mac, vlan, present, first_seen, "
            "last_seen) VALUES ($1, 1, '00:00:00:00:00:01', 1, false, now() - interval '40 days', "
            "now() - interval '31 days'), ($1, 1, '00:00:00:00:00:02', 1, false, now(), "
            "now() - interval '29 days'), ($1, 1, '00:00:00:00:00:03', NULL, true, "
            "now() - interval '90 days', now() - interval '90 days')",
            did,
        )
        await conn.execute(
            "UPDATE arp_entries SET present = false, last_seen = now() - interval '31 days' "
            "WHERE ip = '10.0.0.11'"
        )
        await conn.execute(
            "INSERT INTO dns_cache (ip, name, resolved_at) VALUES "
            "('10.9.9.9', 'old', now() - interval '8 days')"
        )
        out = await runner.cleanup(rt)
        assert out == {"port_macs": 1, "arp_entries": 1, "dns_cache": 1}
        macs = sorted(r["mac"] for r in await conn.fetch("SELECT mac FROM port_macs"))
        assert macs == ["00:00:00:00:00:02", "00:00:00:00:00:03"]
        # settings validation
        r = await admin.put("/api/settings", json={"port_mac_retention_days": 7})
        assert r.json()["port_mac_retention_days"] == 7
        for bad in (
            {"port_inventory_min": 3},
            {"port_inventory_min": 2000},
            {"uplink_mac_threshold": 0},
            {"port_mac_retention_days": 400},
        ):
            assert (await admin.put("/api/settings", json=bad)).status_code == 422, bad
        r = await admin.put("/api/settings", json={"port_inventory_min": 0})
        assert r.json()["port_inventory_min"] == 0
        body = (await admin.get(f"/api/devices/{did}/ports")).json()
        assert body["status"]["next_run_at"] is None and body["settings"]["interval_min"] == 0
    finally:
        await conn.close()


async def test_mac_moves_reset_first_seen(rt, admin, agents) -> None:  # type: ignore[no-untyped-def]
    """A MAC learned on another port gets the new port and first_seen = now; the same port
    keeps first_seen; the uplink threshold applies on read."""
    dev = await _device(admin, agents, "aruba")
    await _refresh(rt, admin, dev["id"])
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        old = rt.clock.now() - timedelta(days=3)
        await conn.execute(
            "UPDATE port_macs SET first_seen = $1, if_index = CASE WHEN mac = "
            "'00:50:56:01:00:01' THEN 5 ELSE if_index END WHERE device_id = $2",
            old,
            dev["id"],
        )
    finally:
        await conn.close()
    await _refresh(rt, admin, dev["id"])
    p = _ports((await admin.get(f"/api/devices/{dev['id']}/ports")).json())
    assert p[1]["first_mac"]["first_seen"] != p[7]["first_mac"]["first_seen"]
    assert p[7]["first_mac"]["first_seen"].startswith(old.strftime("%Y-%m-%d"))
    # threshold 7: port 9 (7 MACs) is no longer an uplink
    await admin.put("/api/settings", json={"uplink_mac_threshold": 7})
    p = _ports((await admin.get(f"/api/devices/{dev['id']}/ports")).json())
    assert p[9]["is_uplink"] is False and len(p[9]["macs"]) == 7


async def test_link_port_names_resolved_at_read_time(rt, admin, agents) -> None:  # type: ignore[no-untyped-def]
    """A manual link created by ifIndex BEFORE the first inventory run stores the index as
    the port; after the run every output shows the current ifName (stored value kept)."""
    aruba = await _device(admin, agents, "aruba")
    comware = await _device(admin, agents, "comware")
    r = await admin.post(
        "/api/topology/links",
        json={
            "a_device_id": comware["id"],
            "a_if_index": 49,
            "b_device_id": aruba["id"],
            "b_if_index": 10,
        },
    )
    assert r.status_code == 201, r.text
    link_id = r.json()["id"]
    assert {r.json()["a"]["port"], r.json()["b"]["port"]} == {"49", "10"}
    for key in ("aruba", "comware"):
        await _refresh(rt, admin, {"aruba": aruba, "comware": comware}[key]["id"])
    det = (await admin.get(f"/api/devices/{aruba['id']}/ports/10")).json()
    assert [n["port"] for n in det["neighbors"]] == ["Ten-GigabitEthernet1/0/49"]
    assert det["neighbor"]["port"] == "Ten-GigabitEthernet1/0/49"
    p = _ports((await admin.get(f"/api/devices/{comware['id']}/ports")).json())
    assert p[49]["neighbor"]["port"] == "10" and p[49]["is_uplink"] is True
    links = (await admin.get("/api/topology/links")).json()["items"]
    ln = next(x for x in links if x["id"] == link_id)
    ends = {ln["a"]["device_id"]: ln["a"]["port"], ln["b"]["device_id"]: ln["b"]["port"]}
    assert ends == {comware["id"]: "Ten-GigabitEthernet1/0/49", aruba["id"]: "10"}
    graph = (await admin.get("/api/topology")).json()
    gl = next(x for x in graph["links"] if x["id"] == link_id)
    assert "Ten-GigabitEthernet1/0/49" in (gl["a"]["port"], gl["b"]["port"])
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        stored = await conn.fetchrow(
            "SELECT a_port, b_port, a_device_id FROM topology_links WHERE id = $1", link_id
        )
    finally:
        await conn.close()
    assert {stored["a_port"], stored["b_port"]} == {"49", "10"}  # stored value kept
