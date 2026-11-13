"""[1.3] "Important port": the one shared definition (data-model "[1.3] Important ports").

A port is important when it is
- an **uplink** (the 2b-2 view rule: a switch / router / AP neighbour, or more MACs than
  `uplink_mac_threshold`; a LAG uses the neighbours of itself and its members, a member
  inherits its LAG's uplink state),
- a **LAG** or a **LAG member** (stored `kind = 'lag'` / `lag_if_index`), or
- a **trunk**: port mode `trunk` with at least `trunk_min_tagged_vlans` tagged VLANs (a member
  without VLANs of its own uses its LAG's), so a hybrid access port with one tagged voice VLAN
  is not a trunk, or
- **[1.3.1] irf**: an IRF physical port of an HPE Comware IRF stack (stored `irf_port` not null,
  from HH3C-STACK-MIB hh3cStackPhyPortInfoBelongtoPort in the port inventory run): an IRF link
  that goes down hurts the whole stack.

Used by the ports API (`important`, `important_reasons`), the template apply selection, the
interface-sensor impact (backfill and new sensors) and the ingest link-down scope
(`PortInfo`). The core is pure; `device_importance()` loads the inputs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from netmon.ports import view

REASONS = ("uplink", "lag", "trunk", "irf")
TRUNK_MIN_DEFAULT = 2


def is_trunk(mode: str | None, tagged_vlans: list[int] | None, trunk_min: int) -> bool:
    return mode == "trunk" and len(list(tagged_vlans or [])) >= max(1, int(trunk_min))


def port_reasons(
    rows: list[dict[str, Any]],
    neighbors_of: Callable[[dict[str, Any]], list[dict[str, Any]]],
    threshold: int,
    trunk_min: int,
) -> dict[int, list[str]]:
    """{ifIndex: reasons} for every port row of one device (reasons in REASONS order; [] =
    not important). `neighbors_of(row)` returns the PortNeighbor list of a row."""
    by_idx = {int(r["if_index"]): r for r in rows}
    members, lag_of = view.lag_groups(rows)
    own_nbs = {idx: neighbors_of(r) for idx, r in by_idx.items()}
    uplink: dict[int, bool] = {}
    for idx, r in by_idx.items():
        nbs = own_nbs[idx]
        if idx in members:  # a LAG: its own links and those of all members
            nbs = view.merge_neighbors(nbs, *(own_nbs[m] for m in members[idx] if m in own_nbs))
        uplink[idx] = view.uplink(nbs, int(r.get("mac_count") or 0), threshold)[0]
    out: dict[int, list[str]] = {}
    for idx, r in by_idx.items():
        reasons: list[str] = []
        lag = lag_of.get(idx)
        if uplink[idx] or (lag is not None and uplink.get(lag, False)):
            reasons.append("uplink")
        if r.get("kind") == "lag" or r.get("lag_if_index") is not None:
            reasons.append("lag")
        vals: dict[str, Any] = r
        if lag is not None and lag in by_idx:
            vals, _ = view.member_vlans(r, by_idx[lag])
        if is_trunk(vals.get("mode"), vals.get("tagged_vlans"), trunk_min):
            reasons.append("trunk")
        if r.get("irf_port") is not None:  # [1.3.1]
            reasons.append("irf")
        out[idx] = reasons
    return out


@dataclass
class DeviceImportance:
    """Importance of one device's ports (None from the loader = no port inventory)."""

    reasons: dict[int, list[str]] = field(default_factory=dict)

    def important(self, if_index: int, include_trunks: bool = True) -> bool:
        rs = self.reasons.get(int(if_index), [])
        if not include_trunks:
            rs = [r for r in rs if r != "trunk"]
        return bool(rs)

    def important_set(self, include_trunks: bool = True) -> set[int]:
        return {i for i in self.reasons if self.important(i, include_trunks)}


async def device_importance(
    session: Any, device_id: int, *, inv: Any = None, ctx: Any = None
) -> DeviceImportance | None:
    """The importance of the device's stored ports; None when it has no `switch_ports` rows."""
    from netmon.ports import read
    from netmon.services.status import load_inventory

    rows = [
        dict(r)
        for r in (
            await session.execute(
                text("SELECT * FROM switch_ports WHERE device_id = :d ORDER BY if_index"),
                {"d": device_id},
            )
        ).mappings()
    ]
    if not rows:
        return None
    if ctx is None:
        if inv is None:
            inv = await load_inventory(session)
        ctx = await read.load_context(session, inv, [device_id])
    reasons = port_reasons(
        rows, lambda r: read.neighbors_of(ctx, device_id, r), ctx.threshold, ctx.trunk_min
    )
    return DeviceImportance(reasons)


def default_interface_impact(imp: DeviceImportance | None, if_index: int | None) -> str:
    """[1.3] Impact of a new snmp_interface sensor without an explicit one: important port ->
    critical, other port -> warning; no port inventory (or no ifIndex) -> critical (the
    importance is unknown, so the 1.2 behaviour is kept)."""
    if imp is None or if_index is None:
        return "critical"
    return "critical" if imp.important(if_index) else "warning"
