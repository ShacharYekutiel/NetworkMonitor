"""[2b-2] Port inventory scheduler: a 60 s loop (first pass 180 s after startup) that runs class
detection and the due devices, at most 4 jobs at once; manual refreshes go to the front of the
queue; a daily cleanup task (architecture § 11.2).

The dispatcher (queue + 4 slots) always exists so manual refresh works even when the loop is
not started (tests start the runtime without maintenance tasks)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from netmon.ports import runner
from netmon.ports.eligibility import DeviceFlags, load_flags

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.ports.scheduler")

MAX_JOBS = 4
FIRST_PASS_S = 180.0
PASS_S = 60.0
FIRST_CLEANUP_S = 3600.0
CLEANUP_S = 86400.0


class RefreshRunningError(Exception):
    pass


class PortInventory:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.max_jobs = MAX_JOBS
        self.first_pass_s = FIRST_PASS_S
        self.pass_s = PASS_S
        self._queue: deque[tuple[str, int, Callable[[], Awaitable[Any]], asyncio.Future[Any]]]
        self._queue = deque()
        self._active: set[asyncio.Task[Any]] = set()
        self._runs: set[int] = set()  # devices with a queued or active run
        self._force_due: set[int] = set()
        self._tasks: list[asyncio.Task[Any]] = []
        self._wake = asyncio.Event()
        self.interval_min = 10

    # ---------------------------------------------------------------- dispatcher
    def running(self, device_id: int) -> bool:
        return device_id in self._runs

    def _submit(
        self, kind: str, device_id: int, factory: Callable[[], Awaitable[Any]], front: bool
    ) -> asyncio.Future[Any]:
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        item = (kind, device_id, factory, fut)
        if front:
            self._queue.appendleft(item)
        else:
            self._queue.append(item)
        self._pump()
        return fut

    def _pump(self) -> None:
        while self._queue and len(self._active) < self.max_jobs:
            kind, did, factory, fut = self._queue.popleft()
            task = asyncio.create_task(
                self._job(kind, did, factory, fut), name=f"netmon-ports-{did}"
            )
            self._active.add(task)
            task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._active.discard(task)
        self._pump()

    async def _job(
        self, kind: str, did: int, factory: Callable[[], Awaitable[Any]], fut: asyncio.Future[Any]
    ) -> None:
        try:
            result = await factory()
            if not fut.done():
                fut.set_result(result)
        except asyncio.CancelledError:
            if not fut.done():
                fut.cancel()
            raise
        except Exception:  # noqa: BLE001 - logged; the future resolves to None
            log.exception("Port inventory %s of device %s failed", kind, did)
            if not fut.done():
                fut.set_result(None)
        finally:
            if kind == "run":
                self._runs.discard(did)

    def submit_run(self, device_id: int, trigger: str, front: bool = False) -> asyncio.Future[Any]:
        if device_id in self._runs:
            raise RefreshRunningError(device_id)
        self._runs.add(device_id)
        self._force_due.discard(device_id)
        return self._submit(
            "run", device_id, lambda: runner.run_device(self.rt, device_id, trigger), front
        )

    def refresh(self, device_id: int) -> asyncio.Future[Any]:
        """Manual refresh: queued at the front (RefreshRunningError when one is queued/active)."""
        return self.submit_run(device_id, "manual", front=True)

    def mark_due(self, device_id: int) -> None:
        """A flag change makes the device due at once (next pass)."""
        self._force_due.add(device_id)

    async def idle(self, timeout: float = 120.0) -> None:
        """Wait until no job is queued or running (tests)."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while self._queue or self._active:
            if loop.time() > end:
                raise TimeoutError("port inventory jobs still running")
            await asyncio.sleep(0.05)

    # ---------------------------------------------------------------- the pass
    async def _interval(self) -> int:
        from netmon.services import settings as settings_svc

        async with self.rt.sessionmaker() as session:
            self.interval_min = int(await settings_svc.get(session, "port_inventory_min"))
        return self.interval_min

    @staticmethod
    def is_due(f: DeviceFlags, now: datetime, interval_min: int, forced: bool) -> bool:
        if not (f.ports or f.arp):
            return False
        if forced or f.last_run_at is None:
            return True
        return f.last_run_at <= now - timedelta(minutes=interval_min)

    async def pass_once(self) -> list[int]:
        """Class detection, then start the due devices (nulls first, then oldest run)."""
        interval = await self._interval()
        if interval <= 0:
            return []
        now = self.rt.clock.now()
        async with self.rt.sessionmaker() as session:
            flags = await load_flags(session)
        detect = [f.id for f in flags.values() if f.needs_detection(now)]
        if detect:
            futs = [
                self._submit("detect", did, lambda d=did: runner.detect_class(self.rt, d), False)
                for did in detect
            ]
            await asyncio.gather(*futs, return_exceptions=True)
            async with self.rt.sessionmaker() as session:
                flags = await load_flags(session)
        due = [
            f
            for f in flags.values()
            if f.id not in self._runs and self.is_due(f, now, interval, f.id in self._force_due)
        ]
        due.sort(key=lambda f: (f.last_run_at is not None, f.last_run_at or now, f.id))
        for f in due:
            with contextlib.suppress(RefreshRunningError):
                self.submit_run(f.id, "scheduled")
        return [f.id for f in due]

    # ---------------------------------------------------------------- loops
    async def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._loop(), name="netmon-ports-scheduler"))
        self._tasks.append(asyncio.create_task(self._cleanup_loop(), name="netmon-ports-cleanup"))

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        await asyncio.sleep(self.first_pass_s)
        while True:
            try:
                await self.pass_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Port inventory pass failed")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.pass_s)

    async def _cleanup_loop(self) -> None:
        await asyncio.sleep(FIRST_CLEANUP_S)
        while True:
            try:
                n = await runner.cleanup(self.rt)
                log.info("Port inventory cleanup: %s", n)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Port inventory cleanup failed")
            await asyncio.sleep(CLEANUP_S)

    async def stop(self) -> None:
        for _, _, _, fut in self._queue:
            if not fut.done():
                fut.cancel()
        self._queue.clear()
        for t in [*self._tasks, *self._active]:
            t.cancel()
        for t in [*self._tasks, *self._active]:
            with contextlib.suppress(BaseException):
                await t
        self._tasks.clear()
        self._runs.clear()


def next_run_at(f: DeviceFlags, now: datetime, interval_min: int) -> datetime | None:
    """last_run_at + interval (now when never run); None when the interval is 0 or nothing is
    effective."""
    if interval_min <= 0 or not (f.ports or f.arp):
        return None
    if f.last_run_at is None:
        return now
    return f.last_run_at + timedelta(minutes=interval_min)
