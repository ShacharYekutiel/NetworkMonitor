"""Process-wide runtime: DB engine, poller, alert engine, WS hub, maintenance tasks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from netmon import __version__
from netmon.alerts.engine import AlertEngine
from netmon.clock import Clock
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


def raise_nofile_limit(target: int = 65536) -> tuple[int, int] | None:
    """POSIX: raise the soft RLIMIT_NOFILE to the hard limit capped at `target`, best effort
    (discovery opens many sockets). Returns (old_soft, new_soft), or None when not applicable."""
    try:
        import resource
    except ImportError:  # Windows
        return None
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = target if hard == resource.RLIM_INFINITY else min(hard, target)
        if soft != resource.RLIM_INFINITY and soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            log.info("Raised the open-file limit from %d to %d", soft, want)
            return soft, want
        return soft, soft
    except (ValueError, OSError) as exc:
        log.debug("Cannot raise the open-file limit: %s", exc)
        return None


class LockHeldError(RuntimeError):
    pass


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        from netmon.alerts import telegram

        telegram.configure(settings.netmon_telegram_api_base)
        self.version = __version__
        self.secret_key: str = ""
        self.engine: AsyncEngine | None = None
        self.sessionmaker: async_sessionmaker[AsyncSession] = None  # type: ignore[assignment]
        self.timescale_enabled = False
        self.retention_policy_ok = False
        self.hub = Hub()
        self.scheduler: Scheduler | None = None
        self.alerts: AlertEngine | None = None
        self.discovery: Any = None  # netmon.discovery.runner.ScanRunner
        self.discovery_scheduler: Any = None
        # [2b-1]
        self.clock = Clock()
        self.maintenance: Any = None  # netmon.services.maintenance.MaintenanceTracker
        self.topology: Any = None  # netmon.topology.refresh.TopologyRunner
        self.topology_scheduler: Any = None
        self.dependencies_enabled = True
        self.semaphore = asyncio.Semaphore(max(1, settings.netmon_max_concurrent_checks))
        self.started_monotonic = time.monotonic()
        self._lock_conn: AsyncConnection | None = None
        self._has_users = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._rollup_event = asyncio.Event()
        self._rollup_snapshot: dict[tuple[str, int], tuple[str, tuple[int, ...]]] | None = None
        self._bg: set[asyncio.Task[Any]] = set()
        self.started = False
        self.on_lock_lost: Any = self._on_lock_lost

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
        raise_nofile_limit()
        s.data_dir.mkdir(parents=True, exist_ok=True)
        self.secret_key = load_or_create_secret(s.data_dir)
        self.engine = make_engine(s.async_database_url)
        self.sessionmaker = make_sessionmaker(self.engine)
        await wait_for_db(self.engine)
        if take_lock:
            await self._acquire_lock()
            self._tasks.append(asyncio.create_task(self._lock_watch_loop(), name="netmon-lock"))
        if migrate:
            await run_migrations(self.engine)
        async with self.sessionmaker() as session:
            retention_days = await settings_svc.get(session, "retention_days")
        ts = await timescale.bootstrap_status(
            self.engine, int(retention_days), disabled=s.netmon_disable_timescale
        )
        self.timescale_enabled = ts.enabled
        self.retention_policy_ok = ts.retention_policy
        from netmon.topology.refresh import TopologyRunner

        self.topology = TopologyRunner(self)
        n = await self.topology.mark_interrupted()
        if n:
            log.warning("Marked %d interrupted topology refresh(es) as failed", n)
        await self._mark_stale_sensors()
        self.alerts = AlertEngine(self)
        from netmon.discovery.runner import ScanRunner
        from netmon.discovery.scheduler import DiscoveryScheduler

        self.discovery = ScanRunner(self)
        n = await self.discovery.mark_interrupted()
        if n:
            log.warning("Marked %d interrupted discovery scan(s) as failed", n)
        if maintenance:
            self.discovery_scheduler = DiscoveryScheduler(self)
            self.discovery_scheduler.start()
        # [2b-1] the alert loop processes overdue alert_state rows right away, then the
        # maintenance tracker, then the topology scheduler
        self.alerts.start()
        from netmon.services.maintenance import MaintenanceTracker

        self.maintenance = MaintenanceTracker(self)
        await self.maintenance.start()
        if maintenance:
            from netmon.topology.scheduler import TopologyScheduler

            self.topology_scheduler = TopologyScheduler(self)
            await self.topology_scheduler.start()
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
        if self.topology_scheduler is not None:
            await self.topology_scheduler.stop()
            self.topology_scheduler = None
        if self.topology is not None:
            with contextlib.suppress(Exception):
                await self.topology.shutdown()
        if self.maintenance is not None:
            await self.maintenance.stop()
        if self.discovery_scheduler is not None:
            await self.discovery_scheduler.stop()
            self.discovery_scheduler = None
        if self.discovery is not None:
            with contextlib.suppress(Exception):
                await self.discovery.shutdown()
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

    def _on_lock_lost(self) -> None:
        """Another instance took over the database: stop hard (exit code 3)."""
        logging.shutdown()
        os._exit(3)

    async def check_lock(self) -> bool:
        """Verify we still hold the advisory lock (it is lost when PostgreSQL restarts);
        re-acquire it, or exit when another instance has taken it."""
        held = False
        if self._lock_conn is not None:
            try:
                held = bool(
                    (
                        await self._lock_conn.execute(
                            text(
                                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                                "AND classid = 0 AND objid = :id AND objsubid = 1 "
                                "AND pid = pg_backend_pid() AND granted"
                            ),
                            {"id": ADVISORY_LOCK_ID},
                        )
                    ).scalar()
                )
                await self._lock_conn.commit()
            except Exception:  # noqa: BLE001
                held = False
        if held:
            return True
        log.error("Lost the single-instance advisory lock (database restart?); re-acquiring")
        if self._lock_conn is not None:
            with contextlib.suppress(Exception):
                await self._lock_conn.close()
            self._lock_conn = None
        try:
            await self._acquire_lock()
        except LockHeldError:
            log.critical(
                "Another NetworkMonitor instance now holds the database lock; shutting down"
            )
            self.on_lock_lost()
            return False
        except Exception as exc:  # noqa: BLE001
            log.error("Cannot re-acquire the advisory lock yet: %s", exc)
            return False
        log.warning("Advisory lock re-acquired")
        return True

    async def _lock_watch_loop(self, interval_s: float = 30.0) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.check_lock()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Lock check failed")

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
                        # down/warning keep their status so alerting stays consistent
                        # (no repeated DOWN, and a later recovery still sends UP)
                        Sensor.status == "up",
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
        while pending := [t for t in self._bg if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)
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
        if entity in ("device", "group") and self.maintenance is not None:
            self.maintenance.request()  # coverage may have changed (moves, new devices)
