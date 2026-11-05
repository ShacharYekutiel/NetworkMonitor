"""[2b-1] Pure units: maintenance occurrences (time zones, DST), dependencies, BFS suggestions,
fingerprint rules F1-F29, model extraction, LLDP parsers, snmp_custom enum."""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest

from netmon.api.errors import ApiError
from netmon.discovery import fingerprint as fpm
from netmon.discovery.classify import classify_fingerprint, classify_host, extract_model
from netmon.poller.sensor_types import validate_config
from netmon.poller.types import CheckResult, evaluate
from netmon.services import dependencies as deps
from netmon.services import maintenance as m
from netmon.services.status import Inventory
from netmon.templates.builtin import suggest
from netmon.topology import collect
from netmon.topology.suggest import pick_root, suggest_dependencies


def U(*a: int) -> datetime:  # noqa: N802
    return datetime(*a, tzinfo=UTC)


def weekly(days: list[int], hhmm: str, minutes: int, **kw: Any) -> dict[str, Any]:
    hh, mm = hhmm.split(":")
    return {
        "kind": "weekly",
        "weekdays": days,
        "start_time": time(int(hh), int(mm)),
        "duration_min": minutes,
        "enabled": True,
        "scope": "global",
        **kw,
    }


# ------------------------------------------------------------------ maintenance
def test_weekly_occurrences_plain_and_across_midnight() -> None:
    w = weekly([7], "02:00", 90)
    occ = m.occurrences(w, "Europe/Berlin", U(2026, 11, 1), U(2026, 11, 16))
    # Sundays 2026-11-01/08/15, CET (+1)
    assert occ == [
        (U(2026, 11, 1, 1, 0), U(2026, 11, 1, 2, 30)),
        (U(2026, 11, 8, 1, 0), U(2026, 11, 8, 2, 30)),
        (U(2026, 11, 15, 1, 0), U(2026, 11, 15, 2, 30)),
    ]
    sat = weekly([6], "23:00", 120)  # Saturday 23:00 -> Sunday 01:00
    assert m.covers(sat, "UTC", U(2026, 11, 8, 0, 30))
    assert not m.covers(sat, "UTC", U(2026, 11, 8, 1, 0))  # end is exclusive
    assert m.covers(sat, "UTC", U(2026, 11, 7, 23, 0))  # start inclusive
    assert m.covering(sat, "UTC", U(2026, 11, 8, 0, 30)) == (U(2026, 11, 7, 23), U(2026, 11, 8, 1))
    # overlapping occurrences merge: the covering one with the latest end wins
    daily = weekly([1, 2, 3, 4, 5, 6, 7], "22:00", 1440 + 120)
    assert m.covering(daily, "UTC", U(2026, 11, 8, 23)) == (U(2026, 11, 8, 22), U(2026, 11, 10, 0))
    assert not m.covers({**sat, "enabled": False}, "UTC", U(2026, 11, 8, 0, 30))


def test_weekly_dst_europe_berlin() -> None:
    w = weekly([7], "02:30", 60)
    # 2026-03-29: 02:00 -> 03:00 (gap). 02:30 does not exist -> lands after the gap (03:30 CEST)
    [occ] = m.occurrences(w, "Europe/Berlin", U(2026, 3, 28), U(2026, 3, 30))
    assert occ == (U(2026, 3, 29, 1, 30), U(2026, 3, 29, 2, 30))
    # 2026-10-25: 03:00 -> 02:00 (repeat). fold=0 -> the first 02:30 (CEST, +2)
    [occ] = m.occurrences(w, "Europe/Berlin", U(2026, 10, 24), U(2026, 10, 26))
    assert occ == (U(2026, 10, 25, 0, 30), U(2026, 10, 25, 1, 30))
    # the duration is real minutes across the change: 01:30 CET + 120 min = 04:30 CEST
    w2 = weekly([7], "01:30", 120)
    [occ] = m.occurrences(w2, "Europe/Berlin", U(2026, 3, 28), U(2026, 3, 30))
    assert occ == (U(2026, 3, 29, 0, 30), U(2026, 3, 29, 2, 30))
    # same wall-clock time, different UTC instants before/after the change
    w3 = weekly([6], "12:00", 10)
    a = m.occurrences(w3, "Europe/Berlin", U(2026, 3, 21), U(2026, 3, 22))[0][0]
    b = m.occurrences(w3, "Europe/Berlin", U(2026, 4, 4), U(2026, 4, 5))[0][0]
    assert (a.hour, b.hour) == (11, 10)


