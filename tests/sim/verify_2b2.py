#!/usr/bin/env python3
"""Standalone check of the Phase 2b-2 simulators (docs/architecture.md §11.6) with pysnmp.

    backend/.venv/bin/python -I tests/sim/verify_2b2.py [--host-prefix 127.0.0.]

Walks every 2b-2 agent started by `tests/sim/lab.sh start` (LAB_2B2=1), applies an independent QA
implementation of the contract parsers (data-model § Port inventory: portlist bit order, bridge-port mapping,
VLAN sets, FDB Q-BRIDGE/BRIDGE, PoE mapping rules 1-2, ARP physical/media) and asserts the contract's
expected tables (port sims) and key values (vendor + synthetic sims). Exits 1 on the first mismatch list.
This checks the *simulators* against the contract, independent of the product.
"""

from __future__ import annotations

import asyncio
import re
import sys

from pyasn1.type import univ
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    bulk_walk_cmd,
    get_cmd,
)

ENGINE = SnmpEngine()
FAIL: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAIL.append(msg)


async def walk(host: str, oid: str) -> dict[str, object]:
    """suffix (after oid.) -> python value (int, bytes, str for OIDs)."""
    out: dict[str, object] = {}
    tgt = await UdpTransportTarget.create((host, 1161), timeout=2, retries=1)
    async for err, status, _, vbs in bulk_walk_cmd(
        ENGINE, CommunityData("public"), tgt, ContextData(), 0, 25, ObjectType(ObjectIdentity(oid)),
        lexicographicMode=False,
    ):
        if err or status:
            raise RuntimeError(f"{host} {oid}: {err or status.prettyPrint()}")
        for name, val in vbs:
            n = str(name)
            if not n.startswith(oid + "."):
                continue
            if val.__class__.__name__ in ("EndOfMibView", "NoSuchObject", "NoSuchInstance"):
                continue
            out[n[len(oid) + 1 :]] = py(val)
    return out


async def get(host: str, oid: str) -> object:
    tgt = await UdpTransportTarget.create((host, 1161), timeout=2, retries=1)
    err, status, _, vbs = await get_cmd(ENGINE, CommunityData("public"), tgt, ContextData(), ObjectType(ObjectIdentity(oid)))
    if err or status:
        raise RuntimeError(f"{host} {oid}: {err or status}")
    val = vbs[0][1]
    if val.__class__.__name__ in ("NoSuchObject", "NoSuchInstance", "EndOfMibView"):
        return None
    return py(val)


def py(val: object) -> object:
    if isinstance(val, univ.OctetString):
        return bytes(val.asOctets())
    if isinstance(val, (univ.ObjectIdentifier, ObjectIdentity)):
        return str(val)
    return int(val)


# ------------------------------------------------------------------ contract parsers (QA implementation)
def portlist(b: object) -> set[int]:
    if not isinstance(b, bytes):
        return set()
    return {i * 8 + bit + 1 for i, o in enumerate(b) for bit in range(8) if o & (0x80 >> bit)}


def mac_text(b: bytes) -> str | None:
    return ":".join(f"{x:02x}" for x in b) if isinstance(b, bytes) and len(b) == 6 else None


def port_kind(t: int | None, name: str) -> str:
    if re.match(r"(?i)^(m-gigabitethernet|mgmt|management|oobm|mgmteth|ma\d)", name):
        return "mgmt"
    if t in (6, 62, 69, 117):
        return "physical"
    if t in (161, 54):
        return "lag"
    if t in (53, 136, 135):
        return "vlan"
    return "other"


IFS = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}
DET = {1: "disabled", 2: "searching", 3: "delivering", 4: "fault", 5: "test", 6: "other_fault"}


