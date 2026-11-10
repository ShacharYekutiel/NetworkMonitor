"""[3] New-device detection, pure core (data-model § [3] New-device alerts "Pure rules and
vectors"): MAC / prefix parsing, `is_access_port`, `classify_sightings`, the flood guard."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

HEX_RE = re.compile(r"^[0-9a-f]+$")
PREFIX_BITS = {6: 24, 7: 28, 9: 36, 12: 48}
FLOOD_LIMIT = 20

SKIP_RANDOM = "skip_random"
UPDATE_KNOWN = "update_known"
BASELINE = "baseline"
ALLOWED = "allowed"
NEW = "new"


def _hex(value: str) -> str:
    return re.sub(r"[\s:.\-]", "", str(value).strip().lower())


def _colons(digits: str) -> str:
    return ":".join(digits[i : i + 2] for i in range(0, len(digits), 2))


def normalize_mac(value: str | None) -> str | None:
    """`aa:bb:cc:dd:ee:ff` from any common notation (`:`, `-`, `.` or none), else None."""
    if not value:
        return None
    h = _hex(value)
    if len(h) != 12 or not HEX_RE.match(h):
        return None
    return _colons(h)


def parse_prefix(value: str | None) -> tuple[str, int] | None:
    """Allow-list entry -> (normalised prefix, bits): 6 hex digits (OUI, 24 bits), 7 (28),
    9 (36) or 12 (a full MAC, 48)."""
    if not value:
        return None
    h = _hex(value)
    if not HEX_RE.match(h or "x") or len(h) not in PREFIX_BITS:
        return None
    return _colons(h), PREFIX_BITS[len(h)]


def prefix_matches(mac: str, prefix: str, bits: int) -> bool:
    return _hex(mac)[: bits // 4] == _hex(prefix)[: bits // 4]


def locally_administered(mac: str) -> bool:
    """Bit 0x02 of the first octet (randomised / VM MACs)."""
    try:
        return bool(int(_hex(mac)[:2], 16) & 0x02)
    except ValueError:
        return False


def multicast(mac: str) -> bool:
    try:
        return bool(int(_hex(mac)[:2], 16) & 0x01)
    except ValueError:
        return False


def is_access_port(port: dict[str, Any], *, is_uplink: bool, excluded: Iterable[int] = ()) -> bool:
    """`port`: a switch_ports row (kind, lag_if_index, mode, if_index); `is_uplink`: the 2b-2
    uplink rule (neighbour or mac_count > threshold)."""
    if port.get("kind") != "physical":
        return False
    if port.get("lag_if_index") is not None:
        return False
    if is_uplink:
        return False
    if port.get("mode") == "trunk":
        return False
    return int(port.get("if_index") or 0) not in set(excluded)


@dataclass(frozen=True)
class Sighting:
    mac: str
    if_index: int
    vlan: int | None = None
    port_name: str = ""


@dataclass(frozen=True)
class ClassifyCtx:
    first_read: bool
    learning: bool
    ignore_random: bool = True


def classify_mac(
    mac: str, known: set[str], allowlist: list[tuple[str, int]], ctx: ClassifyCtx
) -> str:
    """Architecture § 13.4 rules 2-7, in this order."""
    if ctx.ignore_random and (locally_administered(mac) or multicast(mac)):
        return SKIP_RANDOM
    if mac in known:
        return UPDATE_KNOWN
    if ctx.first_read or ctx.learning:
        return BASELINE
    if any(prefix_matches(mac, p, b) for p, b in allowlist):
        return ALLOWED
    return NEW


def classify_sightings(
    sightings: list[Sighting],
    known: set[str],
    allowlist: list[tuple[str, int]],
    ctx: ClassifyCtx,
) -> list[tuple[Sighting, str]]:
    """One decision per MAC (the first sighting of a MAC wins)."""
    out: list[tuple[Sighting, str]] = []
    seen: set[str] = set()
    for s in sightings:
        if s.mac in seen:
            continue
        seen.add(s.mac)
        out.append((s, classify_mac(s.mac, known, allowlist, ctx)))
    return out


def flood_split(new: list[Any], limit: int = FLOOD_LIMIT) -> tuple[list[Any], int]:
    """(the items reported individually, the count summarised as `+N more`)."""
    return new[:limit], max(0, len(new) - limit)


def device_message(
    mac: str,
    vendor: str | None,
    switch: str,
    port: str | None,
    vlan: int | None,
    ip: str | None,
    hostname: str | None,
) -> str:
    """`New device 00:11:22:33:44:55 (Hewlett Packard) on sw-floor2 port 1/0/17, VLAN 20,
    10.0.20.57 pc-1234.mnidom.net`"""
    msg = f"New device {mac}"
    if vendor:
        msg += f" ({vendor})"
    msg += f" on {switch}"
    if port:
        msg += f" port {port}"
    tail = []
    if vlan is not None:
        tail.append(f"VLAN {vlan}")
    addr = " ".join(x for x in (ip, hostname) if x)
    if addr:
        tail.append(addr)
    if tail:
        msg += ", " + ", ".join(tail)
    return msg


def summary_message(n: int, switch: str) -> str:
    return f"+{n} more new devices on {switch}"
