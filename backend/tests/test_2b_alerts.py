"""[2b-1] Persistent alert state: delay, escalation, reminders, ack, maintenance, restart,
dependencies (unreachable). A FakeClock drives the alert loop; checks are fed through persist()."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from netmon.clock import FakeClock
from netmon.poller.processor import persist
from netmon.poller.types import CheckResult
from netmon.runtime import Runtime
from netmon.services import dependencies
from tests.conftest import make_device, make_sensor, requires_db

pytestmark = requires_db

UP = CheckResult(True, "", {"latency_ms": 1.0, "packet_loss_pct": 0.0})
DOWN = CheckResult(False, "Host unreachable")
WARN = CheckResult(True, "", {"latency_ms": 150.0, "packet_loss_pct": 0.0})


@pytest.fixture
async def clock(rt: Runtime, admin: httpx.AsyncClient) -> AsyncIterator[FakeClock]:
    old = rt.clock
    fake = FakeClock(datetime.now(UTC).replace(microsecond=0))
    rt.clock = fake
    yield fake
    rt.clock = old


class Outbox:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []
        self.fail: set[str] = set()

    async def __call__(self, channel, payload, smtp, lang):  # type: ignore[no-untyped-def]
        if channel["name"] in self.fail:
            raise RuntimeError("boom")
        self.items.append((channel["name"], payload))

    def stages(self) -> list[tuple[str, str]]:
        return [(c, p["stage"]) for c, p in self.items]

    def clear(self) -> None:
        self.items.clear()


@pytest.fixture
async def outbox(rt: Runtime, admin: httpx.AsyncClient) -> Outbox:
    box = Outbox()
    rt.alerts.sender = box
    return box


async def channel(c: httpx.AsyncClient, name: str) -> int:
    r = await c.post(
        "/api/notification-channels",
        json={"name": name, "type": "webhook", "config": {"url": f"http://{name}.invalid/"}},
    )
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def run(rt: Runtime, clock: FakeClock, sid: int, raw: CheckResult) -> None:
    await persist(rt, sid, raw, 1.0, clock.now())
    await rt.drain()


async def tick(rt: Runtime, clock: FakeClock, seconds: float) -> None:
    clock.advance(seconds)
    if rt.maintenance is not None:
        await rt.maintenance.evaluate()
    await rt.drain()


async def state(rt: Runtime, sid: int) -> list[dict[str, Any]]:
    async with rt.sessionmaker() as session:
        rows = await session.execute(
            text("SELECT * FROM alert_state WHERE sensor_id = :s ORDER BY rule_id"), {"s": sid}
        )
        return [dict(r._mapping) for r in rows]  # noqa: SLF001


# ------------------------------------------------------------------ delay / escalation / reminder
async def test_delay_problem_escalation_reminders_recovery(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    p, e = await channel(admin, "primary"), await channel(admin, "oncall")
    dev = await make_device(admin, name="core-sw", host="10.0.0.2")
    s = await make_sensor(admin, dev["id"], "ping", name="Ping")
    r = await admin.post(
        "/api/alert-rules",
        json={
            "name": "Core",
            "on_statuses": ["down", "up"],
            "delay_s": 60,
            "channel_ids": [p],
            "escalation_after_s": 300,
            "escalation_channel_ids": [e],
            "repeat_every_s": 600,
        },
    )
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["escalation_after_s"] == 300 and rule["escalation_channel_ids"] == [e]
    assert rule["repeat_every_s"] == 600
    await run(rt, clock, s["id"], UP)
    t0 = clock.now()
    await run(rt, clock, s["id"], DOWN)
    assert outbox.items == []  # delayed
    [row] = await state(rt, s["id"])
    assert row["status"] == "down" and row["due_at"] == t0 + timedelta(seconds=60)
    assert row["escalation_due_at"] == t0 + timedelta(seconds=300)
    await tick(rt, clock, 59)
    assert outbox.items == []
    await tick(rt, clock, 2)  # t0+61
    assert outbox.stages() == [("primary", "problem")]
    payload = outbox.items[0][1]
    assert payload["event"] == "sensor.status_changed" and payload["new_status"] == "down"
    assert payload["problem_since"] == t0.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert payload["after_maintenance"] is False
    await tick(rt, clock, 240)  # t0+301: escalation (measured from problem_since)
    assert outbox.stages()[1:] == [("oncall", "escalation")]
    assert outbox.items[1][1]["event"] == "sensor.escalation"
    await tick(rt, clock, 361)  # t0+662 > 61+600: first reminder
    assert outbox.stages()[2:] == [("primary", "reminder")]
    assert outbox.items[2][1]["event"] == "sensor.reminder"
    await tick(rt, clock, 600)
    assert outbox.stages()[3:] == [("primary", "reminder")]
    [row] = await state(rt, s["id"])
    assert row["reminders_sent"] == 2 and sorted(row["notified_channel_ids"]) == sorted([p, e])
    # a long downtime sends one overdue reminder, not a burst
    await tick(rt, clock, 6000)
    assert len(outbox.items) == 5
    # recovery reaches every channel that was notified (primary and escalation)
    outbox.clear()
    await run(rt, clock, s["id"], UP)
    assert outbox.items == []  # the recovery is delayed like the problem
    await tick(rt, clock, 61)
    assert sorted(outbox.stages()) == [("oncall", "recovery"), ("primary", "recovery")]
    assert all(p_["new_status"] == "up" for _, p_ in outbox.items)
    assert await state(rt, s["id"]) == []


async def test_subjects_and_body(rt: Runtime) -> None:
    from netmon.alerts import templates

    base = {
        "event": "sensor.escalation",
        "stage": "escalation",
        "new_status": "down",
        "old_status": "up",
        "device": {"name": "core-sw", "host": "h"},
        "sensor": {"name": "Ping", "type": "ping"},
        "problem_since": "2026-11-05T08:40:00Z",
        "time": "2026-11-05T08:40:00Z",
    }
    assert templates.subject(base, "en") == "[NetworkMonitor] ESCALATION DOWN: core-sw / Ping"
    assert templates.subject(base, "he") == "[NetworkMonitor] הסלמה DOWN: core-sw / Ping"
    rem = {**base, "stage": "reminder"}
    assert templates.subject(rem, "en") == "[NetworkMonitor] REMINDER DOWN: core-sw / Ping"
    assert templates.subject(rem, "he") == "[NetworkMonitor] תזכורת DOWN: core-sw / Ping"
    after = {**base, "stage": "problem", "after_maintenance": True}
    assert (
        templates.subject(after, "en")
        == "[NetworkMonitor] DOWN (after maintenance): core-sw / Ping"
    )
    assert "(לאחר תחזוקה)" in templates.subject(after, "he")
    token = templates.DISPLAY_TZ.set("Asia/Jerusalem")
    try:
        body = templates.text_body(base, "en")
        assert "Problem since: 2026-11-05 10:40 (Asia/Jerusalem)" in body
        assert "בעיה מאז" in templates.text_body(base, "he")
    finally:
        templates.DISPLAY_TZ.reset(token)


# ------------------------------------------------------------------ acknowledge
async def test_ack_stops_escalation_and_reminders(
    rt: Runtime,
    admin: httpx.AsyncClient,
    viewer: httpx.AsyncClient,
    clock: FakeClock,
    outbox: Outbox,
) -> None:
    p, e = await channel(admin, "primary"), await channel(admin, "oncall")
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "ping")
    await admin.post(
        "/api/alert-rules",
        json={
            "name": "r",
            "on_statuses": ["down", "warning", "up"],
            "channel_ids": [p],
            "escalation_after_s": 300,
            "escalation_channel_ids": [e],
            "repeat_every_s": 300,
        },
    )
    # only down/warning can be acknowledged
    await run(rt, clock, s["id"], UP)
    r = await admin.post(f"/api/sensors/{s['id']}/acknowledge", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_acknowledgeable"
    await run(rt, clock, s["id"], DOWN)
    assert outbox.stages() == [("primary", "problem")]
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    rt.hub.listeners.append(q)
    r = await viewer.post(f"/api/sensors/{s['id']}/acknowledge", json={"comment": "x"})
    assert r.status_code == 403
    r = await admin.post(
        f"/api/sensors/{s['id']}/acknowledge", json={"comment": "Replacing PSU, ETA 14:00"}
    )
    assert r.status_code == 200, r.text
    ack = r.json()["acknowledgement"]
    assert ack["by"] == "admin" and ack["comment"] == "Replacing PSU, ETA 14:00"
    assert ack["status"] == "down" and ack["at"].endswith("Z")
    assert r.json()["status"] == "down"  # state is not hidden
    msgs = [m for m in _drain_q(q) if m["type"] == "sensor.ack"]
    assert msgs and msgs[0]["data"]["acknowledgement"]["comment"] == "Replacing PSU, ETA 14:00"
    ev = (await admin.get("/api/events", params={"kind": "acknowledge"})).json()["items"]
    assert ev[0]["message"] == "Acknowledged by admin: Replacing PSU, ETA 14:00"
    assert ev[0]["old_status"] == ev[0]["new_status"] == "down"
    # re-ack replaces the comment
    r = await admin.post(f"/api/sensors/{s['id']}/acknowledge")
    assert r.status_code == 200 and r.json()["acknowledgement"]["comment"] == ""
    ev = (await admin.get("/api/events", params={"kind": "acknowledge"})).json()["items"]
    assert ev[0]["message"] == "Acknowledged by admin"
    d = (await admin.get("/api/dashboard")).json()
    assert d["acknowledged_count"] == 1 and d["problem_sensors"] == []
    assert d["acknowledged_sensors"][0]["id"] == s["id"]
    lst = (await admin.get("/api/sensors", params={"acknowledged": "true"})).json()
    assert [x["id"] for x in lst["items"]] == [s["id"]]
    tree = (await admin.get("/api/tree")).json()
    assert tree["groups"][0]["devices"][0]["sensors"][0]["acknowledged"] is True
    # no escalation, no reminders while acknowledged
    await tick(rt, clock, 3600)
    assert outbox.stages() == [("primary", "problem")]
    # unacknowledge: reminders resume, escalation does not restart
    r = await admin.delete(f"/api/sensors/{s['id']}/acknowledge")
    assert r.status_code == 200 and r.json()["acknowledgement"] is None
    assert (await admin.delete(f"/api/sensors/{s['id']}/acknowledge")).status_code == 200
    ev = (await admin.get("/api/events", params={"kind": "acknowledge"})).json()["items"]
    assert ev[0]["message"] == "Acknowledgement removed by admin" and len(ev) == 3
    await tick(rt, clock, 301)
    # reminders resume and the escalation that the ack prevented restarts from the unack (M1)
    assert outbox.stages()[0] == ("primary", "problem")
    assert sorted(outbox.stages()[1:]) == [("oncall", "escalation"), ("primary", "reminder")]
    outbox.items[:] = outbox.items[:1] + [i for i in outbox.items[1:] if i[0] == "primary"]
    # ack again, then a status change auto-clears the acknowledgement (no extra event)
    await admin.post(f"/api/sensors/{s['id']}/acknowledge", json={"comment": "c"})
    _drain_q(q)
    await run(rt, clock, s["id"], WARN)
    got = (await admin.get(f"/api/sensors/{s['id']}")).json()
    assert got["status"] == "warning" and got["acknowledgement"] is None
    acks = [m for m in _drain_q(q) if m["type"] == "sensor.ack"]
    assert acks and acks[-1]["data"]["acknowledgement"] is None
    assert (
        len((await admin.get("/api/events", params={"kind": "acknowledge"})).json()["items"]) == 4
    )
    # down -> warning is a change within the problem: notified again
    assert outbox.stages()[-1] == ("primary", "problem")
    await admin.post(f"/api/sensors/{s['id']}/acknowledge", json={"comment": "c"})
    outbox.clear()
    # the recovery of an acknowledged problem still reaches everyone notified
    await run(rt, clock, s["id"], UP)
    assert sorted(outbox.stages()) == [("oncall", "recovery"), ("primary", "recovery")]
    rt.hub.listeners.remove(q)


def _drain_q(q: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def test_rule_validation(admin: httpx.AsyncClient) -> None:
    p = await channel(admin, "p")
    base = {"name": "x", "on_statuses": ["down"], "channel_ids": [p]}
    bad = [
        {"escalation_after_s": 300},  # without channels
        {"escalation_channel_ids": [p]},  # without after
        {"escalation_after_s": 30, "escalation_channel_ids": [p]},  # < 60
        {"escalation_after_s": 120, "escalation_channel_ids": [p], "delay_s": 300},
        {"escalation_after_s": 120, "escalation_channel_ids": [999]},
        {"repeat_every_s": 60},
        {"repeat_every_s": 600, "on_statuses": ["up"]},
    ]
    for extra in bad:
        r = await admin.post("/api/alert-rules", json={**base, **extra})
        assert r.status_code == 422, (extra, r.text)
    r = await admin.post(
        "/api/alert-rules",
        json={**base, "escalation_after_s": 120, "escalation_channel_ids": [p]},
    )
    assert r.status_code == 201
    rid = r.json()["id"]
    r = await admin.patch(f"/api/alert-rules/{rid}", json={"escalation_after_s": None})
    assert r.status_code == 200, r.text
    assert r.json()["escalation_after_s"] is None and r.json()["escalation_channel_ids"] == []


# ------------------------------------------------------------------ maintenance
async def test_maintenance_suppresses_then_one_alert_after(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    p = await channel(admin, "primary")
    dev = await make_device(admin, name="srv", host="10.0.0.9")
    other = await make_device(admin, name="other", host="10.0.0.10")
    s = await make_sensor(admin, dev["id"], "ping", name="Ping")
    s2 = await make_sensor(admin, dev["id"], "ping", name="Ping2")
    s3 = await make_sensor(admin, other["id"], "ping", name="Ping3")
    await admin.post(
        "/api/alert-rules",
        json={"name": "r", "on_statuses": ["down", "up"], "channel_ids": [p]},
    )
    for sid in (s["id"], s2["id"], s3["id"]):
        await run(rt, clock, sid, UP)
    # s3 (other device) is notified before the window; its recovery inside a window of its
    # own device is still sent
    await run(rt, clock, s3["id"], DOWN)
    assert outbox.stages() == [("primary", "problem")]
    outbox.clear()
    now = clock.now()
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    rt.hub.listeners.append(q)
    r = await admin.post(
        "/api/maintenance-windows",
        json={
            "name": "Night patch",
            "scope": "global",
            "kind": "once",
            "starts_at": _z(now - timedelta(minutes=1)),
            "ends_at": _z(now + timedelta(hours=1)),
        },
    )
    assert r.status_code == 201, r.text
    win = r.json()
    assert win["active_now"] is True and win["occurrence_end"] == _z(now + timedelta(hours=1))
    changed = [m for m in _drain_q(q) if m["type"] == "maintenance.changed"]
    assert changed and changed[0]["data"]["active_window_ids"] == [win["id"]]
    assert sorted(changed[0]["data"]["entered_device_ids"]) == sorted([dev["id"], other["id"]])
    ev = (await admin.get("/api/events", params={"kind": "maintenance"})).json()["items"]
    assert ev[0]["message"] == 'Maintenance "Night patch" started'
    got = (await admin.get(f"/api/devices/{dev['id']}")).json()
    assert got["in_maintenance"] is True and got["maintenance"]["window_id"] == win["id"]
    assert (await admin.get(f"/api/sensors/{s['id']}")).json()["in_maintenance"] is True
    await run(rt, clock, s3["id"], UP)  # recovery of a pre-window problem: sent
    assert outbox.stages() == [("primary", "recovery")]
    outbox.clear()
    await run(rt, clock, s["id"], DOWN)  # suppressed
    await run(rt, clock, s2["id"], DOWN)
    await tick(rt, clock, 60)
    await run(rt, clock, s2["id"], UP)  # problem started and ended inside the window
    assert outbox.items == []
    sc = await admin.get("/api/events", params={"kind": "status_change", "in_maintenance": "true"})
    assert sc.json()["total"] >= 3
    [row] = await state(rt, s["id"])
    assert row["maintenance_suppressed"] is True and row["due_at"] is None
    await tick(rt, clock, 3600)  # window over
    assert outbox.stages() == [("primary", "problem")]
    payload = outbox.items[0][1]
    assert payload["after_maintenance"] is True and payload["sensor"]["id"] == s["id"]
    ev = (await admin.get("/api/events", params={"kind": "maintenance"})).json()["items"]
    assert ev[0]["message"] == 'Maintenance "Night patch" ended'
    assert (await admin.get(f"/api/devices/{dev['id']}")).json()["in_maintenance"] is False
    await tick(rt, clock, 600)
    assert len(outbox.items) == 1  # once
    rt.hub.listeners.remove(q)


def _z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ restart
async def test_state_survives_restart(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    p, e = await channel(admin, "primary"), await channel(admin, "oncall")
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "ping")
    await admin.post(
        "/api/alert-rules",
        json={
            "name": "r",
            "on_statuses": ["down", "up"],
            "channel_ids": [p],
            "escalation_after_s": 900,
            "escalation_channel_ids": [e],
        },
    )
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    assert outbox.stages() == [("primary", "problem")]
    # "restart": stop this process' engine, build a new runtime against the same database
    await rt.alerts.stop()
    rt2 = Runtime(rt.settings)
    try:
        await rt2.start(run_poller=False, take_lock=False, migrate=False, maintenance=False)
        rt2.clock = clock
        box2 = Outbox()
        rt2.alerts.sender = box2
        clock.advance(901)
        await rt2.alerts.idle()
        assert box2.stages() == [("oncall", "escalation")]
        await persist(rt2, s["id"], UP, 1.0, clock.now())
        await rt2.drain()
        assert sorted(box2.stages())[-2:] == [("oncall", "recovery"), ("primary", "recovery")]
    finally:
        await rt2.stop()
        rt.alerts.start()
    assert outbox.stages() == [("primary", "problem")]


# ------------------------------------------------------------------ dependencies
class RecordingScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[list[int], float]] = []

    def run_soon(self, sensor_ids: list[int], jitter_s: float = 5.0) -> None:
        self.calls.append((list(sensor_ids), jitter_s))


def _child_only_scheduler() -> Any:
    from netmon.poller.scheduler import Scheduler

    class ChildOnly(Scheduler):
        def __init__(self, rt: Runtime, skip: set[int]) -> None:
            super().__init__(rt)
            self.skip = skip

        def run_soon(self, sensor_ids: list[int], jitter_s: float = 5.0) -> None:
            super().run_soon([i for i in sensor_ids if i not in self.skip], jitter_s)

    return ChildOnly


def ChildOnlyScheduler(rt: Runtime, skip: set[int]) -> Any:  # noqa: N802
    return _child_only_scheduler()(rt, skip)


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.6.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_parent_down_children_unreachable_then_recheck(
    rt: Runtime,
    admin: httpx.AsyncClient,
    clock: FakeClock,
    outbox: Outbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dependencies, "RECHECK_JITTER_S", 0.0)
    p = await channel(admin, "primary")
    gid = (await admin.get("/api/groups", params={"parent_id": "root"})).json()["items"][0]["id"]
    r = await admin.post("/api/groups", json={"name": "Branch", "parent_id": gid})
    branch = r.json()
    gw = await make_device(admin, name="branch-gw", host="127.0.6.1", group_id=branch["id"])
    gw_ping = await make_sensor(admin, gw["id"], "ping", name="Ping")
    r = await admin.patch(
        f"/api/groups/{branch['id']}",
        json={"dependency": {"mode": "device", "device_id": gw["id"]}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["dependency"]["device_name"] == "branch-gw"
    port = _closed_port()
    kids = []
    for i in (2, 3):
        d = await make_device(admin, name=f"ap-{i}", host="127.0.6.1", group_id=branch["id"])
        s = await make_sensor(admin, d["id"], "tcp_port", {"port": port, "timeout_s": 2})
        kids.append((d, s))
    # self rule: the gateway lives in Branch but has no parent
    got = (await admin.get(f"/api/devices/{gw['id']}")).json()
    assert got["effective_dependency"] is None
    eff = (await admin.get(f"/api/devices/{kids[0][0]['id']}")).json()["effective_dependency"]
    assert eff == {
        "device_id": gw["id"],
        "device_name": "branch-gw",
        "sensor_id": gw_ping["id"],
        "sensor_name": "Ping",
        "source": "group",
        "group_id": branch["id"],
        "parent_down": False,
    }
    await admin.post(
        "/api/alert-rules",
        json={"name": "r", "on_statuses": ["down", "up"], "channel_ids": [p]},
    )
    await run(rt, clock, gw_ping["id"], UP)
    for _, s in kids:
        await run(rt, clock, s["id"], UP)
    # child 1 is checked before the parent's outage was noticed -> down, but its alert is
    # held (even with delay_s 0) and the parent is re-checked right away (H2); the cascade
    # turns the child unreachable when the parent is found down: no child DOWN at all
    recorder = RecordingScheduler()
    rt.scheduler = recorder  # type: ignore[assignment]
    try:
        clock.advance(1)
        await run(rt, clock, kids[0][1]["id"], DOWN)
        assert outbox.stages() == []
        assert recorder.calls == [([gw_ping["id"]], 0)]
        clock.advance(1)
        await run(rt, clock, gw_ping["id"], DOWN)
    finally:
        rt.scheduler = None
    assert outbox.stages() == [("primary", "problem")]  # only the parent alerts
    s1 = (await admin.get(f"/api/sensors/{kids[0][1]['id']}")).json()
    assert s1["status"] == "unreachable"
    assert s1["status_message"] == "Unreachable (parent branch-gw down): Host unreachable"
    # child 2 fails while the parent is down -> unreachable directly, no alert
    await run(rt, clock, kids[1][1]["id"], CheckResult(False, "Connection refused"))
    s2 = (await admin.get(f"/api/sensors/{kids[1][1]['id']}")).json()
    assert s2["status"] == "unreachable"
    assert s2["status_message"].startswith("Unreachable (parent branch-gw down): ")
    assert outbox.stages() == [("primary", "problem")]
    ev = (await admin.get("/api/events", params={"sensor_id": kids[0][1]["id"]})).json()["items"]
    # (the fake clock runs ahead of the cascade's wall-clock event time: no ordering check)
    assert ("down", "unreachable") in [(e["old_status"], e["new_status"]) for e in ev]
    # rollups, dashboard, tree
    dev1 = (await admin.get(f"/api/devices/{kids[0][0]['id']}")).json()
    assert dev1["status"] == "unreachable" and dev1["effective_dependency"]["parent_down"]
    assert (await admin.get(f"/api/groups/{branch['id']}")).json()["status"] == "down"
    dash = (await admin.get("/api/dashboard")).json()
    assert {u["name"] for u in dash["unreachable_devices"]} == {"ap-2", "ap-3"}
    assert dash["unreachable_devices"][0]["parent_device_name"] == "branch-gw"
    assert dash["sensor_counts"]["unreachable"] == 2
    tree = (await admin.get("/api/tree")).json()
    tdevs = {d["name"]: d for g in tree["groups"] for c in g["children"] for d in c["devices"]}
    assert (
        tdevs["ap-2"]["parent_device_id"] == gw["id"]
        and tdevs["branch-gw"]["parent_device_id"] is None
    )
    r = await admin.post(f"/api/sensors/{kids[0][1]['id']}/acknowledge")
    assert r.status_code == 409  # unreachable is not acknowledgeable
    assert await state(rt, kids[0][1]["id"]) == [] or all(
        x["status"] == "suspended" for x in await state(rt, kids[0][1]["id"])
    )
    await tick(rt, clock, 300)
    assert outbox.stages() == [("primary", "problem")]  # still only the parent
    clock.set(datetime.now(UTC))  # the re-checks below run on the wall clock
    # parent recovers -> children are re-checked (real tcp checks to a closed port): still
    # down -> unreachable -> down, which alerts like any new problem (after the hold)
    rt.scheduler = ChildOnlyScheduler(rt, {gw_ping["id"]})
    try:
        outbox.clear()
        await run(rt, clock, gw_ping["id"], UP)
        await rt.scheduler.idle()
        await rt.drain()
    finally:
        rt.scheduler = None
    for _, s in kids:
        got = (await admin.get(f"/api/sensors/{s['id']}")).json()
        assert got["status"] == "down", got
    # the real checks used wall-clock times; move the fake clock past them and the hold
    # (parent ping interval 60 s + timeout 60 s)
    clock.set(datetime.now(UTC) + timedelta(seconds=5))
    await rt.drain()
    assert outbox.stages() == [("primary", "recovery")]
    clock.set(datetime.now(UTC) + timedelta(seconds=125))
    await rt.drain()
    stages = outbox.stages()
    assert stages.count(("primary", "problem")) == 2  # both children, genuinely down
    assert stages.count(("primary", "recovery")) == 1  # the parent


async def test_dependency_api_and_cycles(admin: httpx.AsyncClient) -> None:
    gid = (await admin.get("/api/groups", params={"parent_id": "root"})).json()["items"][0]["id"]
    a = await make_device(admin, name="a", host="10.0.0.1")
    b = await make_device(admin, name="b", host="10.0.0.2")
    c = await make_device(admin, name="c", host="10.0.0.3")
    sb = await make_sensor(admin, b["id"], "ping")
    assert a["dependency"] == {
        "mode": "inherit",
        "device_id": None,
        "device_name": None,
        "sensor_id": None,
        "sensor_name": None,
    }
    # validation
    bad = [
        {"mode": "device"},
        {"mode": "device", "device_id": 999},
        {"mode": "device", "device_id": b["id"], "sensor_id": 999},
        {"mode": "none", "device_id": b["id"]},
        {"mode": "bogus"},
        {"mode": "none", "extra": 1},
    ]
    for dep in bad:
        r = await admin.patch(f"/api/devices/{a['id']}", json={"dependency": dep})
        assert r.status_code == 422, (dep, r.text)
    r = await admin.patch(
        f"/api/devices/{a['id']}",
        json={"dependency": {"mode": "device", "device_id": b["id"], "sensor_id": sb["id"]}},
    )
    assert r.status_code == 200
    assert r.json()["dependency"]["sensor_name"] == "Ping"
    assert r.json()["effective_dependency"]["source"] == "device"
    # direct cycle b -> a -> b
    r = await admin.patch(
        f"/api/devices/{b['id']}", json={"dependency": {"mode": "device", "device_id": a["id"]}}
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "dependency_cycle"
    path = r.json()["error"]["details"][0]["device_ids"]
    assert path[0] == path[-1] and set(path) == {a["id"], b["id"]}
    # explicit self dependency is a cycle
    r = await admin.patch(
        f"/api/devices/{c['id']}", json={"dependency": {"mode": "device", "device_id": c["id"]}}
    )
    assert r.status_code == 400
    # via group inheritance: group G depends on a; moving b into G makes b -> a -> b
    g = (
        await admin.post(
            "/api/groups",
            json={
                "name": "G",
                "parent_id": gid,
                "dependency": {"mode": "device", "device_id": a["id"]},
            },
        )
    ).json()
    r = await admin.patch(f"/api/devices/{b['id']}", json={"group_id": g["id"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "dependency_cycle"
    # a inside G with an explicit parent b: fine; c moved into G inherits a
    r = await admin.patch(f"/api/devices/{c['id']}", json={"group_id": g["id"]})
    assert r.status_code == 200
    assert r.json()["effective_dependency"]["device_id"] == a["id"]
    # bulk: atomic, cycles on the final state
    r = await admin.post(
        "/api/devices/dependencies",
        json={
            "items": [
                {"device_id": b["id"], "dependency": {"mode": "device", "device_id": c["id"]}},
                {"device_id": a["id"], "dependency": {"mode": "none"}},
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert sorted(r.json()["updated"]) == sorted([a["id"], b["id"]])
    r = await admin.post(
        "/api/devices/dependencies",
        json={
            "items": [
                {"device_id": a["id"], "dependency": {"mode": "device", "device_id": b["id"]}},
                {"device_id": c["id"], "dependency": {"mode": "none"}},
            ]
        },
    )
    assert r.status_code == 200  # a -> b -> c, c none
    r = await admin.post(
        "/api/devices/dependencies",
        json={
            "items": [
                {"device_id": c["id"], "dependency": {"mode": "device", "device_id": a["id"]}},
            ]
        },
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "dependency_cycle"
    assert (await admin.get(f"/api/devices/{c['id']}")).json()["dependency"]["mode"] == "none"
    r = await admin.post(
        "/api/devices/dependencies",
        json={
            "items": [
                {"device_id": c["id"], "dependency": {"mode": "none"}},
                {"device_id": c["id"], "dependency": {"mode": "none"}},
            ]
        },
    )
    assert r.status_code == 422
    # deleting a parent resets its dependents to none
    assert (await admin.delete(f"/api/devices/{b['id']}")).status_code == 204
    got = (await admin.get(f"/api/devices/{a['id']}")).json()
    assert got["dependency"]["mode"] == "none" and got["effective_dependency"] is None


async def test_delayed_child_alert_dropped_by_cascade(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    p = await channel(admin, "primary")
    gw = await make_device(admin, name="gw", host="10.1.0.1")
    gw_ping = await make_sensor(admin, gw["id"], "ping", name="Ping")
    child = await make_device(
        admin,
        name="cam",
        host="10.1.0.2",
        dependency={"mode": "device", "device_id": gw["id"], "sensor_id": gw_ping["id"]},
    )
    cs = await make_sensor(admin, child["id"], "ping", name="Ping")
    await admin.post(
        "/api/alert-rules",
        json={"name": "r", "on_statuses": ["down", "up"], "delay_s": 120, "channel_ids": [p]},
    )
    await run(rt, clock, gw_ping["id"], UP)
    await run(rt, clock, cs["id"], UP)
    clock.advance(1)
    await run(rt, clock, cs["id"], DOWN)  # the child noticed first: pending (delay)
    assert len(await state(rt, cs["id"])) == 1
    await run(rt, clock, gw_ping["id"], DOWN)
    assert (await admin.get(f"/api/sensors/{cs['id']}")).json()["status"] == "unreachable"
    assert await state(rt, cs["id"]) == []  # nothing was notified: the row is gone
    await tick(rt, clock, 121)
    assert [(p_["sensor"]["id"], p_["stage"]) for _, p_ in outbox.items] == [
        (gw_ping["id"], "problem")
    ]
    # paused wins over unreachable; resume -> unknown
    r = await admin.patch(f"/api/sensors/{cs['id']}", json={"enabled": False})
    assert r.json()["status"] == "paused"
