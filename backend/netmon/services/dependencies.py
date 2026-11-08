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
        (inv.sensors[sid].get("sort_order", 0), sid)
        for sid in _sensors_of(inv, parent_id)
        if inv.sensors[sid].get("type") == "ping" and inv.sensors[sid].get("enabled", True)
    ]
    return min(pings)[1] if pings else None


def _sensors_of(inv: Inventory, device_id: int) -> list[int]:
    ids = inv.device_sensors.get(device_id)
    if ids is None:  # an inventory that was not computed
        ids = [sid for sid, x in inv.sensors.items() if x["device_id"] == device_id]
    return ids


def parent_down(inv: Inventory, eff: EffectiveParent) -> bool:
    p = inv.devices.get(eff.device_id)
    if p is None or not p.get("enabled", True):
        return False
    sid = dependency_sensor(inv, eff.device_id, eff.sensor_id)
    if sid is not None:
        return inv.sensors[sid]["status"] in DOWNISH
    statuses = [
        inv.sensors[x]["status"]
        for x in _sensors_of(inv, eff.device_id)
        if inv.sensors[x]["status"] != "paused"
    ]
    return bool(statuses) and all(st in DOWNISH for st in statuses)


def parents_with_dependents(inv: Inventory) -> set[int]:
    """The reverse index: devices that are somebody's effective parent."""
    out = set()
    for did in inv.devices:
        eff = effective_parent(inv, did)
        if eff is not None:
            out.add(eff.device_id)
    return out


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


# ------------------------------------------------------------------ processor helpers
@dataclass(frozen=True)
class ParentState:
    device_id: int
    name: str
    down: bool
    sensor_id: int | None  # the resolved dependency sensor (None = whole device)
    check_ids: list[int]  # what to re-check: the dependency sensor, or every enabled sensor
    hold_s: int  # dependency sensor interval + timeout (whole device: the largest)
    last_change: Any  # the dependency sensor's last_status_change_at (whole device: latest)


def _timeout_s(interval_s: int) -> int:
    return min(int(interval_s), 120)  # as in services.sensors.build_context


async def parent_state(session: AsyncSession, device: Any) -> ParentState | None:
    """The effective parent of `device` (an ORM object or a row with the device columns) and
    whether it is down, from targeted queries. None: no (enabled) parent."""
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
    if candidate is None or candidate == device.id:
        return None
    prow = (
        await session.execute(
            text("SELECT id, name, enabled FROM devices WHERE id = :id"), {"id": candidate}
        )
    ).first()
    if prow is None or not prow.enabled:
        return None
    inv.devices[prow.id] = {"group_id": -1, "name": prow.name, "enabled": prow.enabled}
    meta: dict[int, Any] = {}
    for r in await session.execute(
        text(
            "SELECT id, type, enabled, sort_order, status, interval_s, last_status_change_at "
            "FROM sensors WHERE device_id = :d"
        ),
        {"d": prow.id},
    ):
        inv.sensors[r.id] = {
            "device_id": prow.id,
            "type": r.type,
            "enabled": r.enabled,
            "sort_order": r.sort_order,
            "status": r.status,
        }
        meta[r.id] = r
    eff = effective_parent(inv, device.id)
    if eff is None:
        return None
    sid = dependency_sensor(inv, eff.device_id, eff.sensor_id)
    if sid is not None:
        ids = [sid]
    else:
        ids = [x for x, m in meta.items() if m.enabled and m.status != "paused"]
    holds = [int(meta[x].interval_s) + _timeout_s(meta[x].interval_s) for x in ids]
    changes = [meta[x].last_status_change_at for x in ids if meta[x].last_status_change_at]
    return ParentState(
        device_id=prow.id,
        name=str(prow.name),
        down=parent_down(inv, eff),
        sensor_id=sid,
        check_ids=sorted(ids),
        hold_s=max(holds) if holds else 0,
        last_change=max(changes) if changes else None,
    )


