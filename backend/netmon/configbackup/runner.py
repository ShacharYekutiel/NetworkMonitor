"""[3] Config backup runner (architecture § 13.2.4-13.2.6): daily staggered schedule, manual
and syslog/trap-triggered queue (debounced), global concurrency, one session per device,
versions on change, status, events, passive `config_backup` sensor conditions, retention.

A component of `rt.features` (feature `config_backup`); `rt.configbackup` is the instance."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from netmon.configbackup import credentials, drivers, normalize, service
from netmon.configbackup.session import HostKeyInfo, SessionError, run_session
from netmon.services import events as events_svc
from netmon.services import settings as settings_svc

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.configbackup")

SENSOR_TYPE = "config_backup"
OFF_MESSAGE = "Configuration backup is turned off"
IDLE_MESSAGE = "Configuration backup OK"
DEBOUNCE_S = 120.0
EVENT_WINDOW_S = 600.0
RETRY_AFTER_S = 900.0
RETRY_CODES = ("connect_timeout", "connect_refused", "prompt_timeout")
MAX_WAIT_S = 30.0
SENSOR_SYNC_S = 3600.0
LAST_SCHEDULE_KEY = "config_backup_last_schedule_at"
PRIORITY = {"manual": 0, "event": 1, "retry": 1, "schedule": 2}
LAST_MATCHED_EVERY = timedelta(minutes=1)


def next_daily(now: datetime, hhmm: str, tz: str | None) -> datetime:
    """Pure: the next instant (> now) at local time `hhmm` in `tz` (UTC when invalid)."""
    try:
        zone = ZoneInfo(tz or "UTC")
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("UTC")
    h, m = (int(x) for x in hhmm.split(":"))
    local = now.astimezone(zone)
    cand = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if cand <= local:
        cand = (local + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
    # re-localise (DST changes between today and tomorrow)
    cand = datetime(cand.year, cand.month, cand.day, h, m, tzinfo=zone)
    return cand.astimezone(now.tzinfo)


def stagger(device_ids: list[int], start: datetime, window_min: int) -> dict[int, datetime]:
    """Pure: due time per device, evenly over `window_min` in device-id order."""
    ids = sorted(device_ids)
    if not ids:
        return {}
    step = (window_min * 60.0) / len(ids) if window_min > 0 else 0.0
    return {d: start + timedelta(seconds=i * step) for i, d in enumerate(ids)}


def change_message(added: int, removed: int, version_no: int) -> str:
    return f"Configuration changed (+{added} −{removed} lines), version {version_no}"


class ConfigBackupRunner:
    features = ("config_backup",)

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.enabled = False
        self.cfg: dict[str, Any] = {}
        self._sched_due: dict[int, tuple[datetime, str]] = {}
        self._event_due: dict[int, datetime] = {}
        self._persist_due: set[int] = set()  # debounced due times not yet in config_backup_status
        self._last_event_run: dict[int, datetime] = {}
        self._queue: asyncio.PriorityQueue[tuple[int, int, int, str]] = asyncio.PriorityQueue()
        self._queued: dict[int, tuple[int, str]] = {}  # device -> (seq, trigger)
        self._running: dict[int, asyncio.Task[Any]] = {}
        self._rerun: dict[int, str] = {}
        self._seq = itertools.count()
        self._tasks: list[asyncio.Task[Any]] = []
        self._wake = asyncio.Event()
        self._sem = asyncio.Semaphore(4)
        self._matched: set[int] = set()
        self._warned_unreadable: set[int] = set()
        self.next_schedule_at: datetime | None = None
        self.last_schedule_at: datetime | None = None
        self._last_sensor_sync = 0.0
        self._pending_schedule_now = False

    # ================================================================ component interface
    async def start(self, snapshot: dict[str, bool]) -> None:
        if snapshot.get("config_backup"):
            await self._on(startup=True)

    async def stop(self) -> None:
        await self._stop_tasks()

    async def feature_changed(self, name: str, on: bool) -> None:
        if name != "config_backup":
            return
        if on:
            await self._on(startup=False)
        else:
            await self._off()

    # ================================================================ lifecycle
    async def _load_cfg(self) -> None:
        async with self.rt.sessionmaker() as session:
            values = await settings_svc.get_all(session)
            last = (
                await session.execute(
                    text("SELECT value FROM settings WHERE key = :k"), {"k": LAST_SCHEDULE_KEY}
                )
            ).scalar()
        self.cfg = values
        self.last_schedule_at = _parse_dt(last)
        self._sem = asyncio.Semaphore(int(values["config_backup_concurrency"]))

    async def _on(self, *, startup: bool) -> None:
        if self.enabled:
            return
        self.enabled = True
        await self._load_cfg()
        now = self.rt.clock.now()
        self.next_schedule_at = next_daily(
            now, self.cfg["config_backup_time"], self.cfg.get("timezone")
        )
        if self.last_schedule_at is not None and now - self.last_schedule_at > timedelta(hours=24):
            self._pending_schedule_now = True  # missed run (process or feature was down)
        if self.rt.event_sensors is not None:
            with contextlib.suppress(Exception):
                await self.rt.event_sensors.reload()  # config_backup trigger rules
        await self.sync_sensors(recompute=True)
        self._tasks = [
            asyncio.create_task(self._dispatch_loop(), name="netmon-configbackup-dispatch"),
            asyncio.create_task(self._worker_loop(), name="netmon-configbackup-worker"),
        ]

    async def _stop_tasks(self) -> None:
        self.enabled = False
        for t in self._tasks:
            t.cancel()
        running = list(self._running.values())
        for t in running:
            t.cancel()
        for t in [*self._tasks, *running]:
            with contextlib.suppress(BaseException):
                await t
        self._tasks = []
        self._running.clear()
        self._queued.clear()
        self._rerun.clear()
        self._sched_due.clear()
        self._event_due.clear()
        self._persist_due.clear()
        while not self._queue.empty():
            self._queue.get_nowait()

    async def _off(self) -> None:
        await self._stop_tasks()
        if self.rt.event_sensors is not None:
            await self.rt.event_sensors.remove_conditions(
                "source IN ('backup_failed','host_key','config_changed')",
                {"types": [SENSOR_TYPE]},
                OFF_MESSAGE,
            )

    async def settings_changed(self) -> None:
        if not self.enabled:
            return
        await self._load_cfg()
        self.next_schedule_at = next_daily(
            self.rt.clock.now(), self.cfg["config_backup_time"], self.cfg.get("timezone")
        )
        self._wake.set()

    # ================================================================ public API
    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def queued_count(self) -> int:
        return len(self._queued) + len(self._event_due) + len(self._sched_due)

    def mark_dirty(self) -> None:
        """Devices / groups changed: sync the passive sensors soon (new eligible devices)."""
        if self.enabled:
            self._last_sensor_sync = 0.0
            self._wake.set()

    def is_running(self, device_id: int) -> bool:
        return device_id in self._running

    def trigger(self, device_id: int, rule_ids: list[int] | None = None) -> None:
        """A config-change syslog/trap rule matched (sync; called from the ingest pipeline):
        debounce 120 s after the last trigger, at most one triggered run per 10 min."""
        if not self.enabled:
            return
        now = self.rt.clock.now()
        due = now + timedelta(seconds=DEBOUNCE_S)
        last = self._last_event_run.get(device_id)
        if last is not None:
            due = max(due, last + timedelta(seconds=EVENT_WINDOW_S))
        self._event_due[device_id] = due
        self._persist_due.add(device_id)  # written by the dispatcher (this method is sync)
        self._matched.update(rule_ids or [])
        self._wake.set()

    def run_now(self, device_ids: list[int], trigger: str = "manual") -> tuple[int, list[int]]:
        """Queue devices ahead of scheduled work. Returns (queued, merged-into-running)."""
        queued = 0
        merged: list[int] = []
        for did in device_ids:
            if did in self._running:
                self._rerun[did] = trigger
                merged.append(did)
                continue
            self._enqueue(did, trigger)
            queued += 1
        return queued, merged

    def _enqueue(self, device_id: int, trigger: str) -> None:
        old = self._queued.get(device_id)
        if old is not None and PRIORITY[old[1]] <= PRIORITY[trigger]:
            return  # already queued with an equal or better priority
        seq = next(self._seq)
        self._queued[device_id] = (seq, trigger)
        self._queue.put_nowait((PRIORITY[trigger], seq, device_id, trigger))

    # ================================================================ loops
    async def _dispatch_loop(self) -> None:
        while True:
            try:
                timeout = await self._dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Config backup dispatcher failed")
                timeout = MAX_WAIT_S
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

    async def _dispatch_once(self) -> float:
        now = self.rt.clock.now()
        if self._pending_schedule_now or (
            self.next_schedule_at is not None and now >= self.next_schedule_at
        ):
            self._pending_schedule_now = False
            await self.schedule_pass(now)
        if self._persist_due:
            await self._write_next_due()
        for did, due in list(self._event_due.items()):
            if due <= now:
                del self._event_due[did]
                self._enqueue(did, "event")
        for did, (due, trig) in list(self._sched_due.items()):
            if due <= now:
                del self._sched_due[did]
                self._enqueue(did, trig)
        if self._matched:
            await self._touch_rules(now)
        if time.monotonic() - self._last_sensor_sync > SENSOR_SYNC_S:
            await self.sync_sensors()
        dues = [d for d in self._event_due.values()] + [d for d, _ in self._sched_due.values()]
        if self.next_schedule_at is not None:
            dues.append(self.next_schedule_at)
        timeout = MAX_WAIT_S
        if dues:
            timeout = min(timeout, max(0.05, (min(dues) - now).total_seconds()))
        return timeout

    async def _write_next_due(self) -> None:
        """Persist the debounced (syslog/trap triggered) due time as `next_due_at` so the API
        shows it; the status write after the run stores the next remaining due time (or NULL)."""
        ids, self._persist_due = sorted(self._persist_due), set()
        dues = {did: self._next_due(did) for did in ids}
        try:
            async with self.rt.sessionmaker() as session:
                await session.execute(
                    text(
                        "INSERT INTO config_backup_status (device_id, next_due_at) "
                        "SELECT u.d, u.due FROM unnest(CAST(:ids AS integer[]), "
                        "CAST(:dues AS timestamptz[])) AS u(d, due) "
                        "WHERE EXISTS (SELECT 1 FROM devices WHERE id = u.d) "
                        "ON CONFLICT (device_id) DO UPDATE SET next_due_at = EXCLUDED.next_due_at"
                    ),
                    {"ids": list(dues), "dues": list(dues.values())},
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("Cannot store the next config backup due time: %s", exc)

    async def schedule_pass(self, now: datetime) -> int:
        """Queue every eligible device, staggered over the window."""
        async with self.rt.sessionmaker() as session:
            ctxs = await service.load_contexts(session, self.rt.secret_key, None, with_status=False)
        ids = [c.id for c in ctxs if c.eligible[0]]
        dues = stagger(ids, now, int(self.cfg.get("config_backup_window_min", 60)))
        for did, due in dues.items():
            cur = self._sched_due.get(did)
            if cur is None or cur[0] > due:
                self._sched_due[did] = (due, "schedule")
        self.last_schedule_at = now
        self.next_schedule_at = next_daily(
            now, self.cfg["config_backup_time"], self.cfg.get("timezone")
        )
        async with self.rt.sessionmaker() as session:
            await settings_svc.put(session, LAST_SCHEDULE_KEY, now.isoformat())
            if dues:
                await session.execute(
                    text(
                        "INSERT INTO config_backup_status (device_id, next_due_at) "
                        "SELECT * FROM unnest(CAST(:ids AS integer[]), "
                        "CAST(:dues AS timestamptz[])) ON CONFLICT (device_id) DO UPDATE "
                        "SET next_due_at = EXCLUDED.next_due_at"
                    ),
                    {"ids": list(dues), "dues": list(dues.values())},
                )
            await session.commit()
        log.info("Config backup schedule: %d device(s) queued", len(dues))
        self._wake.set()
        return len(dues)

    async def _worker_loop(self) -> None:
        while True:
            prio, seq, did, trig = await self._queue.get()
            cur = self._queued.get(did)
            if cur is None or cur[0] != seq:
                continue  # superseded
            del self._queued[did]
            if did in self._running:
                self._rerun[did] = trig
                continue
            await self._sem.acquire()
            sem = self._sem
            task = asyncio.create_task(self._run_one(did, trig, sem), name=f"netmon-backup-{did}")
            self._running[did] = task

    async def _run_one(self, device_id: int, trigger: str, sem: asyncio.Semaphore) -> None:
        try:
            if trigger == "event":
                self._last_event_run[device_id] = self.rt.clock.now()
            await self.attempt(device_id, trigger)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Config backup of device %s failed", device_id)
        finally:
            sem.release()
            self._running.pop(device_id, None)
            again = self._rerun.pop(device_id, None)
            if again is not None and self.enabled:
                self._enqueue(device_id, again)

    async def _touch_rules(self, now: datetime) -> None:
        ids, self._matched = sorted(self._matched), set()
        try:
            async with self.rt.sessionmaker() as session:
                await session.execute(
                    text(
                        "UPDATE event_rules SET last_matched_at = :now WHERE id = ANY(:ids) "
                        "AND (last_matched_at IS NULL OR last_matched_at < :cut)"
                    ),
                    {"now": now, "ids": ids, "cut": now - LAST_MATCHED_EVERY},
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            log.debug("Cannot update last_matched_at: %s", exc)

    # ================================================================ sensors
    async def sync_sensors(self, *, recompute: bool = False) -> int:
        """Create the passive `config_backup` sensor of every eligible device (opt-out
        respected); recompute existing ones (the OFF message goes away)."""
        self._last_sensor_sync = time.monotonic()
        es = self.rt.event_sensors
        if es is None:
            return 0
        async with self.rt.sessionmaker() as session:
            ctxs = await service.load_contexts(session, self.rt.secret_key, None, with_status=False)
            existing = set(
                (
                    await session.execute(
                        text("SELECT device_id FROM sensors WHERE type = :t"), {"t": SENSOR_TYPE}
                    )
                ).scalars()
            )
        n = 0
        for c in ctxs:
            if c.eligible[0] and c.id not in existing:
                n += int(await self._ensure_sensor(c.id))
        if recompute:
            await es.remove_conditions("false", {"types": [SENSOR_TYPE]}, IDLE_MESSAGE)
        return n

    async def _ensure_sensor(self, device_id: int) -> bool:
        es = self.rt.event_sensors
        from netmon.models import Device

        created: list[int] = []
        async with es._lock, self.rt.sessionmaker() as session:  # noqa: SLF001
            device = await session.get(Device, device_id)
            if device is None:
                return False
            sensor, was_created = await es.get_or_create(session, device, SENSOR_TYPE)
            await session.commit()
            if was_created and sensor is not None:
                created.append(sensor.id)
        for sid in created:
            self.rt.config_changed("sensor", sid, "created")
        return bool(created)

    # ================================================================ one attempt
    async def attempt(self, device_id: int, trigger: str) -> dict[str, Any] | None:
        """Run one backup attempt and record it. Returns the stored status row."""
        rt = self.rt
        async with rt.sessionmaker() as session:
            ctxs = await service.load_contexts(session, rt.secret_key, [device_id])
        if not ctxs:
            return None
        c = ctxs[0]
        ok, reason = c.eligible
        if not ok:
            if reason in ("no_credentials", "no_driver"):
                if c.creds_unreadable:
                    await self._warn_unreadable(c)
                return await self._record_failure(c, trigger, reason, _reason_text(reason), 0)
            return None
        assert c.ssh is not None and c.driver is not None
        driver = drivers.get(c.driver)
        if driver is None:
            return await self._record_failure(c, trigger, "no_driver", "Unknown driver", 0)
        try:
            creds = credentials.decrypt(c.ssh, rt.secret_key)
        except credentials.CredentialsUnreadable:
            await self._warn_unreadable(c)
            return await self._record_failure(
                c, trigger, "no_credentials", "The SSH credentials cannot be decrypted", 0
            )
        await self._ensure_sensor(c.id)
        self._broadcast(c, running=True)
        pin: dict[str, HostKeyInfo] = {}
        presented: dict[str, HostKeyInfo] = {}

        def check(info: HostKeyInfo) -> bool:
            decision = normalize.host_key_decision(c.host_key, info.fingerprint)
            if decision == "pin":
                pin["key"] = info
                return True
            if decision == "accept":
                return True
            presented["key"] = info
            return False

        t0 = time.monotonic()
        try:
            result = await run_session(
                c.host,
                creds,
                driver,
                check,
                total_timeout=float(self.cfg.get("config_backup_timeout_s", 120)),
            )
        except SessionError as exc:
            ms = int((time.monotonic() - t0) * 1000)
            if exc.code == "host_key_changed" and "key" in presented:
                await self._host_key_changed(c, presented["key"])
            return await self._record_failure(c, trigger, exc.code, exc.message, ms)
        except asyncio.CancelledError:
            ms = int((time.monotonic() - t0) * 1000)
            with contextlib.suppress(BaseException):
                await asyncio.shield(
                    self._record_failure(
                        c, trigger, "cancelled", "Cancelled", ms, result="cancelled"
                    )
                )
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Config backup session of %s failed", c.name)
            ms = int((time.monotonic() - t0) * 1000)
            return await self._record_failure(c, trigger, "internal", f"{type(exc).__name__}", ms)
        if "key" in pin:
            await self._pin(c.id, pin["key"])
        mask = bool(self.cfg.get("config_backup_mask_secrets", True))
        norm = await asyncio.to_thread(normalize.normalize, result.outputs, driver, mask)
        ms = int((time.monotonic() - t0) * 1000)
        try:
            return await self._store(c, driver.id, trigger, norm, mask, ms)
        except Exception as exc:  # noqa: BLE001
            log.exception("Storing the configuration of %s failed", c.name)
            return await self._record_failure(c, trigger, "internal", f"{type(exc).__name__}", ms)

    async def _store(
        self,
        c: service.DeviceCtx,
        driver_id: str,
        trigger: str,
        norm: normalize.NormalizedConfig,
        mask: bool,
        ms: int,
    ) -> dict[str, Any] | None:
        rt = self.rt
        now = rt.clock.now()
        async with rt.sessionmaker() as session:
            latest = (
                (
                    await session.execute(
                        text(
                            "SELECT id, sha256, content FROM config_versions "
                            "WHERE device_id = :d AND is_latest"
                        ),
                        {"d": c.id},
                    )
                )
                .mappings()
                .first()
            )
        if latest is not None and str(latest["sha256"]).strip() == norm.sha256:
            row = await self._write_status(
                c.id,
                driver=driver_id,
                last_attempt_at=now,
                last_result="unchanged",
                last_error=None,
                last_message=None,
                last_trigger=trigger,
                last_duration_ms=ms,
                last_success_at=now,
                last_unchanged_at=now,
                consecutive_failures=0,
                next_due_at=self._next_due(c.id),
            )
            await self._clear_failed(c.id)
            self._broadcast_status(c, row)
            return row
        initial = latest is None
        added = removed = 0
        if not initial:
            added, removed = await asyncio.to_thread(
                normalize.line_changes, str(latest["content"]), norm.text
            )
        evs = []
        async with rt.sessionmaker() as session:
            if latest is not None:
                await session.execute(
                    text("UPDATE config_versions SET is_latest = false WHERE id = :id"),
                    {"id": latest["id"]},
                )
            new = (
                (
                    await session.execute(
                        text(
                            "INSERT INTO config_versions (device_id, collected_at, trigger, "
                            "driver, sha256, content, masked, size_bytes, lines, added, removed, "
                            "initial, is_latest) VALUES (:d, :at, :tr, :drv, :sha, :content, "
                            ":masked, :size, :lines, :added, :removed, :initial, true) "
                            f"RETURNING {service.VERSION_COLUMNS}"
                        ),
                        {
                            "d": c.id,
                            "at": now,
                            "tr": trigger,
                            "drv": driver_id,
                            "sha": norm.sha256,
                            "content": norm.text,
                            "masked": mask,
                            "size": norm.size_bytes,
                            "lines": norm.lines,
                            "added": added,
                            "removed": removed,
                            "initial": initial,
                        },
                    )
                )
                .mappings()
                .one()
            )
            version_no = int(
                (
                    await session.execute(
                        text("SELECT count(*) FROM config_versions WHERE device_id = :d"),
                        {"d": c.id},
                    )
                ).scalar()
                or 1
            )
            sensor_id = (
                await session.execute(
                    text("SELECT id FROM sensors WHERE device_id = :d AND type = :t"),
                    {"d": c.id, "t": SENSOR_TYPE},
                )
            ).scalar()
            if not initial:
                evs.append(
                    await events_svc.add_event(
                        session,
                        "config_change",
                        change_message(added, removed, version_no),
                        time=now,
                        device_id=c.id,
                        group_id=c.group_id,
                        device_name=c.name,
                        sensor_id=sensor_id,
                        sensor_name="Configuration backup" if sensor_id else None,
                    )
                )
            await session.commit()
        row = await self._write_status(
            c.id,
            driver=driver_id,
            last_attempt_at=now,
            last_result="ok",
            last_error=None,
            last_message=None,
            last_trigger=trigger,
            last_duration_ms=ms,
            last_success_at=now,
            consecutive_failures=0,
            next_due_at=self._next_due(c.id),
            **({} if initial else {"last_change_at": now}),
        )
        if evs:
            events_svc.published(rt, evs)
        await self._clear_failed(c.id)
        if not initial and self.cfg.get("config_change_alert") and rt.event_sensors is not None:
            clear = int(self.cfg.get("config_change_clear_min", 60))
            await rt.event_sensors.set_condition(
                c.id,
                SENSOR_TYPE,
                source="config_changed",
                key="",
                status="warning",
                message=change_message(added, removed, version_no),
                expires_at=now + timedelta(minutes=clear),
            )
        c.versions = version_no
        self._broadcast_status(c, row)
        if not initial:
            rt.hub.broadcast(
                "config_backup.changed",
                {"device_id": c.id, "version": service.version_out(dict(new), version_no)},
            )
        return row

    def _next_due(self, device_id: int) -> datetime | None:
        cands = []
        if device_id in self._sched_due:
            cands.append(self._sched_due[device_id][0])
        if device_id in self._event_due:
            cands.append(self._event_due[device_id])
        return min(cands) if cands else None

    async def _record_failure(
        self,
        c: service.DeviceCtx,
        trigger: str,
        code: str,
        message: str,
        ms: int,
        *,
        result: str = "failed",
    ) -> dict[str, Any] | None:
        now = self.rt.clock.now()
        if result == "failed" and trigger == "schedule" and code in RETRY_CODES and self.enabled:
            self._sched_due[c.id] = (now + timedelta(seconds=RETRY_AFTER_S), "retry")
            self._wake.set()
        row = await self._write_status(
            c.id,
            driver=c.driver,
            last_attempt_at=now,
            last_result=result,
            last_error=code,
            last_message=message[:500],
            last_trigger=trigger,
            last_duration_ms=ms,
            next_due_at=self._next_due(c.id),
            inc_failures=result == "failed",
        )
        failures = int((row or {}).get("consecutive_failures") or 0)
        threshold = int(self.cfg.get("config_backup_fail_threshold", 2))
        es = self.rt.event_sensors
        if (
            result == "failed"
            and failures >= threshold
            and es is not None
            and code != "host_key_changed"
            and self.enabled
        ):
            await es.set_condition(
                c.id,
                SENSOR_TYPE,
                source="backup_failed",
                key="",
                status="warning",
                message=f"Configuration backup failed {failures} times: {code}: {message}"[:500],
            )
        self._broadcast_status(c, row)
        return row

    async def _write_status(self, device_id: int, **fields: Any) -> dict[str, Any] | None:
        inc = bool(fields.pop("inc_failures", False))
        if inc:
            fields.pop("consecutive_failures", None)
        cols = list(fields)
        params: dict[str, Any] = {f"v_{k}": v for k, v in fields.items()}
        params["d"] = device_id
        ins_cols = ["device_id", *cols]
        ins_vals = [":d", *(f":v_{k}" for k in cols)]
        sets = [f"{k} = EXCLUDED.{k}" for k in cols]
        if inc:
            ins_cols.append("consecutive_failures")
            ins_vals.append("1")
            sets.append("consecutive_failures = config_backup_status.consecutive_failures + 1")
        set_sql = ", ".join(sets) or "device_id = EXCLUDED.device_id"
        sql = (
            f"INSERT INTO config_backup_status ({', '.join(ins_cols)}) "  # noqa: S608
            f"VALUES ({', '.join(ins_vals)}) ON CONFLICT (device_id) DO UPDATE SET {set_sql} "
            "RETURNING *"
        )
        try:
            async with self.rt.sessionmaker() as session:
                row = (await session.execute(text(sql), params)).mappings().first()
                await session.commit()
            return dict(row) if row else None
        except Exception as exc:  # noqa: BLE001 - device deleted meanwhile, DB down
            log.warning("Cannot store the backup status of device %s: %s", device_id, exc)
            return None

    async def _clear_failed(self, device_id: int) -> None:
        es = self.rt.event_sensors
        if es is not None:
            await es.remove_device_conditions(device_id, SENSOR_TYPE, ["backup_failed"])

    async def _warn_unreadable(self, c: service.DeviceCtx) -> None:
        if c.id in self._warned_unreadable:
            return
        self._warned_unreadable.add(c.id)
        await events_svc.write_system_event(
            self.rt,
            f"SSH credentials of {c.name} cannot be decrypted (secret.key changed?); "
            "enter them again",
            device_id=c.id,
            device_name=c.name,
            group_id=c.group_id,
        )

    # ================================================================ host keys
    async def _pin(self, device_id: int, info: HostKeyInfo) -> None:
        async with self.rt.sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO ssh_host_keys (device_id, algorithm, fingerprint, public_key, "
                    "state, pinned_at) VALUES (:d, :a, :f, :k, 'pinned', :now) "
                    "ON CONFLICT (device_id) DO NOTHING"
                ),
                {
                    "d": device_id,
                    "a": info.algorithm[:64],
                    "f": info.fingerprint[:64],
                    "k": info.public_key,
                    "now": self.rt.clock.now(),
                },
            )
            await session.commit()

    async def _host_key_changed(self, c: service.DeviceCtx, info: HostKeyInfo) -> None:
        now = self.rt.clock.now()
        old = c.host_key or {}
        async with self.rt.sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE ssh_host_keys SET state = 'changed', presented_algorithm = :a, "
                    "presented_fingerprint = :f, presented_public_key = :k, "
                    "changed_at = CASE WHEN state = 'changed' THEN changed_at ELSE :now END "
                    "WHERE device_id = :d"
                ),
                {
                    "d": c.id,
                    "a": info.algorithm[:64],
                    "f": info.fingerprint[:64],
                    "k": info.public_key,
                    "now": now,
                },
            )
            await session.commit()
        first = old.get("state") != "changed"
        if first:
            await events_svc.write_system_event(
                self.rt,
                f"SSH host key of {c.name} changed: pinned {old.get('algorithm')} "
                f"{old.get('fingerprint')}, presented {info.algorithm} {info.fingerprint}",
                device_id=c.id,
                device_name=c.name,
                group_id=c.group_id,
            )
        es = self.rt.event_sensors
        if es is not None:
            await es.set_condition(
                c.id,
                SENSOR_TYPE,
                source="host_key",
                key="",
                status="down",
                message=f"SSH host key changed (presented {info.fingerprint})",
            )

    async def host_key_resolved(self, device_id: int) -> None:
        """Accept / reset: drop the `host_key` condition."""
        es = self.rt.event_sensors
        if es is not None:
            await es.remove_device_conditions(device_id, SENSOR_TYPE, ["host_key"])

    # ================================================================ status / WS
    def _broadcast(self, c: service.DeviceCtx, *, running: bool) -> None:
        self.rt.hub.broadcast(
            "config_backup.update",
            {"device_id": c.id, "status": service.status_out(c, running)},
        )

    def _broadcast_status(self, c: service.DeviceCtx, row: dict[str, Any] | None) -> None:
        if row is not None:
            c.status = row
        self._broadcast(c, running=False)
        self.rt.config_changed("config_backup", c.id, "updated")


def _reason_text(reason: str) -> str:
    return {
        "no_credentials": "No SSH credentials for this device",
        "no_driver": "No config backup driver matches this device",
    }.get(reason, reason)


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ================================================================ retention (hourly loop)
async def run_retention(rt: Any, batch_devices: int = 500) -> int:
    """Drop versions beyond max_versions / older than retention_days, always keeping the
    latest and the newest min_versions (runs also while the feature is OFF)."""
    async with rt.sessionmaker() as session:
        cfg = await settings_svc.get_all(session)
        rows = (
            await session.execute(
                text(
                    "SELECT device_id, array_agg(id ORDER BY collected_at DESC, id DESC) AS ids, "
                    "array_agg(collected_at ORDER BY collected_at DESC, id DESC) AS ats "
                    "FROM config_versions GROUP BY device_id "
                    "HAVING count(*) > :min OR min(collected_at) < :cut"
                ),
                {
                    "min": int(cfg["config_backup_min_versions"]),
                    "cut": rt.clock.now()
                    - timedelta(days=int(cfg["config_backup_retention_days"])),
                },
            )
        ).all()
    now = rt.clock.now()
    expired: list[int] = []
    for r in rows:
        expired += normalize.select_expired(
            list(zip(r.ids, r.ats, strict=False)),
            now,
            int(cfg["config_backup_max_versions"]),
            int(cfg["config_backup_retention_days"]),
            int(cfg["config_backup_min_versions"]),
        )
    for i in range(0, len(expired), 1000):
        async with rt.sessionmaker() as session:
            await session.execute(
                text("DELETE FROM config_versions WHERE id = ANY(:ids) AND NOT is_latest"),
                {"ids": expired[i : i + 1000]},
            )
            await session.commit()
    return len(expired)
