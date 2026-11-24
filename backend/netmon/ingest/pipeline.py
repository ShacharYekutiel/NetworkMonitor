"""[2b-3] Ingestion pipeline (architecture section 12.2): receivers -> rate limits -> bounded
raw queue -> decode / parse -> attribution -> ignore list / unknown policy -> rules -> SIEM
forwarders -> bounded write queue -> batch INSERT ... RETURNING id -> event sensors -> WS live
batches. One `Ingest` per runtime (`rt.ingest`); features start/stop its parts at runtime.

Receiver callbacks never block and never touch the DB; every queue is bounded and every drop
is counted."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from netmon.ingest import attribution, collapse, oid_names, rules
from netmon.ingest import trap_decode as td
from netmon.ingest.event_sensors import EventSensors, Item
from netmon.ingest.flapping import FlapManager
from netmon.ingest.forward import Forwarder, Target
from netmon.ingest.lag_monitor import LagMonitor
from netmon.ingest.limits import (
    DROP_EVENTS_PER_HOUR,
    DROP_REASONS,
    LIVE_INTERVAL_S,
    LIVE_MAX,
    MAX_DATAGRAM,
    RAW_QUEUE,
    SYSLOG_GLOBAL_BURST,
    SYSLOG_GLOBAL_RATE,
    SYSLOG_PER_SOURCE_BURST,
    SYSLOG_PER_SOURCE_RATE,
    TCP_QUEUE_WAIT_S,
    TRAP_GLOBAL_BURST,
    TRAP_GLOBAL_RATE,
    TRAP_PER_SOURCE_BURST,
    TRAP_PER_SOURCE_RATE,
    WRITE_BATCH,
    WRITE_INTERVAL_S,
    WRITE_QUEUE,
    RateLimiter,
)
from netmon.ingest.query import syslog_out, trap_out
from netmon.ingest.syslog_parse import parse_syslog
from netmon.ingest.syslog_protect import SyslogProtection
from netmon.ingest.syslog_receiver import SyslogTcp
from netmon.ingest.trap_receiver import UdpReceiver, V3Engine, snmp_creds_to_user
from netmon.services.serialize import iso, now_utc

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.ingest")

KINDS = ("trap", "syslog")
SOURCE_REFRESH_S = 600.0
RETRY_RECEIVERS_S = 60.0
PORT_CACHE_TTL_S = 300.0
FLUSH_ON_STOP_S = 5.0
RESOLVE_TIMEOUT_S = 2.0
RESOLVE_PARALLEL = 8
DB_WARN_EVERY_S = 60.0
PROTECT_FLUSH_S = 2.0  # [syslog-protection] repeat counts -> DB
HITS_FLUSH_S = 10.0  # [syslog-protection] filter-rule hit counters -> DB


class Counters:
    def __init__(self) -> None:
        self.received = 0
        self.stored = 0
        self.forwarded = 0
        self.deduplicated = 0  # [syslog-protection] folded into an earlier identical row
        self.pruned = 0  # [syslog-protection] removed by a "keep newest N" filter rule
        self.dropped = dict.fromkeys(DROP_REASONS, 0)
        self._minutes: deque[tuple[int, int]] = deque(maxlen=6)

    def drop(self, reason: str, n: int = 1) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + n

    def accepted(self) -> None:
        minute = int(time.monotonic() // 60)
        if self._minutes and self._minutes[-1][0] == minute:
            self._minutes[-1] = (minute, self._minutes[-1][1] + 1)
        else:
            self._minutes.append((minute, 1))

    def rate_per_min(self) -> float:
        minute = int(time.monotonic() // 60)
        n = sum(c for m, c in self._minutes if minute - 5 <= m < minute)
        return round(n / 5.0, 1)

    def out(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "stored": self.stored,
            "forwarded": self.forwarded,
            "deduplicated": self.deduplicated,
            "pruned": self.pruned,
            "dropped": dict(self.dropped),
        }


class PortInfo:
    def __init__(self, names: dict[int, str], important: set[int] | None, loaded: float) -> None:
        self.names = names
        self.important = important  # None = every port is important
        self.loaded = loaded

    def is_important(self, if_index: int) -> bool:
        return self.important is None or if_index in self.important


class Ingest:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        s = rt.settings
        self.host = s.netmon_ingest_host
        self.counters = {k: Counters() for k in KINDS}
        self.counters_since = now_utc()
        self.raw: dict[str, asyncio.Queue[tuple[Any, ...]]] = {
            k: asyncio.Queue(maxsize=RAW_QUEUE) for k in KINDS
        }
        self.write: dict[str, asyncio.Queue[tuple[dict[str, Any], Any]]] = {
            k: asyncio.Queue(maxsize=WRITE_QUEUE) for k in KINDS
        }
        self.limiters = {
            "trap": RateLimiter(
                (TRAP_PER_SOURCE_RATE, TRAP_PER_SOURCE_BURST), (TRAP_GLOBAL_RATE, TRAP_GLOBAL_BURST)
            ),
            "syslog": RateLimiter(
                (SYSLOG_PER_SOURCE_RATE, SYSLOG_PER_SOURCE_BURST),
                (SYSLOG_GLOBAL_RATE, SYSLOG_GLOBAL_BURST),
            ),
        }
        self.live: dict[str, deque[dict[str, Any]]] = {k: deque(maxlen=LIVE_MAX) for k in KINDS}
        self.live_skipped = dict.fromkeys(KINDS, 0)
        self.trap_rx = UdpReceiver(self, "trap", "trap", s.netmon_trap_port)
        self.syslog_udp = UdpReceiver(self, "syslog", "syslog_udp", s.netmon_syslog_port)
        self.syslog_tcp = SyslogTcp(self, s.syslog_tcp_port)
        self.v3 = V3Engine()
        self.smap = attribution.SourceMap()
        self.ignored: list[Any] = []
        self.unknown_policy = "store"
        self.community_check = "any"
        self.known_communities: set[str] = set()
        self.linkdown_scope = "important"
        self.include_trunks = True  # [1.3] event_linkdown_include_trunks
        self._trunk_cfg: tuple[bool, int] | None = None
        self.collapse_on = False
        self.collapse_oids: list[str] = []
        self.collapse_keep = 10
        self.protect = SyslogProtection()  # [syslog-protection]
        self.cleanup: dict[str, Any] | None = None  # [syslog-protection] the running/last job
        self._trap_settings: dict[str, list[Any]] = {"communities": [], "v3_users": []}
        self._tz: str | None = None
        self.forwarders: dict[str, Forwarder] = {}
        self.recent: deque[tuple[str, bool]] = deque(maxlen=200)
        self.ports: dict[int, PortInfo] = {}
        self.on: set[str] = set()
        self.alerts_on = False
        self.forwarding_on = False
        self._tasks: dict[str, list[asyncio.Task[Any]]] = {k: [] for k in KINDS}
        self._bg: list[asyncio.Task[Any]] = []
        self._sources_dirty = asyncio.Event()
        self._drop_events: dict[str, float] = {}
        self._drop_window: deque[float] = deque()
        self._db_warned = float("-inf")  # monotonic counts from boot: 0.0 hid early warnings
        self.events = EventSensors(rt)
        self.flapping = FlapManager(rt)
        self.lags = LagMonitor(rt)  # [1.3] LAG degraded
        rt.event_sensors = self.events
        rt.flapping = self.flapping

    # ================================================================ lifecycle
    async def start(self, features: dict[str, bool]) -> None:
        """Startup order: forwarders -> pipeline/writer -> rule cache + expiry -> flapping ->
        receivers (last)."""
        await self.reload_config()
        await self.rebuild_sources()
        await self.events.reload()
        self._bg.append(asyncio.create_task(self._maintenance_loop(), name="netmon-ingest-maint"))
        self._bg.append(asyncio.create_task(self._live_loop(), name="netmon-ingest-live"))
        if features.get("siem_forwarding"):
            await self.set_forwarding(True)
        for kind, name in (("trap", "traps"), ("syslog", "syslog")):
            if features.get(name):
                self._start_pipeline(kind)
        self.alerts_on = bool(features.get("event_alerts"))
        if features.get("flapping"):
            await self.flapping.start()
        if self.alerts_on or features.get("flapping"):
            self.events.start()
        for kind, name in (("trap", "traps"), ("syslog", "syslog")):
            if features.get(name):
                await self._start_receivers(kind)

    async def stop(self) -> None:
        for kind in KINDS:
            await self._stop_receivers(kind)
        for kind in KINDS:
            await self._stop_pipeline(kind)
        await self.set_forwarding(False)
        await self.events.stop()
        await self.flapping.stop(clear=False)
        for t in self._bg:
            t.cancel()
        for t in self._bg:
            with contextlib.suppress(BaseException):
                await t
        self._bg.clear()

    async def feature_changed(self, name: str, on: bool) -> None:
        if on and name in ("traps", "syslog", "event_alerts", "flapping"):
            # caches are not maintained while nothing is on
            await self.reload_config()
            await self.rebuild_sources()
            await self.events.reload()
        if name in ("traps", "syslog"):
            kind = "trap" if name == "traps" else "syslog"
            if on:
                await self.reload_config()
                self._start_pipeline(kind)
                await self._start_receivers(kind)
            else:
                await self._stop_receivers(kind)
                await self._stop_pipeline(kind)
                if kind == "trap":
                    self.v3.drop()
        elif name == "event_alerts":
            self.alerts_on = on
            if on:
                await self.events.reload()
                self.events.start()
            else:
                await self.events.event_alerts_off()
                if not self.flapping.enabled:
                    await self.events.stop()
        elif name == "flapping":
            if on:
                await self.flapping.start()
                self.events.start()
            else:
                await self.flapping.stop()
                if not self.alerts_on:
                    await self.events.stop()
        elif name == "siem_forwarding":
            await self.set_forwarding(on)

    # ---------------------------------------------------------------- pipeline tasks
    def _start_pipeline(self, kind: str) -> None:
        if kind in self.on:
            return
        self.on.add(kind)
        self._tasks[kind] = [
            asyncio.create_task(self._worker(kind), name=f"netmon-ingest-{kind}-worker"),
            asyncio.create_task(self._writer(kind), name=f"netmon-ingest-{kind}-writer"),
        ]
        if kind == "syslog":
            self._tasks[kind].append(
                asyncio.create_task(self._protect_loop(), name="netmon-ingest-syslog-protect")
            )

    async def _stop_pipeline(self, kind: str) -> None:
        if kind not in self.on:
            return
        self.on.discard(kind)
        # flush for <= 5 s, then drop what is left (counted)
        deadline = time.monotonic() + FLUSH_ON_STOP_S
        while (not self.raw[kind].empty() or not self.write[kind].empty()) and (  # noqa: ASYNC110
            time.monotonic() < deadline
        ):
            await asyncio.sleep(0.05)
        for t in self._tasks[kind]:
            t.cancel()
        for t in self._tasks[kind]:
            with contextlib.suppress(BaseException):
                await t
        self._tasks[kind] = []
        if kind == "syslog":  # [syslog-protection] last repeat counts and hit counters
            with contextlib.suppress(Exception):
                await self.flush_protection(final=True)
        left = self.raw[kind].qsize() + self.write[kind].qsize()
        if left:
            self.counters[kind].drop("queue_full", left)
        for q in (self.raw[kind], self.write[kind]):
            while not q.empty():
                q.get_nowait()
        self.live[kind].clear()

    def _receivers(self, kind: str) -> list[Any]:
        return [self.trap_rx] if kind == "trap" else [self.syslog_udp, self.syslog_tcp]

    async def _start_receivers(self, kind: str) -> None:
        for r in self._receivers(kind):
            ok = await r.start(self.host)
            if not ok:
                proto = "TCP" if r.status_kind == "syslog_tcp" else "UDP"
                what = "Trap receiver" if kind == "trap" else "Syslog receiver"
                from netmon.services import events as events_svc

                await events_svc.write_system_event(
                    self.rt, f"{what} cannot listen on {proto} {r.port}: {r.error}"
                )

    async def _stop_receivers(self, kind: str) -> None:
        for r in self._receivers(kind):
            await r.stop()

    def receiver_changed(self, r: Any) -> None:
        self.rt.hub.broadcast("ingest.receiver", {"receiver": r.status()}, admin_only=True)

    def receiver_states(self) -> dict[str, str]:
        return {
            "trap": self.trap_rx.state,
            "syslog_udp": self.syslog_udp.state,
            "syslog_tcp": self.syslog_tcp.state,
        }

    # ---------------------------------------------------------------- forwarders
    async def set_forwarding(self, on: bool) -> None:
        self.forwarding_on = on
        for f in list(self.forwarders.values()):
            await f.stop()
        self.forwarders = {}
        if not on:
            return
        async with self.rt.sessionmaker() as session:
            from netmon.services import settings as settings_svc

            targets = await settings_svc.get(session, "siem_targets")
        for t in targets or []:
            target = Target.from_dict(t)
            f = Forwarder(target, self.rt.version)
            self.forwarders[target.id] = f
            f.start()

    def reset_state(self) -> None:
        """Tests: counters, failures, caches and recent sources back to empty."""
        self.counters = {k: Counters() for k in KINDS}
        self.counters_since = now_utc()
        self.v3.failures.clear()
        self.v3.drop()
        self.recent.clear()
        self.ports.clear()
        self.lags.live.clear()
        self.limiters = {
            "trap": RateLimiter(
                (TRAP_PER_SOURCE_RATE, TRAP_PER_SOURCE_BURST), (TRAP_GLOBAL_RATE, TRAP_GLOBAL_BURST)
            ),
            "syslog": RateLimiter(
                (SYSLOG_PER_SOURCE_RATE, SYSLOG_PER_SOURCE_BURST),
                (SYSLOG_GLOBAL_RATE, SYSLOG_GLOBAL_BURST),
            ),
        }
        self._drop_events.clear()
        self._drop_window.clear()
        self.protect.reset()
        self.cleanup = None
        for k in KINDS:
            self.live[k].clear()
        self.flapping.tracker.clear()
        self.flapping.suppressed.clear()
        self.flapping.suppressed_sensors.clear()

    # ================================================================ config / sources
    async def reload_config(self) -> None:
        from netmon.services import settings as settings_svc

        async with self.rt.sessionmaker() as session:
            cfg = await settings_svc.get_all(session)
        self.ignored = attribution.parse_networks(list(cfg.get("ingest_ignored_sources") or []))
        self.unknown_policy = str(cfg.get("ingest_unknown_sources") or "store")
        self.community_check = str(cfg.get("trap_community_check") or "any")
        self.linkdown_scope = str(cfg.get("event_linkdown_scope") or "important")
        # [1.3] the important-port sets depend on these: drop the cached ones on a change
        trunks = (
            bool(cfg.get("event_linkdown_include_trunks", True)),
            int(cfg.get("trunk_min_tagged_vlans") or 2),
        )
        if trunks != self._trunk_cfg:
            self._trunk_cfg = trunks
            self.ports.clear()
        self.include_trunks = trunks[0]
        # [1.2.1] repetitive traps
        was = (self.collapse_on, list(self.collapse_oids), self.collapse_keep)
        self.collapse_on = bool(cfg.get("trap_collapse_enabled", False))
        oids = cfg.get("trap_collapse_oids") or []
        self.collapse_oids = [collapse.normalize_oid(o) for o in oids]
        self.collapse_keep = int(cfg.get("trap_collapse_keep") or 10)
        if self.collapse_on and was != (True, self.collapse_oids, self.collapse_keep):
            self.rt.spawn(collapse.prune_all(self.rt, self.collapse_oids, self.collapse_keep))
        # [syslog-protection] dedup / rate limit (ON by default) and the filter rules
        self.protect.configure(
            dedup_on=bool(cfg.get("syslog_dedup_enabled", True)),
            window_s=int(cfg.get("syslog_dedup_window_s") or 60),
            rate_on=bool(cfg.get("syslog_rate_limit_enabled", True)),
            per_min=int(cfg.get("syslog_rate_limit_per_min") or 3000),
        )
        await self.reload_syslog_filters()
        self._tz = cfg.get("timezone")
        self._trap_settings = {
            "communities": list(cfg.get("trap_communities") or []),
            "v3_users": list(cfg.get("trap_v3_users") or []),
        }
        self.rt.spawn(self.flapping.configure())

    async def reload_syslog_filters(self) -> None:
        """[syslog-protection] Compile the enabled filter rules (called on every change)."""
        async with self.rt.sessionmaker() as session:
            rows = (
                (await session.execute(text("SELECT * FROM syslog_filters ORDER BY position, id")))
                .mappings()
                .all()
            )
        self.protect.set_rules(list(rows))

    def mark_sources_dirty(self) -> None:
        self._sources_dirty.set()

    async def rebuild_sources(self) -> None:
        """SourceMap, known communities and v3 candidates from devices/groups (hostnames are
        resolved in threads, 2 s timeout, 8 in parallel)."""
        async with self.rt.sessionmaker() as session:
            devices = [
                dict(r)
                for r in (
                    await session.execute(
                        text(
                            "SELECT id, name, host, enabled, group_id, snmp_credentials, "
                            "source_addresses, event_sensor_optout FROM devices ORDER BY id"
                        )
                    )
                ).mappings()
            ]
            groups = [
                dict(r)
                for r in (
                    await session.execute(
                        text("SELECT id, parent_id, snmp_credentials FROM groups ORDER BY id")
                    )
                ).mappings()
            ]
        from netmon.services import secretbox

        for d in devices:
            d["source_addresses"] = [str(a) for a in d.get("source_addresses") or []]
            d["snmp_credentials"] = secretbox.open_snmp(d["snmp_credentials"], "device", d["id"])
        for g in groups:
            g["snmp_credentials"] = secretbox.open_snmp(g["snmp_credentials"], "group", g["id"])
        names = sorted(
            {
                str(d["host"]).strip().lower()
                for d in devices
                if d["host"] and attribution.normalize_ip(str(d["host"])) is None
            }
        )
        resolved = await resolve_all(names)
        self.smap = attribution.build(devices, resolved)
        # communities and v3 users of every credential object
        settings = self._trap_settings
        comms = set(settings["communities"])
        cred_users = []
        for g in groups:
            c = g.get("snmp_credentials") or {}
            if c.get("community"):
                comms.add(str(c["community"]))
            u = snmp_creds_to_user(c)
            if u is not None:
                cred_users.append(u)
        device_users: dict[int, dict[str, Any]] = {}
        for d in devices:
            c = d.get("snmp_credentials") or {}
            if c.get("community"):
                comms.add(str(c["community"]))
            u = snmp_creds_to_user(c)
            if u is not None:
                cred_users.append(u)
        # effective credentials per device (own, else nearest group) for the source-IP choice
        by_group = {g["id"]: g for g in groups}
        for d in devices:
            c = d.get("snmp_credentials")
            gid = d.get("group_id")
            seen: set[int] = set()
            while c is None and gid is not None and gid not in seen:
                seen.add(gid)
                g = by_group.get(gid)
                if g is None:
                    break
                c = g.get("snmp_credentials")
                gid = g.get("parent_id")
            u = snmp_creds_to_user(c or {})
            if u is not None:
                device_users[int(d["id"])] = u
        comms.add("public")  # the default community of devices without credentials
        self.known_communities = comms
        self.v3.configure(list(settings["v3_users"]), cred_users, device_users)
        self.ports.clear()

    async def _maintenance_loop(self) -> None:
        last_rebuild = time.monotonic()
        last_retry = time.monotonic()
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._sources_dirty.wait(), 5.0)
            try:
                now = time.monotonic()
                active = bool(self.on) or self.alerts_on or self.flapping.enabled
                if not active:
                    # nothing to attribute: rebuilt when a feature starts
                    self._sources_dirty.clear()
                elif self._sources_dirty.is_set() or now - last_rebuild >= SOURCE_REFRESH_S:
                    self._sources_dirty.clear()
                    await asyncio.sleep(0.5)  # debounce bursts of config changes
                    self._sources_dirty.clear()
                    await self.reload_config()
                    await self.rebuild_sources()
                    await self.events.reload()
                    last_rebuild = time.monotonic()
                if now - last_retry >= RETRY_RECEIVERS_S:
                    last_retry = now
                    for kind in self.on:
                        for r in self._receivers(kind):
                            if r.wanted and r.state == "error":
                                await r.start(self.host)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Ingest maintenance failed")

    # ================================================================ receive path
    def accept(
        self, kind: str, data: bytes, ip: str, addr: Any, transport: str, endpoint: Any
    ) -> None:
        """Receiver callback (sync, never blocks)."""
        c = self.counters[kind]
        c.received += 1
        if kind not in self.on:
            return
        if self.ignored and attribution.in_networks(ip, self.ignored):
            c.drop("ignored_source")
            return
        if len(data) > MAX_DATAGRAM:
            c.drop("too_large")
            return
        if not self.limiters[kind].allow(ip):
            c.drop("rate_limited")
            self._rate_event(kind, ip)
            return
        try:
            self.raw[kind].put_nowait((data, ip, addr, transport, endpoint, self.rt.clock.now()))
        except asyncio.QueueFull:
            c.drop("queue_full")

    async def accept_tcp(self, frame: bytes, ip: str, addr: Any) -> None:
        """A TCP frame: waits up to 2 s for room in the raw queue (back-pressure)."""
        c = self.counters["syslog"]
        c.received += 1
        if "syslog" not in self.on:
            return
        if self.ignored and attribution.in_networks(ip, self.ignored):
            c.drop("ignored_source")
            return
        if not self.limiters["syslog"].allow(ip):
            c.drop("rate_limited")
            self._rate_event("syslog", ip)
            return
        item = (frame, ip, addr, "tcp", None, self.rt.clock.now())
        try:
            self.raw["syslog"].put_nowait(item)
        except asyncio.QueueFull:
            try:
                await asyncio.wait_for(self.raw["syslog"].put(item), TCP_QUEUE_WAIT_S)
            except TimeoutError:
                c.drop("queue_full")

    def _rate_event(self, kind: str, ip: str, per_min: int | None = None) -> None:
        now = time.monotonic()
        key = f"{kind}:{ip}" if per_min is None else f"rows:{ip}"
        last = self._drop_events.get(key)
        if last is not None and now - last < 3600:
            return
        while self._drop_window and now - self._drop_window[0] > 3600:
            self._drop_window.popleft()
        if len(self._drop_window) >= DROP_EVENTS_PER_HOUR:
            return
        self._drop_events[key] = now
        if len(self._drop_events) > 4096:
            self._drop_events.pop(next(iter(self._drop_events)))
        self._drop_window.append(now)
        from netmon.services import events as events_svc

        what = "traps" if kind == "trap" else "syslog"
        msg = f"Dropping {what} from {ip}: rate limit exceeded"
        if per_min is not None:  # [syslog-protection] the storage rate limit
            msg = (
                f"Syslog from {ip} exceeds {per_min} rows per minute: the excess is not stored "
                "(Settings, syslog flood protection)"
            )
        self.rt.spawn(events_svc.write_system_event(self.rt, msg))

    # ---------------------------------------------------------------- decode workers
    async def _worker(self, kind: str) -> None:
        q = self.raw[kind]
        while True:
            item = await q.get()
            try:
                if kind == "trap":
                    await self._handle_trap(*item)
                else:
                    await self._handle_syslog(*item)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never stop the worker
                self.counters[kind].drop("parse_error")
                log.debug("Ingest of a %s record failed", kind, exc_info=True)

    async def _handle_trap(
        self, data: bytes, ip: str, addr: Any, transport: str, endpoint: Any, t: datetime
    ) -> None:
        c = self.counters["trap"]
        try:
            version = td.message_version(data)
            if version == 3:
                engine_id, user, model = td.parse_v3_header(data)
                if model != 3:
                    raise td.TrapDecodeError("unsupported", "security model")
                await self.v3.ensure()
                try:
                    rec = self.v3.decode(data, addr, engine_id, user, self.smap.by_ip.get(ip))
                except td.TrapDecodeError as exc:
                    if exc.reason == "v3_auth":
                        self.v3.fail(self.rt.clock.now(), ip, engine_id, user, str(exc))
                    raise
            else:
                rec = td.decode_v1v2c(data)
        except td.TrapDecodeError as exc:
            c.drop(exc.reason)
            return
        if rec.community is not None and self.community_check == "known":
            if rec.community not in self.known_communities:
                c.drop("bad_community")
                return
        if rec.response is not None and endpoint is not None:
            endpoint.send(rec.response, addr)
        trap_address = td.varbind_value(rec.varbinds, oid_names.SNMP_TRAP_ADDRESS_0)
        device_id = attribution.attribute(
            self.smap, ip, agent_addr=rec.agent_addr, trap_address=trap_address
        )
        if not self._admit("trap", ip, device_id):
            return
        if_index = td.if_index_of(rec.varbinds)
        port_name = None
        info = None
        if device_id is not None and if_index is not None:
            info = await self.port_info(device_id)
            port_name = info.names.get(if_index)
        td.finish(rec, port_name)
        dev = self.smap.device(device_id) or {}
        record: dict[str, Any] = {
            "kind": "trap",
            "time": t,
            "source_ip": ip,
            "agent_addr": rec.agent_addr,
            "device_id": device_id,
            "device_name": dev.get("name"),
            "device_host": dev.get("host"),
            "version": rec.version,
            "pdu": rec.pdu,
            "security_name": rec.security_name,
            "trap_oid": rec.trap_oid,
            "trap_name": rec.trap_name,
            "uptime_ticks": rec.uptime_ticks,
            "varbinds": rec.varbinds,
            "summary": rec.summary,
            "matched_rule_ids": [],
            "raised_status": None,
        }
        # flapping input 1: linkDown / linkUp with an ifIndex
        if (
            device_id is not None
            and if_index is not None
            and rec.trap_oid in (oid_names.LINK_DOWN, oid_names.LINK_UP)
            and self.flapping.enabled
            and dev.get("enabled", True)
        ):
            self.flapping.transition(
                device_id, if_index, rec.trap_oid == oid_names.LINK_UP, port_name
            )
        # [1.3] LAG degraded: a member's linkDown / linkUp
        if (
            device_id is not None
            and if_index is not None
            and rec.trap_oid in (oid_names.LINK_DOWN, oid_names.LINK_UP)
            and dev.get("enabled", True)
        ):
            self.lags.note_trap(device_id, if_index, rec.trap_oid == oid_names.LINK_UP)
        await self._finish(record, info)

    async def _handle_syslog(
        self, data: bytes, ip: str, addr: Any, transport: str, endpoint: Any, t: datetime
    ) -> None:
        rec = parse_syslog(data, t, self._tz)
        device_id = attribution.attribute(self.smap, ip, hostname=rec.hostname or None)
        if not self._admit("syslog", ip, device_id):
            return
        dev = self.smap.device(device_id) or {}
        record: dict[str, Any] = {
            "kind": "syslog",
            "time": t,
            "device_time": rec.device_time,
            "source_ip": ip,
            "device_id": device_id,
            "device_name": dev.get("name"),
            "device_host": dev.get("host"),
            "transport": transport,
            "format": rec.format,
            "facility": rec.facility,
            "severity": rec.severity,
            "hostname": rec.hostname,
            "program": rec.program,
            "pid": rec.pid,
            "msgid": rec.msgid,
            "structured_data": rec.structured_data,
            "message": rec.message,
            "truncated": rec.truncated,
            "matched_rule_ids": [],
            "raised_status": None,
            "repeat_count": 1,
            "last_seen": None,
        }
        await self._finish(record, None)

    def _admit(self, kind: str, ip: str, device_id: int | None) -> bool:
        self.recent.append((ip, ip in self.smap.by_ip))
        if device_id is None and self.unknown_policy == "drop":
            self.counters[kind].drop("unknown_source")
            return False
        return True

    async def _finish(self, record: dict[str, Any], info: PortInfo | None) -> None:
        kind = record["kind"]
        device_id = record["device_id"]
        outcome = None
        dev = self.smap.device(device_id)
        if (
            self.alerts_on
            and device_id is not None
            and dev is not None
            and dev.get("enabled", True)
            and not self.events.sensor_paused(device_id, kind)
        ):
            rules_ = self.events.rules_for(kind, device_id)
            if rules_:
                if kind == "trap" and info is None and self.linkdown_scope != "all":
                    info = await self.port_info(device_id)
                ctx = rules.Context(
                    important=self._important_fn(info),
                    flapping=lambda idx, d=device_id: self.flapping.is_flapping(d, idx),
                )
                outcome = rules.evaluate(record, rules_, ctx)
                record["matched_rule_ids"] = list(outcome.matched_rule_ids)
                if outcome.raised is not None:
                    record["raised_status"] = outcome.raised[0].action
        # [3] config-change triggers: evaluated whenever config_backup is ON, independently
        # of event_alerts (architecture § 13.2.4)
        backup = getattr(self.rt, "configbackup", None)
        if (
            backup is not None
            and backup.enabled
            and device_id is not None
            and dev is not None
            and dev.get("enabled", True)
        ):
            hits = rules.backup_triggers(record, self.events.rules_for(kind, device_id))
            if hits:
                record["matched_rule_ids"] = [
                    *record["matched_rule_ids"],
                    *(i for i in hits if i not in record["matched_rule_ids"]),
                ]
                backup.trigger(device_id, hits)
        if self.forwarding_on and self.forwarders:
            sent = False
            for f in self.forwarders.values():
                sent = f.offer(record) or sent
            if sent:
                self.counters[kind].forwarded += 1
        if kind == "syslog" and not self._protect_admits(record, outcome):
            return
        try:
            self.write[kind].put_nowait((record, outcome))
            self.counters[kind].accepted()
        except asyncio.QueueFull:
            self.counters[kind].drop("queue_full")

    def _protect_admits(self, record: dict[str, Any], outcome: Any) -> bool:
        """[syslog-protection] Filter rules, dedup and the storage rate limit (after event
        rules, backup triggers and forwarding, which see every message). A message that is
        not stored but changed an event sensor still reaches the writer (marked `_skip`) so
        its raise / clear is applied in the writer's transaction."""
        c = self.counters["syslog"]
        reason = self.protect.check(record)
        if reason is None:
            return True
        if reason == "deduplicated":
            c.deduplicated += 1
        else:
            c.drop(reason)
            if reason == "source_rate_limit":
                self._rate_event("syslog", str(record["source_ip"]), self.protect.per_min)
        if outcome is not None and outcome.matched_rule_ids:
            record["_skip"] = True
            with contextlib.suppress(asyncio.QueueFull):  # the message itself is counted
                self.write["syslog"].put_nowait((record, outcome))
        return False

    def _important_fn(self, info: PortInfo | None) -> Any:
        if self.linkdown_scope == "all" or info is None:
            return None
        return info.is_important

    async def port_info(self, device_id: int) -> PortInfo:
        """Port names and the important-port set of a device (cached 5 min)."""
        info = self.ports.get(device_id)
        now = time.monotonic()
        if info is not None and now - info.loaded < PORT_CACHE_TTL_S:
            return info
        try:
            info = await load_port_info(self.rt, device_id, self.include_trunks)
        except Exception:  # noqa: BLE001 - DB trouble: every port important
            log.debug("Port info of device %s failed", device_id, exc_info=True)
            info = PortInfo({}, None, now)
        self.ports[device_id] = info
        if len(self.ports) > 4096:
            self.ports.pop(next(iter(self.ports)))
        return info

    async def is_important_port(self, device_id: int, if_index: int) -> bool:
        if self.linkdown_scope == "all":
            return True
        return (await self.port_info(device_id)).is_important(if_index)

    # ---------------------------------------------------------------- writer
    async def _writer(self, kind: str) -> None:
        q = self.write[kind]
        while True:
            first = await q.get()
            batch = [first]
            deadline = time.monotonic() + WRITE_INTERVAL_S
            while len(batch) < WRITE_BATCH:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(q.get(), timeout))
                except TimeoutError:
                    break
            await self._flush(kind, batch)

    async def _flush(self, kind: str, batch: list[tuple[dict[str, Any], Any]]) -> None:
        skipped = [(r, o) for r, o in batch if r.get("_skip")]
        batch = [(r, o) for r, o in batch if not r.get("_skip")]
        records = [r for r, _ in batch]
        if not records:
            await self._apply_items(kind, [], skipped)
            return
        ids: list[int] | None = None
        for attempt in range(2):
            try:
                ids = await insert_records(self.rt, kind, records)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                now = time.monotonic()
                if now - self._db_warned >= DB_WARN_EVERY_S:
                    self._db_warned = now
                    log.warning("Cannot store %d %s record(s): %s", len(records), kind, exc)
        if ids is None:
            self.counters[kind].drop("db_error", len(records))
            if kind == "syslog":
                for r in records:
                    self.protect.not_stored(r)
            return
        self.counters[kind].stored += len(ids)
        if kind == "syslog":
            for r, id_ in zip(records, ids, strict=False):
                self.protect.stored(r, id_)
            await self._prune_kept(records)
        if kind == "trap" and self.collapse_on and self.collapse_oids:
            groups = [
                (str(r["source_ip"]), collapse.normalize_oid(r["trap_oid"]))
                for r in records
                if collapse.normalize_oid(r["trap_oid"]) in self.collapse_oids
            ]
            if groups:
                try:
                    await collapse.prune(self.rt, groups, self.collapse_keep)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("Cannot prune repetitive traps: %s", exc)
        for (rec, _), id_ in zip(batch, ids, strict=False):
            rec["id"] = id_
            self.live[kind].append(rec)
        await self._apply_items(kind, batch, skipped)

    async def _apply_items(
        self,
        kind: str,
        stored: list[tuple[dict[str, Any], Any]],
        skipped: list[tuple[dict[str, Any], Any]],
    ) -> None:
        """Event-sensor effects of a flush: the stored records and the [syslog-protection]
        ones that were not stored but matched an event rule (ref = the dedup row, if any)."""
        items: list[Item] = []
        for rec, outcome in [*stored, *skipped]:
            if rec["device_id"] is None:
                continue
            dev = self.smap.device(rec["device_id"])
            if dev is None or not dev.get("enabled", True):
                continue
            ref = rec.get("id") if not rec.get("_skip") else rec.get("_dedup_ref")
            txt = rec["summary"] if kind == "trap" else rec["message"]
            items.append(Item(rec["device_id"], kind, ref, str(txt)[:500], outcome))
        if items and self.alerts_on:
            try:
                await self.events.apply_items(items, alerts_on=True)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Applying event conditions failed")

    # ---------------------------------------------------------------- [syslog-protection]
    async def _prune_kept(self, records: list[dict[str, Any]]) -> None:
        """ "Keep newest N" filter rules: prune each (rule, source) written in this batch."""
        groups = {
            (int(r["filter_rule_id"]), str(r["source_ip"]))
            for r in records
            if r.get("filter_rule_id") is not None
        }
        if not groups:
            return
        keep = {r.id: r.keep_n for r in self.protect.rules if r.action == "keep"}
        try:
            async with self.rt.sessionmaker() as session:
                for rid, ip in sorted(groups):
                    if rid not in keep:
                        continue
                    res = await session.execute(_PRUNE_KEPT, {"r": rid, "s": ip, "n": keep[rid]})
                    n = int(res.rowcount or 0)
                    self.counters["syslog"].pruned += n
                    self.protect.count_pruned(ip, n)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Cannot prune syslog kept by a filter rule: %s", exc)

    async def _protect_loop(self) -> None:
        last_hits = time.monotonic()
        while True:
            await asyncio.sleep(PROTECT_FLUSH_S)
            try:
                hits = time.monotonic() - last_hits >= HITS_FLUSH_S
                if hits:
                    last_hits = time.monotonic()
                await self.flush_protection(hits=hits)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Cannot store syslog repeat counts: %s", exc)

    async def flush_protection(self, *, hits: bool = True, final: bool = False) -> int:
        """Write the dirty dedup repeat counts (and the filter-rule hit counters); returns the
        number of rows updated. `final`: every open dedup window is closed first."""
        p = self.protect
        if final:
            p.close_all()
        else:
            p.expire(self.rt.clock.now().timestamp())
        ups = p.repeat_updates()
        n = 0
        if ups:
            async with self.rt.sessionmaker() as session:
                res = await session.execute(
                    _REPEAT_UPDATE,
                    {
                        "i": [u.id for u in ups],
                        "t": [u.time for u in ups],
                        "c": [u.count for u in ups],
                        "ls": [u.last_seen for u in ups],
                    },
                )
                await session.commit()
            n = int(res.rowcount or 0)
            if self.rt.hub.has_subscribers("syslog"):
                self.rt.hub.broadcast_topic(
                    "syslog",
                    "syslog.repeats",
                    {
                        "items": [
                            {"id": u.id, "repeat_count": u.count, "last_seen": iso(u.last_seen)}
                            for u in ups[-LIVE_MAX:]
                        ]
                    },
                )
        if hits:
            pending = p.take_hits()
            if pending:
                try:
                    async with self.rt.sessionmaker() as session:
                        await session.execute(
                            _HITS_UPDATE,
                            {
                                "i": list(pending),
                                "n": [v[0] for v in pending.values()],
                                "t": [v[1] for v in pending.values()],
                            },
                        )
                        await session.commit()
                except Exception:
                    p.give_back_hits(pending)
                    raise
        return n

    # ---------------------------------------------------------------- live
    async def _live_loop(self) -> None:
        skipped = dict.fromkeys(KINDS, 0)
        while True:
            await asyncio.sleep(LIVE_INTERVAL_S)
            for kind in KINDS:
                buf = self.live[kind]
                if not buf:
                    continue
                topic = kind
                items = list(buf)
                buf.clear()
                if kind not in self.on or not self.rt.hub.has_subscribers(topic):
                    continue
                out = trap_out if kind == "trap" else syslog_out
                newest = items[-LIVE_MAX:][::-1]
                skipped[kind] = max(0, len(items) - LIVE_MAX)
                self.rt.hub.broadcast_topic(
                    topic,
                    f"{kind}.batch",
                    {"items": [out(r) for r in newest], "skipped": skipped[kind]},
                )

    # ================================================================ status
    def nat_address(self) -> str | None:
        return attribution.nat_suspect(list(self.recent))

    def status(self, admin: bool) -> dict[str, Any]:
        nat = self.nat_address()
        return {
            "receivers": [
                self.trap_rx.status(),
                self.syslog_udp.status(),
                self.syslog_tcp.status(),
            ],
            "counters_since": iso(self.counters_since),
            "counters": {k: self.counters[k].out() for k in KINDS},
            "rate_per_min": {k: self.counters[k].rate_per_min() for k in KINDS},
            "queue": {
                "trap": self.raw["trap"].qsize() + self.write["trap"].qsize(),
                "syslog": self.raw["syslog"].qsize() + self.write["syslog"].qsize(),
                "capacity": RAW_QUEUE + WRITE_QUEUE,
            },
            "tcp_connections": self.syslog_tcp.count,
            "nat_suspected": nat is not None,
            "nat_address": nat,
            "v3_failures": list(self.v3.failures)[::-1] if admin else [],
            "forwarders": [f.status() for f in self.forwarders.values()] if admin else [],
            "trgm": bool(getattr(self.rt, "trgm_enabled", False)),
            "timescale": bool(self.rt.timescale_enabled),
            "syslog_protection": self.protection_status(),
        }

    def protection_status(self) -> dict[str, Any]:
        """[syslog-protection] current settings, per-source suppression counters (since
        `counters_since`), the filter-rule hits since boot and the bounded state sizes."""
        p = self.protect
        return {
            "dedup_enabled": p.dedup_on,
            "dedup_window_s": int(p.window_s),
            "rate_limit_enabled": p.rate_on,
            "rate_limit_per_min": p.per_min,
            "filter_rules": len(p.rules),
            "rule_hits": {str(k): v for k, v in p.rule_hits.items()},
            "top_sources": p.top_sources(),
            **p.state_size(),
        }


