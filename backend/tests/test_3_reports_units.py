"""[3] Reports: pure availability / schedule vectors (data-model § Report vectors)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from netmon.reports import compute
from netmon.reports.compute import availability, group_availability, next_run, period_for

TZ = "Asia/Jerusalem"
START = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
END = START + timedelta(hours=168)


def h(n: float) -> datetime:
    return START + timedelta(hours=n)


def test_vector_1_simple_outage() -> None:
    a = availability("up", [(h(10), "down"), (h(12), "up")], [], START, END)
    assert a.measured_s == 168 * 3600
    assert a.downtime_s == 2 * 3600
    assert a.availability == 98.81
    assert a.outages == 1 and a.longest_outage_s == 2 * 3600


def test_vector_2_maintenance_excluded() -> None:
    a = availability("up", [(h(10), "down"), (h(12), "up")], [(h(11), h(13))], START, END)
    assert a.measured_s == 166 * 3600
    assert a.downtime_s == 3600
    assert a.availability == 99.40


def test_vector_3_outage_before_period_and_unreachable() -> None:
    a = availability("down", [(h(1), "unreachable"), (h(5), "up")], [], START, END)
    assert a.measured_s == 164 * 3600
    assert a.downtime_s == 3600
    assert a.availability == 99.39
    assert a.outages == 1 and a.longest_outage_s == 3600


def test_vector_4_sensor_created_late() -> None:
    a = availability(None, [(h(100), "up")], [], START, END)
    assert a.measured_s == 68 * 3600
    assert a.availability == 100.0


def test_vector_5_paused_whole_period() -> None:
    a = availability("paused", [], [], START, END)
    assert a.measured_s == 0
    assert a.availability is None


def test_vector_6_warning_is_available() -> None:
    a = availability("up", [(h(10), "warning"), (h(30), "up")], [], START, END)
    assert a.availability == 100.0


def test_group_time_weighted() -> None:
    v1 = availability("up", [(h(10), "down"), (h(12), "up")], [], START, END)
    v4 = availability(None, [(h(100), "up")], [], START, END)
    v5 = availability("paused", [], [], START, END)
    assert group_availability([v1, v4, v5]) == 99.15
    assert group_availability([v5]) is None


def test_outage_split_by_maintenance_counts_once_and_two_outages() -> None:
    a = availability(
        "up",
        [(h(1), "down"), (h(2), "up"), (h(3), "down"), (h(6), "up")],
        [(h(4), h(5))],
        START,
        END,
    )
    assert a.outages == 2
    assert a.downtime_s == 3 * 3600  # 1 h + (3 h - 1 h maintenance)
    assert a.longest_outage_s == 2 * 3600
    # an outage entirely inside maintenance is not an outage
    b = availability("up", [(h(1), "down"), (h(2), "up")], [(h(0), h(3))], START, END)
    assert b.outages == 0 and b.downtime_s == 0


def test_maintenance_interval_pairing() -> None:
    ev = [
        (h(11), "device:3:A", "started"),
        (h(13), "device:3:A", "ended"),
        (h(150), "group:1:B", "started"),
        (h(1), "global::C", "ended"),  # started before the period
    ]
    got = compute.maintenance_intervals(ev, START, END)
    assert got == [(START, h(1)), (h(11), h(13)), (h(150), END)]


def test_schedule_weekly_vector() -> None:
    created = datetime(2026, 11, 8, 12, 0, tzinfo=ZoneInfo(TZ))  # Sunday local
    nxt = next_run("weekly", 1, None, "07:47", TZ, created)
    assert nxt == datetime(2026, 11, 9, 5, 47, tzinfo=UTC)
    s, e = period_for("weekly", nxt, TZ)
    assert s == datetime(2026, 11, 1, 22, 0, tzinfo=UTC)  # 2026-11-02T00:00+02
    assert e == datetime(2026, 11, 8, 22, 0, tzinfo=UTC)
    # a run exactly at the time is not "next" again
    assert next_run("weekly", 1, None, "07:47", TZ, nxt) == nxt + timedelta(days=7)


def test_schedule_monthly_dst_vector() -> None:
    after = datetime(2026, 3, 31, 12, 0, tzinfo=UTC)
    nxt = next_run("monthly", None, 1, "07:47", TZ, after)
    assert nxt == datetime(2026, 4, 1, 4, 47, tzinfo=UTC)  # IDT +3
    s, e = period_for("monthly", nxt, TZ)
    assert s == datetime(2026, 2, 28, 22, 0, tzinfo=UTC)  # 2026-03-01T00:00+02
    assert e == datetime(2026, 3, 31, 21, 0, tzinfo=UTC)  # 2026-04-01T00:00+03
    assert (e - s) == timedelta(hours=743)


def test_schedule_none_and_year_wrap() -> None:
    assert next_run("none", None, None, None, TZ, START) is None
    nxt = next_run("monthly", None, 28, "23:59", "UTC", datetime(2026, 12, 29, tzinfo=UTC))
    assert nxt == datetime(2027, 1, 28, 23, 59, tzinfo=UTC)
    s, e = period_for("monthly", datetime(2027, 1, 1, 8, tzinfo=UTC), "UTC")
    assert (s, e) == (datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))


@pytest.mark.parametrize(
    ("sec", "text"), [(0, "0:00"), (59, "0:01"), (3600 * 25 + 60 * 5, "25:05")]
)
def test_fmt_duration(sec: float, text: str) -> None:
    assert compute.fmt_duration(sec) == text


def test_period_label() -> None:
    s, e = period_for("weekly", datetime(2026, 11, 9, 5, 47, tzinfo=UTC), TZ)
    assert compute.period_label(s, e, TZ) == "02.11–08.11.2026"
