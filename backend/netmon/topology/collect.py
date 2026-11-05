"""[2b-1] LLDP / CDP collection over SNMP: walks (I/O) and pure parsers.

LLDP-MIB `1.0.8802.1.1.2`, CDP `1.3.6.1.4.1.9.9.23`. See docs/data-model.md "Topology"."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from netmon.poller.snmp_client import SnmpError, snmp_get_typed, snmp_walk, walk_to_index

log = logging.getLogger("netmon.topology.collect")

LOC_CHASSIS_SUBTYPE = "1.0.8802.1.1.2.1.3.1.0"
LOC_CHASSIS_ID = "1.0.8802.1.1.2.1.3.2.0"
LOC_SYS_NAME = "1.0.8802.1.1.2.1.3.3.0"
LOC_PORT_TABLE = "1.0.8802.1.1.2.1.3.7.1"  # .2 subtype, .3 id, .4 desc; index lldpLocPortNum
REM_TABLE = "1.0.8802.1.1.2.1.4.1.1"  # index timeMark.localPortNum.remIndex
REM_MAN_ADDR_TABLE = "1.0.8802.1.1.2.1.4.2.1"  # .3 IfSubtype; address in the index
CDP_CACHE_TABLE = "1.3.6.1.4.1.9.9.23.1.2.1.1"  # index cdpCacheIfIndex.cdpCacheDeviceIndex
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
MAX_ROWS = 5000

# subtypes (LLDP-MIB): chassis 4 = macAddress, 5 = networkAddress; port 3 = macAddress,
# 4 = networkAddress, 5 = interfaceName, 7 = local
CHASSIS_MAC, CHASSIS_NET = 4, 5
PORT_MAC, PORT_NET, PORT_IFNAME, PORT_LOCAL = 3, 4, 5, 7

_MAC_TEXT = re.compile(
    r"^([0-9a-f]{2})[:\-. ]?([0-9a-f]{2})[:\-. ]?([0-9a-f]{2})[:\-. ]?"
    r"([0-9a-f]{2})[:\-. ]?([0-9a-f]{2})[:\-. ]?([0-9a-f]{2})$",
    re.I,
)


# ------------------------------------------------------------------ pure helpers
def _bytes(v: Any) -> bytes:
    if v is None:
        return b""
    if isinstance(v, bytes):
        return v
    if isinstance(v, int):
        return str(v).encode()
    return str(v).encode("utf-8", errors="replace")


def _printable(b: bytes) -> str | None:
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        return None
    s = s.strip("\x00").strip()
    if s and all(ch.isprintable() for ch in s):
        return s
    return None


def mac_text(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def _addr_text(b: bytes) -> str | None:
    """An LLDP networkAddress (IANA family octet + address) or a bare 4/16-octet address."""
    if len(b) == 5 and b[0] == 1:
        return str(ipaddress.IPv4Address(b[1:]))
    if len(b) == 17 and b[0] == 2:
        return str(ipaddress.IPv6Address(b[1:]))
    if len(b) == 4:
        return str(ipaddress.IPv4Address(b))
    if len(b) == 16:
        return str(ipaddress.IPv6Address(b))
    return None


def normalize_id(value: Any, subtype: int | None, kind: str = "chassis") -> str:
    """Chassis/port id normalisation: MAC -> `aa:bb:cc:dd:ee:ff`; network address -> text;
    else decoded text when printable, else lower-case hex `0x…`."""
    b = _bytes(value)
    mac_sub = CHASSIS_MAC if kind == "chassis" else PORT_MAC
    net_sub = CHASSIS_NET if kind == "chassis" else PORT_NET
    if subtype == mac_sub or (len(b) == 6 and subtype in (3, 4)):
        if len(b) == 6:
            return mac_text(b)
        text = _printable(b)
        if text and (m := _MAC_TEXT.match(text)):
            return ":".join(g.lower() for g in m.groups())
    if subtype == net_sub:
        addr = _addr_text(b)
        if addr:
            return addr
    text = _printable(b)
    if text is not None:
        if (m := _MAC_TEXT.match(text)) and subtype in (mac_sub, None):
            return ":".join(g.lower() for g in m.groups())
        return text[:255]
    return ("0x" + b.hex())[:255]


def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    try:
        return int(_bytes(v).decode().strip())
    except (ValueError, UnicodeDecodeError):
        return None


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace").strip("\x00").strip()
    return str(v).strip()


def table_columns(rows: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """walk_to_index of a table entry -> {column: {index_suffix: value}}."""
    out: dict[int, dict[str, Any]] = {}
    for suffix, v in rows.items():
        col, _, idx = suffix.partition(".")
        if not col.isdigit() or not idx:
            continue
        out.setdefault(int(col), {})[idx] = v
    return out


@dataclass
class LocalPort:
    num: int
    subtype: int | None
    port_id: str
    desc: str


@dataclass
class Remote:
    local_port: int
    rem_index: int
    chassis_subtype: int | None
    chassis_id: str
    port_subtype: int | None
    port_id: str
    port_desc: str
    sys_name: str
    sys_descr: str
    mgmt_addresses: list[str] = field(default_factory=list)


@dataclass
class CdpEntry:
    if_index: int
    device_index: int
    device_id: str
    device_port: str
    platform: str
    address: str | None


@dataclass
class DeviceData:
    ok: bool = False
    error: str | None = None
    chassis_id: str | None = None
    sys_name: str | None = None
    local_ports: dict[int, LocalPort] = field(default_factory=dict)
    remotes: list[Remote] = field(default_factory=list)
    cdp: list[CdpEntry] = field(default_factory=list)
    if_names: dict[int, str] = field(default_factory=dict)
    if_descrs: dict[int, str] = field(default_factory=dict)


def parse_local_ports(rows: dict[str, Any]) -> dict[int, LocalPort]:
    cols = table_columns(rows)
    out: dict[int, LocalPort] = {}
    nums = set(cols.get(2, {})) | set(cols.get(3, {})) | set(cols.get(4, {}))
    for idx in nums:
        if not idx.isdigit():
            continue
        sub = _int(cols.get(2, {}).get(idx))
        out[int(idx)] = LocalPort(
            int(idx),
            sub,
            normalize_id(cols.get(3, {}).get(idx), sub, "port") if idx in cols.get(3, {}) else "",
            _text(cols.get(4, {}).get(idx)),
        )
    return out


def parse_man_addr_suffix(suffix: str) -> tuple[int, int, str] | None:
    """`timeMark.localPortNum.remIndex.addrSubtype.len.o1.o2…` -> (local port, rem index,
    address text). Only IPv4 (1, len 4) and IPv6 (2, len 16)."""
    parts = suffix.split(".")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) < 6:
        return None
    _, local, rem, subtype, length = nums[:5]
    octets = nums[5:]
    if len(octets) != length or any(not 0 <= o <= 255 for o in octets):
        return None
    if subtype == 1 and length == 4:
        return local, rem, str(ipaddress.IPv4Address(bytes(octets)))
    if subtype == 2 and length == 16:
        return local, rem, str(ipaddress.IPv6Address(bytes(octets)))
    return None


def parse_remotes(rem_rows: dict[str, Any], man_rows: dict[str, Any]) -> list[Remote]:
    cols = table_columns(rem_rows)
    keys: dict[tuple[int, int], str] = {}
    for col in cols.values():
        for idx in col:
            parts = idx.split(".")
            if len(parts) != 3 or not all(p.isdigit() for p in parts):
                continue
            keys[(int(parts[1]), int(parts[2]))] = idx  # the time mark is ignored
    addrs: dict[tuple[int, int], list[str]] = {}
    for suffix in table_columns(man_rows).get(3, {}):
        parsed = parse_man_addr_suffix(suffix)
        if parsed is not None:
            lst = addrs.setdefault((parsed[0], parsed[1]), [])
            if parsed[2] not in lst:
                lst.append(parsed[2])
    out = []
    for (local, rem), idx in sorted(keys.items()):

        def col(n: int, i: str = idx) -> Any:
            return cols.get(n, {}).get(i)

        csub, psub = _int(col(4)), _int(col(6))
        found = addrs.get((local, rem), [])
        v4 = [a for a in found if ":" not in a]
        v6 = [a for a in found if ":" in a]
        out.append(
            Remote(
                local_port=local,
                rem_index=rem,
                chassis_subtype=csub,
                chassis_id=normalize_id(col(5), csub, "chassis") if col(5) is not None else "",
                port_subtype=psub,
                port_id=normalize_id(col(7), psub, "port") if col(7) is not None else "",
                port_desc=_text(col(8)),
                sys_name=_text(col(9)),
                sys_descr=_text(col(10))[:1000],
                mgmt_addresses=v4 + v6,
            )
        )
    return out


def parse_cdp(rows: dict[str, Any]) -> list[CdpEntry]:
    cols = table_columns(rows)
    keys = set()
    for c in (6, 7):
        keys |= set(cols.get(c, {}))
    out = []
    for idx in sorted(keys):
        parts = idx.split(".")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            continue
        addr = None
        atype = _int(cols.get(3, {}).get(idx))
        raw = _bytes(cols.get(4, {}).get(idx))
        if atype == 1 and len(raw) == 4:
            addr = str(ipaddress.IPv4Address(raw))
        out.append(
            CdpEntry(
                if_index=int(parts[0]),
                device_index=int(parts[1]),
                device_id=_text(cols.get(6, {}).get(idx))[:240],
                device_port=_text(cols.get(7, {}).get(idx)),
                platform=_text(cols.get(8, {}).get(idx)),
                address=addr,
            )
        )
    return out


def _if_lookup(text: str, if_names: dict[int, str], if_descrs: dict[int, str]) -> int | None:
    if not text:
        return None
    for table in (if_names, if_descrs):
        hits = sorted(i for i, n in table.items() if n == text)
        if hits:
            return hits[0]
    return None


def port_name(if_index: int | None, if_names: dict[int, str], if_descrs: dict[int, str]) -> str:
    if if_index is None:
        return ""
    return if_names.get(if_index) or if_descrs.get(if_index) or ""


def resolve_local_port(
    port: LocalPort | None, port_num: int, if_names: dict[int, str], if_descrs: dict[int, str]
) -> tuple[int | None, str]:
    """Local LLDP port -> (ifIndex | None, port name). Rules, in order: port id (subtype 5/7)
    equals an ifName/ifDescr; port desc equals one; lldpLocPortNum is an ifIndex; else none."""
    if port is not None and port.subtype in (PORT_IFNAME, PORT_LOCAL):
        idx = _if_lookup(port.port_id, if_names, if_descrs)
        if idx is not None:
            return idx, port_name(idx, if_names, if_descrs)
    if port is not None:
        idx = _if_lookup(port.desc, if_names, if_descrs)
        if idx is not None:
            return idx, port_name(idx, if_names, if_descrs)
    if port_num in if_names or port_num in if_descrs:
        return port_num, port_name(port_num, if_names, if_descrs)
    fallback = ""
    if port is not None:
        fallback = port.desc or port.port_id
    return None, (fallback or str(port_num))[:255]


def resolve_remote_port(
    subtype: int | None,
    port_id: str,
    port_desc: str,
    if_names: dict[int, str],
    if_descrs: dict[int, str],
) -> int | None:
    """Remote port -> ifIndex of the matched device (port id subtype 5/7, or port desc)."""
    if subtype in (PORT_IFNAME, PORT_LOCAL):
        idx = _if_lookup(port_id, if_names, if_descrs)
        if idx is not None:
            return idx
    return _if_lookup(port_desc, if_names, if_descrs)


def norm_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip().strip("[]")))
    except ValueError:
        return None


def name_keys(name: str | None) -> set[str]:
    """Lower-case name and the name with the domain stripped (`sw1.lan` -> `sw1`)."""
    if not name:
        return set()
    n = name.strip().lower()
    if not n:
        return set()
    out = {n}
    if norm_ip(n) is None and "." in n:
        out.add(n.split(".", 1)[0])
    return out


# ------------------------------------------------------------------ I/O
async def collect_device(host: str, creds: dict[str, Any]) -> DeviceData:
    """Local scalars, lldpLocPortTable, lldpRemTable, lldpRemManAddrTable, cdpCacheTable and
    ifName/ifDescr. Missing tables are no rows; a timeout/auth error marks the device failed."""
    data = DeviceData()
    errors: list[str] = []
    try:
        vals = await snmp_get_typed(
            host, creds, [LOC_CHASSIS_SUBTYPE, LOC_CHASSIS_ID, LOC_SYS_NAME]
        )
        data.ok = True
        sub = _int(vals[LOC_CHASSIS_SUBTYPE][0])
        raw = vals[LOC_CHASSIS_ID][0]
        if raw is not None:
            data.chassis_id = normalize_id(raw, sub, "chassis") or None
        name = _text(vals[LOC_SYS_NAME][0])
        data.sys_name = name[:255] or None
    except SnmpError as exc:
        errors.append(str(exc))

    async def walk(oid: str) -> dict[str, Any]:
        try:
            rows = await snmp_walk(host, creds, oid, max_rows=MAX_ROWS)
        except SnmpError as exc:
            errors.append(str(exc))
            return {}
        data.ok = True
        return walk_to_index(rows, oid)

    loc = await walk(LOC_PORT_TABLE)
    if not data.ok and errors:
        data.error = errors[0][:500]  # first two requests failed: unreachable / auth
        return data
    rem = await walk(REM_TABLE)
    man = await walk(REM_MAN_ADDR_TABLE) if rem else {}
    cdp = await walk(CDP_CACHE_TABLE)
    names = await walk(IF_NAME)
    descrs = await walk(IF_DESCR)
    data.local_ports = parse_local_ports(loc)
    data.remotes = parse_remotes(rem, man)
    data.cdp = parse_cdp(cdp)
    data.if_names = {int(k): _text(v) for k, v in names.items() if k.isdigit()}
    data.if_descrs = {int(k): _text(v) for k, v in descrs.items() if k.isdigit()}
    if not data.ok:
        data.error = (errors[0] if errors else "no answer")[:500]
    return data


async def collect_with_budget(host: str, creds: dict[str, Any], budget_s: float) -> DeviceData:
    try:
        return await asyncio.wait_for(collect_device(host, creds), budget_s)
    except TimeoutError:
        return DeviceData(ok=False, error=f"Timed out after {budget_s:g} s")
