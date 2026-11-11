"""[2b-3] Passive event sensors (`trap`, `syslog`, `port_flap`) and their conditions
(architecture section 12.3): rule cache, condition upserts in one short transaction per writer
flush, status recomputation through the normal status-change path, expiry loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.ingest.rules import IF_INDEX_OID, Rule, in_scope, sort_rules
from netmon.models import Device, Event, Sensor, SensorChannel
from netmon.services import events as events_svc
from netmon.services.serialize import iso

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.ingest.event_sensors")

EVENT_TYPES = ("trap", "syslog", "port_flap", "config_backup", "new_mac")  # [3] +2
DEFAULT_NAMES = {
    "trap": "SNMP traps",
    "syslog": "Syslog",
    "port_flap": "Port flapping",
    "config_backup": "Configuration backup",  # [3]
    "new_mac": "New devices",
}
IDLE_MESSAGES = {
    "trap": "Receiving traps",
    "syslog": "Receiving syslog",
    "port_flap": "No flapping ports",
    "config_backup": "Configuration backup OK",  # [3]
    "new_mac": "No new devices",
}
OFF_MESSAGE = "Event alerts are turned off"
FLAP_OFF_MESSAGE = "Port flapping detection is turned off"
CLEARED_MESSAGE = "Cleared by admin"
MAX_CONDITIONS = 256
MAX_MESSAGE = 500
EXPIRY_MAX_WAIT_S = 30.0
LAST_MATCHED_EVERY = timedelta(minutes=1)


def is_event_type(type_: str) -> bool:
    return type_ in EVENT_TYPES


@dataclass
class Item:
    """One stored record's effect on its device's event sensor."""

    device_id: int
    kind: str  # trap | syslog
    ref_id: int | None
    text: str
    outcome: Any = None  # rules.Outcome | None


@dataclass
class _SensorInfo:
    id: int
    enabled: bool


class EventSensors:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._rules: dict[str, list[Rule]] = {"trap": [], "syslog": []}
        self._rules_by_id: dict[int, Rule] = {}
        self._group_chain: dict[int, list[int]] = {}  # device id -> group + ancestors
        self._sensors: dict[tuple[int, str], _SensorInfo] = {}
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.loaded = False

    # ---------------------------------------------------------------- caches
    async def reload(self) -> None:
        async with self.rt.sessionmaker() as session:
            rows = (
                await session.execute(
                    text("SELECT * FROM event_rules WHERE enabled ORDER BY priority, id")
                )
            ).mappings()
            rules = [Rule.from_row(r) for r in rows]
            parents = {
                r.id: r.parent_id
                for r in await session.execute(text("SELECT id, parent_id FROM groups"))
            }
            devices = (await session.execute(text("SELECT id, group_id FROM devices"))).all()
            sensors = (
                await session.execute(
                    text(
                        "SELECT id, device_id, type, enabled FROM sensors "
                        "WHERE type IN ('trap','syslog','port_flap','config_backup','new_mac')"
                    )
                )
            ).all()
        self._rules = {
            "trap": sort_rules([r for r in rules if r.kind == "trap"]),
            "syslog": sort_rules([r for r in rules if r.kind == "syslog"]),
        }
        self._rules_by_id = {r.id: r for r in rules}

        def chain(gid: int | None) -> list[int]:
            out: list[int] = []
            while gid is not None and gid in parents and gid not in out:
                out.append(gid)
                gid = parents[gid]
            return out

        self._group_chain = {d.id: chain(d.group_id) for d in devices}
        self._sensors = {(s.device_id, s.type): _SensorInfo(s.id, s.enabled) for s in sensors}
        self.loaded = True

    def rules_for(self, kind: str, device_id: int) -> list[Rule]:
        chain = self._group_chain.get(device_id, [])
        return [r for r in self._rules.get(kind, []) if in_scope(r, device_id, chain)]

    def rule(self, rule_id: int) -> Rule | None:
        return self._rules_by_id.get(rule_id)

    def sensor_paused(self, device_id: int, type_: str) -> bool:
        info = self._sensors.get((device_id, type_))
        return info is not None and not info.enabled

    # ---------------------------------------------------------------- expiry loop
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._expiry_loop(), name="netmon-event-expiry")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None

    def wake(self) -> None:
        self._wake.set()

    async def _expiry_loop(self) -> None:
        while True:
            timeout = EXPIRY_MAX_WAIT_S
            try:
                await self.expire_once()
                nxt = await self._next_expiry()
                if nxt is not None:
                    timeout = min(timeout, max(0.05, (nxt - self.rt.clock.now()).total_seconds()))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Event condition expiry failed")
            changed = self.rt.clock.changed
            waiters = [
                asyncio.ensure_future(self._wake.wait()),
                asyncio.ensure_future(changed.wait()),
            ]
            try:
                await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()
            self._wake.clear()

    async def _next_expiry(self) -> datetime | None:
        async with self.rt.sessionmaker() as session:
            return (
                await session.execute(text("SELECT min(expires_at) FROM event_conditions"))
            ).scalar()

    async def expire_once(self) -> int:
        """Remove expired conditions and recompute their sensors. Returns the count."""
        now = self.rt.clock.now()
        async with self._lock, self.rt.sessionmaker() as session:
            rows = (
                await session.execute(
                    text(
                        "DELETE FROM event_conditions WHERE expires_at <= :now "
                        "RETURNING sensor_id, source, key"
                    ),
                    {"now": now},
                )
            ).all()
            if not rows:
                await session.rollback()
                return 0
            evs, updates = await self._recompute_many(session, {r.sensor_id for r in rows})
            await session.commit()
        self._publish(evs, updates)
        flaps = [(r.sensor_id, r.key) for r in rows if r.source == "flap"]
        if flaps and getattr(self.rt, "flapping", None) is not None:
            await self.rt.flapping.conditions_expired(flaps)
        return len(rows)

    # ---------------------------------------------------------------- sensors
    async def _device(self, session: AsyncSession, device_id: int) -> Device | None:
        return await session.get(Device, device_id)

    async def get_or_create(
        self, session: AsyncSession, device: Device, type_: str, *, create: bool = True
    ) -> tuple[Sensor | None, bool]:
        """The device's event sensor of `type_` (row locked), created when missing (not for
        disabled devices or opted-out types). Returns (sensor, created)."""
        s = (
            await session.execute(
                select(Sensor)
                .where(Sensor.device_id == device.id, Sensor.type == type_)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if s is not None or not create:
            return s, False
        if not device.enabled or type_ in (device.event_sensor_optout or []):
            return None, False
        now = self.rt.clock.now()
        try:
            async with session.begin_nested():
                s = Sensor(
                    device_id=device.id,
                    name=DEFAULT_NAMES[type_],
                    type=type_,
                    config={},
                    interval_s=60,
                    enabled=True,
                    status="up",
                    status_message=IDLE_MESSAGES[type_],
                    state={},
                    sort_order=0,
                    last_check_at=now,
                    last_status_change_at=now,
                    channels=[SensorChannel(key="active", unit="#", status="up", last_value=0)],
                )
                session.add(s)
                await session.flush()
        except Exception:  # noqa: BLE001 - created concurrently (unique index)
            s = (
                await session.execute(
                    select(Sensor)
                    .where(Sensor.device_id == device.id, Sensor.type == type_)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            return s, False
        self._sensors[(device.id, type_)] = _SensorInfo(s.id, True)
        return s, True

    async def recompute(
        self,
        session: AsyncSession,
        sensor: Sensor,
        device: Device,
        *,
        idle_message: str | None = None,
    ) -> tuple[Event | None, dict[str, Any] | None]:
        """Status = worst active condition (down > warning), none -> up."""
        if not sensor.enabled or not device.enabled:
            return None, None
        conds = (
            await session.execute(
                text(
                    "SELECT c.status, c.message, c.source, r.name AS rule_name "
                    "FROM event_conditions c LEFT JOIN event_rules r ON r.id = c.rule_id "
                    "WHERE c.sensor_id = :s "
                    "ORDER BY (c.status = 'down') DESC, c.last_at DESC, c.id DESC"
                ),
                {"s": sensor.id},
            )
        ).all()
        n = len(conds)
        if n:
            worst = conds[0]
            status = worst.status
            msg = (
                f"{worst.rule_name}: {worst.message}"
                if worst.source == "rule"
                else worst.message  # flap and the [3] sources carry their own text
            )
            if n > 1:
                msg += f" (+{n - 1} more)"
        else:
            status = "up"
            msg = idle_message or IDLE_MESSAGES.get(sensor.type, "")
        msg = msg[:1000]
        now = self.rt.clock.now()
        for ch in sensor.channels:
            if ch.key == "active":
                ch.last_value = float(n)
                ch.status = status
        changed_msg = sensor.status_message != msg
        sensor.last_check_at = now
        ev = None
        old = sensor.status
        if old != status:
            sensor.status = status
            sensor.status_message = msg
            sensor.last_status_change_at = now
            ev = await events_svc.add_status_change(
                session,
                sensor=sensor,
                device=device,
                old_status=old,
                new_status=status,
                message=msg,
                time=now,
                in_maintenance=self.rt.maintenance is not None
                and self.rt.maintenance.in_maintenance(device.id),
            )
        elif changed_msg:
            sensor.status_message = msg
        update = {
            "id": sensor.id,
            "device_id": sensor.device_id,
            "status": sensor.status,
            "status_message": sensor.status_message,
            "last_check_at": iso(now),
            "status_changed": ev is not None,
            "acknowledged": sensor.ack_at is not None,
            "channels": [{"key": "active", "value": float(n), "status": status}],
        }
        return ev, update

    async def _recompute_many(
        self, session: AsyncSession, sensor_ids: Iterable[int], idle_message: str | None = None
    ) -> tuple[list[Event], list[dict[str, Any]]]:
        evs: list[Event] = []
        updates: list[dict[str, Any]] = []
        for sid in sorted(set(sensor_ids)):
            s = (
                await session.execute(select(Sensor).where(Sensor.id == sid).with_for_update())
            ).scalar_one_or_none()
            if s is None:
                continue
            d = await session.get(Device, s.device_id)
            if d is None:
                continue
            ev, upd = await self.recompute(session, s, d, idle_message=idle_message)
            if ev is not None:
                evs.append(ev)
            if upd is not None:
                updates.append(upd)
        return evs, updates

    def _publish(
        self,
        evs: list[Event],
        updates: list[dict[str, Any]],
        created: list[int] | None = None,
    ) -> None:
        for upd in updates:
            self.rt.hub.broadcast("sensor.update", upd)
        if evs:
            events_svc.published(self.rt, evs)
        for sid in created or []:
            self.rt.config_changed("sensor", sid, "created")
        if updates and not evs:
            self.rt.request_rollup()

    # ---------------------------------------------------------------- conditions
    async def _upsert_condition(
        self,
        session: AsyncSession,
        sensor_id: int,
        *,
        source: str,
        rule_id: int | None,
        key: str,
        status: str,
        message: str,
        now: datetime,
        expires_at: datetime | None,
        last_kind: str | None = None,
        last_ref_id: int | None = None,
        count: int | None = None,
    ) -> None:
        await session.execute(
            text(
                """
                INSERT INTO event_conditions (sensor_id, source, rule_id, key, status, first_at,
                    last_at, count, message, last_kind, last_ref_id, expires_at)
                VALUES (:s, :src, :r, :k, :st, :now, :now, COALESCE(:cnt, 1), :msg, :lk, :lr,
                        :exp)
                ON CONFLICT (sensor_id, source, (COALESCE(rule_id, 0)), key) DO UPDATE SET
                    status = EXCLUDED.status, last_at = EXCLUDED.last_at,
                    count = COALESCE(:cnt, event_conditions.count + 1),
                    message = EXCLUDED.message, last_kind = EXCLUDED.last_kind,
                    last_ref_id = EXCLUDED.last_ref_id, expires_at = EXCLUDED.expires_at
                """
            ),
            {
                "s": sensor_id,
                "src": source,
                "r": rule_id,
                "k": key[:128],
                "st": status,
                "now": now,
                "cnt": count,
                "msg": message[:MAX_MESSAGE],
                "lk": last_kind,
                "lr": last_ref_id,
                "exp": expires_at,
            },
        )

    async def _cap(self, session: AsyncSession, sensor_id: int) -> None:
        await session.execute(
            text(
                "DELETE FROM event_conditions WHERE id IN (SELECT id FROM event_conditions "
                "WHERE sensor_id = :s ORDER BY last_at DESC, id DESC OFFSET :n)"
            ),
            {"s": sensor_id, "n": MAX_CONDITIONS},
        )

    async def apply_items(self, items: list[Item], *, alerts_on: bool) -> None:
        """One transaction for a writer flush: create missing sensors (event_alerts ON), apply
        clears/raises, recompute the touched sensors."""
        if not items or not alerts_on:
            return
        now = self.rt.clock.now()
        evs: list[Event] = []
        updates: list[dict[str, Any]] = []
        created: list[int] = []
        matched: set[int] = set()
        by_key: dict[tuple[int, str], list[Item]] = {}
        for it in items:
            by_key.setdefault((it.device_id, it.kind), []).append(it)
        async with self._lock, self.rt.sessionmaker() as session:
            for (device_id, kind), group in sorted(by_key.items()):
                device = await self._device(session, device_id)
                if device is None or not device.enabled:
                    continue
                sensor, was_created = await self.get_or_create(session, device, kind)
                if sensor is None:
                    continue
                if was_created:
                    created.append(sensor.id)
                if not sensor.enabled:
                    continue
                touched = was_created
                for it in group:
                    out = it.outcome
                    if out is None:
                        continue
                    for rule, key in out.clears:
                        res = await session.execute(
                            text(
                                "DELETE FROM event_conditions WHERE sensor_id = :s AND "
                                "source = 'rule' AND rule_id = :r AND key = :k"
                            ),
                            {"s": sensor.id, "r": rule.id, "k": key},
                        )
                        touched = touched or bool(res.rowcount)
                    if out.raised is not None:
                        rule, key = out.raised
                        exp = (
                            now + timedelta(minutes=int(rule.clear_after_min))
                            if rule.clear_after_min
                            else None
                        )
                        await self._upsert_condition(
                            session,
                            sensor.id,
                            source="rule",
                            rule_id=rule.id,
                            key=key,
                            status=rule.action,
                            message=it.text,
                            now=now,
                            expires_at=exp,
                            last_kind=it.kind,
                            last_ref_id=it.ref_id,
                        )
                        touched = True
                    matched.update(out.matched_rule_ids)
                if touched:
                    await self._cap(session, sensor.id)
                    ev, upd = await self.recompute(session, sensor, device)
                    if ev is not None:
                        evs.append(ev)
                    if upd is not None:
                        updates.append(upd)
            if matched:
                await session.execute(
                    text(
                        "UPDATE event_rules SET last_matched_at = :now WHERE id = ANY(:ids) "
                        "AND (last_matched_at IS NULL OR last_matched_at < :cut)"
                    ),
                    {"now": now, "ids": sorted(matched), "cut": now - LAST_MATCHED_EVERY},
                )
            await session.commit()
        self._publish(evs, updates, created)
        if any(it.outcome is not None and it.outcome.raised for it in items):
            self.wake()

    # ---------------------------------------------------------------- admin / feature paths
    async def clear_sensor(
        self, sensor_id: int, message: str = CLEARED_MESSAGE
    ) -> tuple[Sensor | None, Device | None]:
        async with self._lock, self.rt.sessionmaker() as session:
            s = (
                await session.execute(
                    select(Sensor).where(Sensor.id == sensor_id).with_for_update()
                )
            ).scalar_one_or_none()
            if s is None:
                return None, None
            d = await session.get(Device, s.device_id)
            assert d is not None
            await session.execute(
                text("DELETE FROM event_conditions WHERE sensor_id = :s"), {"s": sensor_id}
            )
            ev, upd = await self.recompute(session, s, d, idle_message=message)
            await session.commit()
        self._publish([ev] if ev else [], [upd] if upd else [])
        if s.type == "port_flap" and getattr(self.rt, "flapping", None) is not None:
            await self.rt.flapping.sensor_cleared(s.device_id)
        return s, d

    async def clear_in_session(self, session: AsyncSession, sensor: Sensor) -> None:
        """Pausing an event sensor clears its conditions (the caller commits)."""
        await session.execute(
            text("DELETE FROM event_conditions WHERE sensor_id = :s"), {"s": sensor.id}
        )
        for ch in sensor.channels:
            if ch.key == "active":
                ch.last_value = 0.0

    async def remove_rule_conditions(self, rule_ids: list[int]) -> None:
        """A rule was edited / disabled / deleted: drop its conditions, recompute sensors."""
        if not rule_ids:
            return
        async with self._lock, self.rt.sessionmaker() as session:
            rows = (
                await session.execute(
                    text(
                        "DELETE FROM event_conditions WHERE rule_id = ANY(:ids) "
                        # [1.3] LAG degraded belongs to the built-in link-down rule
                        "OR (source = 'lag_degraded' AND EXISTS (SELECT 1 FROM event_rules r "
                        "WHERE r.id = ANY(:ids) AND r.builtin_key = 'trap_link_down')) "
                        "RETURNING sensor_id"
                    ),
                    {"ids": rule_ids},
                )
            ).all()
            evs, updates = await self._recompute_many(session, {r.sensor_id for r in rows})
            await session.commit()
        self._publish(evs, updates)

    async def remove_conditions(self, where: str, params: dict[str, Any], idle: str) -> None:
        """Delete conditions matching `where`; every sensor of `params["types"]` is recomputed
        (no conditions left -> up with the `idle` message)."""
        async with self._lock, self.rt.sessionmaker() as session:
            rows = (
                await session.execute(
                    text(f"DELETE FROM event_conditions WHERE {where} RETURNING sensor_id"),
                    params,
                )
            ).all()
            ids = {r.sensor_id for r in rows}
            ids |= set(
                (
                    await session.execute(
                        text("SELECT id FROM sensors WHERE type = ANY(:types)"), params
                    )
                ).scalars()
            )
            evs, updates = await self._recompute_many(session, ids, idle_message=idle)
            await session.commit()
        self._publish(evs, updates)

    async def event_alerts_off(self) -> None:
        """Feature OFF: delete all `rule` conditions; trap/syslog sensors -> up."""
        await self.remove_conditions(
            "source IN ('rule', 'lag_degraded')", {"types": ["trap", "syslog"]}, OFF_MESSAGE
        )

    async def flap_conditions_off(self) -> None:
        await self.remove_conditions("source = 'flap'", {"types": ["port_flap"]}, FLAP_OFF_MESSAGE)

    # ---------------------------------------------------------------- [3] other sources
    async def set_condition(
        self,
        device_id: int,
        type_: str,
        *,
        source: str,
        key: str,
        status: str,
        message: str,
        expires_at: datetime | None = None,
        create: bool = True,
    ) -> bool:
        """[3] Raise / refresh one condition (`backup_failed`, `host_key`, `config_changed`,
        `new_mac`) on the device's passive sensor of `type_` (created when missing unless
        opted out). Returns True when the condition is stored."""
        return bool(
            await self.set_conditions(
                device_id,
                type_,
                [(source, key, status, message, expires_at)],
                create=create,
            )
        )

    async def set_conditions(
        self,
        device_id: int,
        type_: str,
        items: list[tuple[str, str, str, str, datetime | None]],
        *,
        create: bool = True,
        session: AsyncSession | None = None,
    ) -> int:
        """[3] Several conditions `(source, key, status, message, expires_at)` on one sensor in
        one transaction (own session unless `session` is given: then the caller commits and
        publishes nothing)."""
        if not items:
            return 0
        now = self.rt.clock.now()
        created: list[int] = []
        evs: list[Event] = []
        updates: list[dict[str, Any]] = []

        async def run(s: AsyncSession) -> int:
            device = await self._device(s, device_id)
            if device is None or not device.enabled:
                return 0
            sensor, was_created = await self.get_or_create(s, device, type_, create=create)
            if sensor is None or not sensor.enabled:
                return 0
            if was_created:
                created.append(sensor.id)
            for source, key, status, message, exp in items:
                await self._upsert_condition(
                    s,
                    sensor.id,
                    source=source,
                    rule_id=None,
                    key=key,
                    status=status,
                    message=message,
                    now=now,
                    expires_at=exp,
                )
            await self._cap(s, sensor.id)
            ev, upd = await self.recompute(s, sensor, device)
            if ev is not None:
                evs.append(ev)
            if upd is not None:
                updates.append(upd)
            return len(items)

        if session is not None:
            return await run(session)
        async with self._lock, self.rt.sessionmaker() as s:
            n = await run(s)
            await s.commit()
        self._publish(evs, updates, created)
        if any(it[4] is not None for it in items):
            self.wake()
        return n

    async def remove_device_conditions(
        self,
        device_id: int | None,
        type_: str,
        sources: list[str],
        keys: list[str] | None = None,
        idle_message: str | None = None,
    ) -> int:
        """[3] Delete conditions of `sources` (optionally only `keys`) on the `type_` sensor of
        one device (or of every device when `device_id` is None) and recompute."""
        where = "s.type = :t AND c.source = ANY(:src)"
        params: dict[str, Any] = {"t": type_, "src": sources}
        if device_id is not None:
            where += " AND s.device_id = :d"
            params["d"] = device_id
        if keys is not None:
            where += " AND c.key = ANY(:keys)"
            params["keys"] = keys
        async with self._lock, self.rt.sessionmaker() as session:
            rows = (
                await session.execute(
                    text(
                        "DELETE FROM event_conditions c USING sensors s WHERE s.id = c.sensor_id "
                        f"AND {where} RETURNING c.sensor_id"
                    ),
                    params,
                )
            ).all()
            evs, updates = await self._recompute_many(
                session, {r.sensor_id for r in rows}, idle_message=idle_message
            )
            await session.commit()
        self._publish(evs, updates)
        return len(rows)

    async def link_down_cleared(self, device_id: int, if_index: int) -> None:
        """Flapping started: remove the Link-down (ifIndex-keyed) conditions of that port."""
        ids = [r.id for r in self._rules.get("trap", []) if r.key_varbind_oid == IF_INDEX_OID]
        if not ids:
            async with self.rt.sessionmaker() as session:
                ids = list(
                    (
                        await session.execute(
                            text(
                                "SELECT id FROM event_rules WHERE kind = 'trap' "
                                "AND key_varbind_oid = :o"
                            ),
                            {"o": IF_INDEX_OID},
                        )
                    ).scalars()
                )
        if not ids:
            return
        async with self._lock, self.rt.sessionmaker() as session:
            rows = (
                await session.execute(
                    text(
                        "DELETE FROM event_conditions c USING sensors s WHERE s.id = c.sensor_id "
                        "AND s.device_id = :d AND s.type = 'trap' AND c.source = 'rule' "
                        "AND c.rule_id = ANY(:ids) AND c.key = :k RETURNING c.sensor_id"
                    ),
                    {"d": device_id, "ids": ids, "k": str(if_index)},
                )
            ).all()
            evs, updates = await self._recompute_many(session, {r.sensor_id for r in rows})
            await session.commit()
        self._publish(evs, updates)

    async def flap_condition(
        self, device_id: int, if_index: int, message: str, count: int, quiet_min: float
    ) -> tuple[bool, list[int]]:
        """Raise / refresh the flap condition of a port on the device's `port_flap` sensor.
        Returns (raised, interface sensor ids of that port)."""
        now = self.rt.clock.now()
        created: list[int] = []
        async with self._lock, self.rt.sessionmaker() as session:
            device = await self._device(session, device_id)
            if device is None or not device.enabled:
                return False, []
            sensor, was_created = await self.get_or_create(session, device, "port_flap")
            if sensor is None or not sensor.enabled:
                await session.commit()
                return False, []
            if was_created:
                created.append(sensor.id)
            await self._upsert_condition(
                session,
                sensor.id,
                source="flap",
                rule_id=None,
                key=str(if_index),
                status="warning",
                message=message,
                now=now,
                expires_at=now + timedelta(minutes=quiet_min),
                count=count,
            )
            await self._cap(session, sensor.id)
            ev, upd = await self.recompute(session, sensor, device)
            iface = await interface_sensor_ids(session, device_id, if_index)
            await session.commit()
        self._publish([ev] if ev else [], [upd] if upd else [], created)
        self.wake()
        return True, iface


async def interface_sensor_ids(session: AsyncSession, device_id: int, if_index: int) -> list[int]:
    rows = await session.execute(
        text("SELECT id, config FROM sensors WHERE device_id = :d AND type = 'snmp_interface'"),
        {"d": device_id},
    )
    out = []
    for r in rows:
        try:
            if int((r.config or {}).get("if_index")) == if_index:
                out.append(int(r.id))
        except (TypeError, ValueError):
            continue
    return out


async def conditions_out(session: AsyncSession, sensor_id: int) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                "SELECT c.*, r.name AS rule_name FROM event_conditions c "
                "LEFT JOIN event_rules r ON r.id = c.rule_id WHERE c.sensor_id = :s "
                "ORDER BY (c.status = 'down') DESC, c.last_at DESC, c.id DESC"
            ),
            {"s": sensor_id},
        )
    ).mappings()
    return [
        {
            "id": int(r["id"]),
            "source": r["source"],
            "rule_id": r["rule_id"],
            "rule_name": r["rule_name"],
            "key": r["key"],
            "status": r["status"],
            "first_at": iso(r["first_at"]),
            "last_at": iso(r["last_at"]),
            "count": int(r["count"]),
            "message": r["message"],
            "last_kind": r["last_kind"],
            "last_ref_id": r["last_ref_id"],
            "expires_at": iso(r["expires_at"]),
        }
        for r in rows
    ]
