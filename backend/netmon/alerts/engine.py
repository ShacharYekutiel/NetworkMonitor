"""AlertEngine: match status-change events to alert rules and send notifications."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from netmon.alerts import templates
from netmon.alerts.email import SmtpError, send_email
from netmon.alerts.telegram import TelegramError, send_telegram
from netmon.alerts.webhook import WebhookError, send_webhook
from netmon.services import events as events_svc
from netmon.services import settings as settings_svc

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.alerts")


def should_fire(old_status: str | None, new_status: str | None) -> bool:
    """Whether a transition can fire at all. An `up` additionally needs a previously *sent*
    down/warning notification of the same rule for the sensor (checked per rule)."""
    if new_status not in ("down", "warning", "up"):
        return False
    if new_status == old_status:
        return False
    return True


def build_payload(
    event: dict[str, Any] | None,
    sensor: dict[str, Any] | None,
    device: dict[str, Any] | None,
    group: dict[str, Any] | None,
    rule: dict[str, Any] | None,
    public_base_url: str | None,
    test_time: str | None = None,
) -> dict[str, Any]:
    if event is None:
        return {
            "event": "test",
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
    return {
        "event": "sensor.status_changed",
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
    """Raises SmtpError / WebhookError / TelegramError."""
    if channel["type"] == "telegram":
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


class AlertEngine:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._last_change: dict[int, int] = {}
        self._timers: set[asyncio.Task[None]] = set()
        self._sent: OrderedDict[tuple[int, int], None] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        # test hook: called with (channel, payload) instead of real sending when set
        self.sender = send_to_channel

    def on_status_change(self, event: dict[str, Any]) -> None:
        sid = event.get("sensor_id")
        if sid is None:
            return
        self._last_change[sid] = event["id"]
        if not should_fire(event.get("old_status"), event.get("new_status")):
            return
        self._spawn(self._handle(event), self._tasks)

    def _spawn(self, coro: Any, bucket: set[asyncio.Task[None]]) -> None:
        task = asyncio.ensure_future(coro)
        bucket.add(task)
        task.add_done_callback(bucket.discard)

    async def stop(self) -> None:
        for t in list(self._timers):
            t.cancel()
        for t in list(self._timers) + list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    async def idle(self) -> None:
        """Wait until no immediate sends are in flight (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    @property
    def pending_timers(self) -> int:
        return len(self._timers)

    async def _handle(self, event: dict[str, Any]) -> None:
        try:
            async with self.rt.sessionmaker() as session:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT r.id, r.name, r.delay_s,
                                   COALESCE(array_agg(rc.channel_id ORDER BY rc.channel_id)
                                       FILTER (WHERE rc.channel_id IS NOT NULL), '{}') AS chans
                            FROM alert_rules r
                            LEFT JOIN alert_rule_channels rc ON rc.rule_id = r.id
                            WHERE r.enabled AND (r.sensor_id IS NULL OR r.sensor_id = :sid)
                              AND :st = ANY(r.on_statuses)
                            GROUP BY r.id ORDER BY r.sensor_id NULLS LAST, r.id
                            """
                        ),
                        {"sid": event["sensor_id"], "st": event["new_status"]},
                    )
                ).all()
        except Exception:  # noqa: BLE001
            log.exception("Cannot load alert rules")
            return
        if event["new_status"] == "up":
            rows = [r for r in rows if r.id in await self._notified_rules(event["sensor_id"])]
        immediate: list[tuple[dict[str, Any], list[int]]] = []
        for r in rows:
            rule = {"id": r.id, "name": r.name}
            if r.delay_s > 0:
                self._spawn(self._delayed(event, rule, list(r.chans), r.delay_s), self._timers)
            else:
                immediate.append((rule, list(r.chans)))
        for rule, chans in immediate:
            await self._fire(event, rule, chans)

    async def _delayed(
        self, event: dict[str, Any], rule: dict[str, Any], chans: list[int], delay: int
    ) -> None:
        await asyncio.sleep(delay)
        if self._last_change.get(event["sensor_id"]) != event["id"]:
            return
        async with self.rt.sessionmaker() as session:
            status = (
                await session.execute(
                    text("SELECT status FROM sensors WHERE id = :id"), {"id": event["sensor_id"]}
                )
            ).scalar_one_or_none()
            # the rule may have been disabled, deleted or changed while we waited
            current = (
                await session.execute(
                    text(
                        """
                        SELECT r.enabled, r.on_statuses, r.sensor_id,
                               COALESCE(array_agg(rc.channel_id ORDER BY rc.channel_id)
                                   FILTER (WHERE rc.channel_id IS NOT NULL), '{}') AS chans
                        FROM alert_rules r
                        LEFT JOIN alert_rule_channels rc ON rc.rule_id = r.id
                        WHERE r.id = :id GROUP BY r.id
                        """
                    ),
                    {"id": rule["id"]},
                )
            ).first()
        if status != event["new_status"]:
            return
        if (
            current is None
            or not current.enabled
            or event["new_status"] not in current.on_statuses
            or current.sensor_id not in (None, event["sensor_id"])
        ):
            return
        if event["new_status"] == "up" and rule["id"] not in await self._notified_rules(
            event["sensor_id"]
        ):
            return
        await self._fire(event, rule, list(current.chans))

    async def _notified_rules(self, sensor_id: int) -> set[int]:
        async with self.rt.sessionmaker() as session:
            rows = await session.execute(
                text("SELECT rule_id FROM alert_notified WHERE sensor_id = :s"), {"s": sensor_id}
            )
            return {r.rule_id for r in rows}

    async def _record_sent(self, rule_id: int, event: dict[str, Any]) -> None:
        try:
            async with self.rt.sessionmaker() as session:
                if event["new_status"] == "up":
                    await session.execute(
                        text("DELETE FROM alert_notified WHERE rule_id = :r AND sensor_id = :s"),
                        {"r": rule_id, "s": event["sensor_id"]},
                    )
                else:
                    await session.execute(
                        text(
                            """
                            INSERT INTO alert_notified (rule_id, sensor_id, status, time)
                            VALUES (:r, :s, :st, now())
                            ON CONFLICT (rule_id, sensor_id)
                            DO UPDATE SET status = EXCLUDED.status, time = now()
                            """
                        ),
                        {"r": rule_id, "s": event["sensor_id"], "st": event["new_status"]},
                    )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - e.g. sensor or rule deleted meanwhile
            log.debug("Cannot record notification state: %s", exc)

    async def _fire(self, event: dict[str, Any], rule: dict[str, Any], chans: list[int]) -> None:
        todo = []
        for cid in chans:
            key = (event["id"], cid)
            if key in self._sent:
                continue
            self._sent[key] = None
            todo.append(cid)
        while len(self._sent) > 20000:
            self._sent.popitem(last=False)
        if not todo:
            # the channels were already served by another rule for this event
            await self._record_sent(rule["id"], event)
            return
        async with self.rt.sessionmaker() as session:
            ctx = (
                await session.execute(
                    text(
                        """
                        SELECT s.id AS sid, s.name AS sname, s.type AS stype,
                               d.id AS did, d.name AS dname, d.host AS dhost,
                               g.id AS gid, g.name AS gname
                        FROM sensors s JOIN devices d ON d.id = s.device_id
                        JOIN groups g ON g.id = d.group_id WHERE s.id = :sid
                        """
                    ),
                    {"sid": event["sensor_id"]},
                )
            ).first()
            channels = (
                await session.execute(
                    text(
                        "SELECT id, name, type, config, enabled FROM notification_channels "
                        "WHERE id = ANY(:ids) AND enabled"
                    ),
                    {"ids": todo},
                )
            ).all()
            cfg = await settings_svc.get_all(session)
        if ctx is None:
            sensor = {"id": event["sensor_id"], "name": event["sensor_name"], "type": None}
            device = {"id": event["device_id"], "name": event["device_name"], "host": None}
            group = None
        else:
            sensor = {"id": ctx.sid, "name": ctx.sname, "type": ctx.stype}
            device = {"id": ctx.did, "name": ctx.dname, "host": ctx.dhost}
            group = {"id": ctx.gid, "name": ctx.gname}
        payload = build_payload(event, sensor, device, group, rule, cfg["public_base_url"])
        sent_any = False
        for ch in channels:
            channel = {"id": ch.id, "name": ch.name, "type": ch.type, "config": ch.config}
            try:
                await self.sender(channel, payload, cfg["smtp"], cfg["default_language"])
                sent_any = True
            except (SmtpError, WebhookError, TelegramError, Exception) as exc:  # noqa: BLE001
                log.warning("Notification via %s failed: %s", ch.name, exc)
                await events_svc.write_notification_event(
                    self.rt,
                    f"{ch.name}: {exc}"[:1000],
                    sensor_id=event.get("sensor_id"),
                    device_id=event.get("device_id"),
                    group_id=event.get("group_id"),
                    sensor_name=event.get("sensor_name"),
                    device_name=event.get("device_name"),
                )
        if sent_any:
            await self._record_sent(rule["id"], event)
