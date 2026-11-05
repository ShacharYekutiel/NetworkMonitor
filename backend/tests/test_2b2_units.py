"""[2b-2] Pure units: port parsers, snapshot builder, view rules, health strip, OUI, search
normalisation, snmp_walk_count, percent_base, template `when`, classification 9a / F23a."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from netmon.api.errors import ApiError
from netmon.discovery.classify import classify_fingerprint, classify_host, classify_rule
from netmon.discovery.fingerprint import find_markers
from netmon.poller.checks.snmp_walk_count import count_rows
from netmon.poller.sensor_types import validate_config
from netmon.ports import collect as c
from netmon.ports import health, oui, view
from netmon.ports.eligibility import arp_effective, effective_class, ports_effective
from netmon.ports.search import normalize_query
from netmon.ports.snapshot import build_snapshot, error_delta, last_change, rate, speed
from netmon.templates.apply import identity, when_passes
from netmon.templates.builtin import oid_has_prefix, rule_matches, suggest

NOW = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)


# ------------------------------------------------------------------ parsers
@pytest.mark.parametrize(
    ("if_type", "name", "kind"),
    [
        (6, "1", "physical"),
        (62, "Fa0/1", "physical"),
        (69, "x", "physical"),
        (117, "Gi1/0/1", "physical"),
        (6, "M-GigabitEthernet0/0/0", "mgmt"),
        (6, "mgmt", "mgmt"),
        (6, "Management1", "mgmt"),
        (6, "oobm", "mgmt"),
        (6, "MgmtEth0/RP0/CPU0/0", "mgmt"),
        (6, "Ma1", "mgmt"),
        (161, "Trk1", "lag"),
        (54, "Bridge-Aggregation1", "lag"),
        (53, "DEFAULT_VLAN", "vlan"),
        (136, "Vlan-interface1", "vlan"),
        (135, "vlan10", "vlan"),
        (24, "InLoopBack0", "other"),
        (None, "x", "other"),
        (1, "NULL0", "other"),
    ],
)
def test_port_kind(if_type: int | None, name: str, kind: str) -> None:
    assert c.port_kind(if_type, name) == kind


def test_portlist_bit_order() -> None:
    assert c.portlist(b"\x80") == {1}
    assert c.portlist(b"\x01") == {8}
    assert c.portlist(b"\x00\x80") == {9}
    assert c.portlist(b"\xc0\x01") == {1, 2, 16}
    assert c.portlist(bytes.fromhex("ff")) == set(range(1, 9))
    assert c.portlist("x") == set() and c.portlist(None) == set() and c.portlist(5) == set()


def test_mac_helpers() -> None:
    assert c.mac_text(bytes.fromhex("001A1E000101")) == "00:1a:1e:00:01:01"
    assert c.mac_text(b"\x00\x01") is None and c.mac_text("abc") is None
    assert c.mac_from_arcs([0, 80, 86, 1, 0, 7]) == "00:50:56:01:00:07"
    assert c.mac_from_arcs([1, 2, 3]) is None and c.mac_from_arcs([300, 0, 0, 0, 0, 0]) is None
    assert c.locally_administered("da:a1:19:00:00:04") is True
    assert c.locally_administered("02:00:00:00:09:01") is True
    assert c.locally_administered("00:50:56:01:00:01") is False
    assert c.locally_administered(None) is False


def test_bridge_map() -> None:
    assert c.bridge_map({1: 1, 5: 49, 6: 0}, {1, 49}) == {1: 1, 5: 49}
    assert c.bridge_map({}, {1, 2, 12}) == {1: 1, 2: 2, 12: 12}  # ArubaOS: bp = ifIndex


def test_vlan_sets() -> None:
    eg = {1: bytes.fromhex("e0"), 10: bytes.fromhex("30"), 20: bytes.fromhex("10")}
    un = {1: bytes.fromhex("e0"), 10: bytes.fromhex("20")}
    out = c.vlan_sets(eg, un, {1: 1, 3: 10}, {1, 2, 3, 4, 5})
    assert (out[1].mode, out[1].pvid, out[1].untagged, out[1].tagged) == ("access", 1, [1], [])
    assert (out[3].mode, out[3].pvid, out[3].untagged, out[3].tagged) == ("access", 10, [1, 10], [])
    assert (out[4].mode, out[4].tagged, out[4].untagged) == ("trunk", [10, 20], [])
    assert (out[5].mode, out[5].pvid) == (None, None)
    # pvid missing and exactly one untagged VLAN -> that VLAN
    assert c.vlan_sets({}, {7: b"\x40"}, {}, {2})[2].pvid == 7
    assert c.vlan_sets({}, {7: b"\x40", 8: b"\x40"}, {}, {2})[2].pvid is None


def test_current_vlan_columns_and_fdb_ids() -> None:
    assert c.current_vlan_columns({"0.1": b"a", "5.1": b"b", "0.10": b"c", "x": 1}) == {
        1: b"b",
        10: b"c",
    }
    assert c.fdb_id_to_vlan({"0.1": 1, "0.10": 1010, "0.20": 1010, "0.30": "x"}) == {
        1: 1,
        1010: 10,
    }


def _q(entries: list[tuple[int, str, int, int]]) -> tuple[dict, dict]:
    ports, statuses = {}, {}
    for fid, mac, bp, st in entries:
        sfx = f"{fid}." + ".".join(str(int(x, 16)) for x in mac.split(":"))
        ports[sfx], statuses[sfx] = bp, st
    return ports, statuses


def test_fdb_q_bridge() -> None:
    ports, st = _q(
        [
            (1, "00:50:56:01:00:01", 1, 3),
            (1, "00:1a:1e:00:00:61", 3, 4),  # self -> dropped
            (1010, "00:0c:29:00:01:04", 4, 3),  # fdbId 1010 -> VLAN 10
            (1, "00:0c:29:00:00:09", 0, 3),  # bridge port 0 -> dropped
            (1, "00:0c:29:00:00:0a", 9, 3),  # unmapped -> dropped
            (2, "00:50:56:01:00:01", 2, 3),  # same MAC, another VLAN
        ]
    )
    out = c.parse_fdb_q(ports, st, {1: 1, 2: 2, 3: 3, 4: 4}, {1: 1, 1010: 10})
    assert [(e.mac, e.vlan, e.if_index) for e in out] == [
        ("00:50:56:01:00:01", 1, 1),
        ("00:50:56:01:00:01", 2, 2),  # fdbId 2 not in dot1qVlanFdbId -> fdbId
        ("00:0c:29:00:01:04", 10, 4),
    ]
    # no dot1qVlanFdbId rows -> vlan = fdbId; duplicates: first by suffix order wins
    ports, st = _q([(5, "00:00:00:00:00:01", 1, 3)])
    ports["5.0.0.0.0.0.1.extra"] = 1
    assert [(e.vlan) for e in c.parse_fdb_q(ports, st, {1: 7}, {})] == [5]
    assert c.parse_fdb_q(ports, st, {1: 7}, {})[0].if_index == 7


def test_fdb_dot1d_fallback() -> None:
    ports = {"0.0.12.0.204.1": 1, "0.0.12.0.204.2": 2, "0.0.12.0.204.3": 1, "1.2": 1}
    st = {"0.0.12.0.204.1": 3, "0.0.12.0.204.2": 3, "0.0.12.0.204.3": 5}
    out = c.parse_fdb_dot1d(ports, st, {1: 1, 2: 49})
    assert [(e.mac, e.vlan, e.if_index) for e in out] == [
        ("00:00:0c:00:cc:01", None, 1),
        ("00:00:0c:00:cc:02", None, 49),
    ]


def test_arp_parsers() -> None:
    m = bytes.fromhex("005056010001")
    addrs = {
        "2.1.4.10.0.0.11": m,  # with the length arc
        "2.1.10.0.0.12": bytes.fromhex("00000c000003"),  # without
        "2.1.4.10.0.0.50": m,  # invalid(2)
        "2.1.4.10.0.0.1": m,  # local(5)
        "2.1.4.10.0.0.2": bytes(6),  # zero MAC
        "2.1.4.10.0.0.3": b"\xff" * 6,  # broadcast
        "2.2.16." + ".".join(["254", "128"] + ["0"] * 13 + ["1"]): m,  # IPv6 ignored
        "2.1.4.10.0.0.99": m,
    }
    types = {k: 3 for k in addrs}
    types.update({"2.1.4.10.0.0.50": 2, "2.1.4.10.0.0.1": 5, "2.1.4.10.0.0.99": 4})
    out = c.parse_arp_phys(addrs, types)
    assert sorted((e.ip, e.mac, e.if_index) for e in out) == [
        ("10.0.0.11", "00:50:56:01:00:01", 2),
        ("10.0.0.12", "00:00:0c:00:00:03", 2),
        ("10.0.0.99", "00:50:56:01:00:01", 2),
    ]
    media = {"1.10.0.0.14": bytes.fromhex("daa119000004"), "1.10.0.0.15": m, "1.10.0.0.16": m}
    out = c.parse_arp_media(media, {"1.10.0.0.14": 3, "1.10.0.0.15": 2, "1.10.0.0.16": 1})
    assert [(e.ip, e.mac) for e in out] == [("10.0.0.14", "da:a1:19:00:00:04")]


def test_name_tail_and_pse_mapping() -> None:
    assert c.name_tail("14") == (1, 14)
    assert c.name_tail("2/14") == (2, 14)
    assert c.name_tail("1/1/14") == (1, 14)
    assert c.name_tail("GigabitEthernet2/0/3") == (2, 3)
    assert c.name_tail("Trk") is None
    # rule 1: PSE port = ifIndex of a physical port
    assert c.map_pse_ports([(1, 1), (1, 2)], {1: "1", 2: "2"}) == {(1, 1): 1, (1, 2): 2}
    # rule 2: stack names member/port (ArubaOS) and CX 1/1/x
    ports = {101: "1/1", 102: "1/2", 201: "2/1"}
    assert c.map_pse_ports([(1, 1), (1, 2), (2, 1)], ports) == {
        (1, 1): 101,
        (1, 2): 102,
        (2, 1): 201,
    }
    cx = {11: "1/1/1", 12: "1/1/2"}
    assert c.map_pse_ports([(1, 1), (1, 2)], cx) == {(1, 1): 11, (1, 2): 12}
    # rule 3: nothing maps
    assert c.map_pse_ports([(1, 1), (3, 9)], {5: "x"}) is None
    assert c.map_pse_ports([], {}) == {}


# ------------------------------------------------------------------ snapshot
def test_rates_and_deltas() -> None:
    assert rate(2000, 1000, 64, 10) == 800.0
    assert rate(100, 2**32 - 100, 32, 8) == 200.0  # 32-bit wrap
    assert rate(100, 1000, 64, 10) is None  # 64-bit reset
    assert rate(1, None, 32, 10) is None and rate(1, 0, 32, 0) is None
    assert error_delta(5, 3) == 2 and error_delta(3, 5) is None and error_delta(None, 1) is None
    assert last_change(NOW, 100000000, 99000000) == NOW - timedelta(seconds=10000)
    assert last_change(NOW, 100, 0) is None and last_change(NOW, 100, 200) is None
    assert last_change(NOW, None, 5) is None
    assert speed(10000, 4294967295) == 10_000_000_000
    assert speed(0, 100000000) == 100000000 and speed(0, 0) is None and speed(None, None) is None


def _walks(**kw):  # type: ignore[no-untyped-def]
    w = c.PortWalks()
    w.sys_uptime = 100000000
    w.if_cols = {
        "descr": {1: b"1", 2: b"2", 3: b"Trk1"},
        "type": {1: 6, 2: 6, 3: 161},
        "speed": {1: 1000000000, 2: 0, 3: 0},
        "phys": {1: bytes.fromhex("001a1e000101")},
        "admin": {1: 1, 2: 1, 3: 1},
        "oper": {1: 1, 2: 2, 3: 1},
        "last_change": {1: 99000000},
        "in_octets": {1: 100, 2: 10},
        "out_octets": {1: 200, 2: 10},
        "in_errors": {1: 5, 2: 0},
        "out_errors": {1: 1, 2: 0},
    }
    w.ifx_cols = {
        "name": {},
        "hc_in": {1: 1000},
        "hc_out": {1: 2000},
        "high_speed": {},
        "alias": {1: b"PC"},
    }
    w.sources = {"if_xtable": "ok", "duplex": "ok", "bridge": "empty", "vlans": "ok", "poe": "ok"}
    w.sources.update({"fdb": "ok", "fdb_mib": "q_bridge", "vlan_mib": "static"})
    w.duplex = {1: 3, 2: 9}
    w.untagged = {1: b"\x80"}
    w.egress = {1: b"\x80", 10: b"\xc0"}
    w.pvids = {}
    w.poe = {(1, 1): (1, 3, 4), (1, 2): (2, 4, None)}
    ports, st = _q([(1, "00:50:56:01:00:01", 1, 3), (10, "00:50:56:01:00:02", 2, 3)])
    w.fdb_q = (ports, st, {})
    for k, v in kw.items():
        setattr(w, k, v)
    return w


def test_build_snapshot_first_and_second_run() -> None:
    snap = build_snapshot({}, _walks(), NOW)
    rows = {r["if_index"]: r for r in snap.rows}
    r1, r2, r3 = rows[1], rows[2], rows[3]
    assert (r1["name"], r1["alias"], r1["kind"], r1["mac"]) == (
        "1",
        "PC",
        "physical",
        "00:1a:1e:00:01:01",
    )
    assert (r1["counter_bits"], r1["in_octets"], r1["out_octets"]) == (64, 1000, 2000)
    assert (r2["counter_bits"], r2["in_octets"]) == (32, 10)
    assert r1["last_change_at"] == NOW - timedelta(seconds=10000)
    assert (r1["duplex"], r2["duplex"], r3["duplex"]) == ("full", "unknown", None)
    assert (r1["speed_bps"], r2["speed_bps"]) == (1000000000, None)
    assert (r1["mode"], r1["pvid"], r1["untagged_vlans"], r1["tagged_vlans"]) == (
        "trunk",
        1,
        [1],
        [10],
    )
    assert (r2["mode"], r2["tagged_vlans"], r2["pvid"]) == ("trunk", [10], None)
    assert (r3["kind"], r3["mode"], r3["bridge_port"]) == ("lag", None, 3)
    assert (r1["poe_admin_enabled"], r1["poe_detection"], r1["poe_class"]) == (
        True,
        "delivering",
        3,
    )
    assert (r2["poe_admin_enabled"], r2["poe_detection"], r2["poe_class"]) == (False, "fault", None)
    assert r3["poe_detection"] is None
    assert [(e.mac, e.vlan, e.if_index) for e in snap.fdb or []] == [
        ("00:50:56:01:00:01", 1, 1),
        ("00:50:56:01:00:02", 10, 2),
    ]
    assert (r1["mac_count"], r2["mac_count"], r3["mac_count"]) == (1, 1, 0)
    assert r1["in_bps"] is None and r1["in_errors_delta"] is None
    # second run 10 s later: rates and deltas
    prev = {r["if_index"]: r for r in snap.rows}
    w = _walks()
    w.ifx_cols["hc_in"] = {1: 2000}
    w.if_cols["in_errors"] = {1: 7, 2: 0}
    w.if_cols["in_octets"] = {1: 100, 2: 5}  # port 2: 32-bit decrease -> wrap
    snap2 = build_snapshot(prev, w, NOW + timedelta(seconds=10))
    rows2 = {r["if_index"]: r for r in snap2.rows}
    assert rows2[1]["in_bps"] == 800.0 and rows2[1]["out_bps"] == 0.0
    assert rows2[1]["in_errors_delta"] == 2 and rows2[1]["out_errors_delta"] == 0
    assert rows2[2]["in_bps"] == round((2**32 - 5) * 8 / 10, 1)
    # counter width changed -> no rate
    w = _walks()
    w.ifx_cols["hc_in"] = {}
    rows3 = {r["if_index"]: r for r in build_snapshot(prev, w, NOW + timedelta(seconds=10)).rows}
    assert rows3[1]["counter_bits"] == 32 and rows3[1]["in_bps"] is None


def test_build_snapshot_errors_keep_previous() -> None:
    prev = {r["if_index"]: r for r in build_snapshot({}, _walks(), NOW).rows}
    prev[1]["name"] = "old-name"
    w = _walks()
    for k in ("if_xtable", "duplex", "vlans", "poe", "fdb", "bridge"):
        w.sources[k] = "error"
    w.duplex, w.poe, w.egress, w.untagged = {}, {}, {}, {}
    snap = build_snapshot(prev, w, NOW + timedelta(seconds=60))
    r1 = {r["if_index"]: r for r in snap.rows}[1]
    assert snap.fdb is None
    assert (r1["name"], r1["alias"], r1["duplex"]) == ("old-name", "PC", "full")
    assert (r1["poe_detection"], r1["mode"], r1["mac_count"], r1["bridge_port"]) == (
        "delivering",
        prev[1]["mode"],
        1,
        1,
    )


def test_build_snapshot_empty_and_unmapped() -> None:
    w = _walks(fdb_q=None)
    w.sources.update({"fdb": "empty", "fdb_mib": None, "if_xtable": "skipped"})
    w.ifx_cols = {}
    w.poe = {(3, 7): (1, 3, 1)}
    snap = build_snapshot({}, w, NOW)
    assert snap.fdb == [] and snap.sources["poe"] == "unmapped"
    r1 = {r["if_index"]: r for r in snap.rows}[1]
    assert r1["poe_detection"] is None and r1["counter_bits"] == 32 and r1["alias"] == ""
    assert snap.sources["arp"] == "skipped"


# ------------------------------------------------------------------ view
def _link(
    id_: int,
    a: int,
    a_if: int | None,
    a_port: str,
    b: int | None,
    b_if=None,
    b_port="",
    nb=None,
    source="lldp",
):  # type: ignore[no-untyped-def]
    return {
        "id": id_,
        "source": source,
        "a_device_id": a,
        "a_if_index": a_if,
        "a_port": a_port,
        "b_device_id": b,
        "b_neighbor_id": nb,
        "b_if_index": b_if,
        "b_port": b_port,
    }


def test_neighbors_uplink_and_tiles() -> None:
    devices = {
        2: {"name": "core", "host": "10.0.0.2", "device_class": "switch", "status": "up"},
        3: {"name": "pc", "host": "10.0.0.3", "device_class": "workstation", "status": "up"},
        4: {"name": "ap", "host": "10.0.0.4", "device_class": "access_point", "status": "down"},
    }
    nbs = {9: {"chassis_id": "aa:bb", "sys_name": None, "mgmt_address": "10.0.0.9"}}
    row = {"if_index": 10, "name": "10", "descr": "Port 10"}
    links = [
        _link(5, 1, None, "port 10", None, nb=9, b_port="eth0"),  # by name (case-insensitive)
        _link(7, 1, 10, "10", 2, 49, "Te1/0/49"),
        _link(3, 2, 50, "Te1/0/50", 1, 10, "10", source="manual"),  # b side
        _link(8, 1, 11, "11", 3),  # another port
    ]
    out = view.port_neighbors(1, row, links, devices, nbs)
    assert [(n["link_id"], n["kind"]) for n in out] == [
        (3, "device"),
        (7, "device"),
        (5, "neighbor"),
    ]
    assert out[0]["port"] == "Te1/0/50" and out[0]["source"] == "manual"
    assert out[2]["name"] == "aa:bb" and out[2]["mgmt_address"] == "10.0.0.9"
    assert view.uplink(out, 0, 5) == (True, "neighbor")
    # client neighbour: MAC count decides; threshold boundary 5 -> no, 6 -> yes
    pc = view.port_neighbors(1, {"if_index": 11, "name": "11", "descr": ""}, links, devices, nbs)
    assert view.uplink(pc, 5, 5) == (False, None)
    assert view.uplink(pc, 6, 5) == (True, "mac_count")
    assert view.connected_kind("up", out[0]) == "switch"
    ap = [{"kind": "device", "device_class": "access_point"}]
    assert view.uplink(ap, 0, 5) == (True, "neighbor")
    assert view.connected_kind("up", ap[0]) == "ap"
    assert view.connected_kind("up", pc[0]) == "client"
    assert view.connected_kind("up", out[2]) == "client"
    assert view.connected_kind("up", None) == "client"
    assert view.connected_kind("down", out[0]) == "none"


def test_state_and_alerts() -> None:
    assert view.port_state("down", "up") == "admin_down"
    assert view.port_state("up", "up") == "up"
    assert view.port_state("up", "notPresent") == "absent"
    assert view.port_state("up", "lowerLayerDown") == "down"
    assert view.alert_reasons("up", "down", 1, None, "fault") == ["sensor", "errors", "poe_fault"]
    assert view.alert_reasons("down", "warning", 0, 0, "other_fault") == ["sensor", "poe_fault"]
    assert view.alert_reasons("up", "up", 0, 0, "delivering") == []
    assert view.alert_reasons("admin_down", "down", 5, 5, "fault") == []


def test_mac_ips_and_first_mac() -> None:
    t0 = NOW
    rows = [
        {"ip": "10.0.0.20", "present": True, "last_seen": t0},
        {"ip": "10.0.0.3", "present": True, "last_seen": t0},
        {"ip": "10.0.0.9", "present": False, "last_seen": t0 + timedelta(days=1)},
    ]
    assert view.mac_ips(rows) == ("10.0.0.3", ["10.0.0.3", "10.0.0.20"])  # tie: lowest IP
    rows = [
        {"ip": "10.0.0.9", "present": False, "last_seen": t0},
        {"ip": "10.0.0.8", "present": False, "last_seen": t0 - timedelta(1)},
    ]
    assert view.mac_ips(rows) == ("10.0.0.9", [])  # last known
    assert view.mac_ips([]) == (None, [])
    macs = [
        {"mac": "00:00:00:00:00:02", "ip": None, "last_seen": t0 + timedelta(hours=1), "vlan": 1},
        {"mac": "00:00:00:00:00:03", "ip": "10.0.0.1", "last_seen": t0, "vlan": None},
        {"mac": "00:00:00:00:00:01", "ip": "10.0.0.2", "last_seen": t0, "vlan": 2},
    ]
    assert view.first_mac(macs)["mac"] == "00:00:00:00:00:01"
    assert view.first_mac([]) is None
    assert [m["mac"] for m in sorted(macs, key=view.mac_sort_key)] == [
        "00:00:00:00:00:03",
        "00:00:00:00:00:02",
        "00:00:00:00:00:01",
    ]


# ------------------------------------------------------------------ health
def _s(id_, type_, name, status="up", value=None, unit="", cfg_unit=None):  # type: ignore[no-untyped-def]
    return {
        "id": id_,
        "type": type_,
        "name": name,
        "config": {"unit": cfg_unit} if cfg_unit is not None else {},
        "status": status,
        "value": value,
        "unit": unit,
    }


@pytest.mark.parametrize(
    ("sensor", "cat"),
    [
        (_s(1, "snmp_custom", "PoE 1 usage"), "poe"),
        (_s(1, "snmp_custom", "PoE 1 power"), "poe"),
        (_s(1, "snmp_cpu", "Anything"), "cpu"),
        (_s(1, "snmp_custom", "CPU Board"), "cpu"),
        (_s(1, "snmp_custom", "מעבד ראשי"), "cpu"),
        (_s(1, "snmp_memory", "x"), "memory"),
        (_s(1, "snmp_custom", "Memory 1"), "memory"),
        (_s(1, "snmp_custom", "זיכרון"), "memory"),
        (_s(1, "snmp_custom", "Over-temperature Sensor"), "temperature"),
        (_s(1, "snmp_custom", "טמפרטורה"), "temperature"),
        (_s(1, "snmp_custom", "Inlet", cfg_unit="°C"), "temperature"),
        (_s(1, "snmp_custom", "Fan Sensor"), "fan"),
        (_s(1, "snmp_custom", "Fan 2"), "fan"),
        (_s(1, "snmp_custom", "System cooling"), "fan"),
        (_s(1, "snmp_custom", "מאוורר 1"), "fan"),
        (_s(1, "snmp_custom", "Power Supply Sensor"), "psu"),
        (_s(1, "snmp_custom", "PSU 1"), "psu"),
        (_s(1, "snmp_custom", "ספק כוח 1"), "psu"),
        (_s(1, "ping", "CPU"), None),
        (_s(1, "snmp_custom", "Sessions"), None),
        (_s(1, "snmp_custom", "Fancy"), None),
        # temperature before cpu / memory (QA #3)
        (_s(1, "snmp_custom", "Temperature CPU"), "temperature"),  # Check Point
        (_s(1, "snmp_custom", "CPU temperature"), "temperature"),  # MikroTik
        (_s(1, "snmp_custom", "Memory module", cfg_unit="°C"), "temperature"),
        (_s(1, "snmp_custom", "CPU Board", cfg_unit=""), "cpu"),  # HPE status enum
    ],
)
def test_categorize(sensor: dict, cat: str | None) -> None:
    assert health.categorize(sensor) == cat


def test_health_items() -> None:
    items = health.health_items(
        [
            _s(5, "snmp_custom", "Power Supply Sensor", "down", 2, ""),
            _s(4, "snmp_custom", "Fan Sensor", "up", 4, ""),
            _s(3, "snmp_custom", "Over-temperature Sensor", "up", 4, ""),
            _s(2, "snmp_custom", "Memory 1", "up", 39.4, "%"),
            _s(1, "snmp_custom", "CPU", "up", 7, "%"),
            _s(6, "snmp_custom", "Temperature Board", "warning", 70, "°C"),
            _s(7, "snmp_custom", "Temperature Inlet", "warning", 75, "°C"),
            _s(8, "snmp_custom", "Fan 2", "paused", None, ""),
            _s(9, "snmp_custom", "PoE 1 usage", "paused", 12, "%"),
        ]
    )
    by = {i["category"]: i for i in items}
    assert [i["category"] for i in items] == ["cpu", "memory", "temperature", "poe", "psu", "fan"]
    assert by["cpu"] == {
        "category": "cpu",
        "status": "up",
        "value": 7.0,
        "unit": "%",
        "text": "",
        "sensor_ids": [1],
        "primary_sensor_id": 1,
    }
    t = by["temperature"]
    assert (t["status"], t["value"], t["unit"], t["text"]) == ("warning", 75.0, "°C", "")
    assert t["sensor_ids"] == [3, 6, 7] and t["primary_sensor_id"] == 7  # tie -> higher value
    assert by["poe"]["status"] == "paused" and by["poe"]["value"] == 12.0
    assert (by["psu"]["text"], by["psu"]["status"], by["psu"]["value"]) == ("0/1", "down", None)
    assert (by["fan"]["text"], by["fan"]["status"]) == ("1/2", "up")
    # temperature without a value -> text
    only = health.health_items([_s(1, "snmp_custom", "Over-temperature Sensor", "up", 4, "")])
    assert only[0]["text"] == "1/1" and only[0]["value"] is None
    assert health.health_items([]) == []


# ------------------------------------------------------------------ OUI / search
def test_oui() -> None:
    assert oui.vendor("00:00:0c:00:00:03").startswith("Cisco")
    assert oui.vendor("00-50-56-01-00-01") == "VMware, Inc."
    assert oui.vendor("da:a1:19:00:00:04") is None  # locally administered
    assert oui.vendor("02:00:00:00:09:01") is None
    assert oui.vendor(None) is None and oui.vendor("zz") is None
    assert len(oui.table()) > 30000


def test_oui_file_format() -> None:
    import gzip

    raw = oui.DATA_FILE.read_bytes()
    assert len(raw) < 1_000_000 and raw[4:8] == b"\x00\x00\x00\x00"  # gzip mtime 0
    lines = gzip.decompress(raw).decode("utf-8").splitlines()
    assert lines[0] == "# source: https://standards-oui.ieee.org/oui/oui.csv"
    assert lines[1].startswith("# retrieved: ") and lines[2] == f"# entries: {len(lines) - 3}"
    body = lines[3:]
    assert body == sorted(body) and all(len(x.split("\t")[0]) == 6 for x in body)
    assert all(len(x.split("\t", 1)[1]) <= 64 for x in body)


def test_update_oui_script(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import gzip
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "update_oui.py"
    spec = importlib.util.spec_from_file_location("update_oui", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    csv_text = (
        "Registry,Assignment,Organization Name,Organization Address\n"
        'MA-L,00000C,"Cisco  Systems,   Inc",x\nMA-M,AABBCCD,Other,y\n'
        f"MA-L,123456,{'A' * 80},z\nMA-L,00000C,Dup,z\n"
    )
    entries = mod.parse_csv(csv_text)
    assert entries == {"00000C": "Cisco Systems, Inc", "123456": "A" * 64}
    a = mod.render(entries, "2026-11-05")
    assert a == mod.render(entries, "2026-11-05")  # deterministic
    text = gzip.decompress(a).decode()
    assert text.splitlines()[2] == "# entries: 2"


@pytest.mark.parametrize(
    ("q", "ip", "expected"),
    [
        ("00:50:56:01:00:07", None, {"mac": "00:50:56:01:00:07", "mac_prefix": None, "ip": None}),
        ("00-50-56-01-00-07", None, {"mac": "00:50:56:01:00:07", "mac_prefix": None, "ip": None}),
        ("0050.5601.0007", None, {"mac": "00:50:56:01:00:07", "mac_prefix": None, "ip": None}),
        ("005056010007", None, {"mac": "00:50:56:01:00:07", "mac_prefix": None, "ip": None}),
        ("00:50:56", None, {"mac": None, "mac_prefix": "005056", "ip": None}),
        ("00505601AB", None, {"mac": None, "mac_prefix": "00505601ab", "ip": None}),
        (None, "10.0.0.12", {"mac": None, "mac_prefix": None, "ip": "10.0.0.12"}),
    ],
)
def test_search_normalisation(q: str | None, ip: str | None, expected: dict) -> None:
    assert normalize_query(q, ip) == expected


@pytest.mark.parametrize(
    ("q", "ip"),
    [
        ("zz", None),
        ("00:50", None),
        ("0050560100070", None),
        (None, None),
        ("005056", "10.0.0.1"),
        (None, "10.0.0"),
        (None, "fe80::1"),
        ("", ""),
    ],
)
def test_search_invalid(q: str | None, ip: str | None) -> None:
    with pytest.raises(ApiError) as e:
        normalize_query(q, ip)
    assert e.value.status == 422


# ------------------------------------------------------------------ eligibility
def test_eligibility() -> None:
    assert effective_class({"device_class": "router"}, "switch") == "router"
    assert effective_class(None, "switch") == "switch" and effective_class(None, None) == "unknown"
    assert ports_effective("auto", True, True, "switch") is True
    assert ports_effective("auto", True, False, "switch") is False
    assert ports_effective("auto", True, True, "router") is False
    assert ports_effective("on", True, False, "unknown") is True
    assert ports_effective("on", False, True, "switch") is False
    assert ports_effective("off", True, True, "switch") is False
    assert arp_effective("auto", True, True, "router") is True
    assert arp_effective("auto", True, True, "switch") is False


# ------------------------------------------------------------------ snmp_walk_count / percent_base
def test_walk_count() -> None:
    assert count_rows([1, 1, 2, b"1", 3, True], [1, 3]) == (6, 3, 3)
    assert count_rows([1, 2], None) == (2, 2, 0)
    assert count_rows([], [1]) == (0, 0, 0)
    cfg = validate_config("snmp_walk_count", {"oid": ".1.3.6.1.4.1.9.1", "count_values": [3]})
    assert cfg == {"oid": "1.3.6.1.4.1.9.1", "count_values": [3], "label": "", "max_rows": 5000}
    for bad in (
        {},
        {"oid": "abc"},
        {"oid": "1.3.6", "count_values": []},
        {"oid": "1.3.6", "count_values": ["x"]},
        {"oid": "1.3.6", "max_rows": 0},
        {"oid": "1.3.6", "max_rows": 10001},
        {"oid": "1.3.6", "label": "x" * 65},
    ):
        with pytest.raises(ApiError):
            validate_config("snmp_walk_count", bad)
    assert identity("snmp_walk_count", {"oid": ".1.3", "count_values": [3, 1]}) == (
        "snmp_walk_count",
        "1.3",
        (1, 3),
    )
    assert identity("snmp_walk_count", {"oid": "1.3", "count_values": None})[2] is None


def test_percent_base_validation() -> None:
    cfg = validate_config(
        "snmp_custom", {"oid": "1.3.6.1", "percent_of_oid": "1.3.6.2", "percent_base": "sum"}
    )
    assert cfg["percent_base"] == "sum"
    assert "percent_base" not in validate_config("snmp_custom", {"oid": "1.3.6.1"})
    with pytest.raises(ApiError) as e:
        validate_config("snmp_custom", {"oid": "1.3.6.1", "percent_base": "sum"})
    assert e.value.details[0]["loc"] == ["body", "config", "percent_base"]
    with pytest.raises(ApiError):
        validate_config("snmp_custom", {"oid": "1.3.6.1", "percent_base": "half"})


# ------------------------------------------------------------------ template `when` / matching
def test_when_passes() -> None:
    f = [{"oid": "1.3.6.1.4.1.2620.1.5.2.0", "in": [1], "not_in": None}]
    assert when_passes(f, {"1.3.6.1.4.1.2620.1.5.2.0": 1}) is None
    assert when_passes(f, {"1.3.6.1.4.1.2620.1.5.2.0": 0}) == "1.3.6.1.4.1.2620.1.5.2.0"
    assert when_passes(f, {}) == "1.3.6.1.4.1.2620.1.5.2.0"  # missing
    assert when_passes(f, {"1.3.6.1.4.1.2620.1.5.2.0": None}) is not None
    g = [{"oid": "1.2", "in": None, "not_in": [5]}, {"oid": ".1.3", "in": None, "not_in": None}]
    assert when_passes(g, {"1.2": 4, "1.3": 7}) is None
    assert when_passes(g, {"1.2": 5, "1.3": 7}) == "1.2"
    assert when_passes(g, {"1.2": 4, "1.3": b"text"}) == "1.3"  # not an integer
    assert when_passes([], {}) is None


def test_rule_matching_2b2() -> None:
    assert oid_has_prefix("1.3.6.1.4.1.674.10892.5", "1.3.6.1.4.1.674.10892.5")
    assert oid_has_prefix(".1.3.6.1.4.1.674.10892.5.1", "1.3.6.1.4.1.674.10892.5")
    assert not oid_has_prefix("1.3.6.1.4.1.674.10892.50", "1.3.6.1.4.1.674.10892.5")
    assert not oid_has_prefix(None, "1.3")
    base = {
        "device_classes": None,
        "vendors": None,
        "requires_snmp": True,
        "any_open_ports": None,
        "enterprises": None,
    }
    r = {**base, "sys_object_id_prefixes": ["1.3.6.1.4.1.674.10892.5"], "sys_descr_regex": None}
    kw = {"device_class": "server", "vendor": "dell", "snmp_ok": True, "open_ports": []}
    assert rule_matches(r, **kw, sys_object_id="1.3.6.1.4.1.674.10892.5")
    assert not rule_matches(r, **kw, sys_object_id="1.3.6.1.4.1.674.10892.2")
    r = {
        **base,
        "sys_object_id_prefixes": None,
        "sys_descr_regex": r"Integrated Lights-Out|\biLO ?\d",
    }
    assert rule_matches(r, **kw, sys_descr="HP ETHERNET MULTI-ENVIRONMENT, iLO 5")
    assert rule_matches(r, **kw, sys_descr="integrated lights-out 4")
    assert not rule_matches(r, **kw, sys_descr="Linux")
    assert not rule_matches(r, **kw, sys_descr=None)


@pytest.mark.parametrize(
    ("oid", "descr", "cls", "vendor", "expected"),
    [
        (
            "1.3.6.1.4.1.2620.1.6.123.1.49",
            "Linux gw-hq 3.10",
            "router",
            "checkpoint",
            "checkpoint_gaia",
        ),
        ("1.3.6.1.4.1.12356.101.1.1", "", "router", "fortinet", "fortinet_fortigate"),
        ("1.3.6.1.4.1.9.1.1208", "Cisco IOS", "switch", "cisco", "cisco_ios"),
        ("1.3.6.1.4.1.25461.2.3.1", "", "router", "paloalto", "paloalto_panos"),
        ("1.3.6.1.4.1.14988.1", "RouterOS", "router", "mikrotik", "mikrotik_routeros"),
        ("1.3.6.1.4.1.318.1.3.27", "APC UPS", "unknown", "apc", "apc_ups"),
        ("1.3.6.1.4.1.232.9.4.10", "iLO 5", "server", "hp", "hpe_ilo"),
        ("1.3.6.1.4.1.8072.3.2.10", "HPE Integrated Lights-Out 4", "server", "linux", "hpe_ilo"),
        ("1.3.6.1.4.1.674.10892.5", "Linux idrac", "server", "dell", "dell_idrac"),
        ("1.3.6.1.4.1.674.10892.2", "PowerEdge iDRAC", "server", "dell", "dell_idrac"),
        ("1.3.6.1.4.1.674.10892.2", "PowerEdge", "server", "dell", "nas_server"),
        (
            "1.3.6.1.4.1.14823.1.2.111",
            "ArubaOS (MODEL: 515)",
            "access_point",
            "aruba",
            "aruba_wifi",
        ),
        ("1.3.6.1.4.1.47196.4.1.1.1.200", "Aruba Swch", "switch", "aruba", "aruba_cx"),
        ("1.3.6.1.4.1.8072.3.2.10", "Linux", "router", "checkpoint", "checkpoint_gaia"),
    ],
)
def test_vendor_template_suggestion(
    oid: str, descr: str, cls: str, vendor: str, expected: str
) -> None:
    got = suggest(
        device_class=cls,
        vendor=vendor,
        snmp_ok=True,
        open_ports=[],
        sys_object_id=oid,
        sys_descr=descr,
    )
    assert got == expected


def test_classification_9a_and_f23a() -> None:
    assert classify_rule(
        "1.3.6.1.4.1.2620.1.6.123.1.49", "Linux gw", "gw", set(), False, [], True
    ) == (
        "router",
        "checkpoint",
        9,
    )
    # the net-snmp Linux OID with the svn subtree probe
    c1 = classify_host(
        "1.3.6.1.4.1.8072.3.2.10", "Linux gw2", "gw2", {"checkpoint"}, False, [], True
    )
    assert (c1.device_class, c1.vendor, c1.confidence, c1.classified_by) == (
        "router",
        "checkpoint",
        "high",
        "snmp",
    )
    # without the probe it stays a Linux server (rule 11)
    c2 = classify_host("1.3.6.1.4.1.8072.3.2.10", "Linux gw2", "gw2", set(), False, [], True)
    assert c2.device_class == "server"
    # enterprise 232 -> server / hp (rule 10)
    assert classify_rule("1.3.6.1.4.1.232.9.4.10", "iLO", "x", set(), False, [], True)[:2] == (
        "server",
        "hp",
    )
    fp = {"http": [{"title": "Gaia Portal", "markers": []}]}
    m = classify_fingerprint(fp)
    assert m is not None and (m.device_class, m.vendor, m.confidence, m.rule) == (
        "router",
        "checkpoint",
        "medium",
        "F23a",
    )
    m = classify_fingerprint({"http": [{"title": "Login", "markers": ["check point"]}]})
    assert m is not None and m.rule == "F23a"
    assert classify_fingerprint({"http": [{"title": "Gaiaxyz", "markers": []}]}) is None
    assert find_markers("Check Point Gaia R81") == ["check point", "gaia"]