async def inventory(host: str) -> dict:
    W = lambda o: walk(host, o)  # noqa: E731
    descr = await W("1.3.6.1.2.1.2.2.1.2")
    typ = await W("1.3.6.1.2.1.2.2.1.3")
    speed = await W("1.3.6.1.2.1.2.2.1.5")
    admin = await W("1.3.6.1.2.1.2.2.1.7")
    oper = await W("1.3.6.1.2.1.2.2.1.8")
    name = await W("1.3.6.1.2.1.31.1.1.1.1")
    high = await W("1.3.6.1.2.1.31.1.1.1.15")
    dup = await W("1.3.6.1.2.1.10.7.2.1.19")
    bpmap = await W("1.3.6.1.2.1.17.1.4.1.2")
    pvid = await W("1.3.6.1.2.1.17.7.1.4.5.1.1")
    st_eg = await W("1.3.6.1.2.1.17.7.1.4.3.1.2")
    st_ut = await W("1.3.6.1.2.1.17.7.1.4.3.1.4")
    cur_eg = await W("1.3.6.1.2.1.17.7.1.4.2.1.4")
    cur_ut = await W("1.3.6.1.2.1.17.7.1.4.2.1.5")
    fdbid = await W("1.3.6.1.2.1.17.7.1.4.2.1.3")
    qport = await W("1.3.6.1.2.1.17.7.1.2.2.1.2")
    qstat = await W("1.3.6.1.2.1.17.7.1.2.2.1.3")
    dport = await W("1.3.6.1.2.1.17.4.3.1.2")
    dstat = await W("1.3.6.1.2.1.17.4.3.1.3")
    padm = await W("1.3.6.1.2.1.105.1.1.1.3")
    pdet = await W("1.3.6.1.2.1.105.1.1.1.6")
    pcls = await W("1.3.6.1.2.1.105.1.1.1.10")

    ports: dict[int, dict] = {}
    for s, d in descr.items():
        i = int(s)
        nm = name.get(s, b"").decode() or d.decode()
        hs = high.get(s, 0)
        sp = hs * 1_000_000 if hs > 0 else (speed.get(s, 0) or None)
        a, o = IFS.get(admin.get(s, 4), "unknown"), IFS.get(oper.get(s, 4), "unknown")
        ports[i] = dict(
            name=nm, kind=port_kind(typ.get(s), nm), speed=sp,
            state="admin_down" if a == "down" else ("up" if o == "up" else ("absent" if o == "notPresent" else "down")),
            duplex={1: "unknown", 2: "half", 3: "full"}.get(dup.get(s)), mode=None, pvid=None,
            untagged=[], tagged=[], poe=None, macs=[],
        )
    # bridge port -> ifIndex
    bridge = {int(k): v for k, v in bpmap.items()} if bpmap else {i: i for i in ports}
    if_of_bp = {bp: i for bp, i in bridge.items() if i in ports}
    # VLANs
    eg, ut, vlan_mib = (st_eg, st_ut, "static") if st_eg else (
        {k.split(".", 1)[1]: v for k, v in cur_eg.items()}, {k.split(".", 1)[1]: v for k, v in cur_ut.items()},
        "current" if cur_eg else None)
    for bp, i in if_of_bp.items():
        p = ports[i]
        u = sorted(int(v) for v in eg if bp in portlist(ut.get(v)))
        t = sorted(int(v) for v in eg if bp in portlist(eg[v]) and bp not in portlist(ut.get(v)))
        p["untagged"], p["tagged"] = u, t
        p["mode"] = "trunk" if t else ("access" if u else None)
        pv = pvid.get(str(bp))
        p["pvid"] = pv if pv is not None else (u[0] if len(u) == 1 else None)
    # FDB
    fdb_to_vlan: dict[int, int] = {}
    for k, f in sorted(fdbid.items(), key=lambda kv: int(kv[0].split(".")[1])):
        fdb_to_vlan.setdefault(f, int(k.split(".")[1]))
    if qport:
        fdb_mib, rows = "q_bridge", []
        for k, bp in qport.items():
            a = k.split(".")
            fid, mac = int(a[0]), ":".join(f"{int(x):02x}" for x in a[1:7])
            if qstat.get(k) != 3:
                continue
            rows.append((mac, (fdb_to_vlan.get(fid) if fdbid else fid), bp))
    elif dport:
        fdb_mib, rows = "bridge", []
        for k, bp in dport.items():
            if dstat.get(k) != 3:
                continue
            rows.append((":".join(f"{int(x):02x}" for x in k.split(".")), None, bp))
    else:
        fdb_mib, rows = None, []
    for mac, vlan, bp in rows:
        if bp in if_of_bp:
            ports[if_of_bp[bp]]["macs"].append((mac, vlan))
    # PoE (rule 1: port value = ifIndex of a physical port, all distinct; rule 2: name tail)
    keys = [tuple(int(x) for x in k.split(".")) for k in pdet]
    phys = {i for i, p in ports.items() if p["kind"] == "physical"}
    mapping: dict[tuple[int, int], int] = {}
    if keys and len({p for _, p in keys}) == len(keys) and all(p in phys for _, p in keys):
        mapping = {(g, p): p for g, p in keys}
    elif keys:
        tails = {}
        for i in phys:
            nums = [int(n) for n in re.findall(r"\d+", ports[i]["name"])]
            tails[(nums[0] if len(nums) >= 2 else 1, nums[-1])] = i
        if all(k in tails for k in keys):
            mapping = {k: tails[k] for k in keys}
    for (g, p), i in mapping.items():
        k = f"{g}.{p}"
        c = pcls.get(k)
        ports[i]["poe"] = (padm.get(k) == 1, DET.get(pdet.get(k)), c - 1 if c else None)
    return dict(ports=ports, fdb_mib=fdb_mib, vlan_mib=vlan_mib, poe="ok" if mapping else ("unmapped" if keys else "empty"),
                bridge="ok" if bpmap else "empty")


