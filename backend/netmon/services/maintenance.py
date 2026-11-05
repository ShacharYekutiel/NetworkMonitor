"""[2b-1] Maintenance windows: pure occurrence/coverage functions and the tracker loop.

See docs/data-model.md "Maintenance windows". Time comes only from `rt.clock.now()`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.maintenance")

LOOP_MAX_S = 30.0
LOOKAHEAD = timedelta(days=8)

Occurrence = tuple[datetime, datetime]


# ------------------------------------------------------------------ time zones
def valid_timezone(name: Any) -> bool:
    if not isinstance(name, str) or not name or len(name) > 64:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def default_timezone() -> str:
    """`TZ` when it is a valid IANA name, else UTC."""
    tz = (os.environ.get("TZ") or "").strip().lstrip(":")
    return tz if valid_timezone(tz) else "UTC"


def zone(name: str | ZoneInfo | None) -> ZoneInfo:
    if isinstance(name, ZoneInfo):
        return name
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo("UTC")


# ------------------------------------------------------------------ pure
def _g(w: Any, key: str) -> Any:
    return w.get(key) if isinstance(w, dict) else getattr(w, key, None)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _start_time(v: Any) -> time:
    if isinstance(v, time):
        return v.replace(second=0, microsecond=0, tzinfo=None)
    hh, mm = str(v).split(":")[:2]
    return time(int(hh), int(mm))


def occurrences(
    window: Any, tz: str | ZoneInfo | None, start: datetime, end: datetime
) -> list[Occurrence]:
    """Occurrences `[s, e)` (UTC) that overlap `[start, end)`, sorted by start. `enabled` is
    not considered here."""
    start, end = _utc(start), _utc(end)
    if end <= start:
        return []
    kind = _g(window, "kind")
    if kind == "once":
        s, e = _g(window, "starts_at"), _g(window, "ends_at")
        if s is None or e is None:
            return []
        s, e = _utc(s), _utc(e)
        return [(s, e)] if s < end and e > start else []
    if kind != "weekly":
        return []
    zi = zone(tz)
    weekdays = set(_g(window, "weekdays") or [])
    duration = timedelta(minutes=int(_g(window, "duration_min") or 0))
    if not weekdays or duration <= timedelta(0) or _g(window, "start_time") is None:
        return []
    st = _start_time(_g(window, "start_time"))
    first: date = (start - duration).astimezone(zi).date() - timedelta(days=1)
    last: date = end.astimezone(zi).date() + timedelta(days=1)
    out: list[Occurrence] = []
    d = first
    while d <= last:
        if d.isoweekday() in weekdays:
            # zoneinfo, fold=0: a start inside a DST gap lands after the gap
            s = datetime.combine(d, st, tzinfo=zi).astimezone(UTC)
            e = s + duration  # real minutes
            if s < end and e > start:
                out.append((s, e))
        d += timedelta(days=1)
    return out


def covering(window: Any, tz: str | ZoneInfo | None, t: datetime) -> Occurrence | None:
    """The occurrence covering t with the latest end (None when not active). Ignores
    `enabled`."""
    t = _utc(t)
    occ = [o for o in occurrences(window, tz, t, t + timedelta(microseconds=1)) if o[0] <= t]
    return max(occ, key=lambda o: o[1]) if occ else None


def covers(window: Any, tz: str | ZoneInfo | None, t: datetime) -> bool:
    """A window is active at t when it is enabled and some occurrence has start <= t < end."""
    return bool(_g(window, "enabled")) and covering(window, tz, t) is not None


def current_or_next(window: Any, tz: str | ZoneInfo | None, now: datetime) -> Occurrence | None:
    """The current occurrence if active (latest end), else the next one; None = none left."""
    cur = covering(window, tz, now)
    if cur is not None:
        return cur
    now = _utc(now)
    horizon = now + (timedelta(days=8) if _g(window, "kind") == "weekly" else timedelta(days=36600))
    nxt = [o for o in occurrences(window, tz, now, horizon) if o[0] > now]
    return nxt[0] if nxt else None


def window_covers_device(window: Any, device_id: int, group_chain: list[int]) -> bool:
    """Scope check: global, the device, or its group or any ancestor (`group_chain`)."""
    scope = _g(window, "scope")
    if scope == "global":
        return True
    if scope == "device":
        return _g(window, "device_id") == device_id
    if scope == "group":
        return _g(window, "group_id") in group_chain
    return False


def boundaries(windows: list[Any], tz: str | ZoneInfo | None, now: datetime) -> list[datetime]:
    """Occurrence starts/ends after `now` within the look-ahead (enabled windows)."""
    out: list[datetime] = []
    for w in windows:
        if not _g(w, "enabled"):
            continue
        for s, e in occurrences(w, tz, now, now + LOOKAHEAD):
            out += [x for x in (s, e) if x > now]
    return sorted(out)


def describe_weekly(weekdays: list[int], start_time: Any, duration_min: int) -> str:
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    st = _start_time(start_time)
    days = ",".join(names[d - 1] for d in sorted(weekdays))
    return f"Every {days} {st.hour:02d}:{st.minute:02d}, {duration_min} min"


# ------------------------------------------------------------------ tracker
class MaintenanceTracker:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.tz = "UTC"
        self.windows: list[dict[str, Any]] = []
        self._covered: dict[int, tuple[int, str, datetime]] = {}
        self._active: dict[int, Occurrence] = {}
        self._names: dict[int, dict[str, Any]] = {}
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._initialized = False

    # ---------------------------------------------------------------- queries (sync)
    def in_maintenance(self, device_id: int | None) -> bool:
        return device_id is not None and device_id in self._covered

    def active_window_ids(self) -> list[int]:
        return sorted(self._active)

    def window_for(self, device_id: int) -> tuple[int, str, datetime] | None:
        return self._covered.get(device_id)

    def covered_device_ids(self) -> list[int]:
        return sorted(self._covered)

    def maintenance_ref(self, device_id: int) -> dict[str, Any] | None:
        from netmon.services.serialize import iso

        w = self._covered.get(device_id)
        if w is None:
            return None
        return {"window_id": w[0], "name": w[1], "ends_at": iso(w[2])}

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        await self.reload()
        self._task = asyncio.create_task(self._loop(), name="netmon-maintenance")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    def request(self) -> None:
        """Re-evaluate soon (device/group changes)."""
        self._wake.set()

    async def reload(self) -> None:
        """Reload windows and the time zone, then re-evaluate (after every mutation)."""
        from netmon.services import settings as settings_svc

        async with self.rt.sessionmaker() as session:
            self.tz = str(await settings_svc.get(session, "timezone"))
            rows = (
                await session.execute(
                    text(
                        "SELECT id, name, enabled, scope, group_id, device_id, kind, starts_at, "
                        "ends_at, weekdays, start_time, duration_min FROM maintenance_windows"
                    )
                )
            ).all()
        self.windows = [dict(r._mapping) for r in rows]  # noqa: SLF001
        await self.evaluate()
        self._wake.set()

    async def evaluate(self) -> None:
        async with self._lock:
            await self._evaluate()

    async def _evaluate(self) -> None:
        now = self.rt.clock.now()
        active: dict[int, Occurrence] = {}
        for w in self.windows:
            if not w["enabled"]:
                continue
            occ = covering(w, self.tz, now)
            if occ is not None:
                active[w["id"]] = occ
        async with self.rt.sessionmaker() as session:
            groups = {
                r.id: r.parent_id
                for r in await session.execute(text("SELECT id, parent_id FROM groups"))
            }
            devices = (await session.execute(text("SELECT id, group_id, name FROM devices"))).all()
        covered: dict[int, tuple[int, str, datetime]] = {}
        if active:
            by_id = {w["id"]: w for w in self.windows}
            for d in devices:
                chain = _chain(groups, d.group_id)
                best: tuple[int, str, datetime] | None = None
                for wid, (_, end) in active.items():
                    w = by_id[wid]
                    if window_covers_device(w, d.id, chain) and (best is None or end > best[2]):
                        best = (wid, w["name"], end)
                if best is not None:
                    covered[d.id] = best
        prev_active, prev_covered = self._active, self._covered
        started = [wid for wid in active if wid not in prev_active]
        ended = [wid for wid in prev_active if wid not in active]
        entered = sorted(set(covered) - set(prev_covered))
        left = sorted(set(prev_covered) - set(covered))
        names = {
            w["id"]: {k: w[k] for k in ("name", "scope", "group_id", "device_id")}
            for w in self.windows
        }
        old_names = self._names
        self._active, self._covered = active, covered
        self._names = {**old_names, **names}
        initial = not self._initialized
        self._initialized = True
        if not initial and (started or ended):
            await self._write_events(started, ended, devices, now)
        if not initial and (started or ended or entered or left):
            self.rt.hub.broadcast(
                "maintenance.changed",
                {
                    "active_window_ids": sorted(active),
                    "entered_device_ids": entered,
                    "left_device_ids": left,
                },
            )
            self.rt.request_rollup()
        if self.rt.alerts is not None and (initial or left):
            # rows suppressed by a window that has ended (also across a restart)
            await self.rt.alerts.left_maintenance()
        self._names = {k: v for k, v in self._names.items() if k in names}

    async def _write_events(
        self, started: list[int], ended: list[int], devices: Any, now: datetime
    ) -> None:
        from netmon.services import events as events_svc

        dev_names = {d.id: d.name for d in devices}
        evs = []
        try:
            async with self.rt.sessionmaker() as session:
                for wid, verb in [(w, "started") for w in started] + [(w, "ended") for w in ended]:
                    info = self._names.get(wid) or {"name": f"#{wid}", "scope": "global"}
                    fields: dict[str, Any] = {"time": now}
                    if info.get("scope") == "device" and info.get("device_id") in dev_names:
                        fields["device_id"] = info["device_id"]
                        fields["device_name"] = dev_names[info["device_id"]]
                    if info.get("scope") == "group" and info.get("group_id") is not None:
                        gid = info["group_id"]
                        exists = (
                            await session.execute(
                                text("SELECT 1 FROM groups WHERE id = :g"), {"g": gid}
                            )
                        ).first()
                        if exists:
                            fields["group_id"] = gid
                    evs.append(
                        await events_svc.add_event(
                            session,
                            "maintenance",
                            f'Maintenance "{info["name"]}" {verb}',
                            **fields,
                        )
                    )
                await session.commit()
        except Exception:  # noqa: BLE001
            log.exception("Cannot write maintenance events")
            return
        events_svc.published(self.rt, evs)

    async def _loop(self) -> None:
        while True:
            now = self.rt.clock.now()
            nxt = boundaries(self.windows, self.tz, now)
            timeout = LOOP_MAX_S
            if nxt:
                timeout = min(timeout, max(0.05, (nxt[0] - now).total_seconds() + 0.05))
            changed = self.rt.clock.changed
            self._wake.clear()
            waiters = [
                asyncio.ensure_future(self._wake.wait()),
                asyncio.ensure_future(changed.wait()),
            ]
            try:
                await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()
            try:
                await self.evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Maintenance evaluation failed")


def _chain(groups: dict[int, int | None], gid: int | None) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    while gid is not None and gid in groups and gid not in seen:
        out.append(gid)
        seen.add(gid)
        gid = groups[gid]
    return out
