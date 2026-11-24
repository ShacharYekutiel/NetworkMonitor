"""[template-switch] The device's sensors a template supersedes ("Sensors this template
replaces" in the apply dialog).

Applying a template only adds sensors. When it replaces another template (generic SNMP ->
vendor template, one vendor template -> another), the old template's sensors stay and may fail
forever (e.g. HOST-RESOURCES-MIB CPU / memory on an HPE Comware switch). The apply dialog lists
them here and the user ticks the ones to delete; `apply_template(remove_sensor_ids=...)` only
accepts ids this module returns for the same device and template.

A sensor is a candidate when it is not produced by the new template and either

- `replaced`: its type is in the new template's `replaces:` list (library YAML), whatever
  created it (except by hand), or
- `other_template`: it was created by a different template (`sensors.origin`
  `template:<id>`); for sensors created before migration 0016 (origin unknown) when another
  library template produces exactly this sensor (the generic CPU / memory / system / ping
  types excepted: those only through `replaces`).

Never candidates: sensors created by hand (`origin = manual`), ping / system, interface
sensors (the apply dialog's Ports section handles those), passive event sensors, and sensors
the new template produces itself (re-apply).

`failing`: down with a "not found / not supported" style message - the sensor cannot work on
this device (the dialog pre-ticks these)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from netmon.models import Sensor
from netmon.poller.sensor_types import AUTO_SENSOR_TYPES, PASSIVE_TYPES
from netmon.templates.apply import identity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

NEVER_TYPES = frozenset({"ping", "snmp_system", "snmp_interface", *PASSIVE_TYPES})
FAILING_RE = re.compile(
    r"not (found|supported|available)|no ?such|endofmibview|unknown object", re.IGNORECASE
)
TEMPLATE_PREFIX = "template:"

Produces = Callable[[str, dict[str, Any]], bool]


def _oid(v: Any) -> str:
    return str(v or "").strip().lstrip(".")


def produces(template: dict[str, Any]) -> Produces:
    """Whether a sensor (type, config) is one the template creates, decided without SNMP:
    a `sensor` entry with the same identity, or an `snmp_table` row (`snmp_custom` whose OID is
    `walk_oid.<row>` and whose `percent_of_oid` matches `percent_of_column`)."""
    static: set[tuple[Any, ...]] = set()
    tables: list[tuple[str, str | None]] = []
    for e in template.get("entries", []):
        if e["kind"] == "sensor":
            key = identity(e["type"], dict(e.get("config") or {}))
            if key is not None:
                static.add(key)
        elif e["kind"] == "snmp_table":
            pct = e.get("percent_of_column")
            tables.append((_oid(e["walk_oid"]), _oid(pct) if pct else None))

    def check(type_: str, config: dict[str, Any]) -> bool:
        if identity(type_, config) in static:
            return True
        if type_ != "snmp_custom":
            return False
        oid, pct = _oid(config.get("oid")), _oid(config.get("percent_of_oid")) or None
        for walk, pct_col in tables:
            if not oid.startswith(walk + "."):
                continue
            if pct_col is None and pct is None:
                return True
            if pct_col is not None and pct is not None and pct.startswith(pct_col + "."):
                return True
        return False

    return check


def origin_template(origin: str | None) -> str | None:
    if origin and origin.startswith(TEMPLATE_PREFIX):
        return origin[len(TEMPLATE_PREFIX) :] or None
    return None


def is_failing(status: str, message: str) -> bool:
    return status == "down" and bool(FAILING_RE.search(message or ""))


def candidates_for(
    sensors: list[Any], template: dict[str, Any], library: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Pure: the candidates among `sensors` (Sensor rows of one device) for `template`."""
    new_id = template["id"]
    produced_new = produces(template)
    repl = set(template.get("replaces") or [])
    others: dict[str, Produces] | None = None
    out: list[dict[str, Any]] = []
    for s in sorted(sensors, key=lambda x: (x.sort_order, x.id)):
        cfg = s.config or {}
        if s.type in NEVER_TYPES or s.origin == "manual" or produced_new(s.type, cfg):
            continue
        src = origin_template(s.origin)
        if src == new_id:
            continue  # created by this template (an earlier version): not "another" template
        reason: str | None = None
        if s.type in repl:
            reason = "replaced"
        elif src is not None:
            reason = "other_template"
        elif s.origin is None and s.type not in AUTO_SENSOR_TYPES:
            if others is None:
                others = {t["id"]: produces(t) for t in library if t["id"] != new_id}
            matches = sorted(tid for tid, f in others.items() if f(s.type, cfg))
            if matches:
                reason = "other_template"
                src = matches[0] if len(matches) == 1 else None
        if reason is None:
            continue
        out.append(
            {
                "sensor_id": s.id,
                "name": s.name,
                "type": s.type,
                "status": s.status,
                "status_message": s.status_message or "",
                "reason": reason,
                "from_template": src,
                "failing": is_failing(s.status, s.status_message or ""),
            }
        )
    return out


async def replace_candidates(
    session: AsyncSession, device_id: int, template: dict[str, Any]
) -> list[dict[str, Any]]:
    """GET /api/devices/{id}/apply-template/replaces (`items`), also the validation of
    `remove_sensor_ids` at apply time."""
    from netmon.templates import registry

    sensors = list(
        (await session.execute(select(Sensor).where(Sensor.device_id == device_id))).scalars().all()
    )
    return candidates_for(sensors, template, registry.current().templates)