G = 1_000_000_000


async def check_arubaos(host: str) -> None:
    inv = await inventory(host)
    P = inv["ports"]
    check(inv["fdb_mib"] == "q_bridge" and inv["vlan_mib"] == "static" and inv["bridge"] == "empty", f"{host} sources {inv}")
    exp = {  # if: state, speed, duplex, mode, pvid, untagged, tagged, poe, macs
        1: ("up", G, "full", "access", 1, [1], [], (True, "delivering", 3), [("00:50:56:01:00:01", 1)]),
        2: ("up", G, "full", "access", 1, [1], [], (True, "searching", None), [("3c:52:82:00:00:02", 1), ("00:00:0c:00:00:03", 1)]),
        3: ("up", 100_000_000, "half", "access", 1, [1], [], (True, "searching", None), []),
        4: ("up", G, "full", "access", 1, [1], [], (True, "delivering", 4), [("da:a1:19:00:00:04", 1)]),
        5: ("admin_down", G, None, "access", 1, [1], [], (False, "disabled", None), []),
        6: ("down", None, "unknown", "access", 1, [1], [], (True, "searching", None), []),
        7: ("up", G, "full", "access", 10, [10], [], (True, "delivering", 0), [("00:50:56:01:00:07", 10)]),
        8: ("down", None, None, "access", 1, [1], [], (True, "fault", None), []),
        9: ("up", 10 * G, "full", "trunk", 1, [1], [10, 20], None, 7),
        10: ("up", 10 * G, "full", "trunk", 1, [], [10, 20], None, 2),
    }
    for i, (st, sp, du, mo, pv, u, t, poe, macs) in exp.items():
        p = P[i]
        got = (p["state"], p["speed"], p["duplex"], p["mode"], p["pvid"], p["untagged"], p["tagged"], p["poe"])
        want = (st, sp, du, mo, pv, u, t, poe)
        check(got == want, f"{host} if {i}: {got} != {want}")
        if isinstance(macs, int):
            check(len(p["macs"]) == macs, f"{host} if {i}: {len(p['macs'])} MACs != {macs}")
        else:
            check(sorted(p["macs"]) == sorted(macs), f"{host} if {i}: {p['macs']} != {macs}")
    check(P[11]["kind"] == "lag" and P[12]["kind"] == "vlan", f"{host} Trk1/DEFAULT_VLAN kinds")
    vl9 = sorted(v for _, v in P[9]["macs"])
    check(vl9 == [1, 1, 1, 10, 10, 20, 20], f"{host} port 9 VLANs {vl9}")
    lc = await get(host, "1.3.6.1.2.1.2.2.1.9.1")
    up = await get(host, "1.3.6.1.2.1.1.3.0")
    check((up - lc) / 100 == 10000, f"{host} last change offset {(up - lc) / 100}")


