"""[1.3] Sensor impact helpers: the default impact of new interface sensors and the one-time
upgrade backfill (data-model "[1.3] Sensor impact")."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

log = logging.getLogger("netmon.impact")

BACKFILL_KEY = "impact_backfill_version"
BACKFILL_VERSION = "1"


def sensor_if_index(config: dict[str, Any] | None) -> int | None:
    try:
        return int((config or {}).get("if_index"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def interface_impact(session: Any, device_id: int, if_index: int | None) -> str:
    """Default impact of a new `snmp_interface` sensor of the device (no explicit impact)."""
    from netmon.ports.importance import default_interface_impact, device_importance

    imp = await device_importance(session, device_id) if if_index is not None else None
    return default_interface_impact(imp, if_index)


async def backfill(rt: Any) -> int:
    """Once (guarded by the settings key `impact_backfill_version`): existing `snmp_interface`
    sensors whose port is not important get impact `warning`; important ports keep `critical`;
    devices without port inventory rows are left as they are. Returns the sensors changed."""
    from netmon.ports.importance import device_importance
    from netmon.services import settings as settings_svc
    from netmon.services.status import load_inventory

    changed: list[int] = []
    async with rt.sessionmaker() as session:
        if await settings_svc.get(session, BACKFILL_KEY) == BACKFILL_VERSION:
            return 0
        rows = (
            await session.execute(
                text(
                    "SELECT s.id, s.device_id, s.config FROM sensors s "
                    "WHERE s.type = 'snmp_interface' AND s.impact = 'critical' AND EXISTS "
                    "(SELECT 1 FROM switch_ports p WHERE p.device_id = s.device_id) "
                    "ORDER BY s.device_id, s.id"
                )
            )
        ).all()
        by_device: dict[int, list[Any]] = {}
        for r in rows:
            by_device.setdefault(r.device_id, []).append(r)
        inv = await load_inventory(session) if by_device else None
        for device_id, sensors in by_device.items():
            imp = await device_importance(session, device_id, inv=inv)
            if imp is None:
                continue
            for r in sensors:
                idx = sensor_if_index(r.config)
                if idx is not None and not imp.important(idx):
                    changed.append(int(r.id))
        if changed:
            await session.execute(
                text("UPDATE sensors SET impact = 'warning' WHERE id = ANY(:ids)"),
                {"ids": changed},
            )
        await settings_svc.put(session, BACKFILL_KEY, BACKFILL_VERSION)
        await session.commit()
    if changed:
        log.info("Interface sensors on unimportant ports set to impact warning: %d", len(changed))
        rt.request_rollup()
        if getattr(rt, "ingest", None) is not None:
            rt.ingest.mark_sources_dirty()  # the link-down port sets use critical sensors
    return len(changed)
