"""[3b] Pure cost estimate (architecture § 14.6.3, data-model "[3b] Pricing vector").

Prices are entered by the admin per million tokens (we never ship prices). Cache read / write
default to 10 % / 125 % of the input price when empty. Without input and output prices there is
no estimate (None) and the cost cap is inactive."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

MILLION = Decimal(1_000_000)
Q = Decimal("0.000001")


def _d(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    return Decimal(str(v))


def effective_prices(prices: dict[str, Any] | None) -> dict[str, Decimal] | None:
    p = prices or {}
    inp, out = _d(p.get("input_per_mtok")), _d(p.get("output_per_mtok"))
    if inp is None or out is None:
        return None
    cr = _d(p.get("cache_read_per_mtok"))
    cw = _d(p.get("cache_write_per_mtok"))
    return {
        "input": inp,
        "output": out,
        "cache_read": cr if cr is not None else inp * Decimal("0.1"),
        "cache_write": cw if cw is not None else inp * Decimal("1.25"),
    }


def estimate(usage: dict[str, Any], prices: dict[str, Any] | None) -> Decimal | None:
    """`usage` keys: input_tokens, output_tokens, cache_read_tokens, cache_write_tokens."""
    ep = effective_prices(prices)
    if ep is None:
        return None
    total = (
        Decimal(int(usage.get("input_tokens") or 0)) * ep["input"]
        + Decimal(int(usage.get("output_tokens") or 0)) * ep["output"]
        + Decimal(int(usage.get("cache_read_tokens") or 0)) * ep["cache_read"]
        + Decimal(int(usage.get("cache_write_tokens") or 0)) * ep["cache_write"]
    ) / MILLION
    return total.quantize(Q, rounding=ROUND_HALF_UP)


def as_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None