async def check_comware(host: str) -> None:
    inv = await inventory(host)
    P = inv["ports"]
    check(inv["fdb_mib"] == "q_bridge" and inv["vlan_mib"] == "static" and inv["bridge"] == "ok", f"{host} sources")
    check(sorted(i for i, p in P.items() if p["kind"] == "physical") == [1, 2, 3, 4, 49, 50, 101, 102], f"{host} physical")
    check(P[400]["kind"] == "mgmt" and P[200]["kind"] == "vlan" and P[300]["kind"] == "other" and P[301]["kind"] == "other", f"{host} kinds")
    check((P[4]["mode"], P[4]["pvid"], P[4]["macs"]) == ("access", 10, [("00:0c:29:00:01:04", 10)]), f"{host} port 4 {P[4]}")
    check(P[49]["mode"] == "trunk" and P[49]["tagged"] == [10, 20] and P[49]["untagged"] == [1], f"{host} 49 {P[49]}")
    # Contract text says "Ports 49/50: trunk, tagged [10, 20]"; by the VLAN rule port 50 (bp 6, in VLAN 1
    # egress but not untagged) is tagged [1, 10, 20] (reported as a contract inconsistency).
    check(P[50]["mode"] == "trunk" and P[50]["tagged"] == [1, 10, 20], f"{host} 50 {P[50]}")
    check(P[101]["macs"] == [("00:0c:29:00:01:07", 1)] and P[3]["macs"] == [("02:00:00:00:09:01", 1)], f"{host} macs 101/3")
    check(P[1]["poe"] == (True, "delivering", 3) and inv["poe"] == "ok", f"{host} PoE {P[1]['poe']}")
    check(P[49]["speed"] == 10 * G, f"{host} 49 speed")
    lldp = await walk(host, "1.0.8802.1.1.2.1.4.1.1.9")
    check(list(lldp.values()) == [b"U6-Lite-Office"] and list(lldp)[0] == "0.2.1", f"{host} LLDP {lldp}")


async def check_cx(host: str) -> None:
    inv = await inventory(host)
    P = inv["ports"]
    check(inv["fdb_mib"] == "bridge" and inv["vlan_mib"] == "current", f"{host} sources {inv['fdb_mib']} {inv['vlan_mib']}")
    check(P[1]["macs"] == [("00:00:0c:00:cc:01", None)], f"{host} port 1 MAC {P[1]['macs']}")
    for i in range(1, 7):
        check((P[i]["mode"], P[i]["pvid"]) == ("access", 1), f"{host} if {i} access/pvid {P[i]['mode']} {P[i]['pvid']}")
    check(P[2000]["kind"] == "mgmt" and P[1000]["kind"] == "vlan", f"{host} kinds")
    check(P[49]["speed"] == 10 * G and P[50]["speed"] == 10 * G, f"{host} 49/50 10G")
    check(all(P[i]["poe"] is not None for i in range(1, 7)) and inv["poe"] == "ok", f"{host} PoE mapped")
    check(await walk(host, "1.3.6.1.2.1.17.7.1.4.3.1") == {} and await walk(host, "1.3.6.1.2.1.17.7.1.4.5.1.1") == {}
          and await walk(host, "1.3.6.1.2.1.17.7.1.2.2.1") == {}, f"{host} must lack static VLAN / PVID / dot1q FDB")


