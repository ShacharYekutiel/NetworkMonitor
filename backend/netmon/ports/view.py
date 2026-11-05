"""[2b-2] Ports read side (pure): neighbours of a port, uplink detection, tile state, alert
reasons, connected kind, MAC -> IP, first MAC (docs/data-model.md "Uplinks, neighbours and tile
fields")."""

from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Any

UPLINK_CLASSES = ("switch", "router", "access_point")
PROBLEM = ("down", "warning")


def port_state(admin_status: str, oper_status: str) -> str:
    if admin_status == "down":
        return "admin_down"
    if oper_status == "up":
        return "up"
    if oper_status == "notPresent":
        return "absent"
    return "down"


def _side_matches(
    dev: int | None, if_idx: int | None, port: str, device_id: int, row: dict
) -> bool:
    if dev != device_id:
        return False
    if if_idx is not None:
        return if_idx == row["if_index"]
    names = {str(row.get("name") or "").lower(), str(row.get("descr") or "").lower()}
    names.discard("")
    return (port or "").lower() in names


def port_neighbors(
    device_id: int,
    row: dict[str, Any],
    links: list[dict[str, Any]],
    devices: dict[int, dict[str, Any]],
    neighbors: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """PortNeighbor dicts of the port. `links`: non-stale topology_links rows of the device;
    `devices`: {id: {name, host, device_class (effective), status}}; `neighbors`:
    {id: {chassis_id, sys_name, mgmt_address}}."""
    out = []
    for ln in links:
        if _side_matches(ln["a_device_id"], ln["a_if_index"], ln["a_port"], device_id, row):
            r_dev, r_nb, r_port = ln["b_device_id"], ln["b_neighbor_id"], ln["b_port"]
        elif _side_matches(ln["b_device_id"], ln["b_if_index"], ln["b_port"], device_id, row):
            r_dev, r_nb, r_port = ln["a_device_id"], None, ln["a_port"]
        else:
            continue
        if r_dev is not None:
            d = devices.get(r_dev) or {}
            out.append(
                {
                    "link_id": ln["id"],
                    "source": ln["source"],
                    "kind": "device",
                    "device_id": r_dev,
                    "neighbor_id": None,
                    "name": str(d.get("name") or ""),
                    "port": r_port or "",
                    "device_class": d.get("device_class") or "unknown",
                    "status": d.get("status"),
                    "mgmt_address": d.get("host"),
                }
            )
        else:
            nb = neighbors.get(r_nb) or {}
            out.append(
                {
                    "link_id": ln["id"],
                    "source": ln["source"],
                    "kind": "neighbor",
                    "device_id": None,
                    "neighbor_id": r_nb,
                    "name": str(nb.get("sys_name") or nb.get("chassis_id") or ""),
                    "port": r_port or "",
                    "device_class": None,
                    "status": None,
                    "mgmt_address": nb.get("mgmt_address"),
                }
            )
    out.sort(key=lambda n: (0 if n["kind"] == "device" else 1, n["link_id"]))
    return out


def uplink(
    neighbors: list[dict[str, Any]], mac_count: int, threshold: int
) -> tuple[bool, str | None]:
    if any(n["kind"] == "device" and n["device_class"] in UPLINK_CLASSES for n in neighbors):
        return True, "neighbor"
    if mac_count > threshold:
        return True, "mac_count"
    return False, None


def alert_reasons(
    state: str,
    sensor_status: str | None,
    in_errors_delta: int | None,
    out_errors_delta: int | None,
    poe_detection: str | None,
) -> list[str]:
    if state == "admin_down":
        return []
    out = []
    if sensor_status in PROBLEM:
        out.append("sensor")
    if (in_errors_delta or 0) + (out_errors_delta or 0) > 0:
        out.append("errors")
    if poe_detection in ("fault", "other_fault"):
        out.append("poe_fault")
    return out


def connected_kind(state: str, primary: dict[str, Any] | None) -> str:
    if state != "up":
        return "none"
    if primary is not None and primary["kind"] == "device":
        if primary["device_class"] in ("switch", "router"):
            return "switch"
        if primary["device_class"] == "access_point":
            return "ap"
    return "client"


def ip_key(ip: str) -> tuple[int, int]:
    try:
        a = ipaddress.ip_address(ip)
        return (a.version, int(a))
    except ValueError:
        return (9, 0)


def mac_ips(rows: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
    """MAC -> (ip, ips) from its arp_entries rows (all sources): ip = the present row with the
    newest last_seen (ties: lowest IP), ips = the present IPs sorted; with none present, the
    newest non-present row is the last known ip and ips is []."""
    present = [r for r in rows if r["present"]]
    pool = present or rows
    if not pool:
        return None, []
    best = min(pool, key=lambda r: (-r["last_seen"].timestamp(), ip_key(r["ip"])))
    ips = sorted({r["ip"] for r in present}, key=ip_key)
    return best["ip"], ips


def first_mac(macs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first PortMac by (has an IP desc, last_seen desc, mac)."""
    if not macs:
        return None

    def key(m: dict[str, Any]) -> tuple[int, float, str]:
        ls = m["last_seen"]
        ts = ls.timestamp() if isinstance(ls, datetime) else 0.0
        return (0 if m.get("ip") else 1, -ts, m["mac"])

    return min(macs, key=key)


def mac_sort_key(m: dict[str, Any]) -> tuple[int, int, str]:
    """MAC list of a port: vlan (NULL first), then mac."""
    v = m.get("vlan")
    return (0 if v is None else 1, v or 0, m["mac"])
