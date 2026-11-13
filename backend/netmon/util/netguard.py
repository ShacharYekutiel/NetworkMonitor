"""[3b] SSRF guard for admin-supplied endpoint / proxy hosts (shared, dependency-free).

`blocked_endpoint_host` rejects the hosts that must never be reachable from a
server-side AWS SigV4 request (the signed ``Authorization`` header would otherwise be
sent to them):

* loopback (``127.0.0.0/8``, ``::1``) — relaxable with ``allow_loopback`` (wired to the
  server's test mode so the local test harness can keep using ``127.0.0.1``),
* link-local, which includes the ``169.254.169.254`` cloud-metadata address and IPv6
  ``fe80::/10`` — always blocked,
* the common cloud-metadata host aliases (Alibaba ``100.100.100.200`` and the literal
  hostnames ``metadata`` / ``metadata.google.internal``) — always blocked,
* the unspecified address (``0.0.0.0`` / ``::``) and multicast — always blocked.

It deliberately does **not** block ordinary private RFC1918 / CGNAT addresses: a
legitimate interface VPC endpoint for Bedrock resolves to a private ``10.x`` /
``172.16.x`` / ``192.168.x`` address and must keep working.

For a hostname it does a best-effort DNS resolution and blocks when *any* resolved
address is blocked. Resolution happens at validation time only, so a hostname could
re-resolve to a different address when the request is later made (TOCTOU). That residual
risk is accepted for this admin-gated settings path; the primary mitigation against
metadata/loopback exfiltration is this allow check (plus https-only in production).

Nothing here imports application code. [1.4.1] The webhook channel, the Teams channel and
the HTTP sensor use the same rules through `netmon.util.egress`, which enforces them at
connection time (resolve, check every address, connect to the checked address; every redirect
hop again), so those paths have no TOCTOU window.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Any

BLOCKED_MSG = "endpoint host is not allowed (loopback/link-local/metadata)"
UNRESOLVED_MSG = "could not resolve endpoint host"

# loopback host aliases: blocked unless ``allow_loopback``
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})
# cloud-metadata host aliases: always blocked (literal hostname forms)
_METADATA_HOSTNAMES = frozenset({"metadata", "metadata.google.internal", "metadata.goog"})
# metadata addresses not caught by the stdlib address flags (CGNAT range, etc.)
_BLOCKED_IPS = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / Azure IMDS (also link-local; kept explicit)
        "100.100.100.200",  # Alibaba Cloud metadata
    }
)

Resolver = Callable[[str], list[Any]]


def _addr_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_loopback: bool) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if str(ip) in _BLOCKED_IPS:
        return True
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        return True
    if ip.is_loopback:
        return not allow_loopback
    return False


def _default_resolver(host: str) -> list[Any]:
    return socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)


def blocked_endpoint_host(
    host: str | None,
    *,
    allow_loopback: bool = False,
    resolver: Resolver | None = None,
) -> str | None:
    """Return a validation message when `host` is a disallowed SSRF target, else ``None``.

    `host` is a bare hostname or IP literal (e.g. ``urlsplit(...).hostname``). IPv6
    brackets and a trailing dot are tolerated. `allow_loopback` relaxes only the loopback
    block (never metadata / link-local). A hostname that fails to resolve returns
    `UNRESOLVED_MSG` rather than raising.
    """
    if not host:
        return None
    h = host.strip().strip("[]").rstrip(".").lower()
    if not h:
        return None
    if h in _METADATA_HOSTNAMES:
        return BLOCKED_MSG
    if h in _LOOPBACK_HOSTNAMES:
        return None if allow_loopback else BLOCKED_MSG

    try:
        literal = ipaddress.ip_address(h)
    except ValueError:
        literal = None
    if literal is not None:
        return BLOCKED_MSG if _addr_blocked(literal, allow_loopback) else None

    resolve = resolver or _default_resolver
    try:
        infos = resolve(h)
    except Exception:  # noqa: BLE001 - any resolver failure/timeout -> validation error
        return UNRESOLVED_MSG
    for info in infos or ():
        try:
            addr = info[4][0]
        except (IndexError, TypeError, KeyError):
            continue
        try:
            rip = ipaddress.ip_address(str(addr).split("%", 1)[0])
        except ValueError:
            continue
        if _addr_blocked(rip, allow_loopback):
            return BLOCKED_MSG
    return None
