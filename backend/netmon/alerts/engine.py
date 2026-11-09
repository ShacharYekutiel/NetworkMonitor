"""AlertEngine: persisted alert state (`alert_state`), problem / recovery / escalation /
reminder notifications.

[2b-1] The event path only upserts `alert_state` rows and wakes the loop; one loop sends every
due notification (docs/data-model.md "Alert state, escalation, reminders, ack"). No in-memory
timers: the state survives restarts. Time comes from `rt.clock`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from netmon.alerts import templates
from netmon.alerts.email import send_email
from netmon.alerts.telegram import send_telegram
from netmon.alerts.webhook import send_webhook
from netmon.services import events as events_svc
from netmon.services import settings as settings_svc
from netmon.services.serialize import iso, now_utc

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.alerts")

PROBLEM = ("down", "warning")
LOOP_MAX_S = 30.0
BATCH = 500
SENDING_STALE_S = 600  # an in-flight marker older than this is ignored
MAX_PARALLEL_SENDS = 16
STAGE_EVENTS = {
    "problem": "sensor.status_changed",
    "recovery": "sensor.status_changed",
    "escalation": "sensor.escalation",
    "reminder": "sensor.reminder",
}


def should_fire(old_status: str | None, new_status: str | None) -> bool:
    """Whether a transition can open/change/recover a problem at all (Phase 1 helper; an `up`
    additionally needs a previously notified problem, see alert_state)."""
    if new_status not in ("down", "warning", "up"):
        return False
    return new_status != old_status


def build_payload(
    event: dict[str, Any] | None,
    sensor: dict[str, Any] | None,
    device: dict[str, Any] | None,
    group: dict[str, Any] | None,
    rule: dict[str, Any] | None,
    public_base_url: str | None,
    test_time: str | None = None,
    *,
    stage: str | None = None,
    after_maintenance: bool = False,
    problem_since: str | None = None,
    after_flapping: bool = False,
) -> dict[str, Any]:
    if event is None:
        return {
            "event": "test",
            "stage": None,
            "after_maintenance": False,
            "after_flapping": False,
            "problem_since": None,
            "event_id": None,
            "time": test_time,
            "old_status": None,
            "new_status": None,
            "message": "Test notification",
            "sensor": None,
            "device": None,
            "group": None,
            "rule": None,
            "url": None,
        }
    url = None
    if public_base_url and event.get("sensor_id") is not None:
        url = f"{public_base_url.rstrip('/')}/sensors/{event['sensor_id']}"
    stage = stage or ("recovery" if event.get("new_status") == "up" else "problem")
    return {
        "event": STAGE_EVENTS.get(stage, "sensor.status_changed"),
        "stage": stage,
        "after_maintenance": bool(after_maintenance),
        "after_flapping": bool(after_flapping),  # [2b-3]
        "problem_since": problem_since,
        "event_id": event["id"],
        "time": event["time"],
        "old_status": event["old_status"],
        "new_status": event["new_status"],
        "message": event["message"],
        "sensor": sensor,
        "device": device,
        "group": group,
        "rule": rule,
        "url": url,
    }


async def send_to_channel(
    channel: dict[str, Any], payload: dict[str, Any], smtp: dict[str, Any], lang: str
) -> None:
    """Raises SmtpError / WebhookError / TelegramError / TeamsError."""
    if channel["type"] == "teams":  # [2b-3]
        from netmon.alerts import teams

        await teams.send(channel["config"], payload, lang)
    elif channel["type"] == "telegram":
        cfg = channel["config"]
        parse_mode = cfg.get("parse_mode", "HTML")
        await send_telegram(cfg, templates.telegram_text(payload, lang, parse_mode), parse_mode)
    elif channel["type"] == "email":
        await send_email(
            smtp,
            list(channel["config"].get("to") or []),
            templates.subject(payload, lang),
            templates.text_body(payload, lang),
            templates.html_body(payload, lang),
        )
    else:
        await send_webhook(channel["config"], payload)


@dataclass
class _Send:
    stage: str
    channels: list[int]
    payload: dict[str, Any]
    keys: dict[int, tuple[Any, ...]] = field(default_factory=dict)
    ok: list[int] = field(default_factory=list)


@dataclass
class _Plan:
    rule_id: int
    sensor_id: int
    updates: dict[str, Any] = field(default_factory=dict)
    delete: bool = False
    sends: list[_Send] = field(default_factory=list)
    event_id: int | None = None
    delete_after: bool = False


class AlertEngine:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._sent: OrderedDict[tuple[Any, ...], bool] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._failures = 0
        self._passes_in_flight = 0
        self._no_passes = asyncio.Event()
        self._no_passes.set()
        # test hook: called with (channel, payload, smtp, lang) instead of real sending
        self.sender = send_to_channel

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop(), name="netmon-alerts")

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(BaseException):
                await self._loop_task
            self._loop_task = None
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    def notify(self) -> None:
        self._wake.set()

    async def idle(self) -> None:
        """Wait until the event path is idle, then process everything due now (tests)."""
        while pending := [t for t in self._tasks if not t.done()]:
            # only pending tasks: awaiting already-finished ones does not yield to the loop,
            # so their done-callbacks (which empty the set) would never run
            await asyncio.gather(*pending, return_exceptions=True)
        await self.run_once()
        while self._passes_in_flight:  # e.g. the background loop sending right now
            await self._no_passes.wait()
            await self.run_once()  # its results may have made more rows due

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------------------------------------------------------------- event path
    def on_status_change(self, event: dict[str, Any], time: datetime | None = None) -> None:
        if event.get("sensor_id") is None:
            return
        if event.get("new_status") == event.get("old_status"):
            return
        self._spawn(self._handle(event, time))

    async def _handle(self, event: dict[str, Any], time: datetime | None) -> None:
        from netmon.services import dependencies

        t = time or _parse(event.get("time")) or now_utc()
        try:
            async with self._lock:
                async with self.rt.sessionmaker() as session:
                    min_due = None
                    if event.get("new_status") == "down" and event.get("device_id") is not None:
                        # a child's DOWN waits until its parent has been re-checked
                        min_due = await dependencies.parent_hold_until(
                            session, event["device_id"], t
                        )
                    await apply_event(session, event, t, min_due=min_due)
                    await session.commit()
        except Exception:  # noqa: BLE001 - e.g. the sensor was deleted meanwhile
            log.exception("Cannot update alert state for event %s", event.get("id"))
        self.notify()

    def request_acknowledged(self, sensor_id: int) -> None:
        """Non-blocking for request handlers (processed in the background)."""
        self._spawn(self.acknowledged(sensor_id))

    def request_unacknowledged(self, sensor_id: int) -> None:
        self._spawn(self.unacknowledged(sensor_id))

    def request_left_maintenance(self) -> None:
        self._spawn(self.left_maintenance())

    async def acknowledged(self, sensor_id: int) -> None:
        async with self._lock:
            async with self.rt.sessionmaker() as session:
                await session.execute(
                    text(
                        "UPDATE alert_state SET due_at = NULL, escalation_due_at = NULL, "
                        "next_reminder_at = NULL, updated_at = now() "
                        "WHERE sensor_id = :s AND status IN ('down','warning')"
                    ),
                    {"s": sensor_id},
                )
                await session.commit()

    async def unacknowledged(self, sensor_id: int) -> None:
        now = self.rt.clock.now()
        async with self._lock:
            async with self.rt.sessionmaker() as session:
                # reminders resume; a problem notification that the ack prevented (delay not
                # over yet) is sent now; an escalation that never happened restarts from now
                await session.execute(
                    text(
                        """
                        UPDATE alert_state s SET
                            next_reminder_at = CASE WHEN r.repeat_every_s IS NOT NULL
                                THEN CAST(:now AS timestamptz)
                                     + make_interval(secs => r.repeat_every_s)
                                ELSE s.next_reminder_at END,
                            due_at = CASE WHEN s.primary_sent_at IS NULL
                                THEN CAST(:now AS timestamptz) ELSE s.due_at END,
                            escalation_due_at = CASE
                                WHEN s.escalated_at IS NULL AND r.escalation_after_s IS NOT NULL
                                     AND cardinality(r.escalation_channel_ids) > 0
                                THEN CAST(:now AS timestamptz)
                                     + make_interval(secs => r.escalation_after_s)
                                ELSE s.escalation_due_at END,
                            updated_at = now()
                        FROM alert_rules r
                        WHERE r.id = s.rule_id AND s.sensor_id = :s
                          AND s.status IN ('down','warning')
                        """
                    ),
                    {"s": sensor_id, "now": now},
                )
                await session.commit()
        self.notify()

    async def left_maintenance(self) -> None:
        """Rows whose notification was suppressed by maintenance and whose device is no longer
        in maintenance: re-arm the problem notification (sent once, `after_maintenance`) and
        restart the escalation timer from now."""
        maint = getattr(self.rt, "maintenance", None)
        now = self.rt.clock.now()
        async with self._lock:
            async with self.rt.sessionmaker() as session:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT s.rule_id, s.sensor_id, s.due_at, s.escalated_at,
                                   s.escalation_due_at, s.primary_sent_at,
                                   se.status AS sensor_status, se.ack_at, se.device_id,
                                   r.enabled, r.on_statuses, r.sensor_id AS rule_sensor_id,
                                   r.escalation_after_s, r.escalation_channel_ids
                            FROM alert_state s JOIN sensors se ON se.id = s.sensor_id
                            JOIN alert_rules r ON r.id = s.rule_id
                            WHERE s.maintenance_suppressed AND s.status IN ('down','warning')
                            FOR UPDATE OF s
                            """
                        )
                    )
                ).all()
                n = 0
                for r in rows:
                    if maint is not None and maint.in_maintenance(r.device_id):
                        continue
                    active = _active(r.enabled, r.on_statuses, r.rule_sensor_id, r)
                    if active and r.ack_at is None:
                        esc = None
                        if (
                            r.escalation_after_s
                            and list(r.escalation_channel_ids or [])
                            and r.escalated_at is None
                            and r.escalation_due_at is None
                        ):
                            esc = now + timedelta(seconds=int(r.escalation_after_s))
                        # the problem was already sent before the window: no second DOWN
                        resend = r.primary_sent_at is None
                        due = r.due_at if r.due_at is not None else (now if resend else None)
                        await session.execute(
                            text(
                                "UPDATE alert_state SET due_at = :due, "
                                "escalation_due_at = COALESCE(:esc, escalation_due_at), "
                                "maintenance_suppressed = :keep, "
                                "updated_at = now() WHERE rule_id = :r AND sensor_id = :s"
                            ),
                            {
                                "due": due,
                                "esc": esc,
                                "keep": resend,  # the marker for `after_maintenance`
                                "r": r.rule_id,
                                "s": r.sensor_id,
                            },
                        )
                    else:
                        await session.execute(
                            text(
                                "UPDATE alert_state SET maintenance_suppressed = false, "
                                "updated_at = now() WHERE rule_id = :r AND sensor_id = :s"
                            ),
                            {"r": r.rule_id, "s": r.sensor_id},
                        )
                    n += 1
                await session.commit()
        if n:
            self.notify()

    async def left_flapping(self, sensor_ids: list[int] | None, all_rows: bool = False) -> None:
        """[2b-3] A port stopped flapping (or the feature was turned off): its suppressed rows
        that are still active and not acknowledged are re-armed once, like leaving
        maintenance (problem sent once with `after_flapping`, escalation restarted)."""
        if not all_rows and not sensor_ids:
            return
        now = self.rt.clock.now()
        maint = getattr(self.rt, "maintenance", None)
        cond = "" if all_rows else " AND s.sensor_id = ANY(:ids)"
        async with self._lock:
            async with self.rt.sessionmaker() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT s.rule_id, s.sensor_id, s.due_at, s.escalated_at, "
                            "s.escalation_due_at, s.primary_sent_at, se.status AS sensor_status, "
                            "se.ack_at, se.device_id, r.enabled, r.on_statuses, "
                            "r.sensor_id AS rule_sensor_id, r.escalation_after_s, "
                            "r.escalation_channel_ids FROM alert_state s "
                            "JOIN sensors se ON se.id = s.sensor_id "
                            "JOIN alert_rules r ON r.id = s.rule_id "
                            "WHERE s.flap_suppressed AND s.status IN ('down','warning')"
                            f"{cond} FOR UPDATE OF s"
                        ),
                        {} if all_rows else {"ids": list(sensor_ids or [])},
                    )
                ).all()
                n = 0
                for r in rows:
                    key = {"r": r.rule_id, "s": r.sensor_id}
                    active = _active(r.enabled, r.on_statuses, r.rule_sensor_id, r)
                    in_maint = bool(maint is not None and maint.in_maintenance(r.device_id))
                    if active and r.ack_at is None and not in_maint:
                        esc = None
                        if (
                            r.escalation_after_s
                            and list(r.escalation_channel_ids or [])
                            and r.escalated_at is None
                        ):
                            esc = now + timedelta(seconds=int(r.escalation_after_s))
                        resend = r.primary_sent_at is None
                        due = r.due_at if r.due_at is not None else (now if resend else None)
                        await session.execute(
                            text(
                                "UPDATE alert_state SET due_at = :due, "
                                "escalation_due_at = COALESCE(:esc, escalation_due_at), "
                                "flap_suppressed = :keep, updated_at = now() "
                                "WHERE rule_id = :r AND sensor_id = :s"
                            ),
                            {**key, "due": due, "esc": esc, "keep": resend},
                        )
                    else:
                        await session.execute(
                            text(
                                "UPDATE alert_state SET flap_suppressed = false, "
                                "updated_at = now() WHERE rule_id = :r AND sensor_id = :s"
                            ),
                            key,
                        )
                    n += 1
                await session.commit()
        if n:
            self.notify()

    # ---------------------------------------------------------------- loop
    async def recover_inflight(self) -> int:
        """Startup: rows whose send was interrupted by the previous process (marker still
        set) are re-armed, so every notification is delivered at least once."""
        now = self.rt.clock.now()
        async with self._lock:
            async with self.rt.sessionmaker() as session:
                res = await session.execute(
                    text(
                        """
                        UPDATE alert_state SET
                            due_at = CASE
                                WHEN status IN ('down','warning') AND primary_sent_at IS NULL
                                     OR status = 'up'
                                THEN COALESCE(due_at, :now) ELSE due_at END,
                            escalation_due_at = CASE
                                WHEN escalated_at IS NOT NULL AND escalated_at >= sending_since
                                THEN :now ELSE escalation_due_at END,
                            escalated_at = CASE
                                WHEN escalated_at IS NOT NULL AND escalated_at >= sending_since
                                THEN NULL ELSE escalated_at END,
                            sending_since = NULL, updated_at = now()
                        WHERE sending_since IS NOT NULL
                        """
                    ),
                    {"now": now},
                )
                await session.commit()
        n = res.rowcount or 0
        if n:
            log.warning("Re-armed %d notification(s) interrupted by a restart", n)
        return n

    async def _loop(self) -> None:
        try:
            await self.recover_inflight()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Cannot re-arm interrupted notifications")
        while True:
            backoff = 0.0
            try:
                await self.run_once()
                if self._failures:
                    log.info("Alert loop recovered after %d failed iteration(s)", self._failures)
                self._failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self._failures += 1
                if self._failures == 1:
                    log.exception("Alert loop iteration failed; retrying with backoff")
                else:
                    log.debug("Alert loop iteration failed again", exc_info=True)
                backoff = min(LOOP_MAX_S, 0.5 * 2 ** min(self._failures, 6))
            timeout = LOOP_MAX_S
            try:
                nxt = await self._next_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                nxt = None
            if nxt is not None:
                # never spin: something overdue that could not be processed waits a bit
                timeout = min(timeout, max(0.05, (nxt - self.rt.clock.now()).total_seconds()))
            if backoff:
                timeout = backoff
            changed = self.rt.clock.changed
            if not self._wake.is_set():
                waiters = [
                    asyncio.ensure_future(self._wake.wait()),
                    asyncio.ensure_future(changed.wait()),
                ]
                try:
                    await asyncio.wait(
                        waiters, timeout=timeout + 0.01, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for w in waiters:
                        w.cancel()
            self._wake.clear()

    async def _next_due(self) -> datetime | None:
        async with self.rt.sessionmaker() as session:
            return (
                await session.execute(
                    text(
                        "SELECT least(min(due_at), min(escalation_due_at), "
                        "min(next_reminder_at)) FROM alert_state"
                    )
                )
            ).scalar()

    async def run_once(self) -> int:
        """Process every due row once. Returns the number of rows handled."""
        total = 0
        while True:
            n = await self._pass()
            total += n
            if n < BATCH:
                break
        return total

    async def _pass(self) -> int:
        """Claim and plan the due rows and apply the state changes under the lock, send
        outside it (rows are marked in flight), then record the results under the lock."""
        maint = getattr(self.rt, "maintenance", None)
        self._pass_begin()  # from the claim on: idle() waits for it
        try:
            async with self._lock:
                claimed = await self._claim(maint)
        except BaseException:
            self._pass_end()
            raise
        if claimed is None:
            self._pass_end()
            return 0
        plans, rows, channels, cfg, now = claimed
        try:
            await self._send_all(plans, channels, cfg, rows)
        finally:
            sent_events: list[Any] = []
            try:
                async with self._lock:
                    async with self.rt.sessionmaker() as session:
                        for p in plans:
                            await _apply_results(session, p, now)
                        sent_events = await _notification_sent_events(
                            session, plans, rows, channels, now
                        )
                        await session.commit()
                if sent_events:
                    events_svc.published(self.rt, sent_events)
            finally:
                self._pass_end()
        return len(rows)

    def _pass_begin(self) -> None:
        self._passes_in_flight += 1
        self._no_passes.clear()

    def _pass_end(self) -> None:
        self._passes_in_flight -= 1
        if self._passes_in_flight <= 0:
            self._passes_in_flight = 0
            self._no_passes.set()

    async def _claim(self, maint: Any) -> Any:
        now = self.rt.clock.now()
        # never decide "not in maintenance" before the tracker evaluated once: until then
        # only recoveries are processed (the other rows stay due)
        ready = maint is not None and bool(getattr(maint, "initialized", False))
        async with self.rt.sessionmaker() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT s.*, r.name AS rule_name, r.enabled AS rule_enabled,
                               r.on_statuses, r.sensor_id AS rule_sensor_id, r.delay_s,
                               r.escalation_after_s, r.escalation_channel_ids, r.repeat_every_s,
                               COALESCE((SELECT array_agg(rc.channel_id ORDER BY rc.channel_id)
                                         FROM alert_rule_channels rc
                                         WHERE rc.rule_id = r.id), '{}') AS rule_channels,
                               se.name AS sname, se.type AS stype, se.status AS sensor_status,
                               se.status_message, se.ack_at, se.device_id,
                               d.name AS dname, d.host AS dhost, g.id AS gid, g.name AS gname
                        FROM alert_state s
                        JOIN alert_rules r ON r.id = s.rule_id
                        JOIN sensors se ON se.id = s.sensor_id
                        JOIN devices d ON d.id = se.device_id
                        JOIN groups g ON g.id = d.group_id
                        WHERE (s.due_at <= :now OR s.escalation_due_at <= :now
                               OR s.next_reminder_at <= :now)
                          AND (s.sending_since IS NULL OR s.sending_since < :stale)
                          AND (:ready OR (s.status = 'up' AND s.due_at <= :now))
                        ORDER BY r.sensor_id NULLS LAST, s.rule_id, s.sensor_id
                        LIMIT :lim
                        FOR UPDATE OF s SKIP LOCKED
                        """
                    ),
                    {
                        "now": now,
                        "lim": BATCH,
                        "ready": ready,
                        "stale": now - timedelta(seconds=SENDING_STALE_S),
                    },
                )
            ).all()
            if not rows:
                return None
            event_ids = [r.event_id for r in rows if r.event_id is not None]
            events: dict[int, dict[str, Any]] = {}
            if event_ids:
                for e in await session.execute(
                    text(
                        "SELECT id, time, sensor_id, device_id, group_id, sensor_name, "
                        "device_name, old_status, new_status, message FROM events "
                        "WHERE id = ANY(:ids)"
                    ),
                    {"ids": event_ids},
                ):
                    events[e.id] = {
                        "id": e.id,
                        "time": iso(e.time),
                        "sensor_id": e.sensor_id,
                        "device_id": e.device_id,
                        "group_id": e.group_id,
                        "sensor_name": e.sensor_name,
                        "device_name": e.device_name,
                        "old_status": e.old_status,
                        "new_status": e.new_status,
                        "message": e.message,
                    }
            cfg = await settings_svc.get_all(session)
            flapping = getattr(self.rt, "flapping", None)
            flap_ids = set(flapping.suppressed_sensors) if flapping is not None else set()
            plans = [
                self._plan(
                    r,
                    now,
                    events,
                    cfg,
                    bool(maint and maint.in_maintenance(r.device_id)),
                    r.sensor_id in flap_ids,
                )
                for r in rows
            ]
            chan_ids = sorted({c for p in plans for s in p.sends for c in s.channels})
            channels: dict[int, dict[str, Any]] = {}
            if chan_ids:
                for ch in await session.execute(
                    text(
                        "SELECT id, name, type, config FROM notification_channels "
                        "WHERE id = ANY(:ids) AND enabled"
                    ),
                    {"ids": chan_ids},
                ):
                    channels[ch.id] = {
                        "id": ch.id,
                        "name": ch.name,
                        "type": ch.type,
                        "config": dict(ch.config),
                    }
            for p in plans:
                if p.sends:
                    p.updates["sending_since"] = now  # in flight: counts as notified
                await _apply_state(session, p)
            await session.commit()
        return plans, rows, channels, cfg, now

    def _plan(
        self,
        r: Any,
        now: datetime,
        events: dict[int, dict[str, Any]],
        cfg: dict[str, Any],
        in_maint: bool,
        in_flap: bool = False,
    ) -> _Plan:
        p = _Plan(r.rule_id, r.sensor_id, event_id=r.event_id)
        active = _active(r.rule_enabled, r.on_statuses, r.rule_sensor_id, r)
        acked = r.ack_at is not None
        notified = list(r.notified_channel_ids or [])
        rule_chans = list(r.rule_channels or [])
        ctx = {
            "sensor": {"id": r.sensor_id, "name": r.sname, "type": r.stype},
            "device": {"id": r.device_id, "name": r.dname, "host": r.dhost},
            "group": {"id": r.gid, "name": r.gname},
            "rule": {"id": r.rule_id, "name": r.rule_name},
        }
        event = events.get(r.event_id) if r.event_id is not None else None
        if event is None:
            event = {
                "id": r.event_id,
                "time": iso(r.problem_since),
                "sensor_id": r.sensor_id,
                "old_status": None,
                "new_status": r.status if r.status in ("down", "warning", "up") else None,
                "message": r.status_message or "",
            }

        def payload(stage: str, after: bool = False, flap: bool = False) -> dict[str, Any]:
            return build_payload(
                event,
                ctx["sensor"],
                ctx["device"],
                ctx["group"],
                ctx["rule"],
                cfg.get("public_base_url"),
                stage=stage,
                after_maintenance=after,
                problem_since=iso(r.problem_since),
                after_flapping=flap,
            )

        def due(v: datetime | None) -> bool:
            return v is not None and v <= now

        if r.status in PROBLEM and due(r.due_at):
            p.updates["due_at"] = None
            if not active:
                if not notified:
                    p.delete = True
            elif acked:
                pass
            elif in_maint:
                p.updates["maintenance_suppressed"] = True
            elif in_flap:  # [2b-3] a flapping port: like maintenance, re-armed when it ends
                p.updates["flap_suppressed"] = True
            else:
                after = bool(r.maintenance_suppressed)
                flap = bool(getattr(r, "flap_suppressed", False))
                stage_key = "problem:after" if after else ("problem:flap" if flap else "problem")
                p.sends.append(_Send(stage_key, rule_chans, payload("problem", after, flap)))
                p.updates["maintenance_suppressed"] = False
                if flap:
                    p.updates["flap_suppressed"] = False
                if r.repeat_every_s:
                    p.updates["next_reminder_at"] = now + timedelta(seconds=int(r.repeat_every_s))
        elif r.status == "up" and due(r.due_at):
            if r.sensor_status == "up":
                p.sends.append(_Send("recovery", notified, payload("recovery")))
                p.updates["due_at"] = None
                p.delete_after = True  # deleted once sent (at-least-once across restarts)
            else:
                p.updates["due_at"] = None
        if p.delete or p.delete_after:
            return p
        if r.status not in PROBLEM:
            # defensive: no escalation/reminder outside a problem (and no stale due_at)
            for col in ("due_at", "escalation_due_at", "next_reminder_at"):
                if due(getattr(r, col)):
                    p.updates[col] = None
            return p
        if due(r.escalation_due_at):
            p.updates["escalation_due_at"] = None
            esc_chans = list(r.escalation_channel_ids or [])
            if active and not acked and r.escalation_after_s and esc_chans:
                if in_flap and not in_maint:
                    p.updates["flap_suppressed"] = True
                elif in_maint:
                    p.updates["maintenance_suppressed"] = True
                else:
                    p.sends.append(_Send("escalation", esc_chans, payload("escalation")))
                    p.updates["escalated_at"] = now
        if due(r.next_reminder_at):
            if active and not acked and r.repeat_every_s:
                if not in_maint and not in_flap:
                    stage = f"reminder:{int(r.reminders_sent) + 1}"
                    p.sends.append(_Send(stage, rule_chans, payload("reminder")))
                p.updates["next_reminder_at"] = now + timedelta(seconds=int(r.repeat_every_s))
            else:
                p.updates["next_reminder_at"] = None
        return p

    async def _send_all(
        self,
        plans: list[_Plan],
        channels: dict[int, dict[str, Any]],
        cfg: dict[str, Any],
        rows: list[Any],
    ) -> None:
        jobs: dict[tuple[Any, ...], tuple[dict[str, Any], dict[str, Any], Any]] = {}
        for p, r in zip(plans, rows, strict=True):
            for s in p.sends:
                ref = r.event_id if r.event_id is not None else iso(r.problem_since)
                for cid in s.channels:
                    if cid not in channels:
                        continue
                    key = (p.sensor_id, ref, s.stage, cid)
                    s.keys[cid] = key
                    if key in self._sent or key in jobs:
                        continue
                    jobs[key] = (channels[cid], s.payload, r)
        results: dict[tuple[Any, ...], bool] = {}
        if jobs:
            sem = asyncio.Semaphore(MAX_PARALLEL_SENDS)
            token = templates.DISPLAY_TZ.set(str(cfg.get("timezone") or "UTC"))
            try:

                async def one(key: tuple[Any, ...], job: Any) -> None:
                    channel, payload, r = job
                    async with sem:
                        results[key] = await self._send_one(channel, payload, cfg, r)

                await asyncio.gather(*(one(k, j) for k, j in jobs.items()))
            finally:
                templates.DISPLAY_TZ.reset(token)
            for key, ok in results.items():
                self._sent[key] = ok
            while len(self._sent) > 20000:
                self._sent.popitem(last=False)
        for p in plans:
            for s in p.sends:
                s.ok = [cid for cid, key in s.keys.items() if self._sent.get(key, False)]

    async def _send_one(
        self, channel: dict[str, Any], payload: dict[str, Any], cfg: dict[str, Any], r: Any
    ) -> bool:
        try:
            await self.sender(channel, payload, cfg["smtp"], cfg["default_language"])
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Notification via %s failed: %s", channel["name"], exc)
            await events_svc.write_notification_event(
                self.rt,
                f"{channel['name']}: {exc}"[:1000],
                sensor_id=r.sensor_id,
                device_id=r.device_id,
                group_id=r.gid,
                sensor_name=r.sname,
                device_name=r.dname,
                time=self.rt.clock.now(),
            )
            return False


STAGE_LABELS = {"escalation": "ESCALATION", "reminder": "REMINDER", "recovery": "RECOVERED"}


async def _notification_sent_events(
    session: Any,
    plans: list[_Plan],
    rows: list[Any],
    channels: dict[int, dict[str, Any]],
    now: datetime,
) -> list[Any]:
    """[2b-3] One `notification_sent` event per successful send stage per rule (the device
    timeline needs them)."""
    out = []
    for p, r in zip(plans, rows, strict=True):
        for snd in p.sends:
            if not snd.ok:
                continue
            stage = snd.stage.split(":", 1)[0]
            payload = snd.payload
            label = STAGE_LABELS.get(stage) or str(payload.get("new_status") or "").upper()
            names = ", ".join(channels[c]["name"] for c in snd.ok if c in channels)
            msg = f'Sent {label} to {names} (rule "{r.rule_name}")'
            out.append(
                await events_svc.add_event(
                    session,
                    "notification_sent",
                    msg[:1000],
                    time=now,
                    sensor_id=r.sensor_id,
                    device_id=r.device_id,
                    group_id=r.gid,
                    sensor_name=r.sname,
                    device_name=r.dname,
                    old_status=payload.get("old_status"),
                    new_status=payload.get("new_status"),
                )
            )
    return out


def _active(enabled: bool, on_statuses: Any, rule_sensor_id: int | None, r: Any) -> bool:
    """Still active: the rule is enabled and matches the sensor, and the sensor's current
    status is down/warning and in the rule's statuses."""
    return (
        bool(enabled)
        and rule_sensor_id in (None, r.sensor_id)
        and r.sensor_status in PROBLEM
        and r.sensor_status in list(on_statuses or [])
    )


async def _apply_state(session: Any, p: _Plan) -> None:
    """Before sending (under the lock): the planned state changes (due columns, deletes,
    escalated_at, reminder count). The send results are recorded by _apply_results."""
    key = {"r": p.rule_id, "s": p.sensor_id}
    if p.delete:
        await session.execute(
            text("DELETE FROM alert_state WHERE rule_id = :r AND sensor_id = :s"), key
        )
        return
    sets = dict(p.updates)
    reminders = sum(1 for s in p.sends if s.stage.startswith("reminder"))
    clauses = [f"{k} = :{k}" for k in sets]
    params: dict[str, Any] = {**key, **sets}
    if reminders:
        clauses.append("reminders_sent = reminders_sent + :rem")
        params["rem"] = reminders
    if not clauses:
        return
    clauses.append("updated_at = now()")
    await session.execute(
        text(
            f"UPDATE alert_state SET {', '.join(clauses)} "  # noqa: S608 - fixed column names
            "WHERE rule_id = :r AND sensor_id = :s"
        ),
        params,
    )


async def _apply_results(session: Any, p: _Plan, now: datetime) -> None:
    """After sending: successful channels join notified_channel_ids (the row may have moved
    on meanwhile, e.g. to `up`: its recovery then reaches these channels too)."""
    if p.delete:
        return
    key = {"r": p.rule_id, "s": p.sensor_id}
    if p.delete_after:
        # the recovery was sent; unless a new problem replaced the row meanwhile
        await session.execute(
            text(
                "DELETE FROM alert_state WHERE rule_id = :r AND sensor_id = :s "
                "AND status = 'up' AND event_id IS NOT DISTINCT FROM CAST(:eid AS bigint)"
            ),
            {**key, "eid": p.event_id},
        )
        await session.execute(
            text(
                "UPDATE alert_state SET sending_since = NULL WHERE rule_id = :r AND sensor_id = :s"
            ),
            key,
        )
        return
    added: list[int] = []
    primary = False
    for s in p.sends:
        if not s.ok:
            continue
        added += s.ok
        primary = primary or s.stage.startswith("problem")
    if not p.sends:
        return
    clauses = ["sending_since = NULL"]
    params: dict[str, Any] = {**key, "added": sorted(set(added))}
    if added:
        clauses.append(
            "notified_channel_ids = ARRAY(SELECT DISTINCT x FROM "
            "unnest(notified_channel_ids || CAST(:added AS integer[])) x ORDER BY x)"
        )
    if primary:
        # only for the same problem (a new problem may have replaced the row meanwhile)
        clauses.append(
            "primary_sent_at = CASE WHEN event_id IS NOT DISTINCT FROM CAST(:eid AS bigint) "
            "THEN COALESCE(primary_sent_at, :now) ELSE primary_sent_at END"
        )
        params["now"] = now
        params["eid"] = p.event_id
    if not clauses:
        return
    clauses.append("updated_at = now()")
    await session.execute(
        text(
            f"UPDATE alert_state SET {', '.join(clauses)} "  # noqa: S608 - fixed column names
            "WHERE rule_id = :r AND sensor_id = :s"
        ),
        params,
    )


async def apply_event(
    session: Any,
    event: dict[str, Any],
    t: datetime,
    *,
    min_due: datetime | None = None,
) -> None:
    """The `status_change` table of docs/data-model.md: upsert/suspend/delete the sensor's
    alert_state rows for event E (sensor X, new status S, time t).

    A row whose notification is being sent right now (`sending_since`, younger than
    SENDING_STALE_S) counts as notified. `min_due`: a new problem is not due before this
    (a child waiting for its parent's re-check)."""
    sid = event["sensor_id"]
    status = event["new_status"]
    eid = event.get("id")
    rows = {
        r.rule_id: r
        for r in (
            await session.execute(
                text(
                    "SELECT s.rule_id, s.status, cardinality(s.notified_channel_ids) AS n, "
                    "s.escalated_at, s.escalation_due_at, s.problem_since, s.sending_since, "
                    "r.enabled, r.on_statuses, r.delay_s, r.escalation_after_s, "
                    "r.escalation_channel_ids FROM alert_state s "
                    "JOIN alert_rules r ON r.id = s.rule_id WHERE s.sensor_id = :s "
                    "FOR UPDATE OF s"
                ),
                {"s": sid},
            )
        ).all()
    }
    if status in PROBLEM:
        rules = (
            await session.execute(
                text(
                    "SELECT id, delay_s, escalation_after_s, escalation_channel_ids "
                    "FROM alert_rules WHERE enabled AND (sensor_id IS NULL OR sensor_id = :s) "
                    "AND :st = ANY(on_statuses)"
                ),
                {"s": sid, "st": status},
            )
        ).all()
        matching = set()
        for rule in rules:
            matching.add(rule.id)
            due = t + timedelta(seconds=int(rule.delay_s or 0))
            row = rows.get(rule.id)
            if row is None or row.status not in PROBLEM:
                if min_due is not None and due < min_due:
                    due = min_due
                esc = None
                if rule.escalation_after_s and list(rule.escalation_channel_ids or []):
                    esc = t + timedelta(seconds=int(rule.escalation_after_s))
                await session.execute(
                    text(
                        """
                        INSERT INTO alert_state (rule_id, sensor_id, status, event_id,
                            problem_since, due_at, escalation_due_at)
                        VALUES (:r, :s, :st, :e, :t, :due, :esc)
                        ON CONFLICT (rule_id, sensor_id) DO UPDATE SET
                            status = EXCLUDED.status, event_id = EXCLUDED.event_id,
                            problem_since = EXCLUDED.problem_since, due_at = EXCLUDED.due_at,
                            primary_sent_at = NULL,
                            escalation_due_at = EXCLUDED.escalation_due_at,
                            escalated_at = NULL, next_reminder_at = NULL, reminders_sent = 0,
                            maintenance_suppressed = false, updated_at = now()
                        """
                    ),
                    {
                        "r": rule.id,
                        "s": sid,
                        "st": status,
                        "e": eid,
                        "t": t,
                        "due": due,
                        "esc": esc,
                    },
                )
            else:
                # change within the problem; an escalation lost while the status did not
                # match the rule is re-armed (measured from problem_since)
                esc = None
                if (
                    row.escalated_at is None
                    and row.escalation_due_at is None
                    and rule.escalation_after_s
                    and list(rule.escalation_channel_ids or [])
                ):
                    esc = max(
                        row.problem_since + timedelta(seconds=int(rule.escalation_after_s)), t
                    )
                await session.execute(
                    text(
                        "UPDATE alert_state SET status = :st, event_id = :e, due_at = :due, "
                        "escalation_due_at = COALESCE(:esc, escalation_due_at), "
                        "updated_at = now() WHERE rule_id = :r AND sensor_id = :s"
                    ),
                    {"r": rule.id, "s": sid, "st": status, "e": eid, "due": due, "esc": esc},
                )
        for rid in rows:
            if rid not in matching:
                await session.execute(
                    text(
                        "UPDATE alert_state SET due_at = NULL, updated_at = now() "
                        "WHERE rule_id = :r AND sensor_id = :s"
                    ),
                    {"r": rid, "s": sid},
                )
        return
    for rid, row in rows.items():
        key = {"r": rid, "s": sid}
        sending = row.sending_since is not None and row.sending_since > t - timedelta(
            seconds=SENDING_STALE_S
        )
        if not row.n and not sending:
            await session.execute(
                text("DELETE FROM alert_state WHERE rule_id = :r AND sensor_id = :s"), key
            )
        elif status == "up":
            if row.enabled and "up" in list(row.on_statuses or []):
                await session.execute(
                    text(
                        # problem_since keeps the start of the problem (recovery payloads)
                        "UPDATE alert_state SET status = 'up', event_id = :e, "
                        "due_at = :due, escalation_due_at = NULL, next_reminder_at = NULL, "
                        "maintenance_suppressed = false, updated_at = now() "
                        "WHERE rule_id = :r AND sensor_id = :s"
                    ),
                    {
                        **key,
                        "e": eid,
                        "due": t + timedelta(seconds=int(row.delay_s or 0)),
                    },
                )
            else:
                await session.execute(
                    text("DELETE FROM alert_state WHERE rule_id = :r AND sensor_id = :s"), key
                )
        else:  # unreachable / paused / unknown
            await session.execute(
                text(
                    "UPDATE alert_state SET status = 'suspended', due_at = NULL, "
                    "escalation_due_at = NULL, next_reminder_at = NULL, updated_at = now() "
                    "WHERE rule_id = :r AND sensor_id = :s"
                ),
                key,
            )


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
