"""[if-noise] Sensor event noise (debounce, flapping collapse, noisiest sensors), the
important-port criteria and overrides, the apply-template interface filters' data and
migration 0019."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy import text

from netmon.clock import FakeClock
from netmon.db import timescale
from netmon.db.engine import make_engine
from netmon.db.migrate import migration_head, run_migrations
from netmon.poller.processor import persist
from netmon.poller.types import CheckResult
from netmon.runtime import Runtime
from netmon.services.event_noise import check_timeout, hold_seconds
from tests.conftest import TEST_DATABASE_URL, _plain, db_url_for, make_device, make_sensor
from tests.conftest import requires_db as _requires_db

pytestmark = _requires_db

UP = CheckResult(True, "", {"latency_ms": 1.0, "packet_loss_pct": 0.0})
DOWN = CheckResult(False, "Host unreachable")
WARN = CheckResult(True, "", {"latency_ms": 150.0, "packet_loss_pct": 0.0})
HEAD = migration_head()


# ------------------------------------------------------------------ helpers
@pytest.fixture
async def clock(rt: Runtime, admin: httpx.AsyncClient) -> AsyncIterator[FakeClock]:
    old = rt.clock
    fake = FakeClock(datetime.now(UTC).replace(microsecond=0))
    rt.clock = fake
    if rt.maintenance is not None:
        await rt.maintenance.evaluate()  # the alert loop sends problems once it has run
    yield fake
    rt.clock = old


class Outbox:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, channel, payload, smtp, lang):  # type: ignore[no-untyped-def]
        self.items.append((channel["name"], payload))


@pytest.fixture
async def outbox(rt: Runtime, admin: httpx.AsyncClient) -> Outbox:
    box = Outbox()
    rt.alerts.sender = box
    return box


@pytest.fixture
def ws(rt: Runtime) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    rt.hub.listeners.append(q)
    return q


def drained(q: asyncio.Queue) -> list[dict[str, Any]]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def run(rt: Runtime, sid: int, raw: CheckResult, at: datetime) -> None:
    if isinstance(rt.clock, FakeClock) and at > rt.clock.now():
        rt.clock.set(at)  # the alert engine compares due times with the clock
    await persist(rt, sid, raw, 1.0, at)
    await rt.drain()


async def rows(rt: Runtime, sid: int) -> list[tuple[str, str | None, str | None, str | None]]:
    """(kind, old, new, noise) of every event row of the sensor, oldest first."""
    async with rt.sessionmaker() as s:
        res = await s.execute(
            text(
                "SELECT kind, old_status, new_status, noise FROM events WHERE sensor_id = :s "
                "AND kind IN ('status_change', 'sensor_flapping') ORDER BY time, id"
            ),
            {"s": sid},
        )
        return [tuple(r) for r in res]  # type: ignore[misc]


async def shown(c: httpx.AsyncClient, sid: int) -> list[tuple[str, str | None, str | None]]:
    items = (
        await c.get(
            "/api/events", params={"sensor_id": sid, "kind": "status_change,sensor_flapping"}
        )
    ).json()["items"]
    return [(e["kind"], e["old_status"], e["new_status"]) for e in reversed(items)]


async def ping(admin: httpx.AsyncClient, name: str = "pc-port") -> int:
    dev = await make_device(admin, name=f"dev-{name}", host="10.9.0.1")
    s = await make_sensor(admin, dev["id"], "ping", name=name, interval_s=60)
    return int(s["id"])


# ------------------------------------------------------------------ pure
def test_hold_seconds() -> None:
    assert hold_seconds(60, 1, 0) == 90  # the next poll (with jitter) is inside the hold
    assert hold_seconds(60, 2, 0) == 150
    assert hold_seconds(30, 1, 300) == 300  # the minimum wins
    assert hold_seconds(0, 0, 0) == 1.5  # sane floor
    assert check_timeout(60) == 60 and check_timeout(600) == 120


# ------------------------------------------------------------------ settings
async def test_defaults_on_and_validation(admin: httpx.AsyncClient) -> None:
    s = (await admin.get("/api/settings")).json()
    assert s["sensor_event_debounce_enabled"] is True
    assert s["sensor_event_debounce_polls"] == 1
    assert s["sensor_event_debounce_min_s"] == 0
    assert s["sensor_event_flap_enabled"] is True
    assert s["important_port_criteria"] == ["uplink", "lag", "trunk", "irf"]
    assert s["important_port_description_pattern"] == ""
    for bad in (
        {"sensor_event_debounce_polls": 0},
        {"sensor_event_debounce_polls": 11},
        {"sensor_event_debounce_min_s": -1},
        {"sensor_event_debounce_min_s": 3601},
        {"important_port_criteria": ["uplink", "toaster"]},
        {"important_port_description_pattern": "(a)\\1"},  # backreference: not RE2
    ):
        assert (await admin.put("/api/settings", json=bad)).status_code == 422, bad
    r = await admin.put(
        "/api/settings",
        json={
            "important_port_criteria": ["critical_sensor", "uplink", "uplink"],
            "important_port_description_pattern": "  uplink|core  ",
        },
    )
    assert r.status_code == 200
    assert r.json()["important_port_criteria"] == ["uplink", "critical_sensor"]
    assert r.json()["important_port_description_pattern"] == "uplink|core"


# ------------------------------------------------------------------ debounce
async def test_blip_leaves_no_event_row_but_alerts_are_not_delayed(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox, ws: asyncio.Queue
) -> None:
    r = await admin.post(
        "/api/notification-channels",
        json={"name": "hook", "type": "webhook", "config": {"url": "http://hook.invalid/"}},
    )
    hook = r.json()["id"]
    r = await admin.post(
        "/api/alert-rules",
        json={"name": "all", "on_statuses": ["down", "up"], "delay_s": 0, "channel_ids": [hook]},
    )
    assert r.status_code == 201
    sid = await ping(admin)
    t0 = clock.now()
    await run(rt, sid, UP, t0)
    drained(ws)
    await run(rt, sid, DOWN, t0 + timedelta(seconds=60))
    # the sensor's current status is real-time and the DOWN alert went out at once
    assert (await admin.get(f"/api/sensors/{sid}")).json()["status"] == "down"
    assert [p["stage"] for _, p in outbox.items] == ["problem"]
    msgs = drained(ws)
    types = [m["type"] for m in msgs]
    assert "sensor.update" in types
    created = [m["data"]["kind"] for m in msgs if m["type"] == "event.created"]
    assert "status_change" not in created  # (the alert's notification_sent row is shown)
    assert await shown(admin, sid) == [("status_change", "unknown", "up")]
    # recovered on the next poll: both rows debounced, never shown
    await run(rt, sid, UP, t0 + timedelta(seconds=120))
    assert [p["stage"] for _, p in outbox.items] == ["problem", "recovery"]
    assert await rows(rt, sid) == [
        ("status_change", "unknown", "up", None),
        ("status_change", "up", "down", "debounced"),
        ("status_change", "down", "up", "debounced"),
    ]
    await rt.event_noise.sweep(clock.now() + timedelta(hours=1))
    assert await shown(admin, sid) == [("status_change", "unknown", "up")]
    assert "status_change" not in [
        m["data"]["kind"] for m in drained(ws) if m["type"] == "event.created"
    ]
    # include / only
    r = await admin.get(
        "/api/events", params={"sensor_id": sid, "noise": "only", "kind": "status_change"}
    )
    assert [e["noise"] for e in r.json()["items"]] == ["debounced", "debounced"]
    r = await admin.get(
        f"/api/sensors/{sid}/events", params={"noise": "include", "kind": "status_change"}
    )
    assert r.json()["total"] == 3
    assert (await admin.get("/api/events", params={"noise": "bogus"})).status_code == 422
    dash = (await admin.get("/api/dashboard")).json()
    assert [e["new_status"] for e in dash["recent_events"]] == ["up"]


async def test_change_confirmed_by_the_next_check_is_shown(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, ws: asyncio.Queue
) -> None:
    sid = await ping(admin)
    t0 = clock.now()
    await run(rt, sid, UP, t0)
    t1 = t0 + timedelta(seconds=60)
    await run(rt, sid, DOWN, t1)
    assert await shown(admin, sid) == [("status_change", "unknown", "up")]
    drained(ws)
    await run(rt, sid, DOWN, t1 + timedelta(seconds=60))  # still down: confirmed, no new row
    assert await shown(admin, sid) == [
        ("status_change", "unknown", "up"),
        ("status_change", "up", "down"),
    ]
    created = [m["data"] for m in drained(ws) if m["type"] == "event.created"]
    assert [(e["new_status"], e["noise"], e["time"]) for e in created] == [
        ("down", None, t1.strftime("%Y-%m-%dT%H:%M:%SZ"))  # the real change time
    ]
    assert not rt.event_noise._pending  # noqa: SLF001
    # two confirming checks with polls = 2; and min_s on top
    await admin.put("/api/settings", json={"sensor_event_debounce_polls": 2})
    t2 = t1 + timedelta(seconds=600)
    await run(rt, sid, UP, t2)
    await run(rt, sid, UP, t2 + timedelta(seconds=60))
    assert (await rows(rt, sid))[-1][3] == "pending"
    await run(rt, sid, UP, t2 + timedelta(seconds=120))
    assert (await rows(rt, sid))[-1][3] is None


async def test_change_no_check_confirms_is_shown_after_the_hold(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, ws: asyncio.Queue
) -> None:
    """E.g. the sensor was paused right after the change: the sweeper shows it after the hold."""
    sid = await ping(admin)
    t0 = clock.now()
    await run(rt, sid, UP, t0)
    t1 = t0 + timedelta(seconds=60)
    await run(rt, sid, DOWN, t1)
    async with rt.sessionmaker() as s:
        until = (
            await s.execute(
                text("SELECT noise_until FROM events WHERE sensor_id = :s AND noise = 'pending'"),
                {"s": sid},
            )
        ).scalar_one()
    assert until == t1 + timedelta(seconds=90 + 60 + 5)
    drained(ws)
    await rt.event_noise.sweep(until - timedelta(seconds=1))
    assert await shown(admin, sid) == [("status_change", "unknown", "up")]
    await rt.event_noise.sweep(until)
    assert await shown(admin, sid) == [
        ("status_change", "unknown", "up"),
        ("status_change", "up", "down"),
    ]
    created = [m["data"] for m in drained(ws) if m["type"] == "event.created"]
    assert [e["new_status"] for e in created] == ["down"]
    assert not rt.event_noise._pending  # noqa: SLF001
    # a recovery after the hold is a normal change again
    await run(rt, sid, UP, t1 + timedelta(seconds=600))
    assert (await rows(rt, sid))[-1] == ("status_change", "down", "up", "pending")


async def test_a_different_change_shows_the_pending_one(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    sid = await ping(admin)
    t0 = clock.now()
    await run(rt, sid, UP, t0)
    await run(rt, sid, WARN, t0 + timedelta(seconds=60))
    await run(rt, sid, DOWN, t0 + timedelta(seconds=120))  # not back to up: warning did hold
    assert await rows(rt, sid) == [
        ("status_change", "unknown", "up", None),
        ("status_change", "up", "warning", None),
        ("status_change", "warning", "down", "pending"),
    ]


async def test_debounce_polls_and_min_seconds(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    await admin.put("/api/settings", json={"sensor_event_flap_enabled": False})
    sid = await ping(admin)
    t0 = clock.now()
    await run(rt, sid, UP, t0)
    # down for two polls: more than the default hold (1.5 intervals) -> shown
    await run(rt, sid, DOWN, t0 + timedelta(seconds=60))
    await run(rt, sid, UP, t0 + timedelta(seconds=180))
    assert [r[3] for r in await rows(rt, sid)] == [None, None, "pending"]
    await rt.event_noise.sweep(t0 + timedelta(hours=1))
    r = await admin.put("/api/settings", json={"sensor_event_debounce_polls": 2})
    assert r.status_code == 200 and rt.event_noise.polls == 2
    await run(rt, sid, DOWN, t0 + timedelta(seconds=400))
    await run(rt, sid, UP, t0 + timedelta(seconds=520))  # 120 s <= 2.5 x 60 s: a blip
    assert [r[3] for r in await rows(rt, sid)][-2:] == ["debounced", "debounced"]
    await admin.put(
        "/api/settings", json={"sensor_event_debounce_polls": 1, "sensor_event_debounce_min_s": 600}
    )
    await run(rt, sid, DOWN, t0 + timedelta(seconds=600))
    await run(rt, sid, UP, t0 + timedelta(seconds=1100))  # 500 s <= 600 s
    assert [r[3] for r in await rows(rt, sid)][-2:] == ["debounced", "debounced"]


async def test_debounce_off_restores_the_previous_behaviour(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, ws: asyncio.Queue
) -> None:
    sid = await ping(admin)
    t0 = clock.now()
    await run(rt, sid, UP, t0)
    await run(rt, sid, DOWN, t0 + timedelta(seconds=60))  # pending
    drained(ws)
    r = await admin.put(
        "/api/settings",
        json={"sensor_event_debounce_enabled": False, "sensor_event_flap_enabled": False},
    )
    assert r.status_code == 200
    # turning it off shows what it held back
    assert [m["data"]["new_status"] for m in drained(ws) if m["type"] == "event.created"] == [
        "down"
    ]
    await run(rt, sid, UP, t0 + timedelta(seconds=120))
    for i in range(8):  # would be flapping with the protection on
        await run(rt, sid, DOWN if i % 2 == 0 else UP, t0 + timedelta(seconds=180 + 60 * i))
    got = await rows(rt, sid)
    assert all(r[3] is None for r in got) and all(r[0] == "status_change" for r in got)
    assert len(await shown(admin, sid)) == len(got) == 11
    created = [m for m in drained(ws) if m["type"] == "event.created"]
    assert len(created) == 9  # one event.created per change, at once (as before)


async def test_restart_shows_rows_whose_hold_passed(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    """Pending rows live in the database: a sweep after a restart shows the due ones."""
    sid = await ping(admin)
    t0 = clock.now()
    await run(rt, sid, UP, t0)
    await run(rt, sid, DOWN, t0 + timedelta(seconds=60))
    rt.event_noise.reset()  # in-memory state lost
    await rt.event_noise.sweep(t0 + timedelta(hours=2))
    assert (await rows(rt, sid))[-1][3] is None


# ------------------------------------------------------------------ flapping
async def test_flapping_collapses_into_one_row_and_settles(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, ws: asyncio.Queue
) -> None:
    sid = await ping(admin)
    t0 = clock.now()
    await run(rt, sid, UP, t0)
    # flap_threshold 5 changes within 10 min (shared with port flapping)
    t = t0
    for i in range(9):
        t = t0 + timedelta(seconds=60 * (i + 1))
        await run(rt, sid, DOWN if i % 2 == 0 else UP, t)
    got = await rows(rt, sid)
    flaps = [r for r in got if r[0] == "sensor_flapping"]
    assert len(flaps) == 1
    changes = [r for r in got if r[0] == "status_change"]
    assert [r[3] for r in changes] == [None] + ["debounced"] * 4 + ["flapping"] * 5
    assert rt.event_noise.is_flapping(sid)
    async with rt.sessionmaker() as s:
        f = (
            await s.execute(
                text(
                    "SELECT flap_count, flap_last, flap_end, new_status, message FROM events "
                    "WHERE kind = 'sensor_flapping' AND sensor_id = :s"
                ),
                {"s": sid},
            )
        ).one()
    assert f.flap_count == 9 and f.flap_last == t and f.flap_end is None
    assert f.new_status == "down" and "is flapping" in f.message
    # one row in the log instead of nine
    assert await shown(admin, sid) == [
        ("status_change", "unknown", "up"),
        ("sensor_flapping", None, "down"),
    ]
    upd = [m for m in drained(ws) if m["type"] == "event.updated"]
    assert upd and upd[-1]["data"]["flap_count"] == 9
    noisy = (await admin.get("/api/events/noisy-sensors", params={"hours": 24})).json()
    top = noisy["items"][0]
    assert top["sensor_id"] == sid and top["changes"] == 10 and top["flapping"] is True
    assert top["shown"] == 1 and top["hidden"] == 9 and noisy["total_changes"] == 10
    # settles after flap_quiet_min (15) without a change, in status down (!= last shown up):
    # the last hidden change is shown so the log ends in the real status
    await rt.event_noise.sweep(t + timedelta(minutes=14))
    assert rt.event_noise.is_flapping(sid)
    await rt.event_noise.sweep(t + timedelta(minutes=15))
    assert not rt.event_noise.is_flapping(sid)
    assert await shown(admin, sid) == [
        ("status_change", "unknown", "up"),
        ("sensor_flapping", None, "down"),
        ("status_change", "up", "down"),
    ]
    async with rt.sessionmaker() as s:
        end = (
            await s.execute(
                text("SELECT flap_end, message FROM events WHERE kind = 'sensor_flapping'")
            )
        ).one()
    assert end.flap_end == t + timedelta(minutes=15) and "stopped flapping" in end.message


async def test_flapping_survives_a_restart_and_off_closes_it(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    sid = await ping(admin)
    t0 = clock.now()
    await run(rt, sid, UP, t0)
    for i in range(6):
        await run(rt, sid, DOWN if i % 2 == 0 else UP, t0 + timedelta(seconds=60 * (i + 1)))
    assert rt.event_noise.is_flapping(sid)
    rt.event_noise.reset()  # "restart": the in-memory tracker is empty ...
    await rt.event_noise._restore()  # noqa: SLF001 - ... and restored from the open row
    assert rt.event_noise.is_flapping(sid)
    await run(rt, sid, DOWN, t0 + timedelta(seconds=420))
    async with rt.sessionmaker() as s:
        n = (
            await s.execute(
                text("SELECT flap_count FROM events WHERE kind = 'sensor_flapping'"),
            )
        ).scalar_one()
    assert n == 7
    r = await admin.put("/api/settings", json={"sensor_event_flap_enabled": False})
    assert r.status_code == 200
    async with rt.sessionmaker() as s:
        open_ = (
            await s.execute(
                text(
                    "SELECT count(*) FROM events WHERE kind = 'sensor_flapping' "
                    "AND flap_end IS NULL"
                )
            )
        ).scalar_one()
    assert open_ == 0 and not rt.event_noise.is_flapping(sid)
    # the log ends in the sensor's real status (down)
    assert (await shown(admin, sid))[-1] == ("status_change", "up", "down")


async def test_flapping_only_without_debounce(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    await admin.put("/api/settings", json={"sensor_event_debounce_enabled": False})
    sid = await ping(admin)
    t0 = clock.now()
    await run(rt, sid, UP, t0)
    for i in range(6):
        await run(rt, sid, DOWN if i % 2 == 0 else UP, t0 + timedelta(seconds=60 * (i + 1)))
    changes = [r[3] for r in await rows(rt, sid) if r[0] == "status_change"]
    assert changes == [None, None, None, None, None, "flapping", "flapping"]


# ------------------------------------------------------------------ important ports
async def test_important_port_criteria_and_upgrade_meaning(
    rt: Runtime, admin: httpx.AsyncClient
) -> None:
    from netmon.ingest.pipeline import load_port_info
    from tests.test_13 import _lab

    a, _ = await _lab(rt, admin)
    # an interface sensor (even a critical one) no longer makes a port important
    await make_sensor(admin, a, "snmp_interface", {"if_index": 8}, impact="critical")
    await make_sensor(admin, a, "snmp_interface", {"if_index": 1}, impact="critical")
    info = await load_port_info(rt, a)
    assert info.important == {3, 4, 5, 6, 7, 11}
    # the pre-upgrade meaning is a selectable criterion
    await admin.put(
        "/api/settings",
        json={"important_port_criteria": ["uplink", "lag", "trunk", "irf", "critical_sensor"]},
    )
    assert (await load_port_info(rt, a)).important == {1, 3, 4, 5, 6, 7, 8, 11}
    # only uplinks
    await admin.put("/api/settings", json={"important_port_criteria": ["uplink"]})
    assert (await load_port_info(rt, a)).important == {6, 7}
    # description pattern (ifAlias of port 1 is "=port 1")
    await admin.put(
        "/api/settings",
        json={"important_port_criteria": [], "important_port_description_pattern": "^=PORT"},
    )
    assert (await load_port_info(rt, a)).important == {1}
    ports = (await admin.get(f"/api/devices/{a}/ports")).json()["ports"]
    p1 = next(p for p in ports if p["if_index"] == 1)
    assert p1["important"] is True and p1["important_reasons"] == ["description"]
    assert next(p for p in ports if p["if_index"] == 7)["important"] is False


async def test_port_importance_override(rt: Runtime, admin: httpx.AsyncClient, viewer) -> None:  # type: ignore[no-untyped-def]
    from netmon.ingest.pipeline import load_port_info
    from tests.test_13 import _lab

    a, _ = await _lab(rt, admin)
    url = f"/api/devices/{a}/ports/8/importance"
    assert (await viewer.put(url, json={"important": True})).status_code == 403
    assert (await admin.put(url, json={"important": [1]})).status_code == 422
    assert (await admin.put(url, json={})).status_code == 422
    assert (
        await admin.put(f"/api/devices/{a}/ports/999/importance", json={"important": True})
    ).status_code == 404
    r = await admin.put(url, json={"important": True})
    assert r.status_code == 200, r.text
    assert r.json()["important"] is True and r.json()["important_reasons"] == ["marked"]
    assert r.json()["importance_override"] is True
    r = await admin.put(f"/api/devices/{a}/ports/7/importance", json={"important": False})
    assert r.json()["important"] is False and r.json()["importance_override"] is False
    rt.ingest.ports.clear()
    assert (await load_port_info(rt, a)).important == {3, 4, 5, 6, 8, 11}
    # the apply-template default selection follows it too
    from netmon.templates.selection import load_selection

    sel = await load_selection(rt, a, "important")
    assert sel.is_important({"if_index": 7})[0] is False
    assert sel.is_important({"if_index": 8}) == (True, ["marked"])
    r = await admin.put(url, json={"important": None})
    assert r.json()["importance_override"] is None and r.json()["important"] is False
    async with rt.sessionmaker() as s:
        n = (await s.execute(text("SELECT count(*) FROM port_importance_overrides"))).scalar()
    assert n == 1


async def test_apply_interfaces_carry_filter_data(rt: Runtime, admin: httpx.AsyncClient) -> None:
    from tests.test_13 import _lab

    a, _ = await _lab(rt, admin)
    r = await admin.get(
        f"/api/devices/{a}/apply-template/interfaces", params={"template_id": "hpe_comware"}
    )
    assert r.status_code == 200, r.text
    by = {i["if_index"]: i for i in r.json()["items"]}
    assert by[1]["mode"] == "access" and by[1]["untagged_vlans"] == [10] and by[1]["pvid"] == 10
    assert by[3]["mode"] == "trunk" and by[3]["tagged_vlans"] == [10, 20, 30]
    assert by[7]["has_neighbor"] is True and by[1]["has_neighbor"] is False
    assert by[1]["important"] is False and by[7]["important"] is True
    # the dialog's preview asks without starting a port inventory run
    r2 = await admin.get(
        f"/api/devices/{a}/apply-template/interfaces",
        params={"template_id": "hpe_comware", "refresh": "false"},
    )
    assert r2.status_code == 200 and r2.json()["items"] == r.json()["items"]


# ------------------------------------------------------------------ migration 0019
@pytest.mark.parametrize("mode", ["timescale", "fallback"])
@pytest.mark.parametrize("existing", [True, False])
async def test_migration_0019_up_down_up(mode: str, existing: bool, rt: Runtime) -> None:
    assert TEST_DATABASE_URL
    if mode == "timescale" and rt.settings.netmon_disable_timescale:
        pytest.skip("suite runs in forced fallback mode")
    name = f"netmon_test_m19_{secrets.token_hex(3)}"
    admin = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = make_engine(db_url_for(name).replace("postgresql://", "postgresql+asyncpg://"))
    url = db_url_for(name)

    async def alembic(op: str, rev: str) -> None:
        from tests.test_migrations import _alembic

        async with engine.connect() as c:
            await c.run_sync(lambda sc: _alembic(sc, op, rev))
            await c.commit()

    async def cols(conn: asyncpg.Connection) -> set[str]:
        rs = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'events'"
        )
        return {r["column_name"] for r in rs}

    try:
        await alembic("upgrade", "0018")
        enabled = await timescale.bootstrap(engine, 90, disabled=(mode == "fallback"))
        if mode == "timescale" and not enabled:
            pytest.skip("TimescaleDB not available")
        conn = await asyncpg.connect(_plain(url))
        try:
            if existing:  # an install that has run before
                await conn.execute(
                    "INSERT INTO settings (key, value) VALUES ('retention_days', '90')"
                )
            await conn.execute("INSERT INTO events (kind, message) VALUES ('system', 'old')")
        finally:
            await conn.close()
        await run_migrations(engine)
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == HEAD
            assert {"noise", "noise_until", "flap_count", "flap_last", "flap_end"} <= await cols(
                conn
            )
            crit = await conn.fetchval(
                "SELECT value FROM settings WHERE key = 'important_port_criteria'"
            )
            if existing:
                assert crit == '["uplink", "lag", "trunk", "irf", "critical_sensor"]'
            else:
                assert crit is None  # a fresh install uses the code default
            await conn.execute(
                "INSERT INTO events (kind, message, flap_count) VALUES ('sensor_flapping', 'f', 3)"
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("INSERT INTO events (kind, noise) VALUES ('system', 'nope')")
            gid = await conn.fetchval("INSERT INTO groups (name) VALUES ('g') RETURNING id")
            did = await conn.fetchval(
                "INSERT INTO devices (group_id, name, host) VALUES ($1, 'd', 'h') RETURNING id", gid
            )
            await conn.execute(
                "INSERT INTO port_importance_overrides (device_id, if_index, important) "
                "VALUES ($1, 3, true)",
                did,
            )
        finally:
            await conn.close()
        await alembic("downgrade", "0018")
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0018"
            assert "noise" not in await cols(conn)
            assert await conn.fetchval("SELECT count(*) FROM events") == 1  # flapping rows gone
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM settings WHERE key = 'important_port_criteria'"
                )
                == 0
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("INSERT INTO events (kind) VALUES ('sensor_flapping')")
        finally:
            await conn.close()
        await run_migrations(engine)  # up again
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == HEAD
            assert await conn.fetchval("SELECT count(*) FROM port_importance_overrides") == 0
            assert "flap_end" in await cols(conn)
        finally:
            await conn.close()
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def test_hidden_change_still_sends_the_current_severity(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, ws: asyncio.Queue
) -> None:
    r = await admin.put("/api/settings", json={"feature_alert_severity_enabled": True})
    assert r.status_code == 200, r.text
    sid = await ping(admin)
    t0 = clock.now()
    await run(rt, sid, UP, t0)
    drained(ws)
    await run(rt, sid, DOWN, t0 + timedelta(seconds=60))
    sev = [m["data"] for m in drained(ws) if m["type"] == "sensor.severity"]
    assert sev and sev[-1]["id"] == sid and sev[-1]["severity"] is not None


async def test_pending_markers_resync(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    sid = await ping(admin)
    t0 = clock.now()
    await run(rt, sid, UP, t0)
    await run(rt, sid, DOWN, t0 + timedelta(seconds=60))
    assert sid in rt.event_noise._pending  # noqa: SLF001
    async with rt.sessionmaker() as s:  # e.g. shown by another path
        await s.execute(text("UPDATE events SET noise = NULL WHERE sensor_id = :s"), {"s": sid})
        await s.commit()
    await rt.event_noise._resync_pending()  # noqa: SLF001
    assert sid not in rt.event_noise._pending  # noqa: SLF001
