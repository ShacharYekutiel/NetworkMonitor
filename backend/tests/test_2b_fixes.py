"""[2b-1] Fix round: regression tests (H1-H3, M1-M6, L1-L12, D5, D6)."""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from netmon.alerts import engine as engine_mod
from netmon.clock import FakeClock
from netmon.poller.processor import persist
from netmon.runtime import Runtime
from netmon.services import dependencies
from tests.conftest import make_device, make_sensor, requires_db
from tests.test_2b_alerts import (
    DOWN,
    UP,
    WARN,
    Outbox,
    RecordingScheduler,
    channel,
    run,
    state,
    tick,
)

pytestmark = requires_db


@pytest.fixture
async def clock(rt: Runtime, admin: httpx.AsyncClient) -> AsyncIterator[FakeClock]:
    """A fake clock slightly behind the wall clock (so its times are never "in the future"
    for the dependency checks)."""
    old = rt.clock
    fake = FakeClock(datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10))
    rt.clock = fake
    yield fake
    rt.clock = old


@pytest.fixture
async def outbox(rt: Runtime, admin: httpx.AsyncClient) -> Outbox:
    box = Outbox()
    rt.alerts.sender = box
    return box


async def _rule(c: httpx.AsyncClient, **kw: Any) -> dict[str, Any]:
    r = await c.post("/api/alert-rules", json={"name": "r", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ H1
async def test_h1_restart_in_maintenance_sends_nothing(
    rt: Runtime, admin: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = Outbox()
    rt.alerts.sender = box
    p = await channel(admin, "primary")
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "ping")
    await _rule(admin, on_statuses=["down", "up"], channel_ids=[p])
    now = datetime.now(UTC)
    r = await admin.post(
        "/api/maintenance-windows",
        json={
            "name": "w",
            "scope": "global",
            "kind": "once",
            "starts_at": _z(now - timedelta(minutes=5)),
            "ends_at": _z(now + timedelta(hours=1)),
        },
    )
    assert r.status_code == 201
    await rt.alerts.stop()
    try:
        await persist(rt, s["id"], UP, 1.0, now)
        await persist(rt, s["id"], DOWN, 1.0, now)
        await rt.drain()  # the stopped engine still upserts; nothing sends
        # the previous process died before it could mark the row suppressed: overdue
        async with rt.sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE alert_state SET due_at = now() - interval '1 minute', "
                    "maintenance_suppressed = false"
                )
            )
            await session.commit()
        box2 = Outbox()
        monkeypatch.setattr(engine_mod, "send_to_channel", box2)
        rt2 = Runtime(rt.settings)
        try:
            await rt2.start(run_poller=False, take_lock=False, migrate=False, maintenance=False)
            await asyncio.sleep(0.2)  # the loop's first pass ran
            await rt2.alerts.idle()
            assert box2.items == []
            [row] = await state(rt2, s["id"])
            assert row["maintenance_suppressed"] is True and row["due_at"] is None
        finally:
            await rt2.stop()
    finally:
        rt.alerts.start()
    assert box.items == []


