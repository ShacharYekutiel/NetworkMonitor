"""Process-wide runtime: DB engine, poller, alert engine, WS hub, maintenance tasks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from netmon import __version__
from netmon.alerts.engine import AlertEngine
from netmon.config import Settings
from netmon.db import timescale
from netmon.db.engine import make_engine, make_sessionmaker, wait_for_db
from netmon.db.migrate import run_migrations
from netmon.poller.scheduler import Scheduler
from netmon.realtime.hub import Hub
from netmon.secrets import load_or_create_secret
from netmon.services import events as events_svc
from netmon.services import retention
from netmon.services import settings as settings_svc
from netmon.services.status import load_inventory

log = logging.getLogger("netmon.runtime")

ADVISORY_LOCK_ID = 727001


class LockHeldError(RuntimeError):
    pass


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.version = __version__
        self.secret_key: str = ""
        self.engine: AsyncEngine | None = None
        self.sessionmaker: async_sessionmaker[AsyncSession] = None  # type: ignore[assignment]
        self.timescale_enabled = False
        self.hub = Hub()
        self.scheduler: Scheduler | None = None
        self.alerts: AlertEngine | None = None
        self.semaphore = asyncio.Semaphore(max(1, settings.netmon_max_concurrent_checks))
        self.started_monotonic = time.monotonic()
        self._lock_conn: AsyncConnection | None = None
        self._has_users = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._rollup_event = asyncio.Event()
        self._rollup_snapshot: dict[tuple[str, int], tuple[str, tuple[int, ...]]] | None = None
        self._bg: set[asyncio.Task[Any]] = set()
        self.started = False

    # ------------------------------------------------------------------ lifecycle
    async def start(
        self,
        *,
        run_poller: bool = True,
        take_lock: bool = True,
        migrate: bool = True,
        maintenance: bool = True,
    ) -> None:
        s = self.settings
        s.data_dir.mkdir(parents=True, exist_ok=True)
        self.secret_key = load_or_create_secret(s.data_dir)
        self.engine = make_engine(s.async_database_url)
        self.sessionmaker = make_sessionmaker(self.engine)
        await wait_for_db(self.engine)
        if take_lock:
            await self._acquire_lock()
        if migrate:
            await run_migrations(self.engine)
        async with self.sessionmaker() as session:
            retention_days = await settings_svc.get(session, "retention_days")
        self.timescale_enabled = await timescale.bootstrap(
            self.engine, int(retention_days), disabled=s.netmon_disable_timescale
        )
        await self._mark_stale_sensors()
        self.alerts = AlertEngine(self)
        self._tasks.append(asyncio.create_task(self._rollup_loop(), name="netmon-rollup"))
        if maintenance:
            self._tasks.append(asyncio.create_task(retention.loop(self), name="netmon-retention"))
        if run_poller:
            self.scheduler = Scheduler(self)
            await self.scheduler.start()
        await events_svc.write_system_event(self, "NetworkMonitor started")
        if not self.timescale_enabled:
            await events_svc.write_system_event(
                self, "TimescaleDB not available — using plain PostgreSQL"
            )
        self.started = True
        log.info("NetworkMonitor %s started (timescale=%s)", self.version, self.timescale_enabled)

    async def stop(self) -> None:
        if self.scheduler:
            await self.scheduler.stop()
        if self.alerts:
            await self.alerts.stop()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        for t in list(self._bg):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(t, 5)
        await self.hub.close_all(1001)
        if self._lock_conn is not None:
            with contextlib.suppress(Exception):
                await self._lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:id)"), {"id": ADVISORY_LOCK_ID}
                )
                await self._lock_conn.close()
            self._lock_conn = None
        if self.engine is not None:
            await self.engine.dispose()
        self.started = False

    async def _acquire_lock(self) -> None:
        assert self.engine is not None
        conn = await self.engine.connect()
        got = (
            await conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": ADVISORY_LOCK_ID})
        ).scalar()
        await conn.commit()
        if not got:
            await conn.close()
            raise LockHeldError(
                "Another NetworkMonitor instance is already running against this database"
            )
        self._lock_conn = conn

    async def _mark_stale_sensors(self) -> None:
        from sqlalchemy import select

        from netmon.models import Device, Sensor
        from netmon.services.sensors import set_sensor_status

        async with self.sessionmaker() as session:
            rows = (
                await session.execute(
                    select(Sensor, Device)
                    .join(Device, Device.id == Sensor.device_id)
                    .where(
                        Sensor.enabled.is_(True),
                        Device.enabled.is_(True),
                        Sensor.status != "unknown",
                        text(
                            "(sensors.last_check_at IS NULL OR sensors.last_check_at < "
                            "now() - make_interval(secs => sensors.interval_s * 3))"
                        ),
                    )
                )
            ).all()
            evs = []
            for sensor, device in rows:
                ev = await set_sensor_status(session, sensor, device, "unknown")
                if ev:
                    evs.append(ev)
            await session.commit()
        if evs:
            events_svc.published(self, evs)

    # ------------------------------------------------------------------ helpers
    def spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def drain(self) -> None:
        """Wait for background work (tests / shutdown)."""
        while self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)
        if self.alerts is not None:
            await self.alerts.idle()

    async def has_users(self) -> bool:
        if self._has_users:
            return True
        async with self.sessionmaker() as session:
            n = (await session.execute(text("SELECT EXISTS (SELECT 1 FROM users)"))).scalar()
        self._has_users = bool(n)
        return self._has_users

    def mark_has_users(self) -> None:
        self._has_users = True

    def uptime_s(self) -> int:
        return int(time.monotonic() - self.started_monotonic)

    # ------------------------------------------------------------------ rollup
    def request_rollup(self) -> None:
        self._rollup_event.set()

    async def _rollup_loop(self) -> None:
        with contextlib.suppress(Exception):
            await self.publish_rollup_changes()
        while True:
            await self._rollup_event.wait()
            await asyncio.sleep(0.2)
            self._rollup_event.clear()
            try:
                await self.publish_rollup_changes()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Rollup update failed")

    async def publish_rollup_changes(self) -> None:
        async with self.sessionmaker() as session:
            inv = await load_inventory(session)
        snap = inv.snapshot()
        prev = self._rollup_snapshot
        self._rollup_snapshot = snap
        if prev is None:
            return
        for (kind, id_), value in snap.items():
            if prev.get((kind, id_)) == value:
                continue
            counts = inv.device_counts[id_] if kind == "device" else inv.group_counts[id_]
            self.hub.broadcast(
                f"{kind}.status",
                {"id": id_, "status": value[0], "status_counts": dict(counts)},
            )

    def config_changed(self, entity: str, id_: int, action: str) -> None:
        self.hub.broadcast("config.changed", {"entity": entity, "id": id_, "action": action})
        self.request_rollup()
