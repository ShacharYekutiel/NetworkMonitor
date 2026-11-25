"""[if-noise] Per-sensor / per-device / per-group overrides of the sensor event noise protections
(debounce, flapping collapse): resolution order, every state at every level, immediate release,
restart persistence, API / audit / bulk, and migration 0020."""

from __future__ import annotations

import asyncio
import json
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
from netmon.runtime import Runtime
from netmon.services.noise_override import Level, resolve
from tests.conftest import TEST_DATABASE_URL, _plain, db_url_for, make_device, make_sensor
from tests.conftest import requires_db as _requires_db
from tests.test_if_noise import DOWN, UP, drained, rows, run, shown

pytestmark = _requires_db


@pytest.fixture
async def clock(rt: Runtime, admin: httpx.AsyncClient) -> AsyncIterator[FakeClock]:
    old = rt.clock
    fake = FakeClock(datetime.now(UTC).replace(microsecond=0))
    rt.clock = fake
    if rt.maintenance is not None:
        await rt.maintenance.evaluate()
    yield fake
    rt.clock = old


@pytest.fixture
def ws(rt: Runtime) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    rt.hub.listeners.append(q)
    return q


GLOB = {"debounce": True, "flap": True, "debounce_polls": 1, "debounce_min_s": 0}
INHERIT = {"debounce": "inherit", "flap": "inherit", "debounce_polls": None, "debounce_min_s": None}


def lv(kind: str, id_: int, **ov: Any) -> Level:
    return Level(kind, id_, f"{kind}{id_}", {**INHERIT, **ov})


# ------------------------------------------------------------------ pure resolution
def test_resolution_order_and_sources() -> None:
    chain = [lv("sensor", 1), lv("device", 2), lv("group", 3), lv("group", 4)]
    e = resolve(chain, GLOB)
    assert (e.debounce.value, e.debounce.source) == (True, "global")
    assert e.debounce.source_id is None and e.debounce_polls.value == 1
    # a top-level group decides when nothing below does
    chain[3] = lv("group", 4, debounce="off", debounce_polls=3)
    e = resolve(chain, GLOB)
    assert (e.debounce.value, e.debounce.source, e.debounce.source_id) == (False, "group", 4)
    assert (e.debounce_polls.value, e.debounce_polls.source_id) == (3, 4)
    assert e.debounce.source_name == "group4"
    # the nearer group wins over its parent
    chain[2] = lv("group", 3, debounce="on")
    assert resolve(chain, GLOB).debounce.source_id == 3
    # the device wins over the groups, the sensor over the device
    chain[1] = lv("device", 2, debounce="off", flap="off")
    e = resolve(chain, GLOB)
    assert (e.debounce.value, e.debounce.source) == (False, "device")
    assert (e.flap.value, e.flap.source) == (False, "device")
    chain[0] = lv("sensor", 1, flap="on", debounce_min_s=0)
    e = resolve(chain, GLOB)
    assert (e.flap.value, e.flap.source) == (True, "sensor")
    assert (e.debounce.value, e.debounce.source) == (False, "device")  # independent keys
    assert (e.debounce_min_s.value, e.debounce_min_s.source) == (0, "sensor")  # 0 is a value
    assert e.debounce_polls.source == "group"


@pytest.mark.parametrize("level", ["sensor", "device", "group"])
@pytest.mark.parametrize("glob_on", [True, False])
@pytest.mark.parametrize("state", ["inherit", "on", "off"])
def test_each_state_at_each_level(level: str, glob_on: bool, state: str) -> None:
    glob = {**GLOB, "debounce": glob_on, "flap": glob_on}
    chain = [lv("sensor", 1), lv("device", 2), lv("group", 3)]
    idx = {"sensor": 0, "device": 1, "group": 2}[level]
    chain[idx] = lv(level, idx + 1, debounce=state, flap=state)
    e = resolve(chain, glob)
    want = glob_on if state == "inherit" else state == "on"
    assert e.debounce.value is want and e.flap.value is want
    assert e.debounce.source == ("global" if state == "inherit" else level)


