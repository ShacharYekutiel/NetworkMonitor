from __future__ import annotations

from netmon.poller.snmp_client import SnmpError, as_text, snmp_get
from netmon.poller.types import CheckContext, CheckResult

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"


async def run(ctx: CheckContext) -> CheckResult:
    try:
        vals = await snmp_get(ctx.host, ctx.snmp, [SYS_DESCR, SYS_UPTIME, SYS_NAME])
    except SnmpError as exc:
        return CheckResult(False, f"SNMP: {exc}")
    uptime = vals.get(SYS_UPTIME)
    if not isinstance(uptime, int):
        return CheckResult(False, "SNMP: sysUpTime not available")
    name = as_text(vals.get(SYS_NAME))
    descr = as_text(vals.get(SYS_DESCR))
    message = f"{name} — {descr}" if name else descr
    return CheckResult(True, message[:200], {"uptime_s": uptime / 100.0})
