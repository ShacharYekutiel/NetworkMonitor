"""[if-noise] Per-sensor / per-device / per-group overrides of the sensor event noise protections
(migration 0020, data-model "[if-noise] Overrides"): the pure resolution sensor -> device ->
group -> parent groups -> global settings, and its loaders. No imports of the services layer, so
`services.status` (the inventory) can use it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

MODES = ("inherit", "on", "off")
# override key -> column (on groups, devices and sensors) and the global settings key
OVERRIDE_KEYS = ("debounce", "flap", "debounce_polls", "debounce_min_s")
COLUMNS = {
    "debounce": "noise_debounce",
    "flap": "noise_flap",
    "debounce_polls": "noise_debounce_polls",
    "debounce_min_s": "noise_debounce_min_s",
}
GLOBAL_KEYS = {
    "debounce": "sensor_event_debounce_enabled",
    "flap": "sensor_event_flap_enabled",
    "debounce_polls": "sensor_event_debounce_polls",
    "debounce_min_s": "sensor_event_debounce_min_s",
}
GLOBAL_DEFAULTS = {"debounce": True, "flap": True, "debounce_polls": 1, "debounce_min_s": 0}


def glob_of(rt: Any) -> dict[str, Any]:
    """The global values the runtime uses now (`rt.event_noise`)."""
    noise = getattr(rt, "event_noise", None)
    return noise.global_values() if noise is not None else dict(GLOBAL_DEFAULTS)


# ------------------------------------------------------------------ overrides (pure)
def override_of(obj: Any) -> dict[str, Any]:
    """The `event_noise` override object of a group / device / sensor row (ORM object, row
    mapping or dict with the column names)."""

    def get(col: str) -> Any:
        if isinstance(obj, Mapping):
            return obj.get(col)
        return getattr(obj, col, None)

    return {
        "debounce": get("noise_debounce") or "inherit",
        "flap": get("noise_flap") or "inherit",
        "debounce_polls": get("noise_debounce_polls"),
        "debounce_min_s": get("noise_debounce_min_s"),
    }


def is_overridden(ov: dict[str, Any]) -> bool:
    return (
        ov.get("debounce", "inherit") != "inherit"
        or ov.get("flap", "inherit") != "inherit"
        or ov.get("debounce_polls") is not None
        or ov.get("debounce_min_s") is not None
    )


def apply(obj: Any, changes: dict[str, Any]) -> bool:
    """Write `event_noise` changes (override keys) onto a group / device / sensor row; True when
    something changed."""
    changed = False
    for key, value in changes.items():
        col = COLUMNS[key]
        if getattr(obj, col) != value:
            setattr(obj, col, value)
            changed = True
    return changed


def audit_values(obj: Any) -> dict[str, Any]:
    """The four columns for the audit diff."""
    return {col: getattr(obj, col, None) for col in COLUMNS.values()}


@dataclass(frozen=True)
class Level:
    kind: str  # sensor | device | group
    id: int
    name: str | None
    override: dict[str, Any]


@dataclass(frozen=True)
class Resolved:
    value: Any
    source: str  # sensor | device | group | global
    source_id: int | None = None
    source_name: str | None = None

    def out(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source": self.source,
            "source_id": self.source_id,
            "source_name": self.source_name,
        }


@dataclass(frozen=True)
class Effective:
    debounce: Resolved
    flap: Resolved
    debounce_polls: Resolved
    debounce_min_s: Resolved

    def out(self) -> dict[str, Any]:
        return {k: getattr(self, k).out() for k in OVERRIDE_KEYS}


def resolve(levels: list[Level], glob: dict[str, Any]) -> Effective:
    """`levels` most specific first (sensor, device, group, parent groups ...). A mode key is
    decided by the first level that says `on` / `off`, a value key by the first non-NULL value;
    else the global settings value (`glob` keyed by OVERRIDE_KEYS)."""
    got: dict[str, Resolved] = {}
    for key in OVERRIDE_KEYS:
        for lv in levels:
            v = lv.override.get(key)
            if key in ("debounce", "flap"):
                if v in ("on", "off"):
                    got[key] = Resolved(v == "on", lv.kind, lv.id, lv.name)
                    break
            elif v is not None:
                got[key] = Resolved(int(v), lv.kind, lv.id, lv.name)
                break
        else:
            got[key] = Resolved(glob[key], "global")
    return Effective(**got)


def group_levels(groups: dict[int, dict[str, Any]], group_id: int | None) -> list[Level]:
    """`groups`: id -> {"parent_id", "name", "noise": override}; the chain from `group_id` up."""
    out: list[Level] = []
    seen: set[int] = set()
    cur = group_id
    while cur is not None and cur in groups and cur not in seen:
        seen.add(cur)
        g = groups[cur]
        out.append(Level("group", cur, g.get("name"), g.get("noise") or {}))
        cur = g.get("parent_id")
    return out


def inv_levels(
    inv: Any, *, device_id: int | None = None, group_id: int | None = None
) -> list[Level]:
    """Levels of a device (device + its groups) or of a group, from a loaded `Inventory`."""
    out: list[Level] = []
    if device_id is not None:
        d = inv.devices.get(device_id)
        if d is None:
            return out
        out.append(Level("device", device_id, d.get("name"), d.get("noise") or {}))
        group_id = d.get("group_id")
    return out + group_levels(inv.groups, group_id)


async def load_groups(session: Any, group_ids: set[int]) -> dict[int, dict[str, Any]]:
    """The given groups and all their ancestors (one recursive query)."""
    if not group_ids:
        return {}
    res = await session.execute(
        text(
            """
            WITH RECURSIVE chain AS (
                SELECT id, parent_id, name, noise_debounce, noise_flap, noise_debounce_polls,
                       noise_debounce_min_s
                FROM groups WHERE id = ANY(:ids)
                UNION
                SELECT g.id, g.parent_id, g.name, g.noise_debounce, g.noise_flap,
                       g.noise_debounce_polls, g.noise_debounce_min_s
                FROM groups g JOIN chain c ON g.id = c.parent_id
            )
            SELECT * FROM chain
            """
        ),
        {"ids": sorted(group_ids)},
    )
    return {
        int(r.id): {"parent_id": r.parent_id, "name": r.name, "noise": override_of(r._mapping)}
        for r in res
    }


async def sensor_levels(session: Any, sensor_ids: list[int]) -> dict[int, list[Level]]:
    """sensor id -> [sensor, device, group, parent groups ...] (two queries for any number)."""
    if not sensor_ids:
        return {}
    rows = (
        await session.execute(
            text(
                """
                SELECT s.id AS sid, s.name AS sname, s.noise_debounce AS s_nd,
                       s.noise_flap AS s_nf, s.noise_debounce_polls AS s_np,
                       s.noise_debounce_min_s AS s_nm, d.id AS did, d.name AS dname, d.group_id,
                       d.noise_debounce AS d_nd, d.noise_flap AS d_nf,
                       d.noise_debounce_polls AS d_np, d.noise_debounce_min_s AS d_nm
                FROM sensors s JOIN devices d ON d.id = s.device_id
                WHERE s.id = ANY(:ids)
                """
            ),
            {"ids": sorted(set(sensor_ids))},
        )
    ).all()
    groups = await load_groups(session, {int(r.group_id) for r in rows})

    def ov(r: Any, p: str) -> dict[str, Any]:
        m = r._mapping
        return override_of(
            {
                "noise_debounce": m[f"{p}_nd"],
                "noise_flap": m[f"{p}_nf"],
                "noise_debounce_polls": m[f"{p}_np"],
                "noise_debounce_min_s": m[f"{p}_nm"],
            }
        )

    return {
        int(r.sid): [
            Level("sensor", int(r.sid), r.sname, ov(r, "s")),
            Level("device", int(r.did), r.dname, ov(r, "d")),
            *group_levels(groups, int(r.group_id)),
        ]
        for r in rows
    }


async def effective_for_sensors(
    session: Any, sensor_ids: list[int], glob: dict[str, Any]
) -> dict[int, Effective]:
    return {
        sid: resolve(levels, glob)
        for sid, levels in (await sensor_levels(session, sensor_ids)).items()
    }
