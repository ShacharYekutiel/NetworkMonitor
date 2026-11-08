"""[2b-2] snmp_walk_count: walk one table column and count its rows, and the rows whose
integer value is in `count_values` (docs/data-model.md "New sensor type snmp_walk_count")."""

from __future__ import annotations

from typing import Any

from netmon.poller.snmp_client import SnmpError, snmp_walk
from netmon.poller.types import CheckContext, CheckResult


def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    return None


def count_rows(values: list[Any], count_values: list[int] | None) -> tuple[int, int, int]:
    """(rows, matched, unmatched). count_values None -> every row matches."""
    rows = len(values)
    if count_values is None:
        return rows, rows, 0
    wanted = set(count_values)
    matched = sum(1 for v in values if (i := _int(v)) is not None and i in wanted)
    return rows, matched, rows - matched


async def run(ctx: CheckContext) -> CheckResult:
    cfg = ctx.config
    oid = str(cfg["oid"]).strip().lstrip(".")
    label = str(cfg.get("label") or "")
    max_rows = int(cfg.get("max_rows") or 5000)
    try:
        rows = await snmp_walk(ctx.host, ctx.snmp, oid, max_rows=max_rows)
    except SnmpError as exc:
        return CheckResult(False, f"SNMP: {exc}")
    n, matched, unmatched = count_rows([v for _, v in rows], cfg.get("count_values"))
    return CheckResult(
        True,
        f"{label or oid}: {matched} of {n}"[:200],
        {"rows": float(n), "matched": float(matched), "unmatched": float(unmatched)},
    )
