"""[2a] Pure discovery logic: target parsing, classification, template matching, options."""

from __future__ import annotations

import pytest

from netmon.discovery.classify import classify, enterprise_of, suggest_template
from netmon.discovery.options import (
    build_credentials,
    merge_options,
    public_options,
    scan_snapshot,
)
from netmon.discovery.targets import (
    TargetError,
    TooManyHostsError,
    expand_targets,
    validate_targets,
)
from netmon.templates.builtin import TEMPLATES, get_template, list_templates


def ips(targets: list[str]) -> list[str]:
    return [h.label for h in expand_targets(targets)]


# ------------------------------------------------------------------ targets
def test_target_forms() -> None:
    assert ips(["192.168.1.0/30"]) == ["192.168.1.1", "192.168.1.2"]
    assert ips(["192.168.1.5/30"]) == ["192.168.1.5", "192.168.1.6"]  # host bits normalised
    assert ips(["10.0.0.0/31"]) == ["10.0.0.0", "10.0.0.1"]
    assert ips(["10.0.0.7/32"]) == ["10.0.0.7"]
    assert len(ips(["192.168.1.0/24"])) == 254
    assert ips(["192.168.1.10-12"]) == ["192.168.1.10", "192.168.1.11", "192.168.1.12"]
    assert ips(["10.0.0.254-10.0.1.1"]) == ["10.0.0.254", "10.0.0.255", "10.0.1.0", "10.0.1.1"]
    assert ips(["10.0.0.1"]) == ["10.0.0.1"]
    assert ips(["fd00::1"]) == ["fd00::1"]
    assert ips(["nas.lan", "NAS.lan", "printer"]) == ["nas.lan", "printer"]
    # sorted ascending, de-duplicated across entries, hostnames last
    assert ips(["nas.lan", "10.0.0.3", "10.0.0.1-3", "fd00::2", "10.0.0.2"]) == [
        "10.0.0.1",
        "10.0.0.2",
        "10.0.0.3",
        "fd00::2",
        "nas.lan",
    ]
    assert ips(["127.0.0.20-30"])[0] == "127.0.0.20" and len(ips(["127.0.0.20-30"])) == 11
    assert expand_targets(["nas.lan"])[0].address is None


@pytest.mark.parametrize(
    "bad",
    [
        "224.0.0.1",
        "239.1.2.3/24",
        "0.0.0.0",
        "0.1.2.3/16",
        "240.0.0.1",
        "255.255.255.255",
        "223.255.255.250-224.0.0.2",
        "ff02::1",
        "::",
        "fd00::/64",
        "10.0.0.0/7",
        "10.0.0.5-3",
        "10.0.0.5-256",
        "10.0.0.9-10.0.0.1",
        "300.1.1.1",
        "1.2.3",
        "",
        "   ",
        "bad_host!",
        "-x.lan",
        "a" * 254,
        "10.0.0.0/33",
    ],
)
def test_rejected_targets(bad: str) -> None:
    with pytest.raises(TargetError) as ei:
        validate_targets(["10.0.0.1", bad])
    assert ei.value.index == 1


def test_target_limits() -> None:
    assert validate_targets(["10.0.0.0/20"]) == 4094  # a /20 fits
    with pytest.raises(TooManyHostsError) as ei:
        validate_targets(["10.0.0.0/16"])
    assert ei.value.count == 65534 and ei.value.max == 4096
    with pytest.raises(TooManyHostsError):
        validate_targets(["10.0.0.0/20", "10.1.0.1-10"])
    # overlaps are counted once
    assert validate_targets(["10.0.0.0/20", "10.0.0.1-10", "10.0.0.5"]) == 4094
    with pytest.raises(TargetError):
        validate_targets([f"10.0.0.{i}" for i in range(65)])
    with pytest.raises(TargetError):
        validate_targets([])
    assert validate_targets(["127.0.0.1", "localhost"]) == 2  # loopback allowed


