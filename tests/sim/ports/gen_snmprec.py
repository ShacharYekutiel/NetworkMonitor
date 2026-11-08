#!/usr/bin/env python3
"""Generates the snmprec files of the Phase 2b-2 port-inventory lab (docs/architecture.md §11.6).

    python3 tests/sim/ports/gen_snmprec.py      # rewrites */public.snmprec next to this script + ptr.json

All agents: SNMP v2c, community `public`, udp/1161 (tests/sim/lab.sh, LAB_2B2=1). The generated files are
committed.

ADDRESSES: the contract (§11.6) places these sims on 127.0.0.61-.65, but 127.0.0.61-.67 are the 2b-1
fingerprint hosts that spec 23 scans with `snmp: true` on udp/1161; SNMP agents there would change the 2b-1
classification results. The lab therefore uses 127.0.0.81-.85 (same data, same order; QA defect report):

  127.0.0.81  arubaos/        (contract .61) ArubaOS-Switch 2930F "port-aos": ports 1-10, Trk1, DEFAULT_VLAN,
                              Q-BRIDGE static, no dot1dBasePortIfIndex, FDB dot1q, PoE group 1, EtherLike duplex
  variant     arubaos_nomac7/ same without the FDB row of 00:50:56:01:00:07 on port 7 (presence test)
  127.0.0.82  comware/        (contract .62) Comware "port-cw": 1-4, 49/50 (10G), 2/0/1-2 (stack member 2),
                              Vlan-interface1, NULL0, InLoopBack0, M-GigabitEthernet0/0/0; dot1dBasePortIfIndex,
                              dot1qVlanFdbId (VLAN 10 -> fdb 1010); QA extra: LLDP neighbour on port 2 = the
                              2b-1 UniFi AP "U6-Lite-Office" (mgmt 127.0.0.54) -> connected_kind "ap"
  127.0.0.83  arubacx/        (contract .63) Aruba CX 6300M: no static VLAN table (current table), no dot1qPvid,
                              no dot1qTpFdb (dot1d fallback), mgmt port
  127.0.0.84  arp_router/     (contract .64) OPNsense-style ARP source: ipNetToPhysicalTable
  127.0.0.85  arp_legacy/     (contract .65) "Linux gw2 5.10": ipNetToMediaTable only
  127.0.0.86  arubaos52/      QA extra: ArubaOS 2930F-48G-4SFP+ "port-aos52", 52 physical ports (49-52 10G)
                              for the RTL/mobile faceplate overflow test
  ptr.json                    NETMON_TEST_PTR_FILE (10.0.0.11 pc-reception.lan, 10.0.0.12 phone-12.lan)

snmprec tags: 2 Integer32, 4 OctetString, 4x hex OctetString, 6 OID, 64 IpAddress, 65 Counter32,
66 Gauge32, 67 TimeTicks, 70 Counter64. Counters are static on purpose (contract: second run -> in_bps 0.0).
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

SYS = "1.3.6.1.2.1.1"
IF = "1.3.6.1.2.1.2.2.1"
IFX = "1.3.6.1.2.1.31.1.1.1"
DOT3_DUPLEX = "1.3.6.1.2.1.10.7.2.1.19"
BASE_PORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"
DOT1D_FDB = "1.3.6.1.2.1.17.4.3.1"
DOT1Q_FDB = "1.3.6.1.2.1.17.7.1.2.2.1"
DOT1Q_CURRENT = "1.3.6.1.2.1.17.7.1.4.2.1"
DOT1Q_STATIC = "1.3.6.1.2.1.17.7.1.4.3.1"
DOT1Q_PVID = "1.3.6.1.2.1.17.7.1.4.5.1.1"
PETH_PORT = "1.3.6.1.2.1.105.1.1.1"
NET_TO_PHYS = "1.3.6.1.2.1.4.35.1"
NET_TO_MEDIA = "1.3.6.1.2.1.4.22.1"
LLDP_LOC = "1.0.8802.1.1.2.1.3"
LLDP_REM = "1.0.8802.1.1.2.1.4.1.1"
LLDP_MAN = "1.0.8802.1.1.2.1.4.2.1"

Rec = tuple[str, str, str]

ARUBA_DESCR = "Aruba JL256A 2930F-48G-4SFP+ Switch, revision WC.16.11.0012, ROM WC.16.01.0010 (/ws/swbuildm/rel_tacoma_qaoff/code/build/lvm(swbuildm_rel_tacoma_qaoff_rel_tacoma))"
COMWARE_DESCR = "HPE Comware Platform Software, Software Version 7.1.070, Release 6328 HPE 5130-24G-PoE+-4SFP+ EI Switch"
CX_DESCR = "Aruba JL658A 6300M 24SFP+ 4SFP56 Swch FL.10.13.1000"
OPN_DESCR = "FreeBSD OPNsense.lan 14.1-RELEASE-p5 FreeBSD 14.1-RELEASE-p5 stable/24.7-n267758-9d6d3e1c0e7 SMP amd64"
LEGACY_DESCR = "Linux gw2 5.10.0-28-amd64 #1 SMP Debian 5.10.209-2 x86_64"
AP_DESCR = "Linux U6-Lite 4.4.153 #1 SMP Wed Jul 5 2023 mips"
MAC_AP = "7483c2000054"  # chassis id of the 2b-1 UniFi LLDP sim "U6-Lite-Office" (tests/sim/hpe/unifi_lldp, 127.0.0.54)


def mac_hex(mac: str) -> str:
    return mac.replace(":", "").replace("-", "").lower()


def mac_arcs(mac: str) -> str:
    h = mac_hex(mac)
    return ".".join(str(int(h[i : i + 2], 16)) for i in range(0, 12, 2))


def portlist(ports: set[int], octets: int) -> str:
    """Q-BRIDGE PortList: MSB of octet 0 = bridge port 1 (data-model § Pure parsers)."""
    b = bytearray(octets)
    for p in ports:
        b[(p - 1) // 8] |= 0x80 >> ((p - 1) % 8)
    return b.hex()


def system(descr: str, oid: str, name: str, uptime: int) -> list[Rec]:
    # sysUpTime static: last_change_at = collected_at - (sysUpTime - ifLastChange) / 100 s is then exact.
    return [
        (f"{SYS}.1.0", "4", descr),
        (f"{SYS}.2.0", "6", oid),
        (f"{SYS}.3.0", "67", str(uptime)),
        (f"{SYS}.4.0", "4", "qa@netmon.local"),
        (f"{SYS}.5.0", "4", name),
        (f"{SYS}.6.0", "4", "QA ports lab"),
        (f"{SYS}.7.0", "2", "6"),
    ]


def interfaces(rows: list[dict]) -> list[Rec]:
    """rows: idx, name, descr?, type (6), speed bit/s (1G; 0 = unknown), high (Mb/s, default speed/1e6),
    admin (1), oper (1 up / 2 down), mac hex, alias, last_change (TimeTicks), in/out octets, errors."""
    out: list[Rec] = [("1.3.6.1.2.1.2.1.0", "2", str(len(rows)))]
    for r in rows:
        i = r["idx"]
        speed = r.get("speed", 1_000_000_000)
        high = r.get("high", speed // 1_000_000)
        ino, outo = r.get("in_octets", 1_000_000 + i * 1000), r.get("out_octets", 2_000_000 + i * 1000)
        out += [
            (f"{IF}.1.{i}", "2", str(i)),
            (f"{IF}.2.{i}", "4", r.get("descr", r["name"])),
            (f"{IF}.3.{i}", "2", str(r.get("type", 6))),
            (f"{IF}.4.{i}", "2", "1500"),
            (f"{IF}.5.{i}", "66", str(min(speed, 4_294_967_295))),
            (f"{IF}.6.{i}", "4x", r.get("mac", "")),
            (f"{IF}.7.{i}", "2", str(r.get("admin", 1))),
            (f"{IF}.8.{i}", "2", str(r.get("oper", 1))),
            (f"{IF}.9.{i}", "67", str(r.get("last_change", 0))),
            (f"{IF}.10.{i}", "65", str(ino % 2**32)),
            (f"{IF}.14.{i}", "65", str(r.get("in_errors", 0))),
            (f"{IF}.16.{i}", "65", str(outo % 2**32)),
            (f"{IF}.20.{i}", "65", str(r.get("out_errors", 0))),
            (f"{IFX}.1.{i}", "4", r["name"]),
            (f"{IFX}.6.{i}", "70", str(ino)),
            (f"{IFX}.10.{i}", "70", str(outo)),
            (f"{IFX}.15.{i}", "66", str(high)),
            (f"{IFX}.18.{i}", "4", r.get("alias", "")),
        ]
    return out


def duplex(rows: dict[int, int]) -> list[Rec]:
    return [(f"{DOT3_DUPLEX}.{i}", "2", str(v)) for i, v in rows.items()]


def base_ports(mapping: dict[int, int]) -> list[Rec]:
    return [(f"{BASE_PORT_IFINDEX}.{bp}", "2", str(ifi)) for bp, ifi in mapping.items()]


def vlan_static(vlans: dict[int, tuple[set[int], set[int], str]], octets: int) -> list[Rec]:
    """vlan -> (egress bridge ports, untagged bridge ports, name)."""
    out: list[Rec] = []
    for v, (eg, ut, name) in vlans.items():
        out += [
            (f"{DOT1Q_STATIC}.1.{v}", "4", name),
            (f"{DOT1Q_STATIC}.2.{v}", "4x", portlist(eg, octets)),
            (f"{DOT1Q_STATIC}.3.{v}", "4x", portlist(set(), octets)),
            (f"{DOT1Q_STATIC}.4.{v}", "4x", portlist(ut, octets)),
            (f"{DOT1Q_STATIC}.5.{v}", "2", "1"),
        ]
    return out


def vlan_current(vlans: dict[int, tuple[set[int], set[int]]], octets: int, fdb_ids: dict[int, int] | None = None) -> list[Rec]:
    """dot1qVlanCurrentTable, index TimeMark(0).VlanIndex; .3 FdbId only when fdb_ids is given."""
    out: list[Rec] = []
    for v, (eg, ut) in vlans.items():
        out += [
            (f"{DOT1Q_CURRENT}.4.0.{v}", "4x", portlist(eg, octets)),
            (f"{DOT1Q_CURRENT}.5.0.{v}", "4x", portlist(ut, octets)),
            (f"{DOT1Q_CURRENT}.6.0.{v}", "2", "2"),  # dot1qVlanStatus permanent
        ]
    for v, f in (fdb_ids or {}).items():
        out.append((f"{DOT1Q_CURRENT}.3.0.{v}", "66", str(f)))
    return out


def pvids(m: dict[int, int]) -> list[Rec]:
    return [(f"{DOT1Q_PVID}.{bp}", "66", str(v)) for bp, v in m.items()]


def fdb_q(rows: list[tuple[int, str, int, int]]) -> list[Rec]:
    """(fdbId, mac, bridge port, status)."""
    out: list[Rec] = []
    for fid, mac, bp, st in rows:
        sfx = f"{fid}.{mac_arcs(mac)}"
        out += [(f"{DOT1Q_FDB}.2.{sfx}", "2", str(bp)), (f"{DOT1Q_FDB}.3.{sfx}", "2", str(st))]
    return out


def fdb_d(rows: list[tuple[str, int, int]]) -> list[Rec]:
    out: list[Rec] = []
    for mac, bp, st in rows:
        sfx = mac_arcs(mac)
        out += [
            (f"{DOT1D_FDB}.1.{sfx}", "4x", mac_hex(mac)),
            (f"{DOT1D_FDB}.2.{sfx}", "2", str(bp)),
            (f"{DOT1D_FDB}.3.{sfx}", "2", str(st)),
        ]
    return out


def poe(group: int, rows: dict[int, tuple[int, int, int | None]]) -> list[Rec]:
    """port -> (admin 1/2, detection 1-6, PowerClassifications raw 1-5 or None = no row)."""
    out: list[Rec] = []
    for p, (adm, det, cls) in rows.items():
        out += [
            (f"{PETH_PORT}.3.{group}.{p}", "2", str(adm)),
            (f"{PETH_PORT}.6.{group}.{p}", "2", str(det)),
        ]
        if cls is not None:
            out.append((f"{PETH_PORT}.10.{group}.{p}", "2", str(cls)))
    return out


def lldp_local(chassis_mac: str, sys_name: str, sys_descr: str, ports: list[tuple[int, int, str, str]]) -> list[Rec]:
    out: list[Rec] = [
        (f"{LLDP_LOC}.1.0", "2", "4"),
        (f"{LLDP_LOC}.2.0", "4x", chassis_mac),
        (f"{LLDP_LOC}.3.0", "4", sys_name),
        (f"{LLDP_LOC}.4.0", "4", sys_descr),
    ]
    for num, sub, pid, desc in ports:
        out += [
            (f"{LLDP_LOC}.7.1.2.{num}", "2", str(sub)),
            (f"{LLDP_LOC}.7.1.3.{num}", "4", pid),
            (f"{LLDP_LOC}.7.1.4.{num}", "4", desc),
        ]
    return out


def lldp_remote(local_port: int, rem_index: int, chassis_mac: str, port_sub: int, port_id: str, port_desc: str,
                sys_name: str, sys_descr: str, mgmt_ipv4: str | None = None) -> list[Rec]:
    idx = f"0.{local_port}.{rem_index}"
    out: list[Rec] = [
        (f"{LLDP_REM}.4.{idx}", "2", "4"),
        (f"{LLDP_REM}.5.{idx}", "4x", chassis_mac),
        (f"{LLDP_REM}.6.{idx}", "2", str(port_sub)),
        (f"{LLDP_REM}.7.{idx}", "4", port_id),
        (f"{LLDP_REM}.8.{idx}", "4", port_desc),
        (f"{LLDP_REM}.9.{idx}", "4", sys_name),
        (f"{LLDP_REM}.10.{idx}", "4", sys_descr),
        (f"{LLDP_REM}.11.{idx}", "4x", "0018"),
        (f"{LLDP_REM}.12.{idx}", "4x", "0010"),  # enabled: wlanAccessPoint
    ]
    if mgmt_ipv4:
        a = f"{idx}.1.4.{mgmt_ipv4}"
        out += [
            (f"{LLDP_MAN}.3.{a}", "2", "2"),
            (f"{LLDP_MAN}.4.{a}", "2", "0"),
            (f"{LLDP_MAN}.5.{a}", "6", "0.0"),
        ]
    return out


def write(rel: str, records: list[Rec]) -> None:
    key = lambda r: tuple(int(x) for x in r[0].split("."))  # noqa: E731
    seen: set[str] = set()
    for oid, _, _ in records:
        assert oid not in seen, f"{rel}: duplicate {oid}"
        seen.add(oid)
    path = HERE / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{o}|{t}|{v}\n" for o, t, v in sorted(records, key=key)))
    print(f"wrote {path.relative_to(HERE)} ({len(records)} records)")


# ------------------------------------------------------------------ 127.0.0.81 ArubaOS-Switch (contract .61)
G, M = 1_000_000_000, 100_000_000
TEN_G = dict(speed=4_294_967_295, high=10_000)
AOS_PORTS = [
    dict(idx=1, name="1", mac="001a1e006101", last_change=99_000_000, alias="Reception PC"),
    dict(idx=2, name="2", mac="001a1e006102", alias="Phone + PC"),
    dict(idx=3, name="3", mac="001a1e006103", speed=M),
    dict(idx=4, name="4", mac="001a1e006104"),
    dict(idx=5, name="5", mac="001a1e006105", admin=2, oper=2, alias="unused (disabled)"),
    dict(idx=6, name="6", mac="001a1e006106", oper=2, speed=0, high=0),
    dict(idx=7, name="7", mac="001a1e006107", alias="Accounting"),
    dict(idx=8, name="8", mac="001a1e006108", oper=2, speed=0, high=0),
    dict(idx=9, name="9", mac="001a1e006109", alias="to unmanaged switch", **TEN_G),
    dict(idx=10, name="10", mac="001a1e00610a", alias="uplink port-cw", **TEN_G),
    dict(idx=11, name="Trk1", type=161, speed=0, high=0, oper=2, mac="001a1e00610b"),
    dict(idx=12, name="DEFAULT_VLAN", type=53, speed=0, high=0, mac="001a1e000061"),
]
AOS_FDB = (
    [
        (1, "00:50:56:01:00:01", 1, 3),
        (1, "3c:52:82:00:00:02", 2, 3),
        (1, "00:00:0c:00:00:03", 2, 3),
        (1, "00:1a:1e:00:00:61", 3, 4),  # self(4): dropped
        (1, "da:a1:19:00:00:04", 4, 3),
        (10, "00:50:56:01:00:07", 7, 3),
    ]
    + [(v, f"02:00:00:00:09:0{n}", 9, 3) for n, v in zip(range(1, 8), (1, 1, 1, 10, 10, 20, 20))]
    + [(10, "02:00:00:00:0a:01", 10, 3), (10, "02:00:00:00:0a:02", 10, 3)]
)


def arubaos(fdb: list[tuple[int, str, int, int]]) -> list[Rec]:
    return (
        system(ARUBA_DESCR, "1.3.6.1.4.1.11.2.3.7.11.181", "port-aos", 100_000_000)
        + interfaces(AOS_PORTS)
        + vlan_static(
            {
                1: ({1, 2, 3, 4, 5, 6, 8, 9}, {1, 2, 3, 4, 5, 6, 8, 9}, "DEFAULT_VLAN"),
                10: ({7, 9, 10}, {7}, "OFFICE"),
                20: ({9, 10}, set(), "VOICE"),
            },
            2,
        )
        + pvids({p: (10 if p == 7 else 1) for p in range(1, 11)})
        + fdb_q(fdb)
        + poe(1, {1: (1, 3, 4), 2: (1, 2, None), 3: (1, 2, None), 4: (1, 3, 5), 5: (2, 1, None),
                  6: (1, 2, None), 7: (1, 3, 1), 8: (1, 4, None)})
        + duplex({1: 3, 2: 3, 4: 3, 7: 3, 9: 3, 10: 3, 3: 2, 6: 1})
    )


write("arubaos/public.snmprec", arubaos(AOS_FDB))
write("arubaos_nomac7/public.snmprec", arubaos([r for r in AOS_FDB if r[1] != "00:50:56:01:00:07"]))

# ------------------------------------------------------------------ 127.0.0.82 Comware (contract .62)
CW_PORTS = [
    dict(idx=1, name="GigabitEthernet1/0/1", descr="GigabitEthernet1/0/1 Interface", mac="3c8c40006201"),
    dict(idx=2, name="GigabitEthernet1/0/2", descr="GigabitEthernet1/0/2 Interface", mac="3c8c40006202", alias="AP lobby"),
    dict(idx=3, name="GigabitEthernet1/0/3", descr="GigabitEthernet1/0/3 Interface", mac="3c8c40006203"),
    dict(idx=4, name="GigabitEthernet1/0/4", descr="GigabitEthernet1/0/4 Interface", mac="3c8c40006204"),
    dict(idx=49, name="Ten-GigabitEthernet1/0/49", descr="Ten-GigabitEthernet1/0/49 Interface", mac="3c8c40006231", **TEN_G),
    dict(idx=50, name="Ten-GigabitEthernet1/0/50", descr="Ten-GigabitEthernet1/0/50 Interface", mac="3c8c40006232", **TEN_G),
    dict(idx=101, name="GigabitEthernet2/0/1", descr="GigabitEthernet2/0/1 Interface", mac="3c8c40006265"),
    dict(idx=102, name="GigabitEthernet2/0/2", descr="GigabitEthernet2/0/2 Interface", mac="3c8c40006266", oper=2, speed=0, high=0),
    dict(idx=200, name="Vlan-interface1", type=136, speed=0, high=0, mac="3c8c40000062"),
    dict(idx=300, name="NULL0", type=1, speed=0, high=0),
    dict(idx=301, name="InLoopBack0", type=24, speed=0, high=0),
    dict(idx=400, name="M-GigabitEthernet0/0/0", descr="M-GigabitEthernet0/0/0 Interface", mac="3c8c40006290"),
]
comware = (
    system(COMWARE_DESCR, "1.3.6.1.4.1.25506.11.1.177", "port-cw", 50_000_000)
    + interfaces(CW_PORTS)
    + base_ports({1: 1, 2: 2, 3: 3, 4: 4, 5: 49, 6: 50, 7: 101, 8: 102})
    + vlan_static(
        {
            1: ({1, 2, 3, 5, 6, 7, 8}, {1, 2, 3, 5, 7, 8}, "VLAN 0001"),
            10: ({4, 5, 6}, {4}, "VLAN 0010"),
            20: ({5, 6}, set(), "VLAN 0020"),
        },
        1,
    )
    # dot1qVlanFdbId lives in the current table (TimeMark 0); only the FdbId column is present here.
    + [(f"{DOT1Q_CURRENT}.3.0.1", "66", "1"), (f"{DOT1Q_CURRENT}.3.0.10", "66", "1010")]
    + pvids({1: 1, 2: 1, 3: 1, 4: 10, 5: 1, 6: 1, 7: 1, 8: 1})
    + fdb_q(
        [
            (1, "00:0c:29:00:01:01", 1, 3),
            (1010, "00:0c:29:00:01:04", 4, 3),
            (1, "00:0c:29:00:01:07", 7, 3),
            (1, "02:00:00:00:09:01", 3, 3),
        ]
    )
    + poe(1, {1: (1, 3, 4), 2: (1, 2, None), 3: (1, 2, None), 4: (1, 2, None)})
    + duplex({1: 3, 2: 3, 3: 3, 4: 3, 49: 3, 50: 3, 101: 3})
    # QA extra: LLDP neighbour on port 2 = the 2b-1 UniFi AP sim (device 127.0.0.54, class access_point by
    # rule 3) -> AP tile. (The Aruba Instant sim 127.0.0.73 would be "switch": rule 10's regex has `arubaos`.)
    + lldp_local("3c8c40000062", "port-cw", COMWARE_DESCR,
                 [(n, 5, nm, nm) for n, nm in ((1, "GigabitEthernet1/0/1"), (2, "GigabitEthernet1/0/2"))])
    + lldp_remote(2, 1, MAC_AP, 5, "eth0", "eth0", "U6-Lite-Office", AP_DESCR, mgmt_ipv4="127.0.0.54")
)
write("comware/public.snmprec", comware)

# ------------------------------------------------------------------ 127.0.0.83 Aruba CX (contract .63)
CX_PORTS = [dict(idx=n, name=f"1/1/{n}", mac=f"88e9a40063{n:02x}", oper=1 if n <= 4 else 2,
                 **({} if n <= 4 else dict(speed=0, high=0))) for n in range(1, 7)] + [
    dict(idx=49, name="1/1/49", mac="88e9a4006331", **TEN_G),
    dict(idx=50, name="1/1/50", mac="88e9a4006332", **TEN_G),
    dict(idx=1000, name="vlan1", type=53, speed=0, high=0, mac="88e9a4000063"),
    dict(idx=2000, name="mgmt", type=6, mac="88e9a40063ff"),
]
arubacx = (
    system(CX_DESCR, "1.3.6.1.4.1.47196.4.1.1.1.200", "port-cx", 70_000_000)
    + interfaces(CX_PORTS)
    + base_ports({n: n for n in (1, 2, 3, 4, 5, 6, 49, 50)})
    + vlan_current({1: ({1, 2, 3, 4, 5, 6}, {1, 2, 3, 4, 5, 6})}, 7)
    + fdb_d([("00:00:0c:00:cc:01", 1, 3)])
    + poe(1, {n: (1, 3 if n == 1 else 2, 4 if n == 1 else None) for n in range(1, 7)})
    + duplex({n: 3 for n in (1, 2, 3, 4, 49, 50)})
)
write("arubacx/public.snmprec", arubacx)

# ------------------------------------------------------------------ 127.0.0.84 ARP router (contract .64)
def phys(ifi: int, ip: str, mac: str, typ: int) -> list[Rec]:
    sfx = f"{ifi}.1.4.{ip}"
    return [(f"{NET_TO_PHYS}.4.{sfx}", "4x", mac_hex(mac)), (f"{NET_TO_PHYS}.6.{sfx}", "2", str(typ))]


V6 = "254.128.0.0.0.0.0.0.2.80.86.255.254.1.0.1"  # fe80::250:56ff:fe01:1
arp_router = (
    system(OPN_DESCR, "1.3.6.1.4.1.8072.3.2.8", "fw-arp", 40_000_000)
    + interfaces([dict(idx=1, name="igb0", mac="000db9006401"), dict(idx=2, name="igb1", mac="000db9006402")])
    + phys(2, "10.0.0.11", "00:50:56:01:00:01", 3)
    + phys(2, "10.0.0.12", "00:00:0c:00:00:03", 3)
    + phys(2, "10.0.0.17", "00:50:56:01:00:07", 3)
    + phys(2, "10.0.0.99", "02:00:00:00:09:01", 4)
    + phys(2, "10.0.0.50", "00:50:56:01:00:50", 2)  # invalid: dropped
    + phys(2, "10.0.0.1", "00:0d:b9:00:64:02", 5)  # local (own MAC): dropped
    + [(f"{NET_TO_PHYS}.4.2.2.16.{V6}", "4x", "005056010001"), (f"{NET_TO_PHYS}.6.2.2.16.{V6}", "2", "3")]
)
write("arp_router/public.snmprec", arp_router)

# ------------------------------------------------------------------ 127.0.0.85 legacy ARP (contract .65)
arp_legacy = (
    system(LEGACY_DESCR, "1.3.6.1.4.1.8072.3.2.10", "gw2", 30_000_000)
    + interfaces([dict(idx=1, name="lo", type=24, speed=0, high=0), dict(idx=2, name="eth0", mac="525400006502")])
    + [
        (f"{NET_TO_MEDIA}.1.2.10.0.0.14", "2", "2"),
        (f"{NET_TO_MEDIA}.2.2.10.0.0.14", "4x", "daa119000004"),
        (f"{NET_TO_MEDIA}.3.2.10.0.0.14", "64", "10.0.0.14"),
        (f"{NET_TO_MEDIA}.4.2.10.0.0.14", "2", "3"),
    ]
)
write("arp_legacy/public.snmprec", arp_legacy)

# ------------------------------------------------------------------ 127.0.0.86 52-port ArubaOS (QA extra)
P52 = [dict(idx=n, name=str(n), mac=f"001a1e0086{n:02x}", oper=1 if n % 3 else 2,
            **(TEN_G if n > 48 else ({} if n % 3 else dict(speed=0, high=0)))) for n in range(1, 53)]
aos52 = (
    system(ARUBA_DESCR, "1.3.6.1.4.1.11.2.3.7.11.181", "port-aos52", 20_000_000)
    + interfaces(P52 + [dict(idx=53, name="DEFAULT_VLAN", type=53, speed=0, high=0, mac="001a1e000086")])
    + vlan_static({1: (set(range(1, 53)), set(range(1, 53)), "DEFAULT_VLAN")}, 7)
    + pvids({p: 1 for p in range(1, 53)})
)
write("arubaos52/public.snmprec", aos52)

(HERE / "ptr.json").write_text(json.dumps({"10.0.0.11": "pc-reception.lan", "10.0.0.12": "phone-12.lan"}, indent=2) + "\n")
print("wrote ptr.json")
