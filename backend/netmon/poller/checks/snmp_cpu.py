from __future__ import annotations

from netmon.poller.snmp_client import SnmpError, snmp_walk
from netmon.poller.types import CheckContext, CheckResult

HR_PROCESSOR_LOAD = "1.3.6.1.2.1.25.3.3.1.2"


async def run(ctx: CheckContext) -> CheckResult:
    try:
        rows = await snmp_walk(ctx.host, ctx.snmp, HR_PROCESSOR_LOAD)
    except SnmpError as exc:
        return CheckResult(False, f"SNMP: {exc}")
    loads = [v for _, v in rows if isinstance(v, int)]
    if not loads:
        return CheckResult(False, "HOST-RESOURCES-MIB hrProcessorLoad not supported")
    return CheckResult(True, "", {"cpu_load_pct": round(sum(loads) / len(loads), 2)})