def test_weekly_dst_asia_jerusalem() -> None:
    # Israel 2026: DST starts Fri 2026-03-27 02:00 (-> 03:00), ends Sun 2026-10-25 02:00
    w = weekly([5], "02:15", 30)
    [occ] = m.occurrences(w, "Asia/Jerusalem", U(2026, 3, 26), U(2026, 3, 28))
    assert occ == (U(2026, 3, 27, 0, 15), U(2026, 3, 27, 0, 45))  # 03:15 IDT
    w = weekly([5], "09:00", 60)
    before = m.occurrences(w, "Asia/Jerusalem", U(2026, 3, 20), U(2026, 3, 21))[0][0]
    after = m.occurrences(w, "Asia/Jerusalem", U(2026, 3, 27), U(2026, 3, 28))[0][0]
    assert before == U(2026, 3, 20, 7) and after == U(2026, 3, 27, 6)
    w = weekly([7], "01:30", 60)  # 2026-10-25 01:30 occurs twice -> the first (IDT, +3)
    [occ] = m.occurrences(w, "Asia/Jerusalem", U(2026, 10, 24), U(2026, 10, 26))
    assert occ == (U(2026, 10, 24, 22, 30), U(2026, 10, 24, 23, 30))


def test_once_and_next() -> None:
    w = {
        "kind": "once",
        "enabled": True,
        "starts_at": U(2026, 11, 10, 22),
        "ends_at": U(2026, 11, 11, 2),
    }
    assert m.current_or_next(w, "UTC", U(2026, 11, 1)) == (U(2026, 11, 10, 22), U(2026, 11, 11, 2))
    assert m.covers(w, "UTC", U(2026, 11, 11, 1, 59))
    assert m.current_or_next(w, "UTC", U(2026, 11, 12)) is None  # past
    wk = weekly([1], "08:00", 30)
    assert m.current_or_next(wk, "UTC", U(2026, 11, 5)) == (
        U(2026, 11, 9, 8),
        U(2026, 11, 9, 8, 30),
    )
    bounds = m.boundaries([w, wk], "UTC", U(2026, 11, 5))
    assert bounds[0] == U(2026, 11, 9, 8)
    assert m.describe_weekly([7, 1], time(2, 0), 90) == "Every Mon,Sun 02:00, 90 min"


def test_scope_and_timezones(monkeypatch: pytest.MonkeyPatch) -> None:
    assert m.window_covers_device({"scope": "global"}, 5, [])
    assert m.window_covers_device({"scope": "device", "device_id": 5}, 5, [])
    assert not m.window_covers_device({"scope": "device", "device_id": 6}, 5, [])
    assert m.window_covers_device({"scope": "group", "group_id": 1}, 5, [3, 2, 1])  # ancestor
    assert not m.window_covers_device({"scope": "group", "group_id": 9}, 5, [3, 2, 1])
    assert m.valid_timezone("Asia/Jerusalem") and m.valid_timezone("UTC")
    assert not m.valid_timezone("Mars/Olympus") and not m.valid_timezone("")
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert m.default_timezone() == "Europe/Berlin"
    monkeypatch.setenv("TZ", "bogus/zone")
    assert m.default_timezone() == "UTC"


# ------------------------------------------------------------------ dependencies
def inv_of(
    groups: dict[int, dict[str, Any]],
    devices: dict[int, dict[str, Any]],
    sensors: dict[int, dict[str, Any]] | None = None,
) -> Inventory:
    inv = Inventory()
    for gid, g in groups.items():
        inv.groups[gid] = {
            "parent_id": None,
            "name": f"g{gid}",
            "snmp_credentials": None,
            "sort_order": 0,
            "dependency_mode": "inherit",
            "dependency_device_id": None,
            "dependency_sensor_id": None,
            **g,
        }
    for did, d in devices.items():
        inv.devices[did] = {
            "group_id": 1,
            "name": f"d{did}",
            "host": "h",
            "enabled": True,
            "snmp_credentials": None,
            "dependency_mode": "inherit",
            "dependency_device_id": None,
            "dependency_sensor_id": None,
            **d,
        }
    for sid, s in (sensors or {}).items():
        inv.sensors[sid] = {
            "status": "up",
            "name": f"s{sid}",
            "type": "ping",
            "enabled": True,
            "sort_order": 0,
            **s,
        }
    inv.compute()
    return inv


