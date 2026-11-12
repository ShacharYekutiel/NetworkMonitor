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
    trunk_min: int = 2  # [1.3] trunk_min_tagged_vlans
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


async def port_names(
    session: Any, device_ids: list[int] | None = None
) -> dict[tuple[int, int], str]:
    """(device_id, if_index) -> the current port name: switch_ports.name wins over the
    snmp_interface sensor's if_name (lowest sensor id)."""
    cond_s, cond_p, params = "", "", {}
    if device_ids is not None:
        if not device_ids:
            return {}
        cond_s, cond_p = " AND device_id = ANY(:ids)", " WHERE device_id = ANY(:ids)"
        params = {"ids": sorted(set(device_ids))}
    out: dict[tuple[int, int], str] = {}
    for r in await session.execute(
        text(
            "SELECT device_id, config FROM sensors WHERE type = 'snmp_interface'"
            f"{cond_s} ORDER BY id DESC"
        ),
        params,
    ):
        cfg = r.config or {}
        try:
            idx = int(cfg.get("if_index"))
        except (TypeError, ValueError):
            continue
        if cfg.get("if_name"):
            out[(r.device_id, idx)] = str(cfg["if_name"])  # DESC: the lowest id wins
    for r in await session.execute(
        text(f"SELECT device_id, if_index, name FROM switch_ports{cond_p}"), params
    ):
        if r.name:
            out[(r.device_id, r.if_index)] = r.name
    return out


def resolve_link_ports(ln: dict[str, Any], names: dict[tuple[int, int], str]) -> dict[str, Any]:
    """A copy of a link row with display port texts for both ends (view.display_port)."""
    out = dict(ln)
    for side in ("a", "b"):
        dev, idx = ln.get(f"{side}_device_id"), ln.get(f"{side}_if_index")
        name = names.get((dev, idx)) if dev is not None and idx is not None else None
        out[f"{side}_port"] = view.display_port(ln.get(f"{side}_port"), idx, name)
    return out


async def load_context(session: Any, inv: Inventory, device_ids: list[int]) -> Context:
    values = await settings_svc.get_all(session)
    ctx = Context(
        await load_flags(session),
        inv,
        int(values["uplink_mac_threshold"]),
        bool(values["reverse_dns"]),
        int(values.get("trunk_min_tagged_vlans") or 2),
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
        link_rows = [dict(r) for r in rows]
        ends = {ln[f"{x}_device_id"] for ln in link_rows for x in "ab"} - {None}
        names = await port_names(session, sorted(ends))
        for raw in link_rows:
            ln = resolve_link_ports(raw, names)
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
        "via_lag": False,  # [2b-3]
        "_last_seen": row["last_seen"],
    }


def _public_mac(m: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in m.items() if not k.startswith("_")}


@dataclass
class LagCtx:
    """[2b-3] The device's LAG structure (only while the `lag` feature is ON)."""

    rows: dict[int, dict[str, Any]]
    members: dict[int, list[int]]
    lag_of: dict[int, int]
    macs: dict[int, list[dict[str, Any]]] = field(default_factory=dict)  # PortMacs by ifIndex


def lag_ctx(rows: list[dict[str, Any]]) -> LagCtx:
    members, lag_of = view.lag_groups(rows)
    return LagCtx({int(r["if_index"]): r for r in rows}, members, lag_of)


def lag_neighbors(ctx: Context, device_id: int, lag: LagCtx, lag_if: int) -> list[dict[str, Any]]:
    """neighbours(L) = own links of L and of all its members."""
    lists = [neighbors_of(ctx, device_id, lag.rows[lag_if])]
    lists += [neighbors_of(ctx, device_id, lag.rows[m]) for m in lag.members.get(lag_if, [])]
    return view.merge_neighbors(*lists)


