"""[premium] Connected device kind of a switch port (pure rules) and the LLDP / CDP capability
parsers. No database."""

from __future__ import annotations

import gzip

import pytest

from netmon.ports import kind as k
from netmon.ports import oui
from netmon.topology import collect as tc


def _dev(cls: str, source: str = "lldp") -> dict:
    return {"kind": "device", "device_class": cls, "source": source, "neighbor_id": None}


def _nb(source: str = "lldp", nid: int = 9) -> dict:
    return {"kind": "neighbor", "device_class": None, "source": source, "neighbor_id": nid}


def _mac(mac: str, vendor: str | None = None) -> dict:
    return {"mac": mac, "vendor": vendor}


def derive(**kw):  # type: ignore[no-untyped-def]
    args = {
        "state": "up",
        "override": None,
        "primary": None,
        "nb_info": None,
        "lag_or_trunk": False,
        "mac_count": 0,
        "macs": [],
    }
    args.update(kw)
    return k.derive(**args)


# ------------------------------------------------------------------ capability parsers
def test_lldp_capabilities_bits() -> None:
    # bridge (2) + telephone (5): 0b0010_0100
    assert tc.lldp_capabilities(bytes([0x24, 0x00])) == ["bridge", "telephone"]
    # bridge + wlan_ap + router: 0b0011_1000
    assert tc.lldp_capabilities(bytes([0x38])) == ["bridge", "router", "wlan_ap"]
    # station only (7) and cvlan (8, second octet)
    assert tc.lldp_capabilities(bytes([0x01, 0x80])) == ["cvlan", "station"]
    assert tc.lldp_capabilities(b"") == []
    assert tc.lldp_capabilities(None) == []
    assert tc.lldp_capabilities("text") == []


def test_cdp_capabilities_mask() -> None:
    assert tc.cdp_capabilities(bytes([0, 0, 0, 0x28])) == ["bridge"]  # switch + IGMP
    assert tc.cdp_capabilities(bytes([0, 0, 0x04, 0x90])) == ["station", "telephone"]
    assert tc.cdp_capabilities(bytes([0, 0, 0, 0x29])) == ["bridge", "router"]
    assert tc.cdp_capabilities(b"") == [] and tc.cdp_capabilities(b"12345") == []


def test_parse_remotes_and_cdp_keep_capabilities() -> None:
    rem = {
        "5.0.3.1": 4,
        "5.0.3.1x": None,
        "4.0.3.1": 4,
        "9.0.3.1": b"phone-1",
        "11.0.3.1": bytes([0x24, 0x00]),  # supported: bridge + telephone
        "12.0.3.1": bytes([0x04, 0x00]),  # enabled: telephone only
        "9.0.4.1": b"pc-1",
        "11.0.4.1": bytes([0x01]),  # supported station, nothing enabled
        "12.0.4.1": bytes([0x00]),
    }
    remotes = {r.local_port: r for r in tc.parse_remotes(rem, {})}
    assert remotes[3].capabilities == ["telephone"]
    assert remotes[4].capabilities == ["station"]
    cdp = tc.parse_cdp({"6.10.1": b"SEP001122334455", "8.10.1": b"Cisco IP Phone 7841",
                        "9.10.1": bytes([0, 0, 0, 0x90])})  # fmt: skip
    assert cdp[0].capabilities == ["station", "telephone"]


# ------------------------------------------------------------------ rules and priority
def test_link_down_or_nothing_learned_is_null() -> None:
    assert derive(state="down", mac_count=1, macs=[_mac("00:11:22:33:44:55")]) == (None, None)
    assert derive(state="admin_down", primary=_dev("switch")) == (None, None)
    assert derive() == (None, None)  # up, no neighbour, no MAC


def test_override_wins_over_everything_and_survives_link_down() -> None:
    assert derive(override="printer", primary=_dev("switch")) == ("printer", "manual")
    assert derive(override="camera", state="down") == ("camera", "manual")
    assert derive(override="unknown", mac_count=1, macs=[_mac("00:50:56:00:00:01")]) == (
        "unknown",
        "manual",
    )


@pytest.mark.parametrize(
    ("cls", "kind"),
    [
        ("switch", "switch"),
        ("router", "router"),
        ("access_point", "ap"),
        ("printer", "printer"),
        ("server", "server"),
        ("nas", "server"),
        ("workstation", "computer"),
    ],
)
def test_managed_neighbor_class(cls: str, kind: str) -> None:
    assert derive(primary=_dev(cls, "cdp"), mac_count=3) == (kind, "cdp")


def test_manual_topology_link_source_is_manual() -> None:
    assert derive(primary=_dev("switch", "manual")) == ("switch", "manual")


@pytest.mark.parametrize(
    ("caps", "kind"),
    [
        (["bridge", "telephone"], "phone"),
        (["bridge", "router", "wlan_ap"], "ap"),
        (["bridge", "router"], "switch"),
        (["bridge"], "switch"),
        (["router"], "router"),
        (["station"], "computer"),
    ],
)
def test_unmanaged_neighbor_capabilities(caps: list[str], kind: str) -> None:
    got = derive(primary=_nb(), nb_info={"capabilities": caps}, mac_count=1, macs=[])
    assert got == (kind, "lldp")