async def check_arp(host: str, legacy: bool) -> None:
    if legacy:
        mac = await walk(host, "1.3.6.1.2.1.4.22.1.2")
        typ = await walk(host, "1.3.6.1.2.1.4.22.1.4")
        got = {".".join(k.split(".")[1:]): mac_text(v) for k, v in mac.items() if typ.get(k) in (3, 4)}
        check(got == {"10.0.0.14": "da:a1:19:00:00:04"}, f"{host} legacy ARP {got}")
        check(await walk(host, "1.3.6.1.2.1.4.35.1") == {}, f"{host} must lack ipNetToPhysical")
        return
    mac = await walk(host, "1.3.6.1.2.1.4.35.1.4")
    typ = await walk(host, "1.3.6.1.2.1.4.35.1.6")
    got = {}
    for k, v in mac.items():
        a = [int(x) for x in k.split(".")]
        if a[1] != 1:
            continue  # IPv6 ignored
        ip = ".".join(map(str, a[3:7] if a[2] == 4 else a[2:6]))
        m = mac_text(v)
        if typ.get(k) in (3, 4) and m not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
            got[ip] = m
    want = {"10.0.0.11": "00:50:56:01:00:01", "10.0.0.12": "00:00:0c:00:00:03", "10.0.0.17": "00:50:56:01:00:07",
            "10.0.0.99": "02:00:00:00:09:01"}
    check(got == want, f"{host} ARP {got}")
    check(any(k.split(".")[1] == "2" for k in mac), f"{host} IPv6 row present")


async def check_aos52(host: str) -> None:
    inv = await inventory(host)
    phys = [i for i, p in inv["ports"].items() if p["kind"] == "physical"]
    check(phys == list(range(1, 53)), f"{host} 52 physical ports")


async def expect_values(host: str, values: dict[str, object]) -> None:
    for oid, want in values.items():
        got = await get(host, oid)
        if isinstance(got, bytes):
            got = got.decode(errors="replace")
        check(got == want, f"{host} {oid} = {got!r}, want {want!r}")


async def check_vendors() -> None:
    cp = "1.3.6.1.4.1.2620"
    await expect_values("127.0.0.71", {
        "1.3.6.1.2.1.1.2.0": "1.3.6.1.4.1.2620.1.6.123.1.49", f"{cp}.1.6.7.2.4.0": 23, f"{cp}.1.6.7.4.3.0": 16_000_000_000,
        f"{cp}.1.6.7.4.4.0": 4_000_000_000, f"{cp}.1.1.25.3.0": 12345, f"{cp}.1.1.25.10.0": 0, f"{cp}.1.5.2.0": 0,
    })
    tun = await walk("127.0.0.71", f"{cp}.500.9002.1.3")
    check(sorted(tun.values()) == [3, 3, 131], f".71 tunnels {tun}")
    check([v.decode() for v in (await walk("127.0.0.71", f"{cp}.1.6.7.8.1.1.3")).values()] == ["44.0", "38.5"], ".71 temps")
    check(list((await walk("127.0.0.71", f"{cp}.1.6.7.8.2.1.6")).values()) == [0, 1], ".71 fans")
    check([v.decode() for v in (await walk("127.0.0.71", f"{cp}.1.6.7.9.1.1.2")).values()] == ["Up", "Down"], ".71 PSUs")
    hrs = [v.decode() for v in (await walk("127.0.0.71", "1.3.6.1.2.1.25.2.3.1.3")).values()]
    check(hrs == ["/", "/var/log"], f".71 hrStorage {hrs}")
    ifs = [v.decode() for v in (await walk("127.0.0.71", "1.3.6.1.2.1.2.2.1.2")).values()]
    check(ifs == ["eth0", "eth1", "eth2", "lo"], f".71 ifs {ifs}")
    await expect_values("127.0.0.72", {
        "1.3.6.1.2.1.1.2.0": "1.3.6.1.4.1.8072.3.2.10", f"{cp}.1.6.7.2.4.0": 41, f"{cp}.1.5.2.0": 1,
        f"{cp}.1.5.6.0": "active", f"{cp}.1.5.101.0": 0,
    })
    check(await walk("127.0.0.72", f"{cp}.500.9002.1.3") == {} and await walk("127.0.0.72", f"{cp}.1.6.7.8") == {}
          and await walk("127.0.0.72", f"{cp}.1.6.7.9") == {}, ".72 must lack tunnel/hardware tables")
    check(bool(await walk("127.0.0.72", f"{cp}.1.6")), ".72 svn subtree (checkpoint probe)")
    ai = "1.3.6.1.4.1.14823.2.3.3.1.2"
    names = [v.decode() for v in (await walk("127.0.0.73", f"{ai}.1.1.2")).values()]
    sts = list((await walk("127.0.0.73", f"{ai}.1.1.11")).values())
    check(names == ["AP-Lobby", "AP-Office", "AP-Store"] and sts == [1, 1, 2], f".73 APs {names} {sts}")
    check(len(await walk("127.0.0.73", f"{ai}.4.1.1")) == 5, ".73 5 clients")
    check(await walk("127.0.0.73", "1.3.6.1.2.1.25") == {}, ".73 must lack HOST-RESOURCES (CPU/Memory probe_failed)")
    await expect_values("127.0.0.73", {"1.3.6.1.2.1.1.2.0": "1.3.6.1.4.1.14823.1.2.111", "1.3.6.1.2.1.1.5.0": "instant-vc"})


