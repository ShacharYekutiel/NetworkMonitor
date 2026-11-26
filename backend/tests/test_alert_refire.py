"""[1.5.1] A second failure of the same sensor(s) after the first one: when is the problem
notified again? (production report: management IP changed, changed back, changed again.)

Every scenario is driven through the real processor (persist) and the real alert engine with a
FakeClock, like tests/test_2b_alerts.py."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from netmon.clock import FakeClock
from netmon.poller.processor import persist
from netmon.poller.types import CheckResult
from netmon.runtime import Runtime
from tests.conftest import make_device, make_sensor, requires_db
from tests.test_2b_alerts import DOWN, UP, RecordingScheduler, channel, run, state, tick

pytestmark = requires_db

DEFAULT_RULE = {"on_statuses": ["down", "up"], "delay_s": 0}  # frontend lib/defaultRule.ts


@pytest.fixture
async def clock(rt: Runtime, admin: httpx.AsyncClient) -> AsyncIterator[FakeClock]:
    """Slightly behind the wall clock (dependency checks never see "future" changes)."""
    old = rt.clock
    fake = FakeClock(datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30))
    rt.clock = fake
    yield fake
    rt.clock = old


class Box:
    """Records sends; `block` holds one stage until released; `fail` makes a stage fail."""

    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []
        self.block: str | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail: set[str] = set()

    async def __call__(self, channel, payload, smtp, lang):  # type: ignore[no-untyped-def]
        stage = payload["stage"]
        if stage == self.block:
            self.block = None  # only the first send of that stage is held
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), 30)
        if stage in self.fail:
            self.fail.discard(stage)
            raise RuntimeError("boom")
        self.items.append((channel["name"], payload))

    def stages(self, sensor_id: int | None = None) -> list[str]:
        return [
            p["stage"]
            for _, p in self.items
            if sensor_id is None or (p.get("sensor") or {}).get("id") == sensor_id
        ]


@pytest.fixture
async def box(rt: Runtime, admin: httpx.AsyncClient) -> Box:
    b = Box()
    rt.alerts.sender = b
    return b


async def _rule(c: httpx.AsyncClient, ch: int, **kw: Any) -> dict[str, Any]:
    r = await c.post("/api/alert-rules", json={"name": "r", "channel_ids": [ch], **kw})
    assert r.status_code == 201, r.text
    return r.json()


async def _setup(admin: httpx.AsyncClient, **rule: Any) -> tuple[int, dict[str, Any]]:
    ch = await channel(admin, "ops")
    dev = await make_device(admin, name="sw1", host="10.9.0.1")
    s = await make_sensor(admin, dev["id"], "ping", name="Ping")
    await _rule(admin, ch, **{**DEFAULT_RULE, **rule})
    return ch, s


async def _events_only(rt: Runtime) -> None:
    """Let the event path (alert_state upserts) finish without running the alert loop."""
    while pending := [t for t in rt.alerts._tasks if not t.done()]:  # noqa: SLF001
        await asyncio.gather(*pending, return_exceptions=True)


async def _until(pred: Callable[[], Any], tries: int = 200) -> Any:
    for _ in range(tries):
        v = await pred()
        if v:
            return v
        rt_notify()
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


_RT: list[Runtime] = []


def rt_notify() -> None:
    for r in _RT:
        r.alerts.notify()


@pytest.fixture(autouse=True)
def _rt_ref(rt: Runtime) -> Any:
    _RT.append(rt)
    yield
    _RT.remove(rt)


async def _sensor_events(rt: Runtime, sid: int, kind: str) -> list[dict[str, Any]]:
    async with rt.sessionmaker() as session:
        rows = await session.execute(
            text("SELECT * FROM events WHERE sensor_id = :s AND kind = :k ORDER BY id"),
            {"s": sid, "k": kind},
        )
        return [dict(r._mapping) for r in rows]  # noqa: SLF001


# ------------------------------------------------------------------ 1. the plain sequence
@pytest.mark.parametrize(
    "rule",
    [
        {},  # the default rule: down + up, no delay
        {"on_statuses": ["down"]},  # no recovery wanted
        {"delay_s": 60},
        {"on_statuses": ["down"], "delay_s": 60},
    ],
)
async def test_1_down_up_down_notifies_twice(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box, rule: dict[str, Any]
) -> None:
    _, s = await _setup(admin, **rule)
    delay = int(rule.get("delay_s", 0))
    with_up = "up" in rule.get("on_statuses", ["up"])
    await run(rt, clock, s["id"], UP)
    for _ in range(2):  # outage 1 and outage 2
        clock.advance(60)
        await run(rt, clock, s["id"], DOWN)
        await tick(rt, clock, delay + 1)
        clock.advance(60)
        await run(rt, clock, s["id"], UP)
        await tick(rt, clock, delay + 1)
    want = ["problem", "recovery"] if with_up else ["problem"]
    assert box.stages() == want * 2
    assert await state(rt, s["id"]) == []
    sent = await _sensor_events(rt, s["id"], "notification_sent")
    assert len(sent) == len(want) * 2  # the timeline shows every send


# ------------------------------------------------------------------ 2. down again before the
# recovery was sent / applied
@pytest.mark.parametrize("on_statuses", [["down", "up"], ["down"]])
async def test_2a_down_again_before_the_loop_ran(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box, on_statuses: list[str]
) -> None:
    """up and the next down land in the same loop tick: the recovery is never sent, the second
    problem is (notified_channel_ids kept, primary_sent_at reset)."""
    _, s = await _setup(admin, on_statuses=on_statuses)
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    assert box.stages() == ["problem"]
    await rt.alerts.stop()
    try:
        clock.advance(30)
        await persist(rt, s["id"], UP, 1.0, clock.now())
        await _events_only(rt)
        clock.advance(30)
        await persist(rt, s["id"], DOWN, 1.0, clock.now())
        await _events_only(rt)
        [row] = await state(rt, s["id"])
        assert row["status"] == "down" and row["primary_sent_at"] is None
        assert row["due_at"] == clock.now()
    finally:
        rt.alerts.start()
    await rt.drain()
    assert box.stages() == ["problem", "problem"]
    second = await _sensor_events(rt, s["id"], "status_change")
    assert box.items[1][1]["event_id"] == second[-1]["id"]


async def test_2b_down_again_within_recovery_delay(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    _, s = await _setup(admin, delay_s=120)
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    await tick(rt, clock, 121)
    assert box.stages() == ["problem"]
    await run(rt, clock, s["id"], UP)  # recovery due in 120 s
    await tick(rt, clock, 60)
    await run(rt, clock, s["id"], DOWN)  # before the recovery: a new problem, delayed again
    await tick(rt, clock, 119)
    assert box.stages() == ["problem"]
    await tick(rt, clock, 2)
    assert box.stages() == ["problem", "problem"]  # DOWN, DOWN (the recovery was dropped)


async def test_2c_down_again_while_recovery_is_being_sent(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    """The ON CONFLICT upsert keeps `sending_since` of the in-flight recovery: the new problem
    waits for that pass, is not deleted by its delete_after, and is sent afterwards."""
    _, s = await _setup(admin)
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    box.block = "recovery"
    clock.advance(30)
    await persist(rt, s["id"], UP, 1.0, clock.now())
    rt.alerts.notify()
    await asyncio.wait_for(box.entered.wait(), 5)  # the recovery is in flight
    clock.advance(5)
    await persist(rt, s["id"], DOWN, 1.0, clock.now())

    async def is_down() -> Any:
        rows = await state(rt, s["id"])
        return rows if rows and rows[0]["status"] == "down" else None

    [row] = await _until(is_down)
    assert row["sending_since"] is not None and row["primary_sent_at"] is None
    box.release.set()

    async def two_problems() -> bool:
        await rt.drain()
        return box.stages().count("problem") == 2

    await _until(two_problems)
    assert box.stages() == ["problem", "recovery", "problem"]
    [row] = await state(rt, s["id"])
    assert row["status"] == "down" and row["sending_since"] is None
    assert row["primary_sent_at"] is not None


async def test_2d_failed_recovery_send(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    _, s = await _setup(admin)
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    box.fail = {"recovery"}
    await run(rt, clock, s["id"], UP)
    assert box.stages() == ["problem"]
    assert await state(rt, s["id"]) == []  # deleted although the recovery failed
    fails = await _sensor_events(rt, s["id"], "notification")
    assert len(fails) == 1 and "boom" in fails[0]["message"]
    await run(rt, clock, s["id"], DOWN)
    assert box.stages() == ["problem", "problem"]


async def test_2e_up_and_down_while_problem_is_being_sent(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    _, s = await _setup(admin)
    await run(rt, clock, s["id"], UP)
    box.block = "problem"
    await persist(rt, s["id"], DOWN, 1.0, clock.now())
    rt.alerts.notify()
    await asyncio.wait_for(box.entered.wait(), 5)
    clock.advance(10)
    await persist(rt, s["id"], UP, 1.0, clock.now())
    clock.advance(10)
    await persist(rt, s["id"], DOWN, 1.0, clock.now())
    last = (await _sensor_events(rt, s["id"], "status_change"))[-1]["id"]

    async def is_new_problem() -> Any:
        rows = await state(rt, s["id"])
        return rows and rows[0]["event_id"] == last and rows[0]["status"] == "down"

    await _until(is_new_problem)
    box.release.set()

    async def two_problems() -> bool:
        await rt.drain()
        return box.stages().count("problem") == 2

    await _until(two_problems)
    assert box.items[-1][1]["event_id"] == last


async def test_2f_new_problem_does_not_inherit_flap_suppressed(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    """A row whose escalation was held by flapping keeps `flap_suppressed`; a NEW problem must
    not inherit it (it was labelled "after flapping" although nothing was flapping), exactly
    like `maintenance_suppressed`, which the upsert already resets."""
    _, s = await _setup(admin, delay_s=60)
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    await tick(rt, clock, 61)
    assert box.stages() == ["problem"]
    async with rt.sessionmaker() as session:  # an escalation was held while flapping
        await session.execute(
            text("UPDATE alert_state SET flap_suppressed = true WHERE sensor_id = :s"),
            {"s": s["id"]},
        )
        await session.commit()
    await run(rt, clock, s["id"], UP)  # recovery due in 60 s
    await tick(rt, clock, 30)
    await run(rt, clock, s["id"], DOWN)  # a new problem before the recovery
    [row] = await state(rt, s["id"])
    assert row["flap_suppressed"] is False
    await tick(rt, clock, 61)
    assert box.stages() == ["problem", "problem"]
    assert box.items[1][1]["after_flapping"] is False


# ------------------------------------------------------------------ 3. acknowledge
async def test_3a_ack_cleared_by_recovery_second_problem_notified(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    _, s = await _setup(admin)
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    r = await admin.post(f"/api/sensors/{s['id']}/acknowledge", json={"comment": "IP change"})
    assert r.status_code == 200
    await rt.drain()
    await run(rt, clock, s["id"], UP)
    assert (await admin.get(f"/api/sensors/{s['id']}")).json()["acknowledgement"] is None
    await run(rt, clock, s["id"], DOWN)
    assert box.stages() == ["problem", "recovery", "problem"]


async def test_3b_ack_cleared_through_unreachable(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    """down (acked) -> unreachable (parent down) -> parent up -> down: notified again."""
    ch = await channel(admin, "ops")
    gw = await make_device(admin, name="gw", host="10.2.0.1")
    gp = await make_sensor(admin, gw["id"], "ping", name="Ping", interval_s=30)
    sw = await make_device(
        admin, name="sw", host="10.2.0.2", dependency={"mode": "device", "device_id": gw["id"]}
    )
    s = await make_sensor(admin, sw["id"], "ping", name="Ping")
    await _rule(admin, ch, **DEFAULT_RULE)
    rt.scheduler = RecordingScheduler()  # type: ignore[assignment]
    try:
        await run(rt, clock, gp["id"], UP)
        await run(rt, clock, s["id"], UP)
        clock.advance(5)
        await run(rt, clock, s["id"], DOWN)
        await tick(rt, clock, 61)  # the parent hold
        assert box.stages(s["id"]) == ["problem"]
        r = await admin.post(f"/api/sensors/{s['id']}/acknowledge", json={"comment": ""})
        assert r.status_code == 200
        await rt.drain()
        await run(rt, clock, gp["id"], DOWN)  # cascade: the switch turns unreachable
        assert (await admin.get(f"/api/sensors/{s['id']}")).json()["status"] == "unreachable"
        assert (await admin.get(f"/api/sensors/{s['id']}")).json()["acknowledgement"] is None
        [row] = await state(rt, s["id"])
        assert row["status"] == "suspended"
        await run(rt, clock, gp["id"], UP)
        clock.advance(5)
        await run(rt, clock, s["id"], DOWN)
        await tick(rt, clock, 61)
        assert box.stages(s["id"]) == ["problem", "problem"]
    finally:
        rt.scheduler = None


async def test_3c_ack_kept_when_up_was_never_observed(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    """Intended: without an observed status change it is the same, acknowledged problem."""
    _, s = await _setup(admin)
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    await admin.post(f"/api/sensors/{s['id']}/acknowledge", json={"comment": ""})
    await rt.drain()
    for _ in range(5):  # the address answered only between two polls
        await tick(rt, clock, 60)
        await run(rt, clock, s["id"], DOWN)
    assert box.stages() == ["problem"]
    assert (await admin.get(f"/api/sensors/{s['id']}")).json()["acknowledgement"] is not None
    assert len(await _sensor_events(rt, s["id"], "status_change")) == 2  # unknown>up, up>down


async def test_3d_old_chat_ack_button_is_stale_for_the_second_problem(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    from netmon.chatack.service import problem_of

    _, s = await _setup(admin)
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    first = box.items[0][1]["event_id"]
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    async with rt.sessionmaker() as session:
        pr = await problem_of(session, s["id"], first)
        assert pr is not None and not pr.active  # pressing it answers "no longer active"
        pr2 = await problem_of(session, s["id"], box.items[2][1]["event_id"])
        assert pr2 is not None and pr2.active


# ------------------------------------------------------------------ 4. several sensors
async def test_4_all_sensors_of_the_device_alert_independently(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    """No device-internal dependency: ping and SNMP sensors each go down (not unreachable)
    and each notifies on both outages."""
    ch = await channel(admin, "ops")
    dev = await make_device(admin, name="sw1", host="10.9.0.1")
    ping = await make_sensor(admin, dev["id"], "ping", name="Ping")
    sysup = await make_sensor(admin, dev["id"], "snmp_system", name="System")
    port = await make_sensor(
        admin, dev["id"], "snmp_interface", {"if_index": 3, "if_name": "Gi1/0/3"}
    )
    await _rule(admin, ch, **DEFAULT_RULE)
    ids = [ping["id"], sysup["id"], port["id"]]
    ok = {
        ping["id"]: UP,
        sysup["id"]: CheckResult(True, "", {}),
        port["id"]: CheckResult(True, "", {"oper_status": 1.0}),
    }
    for sid in ids:
        await run(rt, clock, sid, ok[sid])
    for _ in range(2):
        clock.advance(60)
        for sid in ids:
            await run(rt, clock, sid, DOWN)
        for sid in ids:
            st = (await admin.get(f"/api/sensors/{sid}")).json()["status"]
            assert st == "down"
        clock.advance(60)
        for sid in ids:
            await run(rt, clock, sid, ok[sid])
    for sid in ids:
        assert box.stages(sid) == ["problem", "recovery", "problem", "recovery"]


async def test_4b_switch_with_an_up_parent_held_then_notified_both_times(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    ch = await channel(admin, "ops")
    gw = await make_device(admin, name="core", host="10.2.0.1")
    gp = await make_sensor(admin, gw["id"], "ping", name="Ping", interval_s=30)
    sw = await make_device(
        admin, name="sw", host="10.2.0.2", dependency={"mode": "device", "device_id": gw["id"]}
    )
    s = await make_sensor(admin, sw["id"], "ping", name="Ping")
    await _rule(admin, ch, **DEFAULT_RULE)
    rt.scheduler = RecordingScheduler()  # type: ignore[assignment]
    try:
        await run(rt, clock, gp["id"], UP)
        await run(rt, clock, s["id"], UP)
        for _ in range(2):
            clock.advance(60)
            await run(rt, clock, s["id"], DOWN)
            assert box.stages(s["id"]).count("problem") < 2
            await tick(rt, clock, 61)  # parent interval + timeout
            await run(rt, clock, s["id"], UP)
        assert box.stages(s["id"]) == ["problem", "recovery", "problem", "recovery"]
    finally:
        rt.scheduler = None


# ------------------------------------------------------------------ 5. flapping
def test_5_ip_change_produces_no_oper_status_transition() -> None:
    """A failed (unreachable) SNMP poll is no flapping input, and the poll after the address
    is back sees the same oper_status as before the outage."""
    from types import SimpleNamespace

    from netmon.poller.processor import _oper_transition
    from netmon.poller.types import evaluate

    sensor = SimpleNamespace(
        type="snmp_interface", device_id=1, config={"if_index": 3, "if_name": "Gi1/0/3"}
    )
    row = SimpleNamespace(key="oper_status", last_value=1.0)
    now = datetime.now(UTC)
    defs = [("oper_status", "", {})]
    failed = evaluate(CheckResult(False, "timeout"), defs, 1.0, now)
    assert _oper_transition(sensor, {"oper_status": row}, failed) is None  # type: ignore[arg-type]
    back = evaluate(CheckResult(True, "", {"oper_status": 1.0}), defs, 1.0, now)
    assert _oper_transition(sensor, {"oper_status": row}, back) is None  # type: ignore[arg-type]


async def test_5b_flapping_port_holds_the_interface_problem_silently(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    """Intended (architecture 12.4): an interface sensor of a flapping port sends no problem;
    only `alert_state.flap_suppressed` records it (no event); the ping sensor is unaffected."""
    ch = await channel(admin, "ops")
    dev = await make_device(admin, name="sw1", host="10.9.0.1")
    ping = await make_sensor(admin, dev["id"], "ping", name="Ping")
    port = await make_sensor(
        admin, dev["id"], "snmp_interface", {"if_index": 3, "if_name": "Gi1/0/3"}
    )
    await _rule(admin, ch, **DEFAULT_RULE)
    up_port = CheckResult(True, "", {"oper_status": 1.0})
    await run(rt, clock, ping["id"], UP)
    await run(rt, clock, port["id"], up_port)
    flap = rt.flapping
    assert flap is not None
    flap.suppressed_sensors.add(port["id"])
    try:
        await run(rt, clock, ping["id"], DOWN)
        await run(rt, clock, port["id"], DOWN)
        assert box.stages(ping["id"]) == ["problem"]
        assert box.stages(port["id"]) == []
        [row] = await state(rt, port["id"])
        assert row["flap_suppressed"] is True and row["primary_sent_at"] is None
        assert await _sensor_events(rt, port["id"], "notification_sent") == []
        assert await _sensor_events(rt, port["id"], "notification") == []
    finally:
        flap.suppressed_sensors.discard(port["id"])
    await rt.alerts.left_flapping([port["id"]])  # the quiet period is over: sent once
    await rt.drain()
    assert box.stages(port["id"]) == ["problem"]
    assert box.items[-1][1]["after_flapping"] is True


# ------------------------------------------------------------------ 6. up never observed
async def test_6_up_between_two_polls_is_one_continuous_problem(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    _, s = await _setup(admin)
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    for _ in range(3):
        await tick(rt, clock, 60)
        await run(rt, clock, s["id"], DOWN)
    assert box.stages() == ["problem"]
    [row] = await state(rt, s["id"])
    assert row["status"] == "down" and row["primary_sent_at"] is not None


# ------------------------------------------------------------------ delays (owner update: the
# second round arrived, late)
async def _sent_within(box: Box, n_problems: int, seconds: float) -> float:
    """Wall time until the background loop alone (no drain / notify) sent n problems."""
    t0 = asyncio.get_running_loop().time()
    while box.stages().count("problem") < n_problems:
        assert asyncio.get_running_loop().time() - t0 < seconds, box.stages()
        await asyncio.sleep(0.02)
    return asyncio.get_running_loop().time() - t0


async def test_d1_event_path_wakes_the_loop_no_30s_tick(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    _, s = await _setup(admin)
    await run(rt, clock, s["id"], UP)
    for n in (1, 2):
        await persist(rt, s["id"], DOWN, 1.0, clock.now())  # no drain, no notify
        assert await _sent_within(box, n, 3) < 3  # LOOP_MAX_S is 30
        await run(rt, clock, s["id"], UP)


async def test_d2_second_problem_waits_only_for_the_inflight_recovery(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    """The one source of delay that exists only for a repeated failure: the new problem row
    inherits the in-flight recovery's `sending_since` and is sent right after that send
    returns (bounded by the channel timeout), not at a 30 s tick."""
    _, s = await _setup(admin)
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    box.block = "recovery"
    await persist(rt, s["id"], UP, 1.0, clock.now())
    await asyncio.wait_for(box.entered.wait(), 5)
    await persist(rt, s["id"], DOWN, 1.0, clock.now())
    await asyncio.sleep(0.5)  # held while the recovery is in flight
    assert box.stages() == ["problem"]
    box.release.set()
    assert await _sent_within(box, 2, 3) < 3
    assert box.stages() == ["problem", "recovery", "problem"]


async def test_d3_orphaned_marker_holds_a_new_problem_up_to_stale_limit(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, box: Box
) -> None:
    """Only when a pass could not record its results (DB error; logged as "Alert loop
    iteration failed") does a marker outlive its send: the row then waits SENDING_STALE_S."""
    from netmon.alerts.engine import SENDING_STALE_S

    _, s = await _setup(admin)
    await run(rt, clock, s["id"], UP)
    await rt.alerts.stop()
    try:
        await persist(rt, s["id"], DOWN, 1.0, clock.now())
        await _events_only(rt)
        async with rt.sessionmaker() as session:
            await session.execute(
                text("UPDATE alert_state SET sending_since = :t WHERE sensor_id = :s"),
                {"t": clock.now(), "s": s["id"]},
            )
            await session.commit()
        await rt.alerts.run_once()
        assert box.stages() == []
        clock.advance(SENDING_STALE_S - 1)
        await rt.alerts.run_once()
        assert box.stages() == []
        clock.advance(2)
        await rt.alerts.run_once()
        assert box.stages() == ["problem"]
    finally:
        rt.alerts.start()
