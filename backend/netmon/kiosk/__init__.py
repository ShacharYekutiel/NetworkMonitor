"""[3] NOC kiosk screens (architecture § 13.7): screen tokens, IP/CIDR guard, failure throttle.

The token travels in the URL fragment (`/noc#nmk_…`), never reaches a log, and is sent by the
SPA as the header `X-Netmon-Kiosk`. Every guard failure answers `404 not_found` (wrong token,
wrong IP and feature OFF look the same); 30 failures per IP per 5 min → 429."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from collections import defaultdict, deque

TOKEN_PREFIX = "nmk_"
HEADER = "x-netmon-kiosk"
WIDGETS = ("summary", "down_devices", "problems", "recent_alerts", "map", "group_tiles")
MAX_SCREENS = 20
FAIL_MAX = 30
FAIL_WINDOW_S = 300.0


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    return token[:12]


def plausible_token(token: str | None) -> bool:
    return (
        isinstance(token, str)
        and token.startswith(TOKEN_PREFIX)
        and 20 <= len(token) <= 64
        and all(c.isalnum() or c in "-_" for c in token[4:])
    )


def hash_matches(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(token_hash(token), (stored_hash or "").strip())


def _addr(ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        a = ipaddress.ip_address((ip or "").strip().split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped is not None:
        return a.ipv4_mapped
    return a


def normalize_source(value: str) -> str:
    """Validate one allowed source (IP or CIDR) → canonical text. Raises ValueError."""
    v = value.strip()
    if not v:
        raise ValueError("empty source")
    net = ipaddress.ip_network(v, strict=False)
    if isinstance(net, ipaddress.IPv6Network) and net.network_address.ipv4_mapped is not None:
        if net.prefixlen >= 96:
            net = ipaddress.ip_network(
                f"{net.network_address.ipv4_mapped}/{net.prefixlen - 96}", strict=False
            )
    if net.num_addresses == 1:
        return str(net.network_address)
    return str(net)


def guard(ip: str, sources: list[str]) -> bool:
    """Pure: is `ip` inside one of `sources` (data-model guard vectors)."""
    a = _addr(ip)
    if a is None or not sources:
        return False
    for s in sources:
        try:
            net = ipaddress.ip_network(normalize_source(s), strict=False)
        except ValueError:
            continue
        if a.version == net.version and a in net:
            return True
    return False


class FailureLimiter:
    """30 failed kiosk requests per IP per 5 minutes → 429."""

    def __init__(self, max_failures: int = FAIL_MAX, window_s: float = FAIL_WINDOW_S) -> None:
        self.max = max_failures
        self.window = window_s
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self.reasons: dict[str, int] = defaultdict(int)

    def _prune(self, ip: str) -> deque[float]:
        q = self._fails[ip]
        cut = time.monotonic() - self.window
        while q and q[0] < cut:
            q.popleft()
        return q

    def blocked(self, ip: str) -> bool:
        return ip in self._fails and len(self._prune(ip)) >= self.max

    def fail(self, ip: str, reason: str) -> None:
        self._prune(ip).append(time.monotonic())
        self.reasons[reason] += 1
        if len(self._fails) > 10000:
            for k in list(self._fails)[:5000]:
                if not self._prune(k):
                    del self._fails[k]

    def clear(self) -> None:
        self._fails.clear()
        self.reasons.clear()