# ------------------------------------------------------------------ helpers
async def resolve_all(names: list[str]) -> dict[str, list[str]]:
    import socket

    sem = asyncio.Semaphore(RESOLVE_PARALLEL)
    out: dict[str, list[str]] = {}

    async def one(name: str) -> None:
        async with sem:
            try:
                infos = await asyncio.wait_for(
                    asyncio.to_thread(socket.getaddrinfo, name, None), RESOLVE_TIMEOUT_S
                )
            except (OSError, TimeoutError, UnicodeError):
                return
            out[name] = sorted({str(i[4][0]) for i in infos})

    await asyncio.gather(*(one(n) for n in names[:4096]))
    return out


async def load_port_info(rt: Any, device_id: int, include_trunks: bool = True) -> PortInfo:
    """Port names and the important-port set for link-down / flapping alerts (data-model
    "[1.3] Important ports", "[if-noise] Important-port criteria"): the shared definition
    (the chosen criteria, trunks only when `include_trunks`, description pattern, ports marked
    important) plus, when `critical_sensor` is a chosen criterion, ports whose `snmp_interface`
    sensor has impact `critical`; ports marked "not important" never count; a device with
    neither port inventory rows nor (counted) critical interface sensors -> every port
    important."""
    from netmon.ports import read
    from netmon.ports.importance import device_importance, importance_settings, load_overrides

    async with rt.sessionmaker() as session:
        imp = await device_importance(session, device_id)
        criteria, _ = await importance_settings(session)
        overrides = imp.overrides if imp is not None else await load_overrides(session, device_id)
        critical: set[int] = set()
        for r in await session.execute(
            text(
                "SELECT config FROM sensors WHERE device_id = :d AND type = 'snmp_interface' "
                "AND impact = 'critical'"
            ),
            {"d": device_id},
        ):
            try:
                critical.add(int((r.config or {}).get("if_index")))
            except (TypeError, ValueError):
                continue
        names = await read.port_names(session, [device_id])
        port_names = {idx: n for (d, idx), n in names.items() if d == device_id}
    if "critical_sensor" not in criteria:  # [if-noise] the pre-upgrade meaning, a choice now
        critical = set()
    if imp is None and not critical:
        return PortInfo(port_names, None, time.monotonic())
    important = set(critical)
    if imp is not None:
        important |= imp.important_set(include_trunks)
    important -= {i for i, v in overrides.items() if v is False}  # the user's "not important"
    return PortInfo(port_names, important, time.monotonic())


