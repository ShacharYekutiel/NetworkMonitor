"""[3] New-device alerts end to end: baseline per switch, learning period, access-port rule,
allow-list, random MACs, flood guard, events / `new_mac` conditions, acknowledge, forget,
OFF -> ON."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import text

from netmon.runtime import Runtime
from tests.conftest import make_device
from tests.conftest import requires_db as _requires_db
from tests.test_2b3_ingest import features

pytestmark = _requires_db


async def setup_switch(rt: Runtime, did: int) -> None:
    now = rt.clock.now()
    async with rt.sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO port_inventory_status (device_id, sources) VALUES "
                '(:d, CAST(\'{"fdb": "ok", "fdb_mib": "q_bridge"}\' AS jsonb))'
            ),
            {"d": did},
        )
        for idx, name, kind, mode, lag in (
            (17, "1/0/17", "physical", "access", None),
            (18, "1/0/18", "physical", "access", None),  # excluded below
            (19, "1/0/19", "physical", None, None),
            (49, "1/0/49", "physical", "trunk", None),
            (3, "3", "physical", "access", 100),  # LAG member
        ):
            await session.execute(
                text(
                    "INSERT INTO switch_ports (device_id, if_index, name, kind, admin_status, "
                    "oper_status, mode, lag_if_index, collected_at) VALUES (:d, :i, :n, :k, "
                    "'up', 'up', :m, :lag, :now)"
                ),
                {"d": did, "i": idx, "n": name, "k": kind, "m": mode, "lag": lag, "now": now},
            )
        await session.commit()


async def fdb(rt: Runtime, did: int, entries: list[tuple[int, str, int | None]]) -> Any:
    """Replace the present MACs of the switch, then run detection."""
    now = rt.clock.now()
    async with rt.sessionmaker() as session:
        await session.execute(text("DELETE FROM port_macs WHERE device_id = :d"), {"d": did})
        for idx, mac, vlan in entries:
            await session.execute(
                text(
                    "INSERT INTO port_macs (device_id, if_index, mac, vlan, first_seen, "
                    "last_seen) VALUES (:d, :i, :m, :v, :now, :now)"
                ),
                {"d": did, "i": idx, "m": mac, "v": vlan, "now": now},
            )
        await session.commit()
    return await rt.newdev.after_fdb(did, now)


async def new_mac_sensor(c: httpx.AsyncClient, did: int) -> dict[str, Any] | None:
    items = (await c.get("/api/sensors", params={"device_id": did, "type": "new_mac"})).json()[
        "items"
    ]
    return items[0] if items else None


async def test_new_device_flow(
    rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient
) -> None:
    assert (await admin.get("/api/new-devices")).status_code == 409
    sw = await make_device(admin, name="sw-floor2", host="10.9.9.9", newdev_excluded_ports=[18])
    assert sw["newdev_excluded_ports"] == [18]
    did = sw["id"]
    await setup_switch(rt, did)
    await features(admin, new_device_alerts=True, audit=True)
    st = (await viewer.get("/api/new-devices/status")).json()
    assert st["learning"] is True and st["learning_until"] is not None  # 7 days from ON
    # first FDB read of the switch -> baseline (even after learning ended)
    await admin.put("/api/settings", json={"newdev_learning_days": 0})
    st = (await admin.post("/api/new-devices/restart-learning")).json()
    assert st["learning"] is False
    base = [(17, "00:1b:21:aa:00:01", 20), (49, "00:1b:21:bb:00:01", 1),
            (18, "00:1b:21:cc:00:01", 20), (3, "00:1b:21:dd:00:01", 20)]  # fmt: skip
    await fdb(rt, did, base)
    rows = (await admin.get("/api/new-devices")).json()
    assert [(r["mac"], r["state"]) for r in rows["items"]] == [("00:1b:21:aa:00:01", "baseline")]
    # allow-list
    r = await admin.post("/api/new-devices/allowlist", json={"prefix": "3C-A8-2A", "comment": "HP"})
    assert r.status_code == 201 and r.json()["prefix"] == "3c:a8:2a" and r.json()["bits"] == 24
    assert (
        await admin.post("/api/new-devices/allowlist", json={"prefix": "3ca82a"})
    ).status_code == 409
    assert (
        await admin.post("/api/new-devices/allowlist", json={"prefix": "3ca8"})
    ).status_code == 422
    # second run: new, allowed, known, random
    res = await fdb(
        rt,
        did,
        [*base, (17, "00:1b:21:aa:00:02", 20), (19, "3c:a8:2a:00:00:01", 20),
         (17, "02:00:00:aa:00:02", 20)],
    )  # fmt: skip
    assert res == {"new": 1, "summary_count": 0}
    states = {r["mac"]: r["state"] for r in (await admin.get("/api/new-devices")).json()["items"]}
    assert states == {
        "00:1b:21:aa:00:01": "baseline",
        "00:1b:21:aa:00:02": "new",
        "3c:a8:2a:00:00:01": "allowed",
    }
    lst = (await viewer.get("/api/new-devices", params={"state": "new"})).json()["items"]
    item = lst[0]
    assert item["first_device_name"] == "sw-floor2" and item["first_port_name"] == "1/0/17"
    assert item["first_vlan"] == 20 and item["detected_at"] and not item["locally_administered"]
    evs = (await admin.get("/api/events", params={"kind": "new_device"})).json()["items"]
    assert [e["message"] for e in evs] == [
        f"New device 00:1b:21:aa:00:02 ({item['vendor']}) on sw-floor2 port 1/0/17, VLAN 20"
        if item["vendor"] else "New device 00:1b:21:aa:00:02 on sw-floor2 port 1/0/17, VLAN 20"
    ]  # fmt: skip
    s = await new_mac_sensor(admin, did)
    assert s is not None and s["status"] == "warning"
    assert (await viewer.get("/api/new-devices/status")).json()["new_unacknowledged"] == 1
    # q filter
    q = (await viewer.get("/api/new-devices", params={"q": "001b21aa0002"})).json()
    assert [r["mac"] for r in q["items"]] == ["00:1b:21:aa:00:02"]
    # acknowledge -> known, condition removed
    assert (
        await viewer.post("/api/new-devices/acknowledge", json={"macs": ["00:1b:21:aa:00:02"]})
    ).status_code == 403
    r = await admin.post(
        "/api/new-devices/acknowledge",
        json={"macs": ["00-1B-21-AA-00-02"], "comment": "New laptop for Dana"},
    )
    assert r.json() == {"acknowledged": 1}
    row = (await admin.get("/api/new-devices", params={"state": "known"})).json()["items"][0]
    assert row["ack_by"] == "admin" and row["ack_comment"] == "New laptop for Dana"
    assert (await new_mac_sensor(admin, did))["status"] == "up"
    # flood guard: 25 new -> 20 events + 1 summary (one port behind an unmanaged switch)
    await admin.put("/api/settings", json={"uplink_mac_threshold": 1000})
    flood = [(17, f"00:1b:21:ee:00:{i:02x}", 20) for i in range(25)]
    res = await fdb(rt, did, [*base, *flood])
    assert res == {"new": 25, "summary_count": 5}
    evs = (await admin.get("/api/events", params={"kind": "new_device", "limit": 100})).json()
    msgs = [e["message"] for e in evs["items"]]
    assert len(msgs) == 22 and "+5 more new devices on sw-floor2" in msgs
    s = await new_mac_sensor(admin, did)
    assert s["status"] == "warning" and "(+24 more)" in s["status_message"]
    # forget
    assert (await admin.delete("/api/new-devices/00:1b:21:ee:00:00")).status_code == 204
    assert (await admin.delete("/api/new-devices/00:1b:21:ee:00:00")).status_code == 404
    # OFF: conditions removed, data kept, 409
    await features(admin, new_device_alerts=False)
    s = await new_mac_sensor(admin, did)
    assert s["status"] == "up" and s["status_message"] == "New-device alerts are turned off"
    assert (await admin.get("/api/new-devices")).status_code == 409
    assert await fdb(rt, did, [(17, "00:1b:21:ff:00:01", 20)]) is None
    # ON again: learning not restarted, but the switch baselines again (rule 4)
    await features(admin, new_device_alerts=True)
    await fdb(rt, did, [(17, "00:1b:21:ff:00:02", 20)])
    states = {r["mac"]: r["state"] for r in (await admin.get("/api/new-devices")).json()["items"]}
    assert states["00:1b:21:ff:00:02"] == "baseline" and "00:1b:21:ff:00:01" not in states
    actions = {
        a["action"] for a in (await admin.get("/api/audit", params={"limit": 200})).json()["items"]
    }
    assert {"newdev.acknowledge", "newdev.forget", "newdev.restart_learning",
            "newdev.allowlist_create"} <= actions  # fmt: skip
    entry = (await admin.get("/api/new-devices/allowlist")).json()["items"][0]
    assert (await admin.delete(f"/api/new-devices/allowlist/{entry['id']}")).status_code == 204


async def test_scope_and_uplink(rt: Runtime, admin: httpx.AsyncClient) -> None:
    g = (await admin.get("/api/groups", params={"parent_id": "root"})).json()["items"][0]
    other = (await admin.post("/api/groups", json={"name": "lab", "parent_id": g["id"]})).json()
    sw = await make_device(admin, name="sw-a", host="10.9.9.10")
    await setup_switch(rt, sw["id"])
    await features(admin, new_device_alerts=True)
    await admin.put("/api/settings", json={"newdev_learning_days": 0})
    await admin.post("/api/new-devices/restart-learning")
    await fdb(rt, sw["id"], [])  # first read: baseline
    # uplink by mac_count (> uplink_mac_threshold 5): no detection on that port
    many = [(19, f"00:1b:21:12:00:{i:02x}", 20) for i in range(8)]
    assert (await fdb(rt, sw["id"], many)) == {"new": 0}
    assert (await admin.get("/api/new-devices")).json()["total"] == 0
    # out of scope -> nothing
    await admin.put("/api/settings", json={"newdev_scope_group_ids": [other["id"]]})
    assert (await fdb(rt, sw["id"], [(17, "00:1b:21:13:00:01", 20)])) is None
    st = (await admin.get("/api/new-devices/status")).json()
    assert st["scope_group_ids"] == [other["id"]]


async def test_new_mac_sensor_auto_created_on(rt: Runtime, admin: httpx.AsyncClient) -> None:
    """QA phase 3: the `new_mac` sensor exists for in-scope switches while the feature is ON
    (not only after the first condition); a deleted one is not re-created (opt-out)."""
    g = (await admin.get("/api/groups", params={"parent_id": "root"})).json()["items"][0]
    lab = (await admin.post("/api/groups", json={"name": "lab2", "parent_id": g["id"]})).json()
    sw = await make_device(admin, name="sw-on", host="10.9.8.1", port_inventory="on")
    sw2 = await make_device(
        admin, name="sw-lab", host="10.9.8.2", port_inventory="on", group_id=lab["id"]
    )
    srv = await make_device(admin, name="srv", host="10.9.8.3")  # no port inventory
    assert await new_mac_sensor(admin, sw["id"]) is None
    await features(admin, new_device_alerts=True)
    s = await new_mac_sensor(admin, sw["id"])
    assert s is not None and s["name"] == "New devices" and s["status"] == "up"
    assert await new_mac_sensor(admin, sw2["id"]) is not None
    assert await new_mac_sensor(admin, srv["id"]) is None
    # deleted -> opt-out: not re-created by OFF/ON nor by a detection run
    assert (await admin.delete(f"/api/sensors/{s['id']}")).status_code == 204
    await features(admin, new_device_alerts=False)
    await features(admin, new_device_alerts=True)
    assert await new_mac_sensor(admin, sw["id"]) is None
    await setup_switch(rt, sw["id"])
    await fdb(rt, sw["id"], [])
    assert await new_mac_sensor(admin, sw["id"]) is None
    # scope: only switches of the scope groups get one
    await admin.put("/api/settings", json={"newdev_scope_group_ids": [lab["id"]]})
    sw3 = await make_device(admin, name="sw-new", host="10.9.8.4", port_inventory="on")
    await rt.drain()
    assert await new_mac_sensor(admin, sw3["id"]) is None
    await admin.put("/api/settings", json={"newdev_scope_group_ids": []})
    assert await new_mac_sensor(admin, sw3["id"]) is not None
    # a switch added while ON gets its sensor (device change -> sync)
    sw4 = await make_device(admin, name="sw-added", host="10.9.8.5", port_inventory="on")
    await rt.drain()
    assert await new_mac_sensor(admin, sw4["id"]) is not None
