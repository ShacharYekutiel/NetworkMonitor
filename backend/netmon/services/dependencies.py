"""[2b-1] Device dependencies: resolution, parent state, cycles (pure) and the cascade (I/O).

See docs/data-model.md "Device dependencies"."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.services.status import Inventory

if TYPE_CHECKING:
    from netmon.models import Event
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.dependencies")

DOWNISH = ("down", "unreachable")
RECHECK_JITTER_S = 5.0
UNREACHABLE_PREFIX = "Unreachable (parent {name} down): "


@dataclass(frozen=True)
class EffectiveParent:
    device_id: int
    sensor_id: int | None  # the configured dependency sensor (None = automatic)
    source: str  # "device" | "group"
    group_id: int | None  # the deciding group when source = "group"


# ------------------------------------------------------------------ pure
def effective_parent(inv: Inventory, device_id: int) -> EffectiveParent | None:
    d = inv.devices.get(device_id)
    if d is None:
        return None
    mode = d.get("dependency_mode") or "inherit"
    if mode == "none":
        return None
    if mode == "device":
        pid = d.get("dependency_device_id")
        if pid is None or pid not in inv.devices:
            return None  # parent deleted: behaves as none
        return EffectiveParent(pid, d.get("dependency_sensor_id"), "device", None)
    for gid in inv.ancestors(d["group_id"]):
        g = inv.groups[gid]
        gmode = g.get("dependency_mode") or "inherit"
        if gmode == "inherit":
            continue
        if gmode == "none":
            return None
        pid = g.get("dependency_device_id")
        if pid is None or pid not in inv.devices:
            return None
        if pid == device_id:
            return None  # self rule: an inherited parent equal to the device is ignored
        return EffectiveParent(pid, g.get("dependency_sensor_id"), "group", gid)
    return None


def dependency_sensor(inv: Inventory, parent_id: int, configured: int | None = None) -> int | None:
    """The configured sensor, else the parent's primary availability sensor (enabled ping with
    the lowest (sort_order, id)), else None ("whole device")."""
    if configured is not None and configured in inv.sensors:
        return configured
    pings = [
        (s.get("sort_order", 0), sid)
        for sid, s in inv.sensors.items()
        if s["device_id"] == parent_id and s.get("type") == "ping" and s.get("enabled", True)
    ]
    return min(pings)[1] if pings else None


def parent_down(inv: Inventory, eff: EffectiveParent) -> bool:
    p = inv.devices.get(eff.device_id)
    if p is None or not p.get("enabled", True):
        return False
    sid = dependency_sensor(inv, eff.device_id, eff.sensor_id)
    if sid is not None:
        return inv.sensors[sid]["status"] in DOWNISH
    statuses = [
        s["status"]
        for s in inv.sensors.values()
        if s["device_id"] == eff.device_id and s["status"] != "paused"
    ]
    return bool(statuses) and all(st in DOWNISH for st in statuses)


def find_cycle(inv: Inventory) -> list[int] | None:
    """Follow effective parents from every device; returns the cycle path (e.g. [3, 7, 3])."""
    parent: dict[int, int | None] = {}
    for did in inv.devices:
        eff = effective_parent(inv, did)
        parent[did] = eff.device_id if eff else None
    safe: set[int] = set()
    for start in sorted(inv.devices):
        path: list[int] = []
        on_path: set[int] = set()
        cur: int | None = start
        while cur is not None and cur not in safe:
            if cur in on_path:
                i = path.index(cur)
                return [*path[i:], cur]
            path.append(cur)
            on_path.add(cur)
            cur = parent.get(cur)
        safe.update(path)
    return None


def dependency_out(inv: Inventory, owner: dict[str, Any]) -> dict[str, Any]:
    """The API `Dependency` of a device or group row (dict from the inventory or an ORM obj)."""
    mode = _get(owner, "dependency_mode") or "inherit"
    did = _get(owner, "dependency_device_id")
    sid = _get(owner, "dependency_sensor_id")
    if mode != "device":
        did = sid = None
    return {
        "mode": mode,
        "device_id": did,
        "device_name": inv.devices[did]["name"] if did in inv.devices else None,
        "sensor_id": sid,
        "sensor_name": inv.sensors[sid].get("name") if sid in inv.sensors else None,
    }


def effective_dependency_out(inv: Inventory, device_id: int) -> dict[str, Any] | None:
    eff = effective_parent(inv, device_id)
    if eff is None:
        return None
    sid = dependency_sensor(inv, eff.device_id, eff.sensor_id)
    return {
        "device_id": eff.device_id,
        "device_name": inv.devices[eff.device_id]["name"],
        "sensor_id": sid,
        "sensor_name": inv.sensors[sid].get("name") if sid is not None else None,
        "source": eff.source,
        "group_id": eff.group_id,
        "parent_down": parent_down(inv, eff),
    }


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


# ------------------------------------------------------------------ processor helper
async def down_parent_name(session: AsyncSession, device: Any) -> str | None:
    """The effective parent's name when that parent is down, else None (targeted queries)."""
    mode = device.dependency_mode or "inherit"
    if mode == "none":
        return None
    inv = Inventory()
    inv.devices[device.id] = {
        "group_id": device.group_id,
        "name": device.name,
        "enabled": device.enabled,
        "dependency_mode": mode,
        "dependency_device_id": device.dependency_device_id,
        "dependency_sensor_id": device.dependency_sensor_id,
    }
    candidate: int | None = device.dependency_device_id if mode == "device" else None
    if mode == "inherit":
        rows = await session.execute(
            text(
                """
                WITH RECURSIVE chain(id, parent_id, dependency_mode, dependency_device_id,
                                     dependency_sensor_id, depth) AS (
                    SELECT id, parent_id, dependency_mode, dependency_device_id,
                           dependency_sensor_id, 0 FROM groups WHERE id = :gid
                    UNION ALL
                    SELECT g.id, g.parent_id, g.dependency_mode, g.dependency_device_id,
                           g.dependency_sensor_id, c.depth + 1
                    FROM groups g JOIN chain c ON g.id = c.parent_id
                    WHERE c.depth < 100 AND c.dependency_mode = 'inherit'
                )
                SELECT * FROM chain ORDER BY depth
                """
            ),
            {"gid": device.group_id},
        )
        for r in rows:
            inv.groups[r.id] = {
                "parent_id": r.parent_id,
                "dependency_mode": r.dependency_mode,
                "dependency_device_id": r.dependency_device_id,
                "dependency_sensor_id": r.dependency_sensor_id,
            }
            if r.dependency_mode == "device":
                candidate = r.dependency_device_id
        # an ancestor outside the chain is unknown to `ancestors()` -> treated as the root
    if candidate is None or candidate == device.id and mode != "device":
        return None
    prow = (
        await session.execute(
            text("SELECT id, name, enabled FROM devices WHERE id = :id"), {"id": candidate}
        )
    ).first()
    if prow is None:
        return None
    inv.devices[prow.id] = {
        **inv.devices.get(prow.id, {}),
        "group_id": inv.devices.get(prow.id, {}).get("group_id", -1),
        "name": prow.name,
        "enabled": prow.enabled,
    }
    for r in await session.execute(
        text("SELECT id, type, enabled, sort_order, status FROM sensors WHERE device_id = :d"),
        {"d": prow.id},
    ):
        inv.sensors[r.id] = {
            "device_id": prow.id,
            "type": r.type,
            "enabled": r.enabled,
            "sort_order": r.sort_order,
            "status": r.status,
        }
    eff = effective_parent(inv, device.id)
    if eff is None or not parent_down(inv, eff):
        return None
    return str(prow.name)


