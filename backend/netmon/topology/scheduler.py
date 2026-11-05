"""[2b-1] Scheduled topology refresh: every `topology_refresh_min` minutes (0 = off), first run
2 minutes after startup."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from netmon.topology.refresh import RefreshRunningError

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.topology.scheduler")

FIRST_RUN_S = 120.0


class TopologyScheduler:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self.interval_min = 30
        self.next_at: float | None = None
        self.first_run_s = FIRST_RUN_S

    async def start(self) -> None:
        await self._load()
        loop = asyncio.get_running_loop()
        self.next_at = loop.time() + self.first_run_s if self.interval_min > 0 else None
        self._task = asyncio.create_task(self._loop(), name="netmon-topology-scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    async def _load(self) -> None:
        from netmon.services import settings as settings_svc

        async with self.rt.sessionmaker() as session:
            self.interval_min = int(await settings_svc.get(session, "topology_refresh_min"))

    async def reschedule(self) -> None:
        """After a settings change: the next run is one interval from now (or off)."""
        await self._load()
        loop = asyncio.get_running_loop()
        self.next_at = loop.time() + self.interval_min * 60 if self.interval_min > 0 else None
        self._wake.set()

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            self._wake.clear()
            timeout = None if self.next_at is None else max(0.0, self.next_at - loop.time())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout)
            if self.next_at is None or loop.time() < self.next_at:
                continue
            self.next_at = loop.time() + self.interval_min * 60 if self.interval_min > 0 else None
            try:
                await self.rt.topology.start(trigger="scheduled")
            except RefreshRunningError:
                log.info("Scheduled topology refresh skipped: one is already running")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Scheduled topology refresh failed to start")
