"""[2b-2] snmpsim recordings for the port inventory tests: HPE ArubaOS-Switch, Comware, Aruba CX,
an ARP router (ipNetToPhysicalTable) and a legacy ARP host (ipNetToMediaTable), plus a large
48-port switch with 2000 MACs. Modelled on architecture.md § 11.6 (backend agents use
127.0.7.x)."""

from __future__ import annotations

from tests.snmpsim_agents import Record

SYS = "1.3.6.1.2.1.1"
IF = "1.3.6.1.2.1.2.2.1"
IFX = "1.3.6.1.2.1.31.1.1.1"
DOT3 = "1.3.6.1.2.1.10.7.2.1.19"
BASE_PORT = "1.3.6.1.2.1.17.1.4.1.2"
PVID = "1.3.6.1.2.1.17.7.1.4.5.1.1"
STATIC = "1.3.6.1.2.1.17.7.1.4.3.1"
CURRENT = "1.3.6.1.2.1.17.7.1.4.2.1"
QFDB = "1.3.6.1.2.1.17.7.1.2.2.1"
DFDB = "1.3.6.1.2.1.17.4.3.1"
PETH = "1.3.6.1.2.1.105.1.1.1"
PHYS = "1.3.6.1.2.1.4.35.1"
MEDIA = "1.3.6.1.2.1.4.22.1"

ARUBA_DESCR = (
    "Aruba JL256A 2930F-48G-4SFP+ Switch, revision WC.16.11.0012, ROM WC.16.01.0010 "
    "(/ws/swbuildm/rel_orlando_qaoff/code/build/bom(swbuildm_rel_orlando_qaoff_rel_orlando))"
)
COMWARE_DESCR = (
    "HPE Comware Platform Software, Software Version 7.1.070, Release 6328 "
    "HPE 1950-24G-2SFP+-2XGT Switch"
)
CX_DESCR = "Aruba JL658A 6300M 24SFP+ 4SFP56 Swch FL.10.13.1000"
OPN_DESCR = (
    "FreeBSD OPNsense.localdomain 14.1-RELEASE-p5 FreeBSD 14.1-RELEASE-p5 "
    "stable/24.7-n267758-abc amd64"
)


def system(descr: str, oid: str, name: str, uptime: int = 100000000) -> list[Record]:
    return [
        (f"{SYS}.1.0", 4, descr),
        (f"{SYS}.2.0", 6, oid),
        (f"{SYS}.3.0", 67, str(uptime)),
        (f"{SYS}.5.0", 4, name),
    ]


