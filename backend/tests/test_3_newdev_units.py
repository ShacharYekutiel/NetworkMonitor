"""[3] Pure new-device units (data-model "Pure rules and vectors")."""

from __future__ import annotations

import pytest

from netmon.newdev import detector as d


def test_mac_and_prefix_parsing() -> None:
    assert d.normalize_mac("00-1B-21-AA-00-01") == "00:1b:21:aa:00:01"
    assert d.normalize_mac("001b.21aa.0001") == "00:1b:21:aa:00:01"
    assert d.normalize_mac("001b21aa0001") == "00:1b:21:aa:00:01"
    assert d.normalize_mac("00:1b:21:aa:00") is None
    assert d.normalize_mac("zz:1b:21:aa:00:01") is None
    assert d.parse_prefix("3C-A8-2A") == ("3c:a8:2a", 24)
    assert d.parse_prefix("001b21a") == ("00:1b:21:a", 28)
    assert d.parse_prefix("00:1b:21:aa:b") == ("00:1b:21:aa:b", 36)
    assert d.parse_prefix("00:1b:21:aa:bb:cc") == ("00:1b:21:aa:bb:cc", 48)
    assert d.parse_prefix("00:1b") is None
    assert d.parse_prefix("00:1b:21:a:b") is None  # 8 digits
    assert d.parse_prefix("gg:gg:gg") is None
    assert d.prefix_matches("00:1b:21:a5:00:01", "00:1b:21:a", 28)
    assert not d.prefix_matches("00:1b:21:b5:00:01", "00:1b:21:a", 28)


P = {"kind": "physical", "lag_if_index": None, "mode": "access", "if_index": 5}


@pytest.mark.parametrize(
    ("port", "uplink", "excluded", "want"),
    [
        (P, False, [], True),  # physical access, 1 MAC
        (P, True, [], False),  # uplink by mac_count 40 > 25 / LLDP neighbour switch
        ({**P, "lag_if_index": 100}, False, [], False),  # Trk1 member
        ({**P, "mode": "trunk"}, False, [], False),
        ({**P, "mode": None}, False, [], True),  # no VLAN data
        ({**P, "kind": "mgmt"}, False, [], False),
        (P, False, [5], False),  # excluded ifIndex
    ],
)
def test_is_access_port(port: dict, uplink: bool, excluded: list, want: bool) -> None:
    assert d.is_access_port(port, is_uplink=uplink, excluded=excluded) is want


def test_uplink_rule_vectors() -> None:
    from netmon.ports.view import uplink

    assert uplink([], 40, 25) == (True, "mac_count")
    assert uplink([{"kind": "device", "device_class": "switch"}], 1, 25) == (True, "neighbor")
    assert uplink([], 1, 25) == (False, None)


def test_classify_vectors() -> None:
    allow = [("3c:a8:2a", 24)]
    ctx = d.ClassifyCtx(first_read=False, learning=False, ignore_random=True)
    assert d.classify_mac("02:00:00:aa:00:02", set(), allow, ctx) == d.SKIP_RANDOM
    assert d.classify_mac("01:00:5e:00:00:01", set(), allow, ctx) == d.SKIP_RANDOM
    assert d.classify_mac("00:1b:21:aa:00:01", {"00:1b:21:aa:00:01"}, allow, ctx) == (
        d.UPDATE_KNOWN
    )
    first = d.ClassifyCtx(first_read=True, learning=False)
    assert d.classify_mac("00:1b:21:aa:00:01", set(), allow, first) == d.BASELINE
    learning = d.ClassifyCtx(first_read=False, learning=True)
    assert d.classify_mac("00:1b:21:aa:00:01", set(), allow, learning) == d.BASELINE
    assert d.classify_mac("3c:a8:2a:00:00:01", set(), allow, ctx) == d.ALLOWED
    assert d.classify_mac("00:1b:21:aa:00:01", set(), allow, ctx) == d.NEW
    keep = d.ClassifyCtx(first_read=False, learning=False, ignore_random=False)
    assert d.classify_mac("02:00:00:aa:00:02", set(), allow, keep) == d.NEW


def test_classify_sightings_and_flood() -> None:
    ctx = d.ClassifyCtx(first_read=False, learning=False)
    sights = [d.Sighting(f"00:1b:21:aa:00:{i:02x}", 1 + i % 3, 20) for i in range(25)]
    sights.append(d.Sighting("00:1b:21:aa:00:00", 9, 30))  # duplicate MAC: first wins
    out = d.classify_sightings(sights, set(), [], ctx)
    assert len(out) == 25 and all(dec == d.NEW for _, dec in out)
    assert out[0][0].if_index == 1
    shown, more = d.flood_split([s for s, _ in out])
    assert len(shown) == 20 and more == 5
    assert d.summary_message(5, "sw-floor2") == "+5 more new devices on sw-floor2"


def test_message() -> None:
    assert d.device_message(
        "00:11:22:33:44:55", "Hewlett Packard", "sw-floor2", "1/0/17", 20, "10.0.20.57",
        "pc-1234.mnidom.net",
    ) == (
        "New device 00:11:22:33:44:55 (Hewlett Packard) on sw-floor2 port 1/0/17, VLAN 20, "
        "10.0.20.57 pc-1234.mnidom.net"
    )  # fmt: skip
    assert d.device_message("00:11:22:33:44:55", None, "sw", None, None, None, None) == (
        "New device 00:11:22:33:44:55 on sw"
    )