_TRAP_INSERT = text(
    """
    INSERT INTO traps (time, source_ip, agent_addr, device_id, version, pdu, security_name,
        trap_oid, trap_name, uptime_ticks, varbinds, summary, matched_rule_ids)
    SELECT x.t, CAST(x.s AS inet), CAST(x.a AS inet), x.d, x.v, x.p, x.sn, x.o, x.n, x.u,
           CAST(x.vb AS jsonb), x.sm, CAST(x.r AS integer[])
    FROM unnest(CAST(:t AS timestamptz[]), CAST(:s AS text[]), CAST(:a AS text[]),
                CAST(:d AS integer[]), CAST(:v AS text[]), CAST(:p AS text[]),
                CAST(:sn AS text[]), CAST(:o AS text[]), CAST(:n AS text[]),
                CAST(:u AS bigint[]), CAST(:vb AS text[]), CAST(:sm AS text[]),
                CAST(:r AS text[]))
         WITH ORDINALITY AS x(t, s, a, d, v, p, sn, o, n, u, vb, sm, r, ord)
    ORDER BY x.ord
    RETURNING id
    """
)
_SYSLOG_INSERT = text(
    """
    INSERT INTO syslog_messages (time, device_time, source_ip, device_id, transport, format,
        facility, severity, hostname, program, pid, msgid, structured_data, message, truncated,
        matched_rule_ids, filter_rule_id)
    SELECT x.t, x.dt, CAST(x.s AS inet), x.d, x.tr, x.f, x.fa, x.se, x.h, x.pr, x.pi, x.mi,
           x.sd, x.m, x.tc, CAST(x.r AS integer[]), x.fr
    FROM unnest(CAST(:t AS timestamptz[]), CAST(:dt AS timestamptz[]), CAST(:s AS text[]),
                CAST(:d AS integer[]), CAST(:tr AS text[]), CAST(:f AS text[]),
                CAST(:fa AS smallint[]), CAST(:se AS smallint[]), CAST(:h AS text[]),
                CAST(:pr AS text[]), CAST(:pi AS text[]), CAST(:mi AS text[]),
                CAST(:sd AS text[]), CAST(:m AS text[]), CAST(:tc AS boolean[]),
                CAST(:r AS text[]), CAST(:fr AS integer[]))
         WITH ORDINALITY AS x(t, dt, s, d, tr, f, fa, se, h, pr, pi, mi, sd, m, tc, r, fr, ord)
    ORDER BY x.ord
    RETURNING id
    """
)
# [syslog-protection] dedup repeat counts (absolute values; (id, time) = the hypertable key)
_REPEAT_UPDATE = text(
    """
    UPDATE syslog_messages m SET repeat_count = x.c, last_seen = x.ls
    FROM unnest(CAST(:i AS bigint[]), CAST(:t AS timestamptz[]), CAST(:c AS integer[]),
                CAST(:ls AS timestamptz[])) AS x(i, t, c, ls)
    WHERE m.id = x.i AND m.time = x.t
    """
)
_HITS_UPDATE = text(
    """
    UPDATE syslog_filters f SET hits = f.hits + x.n,
        last_hit_at = GREATEST(f.last_hit_at, x.t)
    FROM unnest(CAST(:i AS integer[]), CAST(:n AS bigint[]), CAST(:t AS timestamptz[]))
        AS x(i, n, t)
    WHERE f.id = x.i
    """
)
# "keep newest N" of a filter rule per source: only the rows stored under that rule (partial
# index ix_syslog_filter_rule), so the work is O(N) per write
_PRUNE_KEPT = text(
    "DELETE FROM syslog_messages t USING ("
    "  SELECT id, time FROM syslog_messages WHERE filter_rule_id = :r"
    "  AND source_ip = CAST(:s AS inet) ORDER BY time DESC, id DESC OFFSET :n"
    ") old WHERE t.id = old.id AND t.time = old.time"
)


