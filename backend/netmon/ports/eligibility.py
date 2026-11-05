"""[2b-2] Effective class and port/ARP eligibility (docs/data-model.md "Devices: inventory
flags"). The pure rules plus one loader that reads every input in three small queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from netmon.poller.sensor_types import SENSOR_TYPES

SNMP_TYPES = tuple(t for t, d in SENSOR_TYPES.items() if d.requires_snmp)
DETECT_MAX_AGE_S = 24 * 3600


def effective_class(identity: dict[str, Any] | None, detected_class: str | None) -> str:
    if identity is not None:
        return str(identity.get("device_class") or "unknown")
    return detected_class or "unknown"


def _effective(mode: str, enabled: bool, has_snmp: bool, eff_class: str, wanted: str) -> bool:
    if mode == "off" or not enabled:
        return False
    if mode == "on":
        return True
    return has_snmp and eff_class == wanted


def ports_effective(mode: str, enabled: bool, has_snmp: bool, eff_class: str) -> bool:
    return _effective(mode, enabled, has_snmp, eff_class, "switch")


def arp_effective(mode: str, enabled: bool, has_snmp: bool, eff_class: str) -> bool:
    return _effective(mode, enabled, has_snmp, eff_class, "router")


@dataclass
class DeviceFlags:
    id: int
    name: str
    host: str
    enabled: bool
    port_inventory: str
    arp_collect: str
    identity: dict[str, Any] | None
    has_snmp: bool
    detected_class: str | None
    detected_at: datetime | None
    last_run_at: datetime | None

    @property
    def effective_class(self) -> str:
        return effective_class(self.identity, self.detected_class)

    @property
    def ports(self) -> bool:
        return ports_effective(
            self.port_inventory, self.enabled, self.has_snmp, self.effective_class
        )

    @property
    def arp(self) -> bool:
        return arp_effective(self.arp_collect, self.enabled, self.has_snmp, self.effective_class)

    def needs_detection(self, now: datetime) -> bool:
        """`auto` device with an SNMP sensor, no identity, detection missing or > 24 h old."""
        if self.identity is not None or not self.has_snmp or not self.enabled:
            return False
        if "auto" not in (self.port_inventory, self.arp_collect):
            return False
        return self.detected_at is None or (now - self.detected_at).total_seconds() > (
            DETECT_MAX_AGE_S
        )


async def load_flags(session: Any, device_id: int | None = None) -> dict[int, DeviceFlags]:
    cond = "" if device_id is None else "WHERE d.id = :id"
    rows = (
        await session.execute(
            text(
                "SELECT d.id, d.name, d.host, d.enabled, d.port_inventory, d.arp_collect, "
                "d.identity, s.detected_class, s.detected_at, s.last_run_at, "
                "EXISTS (SELECT 1 FROM sensors x WHERE x.device_id = d.id "
                "AND x.type = ANY(:t)) AS has_snmp "
                f"FROM devices d LEFT JOIN port_inventory_status s ON s.device_id = d.id {cond}"
            ),
            {"t": list(SNMP_TYPES), **({"id": device_id} if device_id is not None else {})},
        )
    ).all()
    return {
        r.id: DeviceFlags(
            r.id,
            r.name,
            r.host,
            r.enabled,
            r.port_inventory,
            r.arp_collect,
            r.identity,
            bool(r.has_snmp),
            r.detected_class,
            r.detected_at,
            r.last_run_at,
        )
        for r in rows
    }