# ------------------------------------------------------------------ classification
OPN = (
    "FreeBSD OPNsense.localdomain 14.1-RELEASE-p5 FreeBSD 14.1-RELEASE-p5 "
    "stable/24.7-n267758-abc amd64"
)
CASES = [
    # (sys_object_id, sys_descr, sys_name, probes, pf, ports, snmp_ok) -> (class, vendor, template)
    (
        ("1.3.6.1.4.1.8072.3.2.8", OPN, "OPNsense.localdomain", set(), True, [], True),
        ("router", "opnsense", "opnsense"),
    ),
    (
        (
            "1.3.6.1.4.1.8072.3.2.8",
            "FreeBSD fw 14.1-RELEASE stable/24.7-n1 amd64",
            "fw",
            set(),
            False,
            [],
            True,
        ),
        ("router", "opnsense", "opnsense"),
    ),
    (
        ("1.3.6.1.4.1.8072.3.2.8", "FreeBSD fw 14.1-RELEASE amd64", "fw", set(), True, [], True),
        ("router", "freebsd", "opnsense"),
    ),
    (
        ("1.3.6.1.4.1.8072.3.2.8", "FreeBSD box 14.1-RELEASE amd64", "box", set(), False, [], True),
        ("server", "freebsd", "nas_server"),
    ),
    (
        (
            "1.3.6.1.4.1.12325.1.1.2.1.1",
            "pfSense fw.home 2.7.2-RELEASE",
            "fw",
            set(),
            False,
            [],
            True,
        ),
        ("router", "pfsense", "opnsense"),
    ),
    (
        ("1.3.6.1.4.1.12325.1.1.2.1.1", "bsnmpd", "x", set(), False, [], True),
        ("router", "pfsense", "opnsense"),
    ),
    (
        (
            "1.3.6.1.4.1.8072.3.2.10",
            "Linux U6-Lite 4.4.153 #1 SMP mips",
            "U6-Lite-Office",
            {"unifi_vap"},
            False,
            [],
            True,
        ),
        ("access_point", "ubiquiti", "unifi_ap"),
    ),
    (
        ("1.3.6.1.4.1.8072.3.2.10", "Linux ap 4.4.153 mips", "ap", {"unifi_vap"}, False, [], True),
        ("access_point", "ubiquiti", "unifi_ap"),
    ),
    (
        ("1.3.6.1.4.1.8072.3.2.10", "Linux UAP-AC-Pro 3.3.8", "ap", set(), False, [], True),
        ("access_point", "ubiquiti", "unifi_ap"),
    ),
    (
        ("1.3.6.1.4.1.41112.1.6", "UniFi access point", "ap", set(), False, [], True),
        ("access_point", "ubiquiti", "unifi_ap"),
    ),
    (
        ("1.3.6.1.4.1.41112.1.4", "USW-24-PoE, 6.5.59", "sw", set(), False, [], True),
        ("switch", "ubiquiti", "generic_snmp"),
    ),
    (
        ("1.3.6.1.4.1.41112.1.4", "EdgeOS v2.0.9", "er", set(), False, [], True),
        ("router", "ubiquiti", "generic_snmp"),
    ),
    (
        ("1.3.6.1.4.1.41112.1.9", "something", "x", set(), False, [], True),
        ("unknown", "ubiquiti", "generic_snmp"),
    ),
    (
        (
            "1.3.6.1.4.1.2435.2.3.9.1",
            "Brother NC-8300h, Firmware Ver.1.10",
            "BRN",
            set(),
            False,
            [],
            True,
        ),
        ("printer", "brother", "printer"),
    ),
    (
        (
            "1.3.6.1.4.1.11.2.3.9.1",
            "HP ETHERNET MULTI-ENVIRONMENT,JETDIRECT",
            "hp",
            set(),
            False,
            [],
            True,
        ),
        ("printer", "hp", "printer"),
    ),
    (
        ("1.3.6.1.4.1.8072.3.2.10", "Linux printer", "p", {"printer_mib"}, False, [], True),
        ("printer", None, "printer"),
    ),
    (
        ("1.3.6.1.4.1.6574.1", "Linux DiskStation", "ds", set(), False, [], True),
        ("nas", "synology", "nas_server"),
    ),
    (
        ("1.3.6.1.4.1.8072.3.2.10", "Linux nas 4.4", "nas", {"synology"}, False, [], True),
        ("nas", "synology", "nas_server"),
    ),
    (
        ("1.3.6.1.4.1.8072.3.2.10", "Linux qnap", "q", {"qnap"}, False, [], True),
        ("nas", "qnap", "nas_server"),
    ),
    (
        (
            "1.3.6.1.4.1.311.1.1.3.1.2",
            "Hardware: Intel64 - Software: Windows Version 6.3",
            "pc",
            set(),
            False,
            [],
            True,
        ),
        ("workstation", "microsoft", "nas_server"),
    ),
    (
        ("1.3.6.1.4.1.311.1.1.3.1.3", "Windows Server 2022", "srv", set(), False, [], True),
        ("server", "microsoft", "nas_server"),
    ),
    (
        (
            "1.3.6.1.4.1.9.1.1208",
            "Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(7)E8",
            "core-sw",
            set(),
            False,
            [],
            True,
        ),
        ("switch", "cisco", "generic_snmp"),
    ),
    (
        ("1.3.6.1.4.1.9.1.1", "Cisco IOS Software, ISR4321", "rtr", set(), False, [], True),
        ("router", "cisco", "generic_snmp"),
    ),
    (
        ("1.3.6.1.4.1.9.1.2", "Cisco Aironet 1832", "ap", set(), False, [], True),
        ("access_point", "cisco", "generic_snmp"),
    ),
    (
        ("1.3.6.1.4.1.14988.1", "RouterOS CRS326-24G", "crs", set(), False, [], True),
        ("switch", "mikrotik", "generic_snmp"),
    ),
    (
        ("1.3.6.1.4.1.14988.1", "RouterOS RB4011", "rb", set(), False, [], True),
        ("router", "mikrotik", "generic_snmp"),
    ),
    (
        ("1.3.6.1.4.1.318.1.3.27", "APC Web/SNMP Management Card", "ups", set(), False, [], True),
        ("unknown", "apc", "generic_snmp"),
    ),
    (
        (
            "1.3.6.1.4.1.8072.3.2.10",
            "Linux web01 6.1.0-18-amd64 #1 SMP x86_64",
            "web01",
            set(),
            False,
            [22],
            True,
        ),
        ("server", "linux", "nas_server"),
    ),
    (
        ("1.3.6.1.4.1.8072.3.2.13", "Windows host", "w", set(), False, [], True),
        ("workstation", "microsoft", "nas_server"),
    ),
    (
        ("1.3.6.1.4.1.8072.3.2.16", "Darwin mac 23.0", "m", set(), False, [], True),
        ("workstation", "apple", "nas_server"),
    ),
    (
        ("1.3.6.1.4.1.8072.3.2.255", "something", "x", set(), False, [], True),
        ("server", None, "nas_server"),
    ),
    (
        ("1.3.6.1.4.1.77777.1", "Generic switch", "x", set(), False, [], True),
        ("switch", None, "generic_snmp"),
    ),
    (
        ("1.3.6.1.4.1.77777.1", "Home gateway", "x", set(), False, [], True),
        ("router", None, "generic_snmp"),
    ),
    (
        ("1.3.6.1.4.1.77777.1", "Laser printer", "x", set(), False, [], True),
        ("printer", None, "printer"),
    ),
    (
        ("1.3.6.1.4.1.77777.1", "embedded linux", "x", set(), False, [], True),
        ("server", None, "nas_server"),
    ),
    (
        ("1.3.6.1.4.1.77777.1", "Windows 11", "x", set(), False, [], True),
        ("workstation", None, "nas_server"),
    ),
    (
        ("1.3.6.1.4.1.77777.1", "mystery box", "x", set(), False, [], True),
        ("unknown", None, "generic_snmp"),
    ),
    ((None, None, None, set(), False, [9100], False), ("printer", None, "generic_ping")),
    ((None, None, None, set(), False, [80, 631], False), ("printer", None, "http_server")),
    ((None, None, None, set(), False, [3389], False), ("workstation", None, "generic_ping")),
    ((None, None, None, set(), False, [53, 3389], False), ("server", None, "generic_ping")),
    ((None, None, None, set(), False, [53, 443], False), ("router", None, "http_server")),
    ((None, None, None, set(), False, [22], False), ("server", None, "generic_ping")),
    ((None, None, None, set(), False, [445], False), ("workstation", None, "generic_ping")),
    ((None, None, None, set(), False, [8080], False), ("unknown", None, "http_server")),
    ((None, None, None, set(), False, [], False), ("unknown", None, "generic_ping")),
]


