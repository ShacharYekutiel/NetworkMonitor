"""[3b] HTTP clients for AWS calls (architecture § 14.2.2): proxy mode, NO_PROXY, trust bundle.

- `env` (default): httpx `trust_env=True` → HTTPS_PROXY / NO_PROXY of the process;
- `direct`: never a proxy (VPC endpoint over Direct Connect);
- `custom`: `proxy_url` (no credentials).
TLS always verifies with `tlsutil.httpx_verify(True)` (OS store + certifi + NETMON_EXTRA_CA_*).
The instance-metadata / container-credential client is always direct (`metadata_client`).

Tests inject an `httpx` transport through `set_transport_factory` (e.g. `httpx.ASGITransport`
of the Bedrock mock).

[4] Moved from `netmon.assistant.transport` (PR 4-0a). Every client of `make_client` is behind
the 1.4.1 connect-time egress guard (`netmon.util.egress.guard_client`): a direct connection is
pinned to a checked address, proxy mounts are checked; private RFC 1918 targets (interface VPC
endpoints) stay allowed, link-local / metadata are refused. An injected test transport gets the
guard's `NAMES` mode. `metadata_client()` stays unguarded: it reaches only the fixed IMDS / ECS
addresses (never admin input, never a proxy)."""

from __future__ import annotations

import ipaddress
import socket
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from netmon import tlsutil
from netmon.util import egress

CONNECT_TIMEOUT_S = 10.0
NONSTREAM_TIMEOUT_S = 90.0
FIRST_EVENT_TIMEOUT_S = 60.0
IDLE_TIMEOUT_S = 30.0
METADATA_TIMEOUT_S = 2.0
USER_AGENT = "NetworkMonitor-assistant"

_transport_factory: Callable[[dict[str, Any]], Any] | None = None


def set_transport_factory(factory: Callable[[dict[str, Any]], Any] | None) -> None:
    """Tests: `factory(cfg) -> httpx.AsyncBaseTransport` used instead of the network."""
    global _transport_factory
    _transport_factory = factory


def make_client(cfg: dict[str, Any]) -> httpx.AsyncClient:
    mode = cfg.get("proxy_mode") or "env"
    kwargs: dict[str, Any] = {
        "verify": tlsutil.httpx_verify(True),
        "trust_env": mode == "env",
        "timeout": httpx.Timeout(NONSTREAM_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        "headers": {"User-Agent": USER_AGENT},
        "follow_redirects": False,
    }
    if mode == "custom" and cfg.get("proxy_url"):
        kwargs["proxy"] = cfg["proxy_url"]
    if _transport_factory is not None:
        kwargs["transport"] = _transport_factory(cfg)
    return egress.guard_client(httpx.AsyncClient(**kwargs))


def metadata_client() -> httpx.AsyncClient:
    kwargs: dict[str, Any] = {
        "trust_env": False,
        "timeout": httpx.Timeout(METADATA_TIMEOUT_S),
        "headers": {"User-Agent": USER_AGENT},
    }
    if _transport_factory is not None:
        kwargs["transport"] = _transport_factory({"proxy_mode": "direct", "metadata": True})
    return httpx.AsyncClient(**kwargs)


def via(url: str, cfg: dict[str, Any]) -> str:
    """`"direct"` or `"proxy <url>"` for a request to `url` under the proxy mode."""
    mode = cfg.get("proxy_mode") or "env"
    if mode == "direct":
        return "direct"
    if mode == "custom":
        return f"proxy {cfg.get('proxy_url')}" if cfg.get("proxy_url") else "direct"
    host = urlsplit(url).hostname or ""
    scheme = urlsplit(url).scheme or "https"
    proxies = urllib.request.getproxies_environment()
    proxy = proxies.get(scheme) or proxies.get("all")
    if not proxy:
        return "direct"
    if urllib.request.proxy_bypass_environment(host, proxies):
        return "direct"
    return f"proxy {proxy}"


def is_vpce(url: str) -> bool:
    return (urlsplit(url).hostname or "").endswith(".vpce.amazonaws.com")


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


def address_class(host: str) -> str | None:
    """`private` / `public` of the first resolved address (None when it does not resolve)."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        return "private" if (ip.is_private or ip.is_loopback or ip.is_link_local) else "public"
    return None