# ------------------------------------------------------------------ helpers
async def ping_in(admin: httpx.AsyncClient, group_id: int | None = None, **dev: Any) -> dict:
    d = await make_device(
        admin, name=dev.pop("name", "sw"), host="10.9.0.2", group_id=group_id, **dev
    )
    s = await make_sensor(admin, d["id"], "ping", name="port", interval_s=60)
    return {"device": d, "sensor": s, "sid": int(s["id"]), "did": int(d["id"])}


async def patch(c: httpx.AsyncClient, url: str, **noise: Any) -> dict:
    r = await c.patch(url, json={"event_noise": noise})
    assert r.status_code == 200, r.text
    return r.json()


async def blip(rt: Runtime, sid: int, t0: datetime) -> None:
    await run(rt, sid, UP, t0)
    await run(rt, sid, DOWN, t0 + timedelta(seconds=60))
    await run(rt, sid, UP, t0 + timedelta(seconds=120))


async def flap(rt: Runtime, sid: int, t0: datetime, n: int = 7) -> None:
    await run(rt, sid, UP, t0)
    for i in range(n):
        await run(rt, sid, DOWN if i % 2 == 0 else UP, t0 + timedelta(seconds=60 * (i + 1)))


# ------------------------------------------------------------------ API
async def test_api_defaults_validation_and_audit(
    rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient
) -> None:
    r = await admin.put("/api/settings", json={"feature_audit_enabled": True})
    assert r.status_code == 200, r.text
    x = await ping_in(admin)
    assert x["sensor"]["event_noise"] == INHERIT
    eff = x["sensor"]["effective_event_noise"]
    assert eff["debounce"] == {
        "value": True,
        "source": "global",
        "source_id": None,
        "source_name": None,
    }
    assert eff["debounce_polls"]["value"] == 1 and eff["debounce_min_s"]["value"] == 0
    assert x["device"]["event_noise"] == INHERIT
    assert x["device"]["effective_event_noise"]["flap"]["source"] == "global"
    gid = x["device"]["group_id"]
    g = (await viewer.get(f"/api/groups/{gid}")).json()
    assert g["event_noise"] == INHERIT and g["effective_event_noise"]["flap"]["value"] is True
    sid, did = x["sid"], x["did"]
    for bad in (
        {"debounce": "maybe"},
        {"debounce": None},
        {"flap": None},
        {"debounce_polls": 0},
        {"debounce_polls": 11},
        {"debounce_min_s": -1},
        {"debounce_min_s": 3601},
        {"bogus": "on"},
    ):
        for url in (f"/api/sensors/{sid}", f"/api/devices/{did}", f"/api/groups/{gid}"):
            r = await admin.patch(url, json={"event_noise": bad})
            assert r.status_code == 422, (url, bad, r.text)
    assert (
        await viewer.patch(f"/api/sensors/{sid}", json={"event_noise": {"flap": "off"}})
    ).status_code == 403
    # partial: an omitted key keeps its value; null values go back to inherit
    s = await patch(admin, f"/api/sensors/{sid}", debounce="off", debounce_polls=3)
    assert s["event_noise"] == {**INHERIT, "debounce": "off", "debounce_polls": 3}
    s = await patch(admin, f"/api/sensors/{sid}", flap="on")
    assert s["event_noise"] == {
        "debounce": "off",
        "flap": "on",
        "debounce_polls": 3,
        "debounce_min_s": None,
    }
    assert s["effective_event_noise"]["debounce"]["source"] == "sensor"
    assert s["effective_event_noise"]["debounce"]["source_name"] == "port"
    s = await patch(admin, f"/api/sensors/{sid}", debounce_polls=None)
    assert s["event_noise"]["debounce_polls"] is None
    # list and viewer read
    items = (await viewer.get("/api/sensors", params={"device_id": did})).json()["items"]
    assert items[0]["event_noise"]["flap"] == "on"
    assert items[0]["effective_event_noise"]["flap"]["source"] == "sensor"
    async with rt.sessionmaker() as ses:
        changes = [
            json.loads(r[0]) if isinstance(r[0], str) else r[0]
            for r in await ses.execute(
                text(
                    "SELECT changes FROM audit_log WHERE action = 'sensor.update' "
                    "AND entity_id = :s ORDER BY id"
                ),
                {"s": sid},
            )
        ]
    assert changes[0] == {"noise_debounce": ["inherit", "off"], "noise_debounce_polls": [None, 3]}
    assert changes[-1] == {"noise_debounce_polls": [3, None]}
    # create with overrides (sensor, device, group)
    g2 = (
        await admin.post("/api/groups", json={"name": "quiet", "event_noise": {"flap": "off"}})
    ).json()
    assert g2["event_noise"]["flap"] == "off"
    d2 = await make_device(
        admin, name="d2", group_id=g2["id"], event_noise={"debounce": "on", "debounce_min_s": 120}
    )
    assert d2["event_noise"]["debounce_min_s"] == 120
    assert d2["effective_event_noise"]["flap"] == {
        "value": False,
        "source": "group",
        "source_id": g2["id"],
        "source_name": "quiet",
    }
    s2 = await make_sensor(admin, d2["id"], "ping", name="p2", event_noise={"flap": "on"})
    eff2 = s2["effective_event_noise"]
    assert (eff2["flap"]["value"], eff2["flap"]["source"]) == (True, "sensor")
    assert (eff2["debounce_min_s"]["value"], eff2["debounce_min_s"]["source"]) == (120, "device")


