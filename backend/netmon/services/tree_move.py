"""[tree-move] Moving devices and groups between groups (`POST /api/tree/move`).

`plan_move()` is pure: it checks a request against the inventory and returns what moves, or
raises `ApiError` before anything is changed (all-or-nothing). The endpoint then applies the plan
the same way the single edits do (`PATCH /api/devices/{id}` with `group_id`, `PATCH
/api/groups/{id}` with `parent_id`): nothing is copied or rewritten on the moved items. What a
device inherits from its groups (SNMP / SSH credentials, dependency `inherit`, group maintenance
windows, home-map scope) is always resolved from the current ancestors, so after a move it
follows the new location; settings set on the item itself stay as they are.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from netmon.api.errors import ApiError
from netmon.services.status import Inventory


@dataclass
class MovePlan:
    target_group_id: int | None
    # (id, old group / parent) of what actually changes place
    devices: list[tuple[int, int]] = field(default_factory=list)
    groups: list[tuple[int, int | None]] = field(default_factory=list)
    # already in the target, or carried along by a moved ancestor group
    skipped_devices: list[int] = field(default_factory=list)
    skipped_groups: list[int] = field(default_factory=list)


def _unknown(loc: str, ids: list[tuple[int, int]], what: str) -> list[dict[str, Any]]:
    return [{"loc": ["body", loc, i], "msg": f"{what} {x} does not exist"} for i, x in ids]


def plan_move(
    inv: Inventory, device_ids: list[int], group_ids: list[int], target_group_id: int | None
) -> MovePlan:
    """Validate a move; raises ApiError (422 / 400 group_cycle / 409 conflict) on any problem."""
    dids = list(dict.fromkeys(device_ids))
    gids = list(dict.fromkeys(group_ids))
    if not dids and not gids:
        raise ApiError(
            422,
            "validation_error",
            "nothing to move",
            [{"loc": ["body"], "msg": "give at least one device_id or group_id"}],
        )
    details: list[dict[str, Any]] = []
    if target_group_id is not None and target_group_id not in inv.groups:
        details.append({"loc": ["body", "target_group_id"], "msg": "target group does not exist"})
    details += _unknown(
        "device_ids", [(i, x) for i, x in enumerate(device_ids) if x not in inv.devices], "device"
    )
    details += _unknown(
        "group_ids", [(i, x) for i, x in enumerate(group_ids) if x not in inv.groups], "group"
    )
    if details:
        raise ApiError(422, "validation_error", details[0]["msg"], details)
    if target_group_id is None and dids:
        raise ApiError(
            422,
            "validation_error",
            "a device must belong to a group; only groups can move to the top level",
            [{"loc": ["body", "target_group_id"], "msg": "devices need a target group"}],
        )

    # a group cannot go into itself or one of its own descendants
    chain = set(inv.ancestors(target_group_id)) if target_group_id is not None else set()
    cyclic = [g for g in gids if g in chain]
    if cyclic:
        raise ApiError(
            400,
            "group_cycle",
            "A group cannot be moved into itself or one of its own subgroups",
            [{"group_id": g} for g in cyclic],
        )

    plan = MovePlan(target_group_id)
    moved = set(gids)

    def carried(group_id: int | None, *, strict: bool) -> bool:
        """Inside a group that is moved too (it moves along with that group)."""
        if group_id is None:
            return False
        chain = inv.ancestors(group_id)
        return any(a in moved for a in (chain[1:] if strict else chain))

    for g in gids:
        if carried(g, strict=True):
            plan.skipped_groups.append(g)
        elif inv.groups[g]["parent_id"] == target_group_id:
            plan.skipped_groups.append(g)
        else:
            plan.groups.append((g, inv.groups[g]["parent_id"]))
    for d in dids:
        old = int(inv.devices[d]["group_id"])
        if carried(old, strict=False) or old == target_group_id:
            plan.skipped_devices.append(d)
        else:
            plan.devices.append((d, old))

    # group names are unique among siblings (case-insensitive), as for create / rename
    staying = {
        str(inv.groups[c]["name"]).lower(): c
        for c in inv.children.get(target_group_id, [])
        if c not in {g for g, _ in plan.groups}
    }
    clashes: list[dict[str, Any]] = []
    for g, _ in plan.groups:
        key = str(inv.groups[g]["name"]).lower()
        if key in staying:
            clashes.append({"field": "name", "group_id": g})
        staying[key] = g
    if clashes:
        raise ApiError(
            409,
            "conflict",
            "The target group already has a subgroup with this name",
            clashes,
        )
    return plan


# ---------------------------------------------------------------------------------------------
# Preview: what the move changes for the devices it touches (POST /api/tree/move/preview)


def _moved_view(inv: Inventory, plan: MovePlan) -> Inventory:
    """A copy of the inventory with the plan applied (groups / devices dicts only)."""
    groups = dict(inv.groups)
    devices = dict(inv.devices)
    for gid, _old in plan.groups:
        groups[gid] = {**groups[gid], "parent_id": plan.target_group_id}
    for did, _old in plan.devices:
        devices[did] = {**devices[did], "group_id": plan.target_group_id}
    return replace(inv, groups=groups, devices=devices)


def affected_devices(inv: Inventory, plan: MovePlan) -> list[int]:
    """The moved devices and every device inside a moved group (their group chain changes)."""
    out = [d for d, _ in plan.devices]
    for gid, _old in plan.groups:
        for g in inv.descendants(gid):
            out.extend(sorted(inv.group_devices.get(g, [])))
    return list(dict.fromkeys(out))


def _cred_side(inv: Inventory, kind: str, did: int) -> dict[str, Any]:
    """[profiles] also the profile that decides (null for manual / default / none)."""
    res = inv.resolution(kind, device_id=did)
    gid = res.group_id
    prof = res.mode == "profile"
    return {
        "source": res.source,
        "group_id": gid,
        "group_name": str(inv.groups[gid]["name"]) if gid is not None else None,
        "profile_id": res.profile_id if prof else None,
        "profile_name": (res.profile.name if res.profile is not None else None) if prof else None,
    }


def _dep_side(inv: Inventory, did: int) -> dict[str, Any]:
    from netmon.services.dependencies import effective_parent

    eff = effective_parent(inv, did)
    if eff is None:
        none = dict.fromkeys(("group_id", "group_name", "device_id", "device_name"))
        return {"source": "none", **none}
    return {
        "source": eff.source,
        "group_id": eff.group_id,
        "group_name": str(inv.groups[eff.group_id]["name"]) if eff.group_id is not None else None,
        "device_id": eff.device_id,
        "device_name": str(inv.devices[eff.device_id]["name"]),
    }


def _snmp_sensor_count(inv: Inventory, did: int) -> int:
    from netmon.poller.sensor_types import SENSOR_TYPES

    n = 0
    for sid in inv.device_sensors.get(did, []):
        tdef = SENSOR_TYPES.get(str(inv.sensors[sid].get("type")))
        if tdef is not None and tdef.requires_snmp:
            n += 1
    return n


def preview_effects(inv: Inventory, plan: MovePlan) -> dict[str, Any]:
    """For every device whose group chain changes, the effective inherited settings before and
    after: SNMP credentials source (device / group / default), SSH credentials source (device /
    group / none) and the effective dependency. Only real changes are listed, grouped by
    (setting, before, after); a device with its own setting never changes for that setting."""
    after_inv = _moved_view(inv, plan)
    devices = affected_devices(inv, plan)
    effects: dict[tuple[Any, ...], dict[str, Any]] = {}

    def add(setting: str, before: dict[str, Any], after: dict[str, Any], did: int) -> None:
        if before == after:
            return
        key = (setting, tuple(sorted(before.items())), tuple(sorted(after.items())))
        eff = effects.setdefault(
            key, {"setting": setting, "before": before, "after": after, "devices": []}
        )
        item: dict[str, Any] = {"id": did, "name": str(inv.devices[did]["name"])}
        if setting == "snmp":
            item["snmp_sensors"] = _snmp_sensor_count(inv, did)
        eff["devices"].append(item)

    for did in devices:
        add(
            "snmp",
            _cred_side(inv, "snmp", did),
            _cred_side(after_inv, "snmp", did),
            did,
        )
        add(
            "ssh",
            _cred_side(inv, "ssh", did),
            _cred_side(after_inv, "ssh", did),
            did,
        )
        add("dependency", _dep_side(inv, did), _dep_side(after_inv, did), did)
    order = {"snmp": 0, "ssh": 1, "dependency": 2}
    return {
        "devices_affected": len(devices),
        "effects": sorted(effects.values(), key=lambda e: order[e["setting"]]),
    }
