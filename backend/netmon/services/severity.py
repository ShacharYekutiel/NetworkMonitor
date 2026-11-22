"""[severity] Alert severity (api.md / data-model.md "[severity] Alert severity").

Five ordered levels: critical > major > minor > warning > info. Feature `alert_severity` (OFF
by default); while it is OFF nothing is resolved, stored or sent and every existing output is
unchanged.

Where a severity comes from (`resolve`), first match wins:

1. the sensor's override: `sensors.severity_down` (status down) / `severity_warning` (warning);
2. trap / syslog event sensors: the `severity` of the event rule whose condition sets the
   sensor's status (the worst condition, the same one that writes the status message);
3. the admin-editable mapping (setting `severity_mapping`): `down_<impact>` /
   `warning_<impact>` for a problem, `info` for everything else (recoveries, paused,
   unreachable, unknown).

The resolved value is stored, never recomputed for history: `events.severity` when a
status_change event is written, `sensors.severity` (the current problem's severity, NULL when
the sensor has no problem) and `alert_state.severity` (the notified problem's severity). An
active problem is re-resolved when the mapping, the sensor override or the sensor impact
changes (`reresolve`): its `sensors.severity` / `alert_state.severity` follow, one
`sensor.severity` audit row per changed sensor; event rows keep the value they were written
with."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.severity")

FEATURE = "alert_severity"
KEY = "severity_mapping"
LEVELS: tuple[str, ...] = ("critical", "major", "minor", "warning", "info")
RANK: dict[str, int] = {lvl: len(LEVELS) - i for i, lvl in enumerate(LEVELS)}  # critical=5
PROBLEM = ("down", "warning")
IMPACTS = ("critical", "warning", "none")

# Shipped defaults: every row is editable (PUT /api/settings/severity-mapping) and
# POST /api/settings/severity-mapping/reset puts these back.
DEFAULT_MAPPING: dict[str, str] = {
    "down_critical": "critical",
    "down_warning": "major",
    "down_none": "minor",
    "warning_critical": "warning",
    "warning_warning": "warning",
    "warning_none": "warning",
    "info": "info",
}
MAPPING_KEYS: tuple[str, ...] = tuple(DEFAULT_MAPPING)

SESSION_MAPPING = "netmon_severity_mapping"  # per-session cache in Session.info


def is_level(value: Any) -> bool:
    return isinstance(value, str) and value in RANK


def rank(value: str | None) -> int:
    return RANK.get(value or "", 0)


def highest(values: Iterable[str | None]) -> str | None:
    best: str | None = None
    for v in values:
        if is_level(v) and rank(v) > rank(best):
            best = v
    return best


def normalize(value: Any) -> dict[str, str]:
    """The stored mapping merged over the defaults (unknown keys / invalid levels dropped)."""
    out = dict(DEFAULT_MAPPING)
    if isinstance(value, Mapping):
        for k in MAPPING_KEYS:
            if is_level(value.get(k)):
                out[k] = str(value[k])
    return out


def mapping_key(status: str | None, impact: str | None) -> str:
    if status in PROBLEM:
        return f"{status}_{impact if impact in IMPACTS else 'critical'}"
    return "info"


def resolve(
    status: str | None,
    impact: str | None,
    mapping: Mapping[str, str],
    *,
    override_down: str | None = None,
    override_warning: str | None = None,
    rule_severity: str | None = None,
) -> str:
    """Override > event rule > mapping. Non-problem statuses always use the `info` row."""
    if status == "down" and is_level(override_down):
        return str(override_down)
    if status == "warning" and is_level(override_warning):
        return str(override_warning)
    if status in PROBLEM and is_level(rule_severity):
        return str(rule_severity)
    return normalize(mapping)[mapping_key(status, impact)]


def rank_sql(column: str) -> str:
    """SQL CASE giving the numeric rank of a severity column (NULL -> 0)."""
    whens = " ".join(f"WHEN '{lvl}' THEN {RANK[lvl]}" for lvl in LEVELS)
    return f"(CASE {column} {whens} ELSE 0 END)"


def parse_levels(values: Iterable[str] | None, loc: list[Any]) -> list[str] | None:
    """Query parameter `severity=critical,major` -> validated list (422 on an unknown level)."""
    if not values:
        return None
    from netmon.api.errors import validation_error

    out: list[str] = []
    for v in values:
        if not is_level(v):
            raise validation_error(loc, f"unknown severity {v!r} (one of {', '.join(LEVELS)})")
        if v not in out:
            out.append(v)
    return out


def empty_counts() -> dict[str, int]:
    return {**dict.fromkeys(LEVELS, 0), "total": 0}


def count_problems(sensors: Iterable[Mapping[str, Any]], *, unacknowledged: bool = False) -> dict:
    """Counts per severity of the current problems (inventory sensor dicts)."""
    out = empty_counts()
    for s in sensors:
        if s.get("status") not in PROBLEM or not is_level(s.get("severity")):
            continue
        if unacknowledged and s.get("acknowledged"):
            continue
        out[s["severity"]] += 1
        out["total"] += 1
    return out


# ---------------------------------------------------------------- runtime state
def enabled(rt: Any) -> bool:
    features = getattr(rt, "features", None) if rt is not None else None
    if features is None:
        return False
    try:
        return bool(features.enabled(FEATURE))
    except Exception:  # noqa: BLE001
        return False


def session_rt(session: AsyncSession) -> Any:
    try:
        return session.info.get("rt")
    except Exception:  # noqa: BLE001
        return None


def session_enabled(session: AsyncSession) -> bool:
    """The feature state for code that only has a session (Runtime puts itself into every
    session's `info`; a session from elsewhere counts as OFF)."""
    return enabled(session_rt(session))


async def load_mapping(session: AsyncSession) -> dict[str, str]:
    from netmon.services import settings as settings_svc

    return normalize(await settings_svc.get(session, KEY))


async def session_mapping(session: AsyncSession) -> dict[str, str]:
    cached = session.info.get(SESSION_MAPPING)
    if cached is None:
        cached = await load_mapping(session)
        session.info[SESSION_MAPPING] = cached
    return dict(cached)


async def rule_severity_for(session: AsyncSession, sensor_id: int) -> str | None:
    """Event sensors: the severity of the rule behind the worst active condition (the same
    order as `EventSensors.recompute`)."""
    row = (
        await session.execute(
            text(
                "SELECT r.severity FROM event_conditions c "
                "LEFT JOIN event_rules r ON r.id = c.rule_id WHERE c.sensor_id = :s "
                "ORDER BY (c.status = 'down') DESC, c.last_at DESC, c.id DESC LIMIT 1"
            ),
            {"s": sensor_id},
        )
    ).first()
    return row.severity if row is not None else None


async def resolve_for_sensor(
    session: AsyncSession,
    sensor: Any,
    status: str | None,
    *,
    rule_severity: str | None = None,
    mapping: Mapping[str, str] | None = None,
) -> str:
    if mapping is None:
        mapping = await session_mapping(session)
    return resolve(
        status,
        getattr(sensor, "impact", None),
        mapping,
        override_down=getattr(sensor, "severity_down", None),
        override_warning=getattr(sensor, "severity_warning", None),
        rule_severity=rule_severity,
    )


# ---------------------------------------------------------------- re-resolution
async def reresolve(
    session: AsyncSession,
    ctx: Any,
    *,
    sensor_ids: Iterable[int] | None = None,
    reason: str,
    audit_changes: bool = True,
) -> list[dict[str, Any]]:
    """Re-resolve the active problems (sensors down / warning, optionally only `sensor_ids`):
    `sensors.severity` and the problem rows of `alert_state`. Event rows are history and keep
    their value. One `sensor.severity` audit row per changed sensor (same transaction; the
    caller commits). Returns the changed sensors `[{id, device_id, name, old, new}]` for the
    `sensor.severity` WebSocket message (`broadcast` after the commit)."""
    from netmon.services import audit

    ids = None if sensor_ids is None else sorted(set(sensor_ids))
    if ids is not None and not ids:
        return []
    cond = "" if ids is None else " AND s.id = ANY(:ids)"
    rows = (
        await session.execute(
            text(
                "SELECT s.id, s.name, s.device_id, s.type, s.status, s.impact, s.severity, "
                "s.severity_down, s.severity_warning FROM sensors s "
                f"WHERE s.status IN ('down','warning'){cond} ORDER BY s.id FOR UPDATE OF s"
            ),
            {} if ids is None else {"ids": ids},
        )
    ).all()
    mapping = await load_mapping(session)
    session.info[SESSION_MAPPING] = mapping
    changed: list[dict[str, Any]] = []
    for r in rows:
        rule_sev = None
        if r.type in ("trap", "syslog"):
            rule_sev = await rule_severity_for(session, r.id)
        new = resolve(
            r.status,
            r.impact,
            mapping,
            override_down=r.severity_down,
            override_warning=r.severity_warning,
            rule_severity=rule_sev,
        )
        await session.execute(
            text(
                "UPDATE alert_state SET severity = :v, updated_at = now() WHERE sensor_id = :s "
                "AND status IN ('down','warning') AND severity IS DISTINCT FROM :v"
            ),
            {"s": r.id, "v": new},
        )
        if new == r.severity:
            continue
        await session.execute(
            text("UPDATE sensors SET severity = :v WHERE id = :s"), {"s": r.id, "v": new}
        )
        if audit_changes:
            await audit.record(
                session,
                ctx,
                "sensor.severity",
                audit.entity("sensor", r.id, r.name, device_id=r.device_id),
                {"severity": [r.severity, new]},
                reason=reason,
            )
        changed.append(
            {"id": r.id, "device_id": r.device_id, "name": r.name, "old": r.severity, "new": new}
        )
    return changed


def broadcast(rt: Any, changed: list[dict[str, Any]]) -> None:
    hub = getattr(rt, "hub", None)
    if hub is None:
        return
    for c in changed:
        hub.broadcast(
            "sensor.severity", {"id": c["id"], "device_id": c["device_id"], "severity": c["new"]}
        )
    if changed and hasattr(rt, "request_rollup"):
        rt.request_rollup()


async def reresolve_now(
    rt: Any, ctx: Any, *, sensor_ids: Iterable[int] | None = None, reason: str
) -> list[dict[str, Any]]:
    """Own transaction (after an API commit): no-op while the feature is OFF."""
    if not enabled(rt):
        return []
    async with rt.sessionmaker() as session:
        changed = await reresolve(session, ctx, sensor_ids=sensor_ids, reason=reason)
        await session.commit()
    broadcast(rt, changed)
    return changed


async def feature_changed(rt: Runtime, name: str, on: bool) -> None:
    """Feature listener. ON: the active problems are resolved once (their opening events
    stay NULL: no backfill of history); the `feature.enable` audit row covers it. OFF: the
    current-problem severities are cleared, so every API answers null again."""
    if name != FEATURE:
        return
    async with rt.sessionmaker() as session:
        if on:
            changed = await reresolve(session, None, reason="feature_enabled", audit_changes=False)
        else:
            await session.execute(
                text("UPDATE sensors SET severity = NULL WHERE severity IS NOT NULL")
            )
            changed = []
        await session.commit()
    broadcast(rt, changed)  # OFF: `features.changed` tells the clients
