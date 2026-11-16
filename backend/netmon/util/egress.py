"""[1.4.1] SSRF egress filter for server-side requests to admin-supplied URLs.

Used by the webhook channel, the Teams channel and the HTTP sensor (every place where the
server fetches a URL an administrator typed in). The policy is `netmon.util.netguard`:

* always blocked: link-local (incl. the ``169.254.169.254`` cloud-metadata address and IPv6
  ``fe80::/10``), the metadata host aliases, the unspecified address, multicast;
* blocked unless the setting ``egress_allow_loopback`` is ON: loopback (``127.0.0.0/8``,
  ``::1``, ``localhost``);
* **never** blocked: private RFC 1918 / CGNAT / ULA ranges. Monitoring internal devices and
  posting to internal webhooks is what the product is for.

The check is enforced at **connection time**, not only when the URL is saved:
`GuardedTransport` resolves the host itself, checks every resolved address and then connects
to exactly the address it checked (the URL host is replaced by the IP; the ``Host`` header and
the TLS SNI / certificate name stay the original host name). A host name that is re-pointed
after the check (DNS rebinding, TOCTOU) therefore cannot reach a blocked address. httpx sends
every redirect hop through the transport again, so each hop is checked the same way.

Requests that leave through an outbound proxy (``HTTPS_PROXY``; the Teams and webhook clients
honour the environment) cannot be pinned: the proxy resolves and connects. They are still
checked (IP literals, host aliases and, best effort, what the name resolves to here).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

from netmon.util import netguard

MAX_ADDRESSES = 4  # addresses tried per request (connect errors fall through to the next)
BLOCKED_TEXT = "Blocked target: {host} is not allowed (loopback / link-local / cloud metadata)"
LOOPBACK_HINT = (
    "; to reach a service on this server turn on "
    '"Allow monitoring and notification targets on this server (loopback)" '
    "in Settings > General > Security"
)

_state: dict[str, bool | None] = {"allow_loopback": False, "override": None}


def configure(*, allow_loopback: bool) -> None:
    """Set by the runtime at start and after PUT /api/settings (process-wide policy)."""
    _state["allow_loopback"] = bool(allow_loopback)


def override_loopback(value: bool | None) -> None:
    """Test harness only: force the loopback rule whatever the setting says (None = off)."""
    _state["override"] = value


def loopback_allowed() -> bool:
    if _state["override"] is not None:
        return bool(_state["override"])
    return bool(_state["allow_loopback"])


class EgressBlocked(httpx.TransportError):
    """The request target is a blocked address. A `httpx.HTTPError`, so the existing error
    handling of every caller turns it into a normal failure message."""


def _is_loopback(host: str, addrs: list[str]) -> bool:
    if host.lower() in ("localhost", "ip6-localhost", "ip6-loopback"):
        return True
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip.is_loopback:
            return True
    return False


def blocked_text(host: str, addrs: list[str] | None = None) -> str:
    text = BLOCKED_TEXT.format(host=host)
    if not loopback_allowed() and _is_loopback(host, addrs or [host]):
        text += LOOPBACK_HINT
    return text


def _literal(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host.strip("[]").split("%", 1)[0]))
    except ValueError:
        return None


async def _resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in out:
            out.append(addr)
    return out


def check_names(host: str) -> str:
    """The rules that need no DNS (metadata / localhost aliases, IP literals). Returns the
    normalised host; raises `EgressBlocked`."""
    h = host.strip().strip("[]").rstrip(".")
    if netguard.blocked_endpoint_host(h, allow_loopback=loopback_allowed(), resolver=lambda _h: []):
        raise EgressBlocked(blocked_text(h))
    return h


async def checked_addresses(
    host: str, port: int, *, must_resolve: bool = True, timeout: float | None = None
) -> list[str]:
    """Resolve `host` and return its addresses after checking **every** one of them.

    Raises `EgressBlocked` when the host (or any address it resolves to) is blocked, and
    `httpx.ConnectError` when it does not resolve (`must_resolve`; otherwise ``[]``);
    `httpx.ConnectTimeout` when the lookup takes longer than `timeout` seconds."""
    h = check_names(host)
    allow = loopback_allowed()
    lit = _literal(h)
    if lit is not None:
        return [lit]
    try:
        addrs = await asyncio.wait_for(_resolve(h, port), timeout)
    except TimeoutError as exc:
        if must_resolve:
            raise httpx.ConnectTimeout(f"resolving {h} timed out") from exc
        return []
    except (OSError, UnicodeError) as exc:
        if must_resolve:
            raise httpx.ConnectError(f"could not resolve {h}: {exc}") from exc
        return []
    for a in addrs:
        if netguard.blocked_endpoint_host(a.split("%", 1)[0], allow_loopback=allow):
            raise EgressBlocked(blocked_text(h, addrs))
    if not addrs and must_resolve:
        raise httpx.ConnectError(f"could not resolve {h}")
    return addrs


async def url_block_reason(url: str | None) -> str | None:
    """Save-time check of an admin-supplied URL: the message when its host is blocked *now*,
    else None. A host that does not resolve (yet) is not an error here; the connection-time
    check is the one that counts."""
    if not url or not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError:
        return None
    if not host or "{" in host or "}" in host:  # `{host}` placeholder of the HTTP sensor
        return None
    try:
        await checked_addresses(host, port, must_resolve=False, timeout=5.0)
    except EgressBlocked as exc:
        return str(exc)
    except httpx.HTTPError:
        return None
    return None


PIN = "pin"  # direct connection: resolve, check, connect to the checked address
CHECK = "check"  # outbound proxy: check (names, literals, what the name resolves to here)
NAMES = "names"  # a transport that opens no connection itself (mock / in-process app)


class GuardedTransport(httpx.AsyncBaseTransport):
    """Wraps a transport: check the target of every request (so also every redirect hop).

    `PIN` (direct connections): connect to the checked IP address, keeping the original host
    for the ``Host`` header and for TLS (SNI and certificate name check).
    `CHECK` (the inner transport is an outbound proxy, which resolves and connects): check.
    `NAMES`: only the rules that need no DNS."""

    def __init__(self, inner: httpx.AsyncBaseTransport, *, mode: str = PIN) -> None:
        self.inner = inner
        self.mode = mode

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        # the ASCII (punycode) form: what DNS and the TLS server name need
        host = url.raw_host.decode("ascii")
        port = url.port or (443 if url.scheme == "https" else 80)
        if self.mode == NAMES:
            check_names(host)
            return await self.inner.handle_async_request(request)
        # the name lookup used to happen inside the connect, under its timeout: keep that
        limits = request.extensions.get("timeout") or {}
        dns_timeout = limits.get("connect") if isinstance(limits, dict) else None
        if self.mode == CHECK:
            await checked_addresses(host, port, must_resolve=False, timeout=dns_timeout or 5.0)
            return await self.inner.handle_async_request(request)
        addrs = await checked_addresses(host, port, timeout=dns_timeout)
        if _literal(host) is not None:
            return await self.inner.handle_async_request(request)
        last: Exception | None = None
        for addr in addrs[:MAX_ADDRESSES]:
            pinned = httpx.Request(
                request.method,
                url.copy_with(host=addr.split("%", 1)[0]),
                headers=request.headers,  # Host: the original host (set when the request was built)
                stream=request.stream,
                extensions={**request.extensions, "sni_hostname": host.rstrip(".")},
            )
            try:
                return await self.inner.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last = exc
        assert last is not None
        raise last

    async def aclose(self) -> None:
        await self.inner.aclose()


def guard_client(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """Put the egress filter in front of every transport of `client` (direct: pinned; proxy
    mounts created from the environment: checked). Returns the same client."""
    # httpx has no public hook that sees the connection target; the two attributes below have
    # been stable since httpx 0.18 and tests/test_141_egress.py fails loudly if they move.
    direct = client._transport  # noqa: SLF001
    client._transport = GuardedTransport(  # noqa: SLF001
        direct, mode=PIN if isinstance(direct, httpx.AsyncHTTPTransport) else NAMES
    )
    client._mounts = {  # noqa: SLF001
        pattern: (None if transport is None else GuardedTransport(transport, mode=CHECK))
        for pattern, transport in client._mounts.items()  # noqa: SLF001
    }
    return client


def guarded_client(**kwargs: Any) -> httpx.AsyncClient:
    """`httpx.AsyncClient(**kwargs)` behind the egress filter."""
    return guard_client(httpx.AsyncClient(**kwargs))


async def open_checked_connection(
    host: str, port: int, *, ssl: Any = None, server_hostname: str | None = None
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """`asyncio.open_connection` to a checked address of `host` (raises `EgressBlocked`)."""
    addrs = await checked_addresses(host, port, timeout=10.0)
    last: OSError | None = None
    for addr in addrs[:MAX_ADDRESSES]:
        try:
            return await asyncio.open_connection(
                addr, port, ssl=ssl, server_hostname=server_hostname if ssl else None
            )
        except OSError as exc:
            last = exc
    raise last or OSError(f"could not connect to {host}")