def unreachable_message(parent_name: str, message: str) -> str:
    return (UNREACHABLE_PREFIX.format(name=parent_name) + (message or ""))[:4000]


# ------------------------------------------------------------------ cascade
async def on_status_change(rt: Runtime, events: list[Event]) -> None:
    """Called after status_change events were committed: children of a parent that went down
    turn `unreachable`; children of a parent that is no longer down are re-checked."""
    try:
        await _cascade(rt, events)
    except Exception:  # noqa: BLE001
        log.exception("Dependency cascade failed")


async def _cascade(rt: Runtime, events: list[Event]) -> None:
    from netmon.models import Device, Sensor
    from netmon.services import events as events_svc
    from netmon.services.serialize import now_utc
    from netmon.services.status import load_inventory

    changed: dict[int, set[int]] = {}
    for ev in events:
        if ev.kind == "status_change" and ev.device_id is not None and ev.sensor_id is not None:
            changed.setdefault(ev.device_id, set()).add(ev.sensor_id)
    if not changed:
        return
    async with rt.sessionmaker() as session:
        configured = (
            await session.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM devices WHERE dependency_mode = 'device') "
                    "OR EXISTS (SELECT 1 FROM groups WHERE dependency_mode = 'device')"
                )
            )
        ).scalar()
        if not configured:
            return  # no dependencies anywhere: nothing to cascade
        inv = await load_inventory(session)
    to_unreachable: dict[int, str] = {}  # child device -> parent name
    recheck: list[int] = []
    for cid in inv.devices:
        eff = effective_parent(inv, cid)
        if eff is None or eff.device_id not in changed:
            continue
        sid = dependency_sensor(inv, eff.device_id, eff.sensor_id)
        if sid is not None and sid not in changed[eff.device_id]:
            continue
        if parent_down(inv, eff):
            if any(inv.sensors[s]["status"] == "down" for s in inv.device_sensors.get(cid, [])):
                to_unreachable[cid] = inv.devices[eff.device_id]["name"]
        else:
            recheck += [
                s
                for s in inv.device_sensors.get(cid, [])
                if inv.sensors[s]["status"] == "unreachable"
            ]
    new_events: list[Any] = []
    if to_unreachable:
        maint = getattr(rt, "maintenance", None)
        async with rt.sessionmaker() as session:
            sensors = (
                (
                    await session.execute(
                        select(Sensor)
                        .where(Sensor.device_id.in_(list(to_unreachable)), Sensor.status == "down")
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            devices = {
                d.id: d
                for d in (
                    await session.execute(select(Device).where(Device.id.in_(list(to_unreachable))))
                ).scalars()
            }
            now = now_utc()
            for s in sensors:
                dev = devices[s.device_id]
                msg = unreachable_message(to_unreachable[s.device_id], s.status_message)
                s.status = "unreachable"
                s.status_message = msg
                s.last_status_change_at = now
                ev = await events_svc.add_status_change(
                    session,
                    sensor=s,
                    device=dev,
                    old_status="down",
                    new_status="unreachable",
                    message=msg,
                    time=now,
                    in_maintenance=bool(maint and maint.in_maintenance(dev.id)),
                )
                new_events.append(ev)
            await session.commit()
        for s in sensors:
            rt.hub.broadcast(
                "sensor.update",
                {
                    "id": s.id,
                    "device_id": s.device_id,
                    "status": s.status,
                    "status_message": s.status_message,
                    "last_check_at": _iso(s.last_check_at),
                    "status_changed": True,
                    "acknowledged": False,
                    "channels": [
                        {"key": c.key, "value": c.last_value, "status": c.status}
                        for c in s.channels
                    ],
                },
            )
        if new_events:
            events_svc.published(rt, new_events)
    if recheck and rt.scheduler is not None:
        rt.scheduler.run_soon(recheck, jitter_s=RECHECK_JITTER_S)


def _iso(dt: Any) -> str | None:
    from netmon.services.serialize import iso

    return iso(dt)


# ------------------------------------------------------------------ API helpers
async def validated_dependency(
    session: AsyncSession, dep: Any, loc: list[Any]
) -> tuple[str, int | None, int | None]:
    """DependencyIn -> (mode, device_id, sensor_id). Raises ApiError(422) with `loc` prefix
    (e.g. ["body", "dependency"])."""
    from netmon.api.errors import validation_error

    if dep.mode != "device":
        if dep.device_id is not None:
            raise validation_error([*loc, "device_id"], f"must be null for mode {dep.mode}")
        if dep.sensor_id is not None:
            raise validation_error([*loc, "sensor_id"], f"must be null for mode {dep.mode}")
        return dep.mode, None, None
    if dep.device_id is None:
        raise validation_error([*loc, "device_id"], "required for mode device")
    exists = (
        await session.execute(text("SELECT 1 FROM devices WHERE id = :id"), {"id": dep.device_id})
    ).first()
    if exists is None:
        raise validation_error([*loc, "device_id"], "device does not exist")
    if dep.sensor_id is not None:
        owner = (
            await session.execute(
                text("SELECT device_id FROM sensors WHERE id = :id"), {"id": dep.sensor_id}
            )
        ).scalar_one_or_none()
        if owner != dep.device_id:
            raise validation_error([*loc, "sensor_id"], "sensor does not belong to the device")
    return "device", dep.device_id, dep.sensor_id


def apply_dependency(obj: Any, values: tuple[str, int | None, int | None]) -> None:
    obj.dependency_mode, obj.dependency_device_id, obj.dependency_sensor_id = values


async def check_no_cycle(session: AsyncSession) -> None:
    """On the session's current (flushed, uncommitted) state; raises 400 dependency_cycle."""
    from netmon.api.errors import ApiError
    from netmon.services.status import load_inventory

    await session.flush()
    cycle = find_cycle(await load_inventory(session))
    if cycle is not None:
        raise ApiError(
            400,
            "dependency_cycle",
            "This dependency would create a cycle: " + " -> ".join(str(i) for i in cycle),
            [{"device_ids": cycle}],
        )


async def reset_dependents(session: AsyncSession, device_id: int) -> list[int]:
    """Device delete: dependents fall back to `none` (same transaction). Returns the
    affected device ids."""
    ids = list(
        (
            await session.execute(
                text(
                    "UPDATE devices SET dependency_mode = 'none', dependency_device_id = NULL, "
                    "dependency_sensor_id = NULL WHERE dependency_device_id = :id RETURNING id"
                ),
                {"id": device_id},
            )
        ).scalars()
    )
    await session.execute(
        text(
            "UPDATE groups SET dependency_mode = 'none', dependency_device_id = NULL, "
            "dependency_sensor_id = NULL WHERE dependency_device_id = :id"
        ),
        {"id": device_id},
    )
    return ids