async def test_h1_tracker_not_initialized_holds_problems_not_recoveries(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    p = await channel(admin, "primary")
    dev = await make_device(admin)
    s1 = await make_sensor(admin, dev["id"], "ping", name="a")
    s2 = await make_sensor(admin, dev["id"], "ping", name="b")
    await _rule(admin, on_statuses=["down", "up"], channel_ids=[p])
    for sid in (s1["id"], s2["id"]):
        await run(rt, clock, sid, UP)
    await run(rt, clock, s2["id"], DOWN)
    outbox.clear()
    rt.maintenance._initialized = False  # noqa: SLF001
    try:
        await run(rt, clock, s1["id"], DOWN)  # held: stays due
        await run(rt, clock, s2["id"], UP)  # a recovery is not held
        assert outbox.stages() == [("primary", "recovery")]
        [row] = await state(rt, s1["id"])
        assert row["due_at"] is not None
    finally:
        rt.maintenance._initialized = True  # noqa: SLF001
    await rt.drain()
    assert outbox.stages()[-1] == ("primary", "problem")


# ------------------------------------------------------------------ H2
async def _parent_child(c: httpx.AsyncClient) -> tuple[dict, dict, dict]:
    gw = await make_device(c, name="gw", host="10.2.0.1")
    gp = await make_sensor(c, gw["id"], "ping", name="Ping", interval_s=30)
    child = await make_device(
        c, name="cam", host="10.2.0.2", dependency={"mode": "device", "device_id": gw["id"]}
    )
    cs = await make_sensor(c, child["id"], "ping", name="Ping")
    return gp, child, cs


async def test_h2_child_before_parent_no_down_and_hold(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    p = await channel(admin, "primary")
    gp, _, cs = await _parent_child(admin)
    await _rule(admin, on_statuses=["down", "up"], delay_s=0, channel_ids=[p])
    await run(rt, clock, gp["id"], UP)
    await run(rt, clock, cs["id"], UP)
    rec = RecordingScheduler()
    rt.scheduler = rec  # type: ignore[assignment]
    try:
        t = clock.advance(5) or clock.now()
        await run(rt, clock, cs["id"], DOWN)
        assert rec.calls == [([gp["id"]], 0)]  # the parent is re-checked at once
        [row] = await state(rt, cs["id"])
        assert row["due_at"] == t + timedelta(seconds=30 + 30)  # interval + timeout
        await tick(rt, clock, 59)
        assert outbox.items == []
        # case A: the parent is now found down -> the child turns unreachable, no DOWN
        await run(rt, clock, gp["id"], DOWN)
        assert (await admin.get(f"/api/sensors/{cs['id']}")).json()["status"] == "unreachable"
        await tick(rt, clock, 600)
        assert [x[1]["sensor"]["id"] for x in outbox.items] == [gp["id"]]
        # case B: parent up, the child really down -> alert, but only after the hold
        await run(rt, clock, gp["id"], UP)
        outbox.clear()
        clock.advance(5)
        await run(rt, clock, cs["id"], DOWN)
        await tick(rt, clock, 30)
        assert outbox.items == []
        await tick(rt, clock, 31)
        assert outbox.stages() == [("primary", "problem")]
        assert outbox.items[0][1]["sensor"]["id"] == cs["id"]
    finally:
        rt.scheduler = None


async def test_h2_parent_recovered_during_child_check(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    p = await channel(admin, "primary")
    gp, _, cs = await _parent_child(admin)
    await _rule(admin, on_statuses=["down", "up"], channel_ids=[p])
    await run(rt, clock, gp["id"], UP)
    await run(rt, clock, cs["id"], UP)
    clock.advance(1)
    await run(rt, clock, gp["id"], DOWN)
    outbox.clear()
    clock.advance(1)
    child_started = clock.now()  # the child's check starts while the parent is down...
    clock.advance(1)
    await run(rt, clock, gp["id"], UP)  # ...the parent recovers before it finishes
    rec = RecordingScheduler()
    rt.scheduler = rec  # type: ignore[assignment]
    try:
        await persist(rt, cs["id"], DOWN, 1.0, child_started)
        await rt.drain()
    finally:
        rt.scheduler = None
    got = (await admin.get(f"/api/sensors/{cs['id']}")).json()
    assert got["status"] == "unreachable"  # counted as down for that check
    assert rec.calls == [([cs["id"]], dependencies.RECHECK_JITTER_S)]  # re-checked
    await tick(rt, clock, 600)
    assert outbox.stages() == [("primary", "recovery")]  # the parent only; no false DOWN


async def test_h2_run_soon_queues_followup(
    rt: Runtime, admin: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from netmon.poller import scheduler as sched_mod

    dev = await make_device(admin, host="127.0.6.1")
    s = await make_sensor(admin, dev["id"], "tcp_port", {"port": 1, "timeout_s": 1})
    calls: list[int] = []
    real = sched_mod.execute_check

    async def counting(ctx: Any, sem: Any = None) -> Any:
        calls.append(ctx.sensor_id)
        return await real(ctx, sem)

    monkeypatch.setattr(sched_mod, "execute_check", counting)
    sch = sched_mod.Scheduler(rt)
    sch._running.add(s["id"])  # noqa: SLF001 - a regular run is in flight
    sch.run_soon([s["id"]], jitter_s=0)
    await asyncio.sleep(0.05)
    assert calls == [] and s["id"] in sch._followup  # noqa: SLF001
    await sch._run_one(s["id"])  # noqa: SLF001 - the in-flight run finishes ...
    await sch.idle()
    assert calls == [s["id"], s["id"]]  # ... and the follow-up ran once more
    await rt.drain()


# ------------------------------------------------------------------ H3
async def test_h3_slow_channel_blocks_nothing(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    release = asyncio.Event()
    entered = asyncio.Event()
    sent: list[tuple[str, str]] = []

    async def slow(channel, payload, smtp, lang):  # type: ignore[no-untyped-def]
        entered.set()
        await asyncio.wait_for(release.wait(), 30)
        sent.append((channel["name"], payload["stage"]))

    rt.alerts.sender = slow
    p = await channel(admin, "slow")
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "ping", name="a")
    other = await make_sensor(admin, dev["id"], "ping", name="b")
    await _rule(admin, on_statuses=["down", "up"], channel_ids=[p])
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, other["id"], UP)
    await persist(rt, s["id"], DOWN, 1.0, clock.now())
    rt.alerts.notify()
    await asyncio.wait_for(entered.wait(), 5)  # the loop is sending right now
    t0 = time.monotonic()
    r = await admin.post(f"/api/sensors/{s['id']}/acknowledge", json={"comment": "x"})
    assert r.status_code == 200
    r = await admin.post(
        "/api/maintenance-windows",
        json={
            "name": "w",
            "scope": "device",
            "device_id": dev["id"],
            "kind": "weekly",
            "weekdays": [1],
            "start_time": "03:00",
            "duration_min": 10,
        },
    )
    assert r.status_code == 201
    r = await admin.patch(f"/api/maintenance-windows/{r.json()['id']}", json={"enabled": False})
    assert r.status_code == 200
    # status processing goes on: the other sensor's problem row appears
    await persist(rt, other["id"], DOWN, 1.0, clock.now())
    for _ in range(100):
        if await state(rt, other["id"]):
            break
        await asyncio.sleep(0.02)
    assert await state(rt, other["id"])
    # the sending row counts as notified: an `up` now keeps it for the recovery
    await persist(rt, s["id"], UP, 1.0, clock.now())
    for _ in range(100):
        rows = await state(rt, s["id"])
        if rows and rows[0]["status"] == "up":
            break
        await asyncio.sleep(0.02)
    assert rows[0]["status"] == "up" and rows[0]["sending_since"] is not None
    assert time.monotonic() - t0 < 5
    release.set()
    # the recovery goes out on the engine's next pass after the slow send returns; drain()
    # can return before that pass (CI race), so poke the engine until it is sent
    for _ in range(100):
        await rt.drain()
        if ("slow", "recovery") in sent:
            break
        rt.alerts.notify()
        await asyncio.sleep(0.05)
    assert ("slow", "problem") in sent and ("slow", "recovery") in sent
    assert all(r["sending_since"] is None for r in await state(rt, other["id"]))


async def test_h3_interrupted_send_rearmed_on_restart(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    p = await channel(admin, "primary")
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "ping")
    await _rule(admin, on_statuses=["down", "up"], channel_ids=[p])
    await run(rt, clock, s["id"], UP)
    await rt.alerts.stop()
    try:
        await persist(rt, s["id"], DOWN, 1.0, clock.now())
        await rt.drain()
        async with rt.sessionmaker() as session:  # the process died while sending
            await session.execute(
                text("UPDATE alert_state SET due_at = NULL, sending_since = :t"),
                {"t": clock.now()},
            )
            await session.commit()
        assert await rt.alerts.recover_inflight() == 1
    finally:
        rt.alerts.start()
    await rt.drain()
    assert outbox.stages() == [("primary", "problem")]  # at least once
    [row] = await state(rt, s["id"])
    assert row["sending_since"] is None and row["primary_sent_at"] is not None


# ------------------------------------------------------------------ M1 / M2 / M3 / M6
async def test_m1_unack_before_delay_sends_problem_and_escalation(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    p, e = await channel(admin, "primary"), await channel(admin, "oncall")
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "ping")
    await _rule(
        admin,
        on_statuses=["down", "up"],
        delay_s=120,
        channel_ids=[p],
        escalation_after_s=300,
        escalation_channel_ids=[e],
    )
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    await admin.post(f"/api/sensors/{s['id']}/acknowledge")
    await rt.drain()
    await tick(rt, clock, 60)
    await admin.delete(f"/api/sensors/{s['id']}/acknowledge")
    await rt.drain()
    assert outbox.stages() == [("primary", "problem")]  # not lost
    await tick(rt, clock, 299)
    assert outbox.stages() == [("primary", "problem")]
    await tick(rt, clock, 2)  # escalation restarted from the unack
    assert outbox.stages()[-1] == ("oncall", "escalation")


async def test_m2_leaving_maintenance_no_second_down(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    p, e = await channel(admin, "primary"), await channel(admin, "oncall")
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "ping")
    await _rule(
        admin,
        on_statuses=["down", "up"],
        channel_ids=[p],
        escalation_after_s=300,
        escalation_channel_ids=[e],
    )
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    assert outbox.stages() == [("primary", "problem")]
    now = clock.now()
    await admin.post(
        "/api/maintenance-windows",
        json={
            "name": "w",
            "scope": "global",
            "kind": "once",
            "starts_at": _z(now + timedelta(seconds=60)),
            "ends_at": _z(now + timedelta(seconds=900)),
        },
    )
    await tick(rt, clock, 61)
    await tick(rt, clock, 300)  # escalation due inside the window: suppressed
    assert outbox.stages() == [("primary", "problem")]
    [row] = await state(rt, s["id"])
    assert row["maintenance_suppressed"] is True and row["escalation_due_at"] is None
    await tick(rt, clock, 600)  # window over
    [row] = await state(rt, s["id"])
    assert row["maintenance_suppressed"] is False  # no re-send pending
    assert outbox.stages() == [("primary", "problem")]  # no second DOWN
    assert row["escalation_due_at"] is not None  # escalation restarted from the window end
    await tick(rt, clock, 301)
    assert outbox.stages() == [("primary", "problem"), ("oncall", "escalation")]


async def test_m3_escalation_rearmed_after_non_matching_status(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    p, e = await channel(admin, "primary"), await channel(admin, "oncall")
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "ping")
    await _rule(
        admin,
        on_statuses=["down"],
        channel_ids=[p],
        escalation_after_s=300,
        escalation_channel_ids=[e],
    )
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    clock.advance(10)
    await run(rt, clock, s["id"], WARN)  # not matching the rule
    await tick(rt, clock, 300)  # escalation due while not active: dropped
    assert outbox.stages() == [("primary", "problem")]
    await run(rt, clock, s["id"], DOWN)  # back within the same problem
    await rt.drain()
    assert ("oncall", "escalation") in outbox.stages()  # re-armed (overdue: right away)


async def test_m6_recovery_payload_keeps_problem_start(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    p = await channel(admin, "primary")
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "ping")
    await _rule(admin, on_statuses=["down", "up"], channel_ids=[p])
    await run(rt, clock, s["id"], UP)
    start = clock.now()
    await run(rt, clock, s["id"], DOWN)
    clock.advance(600)
    await run(rt, clock, s["id"], UP)
    rec = outbox.items[-1][1]
    assert rec["stage"] == "recovery"
    assert rec["problem_since"] == _z(start)
    assert rec["time"] == _z(clock.now())


# ------------------------------------------------------------------ loop backoff
async def test_alert_loop_backoff_logs_once(
    rt: Runtime, admin: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    eng = engine_mod.AlertEngine(rt)
    calls: list[float] = []

    async def broken() -> int:
        calls.append(time.monotonic())
        raise RuntimeError("bad row")

    async def due() -> datetime:
        return rt.clock.now() - timedelta(minutes=1)  # always overdue

    eng.run_once = broken  # type: ignore[method-assign]
    eng._next_due = due  # type: ignore[method-assign]  # noqa: SLF001
    eng.recover_inflight = lambda: asyncio.sleep(0)  # type: ignore[assignment,method-assign]
    with caplog.at_level(logging.DEBUG, logger="netmon.alerts"):
        eng.start()
        await asyncio.sleep(2.5)
        await eng.stop()
    assert 2 <= len(calls) <= 4  # 1 s, 2 s ... not every 50 ms
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1


# ------------------------------------------------------------------ M4 / L2 cascade
async def test_m4_cascade_coalesced_and_indexed(
    rt: Runtime, admin: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from netmon.services import status as status_mod

    gp, child, cs = await _parent_child(admin)
    lonely = await make_device(admin, name="lonely", host="10.2.0.9")
    ls = await make_sensor(admin, lonely["id"], "ping")
    loads: list[int] = []
    real = status_mod.load_inventory

    async def counting(session: Any) -> Any:
        loads.append(1)
        return await real(session)

    monkeypatch.setattr(status_mod, "load_inventory", counting)
    monkeypatch.setattr(dependencies, "CASCADE_DEBOUNCE_S", 0.2)
    now = datetime.now(UTC)
    await persist(rt, gp["id"], UP, 1.0, now)
    await persist(rt, cs["id"], UP, 1.0, now)
    await persist(rt, ls["id"], UP, 1.0, now)
    await rt.drain()
    assert len(loads) == 1  # one load for the burst
    loads.clear()
    await persist(rt, ls["id"], DOWN, 1.0, now)  # nobody depends on `lonely`
    await rt.drain()
    assert loads == []
    await persist(rt, gp["id"], DOWN, 1.0, now)  # the parent: the cascade runs
    await rt.drain()
    assert len(loads) == 1
    assert (await admin.get(f"/api/sensors/{cs['id']}")).json()["status"] == "up"


async def test_l2_cascade_rechecks_parent_in_lock(
    rt: Runtime, admin: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from netmon.services import status as status_mod

    gp, _, cs = await _parent_child(admin)
    now = datetime.now(UTC)
    await persist(rt, gp["id"], UP, 1.0, now)
    await persist(rt, cs["id"], UP, 1.0, now)
    await persist(rt, cs["id"], DOWN, 1.0, now + timedelta(seconds=1))  # found down
    await rt.drain()
    real = status_mod.load_inventory

    async def stale(session: Any) -> Any:  # an inventory taken while the parent was down
        inv = await real(session)
        inv.sensors[gp["id"]]["status"] = "down"
        return inv

    monkeypatch.setattr(status_mod, "load_inventory", stale)
    await dependencies._cascade(rt, {gp["device_id"]: {gp["id"]}}, None)  # noqa: SLF001
    assert (await admin.get(f"/api/sensors/{cs['id']}")).json()["status"] == "down"


# ------------------------------------------------------------------ L3 tracker wake
async def test_l3_wake_during_evaluate_not_lost(rt: Runtime, admin: httpx.AsyncClient) -> None:
    from netmon.services.maintenance import MaintenanceTracker

    tr = MaintenanceTracker(rt)
    evaluations: list[float] = []
    real = tr._evaluate  # noqa: SLF001

    async def evaluate() -> None:
        evaluations.append(time.monotonic())
        if len(evaluations) == 2:
            tr.request()  # a change arrives while this evaluation runs
        await real()

    tr._evaluate = evaluate  # type: ignore[method-assign]  # noqa: SLF001
    await tr.start()  # evaluation 1
    try:
        tr.request()
        for _ in range(100):
            if len(evaluations) >= 3:
                break
            await asyncio.sleep(0.02)
        assert len(evaluations) >= 3  # the wake during evaluation 2 caused evaluation 3
    finally:
        await tr.stop()


# ------------------------------------------------------------------ L4 test-mode warning
async def test_l4_test_mode_startup_warning(
    rt: Runtime, admin: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    rt.settings.netmon_test_mode = True
    rt2 = Runtime(rt.settings)
    try:
        with caplog.at_level(logging.WARNING, logger="netmon.runtime"):
            await rt2.start(run_poller=False, take_lock=False, migrate=False, maintenance=False)
        assert any("NETMON_TEST_MODE" in r.getMessage() for r in caplog.records)
    finally:
        await rt2.stop()
        rt.settings.netmon_test_mode = False
    ev = (await admin.get("/api/events", params={"kind": "system"})).json()["items"]
    assert any("Test mode is on" in e["message"] for e in ev)


# ------------------------------------------------------------------ L6 fingerprint TLS
def test_l6_fingerprint_tls_context() -> None:
    from netmon.discovery.fingerprint import fingerprint_tls_context

    ctx = fingerprint_tls_context()
    assert ctx.verify_mode == ssl.CERT_NONE and ctx.check_hostname is False
    assert ctx.minimum_version in (ssl.TLSVersion.TLSv1, ssl.TLSVersion.MINIMUM_SUPPORTED)


# ------------------------------------------------------------------ L12 concurrent cycles
async def test_l12_concurrent_dependency_writes_cannot_cycle(admin: httpx.AsyncClient) -> None:
    a = await make_device(admin, name="a", host="10.3.0.1")
    b = await make_device(admin, name="b", host="10.3.0.2")
    for _ in range(3):
        r1, r2 = await asyncio.gather(
            admin.patch(
                f"/api/devices/{a['id']}",
                json={"dependency": {"mode": "device", "device_id": b["id"]}},
            ),
            admin.patch(
                f"/api/devices/{b['id']}",
                json={"dependency": {"mode": "device", "device_id": a["id"]}},
            ),
        )
        assert sorted([r1.status_code, r2.status_code]) == [200, 400]
        for d in (a, b):
            await admin.patch(f"/api/devices/{d['id']}", json={"dependency": {"mode": "none"}})


# ------------------------------------------------------------------ D5 advisory lock
async def test_d5_lock_on_dedicated_connection(databases, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import asyncpg

    from tests.conftest import make_settings

    settings = make_settings(databases["fallback"], tmp_path, NETMON_DISABLE_TIMESCALE="1")
    rt2 = Runtime(settings)
    await rt2.start(run_poller=False, maintenance=False)
    lost: list[bool] = []
    rt2.on_lock_lost = lambda: lost.append(True)
    try:
        assert isinstance(rt2._lock_conn, asyncpg.Connection)  # noqa: SLF001 - not pooled
        # pool churn never touches the lock connection
        await rt2.engine.dispose()
        for _ in range(3):
            assert await rt2.check_lock() is True
        async with rt2.sessionmaker() as session:
            n = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE application_name = "
                        "'netmon-lock' AND datname = current_database()"
                    )
                )
            ).scalar()
        assert n == 1
        assert lost == []
    finally:
        await rt2.stop()


# ------------------------------------------------------------------ D6 topology refresh
async def test_d6_stale_running_refresh_and_failures(
    rt: Runtime, admin: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with rt.sessionmaker() as session:  # a row left `running` by a dead process
        await session.execute(
            text("INSERT INTO topology_refreshes (trigger, status) VALUES ('manual', 'running')")
        )
        await session.commit()
    r = await admin.post("/api/topology/refresh")
    assert r.status_code == 202, r.text  # not a permanent 409
    await rt.topology.wait()
    assert rt.topology.running_id is None
    refreshes = (
        (await admin.get("/api/topology")).json()["last_refresh"],
        None,
    )
    assert refreshes[0]["status"] == "completed"

    async def boom(stats: dict[str, int]) -> dict[str, int]:
        raise RuntimeError("walk exploded")

    monkeypatch.setattr(rt.topology, "refresh", boom)
    await admin.post("/api/topology/refresh")
    await rt.topology.wait()
    last = (await admin.get("/api/topology")).json()["last_refresh"]
    assert last["status"] == "failed" and last["error"] == "walk exploded"
    assert rt.topology.running_id is None

    async def hang(stats: dict[str, int]) -> dict[str, int]:
        await asyncio.sleep(60)
        return stats

    monkeypatch.setattr(rt.topology, "refresh", hang)
    monkeypatch.setattr(rt.topology, "total_budget_s", 0.2)
    await admin.post("/api/topology/refresh")
    await rt.topology.wait()
    last = (await admin.get("/api/topology")).json()["last_refresh"]
    assert last["status"] == "failed" and last["error"].startswith("Timed out")
    r = await admin.post("/api/topology/refresh")  # still usable afterwards
    assert r.status_code == 202
    await rt.topology.wait()