def test_unmanaged_neighbor_text_fallback_and_cdp_source() -> None:
    info = {"capabilities": None, "sys_descr": "Cisco IP Phone 8851"}
    assert derive(primary=_nb("cdp"), nb_info=info) == ("phone", "cdp")
    info = {"capabilities": [], "sys_descr": "cisco AIR-AP2802I-E-K9"}
    assert derive(primary=_nb("cdp"), nb_info=info) == ("ap", "cdp")
    # nothing usable from the neighbour -> the MAC rules
    info = {"capabilities": ["other"], "sys_descr": "Linux 6.1"}
    got = derive(primary=_nb(), nb_info=info, mac_count=1, macs=[_mac("00:11:22:33:44:55")])
    assert got == ("computer", "oui")


def test_unknown_class_device_on_lag_or_trunk_is_switch() -> None:
    assert derive(primary=_dev("unknown"), lag_or_trunk=True) == ("switch", "lldp")
    # on an access port an unknown-class device says nothing: the MAC decides
    macs = [_mac("00:1b:a9:00:00:01", "Brother Industries, LTD.")]
    assert derive(primary=_dev("unknown"), mac_count=1, macs=macs) == ("printer", "oui")


def test_neighbor_beats_oui() -> None:
    macs = [_mac("00:50:56:00:00:01", "VMware, Inc.")]
    assert derive(primary=_dev("switch"), mac_count=1, macs=macs) == ("switch", "lldp")


@pytest.mark.parametrize(
    ("mac", "vendor", "kind"),
    [
        ("00:1b:a9:00:00:01", "Brother Industries, LTD.", "printer"),
        ("00:00:48:00:00:01", "Seiko Epson Corporation", "printer"),
        ("00:40:8c:00:00:01", "Axis Communications AB", "camera"),
        ("44:19:b6:00:00:01", "Hangzhou Hikvision Digital Technology Co.,Ltd.", "camera"),
        ("80:5e:c0:00:00:01", "YEALINK(XIAMEN) NETWORK TECHNOLOGY CO.,LTD.", "phone"),
        ("00:04:f2:00:00:01", "Polycom", "phone"),
        ("24:5a:4c:00:00:01", "Ubiquiti Inc", "ap"),
        ("00:50:56:00:00:01", "VMware, Inc.", "server"),
        ("00:15:5d:00:00:01", "Microsoft Corporation", "server"),  # Hyper-V by prefix
        ("00:0b:86:00:00:01", "Hewlett Packard Enterprise", "ap"),  # Aruba block by prefix
        ("3c:22:fb:00:00:01", "Apple, Inc.", "computer"),  # mixed vendor: not listed
        ("00:11:22:33:44:55", None, "computer"),  # unknown OUI
        ("02:11:22:33:44:55", None, "computer"),  # locally administered (random MAC)
    ],
)
def test_single_mac_oui(mac: str, vendor: str | None, kind: str) -> None:
    assert derive(mac_count=1, macs=[_mac(mac, vendor)]) == (kind, "oui")


def test_vendor_patterns_are_anchored() -> None:
    assert k.vendor_kind("11:22:33:00:00:01", "Ace Axis Limited") is None
    assert k.vendor_kind("11:22:33:00:00:01", "ARMITEL Co.") is None
    assert k.vendor_kind("11:22:33:00:00:01", "Brother, Brother & Sons ApS") is None


def test_several_macs() -> None:
    pc = _mac("00:11:22:33:44:55")
    phone = _mac("80:5e:c0:00:00:01", "YEALINK(XIAMEN) NETWORK TECHNOLOGY CO.,LTD.")
    vm = _mac("00:50:56:00:00:01", "VMware, Inc.")
    assert derive(mac_count=2, macs=[pc, _mac("00:11:22:33:44:66")]) == ("unknown", "oui")
    assert derive(mac_count=2, macs=[pc, phone]) == ("phone", "oui")
    assert derive(mac_count=3, macs=[pc, vm, _mac("00:50:56:00:00:02", "VMware, Inc.")]) == (
        "server",
        "oui",
    )
    # an uplink by MAC count: MACs not loaded, only the count
    assert derive(mac_count=40, macs=[]) == ("unknown", "oui")


def test_tables_are_small_and_valid() -> None:
    assert len(k.VENDOR_KINDS) + len(k.PREFIX_KINDS) < 300
    kinds = {kind for _, kind in k.VENDOR_KINDS} | set(k.PREFIX_KINDS.values())
    assert kinds <= set(k.KINDS)
    for prefix in k.PREFIX_KINDS:
        assert len(prefix) == 6 and prefix == prefix.upper()
        int(prefix, 16)


def test_vendor_patterns_match_the_bundled_oui_listing() -> None:
    """Every curated vendor pattern names at least one organisation of the bundled IEEE
    listing, and every listed prefix is in it (no typo silently matching nothing)."""
    with gzip.open(oui.DATA_FILE, "rt", encoding="utf-8") as f:
        rows = [ln.rstrip("\n").split("\t") for ln in f if ln and not ln.startswith("#")]
    names = [r[1] for r in rows if len(r) == 2]
    prefixes = {r[0].upper() for r in rows if len(r) == 2}
    for pattern, _ in k.VENDOR_KINDS:
        rx = k.re.compile("^" + pattern, k.re.I)
        assert any(rx.search(n) for n in names), pattern
    assert set(k.PREFIX_KINDS) <= prefixes