def test_effective_parent_and_self_rule() -> None:
    inv = inv_of(
        {
            1: {},
            2: {"parent_id": 1, "dependency_mode": "device", "dependency_device_id": 10},
            3: {"parent_id": 2},
            4: {"parent_id": 2, "dependency_mode": "none"},
        },
        {
            10: {"group_id": 2},
            11: {"group_id": 3},
            12: {"group_id": 4},
            13: {"group_id": 3, "dependency_mode": "device", "dependency_device_id": 12},
            14: {"group_id": 3, "dependency_mode": "none"},
            15: {"group_id": 1, "dependency_mode": "device", "dependency_device_id": None},
        },
    )
    assert deps.effective_parent(inv, 10) is None  # self rule (inherited)
    e = deps.effective_parent(inv, 11)
    assert e is not None and (e.device_id, e.source, e.group_id) == (10, "group", 2)
    assert deps.effective_parent(inv, 12) is None  # group 4 = none
    e = deps.effective_parent(inv, 13)
    assert e is not None and (e.device_id, e.source) == (12, "device")
    assert deps.effective_parent(inv, 14) is None
    assert deps.effective_parent(inv, 15) is None  # parent deleted -> none
    assert deps.find_cycle(inv) is None


def test_cycles() -> None:
    inv = inv_of(
        {1: {}},
        {
            3: {"dependency_mode": "device", "dependency_device_id": 7},
            7: {"dependency_mode": "device", "dependency_device_id": 3},
        },
    )
    assert deps.find_cycle(inv) == [3, 7, 3]
    inv = inv_of({1: {}}, {5: {"dependency_mode": "device", "dependency_device_id": 5}})
    assert deps.find_cycle(inv) == [5, 5]
    # through inheritance: group 2 depends on 8, 8 depends explicitly on 9 inside group 2
    inv = inv_of(
        {1: {}, 2: {"parent_id": 1, "dependency_mode": "device", "dependency_device_id": 8}},
        {
            8: {"group_id": 1, "dependency_mode": "device", "dependency_device_id": 9},
            9: {"group_id": 2},
        },
    )
    assert deps.find_cycle(inv) == [8, 9, 8]


def test_parent_down_and_dependency_sensor() -> None:
    inv = inv_of(
        {1: {}},
        {1: {}, 2: {"dependency_mode": "device", "dependency_device_id": 1}},
        {
            10: {"device_id": 1, "type": "http", "status": "down"},
            11: {"device_id": 1, "type": "ping", "sort_order": 5, "status": "up"},
            12: {"device_id": 1, "type": "ping", "sort_order": 1, "status": "down"},
            13: {"device_id": 1, "type": "ping", "sort_order": 0, "enabled": False},
        },
    )
    assert deps.dependency_sensor(inv, 1) == 12  # enabled ping, lowest (sort_order, id)
    assert deps.dependency_sensor(inv, 1, 11) == 11  # configured
    eff = deps.effective_parent(inv, 2)
    assert eff is not None and deps.parent_down(inv, eff)
    inv.sensors[12]["status"] = "unreachable"  # transitive
    assert deps.parent_down(inv, eff)
    inv.devices[1]["enabled"] = False
    assert not deps.parent_down(inv, eff)  # a disabled parent is never down
    # whole-device mode (no ping): every non-paused sensor down/unreachable
    inv2 = inv_of(
        {1: {}},
        {1: {}, 2: {"dependency_mode": "device", "dependency_device_id": 1}},
        {
            20: {"device_id": 1, "type": "http", "status": "down"},
            21: {"device_id": 1, "type": "tcp_port", "status": "paused"},
        },
    )
    eff2 = deps.effective_parent(inv2, 2)
    assert eff2 is not None and deps.parent_down(inv2, eff2)
    inv2.sensors[21]["status"] = "up"
    assert not deps.parent_down(inv2, eff2)
    inv3 = inv_of({1: {}}, {1: {}, 2: {"dependency_mode": "device", "dependency_device_id": 1}})
    assert not deps.parent_down(inv3, deps.effective_parent(inv3, 2))  # type: ignore[arg-type]
    assert deps.unreachable_message("gw", "Timeout") == "Unreachable (parent gw down): Timeout"