@pytest.mark.parametrize(("inputs", "expected"), CASES)
def test_classify(inputs: tuple, expected: tuple) -> None:
    device_class, vendor = classify(*inputs)
    assert (device_class, vendor) == expected[:2]
    tpl = suggest_template(
        {
            "device_class": device_class,
            "vendor": vendor,
            "snmp_ok": inputs[6],
            "open_ports": inputs[5],
        }
    )
    assert tpl == expected[2]


def test_enterprise() -> None:
    assert enterprise_of(".1.3.6.1.4.1.9.1.1208") == 9
    assert enterprise_of("1.3.6.1.2.1.1") is None
    assert enterprise_of(None) is None


# ------------------------------------------------------------------ templates
def test_templates_data() -> None:
    ids = [t["id"] for t in list_templates()]
    assert ids == [
        "opnsense",
        "unifi_ap",
        "printer",
        "hpe_arubaos_switch",
        "hpe_comware",
        "nas_server",
        "generic_snmp",
        "http_server",
        "generic_ping",
    ]
    for t in TEMPLATES:
        assert set(t) == {
            "id",
            "name",
            "description",
            "requires_snmp",
            "priority",
            "match",
            "entries",
        }
        assert set(t["name"]) == {"en", "he"} and set(t["description"]) == {"en", "he"}
        for rule in t["match"]:
            assert set(rule) == {
                "device_classes",
                "vendors",
                "requires_snmp",
                "any_open_ports",
                "enterprises",
            }
        for e in t["entries"]:
            keys = {
                "sensor": {"kind", "type", "name", "config", "channels", "interval_s", "probe"},
                "snmp_interfaces": {
                    "kind",
                    "oper_up_only",
                    "exclude_if_types",
                    "include_name_regex",
                    "exclude_name_regex",
                    "max",
                    "channels",
                },
                "snmp_table": {
                    "kind",
                    "walk_oid",
                    "label_oid",
                    "label2_oid",
                    "percent_of_column",
                    "name",
                    "config",
                    "channels",
                    "max",
                    "skip_zero",
                    "filters",
                    "interval_s",
                },
            }[e["kind"]]
            assert set(e) == keys
    unifi = get_template("unifi_ap")
    assert unifi is not None and unifi["name"]["he"] == "נקודת גישה UniFi"
    assert unifi["entries"][4]["include_name_regex"] == "^eth0$"
    assert get_template("nope") is None
    # read-only: returned copies do not change the built-in data
    unifi["priority"] = -1
    assert get_template("unifi_ap")["priority"] == 80  # type: ignore[index]