def port_out(
    device_id: int,
    row: dict[str, Any],
    ctx: Context,
    macs: list[dict[str, Any]],
    sensor: tuple[int, str] | None,
    lag: LagCtx | None = None,
    important_reasons: list[str] | None = None,
) -> dict[str, Any]:
    """Port (api.md). `macs`: PortMac dicts (with `_last_seen`) of the present rows; `sensor`:
    the linked snmp_interface sensor (id, status); `lag`: the LAG structure while the `lag`
    feature is ON (members inherit VLANs, MACs and uplink state from their LAG)."""
    state = view.port_state(row["admin_status"], row["oper_status"])
    nbs = neighbors_of(ctx, device_id, row)
    primary = nbs[0] if nbs else None
    is_up, reason = view.uplink(nbs, row["mac_count"], ctx.threshold)
    idx = int(row["if_index"])
    lag_ref: dict[str, Any] | None = None
    lag_members: list[int] = []
    vlans_from_lag = False
    mac_count = int(row["mac_count"])
    vlan_vals = {k: row[k] for k in ("pvid", "mode", "untagged_vlans", "tagged_vlans")}
    if lag is not None and idx in lag.members:  # a LAG row
        lag_members = list(lag.members[idx])
        nbs = lag_neighbors(ctx, device_id, lag, idx)
        primary = nbs[0] if nbs else None
        is_up, reason = view.uplink(nbs, mac_count, ctx.threshold)
    elif lag is not None and idx in lag.lag_of:  # a member of L
        lag_if = lag.lag_of[idx]
        lrow = lag.rows[lag_if]
        lag_ref = {"if_index": lag_if, "name": lrow["name"], "attached": row.get("lag_attached")}
        l_nbs = lag_neighbors(ctx, device_id, lag, lag_if)
        l_up, l_reason = view.uplink(l_nbs, int(lrow["mac_count"]), ctx.threshold)
        if not is_up and l_up:
            is_up, reason = True, l_reason
        if primary is None and l_nbs:
            primary = l_nbs[0]
        vlan_vals, vlans_from_lag = view.member_vlans(row, lrow)
        mac_count += int(lrow["mac_count"])
        macs = list(macs) + [{**m, "via_lag": True} for m in lag.macs.get(lag_if, [])]
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
    reasons_imp = list(important_reasons or [])
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
        "pvid": vlan_vals["pvid"],
        "mode": vlan_vals["mode"],
        "untagged_vlans": list(vlan_vals["untagged_vlans"] or []),
        "tagged_vlans": list(vlan_vals["tagged_vlans"] or []),
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
        "trunk": vlan_vals["mode"] == "trunk",
        "neighbor": primary,
        "mac_count": mac_count,
        "first_mac": _public_mac(first) if first else None,
        "macs": [] if is_up else [_public_mac(m) for m in macs_sorted],
        "sensor_id": sensor[0] if sensor else None,
        "sensor_status": sensor[1] if sensor else None,
        "collected_at": iso(row["collected_at"]),
        "lag": lag_ref,  # [2b-3]
        "lag_members": lag_members,
        "vlans_from_lag": vlans_from_lag,
        # [1.3] importance, connected hosts, LLDP neighbour
        "important": bool(reasons_imp),
        "important_reasons": reasons_imp,
        "connected": connected_out(is_up, mac_count, macs_sorted, first),
        "neighbor_name": primary["name"] if primary else None,
        "neighbor_mgmt_address": primary["mgmt_address"] if primary else None,
        "_neighbors": nbs,
        "_macs_all": macs_sorted,
    }


def connected_out(
    is_uplink: bool, mac_count: int, macs: list[dict[str, Any]], first: dict[str, Any] | None
) -> dict[str, Any]:
    """[1.3] Port.connected: the number of MACs on the port (an uplink: its MAC count, the
    MACs themselves are not loaded) and the first one (the port modal's choice; null for
    uplinks)."""
    keys = ("mac", "ip", "hostname", "vendor", "device_id", "device_name")
    return {
        "count": int(mac_count) if is_uplink else len(macs),
        "first": {k: first.get(k) for k in keys} if first is not None else None,
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
    row: Any,
    flags: DeviceFlags,
    running: bool,
    now: datetime,
    interval: int,
    arp_sources: int,
    lag_on: bool = False,
) -> dict[str, Any]:
    sources = {k: "skipped" for k in ALL_SOURCES}
    fdb_mib = vlan_mib = lag_mib = None
    if row is not None and isinstance(row.sources, dict):
        sources.update({k: row.sources[k] for k in ALL_SOURCES if k in row.sources})
        fdb_mib, vlan_mib = row.sources.get("fdb_mib"), row.sources.get("vlan_mib")
        lag_mib = row.sources.get("lag_mib")
    if not lag_on:  # [2b-3] stored LAG data is ignored while the feature is OFF
        sources["lag"], lag_mib = "skipped", None
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
        "lag_mib": lag_mib,
        "ports": row.ports if row is not None else 0,
        "macs": row.macs if row is not None else 0,
        "arp_entries": row.arp_entries if row is not None else 0,
        "arp_sources": arp_sources,
    }


def strip_private(port: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in port.items() if not k.startswith("_")}
