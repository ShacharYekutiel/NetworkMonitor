"""[2b-2] Port inventory SNMP collection: walks (I/O) and the pure parsers.

docs/data-model.md "Port inventory: SNMP data". Every walk is one bulk walk of one column (25
varbinds per PDU); there are no per-port GETs. Each walk holds the runtime's shared SNMP
semaphore only while it runs, so the poller keeps getting slots between walks."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from netmon.poller.snmp_client import SnmpError, snmp_get, snmp_walk

log = logging.getLogger("netmon.ports.collect")

# ------------------------------------------------------------------ OIDs
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
IF_ENTRY = "1.3.6.1.2.1.2.2.1"
IFX_ENTRY = "1.3.6.1.2.1.31.1.1.1"
DOT3_DUPLEX = "1.3.6.1.2.1.10.7.2.1.19"
DOT1D_BASE_PORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"
DOT1Q_PVID = "1.3.6.1.2.1.17.7.1.4.5.1.1"
DOT1Q_STATIC_EGRESS = "1.3.6.1.2.1.17.7.1.4.3.1.2"
DOT1Q_STATIC_UNTAGGED = "1.3.6.1.2.1.17.7.1.4.3.1.4"
DOT1Q_CURRENT_EGRESS = "1.3.6.1.2.1.17.7.1.4.2.1.4"
DOT1Q_CURRENT_UNTAGGED = "1.3.6.1.2.1.17.7.1.4.2.1.5"
DOT1Q_VLAN_FDB_ID = "1.3.6.1.2.1.17.7.1.4.2.1.3"
DOT1Q_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"
DOT1Q_FDB_STATUS = "1.3.6.1.2.1.17.7.1.2.2.1.3"
DOT1D_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"
DOT1D_FDB_STATUS = "1.3.6.1.2.1.17.4.3.1.3"
PETH_PORT_ADMIN = "1.3.6.1.2.1.105.1.1.1.3"
PETH_PORT_DETECTION = "1.3.6.1.2.1.105.1.1.1.6"
PETH_PORT_CLASS = "1.3.6.1.2.1.105.1.1.1.10"
IP_NET_TO_PHYS_ADDR = "1.3.6.1.2.1.4.35.1.4"
IP_NET_TO_PHYS_TYPE = "1.3.6.1.2.1.4.35.1.6"
IP_NET_TO_MEDIA_ADDR = "1.3.6.1.2.1.4.22.1.2"
IP_NET_TO_MEDIA_TYPE = "1.3.6.1.2.1.4.22.1.4"

IF_COLUMNS = {
    "descr": 2,
    "type": 3,
    "speed": 5,
    "phys": 6,
    "admin": 7,
    "oper": 8,
    "last_change": 9,
    "in_octets": 10,
    "in_errors": 14,
    "out_octets": 16,
    "out_errors": 20,
}
IFX_COLUMNS = {"name": 1, "hc_in": 6, "hc_out": 10, "high_speed": 15, "alias": 18}

TABLE_CAP = 5000  # ifTable/ifXTable/dot1d/dot1q/PoE columns
FDB_CAP = 20000  # FDB and ARP

# vendor slug -> (pethPsePortTable-indexed power column OID, multiplier to W). Empty in 2b-2:
# no vendor column is trusted yet (architecture § 11.8).
PORT_POWER_OIDS: dict[str, tuple[str, float]] = {}

IF_STATUS = {
    1: "up",
    2: "down",
    3: "testing",
    4: "unknown",
    5: "dormant",
    6: "notPresent",
    7: "lowerLayerDown",
}
POE_DETECTION = {
    1: "disabled",
    2: "searching",
    3: "delivering",
    4: "fault",
    5: "test",
    6: "other_fault",
}
DUPLEX = {1: "unknown", 2: "half", 3: "full"}

_MGMT_RE = re.compile(r"^(m-gigabitethernet|mgmt|management|oobm|mgmteth|ma\d)", re.IGNORECASE)
PHYSICAL_TYPES = {6, 62, 69, 117}
LAG_TYPES = {161, 54}
VLAN_TYPES = {53, 136, 135}


# ------------------------------------------------------------------ pure helpers
def to_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, bytes):
        try:
            return int(v.decode("ascii").strip())
        except (UnicodeDecodeError, ValueError):
            return None
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return None
    return None


def to_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace").replace("\x00", "").strip()
    return str(v).strip()


def port_kind(if_type: int | None, name: str) -> str:
    """physical | lag | vlan | mgmt | other (first match wins)."""
    if _MGMT_RE.match(name or ""):
        return "mgmt"
    if if_type in PHYSICAL_TYPES:
        return "physical"
    if if_type in LAG_TYPES:
        return "lag"
    if if_type in VLAN_TYPES:
        return "vlan"
    return "other"


def mac_text(octets: Any) -> str | None:
    """6 octets -> lower-case aa:bb:cc:dd:ee:ff, else None."""
    if isinstance(octets, (bytes, bytearray)) and len(octets) == 6:
        return ":".join(f"{b:02x}" for b in octets)
    return None


def mac_from_arcs(arcs: list[int] | tuple[int, ...]) -> str | None:
    if len(arcs) != 6 or any(not 0 <= a <= 255 for a in arcs):
        return None
    return ":".join(f"{a:02x}" for a in arcs)


def locally_administered(mac: str | None) -> bool:
    """Bit 0x02 of the first octet (random / private MACs)."""
    if not mac:
        return False
    try:
        return bool(int(mac[:2], 16) & 0x02)
    except ValueError:
        return False


def portlist(octets: Any) -> set[int]:
    """Q-BRIDGE PortList: bit 7 (MSB) of octet 0 = port 1 ... bit 0 of octet 0 = port 8, MSB of
    octet 1 = port 9, ... A non-bytes value -> empty set."""
    if not isinstance(octets, (bytes, bytearray)):
        return set()
    out = set()
    for i, byte in enumerate(octets):
        for bit in range(8):
            if byte & (0x80 >> bit):
                out.add(i * 8 + bit + 1)
    return out


def _arcs(suffix: str) -> list[int] | None:
    try:
        return [int(p) for p in suffix.split(".")]
    except ValueError:
        return None


def _suffix_key(s: str) -> tuple[int, ...]:
    a = _arcs(s)
    return tuple(a) if a is not None else (1 << 30,)


def int_index(col: dict[str, Any]) -> dict[int, Any]:
    """{suffix: value} with single-arc integer suffixes -> {int: value}."""
    out: dict[int, Any] = {}
    for k, v in col.items():
        if k.isdigit():
            out[int(k)] = v
    return out


def bridge_map(base_port_ifindex: dict[int, Any], if_indexes: set[int]) -> dict[int, int]:
    """bridge port -> ifIndex from dot1dBasePortIfIndex; when that table has no rows, bridge port
    n -> ifIndex n for every existing ifIndex (ArubaOS-Switch style)."""
    if base_port_ifindex:
        out = {}
        for bp, v in base_port_ifindex.items():
            idx = to_int(v)
            if idx is not None and idx > 0:
                out[bp] = idx
        return out
    return {i: i for i in if_indexes}


@dataclass
class PortVlans:
    pvid: int | None = None
    mode: str | None = None
    untagged: list[int] = field(default_factory=list)
    tagged: list[int] = field(default_factory=list)


def vlan_sets(
    egress: dict[int, Any], untagged: dict[int, Any], pvids: dict[int, Any], bridge_ports: set[int]
) -> dict[int, PortVlans]:
    """Per bridge port: untagged = {v : p in Untagged_v}, tagged = {v : p in Egress_v minus
    Untagged_v}; mode trunk if tagged, access if untagged, else None; pvid from dot1qPvid, else
    the single untagged VLAN."""
    eg = {v: portlist(x) for v, x in egress.items()}
    un = {v: portlist(x) for v, x in untagged.items()}
    out: dict[int, PortVlans] = {}
    for p in bridge_ports:
        u = sorted(v for v, ports in un.items() if p in ports)
        t = sorted(v for v, ports in eg.items() if p in ports and p not in un.get(v, set()))
        mode = "trunk" if t else ("access" if u else None)
        pvid = to_int(pvids.get(p)) if p in pvids else None
        if pvid is None and len(u) == 1:
            pvid = u[0]
        out[p] = PortVlans(pvid, mode, u, t)
    return out


def current_vlan_columns(col: dict[str, Any]) -> dict[int, Any]:
    """dot1qVlanCurrentTable column (index TimeMark.VlanIndex) -> {vlan: value} (the newest
    TimeMark wins)."""
    best: dict[int, tuple[int, Any]] = {}
    for suffix, v in col.items():
        arcs = _arcs(suffix)
        if arcs is None or len(arcs) != 2:
            continue
        tm, vlan = arcs
        if vlan not in best or tm >= best[vlan][0]:
            best[vlan] = (tm, v)
    return {vlan: v for vlan, (_, v) in best.items()}


def fdb_id_to_vlan(vlan_fdb_ids: dict[str, Any]) -> dict[int, int]:
    """dot1qVlanFdbId (index TimeMark.VlanIndex -> FdbId) -> {fdbId: lowest VLAN}."""
    out: dict[int, int] = {}
    for suffix, v in vlan_fdb_ids.items():
        arcs = _arcs(suffix)
        fid = to_int(v)
        if arcs is None or len(arcs) != 2 or fid is None:
            continue
        vlan = arcs[1]
        if fid not in out or vlan < out[fid]:
            out[fid] = vlan
    return out


@dataclass(frozen=True)
class FdbEntry:
    mac: str
    vlan: int | None
    if_index: int


LEARNED = 3


def parse_fdb_q(
    ports: dict[str, Any],
    statuses: dict[str, Any],
    bridge: dict[int, int],
    fdb_vlans: dict[int, int] | None,
) -> list[FdbEntry]:
    """dot1qTpFdbTable: suffix fdbId.m1..m6; learned(3) only; bridge port 0 / unmapped dropped;
    vlan = the VLAN whose FdbId equals fdbId (when dot1qVlanFdbId has no rows: fdbId)."""
    out: list[FdbEntry] = []
    seen: set[tuple[str, int | None]] = set()
    for suffix in sorted(ports, key=_suffix_key):
        arcs = _arcs(suffix)
        if arcs is None or len(arcs) != 7:
            continue
        if to_int(statuses.get(suffix)) != LEARNED:
            continue
        mac = mac_from_arcs(arcs[1:])
        bp = to_int(ports[suffix])
        if mac is None or not bp or bp not in bridge:
            continue
        fid = arcs[0]
        vlan = fdb_vlans.get(fid) if fdb_vlans else fid
        if vlan is None:
            vlan = fid
        if (mac, vlan) in seen:
            continue
        seen.add((mac, vlan))
        out.append(FdbEntry(mac, vlan, bridge[bp]))
    return out


def parse_fdb_dot1d(
    ports: dict[str, Any], statuses: dict[str, Any], bridge: dict[int, int]
) -> list[FdbEntry]:
    """dot1dTpFdbTable fallback: suffix m1..m6, learned(3) only, vlan None."""
    out: list[FdbEntry] = []
    seen: set[str] = set()
    for suffix in sorted(ports, key=_suffix_key):
        arcs = _arcs(suffix)
        if arcs is None or len(arcs) != 6:
            continue
        if to_int(statuses.get(suffix)) != LEARNED:
            continue
        mac = mac_from_arcs(arcs)
        bp = to_int(ports[suffix])
        if mac is None or not bp or bp not in bridge or mac in seen:
            continue
        seen.add(mac)
        out.append(FdbEntry(mac, None, bridge[bp]))
    return out


@dataclass(frozen=True)
class ArpEntry:
    ip: str
    mac: str
    if_index: int | None


BAD_MACS = {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}


def _arp_mac(v: Any) -> str | None:
    mac = mac_text(v)
    return None if mac is None or mac in BAD_MACS else mac


def _ipv4(arcs: list[int]) -> str | None:
    if len(arcs) != 4 or any(not 0 <= a <= 255 for a in arcs):
        return None
    return str(ipaddress.IPv4Address(bytes(arcs)))


def parse_arp_phys(addrs: dict[str, Any], types: dict[str, Any]) -> list[ArpEntry]:
    """ipNetToPhysicalTable: suffix ifIndex.1.4.a.b.c.d (or ifIndex.1.a.b.c.d); Type dynamic(3) /
    static(4) only; other address types ignored."""
    out: dict[str, ArpEntry] = {}
    for suffix in sorted(addrs, key=_suffix_key):
        arcs = _arcs(suffix)
        if arcs is None or len(arcs) < 2 or arcs[1] != 1:
            continue
        rest = arcs[2:]
        if len(rest) == 5 and rest[0] == 4:
            rest = rest[1:]
        ip = _ipv4(rest)
        if ip is None or to_int(types.get(suffix)) not in (3, 4):
            continue
        mac = _arp_mac(addrs[suffix])
        if mac is None or ip in out:
            continue
        out[ip] = ArpEntry(ip, mac, arcs[0])
    return list(out.values())


def parse_arp_media(addrs: dict[str, Any], types: dict[str, Any]) -> list[ArpEntry]:
    """ipNetToMediaTable fallback: suffix ifIndex.a.b.c.d, Type dynamic(3) / static(4)."""
    out: dict[str, ArpEntry] = {}
    for suffix in sorted(addrs, key=_suffix_key):
        arcs = _arcs(suffix)
        if arcs is None or len(arcs) != 5:
            continue
        ip = _ipv4(arcs[1:])
        if ip is None or to_int(types.get(suffix)) not in (3, 4):
            continue
        mac = _arp_mac(addrs[suffix])
        if mac is None or ip in out:
            continue
        out[ip] = ArpEntry(ip, mac, arcs[0])
    return list(out.values())


_NUM = re.compile(r"\d+")


def name_tail(name: str) -> tuple[int, int] | None:
    """(member, port number) of a port name: member = the first number when the name has >= 2
    numbers, else 1; port number = the last number."""
    nums = [int(n) for n in _NUM.findall(name or "")]
    if not nums:
        return None
    return (nums[0] if len(nums) >= 2 else 1), nums[-1]


def map_pse_ports(
    keys: list[tuple[int, int]], ports: dict[int, str]
) -> dict[tuple[int, int], int] | None:
    """(pethPsePortGroupIndex, pethPsePortIndex) -> ifIndex. `ports` = {ifIndex: name} of the
    physical ports. Rule 1: every PSE port value is distinct and is a physical ifIndex. Rule 2:
    name tails (member = group, port number = port). None when no rule maps every row."""
    if not keys:
        return {}
    values = [p for _, p in keys]
    if len(set(values)) == len(values) and all(p in ports for p in values):
        return {k: k[1] for k in keys}
    tails: dict[tuple[int, int], int] = {}
    dup: set[tuple[int, int]] = set()
    for idx, name in ports.items():
        t = name_tail(name)
        if t is None:
            continue
        if t in tails:
            dup.add(t)
        tails[t] = idx
    out = {}
    for k in keys:
        if k not in tails or k in dup:
            return None
        out[k] = tails[k]
    return out


# ------------------------------------------------------------------ walks (I/O)
@dataclass
class PortWalks:
    """Raw results of one device run (None = not read / failed: see `sources`)."""

    sys_uptime: int | None = None
    if_cols: dict[str, dict[int, Any]] = field(default_factory=dict)
    ifx_cols: dict[str, dict[int, Any]] = field(default_factory=dict)
    duplex: dict[int, Any] = field(default_factory=dict)
    base_ports: dict[int, Any] = field(default_factory=dict)
    pvids: dict[int, Any] = field(default_factory=dict)
    egress: dict[int, Any] = field(default_factory=dict)
    untagged: dict[int, Any] = field(default_factory=dict)
    # raw FDB columns: Q-BRIDGE (ports, statuses, fdbId -> VLAN) or BRIDGE (ports, statuses);
    # mapped to ifIndexes by the snapshot builder (it owns the bridge-port map)
    fdb_q: tuple[dict[str, Any], dict[str, Any], dict[int, int]] | None = None
    fdb_d: tuple[dict[str, Any], dict[str, Any]] | None = None
    poe: dict[tuple[int, int], tuple[Any, Any, Any]] = field(default_factory=dict)
    arp: list[ArpEntry] = field(default_factory=list)
    sources: dict[str, Any] = field(default_factory=dict)
    error: str | None = None  # the ifTable read failed (run failed)


Walker = Callable[[str, int], Awaitable[dict[str, Any]]]


def make_walker(host: str, creds: dict[str, Any], sem: asyncio.Semaphore | None) -> Walker:
    async def walk(oid: str, cap: int) -> dict[str, Any]:
        if sem is not None:
            async with sem:
                rows = await snmp_walk(host, creds, oid, max_rows=cap)
        else:
            rows = await snmp_walk(host, creds, oid, max_rows=cap)
        prefix = oid + "."
        return {o[len(prefix) :]: v for o, v in rows if o.startswith(prefix)}

    return walk


async def get_uptime(host: str, creds: dict[str, Any], sem: asyncio.Semaphore | None) -> int | None:
    try:
        if sem is not None:
            async with sem:
                res = await snmp_get(host, creds, [SYS_UPTIME])
        else:
            res = await snmp_get(host, creds, [SYS_UPTIME])
    except SnmpError:
        return None
    return to_int(res.get(SYS_UPTIME))


class _Optional:
    """Read optional tables: no rows -> empty, SnmpError -> error."""

    def __init__(self, walk: Walker) -> None:
        self.walk = walk

    async def cols(
        self, oids: list[str], cap: int = TABLE_CAP, *, capped_is_error: bool = False
    ) -> list[dict[str, Any]] | None:
        """`capped_is_error`: a column that returns the cap is probably truncated -> error
        (keeps the previous data instead of marking the tail not present)."""
        out = []
        for oid in oids:
            try:
                rows = await self.walk(oid, cap)
            except SnmpError:
                return None
            if capped_is_error and len(rows) >= cap:
                log.warning("%s returned %d rows (the cap): treated as an error", oid, len(rows))
                return None
            out.append(rows)
        return out


def _state(cols: list[dict[str, Any]] | None) -> str:
    if cols is None:
        return "error"
    return "ok" if any(cols) else "empty"


async def collect_ports(walk: Walker, version: str, walks: PortWalks) -> None:
    """Walks #2-#11 sequentially into `walks`. A failing / empty ifDescr sets `walks.error`."""
    opt = _Optional(walk)
    try:
        descr = await walk(f"{IF_ENTRY}.{IF_COLUMNS['descr']}", TABLE_CAP)
    except SnmpError as exc:
        walks.error = str(exc) or "SNMP error"
        return
    descr_i = int_index(descr)
    if not descr_i:
        walks.error = "ifTable not available"
        return
    walks.if_cols["descr"] = descr_i
    for key, col in IF_COLUMNS.items():
        if key == "descr":
            continue
        try:
            walks.if_cols[key] = int_index(await walk(f"{IF_ENTRY}.{col}", TABLE_CAP))
        except SnmpError:
            walks.if_cols[key] = {}
    src = walks.sources
    # ifXTable (#4): not on SNMP v1
    if version == "v1":
        src["if_xtable"] = "skipped"
    else:
        cols = await opt.cols([f"{IFX_ENTRY}.{c}" for c in IFX_COLUMNS.values()])
        src["if_xtable"] = _state(cols)
        if cols is not None:
            walks.ifx_cols = {k: int_index(c) for k, c in zip(IFX_COLUMNS, cols, strict=True)}
    # EtherLike duplex (#5)
    cols = await opt.cols([DOT3_DUPLEX])
    src["duplex"] = _state(cols)
    if cols is not None:
        walks.duplex = int_index(cols[0])
    # bridge port -> ifIndex (#6)
    cols = await opt.cols([DOT1D_BASE_PORT_IFINDEX])
    src["bridge"] = _state(cols)
    if cols is not None:
        walks.base_ports = int_index(cols[0])
    # VLANs (#7, #8, #8b)
    src["vlan_mib"] = None
    pv = await opt.cols([DOT1Q_PVID])
    static = (
        await opt.cols([DOT1Q_STATIC_EGRESS, DOT1Q_STATIC_UNTAGGED]) if pv is not None else None
    )
    if pv is None or static is None:
        src["vlans"] = "error"
    else:
        walks.pvids = int_index(pv[0])
        if any(static):
            walks.egress, walks.untagged = int_index(static[0]), int_index(static[1])
            src["vlans"], src["vlan_mib"] = "ok", "static"
        else:
            cur = await opt.cols([DOT1Q_CURRENT_EGRESS, DOT1Q_CURRENT_UNTAGGED])
            if cur is None:
                src["vlans"] = "error"
            elif any(cur):
                walks.egress = current_vlan_columns(cur[0])
                walks.untagged = current_vlan_columns(cur[1])
                src["vlans"], src["vlan_mib"] = "ok", "current"
            else:
                src["vlans"] = "ok" if walks.pvids else "empty"
    # FDB (#9, #10, #10b): bridge-port mapping is applied by the snapshot builder
    src["fdb_mib"] = None
    q = await opt.cols([DOT1Q_FDB_PORT, DOT1Q_FDB_STATUS], FDB_CAP, capped_is_error=True)
    if q is None:
        src["fdb"] = "error"
    elif q[0]:
        fids = await opt.cols([DOT1Q_VLAN_FDB_ID])
        if fids is None:
            src["fdb"] = "error"
        else:
            src["fdb"], src["fdb_mib"] = "ok", "q_bridge"
            walks.fdb_q = (q[0], q[1], fdb_id_to_vlan(fids[0]))
    else:
        d = await opt.cols([DOT1D_FDB_PORT, DOT1D_FDB_STATUS], FDB_CAP, capped_is_error=True)
        if d is None:
            src["fdb"] = "error"
        elif d[0]:
            src["fdb"], src["fdb_mib"] = "ok", "bridge"
            walks.fdb_d = (d[0], d[1])
        else:
            src["fdb"] = "empty"
    # PoE (#11)
    cols = await opt.cols([PETH_PORT_ADMIN, PETH_PORT_DETECTION, PETH_PORT_CLASS])
    if cols is None:
        src["poe"] = "error"
    else:
        admin, det, cls = cols
        for suffix in admin.keys() | det.keys():
            arcs = _arcs(suffix)
            if arcs is None or len(arcs) != 2:
                continue
            walks.poe[(arcs[0], arcs[1])] = (admin.get(suffix), det.get(suffix), cls.get(suffix))
        src["poe"] = "ok" if walks.poe else "empty"


async def collect_arp(walk: Walker, walks: PortWalks) -> None:
    """Walks #12 / #12b into `walks.arp` and `sources.arp`."""
    opt = _Optional(walk)
    phys = await opt.cols([IP_NET_TO_PHYS_ADDR, IP_NET_TO_PHYS_TYPE], FDB_CAP, capped_is_error=True)
    if phys is None:
        walks.sources["arp"] = "error"
        return
    entries = parse_arp_phys(phys[0], phys[1])
    if not entries:
        media = await opt.cols(
            [IP_NET_TO_MEDIA_ADDR, IP_NET_TO_MEDIA_TYPE], FDB_CAP, capped_is_error=True
        )
        if media is None:
            walks.sources["arp"] = "error"
            return
        entries = parse_arp_media(media[0], media[1])
    walks.arp = entries
    walks.sources["arp"] = "ok" if entries else "empty"
