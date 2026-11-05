"""[2b-1] Re-identify a device: discovery probe + fingerprint + classification + template
suggestion against `device.host` (POST /api/devices/{id}/identify)."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import TYPE_CHECKING, Any

from netmon.discovery.classify import classify_host, suggest_template
from netmon.discovery.fingerprint import fingerprint_host
from netmon.discovery.options import DEFAULT_TCP_PORTS
from netmon.discovery.probe import ProbeResult, probe_host, resolve_hostname
from netmon.services.serialize import iso, now_utc

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.discovery.identify")

TOTAL_BUDGET_S = 40.0
FINGERPRINT_TIMEOUT_MS = 2000


def identity_out(
    c: Any, res: ProbeResult, template: str, fp: dict[str, Any] | None, when: Any = None
) -> dict[str, Any]:
    return {
        "device_class": c.device_class,
        "vendor": c.vendor,
        "model": c.model,
        "confidence": c.confidence,
        "classified_by": c.classified_by,
        "suggested_template": template,
        "snmp_ok": bool(res.snmp_ok),
        "sys_object_id": res.sys_object_id,
        "sys_descr": res.sys_descr,
        "open_ports": sorted(res.open_ports),
        "fingerprint": fp,
        "identified_at": iso(when or now_utc()),
    }


def identity_from_result(row: Any) -> dict[str, Any]:
    """DeviceIdentity from a discovery result row (import)."""

    def g(k: str) -> Any:
        return getattr(row, k, None)

    return {
        "device_class": g("device_class") or "unknown",
        "vendor": g("vendor"),
        "model": g("model"),
        "confidence": g("confidence") or "low",
        "classified_by": g("classified_by") or "ports",
        "suggested_template": g("suggested_template") or "generic_ping",
        "snmp_ok": bool(g("snmp_ok")),
        "sys_object_id": g("sys_object_id"),
        "sys_descr": g("sys_descr"),
        "open_ports": sorted(int(p) for p in (g("open_ports") or [])),
        "fingerprint": g("fingerprint"),
        "identified_at": iso(g("created_at") or now_utc()),
    }


async def identify_device(rt: Runtime, host: str, creds: dict[str, Any]) -> dict[str, Any]:
    """SNMP identity + subtree probes (the given effective credentials, retries 0), TCP
    connects to the default discovery ports, fingerprinting, classification. No ping, no DNS.
    Total budget 40 s; partial results are kept."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TOTAL_BUDGET_S
    ip = host
    try:
        ipaddress.ip_address(host)
    except ValueError:
        ip = await resolve_hostname(host) or host
    res = ProbeResult(ip=ip)
    options = {
        "ping": False,
        "snmp": True,
        "tcp": True,
        "tcp_ports": list(DEFAULT_TCP_PORTS),
        "tcp_timeout_ms": 1000,
        "dns": False,
    }
    snmp_creds = {**creds, "retries": 0}
    try:
        await asyncio.wait_for(
            probe_host(ip, options, [snmp_creds], hostname=None, result=res),
            max(1.0, deadline - loop.time() - 3.0),
        )
    except TimeoutError:
        log.info("Identify probe of %s hit the budget; keeping partial results", host)
    res.mac = None
    fp = None
    remaining = deadline - loop.time()
    if remaining > 1.0:
        try:
            fp = await asyncio.wait_for(
                fingerprint_host(ip, host, res.open_ports, FINGERPRINT_TIMEOUT_MS), remaining
            )
        except TimeoutError:
            fp = None
    c = classify_host(
        res.sys_object_id,
        res.sys_descr,
        res.sys_name,
        res.probes,
        res.pf,
        res.open_ports,
        res.snmp_ok,
        fp,
    )
    template = suggest_template(
        {
            "device_class": c.device_class,
            "vendor": c.vendor,
            "snmp_ok": res.snmp_ok,
            "open_ports": res.open_ports,
            "sys_object_id": res.sys_object_id,
        }
    )
    return identity_out(c, res, template, fp)
