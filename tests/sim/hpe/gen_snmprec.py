#!/usr/bin/env python3
"""Generates the snmprec files of the Phase 2b-1 HPE + LLDP lab (docs/architecture.md §10.4).

    python3 tests/sim/hpe/gen_snmprec.py        # rewrites the */public.snmprec files next to this script

All agents: SNMP v2c, community `public`, udp/1161 (tests/sim/lab.sh). The generated files are committed.

  127.0.0.51  arubaos/   ArubaOS-Switch 2930F "core-sw": CPU 7 %, memory 39.39 %, hpicfSensorTable with a bad
                         PSU (2) and a notPresent RPS (5), PoE group 1 (45 W of 370 W), ports 1-4 + DEFAULT_VLAN,
                         LLDP (ArubaOS style: lldpLocPortNum = ifIndex, port id subtype 7 = "1".."4")
  127.0.0.52  comware/   Comware 1950 "access-1": entPhysical chassis/board/2 fans/PSU, CPU 12 %, memory 41 %,
                         temperature 43 C, Fan 2 = fanError (41), PoE budget 0 (rows skipped),
                         LLDP (Comware style: lldpLocPortNum 101-104 != ifIndex 1-4, port id subtype 5 = ifName)
  127.0.0.53  access2/   ArubaOS-Switch 2530 "access-2", minimal (system + ifTable + LLDP)
  127.0.0.54  unifi_lldp/ UniFi U6-Lite "U6-Lite-Office" (net-snmp Linux) with LLDP on eth0
  variant     access2_noprinter/  access-2 without the remote row of the unmanaged neighbour (stale test)

Remote rows (index TimeMark.LocalPortNum.RemIndex, time mark 0):
  core 1  <-> access-1 GigabitEthernet1/0/1   (core sees mgmt addr 127.0.0.52; access-1 sees 127.0.0.51)
  core 2  <-> access-2 24                     (no mgmt address on either side: match by chassis id)
  access-1 GigabitEthernet1/0/2 <-> AP eth0   (access-1 sees a chassis id that differs from the AP's own
                                               lldpLocChassisId, so only rule 3 (sysName U6-Lite-Office) matches)
  access-2 3 <-> unmanaged printer-3f         (chassis 00:11:22:33:44:55, mgmt 127.0.0.99, no device)
Expected: 3 device-device links (both sides collapse), 1 neighbour node.

snmprec tags: 2 Integer32, 4 OctetString, 4x hex OctetString, 6 OID, 65 Counter32, 66 Gauge32,
67 TimeTicks, 70 Counter64. `:numeric` = snmpsim variation module (value grows with wall time).
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent

SYS = "1.3.6.1.2.1.1"
IF = "1.3.6.1.2.1.2.2.1"
IFX = "1.3.6.1.2.1.31.1.1.1"
HR_STORAGE = "1.3.6.1.2.1.25.2.3.1"
HR_PROC = "1.3.6.1.2.1.25.3.3.1.2"
HR_RAM = "1.3.6.1.2.1.25.2.1.2"
ENT_PHYS = "1.3.6.1.2.1.47.1.1.1.1"
HH3C_EXT = "1.3.6.1.4.1.25506.2.6.1.1.1.1"
HP_CPU = "1.3.6.1.4.1.11.2.14.11.5.1.9.6.1.0"
HP_MEM = "1.3.6.1.4.1.11.2.14.11.5.1.1.2.1.1.1"
HPICF_SENSOR = "1.3.6.1.4.1.11.2.14.11.1.2.6.1"
PETH_MAIN = "1.3.6.1.2.1.105.1.3.1.1"
LLDP_LOC = "1.0.8802.1.1.2.1.3"
LLDP_REM = "1.0.8802.1.1.2.1.4.1.1"
LLDP_MAN = "1.0.8802.1.1.2.1.4.2.1"

MAC_CORE = "001a1e000051"
MAC_ACC1 = "3c8c40000052"
MAC_ACC2 = "001a1e000053"
MAC_AP = "7483c2000054"
MAC_AP_AS_SEEN = "7483c200005e"  # what access-1 reports for the AP (deliberately != MAC_AP)
MAC_PRINTER = "001122334455"


def system(descr: str, oid: str, name: str, uptime: int) -> list[tuple[str, str, str]]:
    return [
        (f"{SYS}.1.0", "4", descr),
        (f"{SYS}.2.0", "6", oid),
        (f"{SYS}.3.0", "67:numeric", f"initial={uptime},rate=100"),
        (f"{SYS}.4.0", "4", "qa@netmon.local"),
        (f"{SYS}.5.0", "4", name),
        (f"{SYS}.6.0", "4", "QA HPE lab"),
        (f"{SYS}.7.0", "2", "6"),
    ]


def interfaces(rows: list[dict]) -> list[tuple[str, str, str]]:
    """rows: {idx, name, type, speed (bit/s), oper (1 up / 2 down), mac (hex), rate_in, rate_out}."""
    out = [("1.3.6.1.2.1.2.1.0", "2", str(len(rows)))]
    for r in rows:
        i = r["idx"]
        up = r.get("oper", 1) == 1
        rin, rout = (r.get("rate_in", 0), r.get("rate_out", 0)) if up else (0, 0)
        speed = r.get("speed", 1_000_000_000)
        out += [
            (f"{IF}.1.{i}", "2", str(i)),
            (f"{IF}.2.{i}", "4", r.get("descr", r["name"])),
            (f"{IF}.3.{i}", "2", str(r.get("type", 6))),
            (f"{IF}.4.{i}", "2", "1500"),
            (f"{IF}.5.{i}", "66", str(min(speed, 4_294_967_295))),
            (f"{IF}.6.{i}", "4x", r.get("mac", "")),
            (f"{IF}.7.{i}", "2", "1"),
            (f"{IF}.8.{i}", "2", str(r.get("oper", 1))),
            (f"{IF}.10.{i}", "65:numeric" if rin else "65", f"initial=1000,rate={rin},wrap=1" if rin else "0"),
            (f"{IF}.14.{i}", "65", "0"),
            (f"{IF}.16.{i}", "65:numeric" if rout else "65", f"initial=1000,rate={rout},wrap=1" if rout else "0"),
            (f"{IF}.20.{i}", "65", "0"),
            (f"{IFX}.1.{i}", "4", r["name"]),
            (f"{IFX}.6.{i}", "70:numeric" if rin else "70", f"initial=1000,rate={rin}" if rin else "0"),
            (f"{IFX}.10.{i}", "70:numeric" if rout else "70", f"initial=1000,rate={rout}" if rout else "0"),
            (f"{IFX}.15.{i}", "66", str(speed // 1_000_000)),
            (f"{IFX}.18.{i}", "4", r.get("alias", "")),
        ]
    return out


def lldp_local(chassis_mac: str, sys_name: str, sys_descr: str, ports: list[tuple[int, int, str, str]]):
    """ports: (lldpLocPortNum, port id subtype, port id, port desc)."""
    out = [
        (f"{LLDP_LOC}.1.0", "2", "4"),  # lldpLocChassisIdSubtype macAddress
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
                sys_name: str, sys_descr: str, mgmt_ipv4: str | None = None):
    idx = f"0.{local_port}.{rem_index}"
    out = [
        (f"{LLDP_REM}.4.{idx}", "2", "4"),
        (f"{LLDP_REM}.5.{idx}", "4x", chassis_mac),
        (f"{LLDP_REM}.6.{idx}", "2", str(port_sub)),
        (f"{LLDP_REM}.7.{idx}", "4x" if port_sub == 3 else "4", port_id),  # subtype 3 = macAddress (6 octets)
        (f"{LLDP_REM}.8.{idx}", "4", port_desc),
        (f"{LLDP_REM}.9.{idx}", "4", sys_name),
        (f"{LLDP_REM}.10.{idx}", "4", sys_descr),
        (f"{LLDP_REM}.11.{idx}", "4x", "0014"),
        (f"{LLDP_REM}.12.{idx}", "4x", "0004"),
    ]
    if mgmt_ipv4:
        a = f"{idx}.1.4.{mgmt_ipv4}"  # AddrSubtype 1 (IPv4), length 4, address octets
        out += [
            (f"{LLDP_MAN}.3.{a}", "2", "2"),  # lldpRemManAddrIfSubtype ifIndex
            (f"{LLDP_MAN}.4.{a}", "2", "0"),
            (f"{LLDP_MAN}.5.{a}", "6", "0.0"),
        ]
    return out


def write(rel: str, records: list[tuple[str, str, str]]) -> None:
    key = lambda r: tuple(int(x) for x in r[0].split("."))  # noqa: E731
    seen = set()
    for oid, _, _ in records:
        assert oid not in seen, oid
        seen.add(oid)
    path = HERE / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{o}|{t}|{v}\n" for o, t, v in sorted(records, key=key)))
    print(f"wrote {path.relative_to(HERE)} ({len(records)} records)")


ARUBA_DESCR = "Aruba JL256A 2930F-48G-4SFP+ Switch, revision WC.16.11.0012, ROM WC.16.01.0010 (/ws/swbuildm/rel_tacoma_qaoff/code/build/lvm(swbuildm_rel_tacoma_qaoff_rel_tacoma))"
COMWARE_DESCR = "HPE Comware Platform Software, Software Version 7.1.070, Release 6328 HPE 1950-24G-2SFP+-2XGT Switch"
ACC2_DESCR = "HP J9773A 2530-24G-PoEP Switch, revision YA.16.10.0010, ROM YA.15.20 (/ws/swbuildm/rel_yakima_qaoff/code/build/lvm(swbuildm_rel_yakima_qaoff_rel_yakima))"
AP_DESCR = "Linux U6-Lite 4.4.153 #1 SMP Wed Jul 5 2023 mips"

# ------------------------------------------------------------------ 127.0.0.51 ArubaOS-Switch "core-sw"
HPICF_ROWS = [  # index, descr, status (unknown 1, bad 2, warning 3, good 4, notPresent 5)
    (1, "Fan Sensor", 4),
    (2, "Power Supply Sensor", 2),
    (3, "Over-temperature Sensor", 4),
    (4, "Redundant Power Supply Sensor", 5),
]
hpicf = []
for i, descr, st in HPICF_ROWS:
    hpicf += [
        (f"{HPICF_SENSOR}.1.{i}", "2", str(i)),
        (f"{HPICF_SENSOR}.2.{i}", "6", f"1.3.6.1.4.1.11.2.3.7.8.3.{i}"),
        (f"{HPICF_SENSOR}.3.{i}", "2", "1"),
        (f"{HPICF_SENSOR}.4.{i}", "2", str(st)),
        (f"{HPICF_SENSOR}.5.{i}", "65", "0"),
        (f"{HPICF_SENSOR}.6.{i}", "65", "1" if st == 2 else "0"),
        (f"{HPICF_SENSOR}.7.{i}", "4", descr),
    ]

aruba = (
    system(ARUBA_DESCR, "1.3.6.1.4.1.11.2.3.7.11.181", "core-sw", 55_500_000)
    + interfaces(
        [
            dict(idx=1, name="1", mac="001a1e000101", rate_in=2_500_000, rate_out=1_250_000),
            dict(idx=2, name="2", mac="001a1e000102", rate_in=500_000, rate_out=250_000),
            dict(idx=3, name="3", mac="001a1e000103", oper=2),
            dict(idx=4, name="4", mac="001a1e000104", oper=2),
            dict(idx=1001, name="DEFAULT_VLAN", type=53, speed=0, mac=MAC_CORE),
        ]
    )
    + [(HP_CPU, "2", "7")]
    + [
        (f"{HP_MEM}.1.1", "2", "1"),
        (f"{HP_MEM}.5.1", "2", "330000000"),  # hpLocalMemTotalBytes
        (f"{HP_MEM}.6.1", "2", "200000000"),  # hpLocalMemFreeBytes
        (f"{HP_MEM}.7.1", "2", "130000000"),  # hpLocalMemAllocBytes -> 39.39 %
    ]
    + hpicf
    + [
        (f"{PETH_MAIN}.2.1", "66", "370"),  # pethMainPsePower (W)
        (f"{PETH_MAIN}.3.1", "2", "1"),  # pethMainPseOperStatus on
        (f"{PETH_MAIN}.4.1", "66", "45"),  # pethMainPseConsumptionPower -> 12.16 %
    ]
    + lldp_local(MAC_CORE, "core-sw", ARUBA_DESCR, [(n, 7, str(n), str(n)) for n in range(1, 5)])
    + lldp_remote(1, 1, MAC_ACC1, 5, "GigabitEthernet1/0/1", "GigabitEthernet1/0/1 Interface", "access-1",
                  COMWARE_DESCR, mgmt_ipv4="127.0.0.52")
    + lldp_remote(2, 2, MAC_ACC2, 7, "24", "24", "access-2", ACC2_DESCR)
)
write("arubaos/public.snmprec", aruba)

# ------------------------------------------------------------------ 127.0.0.52 Comware "access-1"
ENT_ROWS = [  # entPhysicalIndex, descr, class, name
    (1, "HPE 1950-24G-2SFP+-2XGT Switch Chassis", 3, "Chassis"),
    (2, "HPE 1950-24G-2SFP+-2XGT Switch Board", 9, "Board"),
    (3, "Fan", 7, "Fan 1"),
    (4, "Fan", 7, "Fan 2"),
    (5, "Power Supply Unit", 6, "PSU 1"),
]
ent = []
for i, descr, cls, name in ENT_ROWS:
    ent += [
        (f"{ENT_PHYS}.2.{i}", "4", descr),
        (f"{ENT_PHYS}.4.{i}", "2", "0" if i == 1 else "1"),  # entPhysicalContainedIn
        (f"{ENT_PHYS}.5.{i}", "2", str(cls)),
        (f"{ENT_PHYS}.7.{i}", "4", name),
    ]
hh3c = [
    (f"{HH3C_EXT}.6.1", "2", "0"),
    (f"{HH3C_EXT}.6.2", "2", "12"),  # hh3cEntityExtCpuUsage Board
    (f"{HH3C_EXT}.6.3", "2", "0"),
    (f"{HH3C_EXT}.6.4", "2", "0"),
    (f"{HH3C_EXT}.6.5", "2", "0"),
    (f"{HH3C_EXT}.8.2", "2", "41"),  # hh3cEntityExtMemUsage Board
    (f"{HH3C_EXT}.12.1", "2", "0"),
    (f"{HH3C_EXT}.12.2", "2", "43"),  # hh3cEntityExtTemperature Board
    (f"{HH3C_EXT}.19.1", "2", "1"),  # notSupported (chassis)
    (f"{HH3C_EXT}.19.3", "2", "2"),  # Fan 1 normal
    (f"{HH3C_EXT}.19.4", "2", "41"),  # Fan 2 fanError
    (f"{HH3C_EXT}.19.5", "2", "2"),  # PSU 1 normal
]
comware = (
    system(COMWARE_DESCR, "1.3.6.1.4.1.25506.11.1.177", "access-1", 33_300_000)
    + interfaces(
        [
            dict(idx=1, name="GigabitEthernet1/0/1", descr="GigabitEthernet1/0/1 Interface", mac="3c8c40000101",
                 rate_in=1_250_000, rate_out=2_500_000),
            dict(idx=2, name="GigabitEthernet1/0/2", descr="GigabitEthernet1/0/2 Interface", mac="3c8c40000102",
                 rate_in=250_000, rate_out=750_000),
            dict(idx=3, name="GigabitEthernet1/0/3", descr="GigabitEthernet1/0/3 Interface", mac="3c8c40000103", oper=2),
            dict(idx=4, name="GigabitEthernet1/0/4", descr="GigabitEthernet1/0/4 Interface", mac="3c8c40000104", oper=2),
            # Indexes 101/102/103 deliberately collide with the LLDP local port numbers 101-103: resolving
            # Comware ports by "lldpLocPortNum is an ifIndex" first (rule 3 before rule 1) maps them wrongly.
            dict(idx=101, name="NULL0", type=1, speed=0),
            dict(idx=102, name="InLoopBack0", type=24, speed=0),
            dict(idx=103, name="Vlan-interface1", type=136, speed=0, mac=MAC_ACC1),
        ]
    )
    + ent
    + hh3c
    + [
        (f"{PETH_MAIN}.2.1", "66", "0"),  # pethMainPsePower 0 -> PoE rows filtered / skip_zero
        (f"{PETH_MAIN}.3.1", "2", "2"),  # off
        (f"{PETH_MAIN}.4.1", "66", "0"),
    ]
    + lldp_local(MAC_ACC1, "access-1", COMWARE_DESCR,
                 [(100 + n, 5, f"GigabitEthernet1/0/{n}", f"GigabitEthernet1/0/{n} Interface") for n in range(1, 5)])
    + lldp_remote(101, 1, MAC_CORE, 7, "1", "1", "core-sw", ARUBA_DESCR, mgmt_ipv4="127.0.0.51")
    + lldp_remote(102, 2, MAC_AP_AS_SEEN, 5, "eth0", "eth0", "U6-Lite-Office", AP_DESCR)
)
write("comware/public.snmprec", comware)

# ------------------------------------------------------------------ 127.0.0.53 ArubaOS-Switch "access-2"
def access2(with_printer: bool) -> list[tuple[str, str, str]]:
    recs = (
        system(ACC2_DESCR, "1.3.6.1.4.1.11.2.3.7.11.137", "access-2", 22_200_000)
        + interfaces(
            [
                dict(idx=1, name="1", mac="001a1e000201", oper=2),
                dict(idx=2, name="2", mac="001a1e000202", oper=2),
                dict(idx=3, name="3", mac="001a1e000203", rate_in=12_500, rate_out=2_500),
                dict(idx=24, name="24", mac="001a1e000224", rate_in=250_000, rate_out=500_000),
            ]
        )
        + lldp_local(MAC_ACC2, "access-2", ACC2_DESCR, [(n, 7, str(n), str(n)) for n in (1, 2, 3, 24)])
        + lldp_remote(24, 1, MAC_CORE, 7, "2", "2", "core-sw", ARUBA_DESCR)
    )
    if with_printer:
        recs += lldp_remote(3, 2, MAC_PRINTER, 3, "001122334455", "Ethernet", "printer-3f",
                            "HP LaserJet Pro M404dn", mgmt_ipv4="127.0.0.99")
    return recs


write("access2/public.snmprec", access2(True))
write("access2_noprinter/public.snmprec", access2(False))

# ------------------------------------------------------------------ 127.0.0.54 UniFi AP "U6-Lite-Office"
write(
    "unifi_lldp/public.snmprec",
    system(AP_DESCR, "1.3.6.1.4.1.8072.3.2.10", "U6-Lite-Office", 9_900_000)
    + interfaces(
        [
            dict(idx=1, name="lo", type=24, speed=0, rate_in=50, rate_out=50),
            dict(idx=2, name="eth0", type=6, mac=MAC_AP, rate_in=750_000, rate_out=250_000),
            dict(idx=3, name="br0", type=209, speed=0, mac=MAC_AP, rate_in=700_000, rate_out=240_000),
        ]
    )
    + [(f"{HR_PROC}.196608", "2", "17")]
    + [
        (f"{HR_STORAGE}.1.1", "2", "1"),
        (f"{HR_STORAGE}.2.1", "6", HR_RAM),
        (f"{HR_STORAGE}.3.1", "4", "Physical memory"),
        (f"{HR_STORAGE}.4.1", "2", "1024"),
        (f"{HR_STORAGE}.5.1", "2", "1000000"),
        (f"{HR_STORAGE}.6.1", "2", "400000"),
    ]
    + lldp_local(MAC_AP, "U6-Lite-Office", AP_DESCR, [(2, 5, "eth0", "eth0")])
    + lldp_remote(2, 1, MAC_ACC1, 5, "GigabitEthernet1/0/2", "GigabitEthernet1/0/2 Interface", "access-1",
                  COMWARE_DESCR)
)
