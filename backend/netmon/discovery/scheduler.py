"""DiscoveryScheduler: a 30 s loop that starts due scheduled profile scans."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import text

from netmon.discovery.runner import ScanRunningError
from netmon.discovery.targets import TargetError, TooManyHostsError
from netmon.services.serialize import now_utc

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.discovery.scheduler")

LOOP_INTERVAL_S = 30.0
BUSY_RETRY = timedelta(minutes=10)


class DiscoveryScheduler:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="netmon-discovery-scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except BaseException:  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(LOOP_INTERVAL_S)
            try:
                await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Discovery scheduler iteration failed")

    async def run_due(self, now: datetime | None = None) -> list[int]:
        """Start scans for enabled profiles whose next_run_at is due. Returns started scan ids."""
        now = now or now_utc()
        runner = self.rt.discovery
        async with self.rt.sessionmaker() as session:
            due = (
                await session.execute(
                    text(
                        "SELECT id, name, targets, group_id, options, schedule_every_hours, "
                        "last_run_at FROM discovery_profiles WHERE schedule_enabled "
                        "AND next_run_at IS NOT NULL AND next_run_at <= :now ORDER BY next_run_at"
                    ),
                    {"now": now},
                )
            ).all()
        started: list[int] = []
        for p in due:
            next_at = now + timedelta(hours=int(p.schedule_every_hours))
            if runner is None or runner.busy:
                log.info("Discovery profile %r is due but a scan is running; retry later", p.name)
                await self._set_next(p.id, now + BUSY_RETRY)
                continue
            try:
                scan_id = await runner.start(
                    targets=list(p.targets),
                    group_id=p.group_id,
                    options=dict(p.options or {}),
                    trigger="scheduled",
                    profile_id=p.id,
                    profile_name=p.name,
                    created_by=None,
                    baseline=p.last_run_at is None,
                )
            except ScanRunningError:
                log.info("Discovery profile %r is due but a scan is running; retry later", p.name)
                next_at = now + BUSY_RETRY
            except (TargetError, TooManyHostsError) as exc:
                log.warning("Discovery profile %r has invalid targets: %s", p.name, exc)
            else:
                started.append(scan_id)
            await self._set_next(p.id, next_at)
        return started

    async def _set_next(self, profile_id: int, when: datetime) -> None:
        async with self.rt.sessionmaker() as session:
            await session.execute(
                text("UPDATE discovery_profiles SET next_run_at = :t WHERE id = :id"),
                {"t": when, "id": profile_id},
            )
            await session.commit()
