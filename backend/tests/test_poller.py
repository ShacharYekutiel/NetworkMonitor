"""Result processing, status rollup, events, history, retention, scheduler."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text

from netmon.poller.processor import persist
from netmon.poller.scheduler import Scheduler
from netmon.poller.types import CheckResult
from netmon.services import history, retention
from tests.conftest import default_group_id, make_device, make_sensor, requires_db

pytestmark = requires_db


async def _persist(rt, sid: int, raw: CheckResult, at: datetime | None = None):  # type: ignore[no-untyped-def]
    return await persist(rt, sid, raw, 5.0, at or datetime.now(UTC))


async def test_processor_status_events_and_rollup(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    q: asyncio.Queue = asyncio.Queue()
    rt.hub.listeners.append(q)
    root = await default_group_id(admin)
    dev = await make_device(admin, name="core-sw")
    s = await make_sensor(admin, dev["id"], "ping", name="Ping")
    sid = s["id"]
    await rt.publish_rollup_changes()  # baseline snapshot

    ev = await _persist(rt, sid, CheckResult(True, "", {"latency_ms": 1.8, "packet_loss_pct": 0}))
    assert ev.status == "up"
    s1 = (await admin.get(f"/api/sensors/{sid}")).json()
    assert s1["status"] == "up" and s1["last_check_at"] is not None
    assert s1["channels"][0]["last_value"] == 1.8 and s1["channels"][0]["status"] == "up"
    await rt.publish_rollup_changes()
    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    types = [m["type"] for m in msgs]
    assert "sensor.update" in types and "event.created" in types
    upd = next(m for m in msgs if m["type"] == "sensor.update")["data"]
    assert upd == {
        "id": sid,
        "device_id": dev["id"],
        "status": "up",
        "status_message": "",
        "last_check_at": s1["last_check_at"],
        "status_changed": True,
        "channels": [
            {"key": "latency_ms", "value": 1.8, "status": "up"},
            {"key": "packet_loss_pct", "value": 0, "status": "up"},
        ],
    }
    dstat = next(m for m in msgs if m["type"] == "device.status")["data"]
    assert dstat["id"] == dev["id"] and dstat["status"] == "up"
    gstat = next(m for m in msgs if m["type"] == "group.status")["data"]
    assert gstat["id"] == root and gstat["status_counts"]["up"] == 1

    # threshold breach -> warning; then a failure -> down without measurements
    await _persist(rt, sid, CheckResult(True, "", {"latency_ms": 150, "packet_loss_pct": 0}))
    ev = await _persist(rt, sid, CheckResult(False, "Host unreachable"))
    assert ev.status == "down"
    s2 = (await admin.get(f"/api/sensors/{sid}")).json()
    assert s2["status"] == "down" and s2["status_message"] == "Host unreachable"
    assert s2["channels"][0]["last_value"] is None
    events = (await admin.get("/api/events", params={"sensor_id": sid})).json()["items"]
    assert [(e["old_status"], e["new_status"]) for e in events] == [
        ("warning", "down"),
        ("up", "warning"),
        ("unknown", "up"),
    ]
    assert events[1]["message"] == "latency_ms 150 > 100"
    async with rt.sessionmaker() as session:
        n = (
            await session.execute(
                text("SELECT count(*) FROM measurements WHERE sensor_id=:s"), {"s": sid}
            )
        ).scalar()
    assert n == 4  # 2 channels x 2 successful checks

    dash = (await admin.get("/api/dashboard")).json()
    assert dash["sensor_counts"]["down"] == 1 and dash["sensor_counts"]["total"] == 1
    assert dash["device_counts"]["down"] == 1 and dash["group_count"] == 1
    assert dash["problem_sensors"][0]["id"] == sid
    assert [e["new_status"] for e in dash["recent_events"]] == ["down", "warning", "up"]
    r = await admin.get("/api/sensors", params={"status": "down,warning"})
    assert [x["id"] for x in r.json()["items"]] == [sid]
    tree = (await admin.get("/api/tree")).json()
    assert tree["groups"][0]["status"] == "down"
    assert (await admin.get(f"/api/devices/{dev['id']}")).json()["status"] == "down"

    # paused sensors ignore late results
    await admin.patch(f"/api/sensors/{sid}", json={"enabled": False})
    await _persist(rt, sid, CheckResult(True, "", {"latency_ms": 1, "packet_loss_pct": 0}))
    assert (await admin.get(f"/api/sensors/{sid}")).json()["status"] == "paused"
    assert (await admin.get(f"/api/devices/{dev['id']}")).json()["status"] == "paused"
    rt.hub.listeners.remove(q)


async def test_run_now_and_test_endpoints(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    dev = await make_device(admin, host="127.0.0.1")
    s = await make_sensor(admin, dev["id"], "tcp_port", {"port": port})
    r = await admin.post(f"/api/sensors/{s['id']}/run")
    assert r.status_code == 200, r.text
    res = r.json()
    assert set(res) == {"ok", "status", "message", "duration_ms", "checked_at", "channels"}
    assert res["ok"] is True and res["status"] == "up"
    assert res["channels"][0]["key"] == "connect_time_ms" and res["channels"][0]["value"] > 0
    assert (await admin.get(f"/api/sensors/{s['id']}")).json()["status"] == "up"
    # test persists nothing
    r = await admin.post(
        "/api/sensors/test",
        json={"host": "127.0.0.1", "type": "tcp_port", "config": {"port": port}},
    )
    assert r.json()["ok"] is True
    server.close()
    await server.wait_closed()
    r = await admin.post(
        "/api/sensors/test",
        json={"device_id": dev["id"], "type": "tcp_port", "config": {"port": port}},
    )
    assert r.json()["ok"] is False and r.json()["status"] == "down"
    assert "refused" in r.json()["message"]
    async with rt.sessionmaker() as session:
        n = (await session.execute(text("SELECT count(*) FROM measurements"))).scalar()
    assert n == 1
    r = await admin.post("/api/sensors/test", json={"type": "ping", "config": {}})
    assert r.status_code == 422


async def test_history_buckets(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "ping")
    await _check_history(rt, admin, s["id"], rt.timescale_enabled)


async def _check_history(rt, admin, sid: int, timescale: bool) -> None:  # type: ignore[no-untyped-def]
    base = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    rows = []
    for i in range(30):  # one value per minute for 30 minutes
        rows.append({"t": base + timedelta(minutes=i), "s": sid, "c": "latency_ms", "v": float(i)})
    rows.append({"t": base + timedelta(minutes=3), "s": sid, "c": "packet_loss_pct", "v": 25.0})
    async with rt.sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO measurements (time, sensor_id, channel, value) VALUES (:t, :s, :c, :v)"
            ),
            rows,
        )
        await session.commit()
        res = await history.query_history(
            session,
            timescale=timescale,
            sensor_id=sid,
            channels=[("latency_ms", "ms"), ("packet_loss_pct", "%")],
            start=base,
            end=base + timedelta(hours=24),
        )
    assert res["bucket_s"] == 300
    lat = res["channels"][0]
    assert lat["key"] == "latency_ms" and lat["unit"] == "ms"
    assert [p["t"] for p in lat["points"]] == [
        f"2026-11-03T12:{m:02d}:00Z" for m in (0, 5, 10, 15, 20, 25)
    ]
    assert lat["points"][0] == {"t": "2026-11-03T12:00:00Z", "avg": 2.0, "min": 0.0, "max": 4.0}
    assert res["channels"][1]["points"] == [
        {"t": "2026-11-03T12:00:00Z", "avg": 25.0, "min": 25.0, "max": 25.0}
    ]
    if admin is not None:
        r = await admin.get(
            f"/api/sensors/{sid}/history",
            params={
                "from": "2026-11-03T12:00:00Z",
                "to": "2026-11-03T13:00:00Z",
                "channels": "latency_ms",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["bucket_s"] == 60 and body["from"] == "2026-11-03T12:00:00Z"
        assert [c["key"] for c in body["channels"]] == ["latency_ms"]
        assert len(body["channels"][0]["points"]) == 30
        r = await admin.get(f"/api/sensors/{sid}/history", params={"range": "7d"})
        assert r.json()["bucket_s"] == 1800
        r = await admin.get(f"/api/sensors/{sid}/history", params={"range": "2y"})
        assert r.status_code == 422
        r = await admin.get(f"/api/sensors/{sid}/history", params={"channels": "nope"})
        assert r.status_code == 422


async def test_event_retention(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    async with rt.sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO events (time, kind, message) VALUES "
                "(now() - interval '400 days', 'system', 'old'), (now(), 'system', 'new')"
            )
        )
        await session.commit()
    res = await retention.run_once(rt)
    assert res["events"] == 1
    msgs = [e["message"] for e in (await admin.get("/api/events")).json()["items"]]
    assert "old" not in msgs and "new" in msgs


async def test_scheduler_runs_sensor(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    dev = await make_device(admin, host="127.0.0.1")
    s = await make_sensor(admin, dev["id"], "tcp_port", {"port": port}, interval_s=10)
    sched = Scheduler(rt)
    rt.scheduler = sched
    try:
        await sched.start()
        assert sched.scheduled_count == 1
        for _ in range(100):
            if (await admin.get(f"/api/sensors/{s['id']}")).json()["last_check_at"]:
                break
            await asyncio.sleep(0.2)
        assert (await admin.get(f"/api/sensors/{s['id']}")).json()["status"] == "up"
        health = (await admin.get("/api/health")).json()
        assert health["poller"] == "running" and health["sensors_scheduled"] == 1
        # pause via API removes it from the schedule, resume re-adds
        await admin.patch(f"/api/sensors/{s['id']}", json={"enabled": False})
        assert sched.scheduled_count == 0
        await admin.patch(f"/api/sensors/{s['id']}", json={"enabled": True})
        assert sched.scheduled_count == 1
        await admin.patch(f"/api/devices/{dev['id']}", json={"enabled": False})
        assert sched.scheduled_count == 0
        await admin.patch(f"/api/devices/{dev['id']}", json={"enabled": True})
        await admin.delete(f"/api/sensors/{s['id']}")
        assert sched.scheduled_count == 0
    finally:
        await sched.stop()
        rt.scheduler = None
        server.close()
