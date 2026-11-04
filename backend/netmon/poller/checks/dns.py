from __future__ import annotations

import asyncio
import ipaddress
import socket
import time

import dns.asyncquery
import dns.exception
import dns.flags
import dns.message
import dns.rcode
import dns.rdatatype
import dns.reversename

from netmon.poller.types import CheckContext, CheckResult


async def _server_ip(server: str) -> str:
    try:
        ipaddress.ip_address(server)
        return server
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(server, None, type=socket.SOCK_DGRAM)
    return str(infos[0][4][0])


def _norm(text: str) -> str:
    return text.strip().rstrip(".").lower()


async def run(ctx: CheckContext) -> CheckResult:
    cfg = ctx.config
    rtype_name = cfg.get("record_type", "A")
    rdtype = dns.rdatatype.from_text(rtype_name)
    name = str(cfg["query_name"])
    if rtype_name == "PTR":
        try:
            name = dns.reversename.from_address(name).to_text()
        except (ValueError, dns.exception.SyntaxError):
            pass
    timeout = float(cfg.get("timeout_s", 5))
    port = int(cfg.get("port", 53))
    server = cfg.get("server") or ctx.host
    try:
        server_ip = await asyncio.wait_for(_server_ip(server), timeout)
    except (OSError, TimeoutError) as exc:
        return CheckResult(False, f"Cannot resolve DNS server {server}: {exc}")
    try:
        query = dns.message.make_query(name, rdtype)
    except dns.exception.DNSException as exc:
        return CheckResult(False, f"Invalid query: {exc}")
    start = time.perf_counter()
    try:
        resp = await dns.asyncquery.udp(query, server_ip, timeout=timeout, port=port)
        if resp.flags & dns.flags.TC:
            resp = await dns.asyncquery.tcp(query, server_ip, timeout=timeout, port=port)
    except dns.exception.Timeout:
        return CheckResult(False, f"DNS query timed out after {timeout:g} s")
    except (OSError, dns.exception.DNSException) as exc:
        return CheckResult(False, f"DNS query failed: {exc}")
    elapsed = (time.perf_counter() - start) * 1000.0
    rcode = resp.rcode()
    if rcode == dns.rcode.NXDOMAIN:
        return CheckResult(False, "NXDOMAIN")
    if rcode != dns.rcode.NOERROR:
        return CheckResult(False, f"DNS error: {dns.rcode.to_text(rcode)}")
    answers = [rr.to_text() for rrset in resp.answer if rrset.rdtype == rdtype for rr in rrset]
    if not answers:
        return CheckResult(False, "No answer")
    expected = cfg.get("expected")
    values = {"resolve_time_ms": round(elapsed, 3)}
    if expected:
        want = _norm(expected)
        if not any(_norm(a) == want or _norm(a).strip('"') == want for a in answers):
            return CheckResult(
                False, f"Expected {expected!r}, got {', '.join(answers)}"[:300], values
            )
    return CheckResult(True, ", ".join(_norm(a) for a in answers)[:200], values)
