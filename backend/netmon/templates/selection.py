"""[1.3] Template apply: which interfaces get `snmp_interface` sensors (api.md "[1.3] Template
apply: interface selection").

`interfaces` is `"important"` (default), `"none"`, `"all"` or a list of ifIndexes. Importance is
the shared definition of `netmon.ports.importance` over the stored port inventory. Without
inventory rows (after an optional bounded refresh): a switch (port inventory effective, or the
identity / detected class is `switch`) treats only LAG interfaces (ifType 161) as important; any
other device (server, router, firewall, AP...) keeps the 1.2 behaviour ("important" = the
template's filters, like "all"), because importance is a switch-port notion."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from netmon.models import Device, Sensor
from netmon.ports.importance import DeviceImportance, device_importance

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.templates")

IF_TYPE_LAG = 161  # ieee8023adLag
REFRESH_WAIT_S = 20.0


@dataclass
class Selection:
    """What the apply needs to choose interfaces."""

    mode: str | list[int]
    importance: DeviceImportance | None = None
    switch_like: bool = False
    inventory: str = "missing"  # "ok" | "missing"
    refreshed: bool = False  # a port inventory run was attempted for this selection
    selected: set[int] | None = field(default=None)  # filled by the apply (removal)

    def is_important(self, item: dict[str, Any]) -> tuple[bool, list[str]]:
        idx = int(item["if_index"])
        if self.importance is not None:
            rs = list(self.importance.reasons.get(idx, []))
            return bool(rs), rs
        from netmon.ports.collect import port_kind

        kind = item.get("kind") or port_kind(
            item.get("type"), item.get("name") or item.get("descr") or ""
        )
        if item.get("type") == IF_TYPE_LAG or kind == "lag":
            return True, ["lag"]
        return False, []

    def legacy(self) -> bool:
        """ "important" behaves like "all" (non-switch device without port inventory)."""
        return self.importance is None and not self.switch_like


def entry_passes(entry: dict[str, Any], item: dict[str, Any]) -> bool:
    """The type / name filters of an `snmp_interfaces` entry (not oper_up_only / max)."""
    if item.get("type") in set(entry.get("exclude_if_types") or []):
        return False
    name = item.get("name") or item.get("descr") or ""
    inc, exc = entry.get("include_name_regex"), entry.get("exclude_name_regex")
    if inc and not re.search(inc, name, re.IGNORECASE):
        return False
    return not (exc and re.search(exc, name, re.IGNORECASE))


async def _switch_like(rt: Runtime, device_id: int) -> tuple[bool, bool]:
    """(switch-like, port inventory effective) of the device."""
    from netmon.ports.eligibility import load_flags

    async with rt.sessionmaker() as session:
        flags = (await load_flags(session, device_id)).get(device_id)
    if flags is None:
        return False, False
    like = flags.port_inventory == "on" or (
        flags.port_inventory != "off" and flags.effective_class == "switch"
    )
    return like, flags.ports


async def refresh_inventory(rt: Runtime, device_id: int, timeout_s: float) -> bool:
    """Run (or wait for) a port inventory of the device, at most `timeout_s` seconds. Class
    detection first when the device needs it (10 s budget, inside the timeout). Returns
    whether a run was started / waited for (False: not effective for the device)."""
    from netmon.ports.eligibility import load_flags
    from netmon.ports.scheduler import RefreshRunningError

    ports = getattr(rt, "ports", None)
    if ports is None or timeout_s <= 0:
        return False
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout_s
    async with rt.sessionmaker() as session:
        flags = (await load_flags(session, device_id)).get(device_id)
    if flags is None:
        return False
    if flags.needs_detection(rt.clock.now()):
        from netmon.ports.runner import detect_class

        with contextlib.suppress(Exception):
            await asyncio.wait_for(detect_class(rt, device_id), max(0.1, end - loop.time()))
        async with rt.sessionmaker() as session:
            flags = (await load_flags(session, device_id)).get(device_id)
    if flags is None or not flags.ports:
        return False
    try:
        fut = ports.refresh(device_id)
    except RefreshRunningError:
        while ports.running(device_id) and loop.time() < end:  # noqa: ASYNC110
            await asyncio.sleep(0.1)
        return True
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(fut), max(0.1, end - loop.time()))
    return True


async def load_selection(
    rt: Runtime, device_id: int, mode: str | list[int], refresh_s: float = 0.0
) -> Selection:
    sel = Selection(mode)
    if mode == "none" or mode == "all" or isinstance(mode, list):
        need_refresh = False
    else:
        need_refresh = True
    async with rt.sessionmaker() as session:
        sel.importance = await device_importance(session, device_id)
    if sel.importance is None and need_refresh and refresh_s > 0:
        sel.refreshed = await refresh_inventory(rt, device_id, refresh_s)
        async with rt.sessionmaker() as session:
            sel.importance = await device_importance(session, device_id)
    sel.inventory = "ok" if sel.importance is not None else "missing"
    sel.switch_like, _ = await _switch_like(rt, device_id)
    return sel


async def interface_choices(
    rt: Runtime, device_id: int, template: dict[str, Any], refresh_s: float = REFRESH_WAIT_S
) -> dict[str, Any]:
    """GET /api/devices/{id}/apply-template/interfaces: the device's interfaces with their
    importance and existing sensor, filtered by the template's `snmp_interfaces` type / name
    filters (every interface when the template has no such entry)."""
    from netmon.poller.snmp_client import SnmpError
    from netmon.services.sensors import effective_credentials
    from netmon.services.snmp_enum import enumerate_interfaces

    sel = await load_selection(rt, device_id, "important", refresh_s)
    entries = [e for e in template.get("entries", []) if e["kind"] == "snmp_interfaces"]
    async with rt.sessionmaker() as session:
        device = await session.get(Device, device_id)
        assert device is not None
        existing: dict[int, int] = {}
        for s in (
            await session.execute(
                select(Sensor)
                .where(Sensor.device_id == device_id, Sensor.type == "snmp_interface")
                .order_by(Sensor.id)
            )
        ).scalars():
            try:
                existing.setdefault(int(s.config.get("if_index")), s.id)
            except (TypeError, ValueError):
                continue
        rows: list[dict[str, Any]] = []
        if sel.importance is not None:
            from sqlalchemy import text

            rows = [
                {
                    "if_index": r.if_index,
                    "name": r.name,
                    "descr": r.descr,
                    "alias": r.alias,
                    "oper_status": r.oper_status,
                    "speed_bps": r.speed_bps,
                    "kind": r.kind,
                    "type": r.if_type,
                }
                for r in await session.execute(
                    text(
                        "SELECT if_index, name, descr, alias, oper_status, speed_bps, kind, "
                        "if_type FROM switch_ports WHERE device_id = :d ORDER BY if_index"
                    ),
                    {"d": device_id},
                )
            ]
        else:
            host, creds = device.host, await effective_credentials(session, device)
    if sel.importance is None:
        from netmon.ports.collect import port_kind

        try:
            async with rt.semaphore:
                items = await enumerate_interfaces(host, creds)
        except SnmpError as exc:
            from netmon.api.errors import ApiError

            raise ApiError(502, "snmp_error", f"SNMP enumeration failed: {exc}") from exc
        rows = [
            {
                "if_index": it["if_index"],
                "name": it["name"],
                "descr": it["descr"],
                "alias": it["alias"],
                "oper_status": it["oper_status"],
                "speed_bps": it["speed_bps"],
                "kind": port_kind(it.get("type"), it.get("name") or it.get("descr") or ""),
                "type": it.get("type"),
            }
            for it in items
        ]
    out = []
    for r in rows:
        if entries and not any(entry_passes(e, r) for e in entries):
            continue
        important, reasons = sel.is_important(r)
        sid = existing.get(int(r["if_index"]))
        out.append(
            {
                "if_index": int(r["if_index"]),
                "name": r["name"] or r["descr"] or str(r["if_index"]),
                "alias": r["alias"] or "",
                "oper_status": r["oper_status"],
                "speed_bps": r["speed_bps"],
                "kind": r["kind"],
                "important": important,
                "reasons": reasons,
                "has_sensor": sid is not None,
                "sensor_id": sid,
            }
        )
    return {"items": out, "inventory": sel.inventory}
