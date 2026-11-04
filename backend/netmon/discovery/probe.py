"""Per-host discovery probes: ping, TCP connect, SNMP identity, reverse DNS, best-effort MAC.

The scanner only sends ICMP echo, TCP connects (closed immediately, no payload), SNMP
GET/GETNEXT/GETBULK of the listed OIDs and PTR lookups, and only to the given host."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from netmon.discovery.classify import SUBTREE_PROBES, is_freebsd
from netmon.poller.checks.ping import ping_once
from netmon.poller.snmp_client import (
    SnmpError,
    as_text,
    in_subtree,
    snmp_get_typed,
    snmp_getnext,
    snmp_walk,
)

log = logging.getLogger("netmon.discovery.probe")

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
HOST_TIMEOUT_S = 30.0  # runner backstop per host (partial results are kept)
SNMP_DEADLINE_S = 25.0  # all credential attempts + GETNEXT + ifDescr walk
TCP_CONNECT_LIMIT = 256  # concurrent discovery TCP connects (file descriptors)
_tcp_sem: asyncio.Semaphore | None = None
_tcp_sem_loop: asyncio.AbstractEventLoop | None = None


def _tcp_semaphore() -> asyncio.Semaphore:
    global _tcp_sem, _tcp_sem_loop
    loop = asyncio.get_running_loop()
    if _tcp_sem is None or _tcp_sem_loop is not loop:
        _tcp_sem = asyncio.Semaphore(TCP_CONNECT_LIMIT)
        _tcp_sem_loop = loop
    return _tcp_sem
MAX_CREDENTIAL_ATTEMPTS = 9


@dataclass
class ProbeResult:
    ip: str
    hostname: str | None = None
    mac: str | None = None
    ping_ok: bool = False
    ping_rtt_ms: float | None = None
    snmp_ok: bool = False
    snmp_version: str | None = None
    snmp_credential_index: int | None = None
    sys_name: str | None = None
    sys_descr: str | None = None
    sys_object_id: str | None = None
    open_ports: list[int] = field(default_factory=list)
    probes: set[str] = field(default_factory=set)
    pf: bool = False

    @property
    def found(self) -> bool:
        return self.ping_ok or self.snmp_ok or bool(self.open_ports)


async def tcp_probe(ip: str, port: int, timeout_s: float) -> bool:
    """True when a TCP connect succeeds. The connection is closed immediately. A global
    semaphore bounds the concurrent connects (file descriptors)."""
    async with _tcp_semaphore():
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout_s)
        except (TimeoutError, OSError):
            return False
        except Exception:  # noqa: BLE001
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), 1.0)
        return True


async def tcp_probes(ip: str, ports: list[int], timeout_ms: int) -> list[int]:
    res = await asyncio.gather(*(tcp_probe(ip, p, timeout_ms / 1000.0) for p in ports))
    return sorted(p for p, ok in zip(ports, res, strict=True) if ok)


async def snmp_identity(ip: str, credentials: list[dict[str, Any]], result: ProbeResult) -> None:
    """Credential loop (stops at the first that answers), then the subtree GETNEXT and the
    FreeBSD ifDescr walk."""
    for i, creds in enumerate(credentials[:MAX_CREDENTIAL_ATTEMPTS]):
        try:
            vals = await snmp_get_typed(ip, creds, [SYS_DESCR, SYS_OBJECT_ID, SYS_NAME])
        except SnmpError:
            continue
        except Exception as exc:  # noqa: BLE001
            log.debug("SNMP probe of %s failed: %s", ip, exc)
            continue
        result.snmp_ok = True
        result.snmp_version = creds.get("version", "v2c")
        result.snmp_credential_index = i
        descr = as_text(vals[SYS_DESCR][0]) or None
        result.sys_descr = descr[:1000] if descr else None
        oid = vals[SYS_OBJECT_ID][0]
        result.sys_object_id = (str(oid).lstrip(".")[:255] or None) if oid is not None else None
        name = as_text(vals[SYS_NAME][0]) or None
        result.sys_name = name[:255] if name else None
        await _subtree_probes(ip, creds, result)
        if is_freebsd(result.sys_object_id, result.sys_descr):
            try:
                rows = await snmp_walk(ip, creds, IF_DESCR, max_rows=200)
                result.pf = any(as_text(v) == "pflog0" for _, v in rows)
            except SnmpError:
                pass
        return


async def _subtree_probes(ip: str, creds: dict[str, Any], result: ProbeResult) -> None:
    names = list(SUBTREE_PROBES)
    try:
        rows = await snmp_getnext(ip, creds, [SUBTREE_PROBES[n] for n in names])
    except SnmpError:
        return
    for name, row in zip(names, rows, strict=False):
        oid, value, type_name = row
        if type_name == "EndOfMibView":
            continue
        if in_subtree(oid, SUBTREE_PROBES[name]):
            result.probes.add(name)


async def reverse_dns(ip: str, lifetime_s: float = 2.0) -> str | None:
    try:
        import dns.asyncresolver
        import dns.exception
        import dns.resolver

        answer = await dns.asyncresolver.resolve_address(ip, lifetime=lifetime_s)
    except Exception:  # noqa: BLE001
        return None
    for rr in answer:
        name = str(rr.target).rstrip(".")
        if name:
            return name[:255]
    return None


async def resolve_hostname(name: str) -> str | None:
    """First A, else AAAA (system resolver, without blocking the loop)."""
    loop = asyncio.get_running_loop()
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(name, None, family=family, type=socket.SOCK_STREAM), 5.0
            )
        except (TimeoutError, OSError):
            continue
        for info in infos:
            addr = info[4][0]
            if isinstance(addr, str):
                return addr  # a scope id (%eth0) is rejected by the caller
    return None


def _read_arp(ip: str) -> str | None:
    path = Path("/proc/net/arp")
    try:
        lines = path.read_text(encoding="ascii", errors="replace").splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) >= 4 and parts[0] == ip and parts[2] == "0x2":
            mac = parts[3].lower()
            if mac != "00:00:00:00:00:00" and len(mac) == 17:
                return mac
    return None


async def arp_mac(ip: str) -> str | None:
    """Best effort: Linux ARP cache only; elsewhere None."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        if ipaddress.ip_address(ip).version != 4:
            return None
    except ValueError:
        return None
    return await asyncio.to_thread(_read_arp, ip)


