from __future__ import annotations

from netmon.poller.snmp_client import SnmpError, as_text, snmp_get, snmp_walk, walk_to_index
from netmon.poller.types import CheckContext, CheckResult

HR_STORAGE_TYPE = "1.3.6.1.2.1.25.2.3.1.2"
HR_STORAGE_DESCR = "1.3.6.1.2.1.25.2.3.1.3"
HR_STORAGE_UNITS = "1.3.6.1.2.1.25.2.3.1.4"
HR_STORAGE_SIZE = "1.3.6.1.2.1.25.2.3.1.5"
HR_STORAGE_USED = "1.3.6.1.2.1.25.2.3.1.6"
HR_STORAGE_RAM = "1.3.6.1.2.1.25.2.1.2"


async def run(ctx: CheckContext) -> CheckResult:
    index = ctx.config.get("storage_index")
    try:
        if index is None:
            types = walk_to_index(
                await snmp_walk(ctx.host, ctx.snmp, HR_STORAGE_TYPE), HR_STORAGE_TYPE
            )
            ram = [int(i) for i, t in types.items() if str(t).lstrip(".") == HR_STORAGE_RAM]
            if not ram:
                return CheckResult(False, "HOST-RESOURCES-MIB hrStorageRam not found")
            index = min(ram)
        oids = [
            f"{base}.{index}"
            for base in (HR_STORAGE_DESCR, HR_STORAGE_UNITS, HR_STORAGE_SIZE, HR_STORAGE_USED)
        ]
        vals = await snmp_get(ctx.host, ctx.snmp, oids)
    except SnmpError as exc:
        return CheckResult(False, f"SNMP: {exc}")
    descr, units, size, used = (vals.get(o) for o in oids)
    if not all(isinstance(v, int) for v in (units, size, used)):
        return CheckResult(False, f"hrStorage row {index} not available")
    total_b = float(size * units)
    used_b = float(used * units)
    pct = (used_b / total_b * 100.0) if total_b > 0 else 0.0
    cfg_update = None
    descr_text = as_text(descr)[:255]
    if descr_text != ctx.config.get("storage_descr", ""):
        cfg_update = {"storage_descr": descr_text}
    return CheckResult(
        True,
        "",
        {"mem_used_pct": round(pct, 2), "mem_used_bytes": used_b, "mem_total_bytes": total_b},
        config_update=cfg_update,
    )
