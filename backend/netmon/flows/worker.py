"""[3] Flow worker thread (architecture 13.3.2): drains the bounded raw queue in batches
(<= 500 datagrams), decodes, applies the global record budget and the sampling correction,
aggregates per (exporter, minute received) and hands closed minutes to the event loop with
`call_soon_threadsafe`. Decoding is CPU-bound pure Python and never runs on the loop."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from netmon.flows.aggregate import Flush, MinuteAggregator
from netmon.flows.decode import decode_datagram
from netmon.flows.limits import (
    MINUTE_GRACE_S,
    RAW_QUEUE,
    WORKER_BATCH,
    RecordBudget,
)
from netmon.flows.records import FlowRecord
from netmon.flows.templates import TemplateCache

log = logging.getLogger("netmon.flows.worker")

TEMPLATE_SWEEP_S = 60.0
IDLE_WAIT_S = 0.25


def effective_rate(rec: FlowRecord, override: int | None) -> int:
    """Precedence (architecture 13.3.4): the exporter's `sampling_override` -> the rate the
    decoder found (sFlow sample rate, v9/IPFIX sampler matched by id, exporter-wide options
    sampling, NetFlow v5 header interval) -> 1."""
    return override or rec.rate or 1


@dataclass
class RawItem:
    data: bytes
    source: str  # UDP source address
    exporter: str  # NetFlow/IPFIX: the source; sFlow: the agent address
    sflow: bool
    ts: float  # receive time (epoch seconds, app clock)
    override: int | None = None


@dataclass
class ExporterStats:
    records: int = 0
    protocols: set[str] = field(default_factory=set)
    rate: int | None = None
    egress: bool = False
    ingress: bool = False
    last_seen: float = 0.0


class FlowWorker:
    def __init__(
        self,
        deliver: Callable[[list[Flush], dict[str, ExporterStats], dict[str, int]], None],
        now: Callable[[], float],
        budget_per_s: int,
        aggregator: MinuteAggregator,
    ) -> None:
        """`deliver(flushes, stats, drops)` is called from the worker thread; the caller wraps
        it in `loop.call_soon_threadsafe`. `now()` returns the app clock in epoch seconds."""
        self.raw: queue.Queue[RawItem] = queue.Queue(maxsize=RAW_QUEUE)
        self.cache = TemplateCache()
        self.agg = aggregator
        self.budget = RecordBudget(budget_per_s)
        self.deliver = deliver
        self.now = now
        self.drops: dict[str, int] = {}
        self.records = 0
        self.received = 0
        self._stats: dict[str, ExporterStats] = {}
        self._seconds: deque[tuple[int, int]] = deque(maxlen=64)
        self._stop = threading.Event()
        self._discard = False
        self._requests: queue.Queue[threading.Event] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._last_sweep = time.monotonic()
        self._lock = threading.Lock()  # counters read by the loop

    # ---------------------------------------------------------------- lifecycle
    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.alive:
            return
        self._stop.clear()
        self._discard = False
        self._thread = threading.Thread(target=self._run, name="netmon-flow-worker", daemon=True)
        self._thread.start()

    def stop(self, flush: bool, timeout: float) -> None:
        """flush=True: process what is queued and flush every open minute (shutdown);
        flush=False: discard the queue and the open minutes (feature OFF)."""
        self._discard = not flush
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout)
        self._thread = None

    def request_flush(self, timeout: float = 5.0) -> bool:
        """Process the queue and flush every open minute now (tests, shutdown). Blocking:
        call it in a thread."""
        ev = threading.Event()
        self._requests.put(ev)
        return ev.wait(timeout)

    # ---------------------------------------------------------------- thread
    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._step(IDLE_WAIT_S)
            if self._discard:
                n = 0
                while True:
                    try:
                        self.raw.get_nowait()
                        n += 1
                    except queue.Empty:
                        break
                self.agg.discard()
                self._stats.clear()
            else:
                deadline = time.monotonic() + 2.0
                while not self.raw.empty() and time.monotonic() < deadline:
                    self._step(0)
                self._close(None)
        except Exception:  # noqa: BLE001
            log.exception("Flow worker crashed")
        finally:
            while True:  # never leave a waiter hanging
                try:
                    self._requests.get_nowait().set()
                except queue.Empty:
                    break

    def _step(self, wait: float) -> None:
        batch: list[RawItem] = []
        try:
            batch.append(self.raw.get(timeout=wait) if wait else self.raw.get_nowait())
        except queue.Empty:
            pass
        while batch and len(batch) < WORKER_BATCH:
            try:
                batch.append(self.raw.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._process(batch)
        requests = []
        while True:
            try:
                requests.append(self._requests.get_nowait())
            except queue.Empty:
                break
        if requests:
            while True:  # everything queued before the request
                more = []
                while len(more) < WORKER_BATCH:
                    try:
                        more.append(self.raw.get_nowait())
                    except queue.Empty:
                        break
                if not more:
                    break
                self._process(more)
            self._close(None)
            for ev in requests:
                ev.set()
            return
        now = self.now()
        closed = int((now - MINUTE_GRACE_S) // 60) * 60 - 60
        if any(m <= closed for m in self.agg.minutes()):
            self._close(closed)
        mono = time.monotonic()
        if mono - self._last_sweep >= TEMPLATE_SWEEP_S:
            self._last_sweep = mono
            self.cache.expire(now)

    def _drop(self, reason: str, n: int = 1) -> None:
        with self._lock:
            self.drops[reason] = self.drops.get(reason, 0) + n

    def _process(self, batch: list[RawItem]) -> None:
        for item in batch:
            try:
                self._one(item)
            except Exception:  # noqa: BLE001 - one bad datagram never stops the worker
                log.debug("Flow datagram from %s failed", item.source, exc_info=True)
                self._drop("parse_error")
        if self.agg.overflow:
            self._drop("aggregation_overflow", self.agg.overflow)
            self.agg.overflow = 0

    def _one(self, item: RawItem) -> None:
        res = decode_datagram(item.data, item.source, item.sflow, self.cache, item.ts)
        for reason, n in res.errors.items():
            self._drop(reason, n)
        recs = res.records
        if not recs:
            return
        n = len(recs)
        ok = self.budget.take(n)
        if ok < n:
            self._drop("over_budget", n - ok)
            recs = recs[:ok]
            if not recs:
                return
        exporter = item.exporter
        self.agg.add(exporter, int(item.ts // 60) * 60, recs, item.override)
        st = self._stats.get(exporter)
        if st is None:
            st = self._stats[exporter] = ExporterStats()
        st.records += ok
        if res.protocol:
            st.protocols.add(res.protocol)
        st.rate = effective_rate(recs[-1], item.override)
        st.last_seen = item.ts
        for r in recs:
            if r.egress:
                st.egress = True
            else:
                st.ingress = True
        with self._lock:
            self.records += ok
            sec = int(time.monotonic())
            if self._seconds and self._seconds[-1][0] == sec:
                self._seconds[-1] = (sec, self._seconds[-1][1] + ok)
            else:
                self._seconds.append((sec, ok))

    def _close(self, up_to: int | None) -> None:
        flushes = self.agg.flush(up_to)
        stats, self._stats = self._stats, {}
        with self._lock:
            drops = dict(self.drops)
        if flushes or stats:
            try:
                self.deliver(flushes, stats, drops)
            except Exception:  # noqa: BLE001 - the loop is gone (shutdown)
                log.debug("Delivering flow flushes failed", exc_info=True)

    # ---------------------------------------------------------------- loop side
    def records_per_s(self, window: int = 10) -> float:
        now = int(time.monotonic())
        with self._lock:
            n = sum(c for s, c in self._seconds if now - window <= s < now)
        return round(n / window, 1)

    def counters(self) -> tuple[int, dict[str, int]]:
        with self._lock:
            return self.records, dict(self.drops)

    def reset_counters(self) -> None:
        with self._lock:
            self.records = 0
            self.drops = {}
            self._seconds.clear()


def as_dict(stats: ExporterStats) -> dict[str, Any]:  # pragma: no cover - debugging aid
    return {
        "records": stats.records,
        "protocols": sorted(stats.protocols),
        "rate": stats.rate,
        "egress": stats.egress,
        "ingress": stats.ingress,
    }