async def probe_host(
    ip: str,
    options: dict[str, Any],
    credentials: list[dict[str, Any]],
    hostname: str | None = None,
    result: ProbeResult | None = None,
) -> ProbeResult:
    """Probe one address. Ping, TCP and SNMP run concurrently; reverse DNS and the MAC lookup
    only for found hosts. `result` (optional) is filled in place, so a caller that times out
    still sees what was found. SNMP has its own deadline (SNMP_DEADLINE_S); hitting it keeps
    whatever SNMP already answered."""
    if result is None:
        result = ProbeResult(ip=ip, hostname=hostname)

    async def do_snmp() -> None:
        try:
            await asyncio.wait_for(snmp_identity(ip, credentials, result), SNMP_DEADLINE_S)
        except TimeoutError:
            log.debug("SNMP probe of %s hit its %ss deadline", ip, SNMP_DEADLINE_S)

    async def do_ping() -> None:
        rtt = await ping_once(ip, int(options.get("ping_timeout_ms", 1000)))
        if rtt is not None:
            result.ping_ok = True
            result.ping_rtt_ms = rtt

    async def do_tcp() -> None:
        result.open_ports = await tcp_probes(
            ip, list(options.get("tcp_ports") or []), int(options.get("tcp_timeout_ms", 1000))
        )

    jobs = []
    if options.get("ping", True):
        jobs.append(do_ping())
    if options.get("tcp", True) and options.get("tcp_ports"):
        jobs.append(do_tcp())
    if options.get("snmp", True) and credentials:
        jobs.append(do_snmp())
    await asyncio.gather(*jobs)
    if not result.found:
        return result
    if options.get("dns", True) and hostname is None:
        result.hostname = await reverse_dns(ip)
    result.mac = await arp_mac(ip)
    return result