def test_suggest_dependencies_bfs() -> None:
    # core 1 -- access 2, core 1 -- access 3, access 2 -- ap 4, 3 -- 4 (ring), 5 isolated pair
    edges = [(1, 2), (1, 3), (2, 4), (3, 4), (2, 6), (7, 8)]
    assert pick_root(edges) == 2  # most links (3), ties -> lowest id
    root, res = suggest_dependencies(edges, 1)
    assert root == 1
    assert res[1] == (None, 0)
    assert res[2] == (1, 1) and res[3] == (1, 1)
    assert res[4] == (2, 2)  # tie between 2 and 3 -> more links (2 has 3)
    assert res[6] == (2, 2)
    assert 7 not in res and 8 not in res
    _, res = suggest_dependencies([(1, 2), (1, 3), (2, 4), (3, 4)], 1)
    assert res[4] == (2, 2)  # equal degree -> lowest id
    assert suggest_dependencies([], None) == (None, {})


# ------------------------------------------------------------------ fingerprint rules
def fp(
    titles: list[str] | None = None,
    servers: list[str] | None = None,
    markers: list[str] | None = None,
    ssh: str | None = None,
    telnet: str | None = None,
    realm: str | None = None,
) -> dict[str, Any]:
    http = []
    for i, t in enumerate(titles or [None]):  # type: ignore[list-item]
        http.append(
            {
                "port": 80 + i,
                "scheme": "http",
                "status": 200,
                "title": t,
                "server": (servers or [None])[i] if i < len(servers or [None]) else None,
                "realm": realm,
                "markers": markers or [],
                "redirects": 0,
                "error": None,
            }
        )
    return {
        "http": http,
        "ssh": {"port": 22, "banner": ssh} if ssh is not None else None,
        "telnet": {"port": 23, "banner": telnet} if telnet is not None else None,
    }


FP_CASES = [
    ("F1", fp(["Login | OPNsense"]), ("router", "opnsense", "high")),
    ("F1m", fp(markers=["opnsense"]), ("router", "opnsense", "high")),
    ("F2", fp(["pfSense - Login"]), ("router", "pfsense", "high")),
    ("F3", fp(["RouterOS router configuration page"]), ("router", "mikrotik", "high")),
    ("F3b", fp(ssh="SSH-2.0-ROSSSH"), ("router", "mikrotik", "high")),
    ("F4", fp(["UniFi OS"]), ("router", "ubiquiti", "medium")),
    ("F5", fp(["UniFi Network"]), ("server", "ubiquiti", "medium")),
    ("F6r", fp(["EdgeOS"]), ("router", "ubiquiti", "medium")),
    ("F6s", fp(["EdgeSwitch 24"]), ("switch", "ubiquiti", "medium")),
    ("F6a", fp(markers=["airos"]), ("access_point", "ubiquiti", "medium")),
    ("F6u", fp(markers=["ubiquiti"]), ("unknown", "ubiquiti", "low")),
    ("F7", fp(["TL-SG108E"], markers=["tp-link"]), ("switch", "tp-link", "medium")),
    ("F7b", fp(["Switch"], markers=["easy smart"]), ("switch", "tp-link", "medium")),
    ("F7c", fp(["TL-SG1016DE"]), ("switch", "tp-link", "medium")),
    ("F8s", fp(["TL-SG3428 JetStream"]), ("switch", "tp-link", "medium")),
    ("F8a", fp(["Omada Controller"]), ("access_point", "tp-link", "medium")),
    ("F8a2", fp(["EAP245"]), ("access_point", "tp-link", "medium")),
    ("F8u", fp(["Login"], markers=["tp-link"]), ("unknown", "tp-link", "low")),
    ("F9", fp(ssh="SSH-2.0-Comware-7.1.064"), ("switch", "h3c", "high")),
    ("F10h3c", fp(["H3C Web"]), ("switch", "h3c", "medium")),
    ("F10hp", fp(["ProCurve Switch 2810"]), ("switch", "hp", "medium")),
    ("F10oc", fp(["HPE OfficeConnect Switch 1820"]), ("switch", "hp", "medium")),
    ("F10cx", fp(["Aruba 2930F Switch"]), ("switch", "hp", "medium")),
    ("F11", fp(["Login"], servers=["eHTTP v2.0"]), ("switch", "hp", "medium")),
    ("F12", fp(["iLO 5"]), ("server", "hp", "high")),
    ("F12b", fp(["HPE Integrated Lights-Out"]), ("server", "hp", "high")),
    ("F13", fp(["iDRAC9"]), ("server", "dell", "high")),
    ("F14", fp(["Supermicro BMC"]), ("server", "supermicro", "medium")),
    ("F14b", fp(["IPMI Web"]), ("server", None, "medium")),
    ("F15", fp(["Synology DiskStation"]), ("nas", "synology", "high")),
    ("F16", fp(["QTS Login"]), ("nas", "qnap", "medium")),
    ("F17", fp(["HP LaserJet Pro"]), ("printer", "hp", "high")),
    ("F17s", fp([None], servers=["HP HTTP Server; HP LaserJet"]), ("printer", "hp", "medium")),  # type: ignore[list-item]
    ("F18", fp(["Brother HL-L2350DW"]), ("printer", "brother", "high")),
    ("F18s", fp(["Top"], servers=["debut/1.30"]), ("printer", "brother", "medium")),
    ("F19", fp(["Remote UI"], servers=["CANON HTTP Server"]), ("printer", "canon", "medium")),
    ("F20", fp(["Command Center RX"]), ("printer", "kyocera", "medium")),
    ("F21", fp(["Web Image Monitor"]), ("printer", "ricoh", "medium")),
    ("F22", fp(markers=["epson"]), ("printer", "epson", "medium")),
    ("F22x", fp(["Xerox WorkCentre"]), ("printer", "xerox", "medium")),
    ("F22l", fp(["Lexmark MS510"]), ("printer", "lexmark", "medium")),
    ("F23", fp(["FortiGate"]), ("router", "fortinet", "medium")),
    ("F23s", fp(["x"], servers=["xxxxxxxx-xxxxx"]), ("router", "fortinet", "medium")),
    ("F24s", fp(["Cisco CBS350 Switch"]), ("switch", "cisco", "medium")),
    ("F24r", fp(ssh="SSH-2.0-Cisco-1.25"), ("router", "cisco", "medium")),
    ("F25s", fp(["NETGEAR GS308T"]), ("switch", "netgear", "medium")),
    ("F25u", fp(["NETGEAR Router"]), ("unknown", "netgear", "low")),
    ("F26s", fp(["D-Link DGS-1210-28"]), ("switch", "d-link", "medium")),
    ("F26u", fp(markers=["dlink"]), ("unknown", "d-link", "low")),
    ("F27s", fp(["Zyxel GS1900-8"]), ("switch", "zyxel", "medium")),
    ("F27u", fp(["ZyXEL NAS"]), ("unknown", "zyxel", "low")),
    (
        "F28",
        fp(["IIS Windows Server"], servers=["Microsoft-IIS/10.0"]),
        ("server", "microsoft", "low"),
    ),
    ("F29", fp(ssh="SSH-2.0-OpenSSH_8.9p1 Ubuntu-3ubuntu0.10"), ("server", "linux", "low")),
]