async def test_bulk_event_noise(rt: Runtime, admin: httpx.AsyncClient) -> None:
    d = await make_device(admin, name="sw", host="10.9.0.3")
    a = await make_sensor(admin, d["id"], "ping", name="a")
    b = await make_sensor(admin, d["id"], "ping", name="b", event_noise={"flap": "off"})
    trap = await make_sensor(admin, d["id"], "trap")
    r = await admin.post(
        "/api/sensors/bulk-event-noise",
        json={"sensor_ids": [a["id"], b["id"], trap["id"]], "event_noise": {"flap": "off"}},
    )
    assert r.status_code == 200 and r.json() == {"updated": 1}  # b unchanged, trap skipped
    got = {
        s["id"]: s
        for s in (await admin.get("/api/sensors", params={"device_id": d["id"]})).json()["items"]
    }
    assert got[a["id"]]["event_noise"]["flap"] == "off"
    assert got[trap["id"]]["event_noise"]["flap"] == "inherit"
    r = await admin.post(
        "/api/sensors/bulk-event-noise",
        json={
            "sensor_ids": [a["id"], b["id"]],
            "event_noise": {"flap": "inherit", "debounce_polls": 2},
        },
    )
    assert r.json() == {"updated": 2}
    for bad in (
        {"sensor_ids": [a["id"]], "event_noise": {}},
        {"sensor_ids": [a["id"], 99999], "event_noise": {"flap": "on"}},
        {"sensor_ids": [], "event_noise": {"flap": "on"}},
        {"sensor_ids": [a["id"]], "event_noise": {"flap": None}},
    ):
        assert (await admin.post("/api/sensors/bulk-event-noise", json=bad)).status_code == 422


# ------------------------------------------------------------------ behaviour
async def test_global_on_sensor_off(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, ws: asyncio.Queue
) -> None:
    """Its changes are never pending / debounced / collapsed, though the switches are ON."""
    x = await ping_in(admin)
    await patch(admin, f"/api/sensors/{x['sid']}", debounce="off", flap="off")
    t0 = clock.now()
    drained(ws)
    await blip(rt, x["sid"], t0)
    assert [r[3] for r in await rows(rt, x["sid"])] == [None, None, None]
    created = [m for m in drained(ws) if m["type"] == "event.created"]
    assert len(created) == 3  # at once, as before the protections
    await flap(rt, x["sid"], t0 + timedelta(hours=1), n=8)
    got = await rows(rt, x["sid"])
    assert all(r[3] is None and r[0] == "status_change" for r in got)
    assert not rt.event_noise.is_flapping(x["sid"])
    # a neighbour without an override is still protected
    y = await ping_in(admin, name="other")
    await blip(rt, y["sid"], t0)
    assert [r[3] for r in await rows(rt, y["sid"])] == [None, "debounced", "debounced"]


