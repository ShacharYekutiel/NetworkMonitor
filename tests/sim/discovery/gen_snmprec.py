#!/usr/bin/env python3
"""Generates the snmprec files of the Phase 2a discovery lab (architecture.md §9.6).

    python3 tests/sim/discovery/gen_snmprec.py          # rewrites the *.snmprec files next to this script

The generated files are committed; this script documents how they were made and keeps the OIDs
sorted (snmpsim needs lexicographically ordered OIDs for GETNEXT/GETBULK to work).

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
HR_VMEM = "1.3.6.1.2.1.25.2.1.3"
HR_FIXED = "1.3.6.1.2.1.25.2.1.4"
UNIFI_VAP = "1.3.6.1.4.1.41112.1.6.1.2.1"
PRT_LIFE = "1.3.6.1.2.1.43.10.2.1.4"
PRT_SUP = "1.3.6.1.2.1.43.11.1.1"


def system(descr: str, oid: str, name: str, uptime: int) -> list[tuple[str, str, str]]:
    return [
        (f"{SYS}.1.0", "4", descr),
        (f"{SYS}.2.0", "6", oid),
        (f"{SYS}.3.0", "67:numeric", f"initial={uptime},rate=100"),
        (f"{SYS}.4.0", "4", "qa@netmon.local"),
        (f"{SYS}.5.0", "4", name),
        (f"{SYS}.6.0", "4", "QA discovery lab"),
        (f"{SYS}.7.0", "2", "72"),
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
            (f"{IF}.2.{i}", "4", r["name"]),
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


def storage(rows: list[dict]) -> list[tuple[str, str, str]]:
    """rows: {idx, type oid, descr, units, size, used}."""
    out = []
    for r in rows:
        i = r["idx"]
        out += [
            (f"{HR_STORAGE}.1.{i}", "2", str(i)),
            (f"{HR_STORAGE}.2.{i}", "6", r["type"]),
            (f"{HR_STORAGE}.3.{i}", "4", r["descr"]),
            (f"{HR_STORAGE}.4.{i}", "2", str(r["units"])),
            (f"{HR_STORAGE}.5.{i}", "2", str(r["size"])),
            (f"{HR_STORAGE}.6.{i}", "2", str(r["used"])),
        ]
    return out


def processors(loads: list[int]) -> list[tuple[str, str, str]]:
    return [(f"{HR_PROC}.{196608 + n}", "2", str(v)) for n, v in enumerate(loads)]


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


# --- 127.0.0.21 OPNsense (net-snmp on FreeBSD) -------------------------------------------------
OPNSENSE_DESCR = (
    "FreeBSD OPNsense.localdomain 14.1-RELEASE-p5 FreeBSD 14.1-RELEASE-p5 stable/24.7-n267758-abc amd64"
)
write(
    "opnsense/public.snmprec",
    system(OPNSENSE_DESCR, "1.3.6.1.4.1.8072.3.2.8", "OPNsense.localdomain", 4_200_000)
    + interfaces(
        [
            dict(idx=1, name="igb0", type=6, mac="a0369f000001", rate_in=2_500_000, rate_out=500_000, alias="WAN"),
            dict(idx=2, name="igb1", type=6, mac="a0369f000002", rate_in=1_250_000, rate_out=3_000_000, alias="LAN"),
            dict(idx=3, name="lo0", type=24, speed=0, rate_in=100, rate_out=100),
            dict(idx=4, name="pflog0", type=246, speed=0),
            dict(idx=5, name="enc0", type=1, speed=0, oper=2),
        ]
    )
    + processors([10, 30])
    + storage(
        [
            dict(idx=1, type=HR_RAM, descr="Physical memory", units=4096, size=2_000_000, used=500_000),
            dict(idx=10, type=HR_VMEM, descr="Swap space", units=4096, size=524_288, used=0),
            # "/" is index 31 like a real OPNsense; 25 % used of ~114 GB.
            dict(idx=31, type=HR_FIXED, descr="/", units=4096, size=28_000_000, used=7_000_000),
            dict(idx=32, type=HR_FIXED, descr="/boot/efi", units=512, size=532_480, used=2_048),
        ]
    ),
)

# --- 127.0.0.22 UniFi AP (U6-Lite, net-snmp Linux OID + UBNT-UniFi-MIB VAP table) -------------
VAPS = [  # index, essid, num stations, radio
    (1, "HomeWiFi", 5, "ng"),
    (2, "HomeWiFi", 12, "na"),
]
unifi_vap = []
for i, essid, sta, radio in VAPS:
    unifi_vap += [
        (f"{UNIFI_VAP}.1.{i}", "2", str(i)),  # unifiVapIndex
        (f"{UNIFI_VAP}.2.{i}", "2", "0"),  # unifiVapChannel (placeholder)
        (f"{UNIFI_VAP}.6.{i}", "4", essid),  # unifiVapEssId
        (f"{UNIFI_VAP}.7.{i}", "4", f"ath{i - 1}"),  # unifiVapName
        (f"{UNIFI_VAP}.8.{i}", "66", str(sta)),  # unifiVapNumStations (Gauge32)
        (f"{UNIFI_VAP}.9.{i}", "4", radio),  # unifiVapRadio
    ]
write(
    "unifi_ap/public.snmprec",
    system("Linux U6-Lite 4.4.153 #1 SMP ... mips", "1.3.6.1.4.1.8072.3.2.10", "U6-Lite-Office", 9_900_000)
    + interfaces(
        [
            dict(idx=1, name="lo", type=24, speed=0, rate_in=50, rate_out=50),
            dict(idx=2, name="eth0", type=6, mac="7483c2000001", rate_in=750_000, rate_out=250_000),
            dict(idx=3, name="br0", type=209, speed=0, mac="7483c2000001", rate_in=700_000, rate_out=240_000),
            dict(idx=4, name="ath0", type=71, speed=0, mac="7483c2000002", rate_in=100_000, rate_out=600_000),
        ]
    )
    + processors([17])
    + storage([dict(idx=1, type=HR_RAM, descr="Physical memory", units=1024, size=1_000_000, used=400_000)])
    + unifi_vap,
)

# --- 127.0.0.23 Brother printer, community "printers" (only via extra_communities) -----------
write(
    "printer/printers.snmprec",
    system("Brother NC-8300h, Firmware Ver.1.10", "1.3.6.1.4.1.2435.2.3.9.1", "BRN3C2AF4", 360_000)
    + interfaces([dict(idx=1, name="NC-8300h", type=6, speed=100_000_000, mac="3c2af4000001", rate_in=200, rate_out=100)])
    + [
        (f"{PRT_LIFE}.1.1", "65", "12345"),  # prtMarkerLifeCount
        (f"{PRT_SUP}.1.1.1", "2", "1"),
        (f"{PRT_SUP}.1.1.2", "2", "2"),
        (f"{PRT_SUP}.4.1.1", "2", "3"),  # supplyThatIsConsumed
        (f"{PRT_SUP}.4.1.2", "2", "3"),
        (f"{PRT_SUP}.5.1.1", "2", "3"),  # prtMarkerSuppliesType: toner
        (f"{PRT_SUP}.5.1.2", "2", "9"),  # opc (drum)
        (f"{PRT_SUP}.6.1.1", "4", "Black Toner Cartridge"),
        (f"{PRT_SUP}.6.1.2", "4", "Drum Unit"),
        (f"{PRT_SUP}.7.1.1", "2", "19"),  # percent
        (f"{PRT_SUP}.7.1.2", "2", "19"),
        (f"{PRT_SUP}.8.1.1", "2", "100"),  # MaxCapacity
        (f"{PRT_SUP}.8.1.2", "2", "100"),
        (f"{PRT_SUP}.9.1.1", "2", "40"),  # Level -> 40 %
        (f"{PRT_SUP}.9.1.2", "2", "-3"),  # Level -3 = "some remaining" -> no value
    ],
)

# --- 127.0.0.24 Cisco Catalyst 2960-X, no HOST-RESOURCES-MIB ----------------------------------
write(
    "switch/public.snmprec",
    system(
        "Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(7)E8",
        "1.3.6.1.4.1.9.1.1208",
        "sw-core-2960x",
        77_700_000,
    )
    + interfaces(
        [
            dict(idx=10101, name="Gi1/0/1", mac="00cafe000101", rate_in=5_000_000, rate_out=1_000_000, alias="uplink"),
            dict(idx=10102, name="Gi1/0/2", mac="00cafe000102", rate_in=125_000, rate_out=125_000, alias="ap"),
            dict(idx=10103, name="Gi1/0/3", mac="00cafe000103", oper=2),
            dict(idx=10104, name="Gi1/0/4", mac="00cafe000104", oper=2),
        ]
    ),
)
