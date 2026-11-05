"""snmp_disk: hrStorageTable usage of one entry, selected by description on every poll."""

from __future__ import annotations

import re
from typing import Any

from netmon.poller.snmp_client import SnmpError, as_text, snmp_get, snmp_walk, walk_to_index
from netmon.poller.types import CheckContext, CheckResult

HR_STORAGE_TYPE = "1.3.6.1.2.1.25.2.3.1.2"
HR_STORAGE_DESCR = "1.3.6.1.2.1.25.2.3.1.3"
HR_STORAGE_UNITS = "1.3.6.1.2.1.25.2.3.1.4"
HR_STORAGE_SIZE = "1.3.6.1.2.1.25.2.3.1.5"
HR_STORAGE_USED = "1.3.6.1.2.1.25.2.3.1.6"
HR_STORAGE_RAM = "1.3.6.1.2.1.25.2.1.2"
HR_STORAGE_VIRTUAL = "1.3.6.1.2.1.25.2.1.3"
EXCLUDED_TYPES = {HR_STORAGE_RAM, HR_STORAGE_VIRTUAL}
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:\\?$")
MAX_ROWS = 500


def _idx_key(idx: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in idx.split("."))
    except ValueError:
        return (1 << 30,)


def match_storage(wanted: str, descrs: dict[str, str], types: dict[str, Any]) -> str | None:
    """Index of the row whose description matches `wanted` (exact first; a Windows drive
    letter may also match by prefix). RAM / virtual memory rows never match."""
    candidates = [
        idx
        for idx in sorted(descrs, key=_idx_key)
        if str(types.get(idx) or "").lstrip(".") not in EXCLUDED_TYPES
    ]
    for idx in candidates:
        if descrs[idx] == wanted:
            return idx
    if WINDOWS_DRIVE_RE.match(wanted):
        for idx in candidates:
            if descrs[idx].startswith(wanted):
                return idx
    return None


def unsigned32(v: int) -> int:
    return v + 2**32 if v < 0 else v


async def run(ctx: CheckContext) -> CheckResult:
    wanted = str(ctx.config.get("storage_descr") or "")
    try:
        descr_rows = await snmp_walk(ctx.host, ctx.snmp, HR_STORAGE_DESCR, max_rows=MAX_ROWS)
        type_rows = await snmp_walk(ctx.host, ctx.snmp, HR_STORAGE_TYPE, max_rows=MAX_ROWS)
        descrs = {k: as_text(v) for k, v in walk_to_index(descr_rows, HR_STORAGE_DESCR).items()}
        types = walk_to_index(type_rows, HR_STORAGE_TYPE)
        idx = match_storage(wanted, descrs, types)
        if idx is None:
            return CheckResult(False, f"Storage '{wanted}' not found")
        oids = [f"{b}.{idx}" for b in (HR_STORAGE_UNITS, HR_STORAGE_SIZE, HR_STORAGE_USED)]
        vals = await snmp_get(ctx.host, ctx.snmp, oids)
    except SnmpError as exc:
        return CheckResult(False, f"SNMP: {exc}")
    units, size, used = (vals.get(o) for o in oids)
    if not all(isinstance(v, int) for v in (units, size, used)):
        return CheckResult(False, f"hrStorage row {idx} not available")
    units, size, used = unsigned32(units), unsigned32(size), unsigned32(used)  # type: ignore[arg-type]
    if size == 0:
        return CheckResult(False, f"Storage '{descrs[idx]}' reports size 0")
    total_b = float(size * units)
    used_b = float(used * units)
    free_b = float((size - used) * units)
    cfg_update = None
    try:
        index_int: int | None = int(idx)
    except ValueError:
        index_int = None
    if index_int is not None and ctx.config.get("storage_index") != index_int:
        cfg_update = {"storage_index": index_int}
    return CheckResult(
        True,
        "",
        {
            "disk_used_pct": round(used_b / total_b * 100.0, 2),
            "disk_used_bytes": used_b,
            "disk_free_bytes": free_b,
            "disk_total_bytes": total_b,
        },
        config_update=cfg_update,
    )
