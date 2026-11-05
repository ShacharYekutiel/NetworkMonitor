"""snmp_custom: one SNMP GET of a single OID as a gauge, a counter rate or a string match."""

from __future__ import annotations

import re
import time
from typing import Any

from netmon.poller.snmp_client import MISSING_TYPES, SnmpError, snmp_get_typed
from netmon.poller.types import CheckContext, CheckResult

NUMERIC_RE = re.compile(r"^[-+]?\d+(\.\d+)?")
WRAP32 = 2**32


def fmt_number(v: float) -> str:
    if float(v).is_integer() and abs(v) < 1e15:
        return str(int(v))
    return f"{v:.6g}"


def decode_text(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace").replace("\x00", "").strip()
    if v is None:
        return ""
    return str(v).strip()


def parse_numeric(v: Any) -> float | None:
    """int -> as is; text -> leading number (UCD laLoad "0.15"); else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return float(v)
    m = NUMERIC_RE.match(decode_text(v))
    if m is None:
        return None
    return float(m.group(0))


def counter_bits(type_name: str) -> int:
    if type_name == "Counter32":
        return 32
    return 64


def counter_rate(
    state: dict[str, Any], now: float, value: int, bits: int, multiplier: float
) -> tuple[float | None, dict[str, Any]]:
    """Per-second rate with 32-bit wrap / 64-bit reset handling. Returns (rate, new_state)."""
    new_state = {"t": now, "v": value, "bits": bits}
    if not state or "t" not in state or "v" not in state or state.get("bits") != bits:
        return None, new_state
    dt = now - float(state["t"])
    if dt <= 0:
        return None, new_state
    delta = value - int(state["v"])
    if delta < 0:
        if bits == 32:
            delta += WRAP32
        else:
            return None, new_state  # counter reset: new baseline
    return round(delta / dt * multiplier, 3), new_state


def _message(label: str, text: str) -> str:
    return (f"{label}: {text}" if label else text)[:200]


async def run(ctx: CheckContext) -> CheckResult:
    cfg = ctx.config
    oid = str(cfg["oid"]).lstrip(".")
    value_type = cfg.get("value_type") or "gauge"
    label = str(cfg.get("label") or "")
    unit = str(cfg.get("unit") or "")
    multiplier = float(cfg.get("multiplier", 1.0) or 1.0)
    pct_oid = cfg.get("percent_of_oid") if value_type == "gauge" else None
    pct_oid = str(pct_oid).lstrip(".") if pct_oid else None
    oids = [oid] + ([pct_oid] if pct_oid else [])
    try:
        vals = await snmp_get_typed(ctx.host, ctx.snmp, oids)
    except SnmpError as exc:
        return CheckResult(False, f"SNMP: {exc}")
    for o in oids:
        v, t = vals.get(o, (None, "NoSuchObject"))
        if v is None and (t in MISSING_TYPES or t == "Null"):
            return CheckResult(False, f"OID not found: {o}")
    raw, type_name = vals[oid]
    name = label or oid

    if value_type == "gauge":
        num = parse_numeric(raw)
        if num is None:
            return CheckResult(False, f"Value is not numeric: '{decode_text(raw)[:40]}'")
        if pct_oid:
            divisor = parse_numeric(vals[pct_oid][0])
            if divisor is None:
                return CheckResult(
                    False, f"Value is not numeric: '{decode_text(vals[pct_oid][0])[:40]}'"
                )
            if cfg.get("percent_base") == "sum":
                # [2b-2] percent_of_oid is the other part (e.g. free next to used memory)
                if num < 0 or divisor < 0 or num + divisor <= 0:
                    return CheckResult(
                        True, f"{name}: level not reported (raw={fmt_number(num)})", {"value": None}
                    )
                divisor = num + divisor
            if num < 0 or divisor <= 0:
                return CheckResult(
                    True, f"{name}: level not reported (raw={fmt_number(num)})", {"value": None}
                )
            value = round(num / divisor * 100.0, 6)
        else:
            value = round(num * multiplier, 6)
        text = fmt_number(value) + (f" {unit}" if unit else "")
        return CheckResult(True, _message(label, text) if label else "", {"value": value})

    if value_type == "counter":
        if isinstance(raw, bool) or not isinstance(raw, int):
            return CheckResult(False, "Value is not a counter")
        now = ctx.now if ctx.now is not None else time.time()
        rate, new_state = counter_rate(
            ctx.state or {}, now, int(raw), counter_bits(type_name), multiplier
        )
        msg = ""
        if label and rate is not None:
            msg = _message(label, fmt_number(rate) + (f" {unit}" if unit else ""))
        return CheckResult(True, msg, {"value": rate}, new_state=new_state)

    if value_type == "enum":
        if isinstance(raw, bool) or not isinstance(raw, int):
            return CheckResult(False, "Value is not an integer")
        mapping = cfg.get("value_map") or {}
        entry = mapping.get(str(int(raw)))
        if entry is not None:
            status, text = entry.get("status", "warning"), entry.get("text") or str(raw)
        else:
            status, text = cfg.get("value_map_default") or "warning", "unexpected value"
        return CheckResult(
            True,
            f"{name}: {text} ({int(raw)})"[:200],
            {"value": float(int(raw))},
            status_override=status,
        )

    # string
    text = decode_text(raw)
    expected = cfg.get("expected")
    if expected is not None and expected != "":
        match = cfg.get("match") or "equals"
        ok = (text == expected) if match == "equals" else (expected in text)
        if not ok:
            return CheckResult(False, f"{name}: '{text[:80]}' does not match '{expected}'")
    return CheckResult(True, _message(label, text), {"value": 1.0})
