"""[2b-2] Pure snapshot builder: raw walks + the previous snapshot -> `switch_ports` rows (rates,
error deltas, last change, duplex, VLAN/mode, PoE) and the learned FDB entries.

docs/data-model.md "`switch_ports`" and "Port inventory: SNMP data". An optional table that
failed (`error`) keeps that part of the previous snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from netmon.ports.collect import (
    DUPLEX,
    IF_STATUS,
    POE_DETECTION,
    FdbEntry,
    PortWalks,
    bridge_map,
    lag_sources,
    mac_text,
    map_pse_ports,
    parse_fdb_dot1d,
    parse_fdb_q,
    port_kind,
    resolve_lag,
    to_int,
    to_text,
    vlan_sets,
)

WRAP32 = 2**32
PORT_SOURCES = ("if_xtable", "duplex", "bridge", "vlans", "fdb", "poe")
ALL_SOURCES = (*PORT_SOURCES, "arp", "lag")  # [2b-3] lag
POE_KEYS = ("poe_admin_enabled", "poe_detection", "poe_class", "poe_power_w")
VLAN_KEYS = ("pvid", "mode", "untagged_vlans", "tagged_vlans")


@dataclass
class Snapshot:
    rows: list[dict[str, Any]] = field(default_factory=list)
    fdb: list[FdbEntry] | None = None  # None = the FDB read failed (port_macs untouched)
    sources: dict[str, Any] = field(default_factory=dict)


def skipped_sources() -> dict[str, Any]:
    return {
        **{k: "skipped" for k in ALL_SOURCES},
        "fdb_mib": None,
        "vlan_mib": None,
        "lag_mib": None,
    }


def rate(cur: int | None, prev: int | None, bits: int | None, dt: float) -> float | None:
    """bit/s from two octet counters; 32-bit wrap corrected, a 64-bit decrease -> None."""
    if cur is None or prev is None or bits is None or dt <= 0:
        return None
    delta = cur - prev
    if delta < 0:
        if bits != 32:
            return None
        delta += WRAP32
    return round(delta * 8 / dt, 1)


def error_delta(cur: int | None, prev: int | None) -> int | None:
    if cur is None or prev is None:
        return None
    d = cur - prev
    return d if d >= 0 else None


def last_change(collected_at: datetime, uptime: int | None, changed: int | None) -> datetime | None:
    """ifLastChange (TimeTicks) -> time; 0, unknown or > sysUpTime -> None."""
    if uptime is None or changed is None or changed == 0 or changed > uptime:
        return None
    return collected_at - timedelta(seconds=(uptime - changed) / 100)


def speed(high: int | None, low: int | None) -> int | None:
    if high is not None and high > 0:
        return high * 1_000_000
    if low is not None and low > 0:
        return low
    return None


def _poe_values(raw: tuple[Any, Any, Any]) -> dict[str, Any]:
    admin, det, cls = (to_int(x) for x in raw)
    return {
        "poe_admin_enabled": True if admin == 1 else (False if admin == 2 else None),
        "poe_detection": POE_DETECTION.get(det) if det is not None else None,
        "poe_class": cls - 1 if cls is not None and 1 <= cls <= 5 else None,
        "poe_power_w": None,  # no vendor column in 2b-2 (collect.PORT_POWER_OIDS)
    }


def build_snapshot(
    prev_rows: dict[int, dict[str, Any]], walks: PortWalks, now: datetime
) -> Snapshot:
    """`prev_rows`: the device's current switch_ports rows by ifIndex. `walks.error` must be
    None (a failed ifTable read never builds a snapshot)."""
    src = dict(walks.sources)
    cols, ifx = walks.if_cols, walks.ifx_cols
    descr = cols.get("descr", {})
    if_indexes = set(descr)
    ifx_state = src.get("if_xtable", "skipped")

    # bridge port <-> ifIndex
    if src.get("bridge") == "error":
        bmap = {
            int(r["bridge_port"]): i
            for i, r in prev_rows.items()
            if r.get("bridge_port") is not None
        }
    else:
        bmap = bridge_map(walks.base_ports, if_indexes)
    bmap = {bp: i for bp, i in bmap.items() if i in if_indexes}
    port_of: dict[int, int] = {}
    for bp in sorted(bmap):
        port_of.setdefault(bmap[bp], bp)

    vlans = None
    if src.get("vlans") != "error":
        vlans = vlan_sets(walks.egress, walks.untagged, walks.pvids, set(bmap))

    rows: list[dict[str, Any]] = []
    for idx in sorted(if_indexes):
        prev = prev_rows.get(idx)
        d = to_text(descr.get(idx))[:255]
        if ifx_state == "error":
            name = prev["name"] if prev else d
            alias = prev["alias"] if prev else ""
        else:
            name = to_text(ifx.get("name", {}).get(idx))[:255] or d
            alias = to_text(ifx.get("alias", {}).get(idx))[:255]
        if_type = to_int(cols.get("type", {}).get(idx))
        hc_in, hc_out = ifx.get("hc_in", {}).get(idx), ifx.get("hc_out", {}).get(idx)
        if to_int(hc_in) is not None and to_int(hc_out) is not None:
            bits: int | None = 64
            in_o, out_o = to_int(hc_in), to_int(hc_out)
        else:
            in_o = to_int(cols.get("in_octets", {}).get(idx))
            out_o = to_int(cols.get("out_octets", {}).get(idx))
            bits = 32 if (in_o is not None or out_o is not None) else None
        in_e = to_int(cols.get("in_errors", {}).get(idx))
        out_e = to_int(cols.get("out_errors", {}).get(idx))
        row: dict[str, Any] = {
            "if_index": idx,
            "name": name,
            "descr": d,
            "alias": alias,
            "if_type": if_type,
            "kind": port_kind(if_type, name),
            "admin_status": IF_STATUS.get(to_int(cols.get("admin", {}).get(idx)) or 0, "unknown"),
            "oper_status": IF_STATUS.get(to_int(cols.get("oper", {}).get(idx)) or 0, "unknown"),
            "speed_bps": speed(
                to_int(ifx.get("high_speed", {}).get(idx)), to_int(cols.get("speed", {}).get(idx))
            ),
            "mac": mac_text(cols.get("phys", {}).get(idx)),
            "last_change_at": last_change(
                now, walks.sys_uptime, to_int(cols.get("last_change", {}).get(idx))
            ),
            "bridge_port": port_of.get(idx),
            "in_octets": in_o,
            "out_octets": out_o,
            "counter_bits": bits,
            "in_errors": in_e,
            "out_errors": out_e,
            "in_bps": None,
            "out_bps": None,
            "in_errors_delta": None,
            "out_errors_delta": None,
            "collected_at": now,
        }
        if src.get("duplex") == "error":
            row["duplex"] = prev.get("duplex") if prev else None
        else:
            code = to_int(walks.duplex.get(idx)) if idx in walks.duplex else None
            row["duplex"] = None if code is None else DUPLEX.get(code, "unknown")
        if vlans is None:
            for k in VLAN_KEYS:
                row[k] = (prev or {}).get(k, [] if k.endswith("vlans") else None)
        else:
            pv = vlans.get(port_of[idx]) if idx in port_of else None
            row["pvid"] = pv.pvid if pv else None
            row["mode"] = pv.mode if pv else None
            row["untagged_vlans"] = list(pv.untagged) if pv else []
            row["tagged_vlans"] = list(pv.tagged) if pv else []
        if prev is not None:
            dt = (now - prev["collected_at"]).total_seconds()
            if prev.get("counter_bits") == bits and bits is not None:
                row["in_bps"] = rate(in_o, _int_or_none(prev.get("in_octets")), bits, dt)
                row["out_bps"] = rate(out_o, _int_or_none(prev.get("out_octets")), bits, dt)
            row["in_errors_delta"] = error_delta(in_e, _int_or_none(prev.get("in_errors")))
            row["out_errors_delta"] = error_delta(out_e, _int_or_none(prev.get("out_errors")))
        rows.append(row)

    # PoE
    by_index = {r["if_index"]: r for r in rows}
    if src.get("poe") == "error":
        for r in rows:
            prev = prev_rows.get(r["if_index"]) or {}
            for k in POE_KEYS:
                r[k] = prev.get(k)
    else:
        for r in rows:
            for k in POE_KEYS:
                r[k] = None
        if walks.poe:
            physical = {r["if_index"]: r["name"] for r in rows if r["kind"] == "physical"}
            mapping = map_pse_ports(sorted(walks.poe), physical)
            if mapping is None:
                src["poe"] = "unmapped"
            else:
                for key, idx in mapping.items():
                    by_index[idx].update(_poe_values(walks.poe[key]))

    # FDB -> learned entries and mac_count
    fdb: list[FdbEntry] | None
    if src.get("fdb") == "error":
        fdb = None
    elif walks.fdb_q is not None:
        fdb = parse_fdb_q(walks.fdb_q[0], walks.fdb_q[1], bmap, walks.fdb_q[2])
    elif walks.fdb_d is not None:
        fdb = parse_fdb_dot1d(walks.fdb_d[0], walks.fdb_d[1], bmap)
    else:
        fdb = []
    counts: dict[int, int] = {}
    for e in fdb or []:
        counts[e.if_index] = counts.get(e.if_index, 0) + 1
    for r in rows:
        if fdb is None:
            r["mac_count"] = int((prev_rows.get(r["if_index"]) or {}).get("mac_count") or 0)
        else:
            r["mac_count"] = counts.get(r["if_index"], 0)
    # [2b-3] LAG membership: resolved while the feature is ON (walks read), else the previous
    # snapshot's values are kept (stored columns untouched)
    if walks.lacp is not None or walks.stack is not None:
        resolved = resolve_lag(rows, walks.lacp, walks.stack)
        for r in rows:
            lag = resolved.get(r["if_index"])
            r["lag_if_index"], r["lag_attached"] = lag if lag is not None else (None, None)
        src["lag"], src["lag_mib"] = lag_sources(resolved, walks.lacp, walks.stack)
    else:
        for r in rows:
            prev = prev_rows.get(r["if_index"]) or {}
            r["lag_if_index"] = prev.get("lag_if_index")
            r["lag_attached"] = prev.get("lag_attached")
        if "lag" not in src:
            src["lag"], src["lag_mib"] = "skipped", None
    for k in ALL_SOURCES:
        src.setdefault(k, "skipped")
    src.setdefault("fdb_mib", None)
    src.setdefault("vlan_mib", None)
    return Snapshot(rows, fdb, src)


def _int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