SYNTH = {  # ip -> (template, sysObjectID enterprise or prefix, representative OIDs that must answer)
    "127.0.0.101": ("aruba_cx", "1.3.6.1.4.1.47196", ["1.3.6.1.2.1.99.1.1.1.4.1", "1.3.6.1.2.1.105.1.3.1.1.4.1"]),
    "127.0.0.102": ("aruba_wifi", "1.3.6.1.4.1.14823", ["1.3.6.1.4.1.14823.2.3.3.1.2.1.1.11.1", "1.3.6.1.4.1.14823.2.2.1.1.3.2.0"]),
    "127.0.0.103": ("checkpoint_gaia", "1.3.6.1.4.1.2620", ["1.3.6.1.4.1.2620.1.5.2.0", "1.3.6.1.4.1.2620.500.9002.1.3.1"]),
    "127.0.0.104": ("fortinet_fortigate", "1.3.6.1.4.1.12356", ["1.3.6.1.4.1.12356.101.4.1.3.0", "1.3.6.1.4.1.12356.101.12.2.2.1.20.1"]),
    "127.0.0.105": ("cisco_ios", "1.3.6.1.4.1.9", ["1.3.6.1.4.1.9.9.109.1.1.1.1.8.1", "1.3.6.1.4.1.9.9.13.1.5.1.3.2"]),
    "127.0.0.106": ("paloalto_panos", "1.3.6.1.4.1.25461", ["1.3.6.1.4.1.25461.2.1.2.3.1.0", "1.3.6.1.4.1.25461.2.1.2.1.12.0"]),
    "127.0.0.107": ("mikrotik_routeros", "1.3.6.1.4.1.14988", ["1.3.6.1.4.1.14988.1.1.3.10.0", "1.3.6.1.4.1.14988.1.1.3.100.1.4.3"]),
    "127.0.0.108": ("apc_ups", "1.3.6.1.4.1.318", ["1.3.6.1.4.1.318.1.1.1.2.2.1.0", "1.3.6.1.4.1.318.1.1.1.4.1.1.0"]),
    "127.0.0.109": ("hpe_ilo", "1.3.6.1.4.1.232", ["1.3.6.1.4.1.232.6.1.3.0", "1.3.6.1.4.1.232.3.2.5.1.1.37.2"]),
    "127.0.0.110": ("dell_idrac", "1.3.6.1.4.1.674.10892.5", ["1.3.6.1.4.1.674.10892.5.2.1.0", "1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.24.1"]),
}


async def check_synthetic() -> None:
    for ip, (tid, prefix, oids) in SYNTH.items():
        so = await get(ip, "1.3.6.1.2.1.1.2.0")
        check(so == prefix or str(so).startswith(prefix + "."), f"{ip} {tid} sysObjectID {so}")
        for o in oids:
            check(await get(ip, o) is not None, f"{ip} {tid} {o} missing")


async def main() -> int:
    await check_arubaos("127.0.0.81")
    await check_comware("127.0.0.82")
    await check_cx("127.0.0.83")
    await check_arp("127.0.0.84", legacy=False)
    await check_arp("127.0.0.85", legacy=True)
    await check_aos52("127.0.0.86")
    await check_vendors()
    await check_synthetic()
    if FAIL:
        print("FAIL\n  " + "\n  ".join(FAIL))
        return 1
    print("OK: 2b-2 simulators match the contract tables (ports .81-.86, vendors .71-.73, synthetic .101-.110)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