def _int_array(values: list[int]) -> str:
    return "{" + ",".join(str(int(v)) for v in values) + "}"


async def insert_records(rt: Any, kind: str, records: list[dict[str, Any]]) -> list[int]:
    if kind == "trap":
        params = {
            "t": [r["time"] for r in records],
            "s": [r["source_ip"] for r in records],
            "a": [r["agent_addr"] for r in records],
            "d": [r["device_id"] for r in records],
            "v": [r["version"] for r in records],
            "p": [r["pdu"] for r in records],
            "sn": [r["security_name"] for r in records],
            "o": [r["trap_oid"] for r in records],
            "n": [r["trap_name"] for r in records],
            "u": [r["uptime_ticks"] for r in records],
            "vb": [json.dumps(r["varbinds"], ensure_ascii=False) for r in records],
            "sm": [r["summary"] for r in records],
            "r": [_int_array(r["matched_rule_ids"]) for r in records],
        }
        stmt = _TRAP_INSERT
    else:
        params = {
            "t": [r["time"] for r in records],
            "dt": [r["device_time"] for r in records],
            "s": [r["source_ip"] for r in records],
            "d": [r["device_id"] for r in records],
            "tr": [r["transport"] for r in records],
            "f": [r["format"] for r in records],
            "fa": [r["facility"] for r in records],
            "se": [r["severity"] for r in records],
            "h": [r["hostname"] for r in records],
            "pr": [r["program"] for r in records],
            "pi": [r["pid"] for r in records],
            "mi": [r["msgid"] for r in records],
            "sd": [r["structured_data"] for r in records],
            "m": [r["message"] for r in records],
            "tc": [r["truncated"] for r in records],
            "r": [_int_array(r["matched_rule_ids"]) for r in records],
            "fr": [r.get("filter_rule_id") for r in records],
        }
        stmt = _SYSLOG_INSERT
    async with rt.sessionmaker() as session:
        ids = [int(x) for x in (await session.execute(stmt, params)).scalars()]
        await session.commit()
    return ids
