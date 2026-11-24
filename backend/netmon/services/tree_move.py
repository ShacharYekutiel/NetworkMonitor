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

from dataclasses import dataclass, field
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
