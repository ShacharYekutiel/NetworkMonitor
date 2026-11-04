"""IF-MIB interface traffic with counter delta / wrap handling."""

from __future__ import annotations

import time
from typing import Any

from netmon.poller.snmp_client import SnmpError, snmp_get
from netmon.poller.types import CheckContext, CheckResult

IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
IF_OPER = "1.3.6.1.2.1.2.2.1.8"
IF_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"
IF_IN_ERRORS = "1.3.6.1.2.1.2.2.1.14"
IF_OUT_OCTETS = "1.3.6.1.2.1.2.2.1.16"
IF_OUT_ERRORS = "1.3.6.1.2.1.2.2.1.20"
IF_HC_IN = "1.3.6.1.2.1.31.1.1.1.6"
IF_HC_OUT = "1.3.6.1.2.1.31.1.1.1.10"
IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"

WRAP32 = 2**32


def counter_delta(prev: int, cur: int, hc: bool) -> int | None:
    """Delta between two counter readings. None = counter reset (skip this rate)."""
    d = cur - prev
    if d >= 0:
        return d
    if not hc:
        return d + WRAP32
    return None


def compute_rates(
    state: dict[str, Any],
    now: float,
    counters: dict[str, int],
    hc: bool,
    speed_bps: float | None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    """counters: in_octets, out_octets, in_errors, out_errors. Returns (values, new_state)."""
    new_state = {"t": now, "hc": hc, **counters}
    values: dict[str, float | None] = {}
    if not state or "t" not in state or state.get("hc") != hc:
        return values, new_state
    dt = now - float(state["t"])
    if dt <= 0:
        return values, new_state
    for key, chan, mult in (
        ("in_octets", "in_bps", 8.0),
        ("out_octets", "out_bps", 8.0),
        ("in_errors", "in_errors_ps", 1.0),
        ("out_errors", "out_errors_ps", 1.0),
    ):
        prev, cur = state.get(key), counters.get(key)
        if prev is None or cur is None:
            continue
        # error counters are always Counter32
        d = counter_delta(int(prev), int(cur), hc if key.endswith("octets") else False)
        if d is None:
            continue
        values[chan] = d * mult / dt
    if speed_bps:
        for src, dst in (("in_bps", "in_util_pct"), ("out_bps", "out_util_pct")):
            if values.get(src) is not None:
                values[dst] = round(values[src] / speed_bps * 100.0, 3)  # type: ignore[operator]
    for k in ("in_bps", "out_bps", "in_errors_ps", "out_errors_ps"):
        if values.get(k) is not None:
            values[k] = round(values[k], 3)  # type: ignore[arg-type]
    return values, new_state


async def run(ctx: CheckContext) -> CheckResult:
    idx = int(ctx.config["if_index"])
    base = [
        f"{IF_OPER}.{idx}",
        f"{IF_IN_ERRORS}.{idx}",
        f"{IF_OUT_ERRORS}.{idx}",
        f"{IF_SPEED}.{idx}",
    ]
    hc = False
    hc_vals: dict[str, Any] = {}
    try:
        vals = await snmp_get(ctx.host, ctx.snmp, base)
        if vals.get(f"{IF_OPER}.{idx}") is None:
            return CheckResult(False, f"Interface {idx} not found")
        if ctx.snmp.get("version") != "v1":
            try:
                hc_vals = await snmp_get(
                    ctx.host,
                    ctx.snmp,
                    [f"{IF_HC_IN}.{idx}", f"{IF_HC_OUT}.{idx}", f"{IF_HIGH_SPEED}.{idx}"],
                )
            except SnmpError:
                hc_vals = {}
        hc_in, hc_out = hc_vals.get(f"{IF_HC_IN}.{idx}"), hc_vals.get(f"{IF_HC_OUT}.{idx}")
        if isinstance(hc_in, int) and isinstance(hc_out, int):
            hc = True
            in_oct, out_oct = hc_in, hc_out
        else:
            lo = await snmp_get(
                ctx.host, ctx.snmp, [f"{IF_IN_OCTETS}.{idx}", f"{IF_OUT_OCTETS}.{idx}"]
            )
            in_oct, out_oct = lo.get(f"{IF_IN_OCTETS}.{idx}"), lo.get(f"{IF_OUT_OCTETS}.{idx}")
    except SnmpError as exc:
        return CheckResult(False, f"SNMP: {exc}")
    now = ctx.now if ctx.now is not None else time.time()
    oper = vals.get(f"{IF_OPER}.{idx}")
    override = ctx.config.get("speed_bps_override")
    if override:
        speed: float | None = float(override)
    else:
        high = hc_vals.get(f"{IF_HIGH_SPEED}.{idx}")
        low = vals.get(f"{IF_SPEED}.{idx}")
        speed = (
            float(high) * 1e6
            if isinstance(high, int) and high > 0
            else (float(low) if isinstance(low, int) and low > 0 else None)
        )
    counters: dict[str, int] = {}
    for key, v in (
        ("in_octets", in_oct),
        ("out_octets", out_oct),
        ("in_errors", vals.get(f"{IF_IN_ERRORS}.{idx}")),
        ("out_errors", vals.get(f"{IF_OUT_ERRORS}.{idx}")),
    ):
        if isinstance(v, int):
            counters[key] = v
    values, new_state = compute_rates(ctx.state or {}, now, counters, hc, speed)
    if isinstance(oper, int):
        values["oper_status"] = float(oper)
    return CheckResult(True, "", values, new_state=new_state)
