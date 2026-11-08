"""Check plugins: one module per sensor type exposing `async def run(ctx) -> CheckResult`."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from netmon.poller.checks import (
    dns,
    http,
    ping,
    snmp_cpu,
    snmp_custom,
    snmp_disk,
    snmp_interface,
    snmp_memory,
    snmp_system,
    snmp_walk_count,
    tcp_port,
)
from netmon.poller.types import CheckContext, CheckResult

PLUGINS: dict[str, Callable[[CheckContext], Awaitable[CheckResult]]] = {
    "ping": ping.run,
    "snmp_system": snmp_system.run,
    "snmp_cpu": snmp_cpu.run,
    "snmp_memory": snmp_memory.run,
    "snmp_interface": snmp_interface.run,
    "http": http.run,
    "tcp_port": tcp_port.run,
    "dns": dns.run,
    "snmp_custom": snmp_custom.run,
    "snmp_disk": snmp_disk.run,
    "snmp_walk_count": snmp_walk_count.run,  # [2b-2]
}
