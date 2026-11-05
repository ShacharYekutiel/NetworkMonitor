"""[2b-2] Ports read side: loads the snapshot and its context from the DB and builds the API
shapes with the pure rules of `netmon.ports.view` / `netmon.ports.health`. No SNMP."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, text

from netmon.models import Sensor
from netmon.poller.sensor_types import SENSOR_TYPES
from netmon.ports import oui, view
from netmon.ports.collect import locally_administered
from netmon.ports.eligibility import DeviceFlags, load_flags
from netmon.ports.health import health_items
from netmon.ports.scheduler import next_run_at
from netmon.ports.snapshot import ALL_SOURCES
from netmon.services import settings as settings_svc
from netmon.services.serialize import iso
from netmon.services.status import Inventory

UPLINK_MAC_CAP = 1000


@dataclass
class Context:
    """Everything the port rules need besides the port rows."""

    flags: dict[int, DeviceFlags]
    inv: Inventory
    threshold: int
    reverse_dns: bool
    links: dict[int, list[dict[str, Any]]] = field(default_factory=dict)  # by device id
    neighbors: dict[int, dict[str, Any]] = field(default_factory=dict)
    _device_info: dict[int, dict[str, Any]] | None = None

    def device_info(self) -> dict[int, dict[str, Any]]:
        """Built once per Context (not per port / search row)."""
        if self._device_info is None:
            self._device_info = {
                did: {
                    "name": f.name,
                    "host": f.host,
                    "device_class": f.effective_class,
                    "status": self.inv.device_status.get(did, "unknown"),
                }
                for did, f in self.flags.items()
            }
        return self._device_info


async def load_context(session: Any, inv: Inventory, device_ids: list[int]) -> Context:
    values = await settings_svc.get_all(session)
    ctx = Context(
        await load_flags(session),
        inv,
        int(values["uplink_mac_threshold"]),
        bool(values["reverse_dns"]),
    )
    if device_ids:
        rows = (
            await session.execute(
                text(
                    "SELECT id, source, a_device_id, a_if_index, a_port, b_device_id, "
                    "b_neighbor_id, b_if_index, b_port FROM topology_links WHERE NOT stale "
                    "AND (a_device_id = ANY(:ids) OR b_device_id = ANY(:ids)) ORDER BY id"
                ),
                {"ids": device_ids},
            )
        ).mappings()
        nb_ids: set[int] = set()
        for r in rows:
            ln = dict(r)
            for did in {ln["a_device_id"], ln["b_device_id"]} & set(device_ids):
                ctx.links.setdefault(did, []).append(ln)
            if ln["b_neighbor_id"] is not None:
                nb_ids.add(ln["b_neighbor_id"])
        if nb_ids:
            for r in (
                await session.execute(
                    text(
                        "SELECT id, chassis_id, sys_name, mgmt_address FROM topology_neighbors "
                        "WHERE id = ANY(:ids)"
                    ),
                    {"ids": sorted(nb_ids)},
                )
            ).mappings():
                ctx.neighbors[r["id"]] = dict(r)
    return ctx


def neighbors_of(ctx: Context, device_id: int, row: dict[str, Any]) -> list[dict[str, Any]]:
    return view.port_neighbors(
        device_id, row, ctx.links.get(device_id, []), ctx.device_info(), ctx.neighbors
    )


async def mac_details(
    session: Any, macs: list[str], reverse_dns: bool
) -> dict[str, dict[str, Any]]:
    """MAC -> {ip, ips, hostname, device_id, device_name} from arp_entries (all sources), the
    DNS cache and the monitored devices whose host equals the IP."""
    out: dict[str, dict[str, Any]] = {}
    if not macs:
        return out
    by_mac: dict[str, list[dict[str, Any]]] = {}
    for r in (
        await session.execute(
            text("SELECT mac, ip, present, last_seen FROM arp_entries WHERE mac = ANY(:m)"),
            {"m": sorted(set(macs))},
        )
    ).mappings():
        by_mac.setdefault(r["mac"], []).append(dict(r))
    ips = {view.mac_ips(rows)[0] for rows in by_mac.values()} - {None}
    names: dict[str, str | None] = {}
    hosts: dict[str, tuple[int, str]] = {}
    if ips:
        if reverse_dns:
            for r in await session.execute(
                text("SELECT ip, name FROM dns_cache WHERE ip = ANY(:ips)"), {"ips": sorted(ips)}
            ):
                names[r.ip] = r.name
        for r in await session.execute(
            text("SELECT id, name, host FROM devices WHERE host = ANY(:ips) ORDER BY id"),
            {"ips": sorted(ips)},
        ):
            hosts.setdefault(r.host, (r.id, r.name))
    for mac in set(macs):
        ip, ip_list = view.mac_ips(by_mac.get(mac, []))
        dev = hosts.get(ip) if ip else None
        out[mac] = {
            "ip": ip,
            "ips": ip_list,
            "hostname": names.get(ip) if ip else None,
            "device_id": dev[0] if dev else None,
            "device_name": dev[1] if dev else None,
        }
    return out


def port_mac_out(row: dict[str, Any], det: dict[str, Any]) -> dict[str, Any]:
    mac = row["mac"]
    return {
        "mac": mac,
        "vlan": row["vlan"],
        "vendor": oui.vendor(mac),
        "locally_administered": locally_administered(mac),
        "ip": det.get("ip"),
        "ips": list(det.get("ips") or []),
        "hostname": det.get("hostname"),
        "device_id": det.get("device_id"),
        "device_name": det.get("device_name"),
        "first_seen": iso(row["first_seen"]),
        "last_seen": iso(row["last_seen"]),
        "_last_seen": row["last_seen"],
    }


def _public_mac(m: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in m.items() if not k.startswith("_")}


def port_out(
    device_id: int,
    row: dict[str, Any],
    ctx: Context,
    macs: list[dict[str, Any]],
    sensor: tuple[int, str] | None,
) -> dict[str, Any]:
    """Port (api.md). `macs`: PortMac dicts (with `_last_seen`) of the present rows; `sensor`:
    the linked snmp_interface sensor (id, status)."""
    state = view.port_state(row["admin_status"], row["oper_status"])
    nbs = neighbors_of(ctx, device_id, row)
    primary = nbs[0] if nbs else None
    is_up, reason = view.uplink(nbs, row["mac_count"], ctx.threshold)
    reasons = view.alert_reasons(
        state,
        sensor[1] if sensor else None,
        row["in_errors_delta"],
        row["out_errors_delta"],
        row["poe_detection"],
    )
    macs_sorted = sorted(macs, key=view.mac_sort_key)
    first = None
    if not is_up and macs_sorted:
        picked = view.first_mac([{**m, "last_seen": m["_last_seen"], "_m": m} for m in macs_sorted])
        first = picked["_m"] if picked else None
    has_poe = any(row[k] is not None for k in ("poe_admin_enabled", "poe_detection", "poe_class"))
    return {
        "if_index": row["if_index"],
        "name": row["name"],
        "descr": row["descr"],
        "alias": row["alias"],
        "kind": row["kind"],
        "if_type": row["if_type"],
        "admin_status": row["admin_status"],
        "oper_status": row["oper_status"],
        "state": state,
        "alert": bool(reasons),
        "alert_reasons": reasons,
        "speed_bps": row["speed_bps"],
        "duplex": row["duplex"],
        "mac": row["mac"],
        "last_change_at": iso(row["last_change_at"]),
        "pvid": row["pvid"],
        "mode": row["mode"],
        "untagged_vlans": list(row["untagged_vlans"] or []),
        "tagged_vlans": list(row["tagged_vlans"] or []),
        "poe": {
            "admin_enabled": row["poe_admin_enabled"],
            "detection": row["poe_detection"],
            "power_class": row["poe_class"],
            "power_w": row["poe_power_w"],
        }
        if has_poe
        else None,
        "in_bps": row["in_bps"],
        "out_bps": row["out_bps"],
        "in_errors_delta": row["in_errors_delta"],
        "out_errors_delta": row["out_errors_delta"],
        "is_uplink": is_up,
        "uplink_reason": reason,
        "connected_kind": view.connected_kind(state, primary),
        "trunk": row["mode"] == "trunk",
        "neighbor": primary,
        "mac_count": row["mac_count"],
        "first_mac": _public_mac(first) if first else None,
        "macs": [] if is_up else [_public_mac(m) for m in macs_sorted],
        "sensor_id": sensor[0] if sensor else None,
        "sensor_status": sensor[1] if sensor else None,
        "collected_at": iso(row["collected_at"]),
        "_neighbors": nbs,
        "_macs_all": macs_sorted,
    }


async def interface_sensors(session: Any, device_id: int) -> dict[int, tuple[int, str]]:
    out: dict[int, tuple[int, str]] = {}
    for r in await session.execute(
        text(
            "SELECT id, status, config FROM sensors WHERE device_id = :d "
            "AND type = 'snmp_interface' ORDER BY id"
        ),
        {"d": device_id},
    ):
        try:
            idx = int((r.config or {}).get("if_index"))
        except (TypeError, ValueError):
            continue
        out.setdefault(idx, (r.id, r.status))
    return out


async def present_macs(
    session: Any,
    device_id: int,
    reverse_dns: bool,
    if_index: int | None = None,
    if_indexes: list[int] | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """PortMac dicts of the present MACs by ifIndex: one port (`if_index`), a set of ports
    (`if_indexes`, e.g. the non-uplinks of the list view) or the whole device."""
    params: dict[str, Any] = {"d": device_id}
    cond = ""
    if if_index is not None:
        cond, params["i"] = " AND if_index = :i", if_index
    elif if_indexes is not None:
        if not if_indexes:
            return {}
        cond, params["ifs"] = " AND if_index = ANY(:ifs)", sorted(if_indexes)
    rows = [
        dict(r)
        for r in (
            await session.execute(
                text(
                    "SELECT if_index, mac, vlan, first_seen, last_seen FROM port_macs "
                    f"WHERE device_id = :d AND present{cond}"
                ),
                params,
            )
        ).mappings()
    ]
    det = await mac_details(session, [r["mac"] for r in rows], reverse_dns)
    out: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["if_index"], []).append(port_mac_out(r, det.get(r["mac"], {})))
    return out


async def device_health(session: Any, device_id: int) -> list[dict[str, Any]]:
    sensors = (
        (await session.execute(select(Sensor).where(Sensor.device_id == device_id))).scalars().all()
    )
    items = []
    for s in sensors:
        tdef = SENSOR_TYPES.get(s.type)
        ch = next((c for c in s.channels if tdef and c.key == tdef.primary_channel), None)
        items.append(
            {
                "id": s.id,
                "type": s.type,
                "name": s.name,
                "config": s.config,
                "status": s.status,
                "value": ch.last_value if ch else None,
                "unit": ch.unit if ch else "",
            }
        )
    return health_items(items)


def status_out(
    row: Any, flags: DeviceFlags, running: bool, now: datetime, interval: int, arp_sources: int
) -> dict[str, Any]:
    sources = {k: "skipped" for k in ALL_SOURCES}
    fdb_mib = vlan_mib = None
    if row is not None and isinstance(row.sources, dict):
        sources.update({k: row.sources[k] for k in ALL_SOURCES if k in row.sources})
        fdb_mib, vlan_mib = row.sources.get("fdb_mib"), row.sources.get("vlan_mib")
    return {
        "last_run_at": iso(row.last_run_at) if row is not None else None,
        "last_ok_at": iso(row.last_ok_at) if row is not None else None,
        "duration_ms": row.duration_ms if row is not None else None,
        "error": row.error if row is not None else None,
        "running": running,
        "next_run_at": iso(next_run_at(flags, now, interval)),
        "sources": sources,
        "fdb_mib": fdb_mib,
        "vlan_mib": vlan_mib,
        "ports": row.ports if row is not None else 0,
        "macs": row.macs if row is not None else 0,
        "arp_entries": row.arp_entries if row is not None else 0,
        "arp_sources": arp_sources,
    }


def strip_private(port: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in port.items() if not k.startswith("_")}
