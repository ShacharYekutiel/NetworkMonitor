"""[if-noise] Sensor event noise: debounce and flapping collapse of the event-log rows of polled
sensors (data-model "[if-noise] Sensor event noise", architecture "[if-noise]").

Only the event LOG is affected. Every status change is still written as an `events` row in the
same transaction as the sensor update and handed to the alert engine, the dependency cascade
and the rollup at once (alert timing does not change); the sensor's current status is updated
and broadcast in real time. What changes is whether the row is *shown*:

- **Debounce** (`sensor_event_debounce_enabled`, default ON): a change is written with
  `noise = 'pending'` and shown (`noise` NULL, `event.created` broadcast) once it has held for
  `hold_seconds()` = max((polls + 0.5) x interval, min_s) plus the check timeout and a grace of
  5 s. A change back to the previous status within the hold hides both rows (`debounced`): one
  failed poll that recovers on the next poll leaves no row in the log. Any other change shows
  the pending row at once (the earlier status did hold).
- **Flapping** (`sensor_event_flap_enabled`, default ON): the same definition as port flapping
  (`netmon.ingest.flapping.FlapTracker` with `flap_threshold` changes within `flap_window_min`,
  settled after `flap_quiet_min` without a change). From the change that crosses the threshold
  on, changes are written with `noise = 'flapping'` and one `sensor_flapping` row per episode
  carries the count (`flap_count`, `flap_last`, its `new_status` = the latest status); when the
  sensor settles the row gets `flap_end` and, when the settled status differs from the last
  shown one, the last hidden change is shown so the log ends in the sensor's real status.

Transitions from `unknown` / `paused` / no status (first check, resume, restart) are never
debounced or counted. Both switches OFF: rows are written and shown exactly as before."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text

from netmon.ingest.flapping import FlapTracker
from netmon.models import Event
from netmon.services.serialize import event_out

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.event_noise")

KEYS = (
    "sensor_event_debounce_enabled",
    "sensor_event_debounce_polls",
    "sensor_event_debounce_min_s",
    "sensor_event_flap_enabled",
    "flap_threshold",
    "flap_window_min",
    "flap_quiet_min",
)
DEFAULTS: dict[str, Any] = {
    "sensor_event_debounce_enabled": True,
    "sensor_event_debounce_polls": 1,
    "sensor_event_debounce_min_s": 0,
    "sensor_event_flap_enabled": True,
}
EXEMPT_FROM = (None, "unknown", "paused")
GRACE_S = 5.0
SWEEP_S = 5.0
NOISE_VALUES = ("pending", "debounced", "flapping")


def hold_seconds(interval_s: float, polls: int, min_s: float) -> float:
    """How long a new status must hold before its row is shown: a change back within this time
    is a blip. (polls + 0.5) intervals covers the next `polls` checks with their jitter."""
    return max((max(1, int(polls)) + 0.5) * max(1.0, float(interval_s)), float(min_s or 0))


def check_timeout(interval_s: float) -> float:
    """The check timeout of a sensor (netmon.services.sensors.build_context)."""
    return float(min(interval_s, 120))


def flap_message(name: str, count: int, window_min: int, status: str | None) -> str:
    return f"{name} is flapping ({count} changes, threshold window {window_min} min), now {status}"


def flap_end_message(name: str, count: int, status: str | None) -> str:
    return f"{name} stopped flapping after {count} changes, now {status}"


@dataclass
class Decision:
    """What `on_change` decided for one status-change row (persist publishes accordingly)."""

    visible: bool = True
    shown: list[Event] = field(default_factory=list)  # earlier rows shown now
    flap_created: Event | None = None
    flap_updated: Event | None = None


class EventNoise:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.debounce = bool(DEFAULTS["sensor_event_debounce_enabled"])
        self.polls = int(DEFAULTS["sensor_event_debounce_polls"])
        self.min_s = int(DEFAULTS["sensor_event_debounce_min_s"])
        self.flap = bool(DEFAULTS["sensor_event_flap_enabled"])
        self.tracker = FlapTracker()
        self._counts: dict[int, int] = {}  # changes of the open episode per sensor
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle
    def configure(self, cfg: dict[str, Any]) -> None:
        self.debounce = bool(cfg.get("sensor_event_debounce_enabled", True))
        self.polls = int(cfg.get("sensor_event_debounce_polls") or 1)
        self.min_s = int(cfg.get("sensor_event_debounce_min_s") or 0)
        self.flap = bool(cfg.get("sensor_event_flap_enabled", True))
        self.tracker.configure(
            int(cfg.get("flap_threshold") or 5),
            float(cfg.get("flap_window_min") or 10),
            float(cfg.get("flap_quiet_min") or 15),
        )

    async def load(self) -> None:
        from netmon.services import settings as settings_svc

        async with self.rt.sessionmaker() as session:
            self.configure(await settings_svc.get_all(session))

    async def start(self) -> None:
        await self.load()
        if self.flap:
            await self._restore()
        else:
            await self.end_all_flapping()
        if not self.debounce:
            await self.show_all_pending()
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="netmon-event-noise")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    def reset(self) -> None:
        """Forget the in-memory state and use the default settings (tests: after a truncate)."""
        from netmon.services import settings as settings_svc

        self.tracker.clear()
        self._counts.clear()
        self.configure(settings_svc.defaults())

    async def reload(self) -> None:
        """Settings changed: turning a protection OFF shows what it was holding back now."""
        was_debounce, was_flap = self.debounce, self.flap
        await self.load()
        if was_debounce and not self.debounce:
            await self.show_all_pending()
        if was_flap and not self.flap:
            await self.end_all_flapping()
        self._wake.set()

    async def _restore(self) -> None:
        """Open episodes (rows without `flap_end`) keep suppressing after a restart."""
        async with self.rt.sessionmaker() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT sensor_id, flap_count, flap_last, time FROM events "
                        "WHERE kind = 'sensor_flapping' AND flap_end IS NULL "
                        "AND sensor_id IS NOT NULL"
                    )
                )
            ).all()
        for r in rows:
            last = r.flap_last or r.time
            self.tracker.mark_flapping((int(r.sensor_id), 0), float(last.timestamp()))
            self._counts[int(r.sensor_id)] = int(r.flap_count or 0)

    # ------------------------------------------------------------------ write path
    async def on_change(self, session: Any, sensor: Any, event: Event, t: datetime) -> Decision:
        """Called by the poller (`persist`) in the transaction that wrote `event` (the sensor row
        is locked). Sets `event.noise` and writes the related rows; never delays alerts."""
        d = Decision()
        if not (self.debounce or self.flap) or event.old_status in EXEMPT_FROM:
            return d
        if event.new_status in EXEMPT_FROM:
            return d
        sid = int(sensor.id)
        key = (sid, 0)
        if self.flap:
            change = self.tracker.add(key, str(event.new_status), t.timestamp())
            if self.tracker.is_flapping(key):
                await self._flapping(session, sensor, event, t, change, d)
                return d
        if self.debounce:
            hold = hold_seconds(sensor.interval_s, self.polls, self.min_s)
            pending = (
                await session.execute(
                    select(Event)
                    .where(Event.sensor_id == sid, Event.noise == "pending", Event.id != event.id)
                    .order_by(Event.time.desc(), Event.id.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if pending is not None:
                if (
                    event.new_status == pending.old_status
                    and (t - pending.time).total_seconds() <= hold
                ):
                    pending.noise = "debounced"
                    pending.noise_until = None
                    event.noise = "debounced"
                    d.visible = False
                    return d
                pending.noise = None  # a different change: the earlier status did hold
                pending.noise_until = None
                d.shown.append(pending)
            event.noise = "pending"
            event.noise_until = t + timedelta(
                seconds=hold + check_timeout(sensor.interval_s) + GRACE_S
            )
            d.visible = False
        return d

    async def _flapping(
        self, session: Any, sensor: Any, event: Event, t: datetime, change: Any, d: Decision
    ) -> None:
        sid = int(sensor.id)
        event.noise = "flapping"
        event.noise_until = None
        d.visible = False
        # pending changes of the sensor join the episode
        await session.execute(
            text(
                "UPDATE events SET noise = 'flapping', noise_until = NULL "
                "WHERE sensor_id = :s AND noise = 'pending'"
            ),
            {"s": sid},
        )
        row = (
            await session.execute(
                select(Event)
                .where(
                    Event.sensor_id == sid,
                    Event.kind == "sensor_flapping",
                    Event.flap_end.is_(None),
                )
                .order_by(Event.id.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            count = change.count if change is not None else 1
            self._counts[sid] = count
            from netmon.models import Device

            device = await session.get(Device, sensor.device_id)
            row = Event(
                time=t,
                kind="sensor_flapping",
                sensor_id=sid,
                device_id=sensor.device_id,
                group_id=device.group_id if device is not None else None,
                sensor_name=sensor.name,
                device_name=device.name if device is not None else None,
                old_status=None,
                new_status=event.new_status,
                message=flap_message(
                    sensor.name, count, int(self.tracker.window_s // 60), event.new_status
                ),
                in_maintenance=bool(event.in_maintenance),
                flap_count=count,
                flap_last=t,
            )
            session.add(row)
            await session.flush()
            d.flap_created = row
            return
        count = self._counts.get(sid, int(row.flap_count or 0)) + 1
        self._counts[sid] = count
        row.flap_count = count
        row.flap_last = t
        row.new_status = event.new_status
        row.message = flap_message(
            sensor.name, count, int(self.tracker.window_s // 60), event.new_status
        )
        d.flap_updated = row

    def publish(self, event: Event, d: Decision) -> None:
        """After the commit: broadcasts for the rows that became visible or changed."""
        hub = self.rt.hub
        for ev in d.shown:
            hub.broadcast("event.created", event_out(ev))
        if d.flap_created is not None:
            hub.broadcast("event.created", event_out(d.flap_created))
        if d.flap_updated is not None:
            hub.broadcast("event.updated", event_out(d.flap_updated))
        if not d.visible and event.sensor_id is not None:
            # [severity] the hidden row's event.created would have carried the sensor's current
            # severity: send it on its own so the UI stays real-time
            sev = event.severity if event.new_status in ("down", "warning") else None
            hub.broadcast(
                "sensor.severity",
                {"id": event.sensor_id, "device_id": event.device_id, "severity": sev},
            )

    # ------------------------------------------------------------------ sweeper
    async def _loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Event noise sweep failed")
            changed = self.rt.clock.changed
            waiters = [
                asyncio.ensure_future(self._wake.wait()),
                asyncio.ensure_future(changed.wait()),
            ]
            try:
                await asyncio.wait(waiters, timeout=SWEEP_S, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()
            self._wake.clear()

    async def sweep(self, now: datetime | None = None) -> None:
        """Show the pending rows whose hold is over; close the episodes that settled."""
        now = now or self.rt.clock.now()
        await self._show_due(now)
        for ch in self.tracker.expire(now.timestamp()):
            end = datetime.fromtimestamp(ch.at, tz=now.tzinfo)
            await self._end_episode(int(ch.port[0]), min(end, now))

    async def _show_due(self, now: datetime | None) -> list[Event]:
        cond = "noise = 'pending'" + (" AND noise_until <= :now" if now is not None else "")
        async with self.rt.sessionmaker() as session:
            ids = [
                int(r[0])
                for r in await session.execute(
                    text(
                        f"UPDATE events SET noise = NULL, noise_until = NULL WHERE {cond} "  # noqa: S608
                        "RETURNING id"
                    ),
                    {"now": now} if now is not None else {},
                )
            ]
            rows = (
                list(
                    (
                        await session.execute(
                            select(Event).where(Event.id.in_(ids)).order_by(Event.time, Event.id)
                        )
                    ).scalars()
                )
                if ids
                else []
            )
            await session.commit()
        for ev in rows:
            self.rt.hub.broadcast("event.created", event_out(ev))
        return rows

    async def show_all_pending(self) -> list[Event]:
        return await self._show_due(None)

    async def end_all_flapping(self) -> None:
        async with self.rt.sessionmaker() as session:
            ids = [
                int(r[0])
                for r in await session.execute(
                    text(
                        "SELECT DISTINCT sensor_id FROM events WHERE kind = 'sensor_flapping' "
                        "AND flap_end IS NULL AND sensor_id IS NOT NULL"
                    )
                )
            ]
        now = self.rt.clock.now()
        for sid in ids:
            await self._end_episode(sid, now)
        self.tracker.clear()
        self._counts.clear()

    async def _end_episode(self, sensor_id: int, end: datetime) -> None:
        from netmon.models import Sensor

        self._counts.pop(sensor_id, None)
        self.tracker.forget((sensor_id, 0))
        changed: list[Event] = []
        shown: list[Event] = []
        async with self.rt.sessionmaker() as session:
            sensor = (
                await session.execute(
                    select(Sensor).where(Sensor.id == sensor_id).with_for_update()
                )
            ).scalar_one_or_none()
            rows = (
                await session.execute(
                    select(Event)
                    .where(
                        Event.sensor_id == sensor_id,
                        Event.kind == "sensor_flapping",
                        Event.flap_end.is_(None),
                    )
                    .with_for_update()
                )
            ).scalars()
            for row in rows:
                row.flap_end = end
                if sensor is not None:
                    row.new_status = sensor.status
                    row.message = flap_end_message(
                        sensor.name, int(row.flap_count or 0), sensor.status
                    )
                changed.append(row)
            if sensor is not None:
                shown = await self._reconcile(session, sensor)
            await session.commit()
        for ev in changed:
            self.rt.hub.broadcast("event.updated", event_out(ev))
        for ev in shown:
            self.rt.hub.broadcast("event.created", event_out(ev))

    async def _reconcile(self, session: Any, sensor: Any) -> list[Event]:
        """The log must end in the sensor's real status: when the last shown change does not
        lead to it, show the last hidden change."""
        last_shown = (
            await session.execute(
                select(Event.new_status)
                .where(
                    Event.sensor_id == sensor.id,
                    Event.kind == "status_change",
                    Event.noise.is_(None),
                )
                .order_by(Event.time.desc(), Event.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if last_shown == sensor.status:
            return []
        hidden = (
            await session.execute(
                select(Event)
                .where(
                    Event.sensor_id == sensor.id,
                    Event.kind == "status_change",
                    Event.noise.in_(("flapping", "debounced")),
                    Event.new_status == sensor.status,
                )
                .order_by(Event.time.desc(), Event.id.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if hidden is None:
            return []
        hidden.noise = None
        hidden.noise_until = None
        return [hidden]

    def is_flapping(self, sensor_id: int) -> bool:
        return self.flap and self.tracker.is_flapping((int(sensor_id), 0))
