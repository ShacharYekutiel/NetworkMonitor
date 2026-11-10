#!/usr/bin/env python3
"""Generates the phase-3 new-device lab recordings (docs/architecture.md §13.4, §13.11).

    python3 tests/sim/newdev/gen_snmprec.py     # rewrites run1/ run2/ public.snmprec (committed)

ADDRESS: the contract puts the access switch on 127.0.0.88, which is already the 2b-3 ArubaOS LAG sim
(tests/sim/lab.sh "snmp-lag-aos"), and .89 is the 2b-3 SNMPv3 trap source device (tests/e2e/lib/lab2b3.ts), so it lives on **127.0.0.90** (component "snmp-newdev-access", LAB_3=1).
The test swaps the recording between inventory runs: `$LAB_SH swap snmp-newdev-access tests/sim/newdev/run2`.

"newdev-access" (ArubaOS-Switch 2930F, sysObjectID 1.3.6.1.4.1.11.2.3.7.11.181, v2c community public, udp/1161):
  ports 1-8 access (untagged VLAN 20 "CLIENTS"), 9 uplink to "port-aos52" (127.0.0.86) with an LLDP neighbour
  entry AND 40 MACs, 10 trunk (tagged VLANs 10 + 20), 11 DEFAULT_VLAN (vlan interface).
  ARP (ipNetToPhysicalTable): 00:1b:21:aa:00:01 -> 10.0.20.57 (PTR pc-1234.mnidom.net in tests/sim/ports/ptr.json).

run1 (baseline): port 1 00:50:56:89:00:01, port 2 3c:52:82:89:00:02, port 4 00:23:24:89:00:04,
  port 9 00:16:3e:89:09:01..40, port 10 00:16:3e:89:0a:01..02.
run2 = run1 plus
  00:1b:21:aa:00:01 port 3 VLAN 20  (Intel, globally administered -> NEW, alert with IP/hostname)
  02:00:00:aa:00:02 port 6          (locally administered -> ignored, not stored)
  00:16:3e:89:09:99 port 9          (uplink -> ignored)
  00:16:3e:89:0a:03 port 10         (trunk -> ignored)
  3c:a8:2a:00:00:01 port 4          (HP OUI, allow-listed by the test -> allowed)
  00:0e:c6:5f:00:01..25 port 5      (25 new MACs -> flood guard: 20 events + "+5 more" summary)

CONTRACT NOTE (QA): with the default uplink_mac_threshold 5, port 5 (25 MACs) is an *uplink by mac_count* and
the flood-guard case can never fire, and the 2b-2 rule also needs the LLDP neighbour to be a monitored device
with a topology link for `uplink_reason = neighbor`. The tests set uplink_mac_threshold = 30 (port 9 with 40/41
MACs stays an uplink by mac_count).
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
PORTS_GEN = HERE.parent / "ports" / "gen_snmprec.py"

# reuse the 2b-2 helper functions without running that generator's writes
_src = PORTS_GEN.read_text()
_ns: dict = {"__name__": "ports_gen_helpers", "__file__": str(PORTS_GEN)}
exec(compile(_src[: _src.index("# ------------------------------------------------------------------ 127.0.0.81")],
             str(PORTS_GEN), "exec"), _ns)
system, interfaces, vlan_static, pvids, fdb_q = (_ns[n] for n in ("system", "interfaces", "vlan_static", "pvids",
                                                                  "fdb_q"))
lldp_local, lldp_remote, mac_hex, mac_arcs = _ns["lldp_local"], _ns["lldp_remote"], _ns["mac_hex"], _ns["mac_arcs"]
ARUBA_DESCR = _ns["ARUBA_DESCR"]
NET_TO_PHYS = _ns["NET_TO_PHYS"]

TEN_G = dict(speed=4_294_967_295, high=10_000)
PORTS = [dict(idx=n, name=str(n), mac=f"001a1e0089{n:02x}", alias=f"office-{n}") for n in range(1, 9)] + [
    dict(idx=9, name="9", mac="001a1e008909", alias="uplink port-aos52", **TEN_G),
    dict(idx=10, name="10", mac="001a1e00890a", alias="trunk to AP switch", **TEN_G),
    dict(idx=11, name="DEFAULT_VLAN", type=53, speed=0, high=0, mac="001a1e000089"),
]
BASE = (
    [(20, "00:50:56:89:00:01", 1, 3), (20, "3c:52:82:89:00:02", 2, 3), (20, "00:23:24:89:00:04", 4, 3)]
    + [(20, f"00:16:3e:89:09:{n:02d}", 9, 3) for n in range(1, 41)]
    + [(10, "00:16:3e:89:0a:01", 10, 3), (20, "00:16:3e:89:0a:02", 10, 3)]
)
ADDED = (
    [(20, "00:1b:21:aa:00:01", 3, 3), (20, "02:00:00:aa:00:02", 6, 3), (20, "00:16:3e:89:09:99", 9, 3),
     (20, "00:16:3e:89:0a:03", 10, 3), (20, "3c:a8:2a:00:00:01", 4, 3)]
    + [(20, f"00:0e:c6:5f:00:{n:02d}", 5, 3) for n in range(1, 26)]
)


def arp(ip: str, mac: str, ifi: int = 11) -> list:
    sfx = f"{ifi}.1.4.{ip}"
    return [(f"{NET_TO_PHYS}.4.{sfx}", "4x", mac_hex(mac)), (f"{NET_TO_PHYS}.6.{sfx}", "2", "3")]


def records(fdb: list) -> list:
    return (
        system(ARUBA_DESCR, "1.3.6.1.4.1.11.2.3.7.11.181", "newdev-access", 70_000_000)
        + interfaces(PORTS)
        + vlan_static({1: ({9, 10}, set(), "DEFAULT_VLAN"),
                       10: ({10}, set(), "SERVERS"),
                       20: (set(range(1, 9)) | {9, 10}, set(range(1, 9)), "CLIENTS")}, 2)
        + pvids({**{p: 20 for p in range(1, 9)}, 9: 1, 10: 1})
        + fdb_q(fdb)
        + lldp_local("001a1e000089", "newdev-access", ARUBA_DESCR, [(9, 7, "9", "9"), (10, 7, "10", "10")])
        + lldp_remote(9, 1, "001a1e000086", 7, "49", "49", "port-aos52", ARUBA_DESCR, "127.0.0.86")
        + arp("10.0.20.57", "00:1b:21:aa:00:01") + arp("10.0.20.11", "00:50:56:89:00:01")
    )


def write(rel: str, recs: list) -> None:
    key = lambda r: tuple(int(x) for x in r[0].split("."))  # noqa: E731
    oids = [r[0] for r in recs]
    assert len(oids) == len(set(oids)), rel
    p = HERE / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(f"{o}|{t}|{v}\n" for o, t, v in sorted(recs, key=key)))
    print(f"wrote {p.relative_to(HERE)} ({len(recs)} records)")


write("run1/public.snmprec", records(BASE))
write("run2/public.snmprec", records(BASE + ADDED))
