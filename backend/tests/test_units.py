"""Pure unit tests (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from netmon.api.errors import ApiError
from netmon.poller.checks.ping import parse_ping_output, result_from_rtts
from netmon.poller.checks.snmp_interface import WRAP32, compute_rates, counter_delta
from netmon.poller.sensor_types import validate_channel_overrides, validate_config
from netmon.poller.types import CheckResult, channel_status, evaluate
from netmon.services.history import bucket_for_span
from netmon.services.status import Inventory, device_status, group_status

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "t", "expected"),
    [
        (None, {}, "unknown"),
        (5, {}, "up"),
        (150, {"warning_max": 100, "down_max": 500}, "warning"),
        (600, {"warning_max": 100, "down_max": 500}, "down"),
        (100, {"warning_max": 100, "down_max": 500}, "up"),  # strictly greater
        (10, {"warning_min": 14, "down_min": 3}, "warning"),
        (2, {"warning_min": 14, "down_min": 3}, "down"),
        (2, {"down_max": 1}, "down"),  # oper_status != up
        (1, {"down_max": 1}, "up"),
    ],
)
def test_channel_status(value, t, expected) -> None:  # type: ignore[no-untyped-def]
    assert channel_status(value, t) == expected


def _defs():  # type: ignore[no-untyped-def]
    return [
        ("latency_ms", "ms", {"warning_max": 100, "down_max": 500}),
        ("packet_loss_pct", "%", {"warning_max": 10, "down_max": 50}),
    ]


def test_evaluate_worst_of_and_messages() -> None:
    ev = evaluate(CheckResult(True, "", {"latency_ms": 612, "packet_loss_pct": 0}), _defs(), 1, NOW)
    assert ev.status == "down" and ev.message == "latency_ms 612 > 500"
    ev = evaluate(
        CheckResult(True, "", {"latency_ms": 150, "packet_loss_pct": 20}), _defs(), 1, NOW
    )
    assert ev.status == "warning"
    assert ev.message == "latency_ms 150 > 100, packet_loss_pct 20 > 10"
    ev = evaluate(CheckResult(True, "ok", {"latency_ms": 1.5}), _defs(), 1, NOW)
    assert ev.status == "up" and ev.message == "ok"
    assert [c.status for c in ev.channels] == ["up", "unknown"]
    ev = evaluate(CheckResult(False, "Host unreachable"), _defs(), 1, NOW)
    assert ev.status == "down" and not ev.ok and ev.message == "Host unreachable"
    assert all(c.value is None and c.status == "unknown" for c in ev.channels)


def test_ping_parsing() -> None:
    linux = (
        "64 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=0.045 ms\n"
        "64 bytes from 127.0.0.1: icmp_seq=2 ttl=64 time=0.061 ms\n"
    )
    assert parse_ping_output(linux) == [0.045, 0.061]
    windows = (
        "Reply from 10.0.0.1: bytes=32 time=3ms TTL=64\n"
        "Reply from 10.0.0.1: bytes=32 time<1ms TTL=64"
    )
    assert parse_ping_output(windows) == [3.0, 1.0]
    r = result_from_rtts([1.0, 3.0], 4)
    assert r.ok and r.values == {"latency_ms": 2.0, "packet_loss_pct": 50.0}
    r = result_from_rtts([], 4)
    assert not r.ok and r.message == "Host unreachable"


def test_counter_delta() -> None:
    assert counter_delta(100, 300, hc=False) == 200
    assert counter_delta(WRAP32 - 100, 50, hc=False) == 150  # 32-bit wrap
    assert counter_delta(2**63, 10, hc=True) is None  # 64-bit reset


def test_compute_rates_first_poll_then_delta() -> None:
    c1 = {"in_octets": 1000, "out_octets": 2000, "in_errors": 0, "out_errors": 5}
    values, state = compute_rates({}, 1000.0, c1, hc=True, speed_bps=1e6)
    assert values == {}
    assert state == {"t": 1000.0, "hc": True, **c1}
    c2 = {
        "in_octets": 1000 + 125000 * 10,
        "out_octets": 2000 + 12500 * 10,
        "in_errors": 10,
        "out_errors": 5,
    }
    values, state2 = compute_rates(state, 1010.0, c2, hc=True, speed_bps=1e6)
    assert values["in_bps"] == 1_000_000.0
    assert values["out_bps"] == 100_000.0
    assert values["in_util_pct"] == 100.0 and values["out_util_pct"] == 10.0
    assert values["in_errors_ps"] == 1.0 and values["out_errors_ps"] == 0.0
    assert state2["t"] == 1010.0


def test_compute_rates_wrap_and_reset() -> None:
    s32 = {
        "t": 0.0,
        "hc": False,
        "in_octets": WRAP32 - 1000,
        "out_octets": 10,
        "in_errors": 0,
        "out_errors": 0,
    }
    v, _ = compute_rates(
        s32,
        10.0,
        {"in_octets": 1000, "out_octets": 20, "in_errors": 0, "out_errors": 0},
        hc=False,
        speed_bps=None,
    )
    assert v["in_bps"] == 2000 * 8 / 10
    assert "in_util_pct" not in v  # unknown speed
    s64 = {
        "t": 0.0,
        "hc": True,
        "in_octets": 10**12,
        "out_octets": 10,
        "in_errors": 0,
        "out_errors": 0,
    }
    v, st = compute_rates(
        s64,
        10.0,
        {"in_octets": 5, "out_octets": 20, "in_errors": 0, "out_errors": 0},
        hc=True,
        speed_bps=1e9,
    )
    assert "in_bps" not in v and v["out_bps"] == 8.0
    assert st["in_octets"] == 5  # new baseline
    # hc flag switched -> baseline only
    v, _ = compute_rates(s64, 10.0, {"in_octets": 5, "out_octets": 20}, hc=False, speed_bps=None)
    assert v == {}


def test_validate_config_defaults_and_errors() -> None:
    assert validate_config("ping", {}) == {"count": 4, "timeout_ms": 1000, "packet_size": 56}
    assert validate_config("dns", {"query_name": "example.com"})["record_type"] == "A"
    with pytest.raises(ApiError) as e:
        validate_config("tcp_port", {"port": 0, "extra": 1})
    locs = [d["loc"] for d in e.value.details or []]
    assert ["body", "config", "extra"] in locs and ["body", "config", "port"] in locs
    with pytest.raises(ApiError):
        validate_config("http", {"url": "ftp://x"})
    with pytest.raises(ApiError):
        validate_config("ping", {"count": True})
    merged = validate_config("tcp_port", {"timeout_s": 9}, base={"port": 22, "timeout_s": 5})
    assert merged == {"port": 22, "timeout_s": 9}
    assert validate_channel_overrides("ping", [{"key": "latency_ms", "down_max": None}]) == {
        "latency_ms": {"down_max": None}
    }


def test_rollup_rules() -> None:
    assert device_status(False, ["up"]) == "paused"
    assert device_status(True, []) == "unknown"
    assert device_status(True, ["paused", "paused"]) == "paused"
    assert device_status(True, ["paused", "up", "warning"]) == "warning"
    assert device_status(True, ["unknown", "up"]) == "unknown"
    assert group_status([]) == "unknown"
    assert group_status(["paused"]) == "paused"
    assert group_status(["paused", "up", "down"]) == "down"

    inv = Inventory()
    inv.groups = {
        1: {"parent_id": None, "name": "root", "snmp_credentials": None, "sort_order": 0},
        2: {"parent_id": 1, "name": "a", "snmp_credentials": {"version": "v1"}, "sort_order": 0},
        3: {"parent_id": 2, "name": "b", "snmp_credentials": None, "sort_order": 0},
        4: {"parent_id": 1, "name": "empty", "snmp_credentials": None, "sort_order": 0},
    }
    inv.devices = {
        10: {"group_id": 1, "name": "d10", "enabled": True, "snmp_credentials": None},
        11: {"group_id": 3, "name": "d11", "enabled": True, "snmp_credentials": None},
        12: {"group_id": 3, "name": "d12", "enabled": False, "snmp_credentials": None},
    }
    inv.sensors = {
        100: {"device_id": 10, "status": "up"},
        101: {"device_id": 11, "status": "down"},
        102: {"device_id": 11, "status": "up"},
        103: {"device_id": 12, "status": "paused"},
    }
    inv.compute()
    assert inv.device_status == {10: "up", 11: "down", 12: "paused"}
    assert inv.group_status[3] == "down" and inv.group_status[2] == "down"
    assert inv.group_status[1] == "down"
    assert inv.group_status[4] == "unknown"  # empty group; excluded from the parent's rollup
    assert inv.group_counts[1] == {
        "up": 2,
        "warning": 0,
        "down": 1,
        "paused": 1,
        "unknown": 0,
        "unreachable": 0,
        "total": 4,
    }
    assert inv.ancestors(3) == [3, 2, 1]
    assert sorted(inv.descendants(2)) == [2, 3]
    assert inv.effective_snmp(11) == ("group", 2)
    assert inv.effective_snmp(10) == ("default", None)


@pytest.mark.parametrize(
    ("span", "bucket"),
    [
        (3600, 60),
        (86400, 300),
        (7 * 86400, 1800),
        (30 * 86400, 7200),
        (90 * 86400, 19440),
        (31 * 86400, 6720),
    ],
)
def test_bucket_for_span(span: int, bucket: int) -> None:
    assert bucket_for_span(span) == bucket