@pytest.mark.parametrize("rule,data,expected", FP_CASES, ids=[c[0] for c in FP_CASES])
def test_fingerprint_rules(rule: str, data: dict[str, Any], expected: tuple) -> None:
    got = classify_fingerprint(data)
    assert got is not None, rule
    assert (got.device_class, got.vendor, got.confidence) == expected
    assert got.rule == re.match(r"F\d+", rule).group(0)  # type: ignore[union-attr]


def test_fingerprint_no_match_and_host_classification() -> None:
    assert classify_fingerprint(fp(["Welcome"])) is None
    assert classify_fingerprint(None) is None
    assert classify_fingerprint(fp(ssh="SSH-2.0-OpenSSH_9.7")) is None  # no distro -> no rule
    # no SNMP, fingerprint match
    c = classify_host(None, None, None, [], False, [80], False, fp(["TL-SG108E"]))
    assert (c.device_class, c.vendor, c.model, c.confidence, c.classified_by) == (
        "switch",
        "tp-link",
        "TL-SG108E",
        "medium",
        "fingerprint",
    )
    # no SNMP, no fingerprint match -> port rules / none
    c = classify_host(None, None, None, [], False, [22], False, fp(["Welcome"]))
    assert (c.device_class, c.confidence, c.classified_by) == ("server", "low", "ports")
    c = classify_host(None, None, None, [], False, [], False, None)
    assert (c.device_class, c.vendor, c.confidence, c.classified_by) == (
        "unknown",
        None,
        "low",
        "none",
    )
    # SNMP rules 1-10 -> high; 11-12 -> medium
    c = classify_host("1.3.6.1.4.1.9.1.1208", "Cisco IOS C2960X", "sw", [], False, [], True)
    assert (c.device_class, c.vendor, c.confidence, c.classified_by) == (
        "switch",
        "cisco",
        "high",
        "snmp",
    )
    c = classify_host("1.3.6.1.4.1.8072.3.2.10", "Linux x", "x", [], False, [], True)
    assert (c.device_class, c.confidence) == ("server", "medium")
    # SNMP answered but ended unknown: the fingerprint takes over, SNMP vendor kept
    c = classify_host(
        "1.3.6.1.4.1.99999.1", "Some box", "x", [], False, [80], True, fp(["IPMI Web"])
    )
    assert (c.device_class, c.classified_by, c.confidence) == ("server", "fingerprint", "medium")
    c = classify_host("1.3.6.1.4.1.318.1", "APC UPS", "x", [], False, [80], True, fp(["Welcome"]))
    assert (c.device_class, c.vendor, c.classified_by) == ("unknown", "apc", "snmp")
    # HPE families classify as switches
    c = classify_host(
        "1.3.6.1.4.1.11.2.3.7.11.181",
        "Aruba JL256A 2930F-48G-4SFP+ Switch, revision WC.16.11.0012",
        "core",
        [],
        False,
        [],
        True,
    )
    assert (c.device_class, c.vendor, c.confidence) == ("switch", "hp", "high")
    c = classify_host(
        "1.3.6.1.4.1.25506.11.1.177",
        "HPE Comware Platform Software, Software Version 7.1.070, Release 6328",
        "acc",
        [],
        False,
        [],
        True,
    )
    assert (c.device_class, c.vendor) == ("switch", "h3c")
    c = classify_host("1.3.6.1.4.1.47196.4.1.1.1.1", "Aruba CX 6100", "cx", [], False, [], True)
    assert (c.device_class, c.vendor, c.confidence) == ("switch", "aruba", "high")


