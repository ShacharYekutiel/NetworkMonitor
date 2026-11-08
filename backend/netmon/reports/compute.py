"""[3] Reports: pure interval / availability / schedule maths (architecture § 13.6.1–13.6.2,
data-model § Report vectors).

All datetimes are timezone-aware. Schedules are computed with `zoneinfo` (DST-safe): a local
wall-clock time is converted to UTC per date."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

EXCLUDED = frozenset({"paused", "unknown", "unreachable"})
PROBLEM = "down"
MAX_ON_DEMAND_DAYS = 92

Interval = tuple[datetime, datetime]


# ---------------------------------------------------------------- intervals
def merge_intervals(items: Iterable[Interval]) -> list[Interval]:
    out: list[list[datetime]] = []
    for s, e in sorted(i for i in items if i[1] > i[0]):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def clip(items: Iterable[Interval], start: datetime, end: datetime) -> list[Interval]:
    return [(max(s, start), min(e, end)) for s, e in items if e > start and s < end]


def overlap_s(seg: Interval, merged: Sequence[Interval]) -> float:
    s, e = seg
    total = 0.0
    for ms, me in merged:
        if me <= s:
            continue
        if ms >= e:
            break
        total += (min(e, me) - max(s, ms)).total_seconds()
    return total


def status_segments(
    initial: str | None,
    changes: Sequence[tuple[datetime, str | None]],
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, datetime, str]]:
    """Status per time inside [start, end): `initial` (the new_status of the last change before
    `start`; None → `unknown`) then each change (sorted, inside the period)."""
    segs: list[tuple[datetime, datetime, str]] = []
    cur_t, cur = start, (initial or "unknown")
    for t, st in sorted((c for c in changes if start <= c[0] < end), key=lambda c: c[0]):
        if t > cur_t:
            segs.append((cur_t, t, cur))
        cur_t, cur = t, (st or "unknown")
    if end > cur_t:
        segs.append((cur_t, end, cur))
    return segs


@dataclass
class Availability:
    measured_s: float
    downtime_s: float
    outages: int
    longest_outage_s: float

    @property
    def availability(self) -> float | None:
        return percent(self.measured_s - self.downtime_s, self.measured_s)


def percent(num: float, den: float) -> float | None:
    if den <= 0:
        return None
    value = Decimal(str(num * 100.0 / den)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(value)


def availability(
    initial: str | None,
    changes: Sequence[tuple[datetime, str | None]],
    maintenance: Iterable[Interval],
    start: datetime,
    end: datetime,
) -> Availability:
    """§ 13.6.2: measured = period − paused − unknown − unreachable − in maintenance;
    downtime = `down` outside maintenance; `warning` is available. An outage is a maximal
    `down` run with downtime > 0 inside the period (one that started before the period
    counts; its length is the part within the period)."""
    maint = merge_intervals(clip(maintenance, start, end))
    measured = downtime = 0.0
    outages = 0
    longest = 0.0
    run = 0.0
    in_run = False
    for s, e, st in status_segments(initial, changes, start, end):
        length = (e - s).total_seconds()
        if st in EXCLUDED:  # an excluded state ends a down run
            if in_run:
                if run > 0:
                    outages += 1
                    longest = max(longest, run)
                in_run, run = False, 0.0
            continue
        m = overlap_s((s, e), maint)
        measured += length - m
        if st == PROBLEM:
            downtime += length - m
            in_run = True
            run += length - m
        elif in_run:
            if run > 0:
                outages += 1
                longest = max(longest, run)
            in_run, run = False, 0.0
    if in_run and run > 0:
        outages += 1
        longest = max(longest, run)
    return Availability(measured, downtime, outages, longest)


def group_availability(items: Iterable[Availability]) -> float | None:
    """Time-weighted mean of the measured devices."""
    measured = down = 0.0
    for a in items:
        if a.measured_s > 0:
            measured += a.measured_s
            down += a.downtime_s
    return percent(measured - down, measured)


def maintenance_intervals(
    events: Sequence[tuple[datetime, str, str]], start: datetime, end: datetime
) -> list[Interval]:
    """Pair maintenance `started`/`ended` events (time, key, verb) per window key; a window
    still open at `end` lasts until `end`; an `ended` without `started` covers from `start`."""
    open_: dict[str, datetime] = {}
    out: list[Interval] = []
    for t, key, verb in sorted(events, key=lambda x: x[0]):
        if verb == "started":
            open_.setdefault(key, t)
        elif verb == "ended":
            s = open_.pop(key, None)
            out.append((s if s is not None else start, t))
    out.extend((s, end) for s in open_.values())
    return merge_intervals(clip(out, start, end))


def fmt_duration(seconds: float) -> str:
    """h:mm (downtime)."""
    m = int(round(seconds / 60.0))
    return f"{m // 60}:{m % 60:02d}"


# ---------------------------------------------------------------- schedule
def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def _local(d: date, t: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, t, tzinfo=tz)


def next_run(
    schedule: str,
    weekday: int | None,
    month_day: int | None,
    send_time: str | None,
    tz_name: str,
    after: datetime,
) -> datetime | None:
    """Next run strictly after `after` (UTC result)."""
    if schedule not in ("weekly", "monthly") or not send_time:
        return None
    tz = ZoneInfo(tz_name)
    t = parse_hhmm(send_time)
    local_after = after.astimezone(tz)
    d = local_after.date()
    if schedule == "weekly":
        assert weekday is not None
        d = d + timedelta(days=(weekday - d.isoweekday()) % 7)
        for _ in range(3):
            cand = _local(d, t, tz).astimezone(UTC)
            if cand > after:
                return cand
            d = d + timedelta(days=7)
        return None
    assert month_day is not None
    y, m = d.year, d.month
    for _ in range(3):
        cand = _local(date(y, m, month_day), t, tz).astimezone(UTC)
        if cand > after:
            return cand
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return None


def period_for(schedule: str, run_at: datetime, tz_name: str) -> tuple[datetime, datetime]:
    """weekly → the previous full ISO week (Mon 00:00 → Mon 00:00 local); monthly → the
    previous calendar month; `none` → like weekly (on-demand default)."""
    tz = ZoneInfo(tz_name)
    local = run_at.astimezone(tz)
    if schedule == "monthly":
        first = date(local.year, local.month, 1)
        prev = (
            date(first.year - 1, 12, 1)
            if first.month == 1
            else date(first.year, first.month - 1, 1)
        )
        return (
            _local(prev, time(0), tz).astimezone(UTC),
            _local(first, time(0), tz).astimezone(UTC),
        )
    monday = local.date() - timedelta(days=local.isoweekday() - 1)
    prev = monday - timedelta(days=7)
    return (
        _local(prev, time(0), tz).astimezone(UTC),
        _local(monday, time(0), tz).astimezone(UTC),
    )


def period_label(start: datetime, end: datetime, tz_name: str) -> str:
    """`03.11–09.11.2026` (end shown inclusive: the last day of the period)."""
    tz = ZoneInfo(tz_name)
    s = start.astimezone(tz)
    e = (end - timedelta(seconds=1)).astimezone(tz)
    if s.year == e.year:
        return f"{s:%d.%m}–{e:%d.%m.%Y}"
    return f"{s:%d.%m.%Y}–{e:%d.%m.%Y}"