async def down_parent_name(session: AsyncSession, device: Any) -> str | None:
    """The effective parent's name when that parent is down, else None."""
    ps = await parent_state(session, device)
    return ps.name if ps is not None and ps.down else None


async def parent_hold_until(session: AsyncSession, device_id: int, t: Any) -> Any:
    """A child's new DOWN is not due before its parent was re-checked:
    t + the parent's dependency-sensor interval + timeout. None without an (up) parent."""
    from datetime import timedelta

    from netmon.models import Device

    device = await session.get(Device, device_id)
    if device is None:
        return None
    ps = await parent_state(session, device)
    if ps is None or ps.down or ps.hold_s <= 0:
        return None
    return t + timedelta(seconds=ps.hold_s)


def unreachable_message(parent_name: str, message: str) -> str:
    return (UNREACHABLE_PREFIX.format(name=parent_name) + (message or ""))[:4000]


# ------------------------------------------------------------------ cascade
CASCADE_DEBOUNCE_S = 0.5


class Cascade:
    """Coalesces status changes (debounce, one inventory load per burst) and skips parents
    without dependents (reverse index, invalidated on device/group changes)."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._pending: dict[int, set[int]] = {}
        self._task: Any = None
        self._parents: set[int] | None = None  # None = unknown, load on the next burst

    def invalidate(self) -> None:
        self._parents = None

    def submit(self, events: list[Event]) -> None:
        added = False
        for ev in events:
            if ev.kind != "status_change" or ev.device_id is None or ev.sensor_id is None:
                continue
            if self._parents is not None and ev.device_id not in self._parents:
                continue  # nobody depends on this device
            self._pending.setdefault(ev.device_id, set()).add(ev.sensor_id)
            added = True
        if added and (self._task is None or self._task.done()):
            self._task = self.rt.spawn(self._run())

    async def _run(self) -> None:
        import asyncio

        while self._pending:
            if CASCADE_DEBOUNCE_S > 0:
                await asyncio.sleep(CASCADE_DEBOUNCE_S)
            changed, self._pending = self._pending, {}
            try:
                await _cascade(self.rt, changed, self)
            except Exception:  # noqa: BLE001
                log.exception("Dependency cascade failed")


async def on_status_change(rt: Runtime, events: list[Event]) -> None:
    """Direct (non-coalesced) cascade for a list of committed status_change events."""
    changed: dict[int, set[int]] = {}
    for ev in events:
        if ev.kind == "status_change" and ev.device_id is not None and ev.sensor_id is not None:
            changed.setdefault(ev.device_id, set()).add(ev.sensor_id)
    try:
        await _cascade(rt, changed, None)
    except Exception:  # noqa: BLE001
        log.exception("Dependency cascade failed")


async def _cascade(rt: Runtime, changed: dict[int, set[int]], cache: Cascade | None) -> None:
    """Children of a parent that went down turn `unreachable`; children of a parent that is
    no longer down are re-checked."""
    from netmon.models import Device, Sensor
    from netmon.services import events as events_svc
    from netmon.services.serialize import now_utc
    from netmon.services.status import load_inventory

    if not changed:
        return
    async with rt.sessionmaker() as session:
        inv = await load_inventory(session)
    if cache is not None:
        cache._parents = parents_with_dependents(inv)  # noqa: SLF001
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
    converted: list[Any] = []
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
            # re-evaluate the parent inside the locking transaction: it may have recovered
            still_down: dict[int, str | None] = {}
            for did, dev in devices.items():
                still_down[did] = await down_parent_name(session, dev)
            now = now_utc()
            for s in sensors:
                dev = devices[s.device_id]
                pname = still_down.get(s.device_id)
                if pname is None:
                    continue
                msg = unreachable_message(pname, s.status_message)
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
                converted.append(s)
            await session.commit()
        for s in converted:
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
    # serialize dependency writes: two concurrent requests cannot together create a cycle
    await session.execute(text("SELECT pg_advisory_xact_lock(727003)"))
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