# ------------------------------------------------------------------ options
def test_options() -> None:
    o = merge_options({"extra_communities": ["s3cret"], "snmp_port": 1161}, None)
    assert o["ping"] is True and o["tcp_ports"][0] == 22 and o["snmp_port"] == 1161
    assert merge_options({"extra_communities": None}, o)["extra_communities"] == ["s3cret"]
    assert merge_options({"extra_communities": []}, o)["extra_communities"] == []
    o["extra_v3"] = [
        {
            "v3_username": "u",
            "v3_security_level": "authPriv",
            "v3_auth_protocol": "SHA",
            "v3_auth_key": "authkey123",
            "v3_priv_protocol": "AES128",
            "v3_priv_key": "privkey123",
        }
    ]
    pub = public_options(o)
    assert pub["extra_communities"] is None and pub["extra_communities_count"] == 1
    assert pub["extra_v3"][0]["v3_auth_key"] is None and pub["extra_v3"][0]["v3_auth_key_set"]
    assert "s3cret" not in str(pub) and "authkey123" not in str(pub)
    snap = scan_snapshot(o)
    assert snap["extra_communities"] == [] and snap["extra_communities_count"] == 1
    assert "s3cret" not in str(snap) and "privkey123" not in str(snap)
    assert public_options(snap)["extra_communities_count"] == 1
    assert public_options(snap)["extra_v3"][0]["v3_priv_key_set"] is True
    creds = build_credentials({"version": "v2c", "port": 161, "community": "grp"}, o)
    assert [c["version"] for c in creds] == ["v2c", "v2c", "v3"]
    assert [c["port"] for c in creds] == [1161] * 3
    assert all(c["retries"] == 0 and c["timeout_s"] == 1 for c in creds)
    assert creds[1]["community"] == "s3cret" and creds[2]["v3_priv_key"] == "privkey123"
    o2 = merge_options({"extra_communities": [f"c{i}" for i in range(5)]}, None)
    o2["extra_v3"] = o["extra_v3"] * 3
    assert len(build_credentials({"port": 161}, o2)) == 9
    assert build_credentials({"port": 2161}, merge_options(None))[0]["port"] == 2161
