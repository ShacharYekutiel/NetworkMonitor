"""Scan target parsing and expansion (pure, no I/O). See docs/data-model.md "Scan targets"."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

MAX_HOSTS = 4096
MAX_TARGETS = 64

_REJECT_V4 = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
]
_REJECT_V4_RANGES = [(int(n.network_address), int(n.broadcast_address)) for n in _REJECT_V4]
_SHORT_RANGE = re.compile(r"^(\d{1,3}\.\d{1,3}\.\d{1,3}\.)(\d{1,3})-(\d{1,3})$")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*\.?$")


class TargetError(ValueError):
    """An invalid target entry (index into the request list)."""

    def __init__(self, index: int, message: str) -> None:
        super().__init__(message)
        self.index = index
        self.message = message


class TooManyHostsError(ValueError):
    def __init__(self, count: int, max_hosts: int = MAX_HOSTS) -> None:
        super().__init__(f"Targets expand to {count} hosts (max {max_hosts})")
        self.count = count
        self.max = max_hosts


@dataclass(frozen=True)
class HostTarget:
    """One host to scan: an IP address, or a hostname resolved at scan time."""

    address: str | None
    hostname: str | None = None

    @property
    def label(self) -> str:
        return self.address or self.hostname or ""


def rejected_reason(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if addr.version == 4:
        v = int(addr)
        for lo, hi in _REJECT_V4_RANGES:
            if lo <= v <= hi:
                if lo == int(ipaddress.IPv4Address("224.0.0.0")):
                    return "multicast addresses are not allowed"
                return "this address range is not allowed"
        return None
    if addr.scope_id:
        return "IPv6 scope ids (%zone) are not allowed"
    if addr.ipv4_mapped is not None:
        return "IPv4-mapped IPv6 addresses are not allowed; use the IPv4 address"
    if addr.is_multicast:
        return "multicast addresses are not allowed"
    if addr.is_unspecified:
        return "the unspecified address is not allowed"
    return None


def _check_v4_interval(index: int, lo: int, hi: int) -> None:
    for rlo, rhi in _REJECT_V4_RANGES:
        if lo <= rhi and hi >= rlo:
            first = ipaddress.IPv4Address(max(lo, rlo))
            raise TargetError(index, f"{first}: {rejected_reason(first)}")


def _v4(index: int, text: str) -> int:
    try:
        return int(ipaddress.IPv4Address(text))
    except ValueError as exc:
        raise TargetError(index, f"invalid IPv4 address: {text}") from exc


def parse_target(index: int, raw: str) -> tuple[str, object]:
    """Parse one entry. Returns ("v4", (lo, hi)) | ("v6", IPv6Address) | ("name", str)."""
    if not isinstance(raw, str):
        raise TargetError(index, "must be a string")
    text = raw.strip()
    if not text:
        raise TargetError(index, "must not be empty")
    if len(text) > 253:
        raise TargetError(index, "too long")
    if "/" in text:
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise TargetError(index, f"invalid CIDR: {text}") from exc
        if net.version == 6:
            raise TargetError(index, "IPv6 targets must be single addresses")
        if net.prefixlen < 8:
            raise TargetError(index, "CIDR prefix must be /8 to /32")
        lo, hi = int(net.network_address), int(net.broadcast_address)
        _check_v4_interval(index, lo, hi)
        if net.prefixlen <= 30:
            lo, hi = lo + 1, hi - 1
        return "v4", (lo, hi)
    m = _SHORT_RANGE.match(text)
    if m:
        start, end = int(m.group(2)), int(m.group(3))
        if start > end or end > 255:
            raise TargetError(index, "range must be start <= end <= 255")
        lo = _v4(index, f"{m.group(1)}{start}")
        hi = _v4(index, f"{m.group(1)}{end}")
        _check_v4_interval(index, lo, hi)
        return "v4", (lo, hi)
    if "-" in text and text.count("-") == 1 and ":" not in text:
        a, b = (p.strip() for p in text.split("-"))
        if re.fullmatch(r"[\d.]+", a) and re.fullmatch(r"[\d.]+", b):
            lo, hi = _v4(index, a), _v4(index, b)
            if lo > hi:
                raise TargetError(index, "range start must be <= end")
            _check_v4_interval(index, lo, hi)
            return "v4", (lo, hi)
    try:
        addr = ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        addr = None
    if addr is not None:
        reason = rejected_reason(addr)
        if reason:
            raise TargetError(index, f"{addr}: {reason}")
        if addr.version == 4:
            return "v4", (int(addr), int(addr))
        return "v6", addr
    if re.fullmatch(r"[\d.]+", text):
        raise TargetError(index, f"invalid IPv4 address: {text}")
    if not _HOSTNAME.match(text):
        raise TargetError(index, f"invalid target: {text}")
    return "name", text.rstrip(".")


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for lo, hi in sorted(i for i in intervals if i[0] <= i[1]):
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def count_and_parse(
    targets: list[str],
) -> tuple[list[tuple[int, int]], list[ipaddress.IPv6Address], list[str], int]:
    if not targets:
        raise TargetError(0, "at least one target is required")
    if len(targets) > MAX_TARGETS:
        raise TargetError(MAX_TARGETS, f"at most {MAX_TARGETS} targets")
    v4: list[tuple[int, int]] = []
    v6: set[ipaddress.IPv6Address] = set()
    names: dict[str, str] = {}
    for i, raw in enumerate(targets):
        kind, value = parse_target(i, raw)
        if kind == "v4":
            v4.append(value)  # type: ignore[arg-type]
        elif kind == "v6":
            v6.add(value)  # type: ignore[arg-type]
        else:
            names.setdefault(str(value).lower(), str(value))
    merged = _merge(v4)
    count = sum(hi - lo + 1 for lo, hi in merged) + len(v6) + len(names)
    return merged, sorted(v6), list(names.values()), count


def validate_targets(targets: list[str], max_hosts: int = MAX_HOSTS) -> int:
    """Validate and return the host count. Raises TargetError / TooManyHostsError."""
    _, _, _, count = count_and_parse(targets)
    if count > max_hosts:
        raise TooManyHostsError(count, max_hosts)
    return count


def expand_targets(targets: list[str], max_hosts: int = MAX_HOSTS) -> list[HostTarget]:
    """Sorted (IPv4 ascending, then IPv6, then hostnames in given order), de-duplicated."""
    merged, v6, names, count = count_and_parse(targets)
    if count > max_hosts:
        raise TooManyHostsError(count, max_hosts)
    hosts = [
        HostTarget(str(ipaddress.IPv4Address(v))) for lo, hi in merged for v in range(lo, hi + 1)
    ]
    hosts += [HostTarget(str(a)) for a in v6]
    hosts += [HostTarget(None, n) for n in names]
    return hosts
