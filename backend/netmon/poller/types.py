"""Shared poller types and threshold evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SEVERITY = {"down": 5, "warning": 4, "unreachable": 3, "unknown": 2, "up": 1, "paused": 0}
PROBLEM_STATUSES = ("down", "warning")


def worst(statuses: list[str], default: str = "unknown") -> str:
    if not statuses:
        return default
    return max(statuses, key=lambda s: SEVERITY[s])


@dataclass
class CheckContext:
    sensor_id: int | None
    type: str
    config: dict[str, Any]
    host: str
    snmp: dict[str, Any]
    state: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 60.0
    now: float | None = None  # epoch override for tests


@dataclass
class CheckResult:
    """What a plugin returns. `values` maps channel key -> value (None/missing = no value)."""

    ok: bool
    message: str = ""
    values: dict[str, float | None] = field(default_factory=dict)
    new_state: dict[str, Any] | None = None
    config_update: dict[str, Any] | None = None
    # [2b-1] an ok result whose sensor status is decided by the plugin (snmp_custom enum)
    status_override: str | None = None


@dataclass
class ChannelEval:
    key: str
    unit: str
    value: float | None
    status: str
    thresholds: dict[str, float | None]


@dataclass
class EvaluatedResult:
    ok: bool
    status: str
    message: str
    duration_ms: float
    checked_at: datetime
    channels: list[ChannelEval]
    new_state: dict[str, Any] | None = None
    config_update: dict[str, Any] | None = None


def channel_status(value: float | None, t: dict[str, float | None]) -> str:
    if value is None:
        return "unknown"
    dmax, dmin = t.get("down_max"), t.get("down_min")
    if (dmax is not None and value > dmax) or (dmin is not None and value < dmin):
        return "down"
    wmax, wmin = t.get("warning_max"), t.get("warning_min")
    if (wmax is not None and value > wmax) or (wmin is not None and value < wmin):
        return "warning"
    return "up"


def _fmt(v: float) -> str:
    if float(v).is_integer() and abs(v) < 1e15:
        return str(int(v))
    return f"{v:.6g}"


def breach_message(key: str, value: float, t: dict[str, float | None], status: str) -> str:
    prefix = "down" if status == "down" else "warning"
    mx, mn = t.get(f"{prefix}_max"), t.get(f"{prefix}_min")
    if mx is not None and value > mx:
        return f"{key} {_fmt(value)} > {_fmt(mx)}"
    if mn is not None and value < mn:
        return f"{key} {_fmt(value)} < {_fmt(mn)}"
    return f"{key} {_fmt(value)}"


def evaluate(
    result: CheckResult,
    channel_defs: list[tuple[str, str, dict[str, float | None]]],
    duration_ms: float,
    checked_at: datetime,
) -> EvaluatedResult:
    """channel_defs: ordered list of (key, unit, thresholds)."""
    chans: list[ChannelEval] = []
    for key, unit, thresholds in channel_defs:
        # A failed check may still report some values (e.g. ssl_days_left on a TLS error).
        value = result.values.get(key)
        status = channel_status(value, thresholds)
        chans.append(ChannelEval(key, unit, value, status, thresholds))
    if not result.ok:
        return EvaluatedResult(
            False,
            "down",
            result.message or "Check failed",
            duration_ms,
            checked_at,
            chans,
            result.new_state,
            result.config_update,
        )
    statuses = [c.status for c in chans if c.status != "unknown"]
    if result.status_override in ("up", "warning", "down"):
        statuses.append(result.status_override)
    status = worst(statuses, default="up")
    message = result.message
    if status in ("down", "warning"):
        breaches = [
            breach_message(c.key, c.value, c.thresholds, c.status)
            for c in chans
            if c.status == status and c.value is not None
        ]
        if breaches:
            message = ", ".join(breaches)
    return EvaluatedResult(
        True,
        status,
        message,
        duration_ms,
        checked_at,
        chans,
        result.new_state,
        result.config_update,
    )
