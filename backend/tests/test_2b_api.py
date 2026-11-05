"""[2b-1] Maintenance windows API, time zone setting, test clock, event filters, WS shapes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from netmon.runtime import Runtime
from tests.conftest import ADMIN, make_device, requires_db

pytestmark = requires_db


def z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_setup_timezone(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/setup", json={**ADMIN, "timezone": "Mars/Base"})
    assert r.status_code == 422
    r = await client.post("/api/setup", json={**ADMIN, "timezone": "Asia/Jerusalem"})
    assert r.status_code == 201, r.text
    assert (await client.get("/api/settings/public")).json()["timezone"] == "Asia/Jerusalem"
    s = (await client.get("/api/settings")).json()
    assert s["timezone"] == "Asia/Jerusalem" and s["topology_refresh_min"] == 30
    assert (await client.put("/api/settings", json={"timezone": "Nope/Zone"})).status_code == 422
    r = await client.put("/api/settings", json={"timezone": "Europe/Berlin"})
    assert r.json()["timezone"] == "Europe/Berlin"


async def test_maintenance_crud_and_validation(
    rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient
) -> None:
    await admin.put("/api/settings", json={"timezone": "Asia/Jerusalem"})
    root = (await admin.get("/api/groups", params={"parent_id": "root"})).json()["items"][0]["id"]
    branch = (await admin.post("/api/groups", json={"name": "Branch", "parent_id": root})).json()
    sub = (
        await admin.post("/api/groups", json={"name": "Floor", "parent_id": branch["id"]})
    ).json()
    d_sub = await make_device(admin, name="deep", host="10.0.0.5", group_id=sub["id"])
    d_root = await make_device(admin, name="top", host="10.0.0.6")
    now = rt.clock.now()
    good_once = {
        "name": "Firmware night",
        "scope": "group",
        "group_id": branch["id"],
        "kind": "once",
        "starts_at": z(now - timedelta(minutes=5)),
        "ends_at": z(now + timedelta(hours=2)),
        "comment": "HPE firmware 16.11",
    }
    bad = [
        {**good_once, "group_id": None},
        {**good_once, "group_id": 99999},
        {**good_once, "device_id": d_root["id"]},
        {**good_once, "scope": "global"},  # group_id must be null
        {**good_once, "ends_at": good_once["starts_at"]},
        {**good_once, "ends_at": z(now + timedelta(days=400))},
        {**good_once, "weekdays": [1]},
        {**good_once, "kind": "weekly"},
        {**good_once, "name": ""},
        {**good_once, "bogus": 1},
        {
            "name": "w",
            "scope": "global",
            "kind": "weekly",
            "weekdays": [8],
            "start_time": "02:00",
            "duration_min": 30,
        },
        {
            "name": "w",
            "scope": "global",
            "kind": "weekly",
            "weekdays": [1, 1],
            "start_time": "02:00",
            "duration_min": 30,
        },
        {
            "name": "w",
            "scope": "global",
            "kind": "weekly",
            "weekdays": [],
            "start_time": "02:00",
            "duration_min": 30,
        },
        {
            "name": "w",
            "scope": "global",
            "kind": "weekly",
            "weekdays": [1],
            "start_time": "24:00",
            "duration_min": 30,
        },
        {
            "name": "w",
            "scope": "global",
            "kind": "weekly",
            "weekdays": [1],
            "start_time": "02:00",
            "duration_min": 10081,
        },
    ]
    for body in bad:
        r = await admin.post("/api/maintenance-windows", json=body)
        assert r.status_code == 422, (body, r.text)
    assert (await viewer.post("/api/maintenance-windows", json=good_once)).status_code == 403
    r = await admin.post("/api/maintenance-windows", json=good_once)
    assert r.status_code == 201, r.text
    w = r.json()
    assert w["group_name"] == "Branch" and w["timezone"] == "Asia/Jerusalem"
    assert w["active_now"] is True and w["created_by"] == "admin" and w["enabled"] is True
    assert w["weekdays"] is None and w["start_time"] is None
    # group scope covers subgroups, not other groups
    assert (await admin.get(f"/api/devices/{d_sub['id']}")).json()["in_maintenance"] is True
    assert (await admin.get(f"/api/devices/{d_root['id']}")).json()["in_maintenance"] is False
    act = (await viewer.get("/api/maintenance/active")).json()
    assert [x["id"] for x in act["windows"]] == [w["id"]] and act["device_ids"] == [d_sub["id"]]
    tree = (await admin.get("/api/tree")).json()
    deep = tree["groups"][0]["children"][0]["children"][0]["devices"][0]
    assert deep["in_maintenance"] is True
    assert (await admin.get("/api/dashboard")).json()["maintenance"] == {
        "active_window_ids": [w["id"]],
        "device_count": 1,
    }
    # weekly window in the configured zone
    r = await admin.post(
        "/api/maintenance-windows",
        json={
            "name": "Weekly backup",
            "scope": "device",
            "device_id": d_root["id"],
            "kind": "weekly",
            "weekdays": [7, 3],
            "start_time": "02:00",
            "duration_min": 90,
        },
    )
    assert r.status_code == 201, r.text
    wk = r.json()
    assert wk["weekdays"] == [3, 7] and wk["start_time"] == "02:00" and wk["device_name"] == "top"
    occ = (
        await viewer.get(f"/api/maintenance-windows/{wk['id']}/occurrences", params={"days": 14})
    ).json()["items"]
    assert 3 <= len(occ) <= 5
    for o in occ:
        start = datetime.fromisoformat(o["start"].replace("Z", "+00:00"))
        local = start.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Jerusalem"))
        assert (local.hour, local.minute) == (2, 0) and local.isoweekday() in (3, 7)
        end = datetime.fromisoformat(o["end"].replace("Z", "+00:00"))
        assert end - start == timedelta(minutes=90)
    r = await viewer.get(f"/api/maintenance-windows/{wk['id']}/occurrences", params={"days": 91})
    assert r.status_code == 422
    # a past once window is allowed (a record)
    past = await admin.post(
        "/api/maintenance-windows",
        json={
            "name": "Old",
            "scope": "global",
            "kind": "once",
            "starts_at": z(now - timedelta(days=3)),
            "ends_at": z(now - timedelta(days=2)),
        },
    )
    assert past.status_code == 201 and past.json()["occurrence_start"] is None
    lst = (await viewer.get("/api/maintenance-windows")).json()
    assert [x["id"] for x in lst["items"]] == [w["id"], wk["id"], past.json()["id"]]
    lst = (await viewer.get("/api/maintenance-windows", params={"include_past": "false"})).json()
    assert past.json()["id"] not in [x["id"] for x in lst["items"]]
    lst = (await viewer.get("/api/maintenance-windows", params={"active": "true"})).json()
    assert [x["id"] for x in lst["items"]] == [w["id"]]
    lst = (await viewer.get("/api/maintenance-windows", params={"scope": "device"})).json()
    assert [x["id"] for x in lst["items"]] == [wk["id"]]
    # PATCH: kind change must send the matching fields; old ones are reset
    r = await admin.patch(f"/api/maintenance-windows/{wk['id']}", json={"kind": "once"})
    assert r.status_code == 422
    r = await admin.patch(
        f"/api/maintenance-windows/{wk['id']}",
        json={
            "kind": "once",
            "starts_at": z(now + timedelta(days=1)),
            "ends_at": z(now + timedelta(days=1, hours=1)),
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["weekdays"] is None and r.json()["kind"] == "once"
    r = await admin.patch(f"/api/maintenance-windows/{wk['id']}", json={"scope": "global"})
    assert r.status_code == 200 and r.json()["device_id"] is None
    # disabling ends the coverage immediately (maintenance.changed + "ended" event)
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    rt.hub.listeners.append(q)
    r = await admin.patch(f"/api/maintenance-windows/{w['id']}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["active_now"] is False
    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    rt.hub.listeners.remove(q)
    types = [m["type"] for m in msgs]
    assert "maintenance.changed" in types and "config.changed" in types
    mc = next(m for m in msgs if m["type"] == "maintenance.changed")["data"]
    assert mc == {
        "active_window_ids": [],
        "entered_device_ids": [],
        "left_device_ids": [d_sub["id"]],
    }
    cc = next(m for m in msgs if m["type"] == "config.changed")["data"]
    assert cc == {"entity": "maintenance_window", "id": w["id"], "action": "updated"}
    ev = (await admin.get("/api/events", params={"kind": "maintenance"})).json()["items"]
    assert (
        ev[0]["message"] == 'Maintenance "Firmware night" ended'
        and ev[0]["group_id"] == branch["id"]
    )
    assert (await admin.delete(f"/api/maintenance-windows/{w['id']}")).status_code == 204
    assert (await admin.get(f"/api/maintenance-windows/{w['id']}")).status_code == 404


async def test_timezone_change_moves_weekly_windows(rt: Runtime, admin: httpx.AsyncClient) -> None:
    now = rt.clock.now()
    utc_now = now.astimezone(UTC)
    # a daily 60-min window starting at the current UTC hour: active in UTC
    hhmm = f"{utc_now.hour:02d}:00"
    await admin.put("/api/settings", json={"timezone": "UTC"})
    r = await admin.post(
        "/api/maintenance-windows",
        json={
            "name": "hourly",
            "scope": "global",
            "kind": "weekly",
            "weekdays": [1, 2, 3, 4, 5, 6, 7],
            "start_time": hhmm,
            "duration_min": 60,
        },
    )
    assert r.json()["active_now"] is True
    assert rt.maintenance.active_window_ids() == [r.json()["id"]]
    # in Asia/Tokyo (+9, no DST) the same wall-clock hour is 9 hours earlier in UTC
    await admin.put("/api/settings", json={"timezone": "Asia/Tokyo"})
    assert rt.maintenance.active_window_ids() == []
    assert (await admin.get(f"/api/maintenance-windows/{r.json()['id']}")).json()[
        "active_now"
    ] is False


async def test_test_clock(rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient) -> None:
    assert rt.settings.netmon_test_mode is False
    r = await admin.put("/api/test/clock", json={"offset_s": 60})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    rt.settings.netmon_test_mode = True
    try:
        assert (await viewer.put("/api/test/clock", json={"offset_s": 60})).status_code == 403
        bad = await admin.put("/api/test/clock", json={"offset_s": 31536001})
        assert bad.status_code == 422
        before = datetime.now(UTC)
        r = await admin.put("/api/test/clock", json={"offset_s": 3600})
        assert r.status_code == 200 and r.json()["offset_s"] == 3600
        now = datetime.fromisoformat(r.json()["now"].replace("Z", "+00:00"))
        assert timedelta(minutes=59) < now - before < timedelta(minutes=61)
        # a window that starts in 30 minutes is active at the shifted time
        start = before + timedelta(minutes=30)
        w = await admin.post(
            "/api/maintenance-windows",
            json={
                "name": "soon",
                "scope": "global",
                "kind": "once",
                "starts_at": z(start),
                "ends_at": z(start + timedelta(hours=1)),
            },
        )
        assert w.json()["active_now"] is True and rt.maintenance.active_window_ids() == [
            w.json()["id"]
        ]
        r = await admin.put("/api/test/clock", json={"offset_s": 0})
        assert r.json()["offset_s"] == 0 and rt.maintenance.active_window_ids() == []
    finally:
        rt.settings.netmon_test_mode = False
        rt.clock.set_offset(0)


@pytest.mark.parametrize("value", ["true", "false"])
async def test_event_filters(admin: httpx.AsyncClient, value: str) -> None:
    r = await admin.get("/api/events", params={"in_maintenance": value, "kind": "maintenance"})
    assert r.status_code == 200 and r.json()["total"] == 0
    r = await admin.get("/api/sensors", params={"status": "unreachable", "acknowledged": value})
    assert r.status_code == 200
    r = await admin.get("/api/devices", params={"status": "unreachable"})
    assert r.status_code == 200


async def test_sensor_types_value_map(admin: httpx.AsyncClient) -> None:
    types = {t["type"]: t for t in (await admin.get("/api/sensor-types")).json()["items"]}
    fields = {f["key"]: f for f in types["snmp_custom"]["config_fields"]}
    assert fields["value_map"]["type"] == "value_map"
    assert fields["value_map_default"]["options"] == ["up", "warning", "down"]
    assert "enum" in fields["value_type"]["options"]
    tpl = (await admin.get("/api/device-templates/hpe_comware")).json()
    assert tpl["match"][0]["enterprises"] == [25506]
    table = next(
        e
        for e in tpl["entries"]
        if e["kind"] == "snmp_table" and e["config"].get("value_type") == "enum"
    )
    assert table["filters"][0] == {"oid": "1.3.6.1.2.1.47.1.1.1.1.5", "in": [6, 7], "not_in": None}
    assert table["interval_s"] == 300 and table["skip_zero"] is False
