"""[2b-3] Source -> device attribution (architecture section 12.2.5).

`SourceMap` is built from the device list (pure `build`); hostname resolution happens in the
pipeline (threads) and is passed in. `attribute()` is pure."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

# precedence classes (lower wins)
EXPLICIT, LITERAL, RESOLVED = 0, 1, 2


def normalize_ip(value: str) -> str | None:
    """Canonical text of an IP; IPv4-mapped IPv6 -> IPv4; None when not an IP."""
    try:
        addr = ipaddress.ip_address(str(value).strip().split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return str(addr)


@dataclass
class SourceMap:
    by_ip: dict[str, int] = field(default_factory=dict)
    by_name: dict[str, set[int]] = field(default_factory=dict)
    devices: dict[int, dict[str, Any]] = field(default_factory=dict)  # id -> info

    def device(self, device_id: int | None) -> dict[str, Any] | None:
        return self.devices.get(device_id) if device_id is not None else None


def build(devices: list[dict[str, Any]], resolved: dict[str, list[str]]) -> SourceMap:
    """`devices`: {id, name, host, enabled, source_addresses, ...}; `resolved`: hostname ->
    its A/AAAA addresses."""
    smap = SourceMap()
    best: dict[str, tuple[int, int, int]] = {}  # ip -> (class, disabled, id)

    def offer(ip: str | None, cls: int, d: dict[str, Any]) -> None:
        if ip is None:
            return
        key = (cls, 0 if d.get("enabled", True) else 1, int(d["id"]))
        cur = best.get(ip)
        if cur is None or key < cur:
            best[ip] = key

    for d in devices:
        smap.devices[int(d["id"])] = d
        for addr in d.get("source_addresses") or []:
            offer(normalize_ip(str(addr)), EXPLICIT, d)
        host = str(d.get("host") or "").strip()
        lit = normalize_ip(host)
        if lit is not None:
            offer(lit, LITERAL, d)
        elif host:
            for addr in resolved.get(host.lower(), []):
                offer(normalize_ip(addr), RESOLVED, d)
        for key in _name_keys(d):
            smap.by_name.setdefault(key, set()).add(int(d["id"]))
    smap.by_ip = {ip: k[2] for ip, k in best.items()}
    return smap


def _name_keys(d: dict[str, Any]) -> set[str]:
    keys = set()
    host = str(d.get("host") or "").strip().lower()
    name = str(d.get("name") or "").strip().lower()
    if host:
        keys.add(host)
    if name:
        keys.add(name)
        keys.add(name.split(".", 1)[0])
    return keys


def attribute(
    smap: SourceMap,
    source_ip: str,
    *,
    agent_addr: str | None = None,
    trap_address: str | None = None,
    hostname: str | None = None,
) -> int | None:
    """First hit wins: source IP, then (traps) v1 agent_addr and snmpTrapAddress.0, then
    (syslog) a unique hostname match."""
    for ip in (source_ip, agent_addr, trap_address):
        if not ip:
            continue
        n = normalize_ip(ip)
        if n is not None and n in smap.by_ip:
            return smap.by_ip[n]
    if hostname:
        ids = smap.by_name.get(hostname.strip().lower())
        if ids is not None and len(ids) == 1:
            return next(iter(ids))
    return None


def in_networks(ip: str, networks: list[Any]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr.version == n.version and addr in n for n in networks)


def parse_networks(values: list[str]) -> list[Any]:
    out = []
    for v in values:
        try:
            out.append(ipaddress.ip_network(str(v).strip(), strict=False))
        except ValueError:
            continue
    return out


NAT_NETWORKS = (ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("192.168.65.0/24"))


def nat_suspect(recent: list[tuple[str, bool]]) -> str | None:
    """`recent`: the last <= 200 (source_ip, is_device) pairs. Returns the address when >= 90 %
    come from one non-device IP in 172.16.0.0/12 or 192.168.65.0/24."""
    if len(recent) < 20:
        return None
    counts: dict[str, int] = {}
    for ip, is_device in recent:
        if not is_device:
            counts[ip] = counts.get(ip, 0) + 1
    if not counts:
        return None
    ip, n = max(counts.items(), key=lambda kv: kv[1])
    if n * 10 < len(recent) * 9:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if any(addr.version == net.version and addr in net for net in NAT_NETWORKS):
        return ip
    return None