def port(
    idx: int,
    name: str,
    type_: int = 6,
    *,
    admin: int = 1,
    oper: int = 1,
    speed: int = 1000000000,
    high: int | None = None,
    mac: str | None = None,
    last_change: int = 0,
    octets: tuple[int, int] = (1000, 2000),
    hc: bool = True,
    alias: str = "",
) -> list[Record]:
    recs: list[Record] = [
        (f"{IF}.1.{idx}", 2, str(idx)),
        (f"{IF}.2.{idx}", 4, name),
        (f"{IF}.3.{idx}", 2, str(type_)),
        (f"{IF}.5.{idx}", 66, str(speed)),
        (f"{IF}.6.{idx}", "4x", mac or f"001a1e0001{idx % 256:02x}"),
        (f"{IF}.7.{idx}", 2, str(admin)),
        (f"{IF}.8.{idx}", 2, str(oper)),
        (f"{IF}.9.{idx}", 67, str(last_change)),
        (f"{IF}.10.{idx}", 65, str(octets[0])),
        (f"{IF}.14.{idx}", 65, "0"),
        (f"{IF}.16.{idx}", 65, str(octets[1])),
        (f"{IF}.20.{idx}", 65, "0"),
        (f"{IFX}.1.{idx}", 4, name),
        (f"{IFX}.18.{idx}", 4, alias),
    ]
    if hc:
        recs += [(f"{IFX}.6.{idx}", 70, str(octets[0])), (f"{IFX}.10.{idx}", 70, str(octets[1]))]
    recs.append((f"{IFX}.15.{idx}", 66, str(high if high is not None else speed // 1000000)))
    return recs


def portlist_hex(ports: set[int], octets: int = 8) -> str:
    b = bytearray(octets)
    for p in ports:
        b[(p - 1) // 8] |= 0x80 >> ((p - 1) % 8)
    return b.hex()


def mac_arcs(mac: str) -> str:
    return ".".join(str(int(x, 16)) for x in mac.split(":"))


def static_vlans(vlans: dict[int, tuple[set[int], set[int]]], octets: int = 8) -> list[Record]:
    """{vlan: (egress, untagged)} -> dot1qVlanStaticTable rows."""
    recs: list[Record] = []
    for v, (egress, untagged) in vlans.items():
        recs += [
            (f"{STATIC}.2.{v}", "4x", portlist_hex(egress, octets)),
            (f"{STATIC}.4.{v}", "4x", portlist_hex(untagged, octets)),
        ]
    return recs


def qfdb(entries: list[tuple[int, str, int, int]]) -> list[Record]:
    """(fdbId, mac, bridge port, status)."""
    recs: list[Record] = []
    for fid, mac, bp, status in entries:
        recs += [
            (f"{QFDB}.2.{fid}.{mac_arcs(mac)}", 2, str(bp)),
            (f"{QFDB}.3.{fid}.{mac_arcs(mac)}", 2, str(status)),
        ]
    return recs


def poe(rows: list[tuple[int, int, int, int, int | None]]) -> list[Record]:
    """(group, port, admin, detection, class or None)."""
    recs: list[Record] = []
    for g, p, admin, det, cls in rows:
        recs += [(f"{PETH}.3.{g}.{p}", 2, str(admin)), (f"{PETH}.6.{g}.{p}", 2, str(det))]
        if cls is not None:
            recs.append((f"{PETH}.10.{g}.{p}", 2, str(cls)))
    return recs


# ------------------------------------------------------------------ .61 ArubaOS-Switch
def arubaos_records(*, drop_port7_mac: bool = False) -> list[Record]:
    recs = system(ARUBA_DESCR, "1.3.6.1.4.1.11.2.3.7.11.181", "aruba-61")
    for i in range(1, 11):
        kw: dict = {}
        if i == 1:
            kw["last_change"] = 99000000
            kw["alias"] = "Reception PC"
        if i == 3:
            kw["speed"] = 100000000
        if i == 5:
            kw["admin"], kw["oper"] = 2, 2
        if i in (6, 8):
            kw["oper"] = 2
        if i == 6:
            kw["speed"], kw["high"] = 0, 0
        if i in (9, 10):
            kw["speed"], kw["high"] = 4294967295, 10000
        recs += port(i, str(i), **kw)
    recs += port(11, "Trk1", 161)
    recs += port(12, "DEFAULT_VLAN", 53)
    recs += static_vlans(
        {
            1: ({1, 2, 3, 4, 5, 6, 8, 9}, {1, 2, 3, 4, 5, 6, 8, 9}),
            10: ({7, 9, 10}, {7}),
            20: ({9, 10}, set()),
        }
    )
    recs += [(f"{PVID}.{bp}", 66, "10" if bp == 7 else "1") for bp in range(1, 11)]
    fdb = [
        (1, "00:50:56:01:00:01", 1, 3),
        (1, "3c:52:82:00:00:02", 2, 3),
        (1, "00:00:0c:00:00:03", 2, 3),
        (1, "00:1a:1e:00:00:61", 3, 4),
        (1, "da:a1:19:00:00:04", 4, 3),
        (10, "02:00:00:00:0a:01", 10, 3),
        (10, "02:00:00:00:0a:02", 10, 3),
    ]
    if not drop_port7_mac:
        fdb.append((10, "00:50:56:01:00:07", 7, 3))
    for n, vlan in zip(range(1, 8), (1, 1, 1, 10, 10, 20, 20), strict=True):
        fdb.append((vlan, f"02:00:00:00:09:{n:02x}", 9, 3))
    recs += qfdb(fdb)
    recs += poe(
        [
            (1, 1, 1, 3, 4),
            (1, 2, 1, 2, None),
            (1, 3, 1, 2, None),
            (1, 4, 1, 3, 5),
            (1, 5, 2, 1, None),
            (1, 6, 1, 2, None),
            (1, 7, 1, 3, 1),
            (1, 8, 1, 4, None),
        ]
    )
    duplex = {1: 3, 2: 3, 4: 3, 7: 3, 9: 3, 10: 3, 3: 2, 6: 1}
    recs += [(f"{DOT3}.{i}", 2, str(v)) for i, v in duplex.items()]
    return recs


# ------------------------------------------------------------------ .62 Comware
def comware_records() -> list[Record]:
    recs = system(COMWARE_DESCR, "1.3.6.1.4.1.25506.11.1.177", "comware-62")
    for i in range(1, 5):
        recs += port(i, f"GigabitEthernet1/0/{i}")
    recs += port(49, "Ten-GigabitEthernet1/0/49", high=10000, speed=4294967295)
    recs += port(50, "Ten-GigabitEthernet1/0/50", high=10000, speed=4294967295)
    recs += port(101, "GigabitEthernet2/0/1")
    recs += port(102, "GigabitEthernet2/0/2")
    recs += port(200, "Vlan-interface1", 136)
    recs += port(300, "NULL0", 1)
    recs += port(301, "InLoopBack0", 24)
    recs += port(400, "M-GigabitEthernet0/0/0", 6)
    bps = {1: 1, 2: 2, 3: 3, 4: 4, 5: 49, 6: 50, 7: 101, 8: 102}
    recs += [(f"{BASE_PORT}.{bp}", 2, str(i)) for bp, i in bps.items()]
    recs += static_vlans(
        {1: ({1, 2, 3, 5, 6, 7, 8}, {1, 2, 3, 5, 7, 8}), 10: ({4, 5, 6}, {4}), 20: ({5, 6}, set())},
        2,
    )
    recs += [(f"{CURRENT}.3.0.1", 66, "1"), (f"{CURRENT}.3.0.10", 66, "1010")]
    recs += qfdb(
        [
            (1, "00:0c:29:00:01:01", 1, 3),
            (1010, "00:0c:29:00:01:04", 4, 3),
            (1, "00:0c:29:00:01:07", 7, 3),
            (1, "02:00:00:00:09:01", 3, 3),
        ]
    )
    recs += poe([(1, 1, 1, 3, 3), (1, 2, 1, 2, None), (1, 3, 1, 2, None), (1, 4, 1, 2, None)])
    return recs


# ------------------------------------------------------------------ .63 Aruba CX
def arubacx_records() -> list[Record]:
    recs = system(CX_DESCR, "1.3.6.1.4.1.47196.4.1.1.1.200", "cx-63")
    for i in range(1, 7):
        recs += port(i, f"1/1/{i}")
    recs += port(49, "1/1/49", high=10000, speed=4294967295)
    recs += port(50, "1/1/50", high=10000, speed=4294967295)
    recs += port(1000, "vlan1", 53)
    recs += port(2000, "mgmt", 6)
    recs += [(f"{BASE_PORT}.{n}", 2, str(n)) for n in (1, 2, 3, 4, 5, 6, 49, 50)]
    full = portlist_hex({1, 2, 3, 4, 5, 6})
    recs += [(f"{CURRENT}.4.0.1", "4x", full), (f"{CURRENT}.5.0.1", "4x", full)]
    recs += [
        (f"{DFDB}.2.{mac_arcs('00:00:0c:00:cc:01')}", 2, "1"),
        (f"{DFDB}.3.{mac_arcs('00:00:0c:00:cc:01')}", 2, "3"),
    ]
    recs += poe([(1, p, 1, 2, None) for p in range(1, 7)])
    return recs


# ------------------------------------------------------------------ .64 ARP router, .65 legacy
def arp_router_records() -> list[Record]:
    recs = system(OPN_DESCR, "1.3.6.1.4.1.8072.3.2.8", "OPNsense.localdomain")
    recs += port(1, "igb0") + port(2, "igb1")
    rows = [
        ("10.0.0.11", "005056010001", 3),
        ("10.0.0.12", "00000c000003", 3),
        ("10.0.0.17", "005056010007", 3),
        ("10.0.0.99", "020000000901", 4),
        ("10.0.0.50", "005056010050", 2),
        ("10.0.0.1", "001a1e0001ff", 5),
    ]
    for ip, mac, typ in rows:
        sfx = f"2.1.4.{ip}"
        recs += [(f"{PHYS}.4.{sfx}", "4x", mac), (f"{PHYS}.6.{sfx}", 2, str(typ))]
    v6 = "2.2.16." + ".".join(["254", "128"] + ["0"] * 13 + ["1"])
    recs += [(f"{PHYS}.4.{v6}", "4x", "005056010099"), (f"{PHYS}.6.{v6}", 2, "3")]
    return recs


def arp_legacy_records() -> list[Record]:
    recs = system("Linux gw2 5.10", "1.3.6.1.4.1.8072.3.2.10", "gw2")
    recs += port(1, "eth0")
    recs += [
        (f"{MEDIA}.2.1.10.0.0.14", "4x", "daa119000004"),
        (f"{MEDIA}.4.1.10.0.0.14", 2, "3"),
    ]
    return recs


# ------------------------------------------------------------------ big switch (performance)
def big_switch_records(ports: int = 48, macs: int = 2000) -> list[Record]:
    recs = system(ARUBA_DESCR, "1.3.6.1.4.1.11.2.3.7.11.181", "big")
    for i in range(1, ports + 1):
        recs += port(i, str(i))
    recs += static_vlans({1: (set(range(1, ports + 1)), set(range(1, ports + 1)))})
    entries = []
    for n in range(macs):
        mac = f"00:50:56:{(n >> 16) & 255:02x}:{(n >> 8) & 255:02x}:{n & 255:02x}"
        entries.append((1, mac, 1 + n % ports, 3))
    recs += qfdb(entries)
    recs += poe([(1, p, 1, 3, 2) for p in range(1, ports + 1)])
    return recs
