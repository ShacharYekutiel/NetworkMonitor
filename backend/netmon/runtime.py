"""Process-wide runtime: DB engine, poller, alert engine, WS hub, maintenance tasks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

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
        self.ports: Any = None  # [2b-2] netmon.ports.scheduler.PortInventory
        # [2b-3] feature toggles, ingestion (traps/syslog), event sensors, flapping
        from netmon.services.features import FeatureManager

        self.features = FeatureManager(self)
        self.ingest: Any = None  # netmon.ingest.pipeline.Ingest
        self.flows: Any = None  # [3] netmon.flows.service.FlowCollector
        self.flows_rollup: str | None = None  # [3] "cagg" | "table" | None
        self.flows_policies_ok = False
        self.configbackup: Any = None  # [3] netmon.configbackup.runner.ConfigBackupRunner
        self.newdev: Any = None  # [3] netmon.newdev.service.NewDeviceDetector
        # [3] SSO / reports / chat ack / kiosk (netmon.auth.sso_component.register_components)
        self.kerberos: Any = None
        self.oidc: Any = None
        self.sso: Any = None
        self.reports: Any = None
        self.telegram_pollers: Any = None
        self.kiosk: Any = None
        self.assistant: Any = None  # [3b] netmon.assistant.service.AssistantService
        self.ai_features: Any = None  # [3b] netmon.assistant.features.component.AiFeatures
        self.event_sensors: Any = None  # netmon.ingest.event_sensors.EventSensors
        self.flapping: Any = None  # netmon.ingest.flapping.FlapManager
        self.trgm_enabled = False
        self.ingest_retention_policy_ok = False
        self.dependencies_enabled = True
        from netmon.services.dependencies import Cascade

        self.cascade = Cascade(self)
        self.semaphore = asyncio.Semaphore(max(1, settings.netmon_max_concurrent_checks))
        self.started_monotonic = time.monotonic()
        # a dedicated asyncpg connection outside the SQLAlchemy pool holds the session-level
        # advisory lock; the watchdog checks that same connection
        self._lock_conn: Any = None
        self._has_users = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._rollup_event = asyncio.Event()
        self._rollup_snapshot: dict[tuple[str, int], tuple[str, tuple[int, ...]]] | None = None
        self._bg: set[asyncio.Task[Any]] = set()
        # [1.4.1] cached security settings and the session ids revoked by a logout
        from netmon.services.security import DEFAULT_POLICY

        self.security = DEFAULT_POLICY
        self.revoked_sessions: set[str] = set()
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
        from netmon.configbackup import credentials as ssh_credentials

        ssh_credentials.configure(self.secret_key)  # [3]
        self.engine = make_engine(s.async_database_url)
        self.sessionmaker = make_sessionmaker(self.engine)
        await wait_for_db(self.engine)
        if take_lock:
            await self._acquire_lock()
            self._tasks.append(asyncio.create_task(self._lock_watch_loop(), name="netmon-lock"))
        if migrate:
            await run_migrations(self.engine)
        from netmon.services import security as security_svc

        await security_svc.load(self)  # [1.4.1] egress filter, session lifetime, login cap
        await security_svc.load_revoked(self)
        async with self.sessionmaker() as session:
            retention_days = await settings_svc.get(session, "retention_days")
        ts = await timescale.bootstrap_status(
            self.engine, int(retention_days), disabled=s.netmon_disable_timescale
        )
        self.timescale_enabled = ts.enabled
        self.retention_policy_ok = ts.retention_policy
        # [2b-3] pg_trgm for the trap/syslog text search (failure -> sequential scans)
        from netmon.db.extensions import ensure_trgm

        self.trgm_enabled = await ensure_trgm(self.engine)
        async with self.sessionmaker() as session:
            trap_days = await settings_svc.get(session, "trap_retention_days")
            syslog_days = await settings_svc.get(session, "syslog_retention_days")
        self.ingest_retention_policy_ok = await timescale.bootstrap_ingest(
            self.engine, self.timescale_enabled, int(trap_days), int(syslog_days)
        )
        # [1.2.1] stored trap names follow the built-in OID name table (once per table version)
        self.spawn(self._backfill_trap_names())
        # [1.3] interface sensors on unimportant ports -> impact warning (once)
        self.spawn(self._backfill_impact())
        from netmon.topology.refresh import TopologyRunner

        self.topology = TopologyRunner(self)
        n = await self.topology.mark_interrupted()
        if n:
            log.warning("Marked %d interrupted topology refresh(es) as failed", n)
        await self._mark_stale_sensors()
        # [2b-2] template library (built-in + ${NETMON_DATA_DIR}/templates), before discovery
        from netmon.templates import registry as template_registry

        try:
            await template_registry.reload(self, broadcast=False)
        except Exception:  # noqa: BLE001 - the server always starts
            log.exception("Loading the template library failed")
        # [2b-2] the OUI vendor table (~40k rows) is parsed once, off the event loop
        from netmon.ports import oui

        await asyncio.to_thread(oui.table)
        from netmon.ports.scheduler import PortInventory

        self.ports = PortInventory(self)
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
        # [2b-1] the maintenance tracker evaluates once BEFORE the alert loop starts, so
        # overdue rows of devices in maintenance are not sent after a restart
        from netmon.services.maintenance import MaintenanceTracker

        self.maintenance = MaintenanceTracker(self)
        await self.maintenance.start()
        self.alerts.start()
        # [2b-3] features: forwarders -> pipeline -> rules + expiry -> flapping -> receivers
        from netmon.ingest.pipeline import Ingest

        self.ingest = Ingest(self)
        # [3] flow collector: hourly objects bootstrapped whether or not `flows` is ON
        from netmon.flows.service import FlowCollector

        self.flows = FlowCollector(self)
        await self.flows.bootstrap()
        self.features.register(self.flows)
        # [3] config backup runner, new-device detector
        from netmon.configbackup.runner import ConfigBackupRunner
        from netmon.newdev.service import NewDeviceDetector

        self.configbackup = ConfigBackupRunner(self)
        self.features.register(self.configbackup)
        self.newdev = NewDeviceDetector(self)
        self.features.register(self.newdev)
        # [3] SSO, report scheduler, Telegram update poller, kiosk hub
        from netmon.auth.sso_component import register_components

        register_components(self)
        # [3b] assistant connection layer (rt.assistant; netmon.assistant.service)
        from netmon.assistant.service import AssistantService

        self.assistant = AssistantService(self)
        self.features.register(self.assistant)
        from netmon.assistant import agent as assistant_agent

        assistant_agent.attach(self.assistant)  # [3b] runs, cancel on OFF, tool status
        # [3b] AI features: correlation, alert notes, digest, template walks, /ask (§ 14.8)
        from netmon.assistant.features.component import AiFeatures

        self.ai_features = AiFeatures(self)
        self.features.register(self.ai_features)
        try:
            await assistant_agent.mark_interrupted(self)
        except Exception:  # noqa: BLE001
            log.exception("Marking interrupted assistant answers failed")
        try:
            await self.features.start()
        except Exception:  # noqa: BLE001 - the server always starts
            log.exception("Starting the 2b-3 features failed")
        if maintenance:
            from netmon.topology.scheduler import TopologyScheduler

            self.topology_scheduler = TopologyScheduler(self)
            await self.topology_scheduler.start()
            await self.ports.start()  # [2b-2] port inventory scheduler
        self._tasks.append(asyncio.create_task(self._rollup_loop(), name="netmon-rollup"))
        if maintenance:
            self._tasks.append(asyncio.create_task(retention.loop(self), name="netmon-retention"))
        if run_poller:
            self.scheduler = Scheduler(self)
            await self.scheduler.start()
        await events_svc.write_system_event(self, "NetworkMonitor started")
        # [1.4.1] existing targets the egress filter now refuses: one WARNING (off the start path)
        self.spawn(self._report_blocked_targets())
        if s.netmon_test_mode:
            log.warning(
                "NETMON_TEST_MODE is on: PUT /api/test/clock can shift the maintenance/alerting "
                "clock. Never use this in production."
            )
            await events_svc.write_system_event(
                self, "Test mode is on (NETMON_TEST_MODE): the test clock API is enabled"
            )
        if not self.timescale_enabled:
            await events_svc.write_system_event(
                self, "TimescaleDB not available — using plain PostgreSQL"
            )
        self.started = True
        log.info("NetworkMonitor %s started (timescale=%s)", self.version, self.timescale_enabled)

    async def _report_blocked_targets(self) -> None:
        """[1.4.1] HTTP sensors / webhook / Teams channels that point at a target the SSRF egress
        filter refuses: a startup WARNING and a `system` event naming them."""
        from netmon.services import security as security_svc

        try:
            blocked = await security_svc.warn_blocked_targets(self)
            if blocked:
                await events_svc.write_system_event(
                    self,
                    f"{len(blocked)} monitoring / notification target(s) are blocked by the "
                    "egress filter (loopback / link-local / cloud metadata): "
                    + "; ".join(blocked)[:900],
                )
        except Exception:  # noqa: BLE001 - a report only
            log.exception("Checking the existing targets against the egress filter failed")

    async def stop(self) -> None:
        # [2b-3] receivers first (sockets closed), pipeline flush <= 5 s, forwarders <= 2 s
        with contextlib.suppress(Exception):
            await self.features.stop()
        if self.ports is not None:
            with contextlib.suppress(Exception):
                await self.ports.stop()
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
                await self._lock_conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_ID)
            with contextlib.suppress(Exception):
                await self._lock_conn.close()
            self._lock_conn = None
        if self.engine is not None:
            await self.engine.dispose()
        self.started = False

    async def _lock_connect(self) -> Any:
        import asyncpg

        url = self.settings.async_database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        return await asyncio.wait_for(
            asyncpg.connect(url, server_settings={"application_name": "netmon-lock"}), 30
        )

    async def _acquire_lock(self) -> None:
        conn = await self._lock_connect()
        try:
            got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_ID)
        except BaseException:
            with contextlib.suppress(Exception):
                await conn.close()
            raise
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
        reason = "no lock connection"
        if self._lock_conn is not None:
            try:
                held = bool(
                    await asyncio.wait_for(
                        self._lock_conn.fetchval(
                            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                            "AND database = (SELECT oid FROM pg_database "
                            "WHERE datname = current_database()) "
                            "AND classid = 0 AND objid = $1 AND objsubid = 1 "
                            "AND pid = pg_backend_pid() AND granted",
                            ADVISORY_LOCK_ID,
                        ),
                        10,
                    )
                )
                reason = "the lock is no longer held by this connection"
            except Exception as exc:  # noqa: BLE001
                held = False
                reason = f"the lock connection failed: {type(exc).__name__}: {exc}"
        if held:
            return True
        log.error("Lost the single-instance advisory lock (%s); re-acquiring", reason)
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
        from netmon.poller.sensor_types import PASSIVE_TYPES
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
                        Sensor.type.not_in(PASSIVE_TYPES),  # [2b-3]/[3] passive
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
    async def _backfill_trap_names(self) -> None:
        from netmon.ingest import collapse

        try:
            await collapse.backfill_names(self)
        except Exception as exc:  # noqa: BLE001 - names are cosmetic, never block startup
            log.warning("Cannot update the stored trap names: %s", exc)

    async def _backfill_impact(self) -> None:
        from netmon.services import impact

        try:
            await impact.backfill(self)
        except Exception as exc:  # noqa: BLE001 - never block startup; retried next start
            log.warning("Cannot set the impact of existing interface sensors: %s", exc)

    def spawn(self, coro: Any) -> asyncio.Future[Any]:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

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

    def correlation_graph_changed(self) -> None:
        """[3b] § 14.8.2: drop the correlation dependency/topology graph cache (60 s TTL stays
        as the backstop)."""
        ai = self.ai_features
        corr = getattr(ai, "correlation", None) if ai is not None else None
        if corr is not None:
            corr.invalidate()

    def topology_changed(self, reason: str, refresh_id: int | None = None) -> None:
        """Topology links or layout changed: `topology.updated` + correlation graph refresh."""
        self.hub.broadcast("topology.updated", {"reason": reason, "refresh_id": refresh_id})
        if reason != "layout":
            self.correlation_graph_changed()

    def config_changed(self, entity: str, id_: int, action: str) -> None:
        self.hub.broadcast("config.changed", {"entity": entity, "id": id_, "action": action})
        if entity == "notification_channel" and self.telegram_pollers is not None:
            self.telegram_pollers.channels_changed()  # [3] chat_ack: pollers follow channels
        self.request_rollup()
        if entity in ("device", "group", "sensor", "event_rule") and self.ingest is not None:
            self.ingest.mark_sources_dirty()  # [2b-3] attribution / rule cache
        if entity == "device" and self.flows is not None:
            self.flows.mark_sources_dirty()  # [3] exporter attribution
        if entity in ("device", "group") and self.configbackup is not None:
            self.configbackup.mark_dirty()  # [3] config_backup sensors of new eligible devices
        if entity in ("device", "group") and self.newdev is not None:
            self.newdev.mark_dirty()  # [3] new_mac sensors of new in-scope switches
        if entity in ("device", "group"):
            self.cascade.invalidate()  # dependencies may have changed
            self.correlation_graph_changed()  # [3b] § 14.8.2: dependency graph
            if self.maintenance is not None:
                self.maintenance.request()  # coverage may have changed (moves, new devices)