def test_template_suggestion_by_enterprise() -> None:
    def sug(oid: str | None, cls: str = "switch", vendor: str | None = "hp") -> str:
        return suggest(
            device_class=cls, vendor=vendor, snmp_ok=True, open_ports=[], sys_object_id=oid
        )

    assert sug("1.3.6.1.4.1.11.2.3.7.11.181") == "hpe_arubaos_switch"
    assert sug("1.3.6.1.4.1.25506.11.1.177", vendor="h3c") == "hpe_comware"
    assert sug("1.3.6.1.4.1.47196.4.1", vendor="aruba") == "generic_snmp"  # CX: no template
    assert sug("1.3.6.1.4.1.11.2.3.9.1", cls="printer") == "printer"  # JetDirect
    assert sug(None) == "generic_snmp"


def test_model_extraction() -> None:
    assert extract_model(fp(["TL-SG108E"])) == "TL-SG108E"
    assert extract_model(fp(["Aruba JL256A 2930F"])) == "JL256A"
    assert extract_model(fp(["HPE OfficeConnect Switch 1920S 24G"])) == "1920S"
    assert extract_model(fp(["Synology DiskStation DS920+"])) == "DS920+"
    assert extract_model(fp(["NETGEAR GS308T"])) == "GS308T"
    assert extract_model(fp(["x"], realm="DGS-1210-28")) == "DGS-1210-28"
    assert extract_model(fp(["U6-Lite"])) == "U6-Lite"
    assert extract_model(fp(["x"], telnet="TL-SG2008 login:")) == "TL-SG2008"
    assert extract_model(fp(["Welcome"])) is None


def test_fingerprint_helpers() -> None:
    assert fpm.extract_title(b"<html><TITLE> Login &amp;\n  Status </TITLE>") == "Login & Status"
    assert fpm.extract_title(b"<html>no title</html>") is None
    assert fpm.extract_title(b"<title>" + b"x" * 500 + b"</title>") == "x" * 200
    assert fpm.find_markers("Login | OPNsense", "body TP-LINK and Easy Smart") == [
        "easy smart",
        "opnsense",
        "tp-link",
    ]
    assert fpm.find_markers("hpe-switch", "") == ["hpe"]
    assert fpm.find_markers("ciscoworks", "") == []  # word boundaries
    assert fpm.extract_realm('Basic realm="DGS-1210-28"') == "DGS-1210-28"
    assert fpm.extract_realm("Bearer") is None
    raw = b"\xff\xfb\x01\xff\xfd\x03\xff\xfa\x18\x01\xff\xf0\r\nUser Access Verification\r\n"
    assert fpm.first_line(fpm.strip_telnet(raw)) == "User Access Verification"
    assert fpm.first_line(b"SSH-2.0-Comware-7.1.064\r\n") == "SSH-2.0-Comware-7.1.064"
    assert fpm.redirect_target("http://10.0.0.1:80/", "/ui/", "10.0.0.1", None) == (
        "http",
        80,
        "/ui/",
    )
    assert fpm.redirect_target("http://10.0.0.1:80/", "https://10.0.0.1/x", "10.0.0.1", None) == (
        "https",
        443,
        "/x",
    )
    assert (
        fpm.redirect_target("http://10.0.0.1:80/", "http://example.com/", "10.0.0.1", None) is None
    )
    assert (
        fpm.redirect_target("http://10.0.0.1:80/", "http://10.0.0.1:9000/", "10.0.0.1", None)
        is None
    )
    assert fpm.redirect_target("http://sw1:80/", "http://SW1/login", "10.0.0.1", "sw1") == (
        "http",
        80,
        "/login",
    )


