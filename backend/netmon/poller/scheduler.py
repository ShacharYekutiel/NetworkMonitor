"""Min-heap scheduler with bounded concurrency."""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import logging
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from netmon.models import Device, Sensor
from netmon.poller.processor import execute_check, persist
from netmon.services.sensors import build_context, effective_paused

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.poller.scheduler")


@dataclass
class _Entry:
    gen: int
    interval_s: int
    scheduled: float


class Scheduler:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._heap: list[tuple[float, int, int]] = []  # (when, sensor_id, gen)
        self._entries: dict[int, _Entry] = {}
        self._running: set[int] = set()
        self._followup: set[int] = set()  # [2b-1] run once more after the current run
        self._tasks: set[asyncio.Task[None]] = set()
        self._gen = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.running = False

    @property
    def scheduled_count(self) -> int:
        return len(self._entries)

    def _loop_time(self) -> float:
        return asyncio.get_running_loop().time()

    def _push(self, sensor_id: int, interval_s: int, when: float) -> None:
        self._gen += 1
        self._entries[sensor_id] = _Entry(self._gen, interval_s, when)
        heapq.heappush(self._heap, (when, sensor_id, self._gen))
        self._wake.set()

    async def start(self) -> None:
        async with self.rt.sessionmaker() as session:
            rows = await session.execute(
                select(Sensor.id, Sensor.interval_s)
                .join(Device, Device.id == Sensor.device_id)
                .where(Sensor.enabled.is_(True), Device.enabled.is_(True))
            )
            now = self._loop_time()
            for sid, interval in rows:
                self._push(sid, interval, now + random.uniform(0, min(interval, 30)))
        self.running = True
        self._task = asyncio.create_task(self._loop(), name="netmon-scheduler")
        log.info("Scheduler started with %d sensors", len(self._entries))

    async def stop(self, grace_s: float = 5.0) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=grace_s)
            for t in pending:
                t.cancel()
        self._entries.clear()
        self._heap.clear()

    def remove(self, sensor_id: int) -> None:
        self._entries.pop(sensor_id, None)

    async def upsert(self, sensor_id: int) -> None:
        async with self.rt.sessionmaker() as session:
            row = (
                await session.execute(
                    select(Sensor.interval_s, Sensor.enabled, Device.enabled)
                    .join(Device, Device.id == Sensor.device_id)
                    .where(Sensor.id == sensor_id)
                )
            ).first()
        if row is None or not row[1] or not row[2]:
            self.remove(sensor_id)
            return
        interval = int(row[0])
        entry = self._entries.get(sensor_id)
        now = self._loop_time()
        if entry is not None and entry.interval_s == interval:
            return
        self._push(sensor_id, interval, now + random.uniform(0.5, 2.0))

    async def reload_device(self, device_id: int) -> None:
        async with self.rt.sessionmaker() as session:
            ids = (
                (await session.execute(select(Sensor.id).where(Sensor.device_id == device_id)))
                .scalars()
                .all()
            )
        for sid in ids:
            await self.upsert(sid)

    def remove_many(self, sensor_ids: list[int]) -> None:
        for sid in sensor_ids:
            self.remove(sid)

    def run_soon(self, sensor_ids: list[int], jitter_s: float = 5.0) -> None:
        """[2b-1] One extra run of each sensor after a random 0..jitter_s delay. The regular
        schedule is not moved; a sensor that is running at that moment is skipped."""
        for sid in dict.fromkeys(sensor_ids):
            delay = random.uniform(0, jitter_s) if jitter_s > 0 else 0.0
            task = asyncio.create_task(self._soon(sid, delay))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _soon(self, sensor_id: int, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        if sensor_id in self._running:
            # a check is in flight (it may have started before the reason for this extra
            # run): run once more right after it
            self._followup.add(sensor_id)
            return
        self._running.add(sensor_id)
        await self._run_one(sensor_id, remove_on_pause=False)

    async def idle(self) -> None:
        """Wait for in-flight checks (tests)."""
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            now = self._loop_time()
            while self._heap and self._heap[0][0] <= now:
                when, sid, gen = heapq.heappop(self._heap)
                entry = self._entries.get(sid)
                if entry is None or entry.gen != gen:
                    continue
                nxt = when + entry.interval_s
                if nxt <= now:
                    nxt = now + entry.interval_s
                entry.scheduled = nxt
                heapq.heappush(self._heap, (nxt, sid, gen))
                if sid in self._running:
                    log.debug("Sensor %s still running; skipping this run", sid)
                    continue
                self._running.add(sid)
                task = asyncio.create_task(self._run_one(sid))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            timeout = None
            if self._heap:
                timeout = max(0.0, self._heap[0][0] - self._loop_time())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout)

    async def _run_one(self, sensor_id: int, remove_on_pause: bool = True) -> None:
        try:
            async with self.rt.sessionmaker() as session:
                sensor = await session.get(Sensor, sensor_id)
                if sensor is None:
                    self.remove(sensor_id)
                    return
                device = await session.get(Device, sensor.device_id)
                if device is None or effective_paused(sensor, device):
                    if remove_on_pause:
                        self.remove(sensor_id)
                    return
                ctx = await build_context(session, sensor, device)
            raw, duration, checked_at = await execute_check(ctx, self.rt.semaphore)
            await persist(self.rt, sensor_id, raw, duration, checked_at)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Check of sensor %s failed", sensor_id)
        finally:
            self._running.discard(sensor_id)
            if sensor_id in self._followup:
                self._followup.discard(sensor_id)
                task = asyncio.create_task(self._soon(sensor_id, 0.0))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
