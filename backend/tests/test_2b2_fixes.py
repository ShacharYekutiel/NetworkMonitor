"""[2b-2] Regression tests for the Architect review fixes (HIGH-1..4, MEDIUM-1..4, LOW-1..3)."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest

from netmon.discovery.classify import classify_rule
from netmon.ports import collect, dns, read, runner
from netmon.ports.eligibility import load_flags
from netmon.ports.search import prefix_pattern
from netmon.templates.library import load_dir
from tests.conftest import _plain, make_device, make_sensor, requires_db
from tests.snmpsim_agents import Record, start_agent, system

CP_IP, SW_IP = "127.0.7.93", "127.0.7.94"


def _free_udp_port(ip: str) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((ip, 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------ HIGH-1 PTR executor
async def test_slow_ptr_lookups_do_not_block_getaddrinfo(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow(addr: tuple[str, int], flags: int) -> tuple[str, str]:
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.6)
        with lock:
            active["now"] -= 1
        return (f"host-{addr[0]}.lan.", "0")

    monkeypatch.setattr(dns.socket, "getnameinfo", slow)
    loop = asyncio.get_running_loop()
    lookups = asyncio.gather(*(dns.lookup(f"10.0.0.{i}") for i in range(20)))
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    await asyncio.wait_for(loop.getaddrinfo("127.0.0.1", 161), 5)  # what pysnmp targets use
    assert time.monotonic() - t0 < 0.3
    names = await lookups
    assert names[3] == "host-10.0.0.3.lan" and active["max"] <= 8


@requires_db
async def test_slow_ptr_lookups_do_not_delay_snmp_get(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from netmon.poller.snmp_client import snmp_get

    port = _free_udp_port(SW_IP)
    agent = await asyncio.to_thread(
        start_agent, SW_IP, port, {"public": system("x", "1.3.6.1.4.1.9.1", "x")}
    )
    if agent is None:
        pytest.skip("snmpsim not available")
    try:
        monkeypatch.setattr(
            dns.socket, "getnameinfo", lambda a, f: (time.sleep(1.0), ("h", "0"))[1]
        )
        lookups = asyncio.gather(*(dns.lookup(f"10.1.0.{i}") for i in range(20)))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        res = await snmp_get(
            SW_IP, {"port": port, "timeout_s": 2, "retries": 0}, ["1.3.6.1.2.1.1.5.0"]
        )
        assert res["1.3.6.1.2.1.1.5.0"] == b"x"
        assert time.monotonic() - t0 < 0.8
        await lookups
    finally:
        await asyncio.to_thread(agent.stop)


# ------------------------------------------------------------------ HIGH-4 Aruba wireless
@pytest.mark.parametrize(
    ("descr", "cls"),
    [
        ("ArubaOS (MODEL: S2500-24P), Version 7.4.1.11", "switch"),
        ("ArubaOS (MODEL: 515), Version 8.10.0.6", "access_point"),
        ("ArubaOS (MODEL: IAP-305), Version 6.5.4.3", "access_point"),
        ("ArubaOS (MODEL: Aruba7010), Version 8.6.0.4", "access_point"),
        ("Aruba Instant Switch", "switch"),
    ],
)
def test_aruba_14823_rule10(descr: str, cls: str) -> None:
    assert classify_rule("1.3.6.1.4.1.14823.1.2.111", descr, "x", set(), False, [], True) == (
        cls,
        "aruba",
        10,
    )


# ------------------------------------------------------------------ MEDIUM-1 template files
def test_deeply_nested_files_are_rejected_per_file(tmp_path: Path) -> None:
    good = tmp_path / "a" / "good_one.yaml"
    good.parent.mkdir()
    good.write_text(
        "schema: 1\nid: good_one\nname: {en: G}\npriority: 1\nmatch: [{}]\n"
        "entries: [{kind: sensor, type: ping, name: {en: Ping}}]\n"
    )
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "deep.yaml").write_text("x: " + "[" * 50000 + "]" * 50000 + "\n")
    (tmp_path / "b" / "deep_json.json").write_text('{"x": ' + "[" * 100000 + "]" * 100000 + "}")
    (tmp_path / "b" / "huge.yaml").write_text("# " + "x" * (300 * 1024))
    templates, errors = load_dir(tmp_path, "user")
    assert [t["id"] for t in templates] == ["good_one"]
    by = {e["file"]: e["message"] for e in errors}
    assert by["b/deep.yaml"].startswith("invalid YAML: ")
    assert by["b/deep_json.json"].startswith("invalid JSON: ")
    assert by["b/huge.yaml"] == "file too large (max 256 KiB)"


@requires_db
async def test_reload_survives_deep_files(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    from netmon.templates import registry

    udir = registry.user_dir(rt)
    udir.mkdir(parents=True, exist_ok=True)
    (udir / "deep.json").write_text('{"x": ' + "[" * 100000 + "]" * 100000 + "}")
    try:
        r = await admin.post("/api/device-templates/reload")
        assert r.status_code == 200
        assert [e["file"] for e in r.json()["errors"]] == ["deep.json"]
        assert r.json()["builtin_count"] == 19
    finally:
        (udir / "deep.json").unlink()
        await registry.reload(rt, broadcast=False)


# ------------------------------------------------------------------ MEDIUM-4 prefix pattern
def test_prefix_pattern() -> None:
    assert prefix_pattern("005056") == "00:50:56%"
    assert prefix_pattern("0050560") == "00:50:56:0%"
    assert prefix_pattern("00505601ab") == "00:50:56:01:ab%"
    assert prefix_pattern("00505601ab0") == "00:50:56:01:ab:0%"


# ------------------------------------------------------------------ LOW-1 capped walks
def _fake_walker(tables: dict[str, dict[str, Any]]):  # type: ignore[no-untyped-def]
    async def walk(oid: str, cap: int) -> dict[str, Any]:
        rows = tables.get(oid, {})
        return dict(list(rows.items())[:cap])

    return walk


async def test_capped_fdb_and_arp_are_errors(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(collect, "FDB_CAP", 4)
    fdb = {f"1.0.0.0.0.0.{i}": 1 for i in range(1, 5)}  # exactly the cap
    tables = {
        f"{collect.IF_ENTRY}.2": {"1": b"1"},
        collect.DOT1Q_FDB_PORT: fdb,
        collect.DOT1Q_FDB_STATUS: {k: 3 for k in fdb},
        collect.IP_NET_TO_PHYS_ADDR: {
            f"2.1.4.10.0.0.{i}": bytes(5) + bytes([i]) for i in range(1, 5)
        },
        collect.IP_NET_TO_PHYS_TYPE: {f"2.1.4.10.0.0.{i}": 3 for i in range(1, 5)},
    }
    w = collect.PortWalks()
    await collect.collect_ports(_fake_walker(tables), "v2c", w)
    assert w.error is None and w.sources["fdb"] == "error" and w.fdb_q is None
    await collect.collect_arp(_fake_walker(tables), w)
    assert w.sources["arp"] == "error" and w.arp == []
    # below the cap: ok
    monkeypatch.setattr(collect, "FDB_CAP", 5)
    w = collect.PortWalks()
    await collect.collect_ports(_fake_walker(tables), "v2c", w)
    assert w.sources["fdb"] == "ok"
    await collect.collect_arp(_fake_walker(tables), w)
    assert w.sources["arp"] == "ok" and len(w.arp) == 4


# ------------------------------------------------------------------ DB / snmpsim tests
@pytest.fixture(scope="module")
def cp_agent() -> Iterator[Any]:
    recs: list[Record] = system("Linux gw-b 3.10.0 #1 SMP x86_64", "1.3.6.1.4.1.8072.3.2.10", "gw")
    recs += [("1.3.6.1.4.1.2620.1.6.7.2.4.0", 66, "41")]
    a = start_agent(CP_IP, _free_udp_port(CP_IP), {"public": recs})
    if a is None:
        pytest.skip("snmpsim not available")
    yield a
    a.stop()


async def _status(rt, did: int) -> Any:  # type: ignore[no-untyped-def]
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        return await conn.fetchrow("SELECT * FROM port_inventory_status WHERE device_id = $1", did)
    finally:
        await conn.close()


async def _exec(rt, sql: str, *args: Any) -> None:  # type: ignore[no-untyped-def]
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


@requires_db
async def test_detection_sends_subtree_probes(rt, admin, cp_agent) -> None:  # type: ignore[no-untyped-def]
    """MEDIUM-2: a Check Point gateway with the net-snmp OID is a router (ARP auto on)."""
    dev = await make_device(
        admin,
        name="gw",
        host=CP_IP,
        snmp_credentials={"port": cp_agent.port, "timeout_s": 1, "retries": 0},
        auto_sensors=["ping", "snmp_system"],
    )
    assert await runner.detect_class(rt, dev["id"]) == "router"
    d = (await admin.get(f"/api/devices/{dev['id']}")).json()
    assert d["effective_class"] == "router" and d["arp_effective"] is True


@requires_db
async def test_failed_detection_keeps_class(rt, admin, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """HIGH-2: a failure keeps the previous class and is retried after 1 h; the device's own
    retries are used (at least 1); a first-ever failure stores unknown."""
    seen: list[dict[str, Any]] = []

    async def failing_get(host: str, creds: dict[str, Any], oids: list[str]) -> Any:
        seen.append(creds)
        from netmon.poller.snmp_client import SnmpError

        raise SnmpError("No SNMP response received before timeout")

    monkeypatch.setattr(runner, "snmp_get", failing_get)
    dev = await make_device(
        admin,
        name="sw",
        host="127.0.7.250",
        snmp_credentials={"port": 1, "retries": 0},
        auto_sensors=["ping", "snmp_system"],
    )
    did = dev["id"]
    assert await runner.detect_class(rt, did) == "unknown"  # first-ever failure
    assert seen[-1]["retries"] == 1
    await _exec(
        rt,
        "UPDATE port_inventory_status SET detected_class = 'switch', "
        "detected_at = now() - interval '25 hours' WHERE device_id = $1",
        did,
    )
    await admin.patch(f"/api/devices/{did}", json={"snmp_credentials": {"retries": 3}})
    await _exec(
        rt,
        "UPDATE port_inventory_status SET detected_at = now() - interval '25 hours' "
        "WHERE device_id = $1",
        did,
    )
    assert await runner.detect_class(rt, did) == "switch"
    assert seen[-1]["retries"] == 3
    row = await _status(rt, did)
    assert row["detected_class"] == "switch"
    now = rt.clock.now()
    age = now - row["detected_at"]
    assert timedelta(hours=22, minutes=59) < age < timedelta(hours=23, minutes=1)
    d = (await admin.get(f"/api/devices/{did}")).json()
    assert d["effective_class"] == "switch" and d["ports_effective"] is True
    async with rt.sessionmaker() as session:
        flags = (await load_flags(session, did))[did]
    assert flags.needs_detection(now) is False
    assert flags.needs_detection(now + timedelta(minutes=61)) is True


@requires_db
async def test_patch_unchanged_credentials_keeps_detection(rt, admin) -> None:  # type: ignore[no-untyped-def]
    """HIGH-3: a rename that re-sends the same snmp_credentials is no identity change."""
    creds = {"version": "v2c", "port": 1161, "community": "c1", "timeout_s": 2, "retries": 1}
    dev = await make_device(admin, name="a", host="127.0.7.251", snmp_credentials=creds)
    did = dev["id"]
    await _exec(
        rt,
        "INSERT INTO port_inventory_status (device_id, detected_class, detected_at) "
        "VALUES ($1, 'switch', now())",
        did,
    )
    rt.ports._force_due.discard(did)  # noqa: SLF001
    sent = dict(dev["snmp_credentials"])
    sent.pop("community_set", None)
    r = await admin.patch(
        f"/api/devices/{did}",
        json={
            "name": "renamed",
            "description": "x",
            "snmp_credentials": {**sent, "community": None},
        },
    )
    assert r.status_code == 200, r.text
    assert (await _status(rt, did))["detected_at"] is not None
    assert did not in rt.ports._force_due  # noqa: SLF001
    r = await admin.patch(f"/api/devices/{did}", json={"snmp_credentials": {"community": "c2"}})
    assert r.status_code == 200
    assert (await _status(rt, did))["detected_at"] is None
    assert did in rt.ports._force_due  # noqa: SLF001
    # host change too
    await _exec(
        rt, "UPDATE port_inventory_status SET detected_at = now() WHERE device_id = $1", did
    )
    await admin.patch(f"/api/devices/{did}", json={"host": "127.0.7.252"})
    assert (await _status(rt, did))["detected_at"] is None


async def _uplink_device(rt, admin, n_macs: int) -> int:  # type: ignore[no-untyped-def]
    dev = await make_device(admin, name="core", host="127.0.7.253", port_inventory="on")
    did = dev["id"]
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        await conn.execute(
            "INSERT INTO switch_ports (device_id, if_index, name, kind, admin_status, "
            "oper_status, mac_count, collected_at) VALUES ($1, 1, '1', 'physical', 'up', 'up', "
            "$2, now()), ($1, 2, '2', 'physical', 'up', 'up', 1, now())",
            did,
            n_macs,
        )
        await conn.execute(
            "INSERT INTO port_macs (device_id, if_index, mac, vlan, first_seen, last_seen) "
            "SELECT $1, 1, '00:50:' || lpad(to_hex(g / 65536), 2, '0') || ':' || "
            "lpad(to_hex((g / 256) % 256), 2, '0') || ':' || lpad(to_hex(g % 256), 2, '0') "
            "|| ':00', 1, now(), now() FROM generate_series(1, $2) g",
            did,
            n_macs,
        )
        await conn.execute(
            "INSERT INTO port_macs (device_id, if_index, mac, vlan, first_seen, last_seen) "
            "VALUES ($1, 2, '00:50:56:01:00:01', 1, now(), now())",
            did,
        )
    finally:
        await conn.close()
    return did


@requires_db
async def test_list_skips_uplink_mac_details(rt, admin, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """MEDIUM-3: 20k MACs on one uplink: the list endpoint builds MAC dicts only for the
    non-uplink port; the detail endpoint still lists them on request."""
    did = await _uplink_device(rt, admin, 20000)
    built = {"n": 0}
    real = read.port_mac_out

    def counting(row: dict[str, Any], det: dict[str, Any]) -> dict[str, Any]:
        built["n"] += 1
        return real(row, det)

    monkeypatch.setattr(read, "port_mac_out", counting)
    t0 = time.monotonic()
    body = (await admin.get(f"/api/devices/{did}/ports")).json()
    assert time.monotonic() - t0 < 5
    p = {x["if_index"]: x for x in body["ports"]}
    assert p[1]["is_uplink"] and p[1]["mac_count"] == 20000 and p[1]["macs"] == []
    assert [m["mac"] for m in p[2]["macs"]] == ["00:50:56:01:00:01"]
    assert built["n"] == 1
    det = (
        await admin.get(f"/api/devices/{did}/ports/1", params={"include_uplink_macs": "true"})
    ).json()
    assert len(det["macs"]) == 1000  # capped


@requires_db
async def test_context_device_info_cached(rt, admin) -> None:  # type: ignore[no-untyped-def]
    from netmon.services.status import load_inventory

    await make_device(admin, name="a", host="127.0.7.254")
    async with rt.sessionmaker() as session:
        ctx = await read.load_context(session, await load_inventory(session), [])
    assert ctx.device_info() is ctx.device_info()


@requires_db
async def test_prefix_search_odd_even_and_index(rt, admin) -> None:  # type: ignore[no-untyped-def]
    did = await _uplink_device(rt, admin, 300)
    # port 1 MACs are 00:50:00:YY:ZZ:00 for g = 1..300 (YY = g // 256, ZZ = g % 256)
    for q, n in (("0050000001", 1), ("005000000", 15), ("00500001", 45), ("005000012", 13)):
        res = (await admin.get("/api/search/mac", params={"q": q})).json()
        assert res["query"]["mac_prefix"] == q
        macs = {i["mac"] for i in res["items"]}
        assert len(macs) == n and all(m.replace(":", "").startswith(q) for m in macs), q
    res = (await admin.get("/api/search/mac", params={"q": "0050560"})).json()
    assert [i["mac"] for i in res["items"]] == ["00:50:56:01:00:01"]
    assert res["items"][0]["device_id"] == did
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        await conn.execute("SET enable_seqscan = off")
        plan = "\n".join(
            r[0]
            for r in await conn.fetch(
                "EXPLAIN SELECT * FROM port_macs WHERE mac LIKE '00:50:56:0%'"
            )
        )
        assert "ix_port_macs_mac_pattern" in plan or "ix_port_macs_mac" in plan, plan
    finally:
        await conn.close()


@requires_db
async def test_percent_base_total_edit_keeps_state(rt, admin) -> None:  # type: ignore[no-untyped-def]
    """LOW-2: a first edit of a 2b-1 sensor that sends percent_base "total" is no change."""
    dev = await make_device(admin, name="r", host="127.0.7.249")
    s = await make_sensor(
        admin, dev["id"], "snmp_custom", {"oid": "1.3.6.1.2.1.1.3.0", "value_type": "counter"}
    )
    assert "percent_base" not in s["config"]
    await _exec(
        rt, "UPDATE sensors SET state = $1 WHERE id = $2", json.dumps({"t": 1, "v": 5}), s["id"]
    )

    async def state() -> Any:
        conn = await asyncpg.connect(_plain(rt.settings.database_url))
        try:
            return json.loads(
                await conn.fetchval("SELECT state FROM sensors WHERE id = $1", s["id"])
            )
        finally:
            await conn.close()

    r = await admin.patch(f"/api/sensors/{s['id']}", json={"config": {"percent_base": "total"}})
    assert r.status_code == 200 and await state() == {"t": 1, "v": 5}
    r = await admin.patch(
        f"/api/sensors/{s['id']}",
        json={
            "config": {
                "value_type": "gauge",
                "percent_of_oid": "1.3.6.1.2.1.1.3.0",
                "percent_base": "sum",
            }
        },
    )
    assert r.status_code == 200 and await state() == {}


# ------------------------------------------------------------------ QA round
@requires_db
async def test_manual_link_default_port_from_inventory(rt, admin) -> None:  # type: ignore[no-untyped-def]
    """QA #4: a manual link with only an ifIndex is named from switch_ports.name, then the
    snmp_interface sensor's if_name, then the index."""
    a = await make_device(admin, name="comware", host="127.0.7.240")
    b = await make_device(admin, name="aruba", host="127.0.7.241")
    await _exec(
        rt,
        "INSERT INTO switch_ports (device_id, if_index, name, kind, admin_status, oper_status, "
        "collected_at) VALUES ($1, 49, 'Ten-GigabitEthernet1/0/49', 'physical', 'up', 'up', "
        "now())",
        a["id"],
    )
    await make_sensor(admin, b["id"], "snmp_interface", {"if_index": 10, "if_name": "eth10"})
    r = await admin.post(
        "/api/topology/links",
        json={"a_device_id": a["id"], "a_if_index": 49, "b_device_id": b["id"], "b_if_index": 10},
    )
    assert r.status_code == 201, r.text
    link = r.json()
    ports = {link["a_device_id"]: link["a_port"], link["b_device_id"]: link["b_port"]}
    assert ports == {a["id"]: "Ten-GigabitEthernet1/0/49", b["id"]: "eth10"}
    r = await admin.post(
        "/api/topology/links",
        json={"a_device_id": a["id"], "a_if_index": 7, "b_device_id": b["id"], "b_if_index": 3},
    )
    link = r.json()
    assert {link["a_port"], link["b_port"]} == {"7", "3"}


@requires_db
async def test_patch_inventory_mode_null_is_422(rt, admin) -> None:  # type: ignore[no-untyped-def]
    """QA #5: an explicit null port_inventory / arp_collect is invalid; omitting is fine."""
    dev = await make_device(admin, name="n", host="127.0.7.242")
    for key in ("port_inventory", "arp_collect"):
        r = await admin.patch(f"/api/devices/{dev['id']}", json={key: None})
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error", key
        assert r.json()["error"]["details"][0]["loc"] == ["body", key]
    r = await admin.patch(f"/api/devices/{dev['id']}", json={"name": "renamed"})
    assert r.status_code == 200 and r.json()["port_inventory"] == "auto"
    r = await admin.patch(f"/api/devices/{dev['id']}", json={"port_inventory": "on"})
    assert r.json()["port_inventory"] == "on"