async def test_global_off_sensor_on(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    r = await admin.put(
        "/api/settings",
        json={"sensor_event_debounce_enabled": False, "sensor_event_flap_enabled": False},
    )
    assert r.status_code == 200
    x = await ping_in(admin)
    y = await ping_in(admin, name="other")
    await patch(admin, f"/api/sensors/{x['sid']}", debounce="on", flap="on")
    t0 = clock.now()
    await blip(rt, x["sid"], t0)
    await blip(rt, y["sid"], t0)
    assert [r[3] for r in await rows(rt, x["sid"])] == [None, "debounced", "debounced"]
    assert [r[3] for r in await rows(rt, y["sid"])] == [None, None, None]
    await flap(rt, x["sid"], t0 + timedelta(hours=1), n=8)
    assert rt.event_noise.is_flapping(x["sid"])
    assert "sensor_flapping" in [k for k, _o, _n in await shown(admin, x["sid"])]
    # turning a global key ON and OFF again does not release the sensor that says `on`
    await admin.put("/api/settings", json={"sensor_event_flap_enabled": True})
    await admin.put("/api/settings", json={"sensor_event_flap_enabled": False})
    assert rt.event_noise.is_flapping(x["sid"])


async def test_device_and_group_levels(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    parent = (await admin.post("/api/groups", json={"name": "site"})).json()
    child = (
        await admin.post("/api/groups", json={"name": "floor", "parent_id": parent["id"]})
    ).json()
    other = (await admin.post("/api/groups", json={"name": "core"})).json()
    x = await ping_in(admin, group_id=child["id"])
    sid, did = x["sid"], x["did"]
    t0 = clock.now()

    async def eff() -> dict:
        return (await admin.get(f"/api/sensors/{sid}")).json()["effective_event_noise"]

    # parent group OFF -> inherited through the child group and the device
    await patch(admin, f"/api/groups/{parent['id']}", debounce="off")
    e = await eff()
    assert (e["debounce"]["value"], e["debounce"]["source"], e["debounce"]["source_id"]) == (
        False,
        "group",
        parent["id"],
    )
    dev = (await admin.get(f"/api/devices/{did}")).json()
    assert dev["effective_event_noise"]["debounce"]["source_name"] == "site"
    await blip(rt, sid, t0)
    assert [r[3] for r in await rows(rt, sid)][-2:] == [None, None]
    # the child group says ON
    await patch(admin, f"/api/groups/{child['id']}", debounce="on")
    assert (await eff())["debounce"]["source_id"] == child["id"]
    # the device says OFF again, the sensor ON: the sensor wins
    await patch(admin, f"/api/devices/{did}", debounce="off")
    assert (await eff())["debounce"]["source"] == "device"
    await patch(admin, f"/api/sensors/{sid}", debounce="on")
    e = await eff()
    assert (e["debounce"]["value"], e["debounce"]["source"]) == (True, "sensor")
    await blip(rt, sid, t0 + timedelta(hours=1))
    assert [r[3] for r in await rows(rt, sid)][-2:] == ["debounced", "debounced"]
    # back to inherit everywhere below the parent: OFF from the parent again
    await patch(admin, f"/api/sensors/{sid}", debounce="inherit")
    await patch(admin, f"/api/devices/{did}", debounce="inherit")
    await patch(admin, f"/api/groups/{child['id']}", debounce="inherit")
    assert (await eff())["debounce"]["source_id"] == parent["id"]
    # moving the device to another group: it inherits from there (global here)
    r = await admin.patch(f"/api/devices/{did}", json={"group_id": other["id"]})
    assert r.status_code == 200
    assert (await eff())["debounce"]["source"] == "global"
    # the tree move does the same
    r = await admin.post(
        "/api/tree/move",
        json={"device_ids": [did], "group_ids": [], "target_group_id": child["id"]},
    )
    assert r.status_code == 200, r.text
    assert (await eff())["debounce"]["source_id"] == parent["id"]


async def test_debounce_values_per_level(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    await admin.put("/api/settings", json={"sensor_event_flap_enabled": False})
    x = await ping_in(admin)
    sid = x["sid"]
    t0 = clock.now()
    await run(rt, sid, UP, t0)
    # device: 2 polls -> a 120 s blip is debounced (2.5 x 60 s), with the global 1 it is not
    await patch(admin, f"/api/devices/{x['did']}", debounce_polls=2)
    await run(rt, sid, DOWN, t0 + timedelta(seconds=60))
    await run(rt, sid, UP, t0 + timedelta(seconds=180))
    assert [r[3] for r in await rows(rt, sid)][-2:] == ["debounced", "debounced"]
    # two confirming checks needed now
    await run(rt, sid, DOWN, t0 + timedelta(seconds=600))
    await run(rt, sid, DOWN, t0 + timedelta(seconds=660))
    assert (await rows(rt, sid))[-1][3] == "pending"
    await run(rt, sid, DOWN, t0 + timedelta(seconds=720))
    assert (await rows(rt, sid))[-1][3] is None
    # the sensor's own min seconds win over the global 0
    await patch(admin, f"/api/sensors/{sid}", debounce_polls=1, debounce_min_s=600)
    await run(rt, sid, UP, t0 + timedelta(seconds=1000))
    await run(rt, sid, DOWN, t0 + timedelta(seconds=1500))  # 500 s <= 600 s
    assert [r[3] for r in await rows(rt, sid)][-2:] == ["debounced", "debounced"]


async def test_override_change_releases_at_once(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, ws: asyncio.Queue
) -> None:
    x = await ping_in(admin)
    y = await ping_in(admin, name="other")
    sid = x["sid"]
    t0 = clock.now()
    # a pending change is shown as soon as the sensor's debounce goes OFF
    await run(rt, sid, UP, t0)
    await run(rt, y["sid"], UP, t0)
    await run(rt, sid, DOWN, t0 + timedelta(seconds=60))
    await run(rt, y["sid"], DOWN, t0 + timedelta(seconds=60))
    assert (await rows(rt, sid))[-1][3] == "pending"
    drained(ws)
    await patch(admin, f"/api/sensors/{sid}", debounce="off")
    assert (await rows(rt, sid))[-1][3] is None
    created = [m["data"] for m in drained(ws) if m["type"] == "event.created"]
    assert [(e["sensor_id"], e["new_status"]) for e in created] == [(sid, "down")]
    assert sid not in rt.event_noise._pending  # noqa: SLF001
    assert (await rows(rt, y["sid"]))[-1][3] == "pending"  # the other sensor is untouched
    # an open flapping episode is settled as soon as the flapping collapse goes OFF (device)
    await flap(rt, y["sid"], t0 + timedelta(hours=1))
    assert rt.event_noise.is_flapping(y["sid"])
    await patch(admin, f"/api/devices/{y['did']}", flap="off")
    assert not rt.event_noise.is_flapping(y["sid"])
    async with rt.sessionmaker() as s:
        end = (
            await s.execute(
                text(
                    "SELECT flap_end FROM events WHERE kind = 'sensor_flapping' AND sensor_id = :s"
                ),
                {"s": y["sid"]},
            )
        ).scalar_one()
    assert end is not None
    assert (await shown(admin, y["sid"]))[-1][2] == "down"  # the log ends in the real status
    # a group override releases the sensors below it
    z = await ping_in(admin, name="third")
    await run(rt, z["sid"], UP, t0 + timedelta(hours=2))
    await run(rt, z["sid"], DOWN, t0 + timedelta(hours=2, seconds=60))
    assert (await rows(rt, z["sid"]))[-1][3] == "pending"
    await patch(admin, f"/api/groups/{z['device']['group_id']}", debounce="off")
    assert (await rows(rt, z["sid"]))[-1][3] is None
    # bulk does the same
    await patch(admin, f"/api/groups/{z['device']['group_id']}", debounce="inherit")
    await run(rt, z["sid"], UP, t0 + timedelta(hours=3))
    assert (await rows(rt, z["sid"]))[-1][3] == "pending"
    r = await admin.post(
        "/api/sensors/bulk-event-noise",
        json={"sensor_ids": [z["sid"]], "event_noise": {"debounce": "off"}},
    )
    assert r.json() == {"updated": 1}
    assert (await rows(rt, z["sid"]))[-1][3] is None


async def test_turning_on_holds_nothing_back_retroactively(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    x = await ping_in(admin)
    await patch(admin, f"/api/sensors/{x['sid']}", debounce="off")
    t0 = clock.now()
    await run(rt, x["sid"], UP, t0)
    await run(rt, x["sid"], DOWN, t0 + timedelta(seconds=60))
    await patch(admin, f"/api/sensors/{x['sid']}", debounce="on")
    assert (await rows(rt, x["sid"]))[-1][3] is None  # stays shown
    await run(rt, x["sid"], UP, t0 + timedelta(seconds=120))
    assert (await rows(rt, x["sid"]))[-1][3] == "pending"  # the next change is protected


async def test_overrides_survive_a_restart(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    x = await ping_in(admin)
    y = await ping_in(admin, name="other")
    await admin.put("/api/settings", json={"sensor_event_debounce_enabled": False})
    await patch(admin, f"/api/sensors/{x['sid']}", debounce="on")
    t0 = clock.now()
    await run(rt, x["sid"], UP, t0)
    await run(rt, x["sid"], DOWN, t0 + timedelta(seconds=60))
    assert (await rows(rt, x["sid"]))[-1][3] == "pending"
    # restart: the in-memory state is lost and rebuilt from the database
    await rt.event_noise.stop()
    rt.event_noise.reset()
    await rt.event_noise.start()
    assert rt.event_noise.debounce is False  # the global key
    assert (await rows(rt, x["sid"]))[-1][3] == "pending"  # still held: the override says on
    assert x["sid"] in rt.event_noise._pending  # noqa: SLF001
    await run(rt, x["sid"], UP, t0 + timedelta(seconds=120))
    assert [r[3] for r in await rows(rt, x["sid"])][-2:] == ["debounced", "debounced"]
    await blip(rt, y["sid"], t0)
    assert [r[3] for r in await rows(rt, y["sid"])] == [None, None, None]
    # an override written while the server was down is applied at start (pending -> shown)
    await run(rt, x["sid"], DOWN, t0 + timedelta(seconds=600))
    assert (await rows(rt, x["sid"]))[-1][3] == "pending"
    await rt.event_noise.stop()
    async with rt.sessionmaker() as s:
        await s.execute(
            text("UPDATE sensors SET noise_debounce = 'off' WHERE id = :s"), {"s": x["sid"]}
        )
        await s.commit()
    rt.event_noise.reset()
    await rt.event_noise.start()
    assert (await rows(rt, x["sid"]))[-1][3] is None


async def test_noisy_sensors_carry_the_override(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    x = await ping_in(admin)
    await patch(admin, f"/api/devices/{x['did']}", flap="off")
    await blip(rt, x["sid"], clock.now())
    item = (await admin.get("/api/events/noisy-sensors")).json()["items"][0]
    assert item["sensor_id"] == x["sid"] and item["event_noise"] == INHERIT
    assert item["effective_event_noise"]["flap"]["source"] == "device"
    await patch(admin, f"/api/sensors/{x['sid']}", flap="on")
    item = (await admin.get("/api/events/noisy-sensors")).json()["items"][0]
    assert item["event_noise"]["flap"] == "on"


async def test_sensor_deleted_with_something_held_back(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    x = await ping_in(admin)
    await flap(rt, x["sid"], clock.now())
    assert rt.event_noise.is_flapping(x["sid"])
    assert (await admin.delete(f"/api/sensors/{x['sid']}")).status_code == 204
    await rt.event_noise.release()
    assert not rt.event_noise.is_flapping(x["sid"])


# ------------------------------------------------------------------ migration 0020
@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_0020_up_down_up(mode: str, rt: Runtime) -> None:
    assert TEST_DATABASE_URL
    if mode == "timescale" and rt.settings.netmon_disable_timescale:
        pytest.skip("suite runs in forced fallback mode")
    name = f"netmon_test_m20_{secrets.token_hex(3)}"
    admin = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = make_engine(db_url_for(name).replace("postgresql://", "postgresql+asyncpg://"))
    url = db_url_for(name)

    async def alembic(op: str, rev: str) -> None:
        from tests.test_migrations import _alembic

        async with engine.connect() as c:
            await c.run_sync(lambda sc: _alembic(sc, op, rev))
            await c.commit()

    async def cols(conn: asyncpg.Connection, table: str) -> set[str]:
        rs = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = $1", table
        )
        return {r["column_name"] for r in rs}

    noise_cols = {"noise_debounce", "noise_flap", "noise_debounce_polls", "noise_debounce_min_s"}
    try:
        await alembic("upgrade", "0019")
        enabled = await timescale.bootstrap(engine, 90, disabled=(mode == "fallback"))
        if mode == "timescale" and not enabled:
            pytest.skip("TimescaleDB not available")
        conn = await asyncpg.connect(_plain(url))
        try:  # existing rows get `inherit`
            gid = await conn.fetchval("INSERT INTO groups (name) VALUES ('g') RETURNING id")
            did = await conn.fetchval(
                "INSERT INTO devices (group_id, name, host) VALUES ($1, 'd', 'h') RETURNING id", gid
            )
            await conn.execute(
                "INSERT INTO sensors (device_id, name, type, config) "
                "VALUES ($1, 's', 'ping', '{}')",
                did,
            )
        finally:
            await conn.close()
        await run_migrations(engine)
        conn = await asyncpg.connect(_plain(url))
        try:
            assert (
                await conn.fetchval("SELECT version_num FROM alembic_version") == migration_head()
            )
            for table in ("groups", "devices", "sensors"):
                assert noise_cols <= await cols(conn, table)
                row = await conn.fetchrow(
                    f"SELECT noise_debounce, noise_flap, "  # noqa: S608
                    f"noise_debounce_polls FROM {table}"
                )
                assert tuple(row) == ("inherit", "inherit", None)
                for bad in (
                    "noise_debounce = 'maybe'",
                    "noise_flap = 'x'",
                    "noise_debounce_polls = 0",
                    "noise_debounce_min_s = 3601",
                ):
                    with pytest.raises(asyncpg.CheckViolationError):
                        await conn.execute(f"UPDATE {table} SET {bad}")  # noqa: S608
                await conn.execute(
                    f"UPDATE {table} SET noise_flap = 'off', "  # noqa: S608
                    "noise_debounce_min_s = 60"
                )
        finally:
            await conn.close()
        await alembic("downgrade", "0019")
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0019"
            for table in ("groups", "devices", "sensors"):
                assert not noise_cols & await cols(conn, table)
            assert await conn.fetchval("SELECT count(*) FROM sensors") == 1
        finally:
            await conn.close()
        await run_migrations(engine)  # up again
        conn = await asyncpg.connect(_plain(url))
        try:
            assert (
                await conn.fetchval("SELECT version_num FROM alembic_version") == migration_head()
            )
            assert await conn.fetchval("SELECT noise_flap FROM sensors") == "inherit"
        finally:
            await conn.close()
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
