"""[2b-3] Port flapping detection (architecture section 12.4): pure `FlapTracker`."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

DUPLICATE_S = 60.0
MAX_TIMES = 64
MAX_PORTS = 10000

Port = tuple[int, int]  # (device_id, if_index)


@dataclass
class FlapChange:
    kind: str  # start | update | end
    port: Port
    count: int  # transitions within the window
    at: float


@dataclass
class _State:
    times: deque[float] = field(default_factory=lambda: deque(maxlen=MAX_TIMES))
    last_dir: str | None = None
    last_t: float | None = None
    flapping: bool = False


class FlapTracker:
    """Times are seconds (any epoch). `add()` reports start/update; `expire(now)` reports
    ports whose quiet period is over."""

    def __init__(self, threshold: int = 5, window_min: float = 10, quiet_min: float = 15) -> None:
        self.threshold = threshold
        self.window_s = window_min * 60.0
        self.quiet_s = quiet_min * 60.0
        self._ports: OrderedDict[Port, _State] = OrderedDict()

    def configure(self, threshold: int, window_min: float, quiet_min: float) -> None:
        self.threshold = threshold
        self.window_s = window_min * 60.0
        self.quiet_s = quiet_min * 60.0

    def flapping(self) -> list[Port]:
        return [p for p, s in self._ports.items() if s.flapping]

    def is_flapping(self, port: Port) -> bool:
        s = self._ports.get(port)
        return bool(s and s.flapping)

    def clear(self) -> None:
        self._ports.clear()

    def forget(self, port: Port) -> None:
        self._ports.pop(port, None)

    def mark_flapping(self, port: Port, last_t: float) -> None:
        """Restore a flapping port (a persisted condition after a restart)."""
        s = self._state(port)
        s.flapping = True
        s.last_t = last_t

    def _state(self, port: Port) -> _State:
        s = self._ports.get(port)
        if s is None:
            s = _State()
            self._ports[port] = s
            if len(self._ports) > MAX_PORTS:
                self._ports.popitem(last=False)
        else:
            self._ports.move_to_end(port)
        return s

    def add(self, port: Port, direction: str, t: float) -> FlapChange | None:
        s = self._state(port)
        if s.last_dir == direction and s.last_t is not None and t - s.last_t < DUPLICATE_S:
            return None  # the same event seen by two sources
        s.last_dir = direction
        s.last_t = t
        s.times.append(t)
        while s.times and s.times[0] < t - self.window_s:
            s.times.popleft()
        count = len(s.times)
        if s.flapping:
            return FlapChange("update", port, count, t)
        if count >= self.threshold:
            s.flapping = True
            return FlapChange("start", port, count, t)
        return None

    def count(self, port: Port, now: float) -> int:
        s = self._ports.get(port)
        if s is None:
            return 0
        return sum(1 for x in s.times if x >= now - self.window_s)

    def expire(self, now: float) -> list[FlapChange]:
        out = []
        for port, s in list(self._ports.items()):
            if s.flapping and s.last_t is not None and now - s.last_t >= self.quiet_s:
                s.flapping = False
                s.times.clear()
                out.append(FlapChange("end", port, 0, s.last_t + self.quiet_s))
        return out

    def next_expiry(self) -> float | None:
        ends = [s.last_t + self.quiet_s for s in self._ports.values() if s.flapping and s.last_t]
        return min(ends) if ends else None


# ------------------------------------------------------------------ I/O (rt.flapping)
class FlapManager:
    """Feeds transitions into the tracker while the `flapping` feature is ON, keeps the flap
    conditions on the `port_flap` event sensors and the alert suppression set
    (`suppressed_sensors`: snmp_interface sensor ids of flapping ports)."""

    def __init__(self, rt: Any) -> None:
        import asyncio

        self.rt = rt
        self.tracker = FlapTracker()
        self.suppressed: dict[Port, set[int]] = {}
        self.suppressed_sensors: set[int] = set()
        self.enabled = False
        self._lock = asyncio.Lock()
        self._names: dict[Port, str] = {}

    def _now(self) -> float:
        return float(self.rt.clock.now().timestamp())

    async def configure(self) -> None:
        from netmon.services import settings as settings_svc

        async with self.rt.sessionmaker() as session:
            cfg = await settings_svc.get_all(session)
        self.tracker.configure(
            int(cfg["flap_threshold"]), float(cfg["flap_window_min"]), float(cfg["flap_quiet_min"])
        )

    async def start(self) -> None:
        """Feature ON (or startup): restore flapping ports from persisted conditions."""
        from sqlalchemy import text

        await self.configure()
        self.enabled = True
        async with self.rt.sessionmaker() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT s.device_id, c.key, c.last_at FROM event_conditions c "
                        "JOIN sensors s ON s.id = c.sensor_id WHERE c.source = 'flap'"
                    )
                )
            ).all()
            for r in rows:
                try:
                    idx = int(r.key)
                except ValueError:
                    continue
                from netmon.ingest.event_sensors import interface_sensor_ids

                port = (int(r.device_id), idx)
                self.tracker.mark_flapping(port, float(r.last_at.timestamp()))
                ids = set(await interface_sensor_ids(session, port[0], idx))
                self.suppressed[port] = ids
        self._rebuild_sensor_set()

    async def stop(self, *, clear: bool = True) -> None:
        """Feature OFF: no tracking; flap conditions deleted, suppression emptied with re-arm."""
        self.enabled = False
        ids = set(self.suppressed_sensors)
        self.tracker.clear()
        self.suppressed.clear()
        self.suppressed_sensors.clear()
        if not clear:
            return
        es = getattr(self.rt, "event_sensors", None)
        if es is not None:
            await es.flap_conditions_off()
        if self.rt.alerts is not None:
            await self.rt.alerts.left_flapping(None if not ids else sorted(ids), all_rows=True)

    def _rebuild_sensor_set(self) -> None:
        self.suppressed_sensors = {s for ids in self.suppressed.values() for s in ids}

    def is_flapping(self, device_id: int, if_index: int) -> bool:
        return self.enabled and self.tracker.is_flapping((device_id, if_index))

    def transition(
        self, device_id: int, if_index: int, up: bool, port_name: str | None = None
    ) -> FlapChange | None:
        """Non-blocking: feed one transition; DB work for start/update runs in the
        background."""
        if not self.enabled:
            return None
        port = (device_id, if_index)
        if port_name:
            self._names[port] = port_name
            if len(self._names) > MAX_PORTS:
                self._names.pop(next(iter(self._names)))
        change = self.tracker.add(port, "up" if up else "down", self._now())
        if change is not None:
            self.rt.spawn(self._apply(change))
        return change

    async def _apply(self, change: FlapChange) -> None:
        es = getattr(self.rt, "event_sensors", None)
        if es is None:
            return
        device_id, idx = change.port
        name = self._names.get(change.port) or await self._port_name(device_id, idx)
        window = int(self.tracker.window_s // 60)
        msg = f"Port {name} is flapping ({change.count} changes in {window} min)"
        ingest = getattr(self.rt, "ingest", None)
        if ingest is not None and not await ingest.is_important_port(device_id, idx):
            return  # event_linkdown_scope = important: flapping alerts for important ports only
        async with self._lock:
            if not self.enabled:
                return
            raised, iface = await es.flap_condition(
                device_id, idx, msg, change.count, self.tracker.quiet_s / 60.0
            )
            if change.kind == "start":
                await es.link_down_cleared(device_id, idx)
                self.suppressed[change.port] = set(iface)
                self._rebuild_sensor_set()
            elif change.port not in self.suppressed:
                self.suppressed[change.port] = set(iface)
                self._rebuild_sensor_set()

    async def _port_name(self, device_id: int, if_index: int) -> str:
        from netmon.ports.read import port_names

        async with self.rt.sessionmaker() as session:
            names = await port_names(session, [device_id])
        return names.get((device_id, if_index)) or str(if_index)

    async def conditions_expired(self, items: list[tuple[int, str]]) -> None:
        """Flap conditions expired (quiet period over): the port leaves `suppressed` and its
        suppressed alerts are re-armed once."""
        from sqlalchemy import text

        if not items:
            return
        async with self.rt.sessionmaker() as session:
            sensor_dev = {
                r.id: r.device_id
                for r in await session.execute(
                    text("SELECT id, device_id FROM sensors WHERE id = ANY(:ids)"),
                    {"ids": sorted({s for s, _ in items})},
                )
            }
        rearm: set[int] = set()
        for sid, key in items:
            dev = sensor_dev.get(sid)
            try:
                port = (int(dev), int(key)) if dev is not None else None
            except ValueError:
                port = None
            if port is None:
                continue
            self.tracker.forget(port)
            rearm |= self.suppressed.pop(port, set())
        self._rebuild_sensor_set()
        if rearm and self.rt.alerts is not None:
            await self.rt.alerts.left_flapping(sorted(rearm))

    async def sensor_cleared(self, device_id: int) -> None:
        """Manual clear of a port_flap sensor: its ports stop flapping."""
        ports = [p for p in self.suppressed if p[0] == device_id]
        rearm: set[int] = set()
        for p in ports:
            self.tracker.forget(p)
            rearm |= self.suppressed.pop(p, set())
        self._rebuild_sensor_set()
        if rearm and self.rt.alerts is not None:
            await self.rt.alerts.left_flapping(sorted(rearm))