# ------------------------------------------------------------------ LLDP parsers
def test_lldp_normalisation() -> None:
    mac = bytes.fromhex("001a1e000051")
    assert collect.normalize_id(mac, 4, "chassis") == "00:1a:1e:00:00:51"
    assert collect.normalize_id(mac, 3, "port") == "00:1a:1e:00:00:51"
    assert collect.normalize_id(mac, 7, "chassis") == "0x001a1e000051"  # not printable, not mac
    assert collect.normalize_id(b"\x01\x0a\x00\x00\x05", 5, "chassis") == "10.0.0.5"
    assert collect.normalize_id(b"\x01\x0a\x00\x00\x05", 4, "port") == "10.0.0.5"
    assert collect.normalize_id(b"GigabitEthernet1/0/1", 5, "port") == "GigabitEthernet1/0/1"
    assert collect.normalize_id(b"00-1A-1E-00-00-51", 4, "chassis") == "00:1a:1e:00:00:51"
    assert collect.normalize_id(b"\x00\x01\xfe", 1, "chassis") == "0x0001fe"
    assert collect.parse_man_addr_suffix("0.3.1.1.4.127.0.0.52") == (3, 1, "127.0.0.52")
    six = ".".join(["32", "1", "13", "184"] + ["0"] * 11 + ["1"])
    assert collect.parse_man_addr_suffix(f"5.1.2.2.16.{six}") == (1, 2, "2001:db8::1")
    assert collect.parse_man_addr_suffix("0.3.1.1.4.127.0.0") is None
    assert collect.name_keys("SW1.lan") == {"sw1.lan", "sw1"}
    assert collect.name_keys("10.0.0.1") == {"10.0.0.1"}


def test_local_port_resolution_both_families() -> None:
    # ArubaOS-Switch: port num = ifIndex, port id subtype 7 "local" = port number
    names = {1: "1", 2: "2", 3: "3", 4: "4", 53: "DEFAULT_VLAN"}
    port = collect.LocalPort(2, 7, "2", "2")
    assert collect.resolve_local_port(port, 2, names, {}) == (2, "2")
    assert collect.resolve_local_port(None, 3, names, {}) == (3, "3")  # step 3
    # Comware: lldpLocPortNum 101.. != ifIndex; subtype 5 with the interface name
    cw = {1: "GigabitEthernet1/0/1", 2: "GigabitEthernet1/0/2"}
    port = collect.LocalPort(102, 5, "GigabitEthernet1/0/2", "GigabitEthernet1/0/2 Interface")
    assert collect.resolve_local_port(port, 102, cw, {}) == (2, "GigabitEthernet1/0/2")
    port = collect.LocalPort(103, 7, "x", "GigabitEthernet1/0/1")  # by port desc (step 2)
    assert collect.resolve_local_port(port, 103, cw, {}) == (1, "GigabitEthernet1/0/1")
    port = collect.LocalPort(104, 7, "x", "y")
    assert collect.resolve_local_port(port, 104, cw, {}) == (None, "y")  # unresolved
    assert collect.resolve_remote_port(5, "GigabitEthernet1/0/1", "", cw, {}) == 1
    assert collect.resolve_remote_port(3, "00:11:22:33:44:55", "nope", cw, {}) is None


# ------------------------------------------------------------------ snmp_custom enum
VMAP = {
    "1": {"status": "warning", "text": "unknown"},
    "2": {"status": "down", "text": "bad"},
    "4": {"status": "up", "text": "good"},
}


