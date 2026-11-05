#!/usr/bin/env python3
"""Generates the snmprec files of the Phase 2b-2 vendor template lab (docs/architecture.md §11.6).

    python3 tests/sim/vendors/gen_snmprec.py    # rewrites */public.snmprec next to this script

All agents: SNMP v2c, community `public`, udp/1161 (tests/sim/lab.sh, LAB_2B2=1). Files are committed.

Contract sims (architecture §11.6 "Vendor template sims"):
  127.0.0.71  checkpoint/          Check Point standalone appliance "gw-hq" (enterprise 2620)
  127.0.0.72  checkpoint_cluster/  ClusterXL member VM "gw-cl1": net-snmp Linux sysObjectID + svn subtree
  127.0.0.73  aruba_instant/       Aruba Instant virtual controller "instant-vc" (AI-AP-MIB)

Synthetic sims (one per new vendor template, data-model § New vendor templates): every OID the template
reads, scalars with a value that passes probes/`when`, table columns with two rows (suffix 1, 2; mikrotik's
shared gauge table has 3, one per unit filter) that pass the filters, label columns with text. Applying the
template must end with no `probe_failed` / `no_rows` / `snmp_error` / `not_applicable` (except the http
"Web" probe of hpe_ilo / dell_idrac: nothing listens on 443 there). The OIDs are written from the contract
tables, so a mismatch also catches a YAML OID that differs from the contract.
  127.0.0.101 synthetic/aruba_cx        127.0.0.106 synthetic/paloalto_panos
  127.0.0.102 synthetic/aruba_wifi      127.0.0.107 synthetic/mikrotik_routeros
  127.0.0.103 synthetic/checkpoint_gaia 127.0.0.108 synthetic/apc_ups
  127.0.0.104 synthetic/fortinet_fortigate 127.0.0.109 synthetic/hpe_ilo
  127.0.0.105 synthetic/cisco_ios       127.0.0.110 synthetic/dell_idrac
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
Rec = tuple[str, str, str]

SYS = "1.3.6.1.2.1.1"
IF = "1.3.6.1.2.1.2.2.1"
IFX = "1.3.6.1.2.1.31.1.1.1"
HR_STORAGE = "1.3.6.1.2.1.25.2.3.1"
HR_PROC = "1.3.6.1.2.1.25.3.3.1.2"
HR_RAM, HR_DISK = "1.3.6.1.2.1.25.2.1.2", "1.3.6.1.2.1.25.2.1.4"
CP = "1.3.6.1.4.1.2620"


def system(descr: str, oid: str, name: str) -> list[Rec]:
    return [
        (f"{SYS}.1.0", "4", descr),
        (f"{SYS}.2.0", "6", oid),
        (f"{SYS}.3.0", "67:numeric", "initial=12345600,rate=100"),
        (f"{SYS}.4.0", "4", "qa@netmon.local"),
        (f"{SYS}.5.0", "4", name),
        (f"{SYS}.6.0", "4", "QA vendor lab"),
        (f"{SYS}.7.0", "2", "78"),
    ]


def interfaces(rows: list[tuple[int, str, int, int]]) -> list[Rec]:
    """(ifIndex, name, type, oper)."""
    out: list[Rec] = [("1.3.6.1.2.1.2.1.0", "2", str(len(rows)))]
    for i, name, typ, oper in rows:
        out += [
            (f"{IF}.1.{i}", "2", str(i)),
            (f"{IF}.2.{i}", "4", name),
            (f"{IF}.3.{i}", "2", str(typ)),
            (f"{IF}.5.{i}", "66", "1000000000" if typ == 6 else "0"),
            (f"{IF}.6.{i}", "4x", f"00163e0000{i:02x}" if typ != 24 else ""),
            (f"{IF}.7.{i}", "2", "1"),
            (f"{IF}.8.{i}", "2", str(oper)),
            (f"{IF}.10.{i}", "65", "1000"),
            (f"{IF}.14.{i}", "65", "0"),
            (f"{IF}.16.{i}", "65", "2000"),
            (f"{IF}.20.{i}", "65", "0"),
            (f"{IFX}.1.{i}", "4", name),
            (f"{IFX}.6.{i}", "70", "1000"),
            (f"{IFX}.10.{i}", "70", "2000"),
            (f"{IFX}.15.{i}", "66", "1000" if typ == 6 else "0"),
            (f"{IFX}.18.{i}", "4", ""),
        ]
    return out


def hr(cpu: list[int] | None = (17,), ram: tuple[int, int] | None = (1_000_000, 400_000),
       disks: list[tuple[int, str, int, int]] = ()) -> list[Rec]:
    """hrProcessorLoad rows, RAM (size, used in 1 KiB units), disks (index, descr, size, used in 4 KiB)."""
    out: list[Rec] = []
    for n, v in enumerate(cpu or []):
        out.append((f"{HR_PROC}.{196608 + n}", "2", str(v)))
    rows = ([(1, HR_RAM, "Physical memory", 1024, *ram)] if ram else []) + [
        (i, HR_DISK, d, 4096, s, u) for i, d, s, u in disks
    ]
    for i, typ, descr, unit, size, used in rows:
        out += [
            (f"{HR_STORAGE}.1.{i}", "2", str(i)),
            (f"{HR_STORAGE}.2.{i}", "6", typ),
            (f"{HR_STORAGE}.3.{i}", "4", descr),
            (f"{HR_STORAGE}.4.{i}", "2", str(unit)),
            (f"{HR_STORAGE}.5.{i}", "2", str(size)),
            (f"{HR_STORAGE}.6.{i}", "2", str(used)),
        ]
    return out


def table(col: str, rows: dict[int | str, tuple[str, str]]) -> list[Rec]:
    return [(f"{col}.{sfx}", t, v) for sfx, (t, v) in rows.items()]


def two(col: str, tag: str, v1: str, v2: str | None = None) -> list[Rec]:
    return [(f"{col}.1", tag, v1), (f"{col}.2", tag, v1 if v2 is None else v2)]


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


# ================================================================== contract sims
CP_DESCR = "Linux gw-hq 3.10.0-1160.15.2cpx86_64 #1 SMP Wed Jun 1 12:00:00 IDT 2022 x86_64"
CP_IFS = [(1, "eth0", 6, 1), (2, "eth1", 6, 1), (3, "eth2", 6, 2), (4, "lo", 24, 1)]
CP_DISKS = [(31, "/", 4_000_000, 1_000_000), (32, "/var/log", 8_000_000, 2_000_000)]

# 127.0.0.71 Check Point standalone appliance
write(
    "checkpoint/public.snmprec",
    system(CP_DESCR, f"{CP}.1.6.123.1.49", "gw-hq")
    + interfaces(CP_IFS)
    + hr(cpu=None, ram=None, disks=CP_DISKS)
    + [
        (f"{CP}.1.1.25.3.0", "2", "12345"),  # fwNumConn
        (f"{CP}.1.1.25.10.0", "2", "0"),  # fwConnTableLimit 0 = automatic
        (f"{CP}.1.5.2.0", "2", "0"),  # haInstalled
        (f"{CP}.1.6.4.1.0", "4", "R81.20"),  # svnVersion (svn subtree 1.3.6.1.4.1.2620.1.6)
        (f"{CP}.1.6.7.2.4.0", "66", "23"),  # procUsage
        (f"{CP}.1.6.7.4.3.0", "70", "16000000000"),  # memTotalReal64
        (f"{CP}.1.6.7.4.4.0", "70", "4000000000"),  # memActiveReal64 -> 25 %
    ]
    + table(f"{CP}.1.6.7.8.1.1.2", {1: ("4", "CPU"), 2: ("4", "Intake")})
    + table(f"{CP}.1.6.7.8.1.1.3", {1: ("4", "44.0"), 2: ("4", "38.5")})
    + table(f"{CP}.1.6.7.8.2.1.2", {1: ("4", "Fan 1"), 2: ("4", "Fan 2")})
    + table(f"{CP}.1.6.7.8.2.1.6", {1: ("2", "0"), 2: ("2", "1")})
    + table(f"{CP}.1.6.7.9.1.1.1", {1: ("2", "1"), 2: ("2", "2")})
    + table(f"{CP}.1.6.7.9.1.1.2", {1: ("4", "Up"), 2: ("4", "Down")})
    + table(f"{CP}.500.9002.1.3", {1: ("2", "3"), 2: ("2", "3"), 3: ("2", "131")})  # tunnelState
)

# 127.0.0.72 ClusterXL member (VM): net-snmp Linux sysObjectID, classified only by the `checkpoint` probe
write(
    "checkpoint_cluster/public.snmprec",
    system("Linux gw-cl1 3.10.0-1160.15.2cpx86_64 #1 SMP Wed Jun 1 12:00:00 IDT 2022 x86_64",
           "1.3.6.1.4.1.8072.3.2.10", "gw-cl1")
    + interfaces(CP_IFS)
    + hr(cpu=None, ram=None, disks=CP_DISKS)
    + [
        (f"{CP}.1.1.25.3.0", "2", "321"),
        (f"{CP}.1.1.25.10.0", "2", "0"),
        (f"{CP}.1.5.2.0", "2", "1"),  # haInstalled
        (f"{CP}.1.5.6.0", "4", "active"),  # haState
        (f"{CP}.1.5.101.0", "2", "0"),  # haStatCode OK
        (f"{CP}.1.6.4.1.0", "4", "R81.20"),
        (f"{CP}.1.6.7.2.4.0", "66", "41"),
        (f"{CP}.1.6.7.4.3.0", "70", "8000000000"),
        (f"{CP}.1.6.7.4.4.0", "70", "2000000000"),
    ]
)

# 127.0.0.73 Aruba Instant virtual controller
AI = "1.3.6.1.4.1.14823.2.3.3.1.2"
APS = [("20:a6:cd:00:01:01", "AP-Lobby", 1), ("20:a6:cd:00:01:02", "AP-Office", 1), ("20:a6:cd:00:01:03", "AP-Store", 2)]
CLIENTS = [f"3c:22:fb:00:00:0{n}" for n in range(1, 6)]


def arcs(mac: str) -> str:
    return ".".join(str(int(x, 16)) for x in mac.split(":"))


instant = system("ArubaOS (MODEL: 515), Version 8.10.0.6", "1.3.6.1.4.1.14823.1.2.111", "instant-vc") + interfaces(
    [(1, "bond0", 6, 1), (2, "eth0", 6, 1)]
)
for mac, name, st in APS:
    instant += [
        (f"{AI}.1.1.1.{arcs(mac)}", "4x", mac.replace(":", "")),  # aiAPMACAddress
        (f"{AI}.1.1.2.{arcs(mac)}", "4", name),  # aiAPName
        (f"{AI}.1.1.11.{arcs(mac)}", "2", str(st)),  # aiAPStatus up(1)/down(2)
    ]
for mac in CLIENTS:
    instant += [
        (f"{AI}.4.1.1.{arcs(mac)}", "4x", mac.replace(":", "")),  # aiClientMACAddress
        (f"{AI}.4.1.2.{arcs(mac)}", "4x", APS[0][0].replace(":", "")),  # aiClientWlanMACAddress
    ]
write("aruba_instant/public.snmprec", instant)

# ================================================================== synthetic sims (one per new template)
ETH = interfaces([(1, "lo", 24, 1), (2, "eth0", 6, 1), (3, "eth1", 6, 1)])
PETH = "1.3.6.1.2.1.105.1.3.1.1"
SYNTH: dict[str, list[Rec]] = {}

SYNTH["aruba_cx"] = (
    system("Aruba JL658A 6300M 24SFP+ 4SFP56 Swch FL.10.13.1000", "1.3.6.1.4.1.47196.4.1.1.1.200", "syn-cx")
    + ETH + hr()
    + two("1.3.6.1.2.1.99.1.1.1.1", "2", "8")  # entPhySensorType celsius
    + two("1.3.6.1.2.1.99.1.1.1.4", "2", "41", "38")  # entPhySensorValue
    + two("1.3.6.1.2.1.99.1.1.1.5", "2", "1")  # entPhySensorOperStatus ok
    + two("1.3.6.1.2.1.47.1.1.1.1.7", "4", "Sensor A", "Sensor B")  # entPhysicalName
    + two(f"{PETH}.2", "66", "370")
    + two(f"{PETH}.3", "2", "1")
    + two(f"{PETH}.4", "66", "45")
)
SYNTH["aruba_wifi"] = (
    system("ArubaOS (MODEL: 515), Version 8.10.0.6", "1.3.6.1.4.1.14823.1.2.111", "syn-aruba-wifi")
    + ETH + hr()
    + two(f"{AI}.1.1.11", "2", "1")
    + two(f"{AI}.1.1.2", "4", "AP-One", "AP-Two")
    + two(f"{AI}.4.1.1", "4x", "3c22fb000001", "3c22fb000002")
    + two("1.3.6.1.4.1.14823.2.2.1.5.2.1.4.1.19", "2", "1")  # wlanAPStatus
    + two("1.3.6.1.4.1.14823.2.2.1.5.2.1.4.1.3", "4", "MC-AP-1", "MC-AP-2")  # wlanAPName
    + [("1.3.6.1.4.1.14823.2.2.1.1.3.2.0", "2", "7")]  # wlsxSwitchTotalNumStationsAssociated
)
SYNTH["checkpoint_gaia"] = (
    system(CP_DESCR, f"{CP}.1.6.123.1.49", "syn-checkpoint")
    + ETH + hr(cpu=None, ram=None, disks=CP_DISKS)
    + [
        (f"{CP}.1.1.25.3.0", "2", "5000"),
        (f"{CP}.1.1.25.10.0", "2", "100000"),
        (f"{CP}.1.5.2.0", "2", "1"),
        (f"{CP}.1.5.6.0", "4", "active"),
        (f"{CP}.1.5.101.0", "2", "0"),
        (f"{CP}.1.6.7.2.4.0", "66", "11"),
        (f"{CP}.1.6.7.4.3.0", "70", "16000000000"),
        (f"{CP}.1.6.7.4.4.0", "70", "8000000000"),
    ]
    + two(f"{CP}.500.9002.1.3", "2", "3")
    + two(f"{CP}.1.6.7.8.1.1.2", "4", "CPU", "Intake")
    + two(f"{CP}.1.6.7.8.1.1.3", "4", "41.0", "35.5")
    + two(f"{CP}.1.6.7.8.2.1.2", "4", "Fan 1", "Fan 2")
    + two(f"{CP}.1.6.7.8.2.1.6", "2", "0")
    + two(f"{CP}.1.6.7.9.1.1.1", "2", "1", "2")
    + two(f"{CP}.1.6.7.9.1.1.2", "4", "Up")
)
FG = "1.3.6.1.4.1.12356.101"
SYNTH["fortinet_fortigate"] = (
    system("FortiGate-60F v7.2.8,build1639", "1.3.6.1.4.1.12356.101.1.641", "syn-fortigate")
    + ETH
    + [
        (f"{FG}.4.1.3.0", "66", "9"),  # fgSysCpuUsage
        (f"{FG}.4.1.4.0", "66", "44"),  # fgSysMemUsage
        (f"{FG}.4.1.6.0", "66", "1200"),  # fgSysDiskUsage
        (f"{FG}.4.1.7.0", "66", "4800"),  # fgSysDiskCapacity
        (f"{FG}.4.1.8.0", "66", "321"),  # fgSysSesCount
        (f"{FG}.13.1.1.0", "2", "1"),  # fgHaSystemMode standalone
    ]
    + two(f"{FG}.13.2.1.1.12", "2", "1")  # fgHaStatsSyncStatus
    + two(f"{FG}.13.2.1.1.11", "4", "fg-a", "fg-b")
    + two(f"{FG}.12.2.2.1.20", "2", "2")  # fgVpnTunEntStatus up
)
CI = "1.3.6.1.4.1.9.9"
SYNTH["cisco_ios"] = (
    system("Cisco IOS Software, ISR4300 Software (X86_64_LINUX_IOSD-UNIVERSALK9-M), Version 16.9.4",
           "1.3.6.1.4.1.9.1.2068", "syn-cisco")
    + ETH
    + two(f"{CI}.109.1.1.1.1.8", "66", "12", "8")  # cpmCPUTotal5minRev
    + two(f"{CI}.48.1.1.1.2", "4", "Processor", "I/O")  # ciscoMemoryPoolName
    + two(f"{CI}.48.1.1.1.5", "66", "300000000", "20000000")  # Used
    + two(f"{CI}.48.1.1.1.6", "66", "700000000", "80000000")  # Free
    + two(f"{CI}.13.1.3.1.2", "4", "Inlet", "Outlet")
    + two(f"{CI}.13.1.3.1.3", "66", "31", "36")
    + two(f"{CI}.13.1.3.1.6", "2", "1")
    + two(f"{CI}.13.1.4.1.2", "4", "Fan 1", "Fan 2")
    + two(f"{CI}.13.1.4.1.3", "2", "1")
    + two(f"{CI}.13.1.5.1.2", "4", "PS 1", "PS 2")
    + two(f"{CI}.13.1.5.1.3", "2", "1")
)
PAN = "1.3.6.1.4.1.25461.2.1.2"
SYNTH["paloalto_panos"] = (
    system("Palo Alto Networks PA-440 series firewall", "1.3.6.1.4.1.25461.2.3.53", "syn-panos")
    + ETH + hr(cpu=None, ram=None)
    + two(HR_PROC, "2", "5", "20")
    + [
        (f"{PAN}.3.1.0", "66", "3"),  # panSessionUtilization
        (f"{PAN}.3.3.0", "66", "1234"),  # panSessionActive
        (f"{PAN}.1.11.0", "4", "active"),  # panSysHAState
        (f"{PAN}.1.12.0", "4", "passive"),  # panSysHAPeerState
    ]
)
MT = "1.3.6.1.4.1.14988.1.1.3"
SYNTH["mikrotik_routeros"] = (
    system("RouterOS RB4011iGS+", "1.3.6.1.4.1.14988.1", "syn-mikrotik")
    + ETH + hr()
    + [(f"{MT}.8.0", "2", "240"), (f"{MT}.10.0", "2", "450"), (f"{MT}.11.0", "2", "520")]
    # mtxrGaugeTable: one row per unit filter (celsius 1, rpm 2, dV 3)
    + table(f"{MT}.100.1.2", {1: ("4", "cpu-temperature"), 2: ("4", "fan1-speed"), 3: ("4", "psu-voltage")})
    + table(f"{MT}.100.1.3", {1: ("2", "48"), 2: ("2", "3000"), 3: ("2", "240")})
    + table(f"{MT}.100.1.4", {1: ("2", "1"), 2: ("2", "2"), 3: ("2", "3")})
)
APC = "1.3.6.1.4.1.318.1.1.1"
SYNTH["apc_ups"] = (
    system("APC Web/SNMP Management Card (MB:v4.1.0 PF:v6.9.6 PN:apc_hw05_aos_696.bin)",
           "1.3.6.1.4.1.318.1.3.27", "syn-apc")
    + [
        (f"{APC}.2.1.1.0", "2", "2"),  # upsBasicBatteryStatus normal
        (f"{APC}.2.2.1.0", "66", "100"),  # upsAdvBatteryCapacity
        (f"{APC}.2.2.2.0", "66", "25"),  # upsAdvBatteryTemperature
        (f"{APC}.2.2.3.0", "67", "360000"),  # upsAdvBatteryRunTimeRemaining (60 min)
        (f"{APC}.2.2.4.0", "2", "1"),  # upsAdvBatteryReplaceIndicator ok
        (f"{APC}.4.1.1.0", "2", "2"),  # upsBasicOutputStatus on line
        (f"{APC}.4.2.3.0", "66", "30"),  # upsAdvOutputLoad
    ]
)
CPQ = "1.3.6.1.4.1.232"
SYNTH["hpe_ilo"] = (
    system("Integrated Lights-Out 5 2.72 Sep 04 2022", "1.3.6.1.4.1.232.9.4.11", "syn-ilo")
    + ETH
    + [(f"{CPQ}.6.1.3.0", "2", "2")]  # cpqHeMibCondition ok
    + two(f"{CPQ}.6.2.9.3.1.3", "2", "3")  # PowerSupplyPresent present
    + two(f"{CPQ}.6.2.9.3.1.4", "2", "2")  # PowerSupplyCondition ok
    + two(f"{CPQ}.6.2.6.7.1.4", "2", "3")  # FanPresent
    + two(f"{CPQ}.6.2.6.7.1.9", "2", "2")  # FanCondition
    + two(f"{CPQ}.6.2.6.8.1.4", "2", "21", "40")  # TemperatureCelsius
    + two(f"{CPQ}.3.2.5.1.1.37", "2", "2")  # cpqDaPhyDrvCondition
)
DELL = "1.3.6.1.4.1.674.10892.5"
SYNTH["dell_idrac"] = (
    system("Dell Out-of-band SNMP Agent for Remote Access Controller", "1.3.6.1.4.1.674.10892.5", "syn-idrac")
    + ETH
    + [(f"{DELL}.2.1.0", "2", "3")]  # globalSystemStatus ok
    + two(f"{DELL}.4.600.12.1.5", "2", "3")
    + two(f"{DELL}.4.600.12.1.8", "4", "PS1 Status", "PS2 Status")
    + two(f"{DELL}.4.700.12.1.5", "2", "3")
    + two(f"{DELL}.4.700.12.1.8", "4", "System Board Fan1", "System Board Fan2")
    + two(f"{DELL}.4.700.20.1.6", "2", "250", "400")
    + two(f"{DELL}.4.700.20.1.8", "4", "System Board Inlet Temp", "CPU1 Temp")
    + two(f"{DELL}.5.1.20.130.4.1.24", "2", "3")
    + two(f"{DELL}.5.1.20.130.4.1.2", "4", "Physical Disk 0:1:0", "Physical Disk 0:1:1")
)

for tid, recs in SYNTH.items():
    write(f"synthetic/{tid}/public.snmprec", recs)
