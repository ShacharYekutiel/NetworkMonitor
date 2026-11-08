"""[3] Flow collector runtime (`rt.flows`, a phase-3 feature component for `flows`).

Startup (feature ON): writer -> worker thread -> receivers. Shutdown: receivers first, the
worker flushes the current minute (<= 3 s), the writer drains. Feature OFF: receivers closed,
worker stopped and the current minute discarded; data, exporters, retention and (fallback)
hourly rollup are kept.

Receive path (event loop, never blocks): per-exporter datagram bucket -> exporter gate
(accepted / pending / ignored / limit) -> bounded raw queue. Worker thread: decode ->
record budget -> sampling correction -> MinuteAggregator -> closed minutes handed back with
`call_soon_threadsafe` -> bounded write queue -> batch writer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from netmon.flows import settings as flow_settings
from netmon.flows import storage
from netmon.flows.aggregate import Flush, MinuteAggregator
from netmon.flows.decode import exporter_of
from netmon.flows.limits import (
    DB_RETRY_S,
    DROP_REASONS,
    EXPORTER_DATAGRAMS_BURST,
    EXPORTER_DATAGRAMS_PER_S,
    MAX_DATAGRAM,
    MAX_EXPORTERS,
    RAW_QUEUE,
    RETRY_RECEIVERS_S,
    STOP_FLUSH_S,
    TRACKED_SOURCES,
    UNSAMPLED_WARN_RECORDS_PER_S,
    WRITE_QUEUE,
    clamp_budget,
)
from netmon.flows.receiver import FlowReceiver
from netmon.flows.services import ServiceMap
from netmon.flows.worker import ExporterStats, FlowWorker, RawItem
from netmon.ingest import attribution
from netmon.ingest.limits import TokenBucket
from netmon.services.serialize import iso, now_utc

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.flows")

SOURCE_REFRESH_S = 60.0
EXPORTER_SYNC_S = 60.0
WRITE_BATCH_FLUSHES = 20
RATE_WINDOW_S = 300.0
DIRECTION_WARN_S = 900.0


@dataclass
class Exporter:
    address: str
    state: str
    id: int | None = None
    device_id: int | None = None
    protocols: set[str] = field(default_factory=set)
    sampling_override: int | None = None
    sampling_seen: int | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    records_total: int = 0
    pending_records: int = 0  # not yet added to records_total in the DB
    dirty: bool = False
    egress_at: float = 0.0
    ingress_at: float = 0.0
    window: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=64))

    def records_per_s(self, now: float | None = None) -> float:
        t = time.monotonic() if now is None else now
        n = sum(c for ts, c in self.window if t - ts <= RATE_WINDOW_S)
        return round(n / RATE_WINDOW_S, 1)


class FlowCollector:
    features = ("flows",)

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        s = rt.settings
        self.host = s.netmon_flow_host
        self.receivers = [
            FlowReceiver(self, "netflow", s.netmon_netflow_port),
            FlowReceiver(self, "ipfix", s.netmon_ipfix_port),
            FlowReceiver(self, "sflow", s.netmon_sflow_port),
        ]
        self.on = False
        self.cfg: dict[str, Any] = flow_settings.values_of({})
        self.exporters: dict[str, Exporter] = {}
        self.smap = attribution.SourceMap()
        self.received = 0
        self.stored_rows = 0
        self.drops: dict[str, int] = dict.fromkeys(DROP_REASONS, 0)
        self.counters_since = now_utc()
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()
        self.worker: FlowWorker | None = None
        self.write_q: asyncio.Queue[Flush] = asyncio.Queue(maxsize=WRITE_QUEUE)
        self._tasks: list[asyncio.Task[Any]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sources_dirty = asyncio.Event()
        self._creating: set[str] = set()
        self._worker_base: tuple[int, dict[str, int]] = (0, {})

    # ================================================================ lifecycle
    async def bootstrap(self) -> None:
        """At server start (whether or not `flows` is ON): hourly objects and their mode."""
        assert self.rt.engine is not None
        try:
            async with self.rt.sessionmaker() as session:
                self.cfg = await flow_settings.load(session)
            mode, policies = await storage.bootstrap(
                self.rt.engine, self.rt.timescale_enabled, self.cfg
            )
        except Exception:  # noqa: BLE001 - the server always starts
            log.exception("Flow storage bootstrap failed")
            mode, policies = None, False
        self.rt.flows_rollup = mode
        self.rt.flows_policies_ok = policies

    async def start(self, snapshot: dict[str, bool]) -> None:
        await self.load_exporters()
        if snapshot.get("flows"):
            await self._start()

    async def stop(self) -> None:
        await self._stop(flush=True)

    async def feature_changed(self, name: str, on: bool) -> None:
        if name != "flows":
            return
        if on:
            await self._start()
        else:
            await self._stop(flush=False)

    async def _start(self) -> None:
        if self.on:
            return
        self._loop = asyncio.get_running_loop()
        async with self.rt.sessionmaker() as session:
            self.cfg = await flow_settings.load(session)
        await self.load_exporters()
        await self.rebuild_sources()
        agg = MinuteAggregator(
            top_apps=int(self.cfg["flow_top_apps"]),
            top_hosts=int(self.cfg["flow_top_hosts"]),
            top_conversations=int(self.cfg["flow_top_conversations"]),
            services=ServiceMap(overrides=self.cfg.get("flow_app_overrides") or []),
        )
        self.write_q = asyncio.Queue(maxsize=WRITE_QUEUE)
        self.worker = FlowWorker(
            self._deliver_threadsafe,
            lambda: self.rt.clock.now().timestamp(),
            clamp_budget(self.cfg["flow_max_records_per_s"]),
            agg,
        )
        self._tasks = [
            asyncio.create_task(self._writer(), name="netmon-flows-writer"),
        ]
        self.worker.start()
        self._tasks.append(asyncio.create_task(self._maintenance(), name="netmon-flows-maint"))
        self.on = True
        for r in self.receivers:
            ok = await r.start(self.host)
            if not ok:
                await self._bind_event(r)

    async def _stop(self, flush: bool) -> None:
        if not self.on and self.worker is None:
            return
        self.on = False
        for r in self.receivers:
            await r.stop()
        worker, self.worker = self.worker, None
        if worker is not None:
            await asyncio.to_thread(worker.stop, flush, STOP_FLUSH_S)
            records, drops = worker.counters()
            base_r, base_d = self._worker_base
            for k, v in drops.items():
                base_d[k] = base_d.get(k, 0) + v
            self._worker_base = (base_r + records, base_d)
            dropped = worker.raw.qsize()
            if dropped:
                self._drop("queue_full", dropped)
        await asyncio.sleep(0)  # deliveries scheduled by the worker run now
        # the writer drains what was handed over (<= 3 s), then stops
        deadline = time.monotonic() + STOP_FLUSH_S
        while not self.write_q.empty() and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(BaseException):
                await t
        self._tasks = []
        left = self.write_q.qsize()
        if left:
            self._drop("queue_full", left)
        while not self.write_q.empty():
            self.write_q.get_nowait()
            self.write_q.task_done()
        with contextlib.suppress(Exception):
            await self.sync_exporters()

    async def _bind_event(self, r: FlowReceiver) -> None:
        from netmon.services import events as events_svc

        name = {"netflow": "NetFlow", "ipfix": "IPFIX", "sflow": "sFlow"}[r.kind]
        await events_svc.write_system_event(
            self.rt, f"{name} receiver cannot listen on UDP {r.port}: {r.error}"
        )

    def receiver_changed(self, r: FlowReceiver) -> None:
        self.rt.hub.broadcast("ingest.receiver", {"receiver": r.status()}, admin_only=True)

    def receiver_states(self) -> dict[str, str]:
        return {r.kind: r.state for r in self.receivers}

    # ================================================================ configuration
    def configure(self, cfg: dict[str, Any]) -> None:
        """Settings changed (budget, top-N, app overrides): applied to the running worker."""
        self.cfg = dict(cfg)
        w = self.worker
        if w is None:
            return
        w.budget.set_rate(clamp_budget(cfg["flow_max_records_per_s"]))
        w.agg.configure(
            top_apps=int(cfg["flow_top_apps"]),
            top_hosts=int(cfg["flow_top_hosts"]),
            top_conversations=int(cfg["flow_top_conversations"]),
            services=ServiceMap(overrides=cfg.get("flow_app_overrides") or []),
        )

    async def retention_changed(self, cfg: dict[str, Any]) -> None:
        self.cfg = dict(cfg)
        if self.rt.flows_rollup == "cagg" and self.rt.engine is not None:
            self.rt.flows_policies_ok = await storage.apply_policies(self.rt.engine, cfg)

    def services(self) -> ServiceMap:
        return ServiceMap(overrides=self.cfg.get("flow_app_overrides") or [])

    def mark_sources_dirty(self) -> None:
        self._sources_dirty.set()

    async def rebuild_sources(self) -> None:
        from netmon.ingest.pipeline import resolve_all

        async with self.rt.sessionmaker() as session:
            devices = [
                dict(r)
                for r in (
                    await session.execute(
                        text("SELECT id, name, host, enabled, source_addresses FROM devices")
                    )
                ).mappings()
            ]
        for d in devices:
            d["source_addresses"] = [str(a) for a in d.get("source_addresses") or []]
        names = sorted(
            {
                str(d["host"]).strip().lower()
                for d in devices
                if d["host"] and attribution.normalize_ip(str(d["host"])) is None
            }
        )
        resolved = await resolve_all(names)
        self.smap = attribution.build(devices, resolved)
        await self._reattribute_pending()

    async def _reattribute_pending(self) -> None:
        """Pending exporters that now match a device are accepted (architecture 13.3.5)."""
        for ex in list(self.exporters.values()):
            if ex.state != "pending" or ex.id is None:
                continue
            device_id = attribution.attribute(self.smap, ex.address)
            if device_id is None:
                continue
            async with self.rt.sessionmaker() as session:
                await session.execute(
                    text(
                        "UPDATE flow_exporters SET state = 'accepted', device_id = :d "
                        "WHERE id = :id AND state = 'pending'"
                    ),
                    {"d": device_id, "id": ex.id},
                )
                await session.commit()
            ex.state, ex.device_id = "accepted", device_id
            await self.broadcast_exporter(ex.id)

    # ================================================================ exporters
    async def load_exporters(self) -> None:
        try:
            async with self.rt.sessionmaker() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT id, host(address) AS address, device_id, state, protocols, "
                            "sampling_override, sampling_seen, first_seen, last_seen, "
                            "records_total FROM flow_exporters"
                        )
                    )
                ).mappings()
                fresh: dict[str, Exporter] = {}
                for r in rows:
                    addr = attribution.normalize_ip(str(r["address"])) or str(r["address"])
                    old = self.exporters.get(addr)
                    ex = Exporter(
                        address=addr,
                        state=str(r["state"]),
                        id=int(r["id"]),
                        device_id=r["device_id"],
                        protocols=set(r["protocols"] or []),
                        sampling_override=r["sampling_override"],
                        sampling_seen=r["sampling_seen"],
                        first_seen=r["first_seen"],
                        last_seen=r["last_seen"],
                        records_total=int(r["records_total"] or 0),
                    )
                    if old is not None:  # keep the in-memory statistics
                        ex.pending_records = old.pending_records
                        ex.window = old.window
                        ex.egress_at, ex.ingress_at = old.egress_at, old.ingress_at
                        ex.dirty = old.dirty
                        if old.last_seen and (ex.last_seen is None or old.last_seen > ex.last_seen):
                            ex.last_seen = old.last_seen
                        ex.protocols |= old.protocols
                        ex.sampling_seen = old.sampling_seen or ex.sampling_seen
                    fresh[addr] = ex
        except Exception:  # noqa: BLE001 - e.g. migration 0007 not applied
            log.exception("Loading the flow exporters failed")
            return
        self.exporters = fresh

    def _new_exporter(self, address: str) -> Exporter | None:
        if len(self.exporters) >= MAX_EXPORTERS:
            return None
        device_id = attribution.attribute(self.smap, address)
        now = self.rt.clock.now()
        ex = Exporter(
            address=address,
            state="accepted" if device_id is not None else "pending",
            device_id=device_id,
            first_seen=now,
            last_seen=now,
        )
        self.exporters[address] = ex
        if address not in self._creating:
            self._creating.add(address)
            self.rt.spawn(self._insert_exporter(ex))
        return ex

    async def _insert_exporter(self, ex: Exporter) -> None:
        try:
            async with self.rt.sessionmaker() as session:
                row = (
                    await session.execute(
                        text(
                            "INSERT INTO flow_exporters (address, device_id, state, protocols, "
                            "first_seen, last_seen) VALUES (CAST(:a AS inet), :d, :s, "
                            "CAST(:p AS text[]), :fs, :ls) ON CONFLICT (address) DO UPDATE SET "
                            "last_seen = EXCLUDED.last_seen RETURNING id, state, device_id, "
                            "sampling_override"
                        ),
                        {
                            "a": ex.address,
                            "d": ex.device_id,
                            "s": ex.state,
                            "p": sorted(ex.protocols),
                            "fs": ex.first_seen,
                            "ls": ex.last_seen,
                        },
                    )
                ).first()
                await session.commit()
            if row is not None:
                ex.id = int(row.id)
                ex.state = str(row.state)
                ex.device_id = row.device_id
                ex.sampling_override = row.sampling_override
                self.rt.config_changed("flow_exporter", ex.id, "created")
                await self.broadcast_exporter(ex.id)
        except Exception:  # noqa: BLE001
            log.exception("Creating flow exporter %s failed", ex.address)
            self.exporters.pop(ex.address, None)  # retried at the next datagram
        finally:
            self._creating.discard(ex.address)

    async def broadcast_exporter(self, exporter_id: int) -> None:
        from netmon.flows import query

        try:
            async with self.rt.sessionmaker() as session:
                items = await query.exporters_out(self, session, ids=[exporter_id])
        except Exception:  # noqa: BLE001
            return
        if items:
            self.rt.hub.broadcast("flows.exporter", {"exporter": items[0]}, admin_only=True)

    def exporter_updated(
        self, address: str, state: str, device_id: int | None, override: int | None
    ) -> None:
        ex = self.exporters.get(address)
        if ex is None:
            return
        ex.state, ex.device_id, ex.sampling_override = state, device_id, override

    def exporter_deleted(self, address: str) -> None:
        self.exporters.pop(address, None)

    async def sync_exporters(self) -> None:
        """Statistics of the exporters into `flow_exporters` (at most once per minute)."""
        dirty = [e for e in self.exporters.values() if e.dirty and e.id is not None]
        if not dirty:
            return
        async with self.rt.sessionmaker() as session:
            for ex in dirty:
                delta = ex.pending_records
                await session.execute(
                    text(
                        "UPDATE flow_exporters SET protocols = CAST(:p AS text[]), "
                        "sampling_seen = :s, last_seen = GREATEST(last_seen, :ls), "
                        "records_total = records_total + :n WHERE id = :id"
                    ),
                    {
                        "p": sorted(ex.protocols),
                        "s": ex.sampling_seen,
                        "ls": ex.last_seen,
                        "n": delta,
                        "id": ex.id,
                    },
                )
                ex.pending_records -= delta
                ex.records_total += delta
                ex.dirty = False
            await session.commit()

    # ================================================================ receive path
    def accept(self, data: bytes, ip: str, sflow: bool) -> None:
        """Receiver callback (event loop, never blocks)."""
        self.received += 1
        if not self.on or self.worker is None:
            return
        if len(data) > MAX_DATAGRAM:
            self._drop("parse_error")
            return
        if not self._allow(ip):
            self._drop("rate_limited")
            return
        exporter = exporter_of(data, ip, sflow)
        if exporter is None:
            v = int.from_bytes(data[:4], "big") if len(data) >= 4 else 5
            self._drop("unsupported" if v != 5 else "parse_error")
            return
        ex = self.exporters.get(exporter)
        if ex is None:
            ex = self._new_exporter(exporter)
            if ex is None:
                self._drop("exporter_limit")
                return
        if ex.state != "accepted":
            if ex.state == "pending":
                ex.last_seen = self.rt.clock.now()
                ex.dirty = True
            self._drop("unknown_exporter" if ex.state == "pending" else "ignored_exporter")
            return
        item = RawItem(
            data, ip, exporter, sflow, self.rt.clock.now().timestamp(), ex.sampling_override
        )
        try:
            self.worker.raw.put_nowait(item)
        except queue.Full:
            self._drop("queue_full")

    def _allow(self, ip: str) -> bool:
        b = self._buckets.get(ip)
        if b is None:
            b = TokenBucket(EXPORTER_DATAGRAMS_PER_S, EXPORTER_DATAGRAMS_BURST)
            self._buckets[ip] = b
            if len(self._buckets) > TRACKED_SOURCES:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(ip)
        return b.take()

    def _drop(self, reason: str, n: int = 1) -> None:
        self.drops[reason] = self.drops.get(reason, 0) + n

    # ================================================================ worker -> loop
    def _deliver_threadsafe(
        self, flushes: list[Flush], stats: dict[str, ExporterStats], drops: dict[str, int]
    ) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._on_flushes, flushes, stats)

    def _on_flushes(self, flushes: list[Flush], stats: dict[str, ExporterStats]) -> None:
        mono = time.monotonic()
        for address, st in stats.items():
            ex = self.exporters.get(address)
            if ex is None:
                continue
            ex.pending_records += st.records
            ex.window.append((mono, st.records))
            ex.protocols |= st.protocols
            if st.rate is not None:
                ex.sampling_seen = st.rate
            if st.last_seen:
                ex.last_seen = datetime.fromtimestamp(st.last_seen, self.rt.clock.now().tzinfo)
            if st.egress:
                ex.egress_at = mono
            if st.ingress:
                ex.ingress_at = mono
            ex.dirty = True
        for f in flushes:
            try:
                self.write_q.put_nowait(f)
            except asyncio.QueueFull:
                self._drop("queue_full")

    async def _writer(self) -> None:
        while True:
            first = await self.write_q.get()
            batch = [first]
            while len(batch) < WRITE_BATCH_FLUSHES and not self.write_q.empty():
                batch.append(self.write_q.get_nowait())
            try:
                await self._write(batch)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Flow writer failed")
            finally:
                for _ in batch:
                    self.write_q.task_done()

    async def _write(self, batch: list[Flush]) -> None:
        # an exporter row may still be being inserted (rare): wait <= 2 s for it
        deadline = time.monotonic() + 2.0
        pending = any(f.exporter in self._creating for f in batch)
        while pending and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.02)
            pending = any(f.exporter in self._creating for f in batch)
        rows: list[tuple[int, Flush]] = []
        for f in batch:
            ex = self.exporters.get(f.exporter)
            if ex is None or ex.id is None:
                self._drop("db_error")  # the exporter row could not be created
                continue
            rows.append((ex.id, f))
        if not rows:
            return
        for attempt in (0, 1):
            try:
                self.stored_rows += await storage.write_batch(self.rt, rows)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if attempt == 0:
                    log.warning("Writing flows failed (%s); retrying once", exc)
                    await asyncio.sleep(DB_RETRY_S)
                    continue
                log.warning("Writing flows failed again; %d flush(es) dropped", len(rows))
                self._drop("db_error", len(rows))

    async def _maintenance(self) -> None:
        last_sources = time.monotonic()
        last_sync = time.monotonic()
        last_retry = time.monotonic()
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._sources_dirty.wait(), 5.0)
            try:
                now = time.monotonic()
                if self._sources_dirty.is_set() or now - last_sources >= SOURCE_REFRESH_S:
                    self._sources_dirty.clear()
                    await asyncio.sleep(0.5)  # debounce bursts of device edits
                    self._sources_dirty.clear()
                    await self.rebuild_sources()
                    last_sources = time.monotonic()
                if now - last_sync >= EXPORTER_SYNC_S:
                    last_sync = now
                    await self.sync_exporters()
                if now - last_retry >= RETRY_RECEIVERS_S:
                    last_retry = now
                    for r in self.receivers:
                        if r.wanted and r.state == "error":
                            await r.start(self.host)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Flow maintenance failed")

    # ================================================================ status / tests
    def counters(self) -> dict[str, Any]:
        base_r, base_d = self._worker_base
        records = base_r
        drops = dict(self.drops)
        for k, v in base_d.items():
            drops[k] = drops.get(k, 0) + v
        if self.worker is not None:
            r, d = self.worker.counters()
            records += r
            for k, v in d.items():
                drops[k] = drops.get(k, 0) + v
        return {
            "received_datagrams": self.received,
            "records": records,
            "stored_rows": self.stored_rows,
            "dropped": {k: int(drops.get(k, 0)) for k in DROP_REASONS},
        }

    def exporter_flags(self, ex: Exporter) -> dict[str, Any]:
        mono = time.monotonic()
        rps = ex.records_per_s(mono)
        both = (
            ex.egress_at > 0
            and ex.ingress_at > 0
            and mono - ex.egress_at <= DIRECTION_WARN_S
            and mono - ex.ingress_at <= DIRECTION_WARN_S
        )
        rate = ex.sampling_override or ex.sampling_seen or 1
        return {
            "records_per_s": rps,
            "both_directions_warning": bool(both),
            "unsampled_warning": rate == 1 and rps > UNSAMPLED_WARN_RECORDS_PER_S,
        }

    async def status(self, admin: bool) -> dict[str, Any]:
        w = self.worker
        return {
            "receivers": [r.status() for r in self.receivers],
            "counters_since": iso(self.counters_since),
            "counters": self.counters(),
            "records_per_s": w.records_per_s() if w is not None else 0.0,
            "budget_per_s": clamp_budget(self.cfg.get("flow_max_records_per_s")),
            "queue": (w.raw.qsize() if w is not None else 0) + self.write_q.qsize(),
            "queue_capacity": RAW_QUEUE + WRITE_QUEUE,
            "templates": len(w.cache) if w is not None else 0,
            "rollup": getattr(self.rt, "flows_rollup", None),
            "storage_bytes": await storage.storage_bytes(self.rt) if admin else None,
        }

    def reset_state(self) -> None:
        """Tests: counters, buckets and in-memory exporters back to empty."""
        self.received = 0
        self.stored_rows = 0
        self.drops = dict.fromkeys(DROP_REASONS, 0)
        self._worker_base = (0, {})
        self.counters_since = now_utc()
        self._buckets.clear()
        self.exporters.clear()
        if self.worker is not None:
            self.worker.reset_counters()
            self.worker.cache.clear()

    async def sync(self, timeout: float = 10.0) -> None:
        """Tests / shutdown: process everything queued, flush every open minute, write it and
        the exporter statistics."""
        await self.rt.drain()  # exporter rows being created
        w = self.worker
        if w is not None:
            await asyncio.to_thread(w.request_flush, timeout)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.wait_for(self.write_q.join(), timeout)
        await self.rt.drain()
        await self.sync_exporters()