def test_enum_config_validation() -> None:
    cfg = validate_config(
        "snmp_custom", {"oid": "1.3.6.1.4.1.11", "value_type": "enum", "value_map": VMAP}
    )
    assert cfg["value_map"] == VMAP and cfg["value_map_default"] == "warning"
    bad = [
        {"value_type": "enum"},  # value_map required
        {"value_type": "enum", "value_map": {}},
        {"value_type": "enum", "value_map": {"x": {"status": "up", "text": ""}}},
        {"value_type": "enum", "value_map": {"1": {"status": "bogus", "text": ""}}},
        {"value_type": "enum", "value_map": {"1": {"status": "up", "text": "x" * 33}}},
        {
            "value_type": "enum",
            "value_map": {str(i): {"status": "up", "text": ""} for i in range(33)},
        },
        {"value_type": "enum", "value_map": VMAP, "value_map_default": "paused"},
        {"value_type": "enum", "value_map": VMAP, "percent_of_oid": "1.3.6.1.2"},
    ]
    for extra in bad:
        with pytest.raises(ApiError):
            validate_config("snmp_custom", {"oid": "1.3.6.1.4.1.11", **extra})


def test_status_override_in_evaluate() -> None:
    now = datetime.now(UTC)
    chans = [
        (
            "value",
            "",
            {"warning_min": None, "warning_max": None, "down_min": None, "down_max": None},
        )
    ]
    ev = evaluate(
        CheckResult(True, "PSU: bad (2)", {"value": 2.0}, status_override="down"), chans, 1.0, now
    )
    assert (ev.ok, ev.status, ev.message) == (True, "down", "PSU: bad (2)")
    ev = evaluate(
        CheckResult(True, "Fan: good (4)", {"value": 4.0}, status_override="up"), chans, 1.0, now
    )
    assert ev.status == "up"
    # thresholds still apply to the value: worst of both
    chans[0][2]["down_max"] = 3.0
    ev = evaluate(
        CheckResult(True, "x: good (4)", {"value": 4.0}, status_override="warning"), chans, 1.0, now
    )
    assert ev.status == "down" and ev.message == "value 4 > 3"


async def test_enum_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from netmon.poller.checks import snmp_custom
    from netmon.poller.types import CheckContext

    values: dict[str, tuple[Any, str]] = {}

    async def fake_get(host: str, creds: dict[str, Any], oids: list[str]) -> dict[str, Any]:
        return {o: values.get(o, (None, "NoSuchObject")) for o in oids}

    monkeypatch.setattr(snmp_custom, "snmp_get_typed", fake_get)
    cfg = validate_config(
        "snmp_custom",
        {"oid": "1.3.6.1.4.1.11.1", "value_type": "enum", "value_map": VMAP, "label": "PSU"},
    )
    ctx = CheckContext(1, "snmp_custom", cfg, "h", {})
    for raw, status, msg in [
        (2, "down", "PSU: bad (2)"),
        (4, "up", "PSU: good (4)"),
        (9, "warning", "PSU: unexpected value (9)"),
    ]:
        values["1.3.6.1.4.1.11.1"] = (raw, "Integer32")
        r = await snmp_custom.run(ctx)
        assert (r.ok, r.status_override, r.message, r.values["value"]) == (True, status, msg, raw)
    ctx.config = {**cfg, "value_map_default": "down"}
    assert (await snmp_custom.run(ctx)).status_override == "down"
    values["1.3.6.1.4.1.11.1"] = (b"good", "OctetString")
    r = await snmp_custom.run(ctx)
    assert (r.ok, r.message) == (False, "Value is not an integer")
    del values["1.3.6.1.4.1.11.1"]
    assert (await snmp_custom.run(ctx)).ok is False


def test_occurrences_window_object_and_strings() -> None:
    class W:
        kind = "weekly"
        weekdays = [3]
        start_time = "07:15"
        duration_min = 15
        enabled = True

    occ = m.occurrences(W(), "UTC", U(2026, 11, 4), U(2026, 11, 5))
    assert occ == [(U(2026, 11, 4, 7, 15), U(2026, 11, 4, 7, 30))]
    assert (
        m.occurrences({"kind": "weekly", "weekdays": []}, "UTC", U(2026, 1, 1), U(2026, 2, 1)) == []
    )
    assert m.occurrences({**weekly([1], "00:00", 10)}, "UTC", U(2026, 1, 2), U(2026, 1, 1)) == []
    assert m.covering(weekly([1], "00:00", 10), "UTC", U(2026, 11, 9) + timedelta(minutes=5))
