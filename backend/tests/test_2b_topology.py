"""[2b-1] Topology: LLDP/CDP collection against snmpsim, matching, links, stale, promotion,
dependency suggestions and the API. Agents on 127.0.6.61-.64 (outside QA's lab range)."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from netmon.runtime import Runtime
from tests.conftest import make_device, make_sensor, requires_db
from tests.snmpsim_agents import Agent, Record, interfaces, start_agent, system

pytestmark = requires_db

CORE, ACC1, ACC2, AP = "127.0.6.61", "127.0.6.62", "127.0.6.63", "127.0.6.64"
PRINTER_IP, PHONE_IP = "127.0.6.99", "127.0.6.98"
LOC = "1.0.8802.1.1.2.1.3"
REM = "1.0.8802.1.1.2.1.4.1.1"
MAN = "1.0.8802.1.1.2.1.4.2.1"
CDP = "1.3.6.1.4.1.9.9.23.1.2.1.1"
MAC = {
    "core": "001a1e000051",
    "acc1": "001a1e000052",
    "acc2": "001a1e000053",
    "ap": "aabbccdd0054",
    "printer": "001122334455",
}


def local(chassis_hex: str, sys_name: str, ports: list[tuple[int, int, str, str]]) -> list[Record]:
    recs: list[Record] = [
        (f"{LOC}.1.0", 2, "4"),
        (f"{LOC}.2.0", "4x", chassis_hex),
        (f"{LOC}.3.0", 4, sys_name),
    ]
    for num, subtype, pid, desc in ports:
        recs += [
            (f"{LOC}.7.1.2.{num}", 2, str(subtype)),
            (f"{LOC}.7.1.3.{num}", 4, pid),
            (f"{LOC}.7.1.4.{num}", 4, desc),
        ]
    return recs


def remote(
    local_port: int,
    idx: int,
    chassis_hex: str,
    port_subtype: int,
    port_id: str,
    sys_name: str,
    port_desc: str = "",
    mgmt: str | None = None,
    port_hex: bool = False,
) -> list[Record]:
    key = f"0.{local_port}.{idx}"
    recs: list[Record] = [
        (f"{REM}.4.{key}", 2, "4"),
        (f"{REM}.5.{key}", "4x", chassis_hex),
        (f"{REM}.6.{key}", 2, str(port_subtype)),
        (f"{REM}.7.{key}", "4x" if port_hex else 4, port_id),
        (f"{REM}.8.{key}", 4, port_desc),
        (f"{REM}.9.{key}", 4, sys_name),
        (f"{REM}.10.{key}", 4, f"{sys_name} system"),
    ]
    if mgmt:
        recs.append((f"{MAN}.3.{key}.1.4.{mgmt}", 2, "2"))
    return recs


def core_records(with_acc2: bool = True) -> list[Record]:
    recs = system("Aruba JL256A 2930F Switch", "1.3.6.1.4.1.11.2.3.7.11.181", "core")
    recs += interfaces([(i, str(i), 6, 1) for i in (1, 2, 3, 4)])
    recs += local(MAC["core"], "core", [(i, 7, str(i), str(i)) for i in (1, 2, 3, 4)])
    recs += remote(1, 1, MAC["acc1"], 5, "GigabitEthernet1/0/1", "access-1", mgmt=ACC1)
    if with_acc2:
        recs += remote(2, 3, MAC["acc2"], 7, "24", "access-2")  # no mgmt address: by chassis
    return recs


def acc1_records() -> list[Record]:
    recs = system("HPE Comware Platform Software, 1950", "1.3.6.1.4.1.25506.11.1.177", "access-1")
    recs += interfaces([(i, f"GigabitEthernet1/0/{i}", 6, 1) for i in (1, 2, 3, 4)])
    # Comware: lldpLocPortNum 101.. is not the ifIndex; port id subtype 5 = interface name
    recs += local(
        MAC["acc1"],
        "access-1",
        [
            (100 + i, 5, f"GigabitEthernet1/0/{i}", f"GigabitEthernet1/0/{i} Interface")
            for i in (1, 2, 3, 4)
        ],
    )
    recs += remote(101, 1, MAC["core"], 7, "1", "core")
    recs += remote(
        102, 1, MAC["ap"], 3, MAC["ap"], "U6-Lite-Office", port_desc="eth0", port_hex=True
    )
    return recs


def acc2_records(with_core: bool = True) -> list[Record]:
    recs = system("Aruba 2530 Switch", "1.3.6.1.4.1.11.2.3.7.11.100", "access-2")
    recs += interfaces([(3, "3", 6, 1), (24, "24", 6, 1)])
    recs += local(MAC["acc2"], "access-2", [(3, 7, "3", "3"), (24, 7, "24", "24")])
    if with_core:
        recs += remote(24, 1, MAC["core"], 7, "2", "core")
    recs += remote(
        3, 2, MAC["printer"], 3, MAC["printer"], "printer-3f", mgmt=PRINTER_IP, port_hex=True
    )
    # one CDP neighbour on ifIndex 3 (a phone, no device)
    phone = "".join(f"{int(o):02x}" for o in PHONE_IP.split("."))
    recs += [
        (f"{CDP}.3.3.1", 2, "1"),
        (f"{CDP}.4.3.1", "4x", phone),
        (f"{CDP}.6.3.1", 4, "SEP001122334466"),
        (f"{CDP}.7.3.1", 4, "Port 1"),
        (f"{CDP}.8.3.1", 4, "Cisco IP Phone 7841"),
    ]
    return recs


def ap_records() -> list[Record]:
    recs = system("Linux U6-Lite 4.4.153", "1.3.6.1.4.1.8072.3.2.10", "U6-Lite-Office")
    recs += interfaces([(1, "lo", 24, 1), (2, "eth0", 6, 1)])
    recs += local(MAC["ap"], "U6-Lite-Office", [(2, 3, "aabbccdd0054", "eth0")])
    recs[-2] = (f"{LOC}.7.1.3.2", "4x", MAC["ap"])
    recs += remote(2, 1, MAC["acc1"], 5, "GigabitEthernet1/0/2", "access-1")
    return recs


def _port(ip: str) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((ip, 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Lab:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self.ports: dict[str, int] = {}

    def start(self, ip: str, recs: list[Record]) -> None:
        if ip in self.agents:
            self.agents.pop(ip).stop()
        port = self.ports.get(ip) or _port(ip)
        a = start_agent(ip, port, {"public": recs})
        if a is None:
            self.stop()
            pytest.skip("snmpsim not available")
        self.agents[ip] = a
        self.ports[ip] = port

    def stop(self) -> None:
        for a in self.agents.values():
            a.stop()
        self.agents.clear()


@pytest.fixture(scope="module")
def lab() -> Iterator[Lab]:
    lab = Lab()
    for ip, recs in (
        (CORE, core_records()),
        (ACC1, acc1_records()),
        (ACC2, acc2_records()),
        (AP, ap_records()),
    ):
        lab.start(ip, recs)
    yield lab
    lab.stop()


async def _snmp_device(c: httpx.AsyncClient, lab: Lab, ip: str, name: str) -> dict[str, Any]:
    d = await make_device(
        c,
        name=name,
        host=ip,
        snmp_credentials={"version": "v2c", "port": lab.ports[ip], "community": "public"},
    )
    await make_sensor(c, d["id"], "snmp_system")
    return d


async def _refresh(rt: Runtime, c: httpx.AsyncClient) -> dict[str, Any]:
    r = await c.post("/api/topology/refresh")
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "running" and r.json()["trigger"] == "manual"
    await rt.topology.wait()
    graph = (await c.get("/api/topology")).json()
    assert graph["last_refresh"]["id"] == r.json()["id"]
    return graph


def _pairs(graph: dict[str, Any]) -> set[tuple[str, str, str, str]]:
    names = {n["key"]: n["name"] for n in graph["nodes"]}
    return {
        (names[ln["a"]["node"]], ln["a"]["port"], names[ln["b"]["node"]], ln["b"]["port"])
        for ln in graph["links"]
    }


async def test_topology_refresh_end_to_end(
    rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient, lab: Lab
) -> None:
    core = await _snmp_device(admin, lab, CORE, "core")
    acc1 = await _snmp_device(admin, lab, ACC1, "access-1")
    acc2 = await _snmp_device(admin, lab, ACC2, "access-2")
    ap = await _snmp_device(admin, lab, AP, "ap-office")
    await make_device(admin, name="ping-only", host="127.0.6.70")  # not walked
    up = await make_sensor(admin, acc1["id"], "snmp_interface", {"if_index": 1, "if_name": "Gi1"})
    assert (await viewer.post("/api/topology/refresh")).status_code == 403
    graph = await _refresh(rt, admin)
    last = graph["last_refresh"]
    assert last["status"] == "completed", last
    assert (last["devices_total"], last["devices_ok"], last["devices_failed"]) == (4, 4, 0)
    assert graph["refresh_running"] is False
    pairs = _pairs(graph)
    assert pairs == {
        ("core", "1", "access-1", "GigabitEthernet1/0/1"),
        ("core", "2", "access-2", "24"),
        ("access-1", "GigabitEthernet1/0/2", "ap-office", "eth0"),
        ("access-2", "3", "printer-3f", "00:11:22:33:44:55"),
        ("access-2", "3", "SEP001122334466", "Port 1"),
    }
    assert last["links_found"] == 5
    links = {(ln["a"]["device_id"], ln["b"]["device_id"]): ln for ln in graph["links"]}
    cl = links[(core["id"], acc1["id"])]
    assert cl["source"] == "lldp" and cl["a"]["if_index"] == 1 and cl["b"]["if_index"] == 1
    assert cl["b"]["sensor_id"] == up["id"]  # the interface sensor of that end
    assert cl["stale"] is False and cl["missed_refreshes"] == 0
    apl = links[(acc1["id"], ap["id"])]
    assert apl["a"]["if_index"] == 2 and apl["b"]["if_index"] == 2  # Comware 102 -> ifIndex 2
    assert links[(core["id"], acc2["id"])]["b"]["if_index"] == 24
    nb = [n for n in graph["nodes"] if n["kind"] == "neighbor"]
    printer = next(n for n in nb if n["name"] == "printer-3f")
    assert printer["chassis_id"] == "00:11:22:33:44:55" and printer["mgmt_address"] == PRINTER_IP
    phone = next(n for n in nb if n["name"] == "SEP001122334466")
    assert phone["chassis_id"] == "cdp:SEP001122334466" and phone["mgmt_address"] == PHONE_IP
    cdp_link = next(ln for ln in graph["links"] if ln["source"] == "cdp")
    assert cdp_link["a"]["if_index"] == 3
    devnodes = {n["name"]: n for n in graph["nodes"] if n["kind"] == "device"}
    assert set(devnodes) == {"core", "access-1", "access-2", "ap-office", "ping-only"}
    assert devnodes["core"]["x"] is None and devnodes["core"]["in_filter"] is True
    # devices learned their own LLDP identity
    async with rt.sessionmaker() as session:
        from sqlalchemy import text

        row = (
            await session.execute(
                text("SELECT lldp_chassis_id, lldp_sys_name FROM devices WHERE id = :i"),
                {"i": ap["id"]},
            )
        ).one()
    assert tuple(row) == ("aa:bb:cc:dd:00:54", "U6-Lite-Office")

    # viewers read the map; the links list is admin-only
    assert (await viewer.get("/api/topology")).status_code == 200
    assert (await viewer.get("/api/topology/links")).status_code == 403
    page = (await admin.get("/api/topology/links", params={"device_id": acc2["id"]})).json()
    assert page["total"] == 3
    assert (await admin.get("/api/topology/links", params={"source": "cdp"})).json()["total"] == 1

    # dependency suggestions (root = core)
    r = await admin.get(
        "/api/topology/dependency-suggestions", params={"root_device_id": core["id"]}
    )
    sug = r.json()
    assert sug["root_device_id"] == core["id"]
    got = {i["device_name"]: (i["suggested_parent_name"], i["hops"]) for i in sug["items"]}
    assert got == {"access-1": ("core", 1), "access-2": ("core", 1), "ap-office": ("access-1", 2)}
    assert sug["items"][0]["current"]["mode"] == "inherit"
    assert sug["items"][0]["effective_parent_id"] is None
    auto = (await admin.get("/api/topology/dependency-suggestions")).json()
    assert auto["root_device_id"] in (core["id"], acc1["id"])  # most links, ties -> lowest id
    # apply them through the bulk endpoint
    items = [
        {
            "device_id": i["device_id"],
            "dependency": {"mode": "device", "device_id": i["suggested_parent_id"]},
        }
        for i in sug["items"]
    ]
    r = await admin.post("/api/devices/dependencies", json={"items": items})
    assert r.status_code == 200 and len(r.json()["updated"]) == 3
    got = (await admin.get(f"/api/devices/{ap['id']}")).json()["effective_dependency"]
    assert got["device_id"] == acc1["id"]

    # layout
    r = await admin.put(
        "/api/topology/layout",
        json={
            "positions": [
                {"node": f"d:{core['id']}", "x": 120.5, "y": -40},
                {"node": "d:99999", "x": 1, "y": 1},
            ]
        },
    )
    assert r.status_code == 204
    graph = (await admin.get("/api/topology")).json()
    node = next(n for n in graph["nodes"] if n["key"] == f"d:{core['id']}")
    assert (node["x"], node["y"]) == (120.5, -40)
    assert (await viewer.put("/api/topology/layout", json={"positions": []})).status_code == 403
    assert (await admin.put("/api/topology/layout", json={"positions": []})).status_code == 422
    printer_key = printer["key"]
    await admin.put(
        "/api/topology/layout", json={"positions": [{"node": printer_key, "x": 5, "y": 6}]}
    )

    # group filter: only links touching the group's devices; outside devices in_filter false
    gid = (await admin.get("/api/groups", params={"parent_id": "root"})).json()["items"][0]["id"]
    sub = (await admin.post("/api/groups", json={"name": "Edge", "parent_id": gid})).json()
    await admin.patch(f"/api/devices/{ap['id']}", json={"group_id": sub["id"]})
    g2 = (await admin.get("/api/topology", params={"group_id": sub["id"]})).json()
    assert {(n["name"], n["in_filter"]) for n in g2["nodes"]} == {
        ("ap-office", True),
        ("access-1", False),
    }
    assert len(g2["links"]) == 1

    # manual links
    r = await admin.post(
        "/api/topology/links",
        json={
            "a_device_id": acc2["id"],
            "a_if_index": 24,
            "b_device_id": core["id"],
            "b_neighbor_id": None,
            "b_if_index": None,
            "b_port": "uplink",
        },
    )
    assert r.status_code == 201, r.text
    manual = r.json()
    assert manual["source"] == "manual"
    # canonical order: core (lower id) is end a
    assert manual["a"]["device_id"] == core["id"] and manual["b"]["port"] == "24"
    assert manual["a"]["port"] == "uplink"
    bad = [
        {"a_device_id": core["id"]},
        {"a_device_id": core["id"], "b_device_id": acc1["id"], "b_neighbor_id": 1},
        {"a_device_id": core["id"], "b_device_id": core["id"]},
        {"a_device_id": 99999, "b_device_id": core["id"]},
        {"a_device_id": core["id"], "b_device_id": acc1["id"], "bogus": 1},
    ]
    for body in bad:
        assert (await admin.post("/api/topology/links", json=body)).status_code == 422, body
    r = await admin.patch(f"/api/topology/links/{manual['id']}", json={"b_port": "Port 24"})
    assert r.status_code == 200 and r.json()["b"]["port"] == "Port 24"
    r = await admin.patch(f"/api/topology/links/{cl['id']}", json={"b_port": "x"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "link_not_manual"
    # a discovered link identical to a manual one is hidden
    dup = await admin.post(
        "/api/topology/links",
        json={
            "a_device_id": core["id"],
            "a_port": "1",
            "b_device_id": acc1["id"],
            "b_port": "GigabitEthernet1/0/1",
        },
    )
    assert dup.status_code == 201
    g3 = (await admin.get("/api/topology")).json()
    ends = [
        (ln["source"], ln["a"]["port"], ln["b"]["port"])
        for ln in g3["links"]
        if ln["a"]["device_id"] == core["id"] and ln["b"]["device_id"] == acc1["id"]
    ]
    assert ends == [("manual", "1", "GigabitEthernet1/0/1")]
    assert (await admin.delete(f"/api/topology/links/{dup.json()['id']}")).status_code == 204

    # stale: core and access-2 stop reporting each other; 3 refreshes -> stale
    await asyncio.to_thread(lab.start, CORE, core_records(with_acc2=False))
    await asyncio.to_thread(lab.start, ACC2, acc2_records(with_core=False))
    for n in (1, 2, 3):
        graph = await _refresh(rt, admin)
        ln = next(x for x in graph["links"] if x["id"] == links[(core["id"], acc2["id"])]["id"])
        assert ln["missed_refreshes"] == n and ln["stale"] is (n >= 3)
    nostale = (await admin.get("/api/topology", params={"include_stale": "false"})).json()
    assert all(not x["stale"] for x in nostale["links"])
    # the manual link core-access-2 stays fresh
    assert next(x for x in graph["links"] if x["id"] == manual["id"])["stale"] is False

    # promotion: a device with the printer's management address appears
    printer_dev = await make_device(admin, name="printer", host=PRINTER_IP)
    graph = await _refresh(rt, admin)
    assert "printer-3f" not in {n["name"] for n in graph["nodes"]}
    pl = next(x for x in graph["links"] if x["b"]["device_id"] == printer_dev["id"])
    assert pl["a"]["device_id"] == acc2["id"] and pl["source"] == "lldp"
    node = next(n for n in graph["nodes"] if n["key"] == f"d:{printer_dev['id']}")
    assert (node["x"], node["y"]) == (5, 6)  # the layout row moved with it
    assert len([x for x in graph["links"] if x["b"]["device_id"] == printer_dev["id"]]) == 1

    # delete all stale links
    assert (await admin.delete("/api/topology/links")).status_code == 422
    r = await admin.delete("/api/topology/links", params={"stale": "true"})
    assert r.status_code == 200 and r.json() == {"deleted": 1}
    assert (await admin.delete("/api/topology/layout")).status_code == 204
    graph = (await admin.get("/api/topology")).json()
    assert all(n["x"] is None for n in graph["nodes"])


async def test_refresh_conflict_and_failed_device(
    rt: Runtime, admin: httpx.AsyncClient, lab: Lab
) -> None:
    # a device that does not answer SNMP -> counted failed, refresh still completes
    d = await make_device(
        admin,
        name="dead",
        host="127.0.6.77",
        snmp_credentials={
            "version": "v2c",
            "port": _port("127.0.6.77"),
            "timeout_s": 1,
            "retries": 0,
        },
    )
    await make_sensor(admin, d["id"], "snmp_system")
    r1 = await admin.post("/api/topology/refresh")
    assert r1.status_code == 202
    r2 = await admin.post("/api/topology/refresh")
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "topology_refresh_running"
    assert r2.json()["error"]["details"] == [{"refresh_id": r1.json()["id"]}]
    await rt.topology.wait()
    last = (await admin.get("/api/topology")).json()["last_refresh"]
    assert last["status"] == "completed" and last["devices_failed"] == 1
    assert last["devices_ok"] == 0


async def test_mark_interrupted_and_scheduler(rt: Runtime, admin: httpx.AsyncClient) -> None:
    async with rt.sessionmaker() as session:
        from sqlalchemy import text

        await session.execute(
            text("INSERT INTO topology_refreshes (trigger, status) VALUES ('scheduled', 'running')")
        )
        await session.commit()
    assert await rt.topology.mark_interrupted() == 1
    last = (await admin.get("/api/topology")).json()["last_refresh"]
    assert last["status"] == "failed" and last["error"] == "Interrupted by restart"
    # settings: 0 = off, 5-1440
    for bad in (1, 4, 1441, -1):
        r = await admin.put("/api/settings", json={"topology_refresh_min": bad})
        assert r.status_code == 422, bad
    r = await admin.put("/api/settings", json={"topology_refresh_min": 0})
    assert r.json()["topology_refresh_min"] == 0
    from netmon.topology.scheduler import TopologyScheduler

    sched = TopologyScheduler(rt)
    sched.first_run_s = 0.0
    await sched.start()
    try:
        assert sched.next_at is None  # off
        await admin.put("/api/settings", json={"topology_refresh_min": 5})
        await sched.reschedule()
        assert sched.interval_min == 5 and sched.next_at is not None
    finally:
        await sched.stop()


async def test_l5_no_miss_without_lldp_data(
    rt: Runtime, admin: httpx.AsyncClient, lab: Lab
) -> None:
    """A device that answers SNMP but returns no LLDP/CDP rows does not age its links."""
    from sqlalchemy import text

    bare_ip = "127.0.6.65"
    await asyncio.to_thread(lab.start, bare_ip, system("Bare switch", "1.3.6.1.4.1.9999.1", "bare"))
    bare = await _snmp_device(admin, lab, bare_ip, "bare")
    other = await make_device(admin, name="peer", host="127.0.6.66")  # not walked
    core = await _snmp_device(admin, lab, CORE, "core")
    async with rt.sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO topology_links (source, a_device_id, a_port, b_device_id, b_port) "
                "VALUES ('lldp', :a, '1', :b, '2'), ('lldp', :c, '9', :a, '9')"
            ),
            {"a": bare["id"], "b": other["id"], "c": core["id"]},
        )
        await session.commit()
    graph = await _refresh(rt, admin)
    assert graph["last_refresh"]["devices_ok"] == 2
    by = {(ln["a"]["device_id"], ln["b"]["device_id"]): ln for ln in graph["links"]}
    lone = by.get((bare["id"], other["id"])) or by[(other["id"], bare["id"])]
    assert lone["missed_refreshes"] == 0  # the bare device says nothing about its links
    seen_by_core = by.get((core["id"], bare["id"])) or by[(bare["id"], core["id"])]
    assert seen_by_core["missed_refreshes"] == 1  # core returned LLDP data without it


async def test_l7_promotion_merges_parallel_link(rt: Runtime, admin: httpx.AsyncClient) -> None:
    from sqlalchemy import text

    from netmon.topology.collect import DeviceData
    from netmon.topology.refresh import Matcher, TopologyRunner, _Dev

    x = await make_device(admin, name="sw", host="127.0.6.80")
    y = await make_device(admin, name="nas", host="127.0.6.81")
    async with rt.sessionmaker() as session:
        nb = (
            await session.execute(
                text(
                    "INSERT INTO topology_neighbors (chassis_id, sys_name, mgmt_address) "
                    "VALUES ('aa:bb:cc:00:00:01', 'nas', '127.0.6.81') RETURNING id"
                )
            )
        ).scalar_one()
        old = (
            await session.execute(
                text(
                    "INSERT INTO topology_links (source, a_device_id, a_port, b_neighbor_id, "
                    "b_port, first_seen) VALUES ('lldp', :x, '3', :n, 'eth0', "
                    "now() - interval '3 days') RETURNING id"
                ),
                {"x": x["id"], "n": nb},
            )
        ).scalar_one()
        direct = (
            await session.execute(
                text(
                    "INSERT INTO topology_links (source, a_device_id, a_port, b_device_id, "
                    "b_if_index, b_port) VALUES ('lldp', :x, '3', :y, 2, 'Ethernet 0') "
                    "RETURNING id"
                ),
                {"x": x["id"], "y": y["id"]},
            )
        ).scalar_one()
        devs = [_Dev(x["id"], "sw", "127.0.6.80", None, None), _Dev(y["id"], "nas", "127.0.6.81",
                None, None)]  # fmt: skip
        results = {y["id"]: DeviceData(ok=True, if_names={2: "Ethernet 0"})}
        await TopologyRunner(rt)._promote(session, Matcher(devs), {direct}, results)  # noqa: SLF001
        await session.commit()
        rows = (await session.execute(text("SELECT * FROM topology_links"))).all()
        nbs = (await session.execute(text("SELECT count(*) FROM topology_neighbors"))).scalar()
    assert nbs == 0
    assert [r.id for r in rows] == [direct]  # one edge, no duplicate
    assert rows[0].b_port == "Ethernet 0" and rows[0].b_if_index == 2
    assert rows[0].first_seen < datetime.now(UTC) - timedelta(days=2)  # the oldest kept
    assert old not in [r.id for r in rows]
