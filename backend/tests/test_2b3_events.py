"""[2b-3] Event rules API, passive event sensors and conditions (with the alert engine), flapping,
device timeline, LAG membership in the ports view, migration 0006."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest

from netmon.clock import FakeClock
from netmon.db import timescale
from netmon.db.engine import make_engine
from netmon.db.migrate import run_migrations
from netmon.runtime import Runtime
from tests.conftest import TEST_DATABASE_URL, _plain, db_url_for, make_device, make_sensor
from tests.conftest import requires_db as _requires_db
from tests.ingest_helpers import send_udp, src, v2c_trap, wait_for
from tests.test_2b3_ingest import features, has, ports

pytestmark = _requires_db

LINK_DOWN, LINK_UP = "1.3.6.1.6.3.1.1.5.3", "1.3.6.1.6.3.1.1.5.4"
IFINDEX, ADMIN_ST = "1.3.6.1.2.1.2.2.1.1", "1.3.6.1.2.1.2.2.1.7"


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

    async def __call__(self, channel, payload, smtp, lang):  # type: ignore[no-untyped-def]
        self.items.append((channel["name"], payload))


def link(oid: str, idx: int, admin_status: int = 1) -> bytes:
    return v2c_trap(oid, [(f"{IFINDEX}.{idx}", "i", idx), (f"{ADMIN_ST}.{idx}", "i", admin_status)])


async def rules_by_key(c: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    items = (await c.get("/api/event-rules")).json()["items"]
    return {r["builtin"] or r["name"]: r for r in items}


async def sensor_of(c: httpx.AsyncClient, device_id: int, type_: str) -> dict[str, Any] | None:
    r = await c.get("/api/sensors", params={"device_id": device_id, "type": type_})
    items = r.json()["items"]
    return items[0] if items else None


# ------------------------------------------------------------------ rules API
async def test_event_rules_api(rt: Runtime, admin: httpx.AsyncClient) -> None:
    await features(admin, traps=True)
    rules = await rules_by_key(admin)
    assert set(rules) == {"trap_link_down", "trap_auth_failure", "trap_restart", "syslog_error",
                          # [3] built-in config-change triggers (action config_backup)
                          "syslog_comware_cfg_changed", "syslog_arubaos_cfg_changed",
                          "syslog_cisco_config_i"}  # fmt: skip
    ld = rules["trap_link_down"]
    assert ld["enabled"] is False and ld["priority"] == 1000 and ld["name"] == "Link down"
    assert ld["key_varbind_oid"] == IFINDEX and ld["clear_after_min"] == 1440
    assert ld["match"]["ports"] == "important" and ld["active_conditions"] == 0
    # localized built-in names (the request user's language)
    await admin.patch("/api/auth/me", json={"language": "he"})
    he = await rules_by_key(admin)
    assert he["trap_link_down"]["name"] == "קישור נפל"
    await admin.patch("/api/auth/me", json={"language": "en"})
    # create / validate / patch / delete / reset
    body = {"name": "Fan", "kind": "syslog", "scope": "global", "action": "down",
            "match": {"severity_max": 4, "text": r"fan (\d+)", "text_regex": True},
            "key_regex": r"fan (\d+)", "clear_after_min": 10}  # fmt: skip
    r = await admin.post("/api/event-rules", json=body)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["builtin"] is None and r.json()["match"]["facilities"] == []
    r = await admin.post("/api/event-rules", json={**body, "key_regex": "(a)(b)"})
    assert r.status_code == 422
    r = await admin.post("/api/event-rules", json={**body, "scope": "device", "device_id": 999})
    assert r.status_code == 422
    r = await admin.patch(f"/api/event-rules/{rid}", json={"kind": "trap"})
    assert r.status_code == 422
    r = await admin.patch(f"/api/event-rules/{rid}", json={"priority": 5, "enabled": False})
    assert r.status_code == 200 and r.json()["priority"] == 5 and r.json()["enabled"] is False
    assert (await admin.get(f"/api/event-rules/{rid}")).json()["name"] == "Fan"
    r = await admin.delete(f"/api/event-rules/{ld['id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "builtin_rule"
    r = await admin.patch(f"/api/event-rules/{ld['id']}", json={"name": "Mine", "enabled": True})
    assert r.json()["name"] == "Mine"
    r = await admin.post(f"/api/event-rules/{ld['id']}/reset")
    assert r.json()["name"] == "Link down" and r.json()["enabled"] is True
    assert (await admin.post(f"/api/event-rules/{rid}/reset")).status_code == 404
    assert (await admin.delete(f"/api/event-rules/{rid}")).status_code == 204
    assert (await admin.get(f"/api/event-rules/{rid}")).status_code == 404
    assert [
        x["kind"] for x in (await admin.get("/api/event-rules?kind=syslog")).json()["items"]
    ] == ["syslog"] * 4  # [3] + the 3 built-in config-change rules


async def test_rule_test_preview_and_viewer(
    rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient
) -> None:
    tp, _ = ports(rt)
    await make_device(admin, name="sw", host=src("127.0.0.81"))
    await features(admin, traps=True)
    send_udp(tp, link(LINK_DOWN, 5))
    send_udp(tp, link(LINK_UP, 5))
    await wait_for(lambda: has(admin, "/api/traps", 2))
    rules = await rules_by_key(admin)
    ld = rules["trap_link_down"]
    rule = {k: ld[k] for k in ("name", "kind", "match", "action", "key_varbind_oid",
                               "clear_match", "clear_after_min")}  # fmt: skip
    r = await admin.post("/api/event-rules/test", json={"rule": rule, "hours": 1})
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["scanned"], out["matched"], out["cleared"]) == (2, 1, 1)
    assert {(i["result"], i["key"]) for i in out["items"]} == {("raise", "5"), ("clear", "5")}
    assert (await viewer.get("/api/event-rules")).status_code == 200
    assert (await viewer.post("/api/event-rules", json=rule)).status_code == 403


# ------------------------------------------------------------------ [1.3.3] ReDoS fix
async def test_event_rule_rejects_unsafe_regex(rt: Runtime, admin: httpx.AsyncClient) -> None:
    """RE2-incompatible patterns (backreference, lookaround) are rejected at save time."""
    await features(admin, syslog=True)
    base = {"name": "r", "kind": "syslog", "scope": "global", "action": "down",
            "match": {"text_regex": True}, "clear_after_min": 10}  # fmt: skip
    for pat in (r"(a)\1", "(?=secret)", "(?<=x)y"):
        r = await admin.post(
            "/api/event-rules", json={**base, "match": {"text": pat, "text_regex": True}}
        )
        assert r.status_code == 422, (pat, r.text)
    # an unsafe key_regex is rejected too
    r = await admin.post(
        "/api/event-rules",
        json={**base, "match": {"text": "down", "text_regex": True}, "key_regex": r"(a)\1"},
    )
    assert r.status_code == 422
    # a catastrophic-for-`re` but RE2-safe pattern is accepted (it is linear under RE2)
    r = await admin.post(
        "/api/event-rules", json={**base, "match": {"text": "(a+)+$", "text_regex": True}}
    )
    assert r.status_code == 201, r.text


async def test_event_rule_test_redos_does_not_block_loop(
    rt: Runtime, admin: httpx.AsyncClient
) -> None:
    """A ReDoS pattern tested against a hostile message returns promptly and the scan runs off
    the event loop, so a concurrent request is served meanwhile."""
    _, sp = ports(rt)
    await make_device(admin, name="sw81", host=src("127.0.0.81"))
    await features(admin, syslog=True)
    send_udp(sp, b"<13>" + b"a" * 60 + b"!")  # hostile payload for `(a+)+$`
    await wait_for(lambda: has(admin, "/api/syslog", 1))
    rule = {"name": "redos", "kind": "syslog", "scope": "global", "action": "down",
            "match": {"text": "(a+)+$", "text_regex": True}, "clear_after_min": 10}  # fmt: skip
    start = time.perf_counter()
    test_resp, status_resp = await asyncio.gather(
        admin.post("/api/event-rules/test", json={"rule": rule, "hours": 1}),
        admin.get("/api/setup/status"),
    )
    elapsed = time.perf_counter() - start
    assert test_resp.status_code == 200, test_resp.text
    assert test_resp.json()["scanned"] >= 1
    assert status_resp.status_code == 200
    assert status_resp.json() == {"setup_required": False}
    assert elapsed < 2.0  # linear matching + worker thread: nowhere near a hang


# ------------------------------------------------------------------ event sensors
async def test_event_alert_flow(rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock) -> None:
    tp, sp = ports(rt)
    d = await make_device(admin, name="core", host=src("127.0.0.81"))
    box = Outbox()
    rt.alerts.sender = box
    ch = (
        await admin.post(
            "/api/notification-channels",
            json={"name": "hook", "type": "webhook", "config": {"url": "http://h.invalid/"}},
        )
    ).json()["id"]
    r = await admin.post(
        "/api/alert-rules",
        json={"name": "All", "on_statuses": ["down", "warning", "up"], "channel_ids": [ch]},
    )
    assert r.status_code == 201, r.text
    await features(admin, traps=True, syslog=True, event_alerts=True)
    for key in ("trap_link_down", "syslog_error"):
        rid = (await rules_by_key(admin))[key]["id"]
        await admin.patch(f"/api/event-rules/{rid}", json={"enabled": True})
    # linkDown (device without port data: every port important) -> trap sensor warning
    send_udp(tp, link(LINK_DOWN, 5))
    ts = await wait_for(lambda: _status(admin, d["id"], "trap", "warning"))
    assert ts["name"] == "SNMP traps" and ts["status_message"].startswith("Link down: linkDown")
    conds = (await admin.get(f"/api/sensors/{ts['id']}/conditions")).json()["items"]
    assert len(conds) == 1 and conds[0]["key"] == "5" and conds[0]["rule_name"] == "Link down"
    assert conds[0]["last_kind"] == "trap" and conds[0]["expires_at"] is not None
    await rt.drain()
    assert [p["stage"] for _, p in box.items] == ["problem"]
    assert box.items[0][1]["after_flapping"] is False
    ev = (await admin.get("/api/events", params={"kind": "notification_sent"})).json()["items"]
    assert ev and ev[0]["message"] == 'Sent WARNING to hook (rule "All")'
    # linkUp of the same ifIndex clears -> up -> recovery
    send_udp(tp, link(LINK_UP, 5))
    await wait_for(lambda: _status(admin, d["id"], "trap", "up"))
    await rt.drain()
    assert [p["stage"] for _, p in box.items] == ["problem", "recovery"]
    traps = (await admin.get("/api/traps")).json()["items"]
    assert all(t["matched_rule_ids"] for t in traps)
    # syslog err -> syslog sensor warning; auto-clear after 30 min (test clock)
    send_udp(sp, b"<187>Nov  8 12:00:00 core app: disk failure")
    ss = await wait_for(lambda: _status(admin, d["id"], "syslog", "warning"))
    clock.advance(29 * 60)
    await rt.event_sensors.expire_once()
    assert (await _status(admin, d["id"], "syslog", "warning")) is not None
    clock.advance(2 * 60)
    assert await rt.event_sensors.expire_once() == 1
    assert await _status(admin, d["id"], "syslog", "up")
    # manual clear, passive refusals, pause
    send_udp(sp, b"<187>Nov  8 12:00:00 core app: disk failure again")
    await wait_for(lambda: _status(admin, d["id"], "syslog", "warning"))
    r = await admin.post(f"/api/sensors/{ss['id']}/clear")
    assert r.status_code == 200 and r.json()["status"] == "up"
    assert r.json()["status_message"] == "Cleared by admin"
    assert (await admin.post(f"/api/sensors/{ss['id']}/run")).status_code == 409
    r = await admin.post("/api/sensors/test", json={"type": "syslog", "host": "x", "config": {}})
    assert r.status_code == 409 and r.json()["error"]["code"] == "sensor_passive"
    ping = await make_sensor(admin, d["id"], "ping")
    r = await admin.get(f"/api/sensors/{ping['id']}/conditions")
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_event_sensor"
    r = await admin.patch(f"/api/sensors/{ss['id']}", json={"enabled": False, "interval_s": 999})
    assert r.json()["status"] == "paused" and r.json()["interval_s"] == 60
    r = await admin.patch(f"/api/sensors/{ss['id']}", json={"enabled": True})
    assert r.json()["status"] == "up"
    # delete -> opt-out, not re-created; manual create removes the opt-out
    assert (await admin.delete(f"/api/sensors/{ss['id']}")).status_code == 204
    dev = (await admin.get(f"/api/devices/{d['id']}")).json()
    assert dev["event_sensor_optout"] == ["syslog"]
    send_udp(sp, b"<187>Nov  8 12:00:00 core app: x")
    await wait_for(lambda: has(admin, "/api/syslog", 3))
    await rt.drain()
    assert await sensor_of(admin, d["id"], "syslog") is None
    r = await admin.post(
        "/api/sensors", json={"device_id": d["id"], "type": "syslog", "config": {}}
    )
    assert r.status_code == 201 and r.json()["status"] == "up"
    r = await admin.post(
        "/api/sensors", json={"device_id": d["id"], "type": "syslog", "config": {}}
    )
    assert r.status_code == 409 and r.json()["error"]["details"] == [{"field": "type"}]
    assert (await admin.get(f"/api/devices/{d['id']}")).json()["event_sensor_optout"] == []
    # event_alerts OFF: conditions deleted, sensors up with the explanation
    send_udp(tp, link(LINK_DOWN, 7))
    await wait_for(lambda: _status(admin, d["id"], "trap", "warning"))
    await features(admin, event_alerts=False)
    ts = await sensor_of(admin, d["id"], "trap")
    assert ts is not None and ts["status"] == "up"
    assert ts["status_message"] == "Event alerts are turned off"
    assert (await admin.get(f"/api/sensors/{ts['id']}/conditions")).json()["items"] == []
    types = {t["type"]: t for t in (await admin.get("/api/sensor-types")).json()["items"]}
    assert types["trap"]["passive"] is True and types["ping"]["passive"] is False


async def _status(c: httpx.AsyncClient, did: int, type_: str, status: str) -> Any:
    s = await sensor_of(c, did, type_)
    return s if s is not None and s["status"] == status else None


async def test_important_ports_and_linkdown_scope(rt: Runtime, admin: httpx.AsyncClient) -> None:
    tp, _ = ports(rt)
    d = await make_device(admin, name="acc", host=src("127.0.0.81"))
    async with rt.sessionmaker() as s:
        from sqlalchemy import text

        for idx in (5, 7):
            await s.execute(
                text(
                    "INSERT INTO switch_ports (device_id, if_index, name, kind, admin_status, "
                    "oper_status, collected_at) VALUES (:d, :i, :n, 'physical', 'up', 'up', now())"
                ),
                {"d": d["id"], "i": idx, "n": f"Gi1/0/{idx}"},
            )
        await s.commit()
    # [1.3] a port with a *critical* interface sensor is important (a new interface sensor on
    # an unimportant port defaults to impact warning)
    sens = await make_sensor(
        admin, d["id"], "snmp_interface", {"if_index": 5, "if_name": "Gi1/0/5"}
    )
    assert sens["impact"] == "warning"
    await admin.patch(f"/api/sensors/{sens['id']}", json={"impact": "critical"})
    await features(admin, traps=True, event_alerts=True)
    rid = (await rules_by_key(admin))["trap_link_down"]["id"]
    await admin.patch(f"/api/event-rules/{rid}", json={"enabled": True})
    send_udp(tp, link(LINK_DOWN, 7))  # unimportant port: nothing
    await wait_for(lambda: has(admin, "/api/traps", 1))
    await rt.drain()
    # the trap event sensor is created asynchronously: wait for it instead of reading once
    ts = await wait_for(lambda: sensor_of(admin, d["id"], "trap"))
    assert ts["status"] == "up"
    t = (await admin.get("/api/traps")).json()["items"][0]
    assert t["summary"].endswith("port=Gi1/0/7") and t["matched_rule_ids"] == []
    send_udp(tp, link(LINK_DOWN, 5))  # critical interface sensor -> important
    await wait_for(lambda: _status(admin, d["id"], "trap", "warning"))
    await admin.put("/api/settings", json={"event_linkdown_scope": "all"})
    send_udp(tp, link(LINK_DOWN, 7))
    await wait_for(lambda: _conds(admin, ts["id"], 2))


async def _conds(c: httpx.AsyncClient, sid: int, n: int) -> bool:
    return len((await c.get(f"/api/sensors/{sid}/conditions")).json()["items"]) == n


# ------------------------------------------------------------------ flapping
async def test_flapping(rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock) -> None:
    tp, _ = ports(rt)
    d = await make_device(admin, name="flappy", host=src("127.0.0.81"))
    await features(admin, traps=True, flapping=True)
    for i in range(6):
        send_udp(tp, link(LINK_DOWN if i % 2 == 0 else LINK_UP, 5))
        await wait_for(lambda i=i: has(admin, "/api/traps", i + 1))
        clock.advance(10)  # alternating directions: never a duplicate
    s = await wait_for(lambda: _status(admin, d["id"], "port_flap", "warning"))
    assert s["name"] == "Port flapping"
    assert s["status_message"] == "Port 5 is flapping (6 changes in 10 min)"
    assert rt.flapping.is_flapping(d["id"], 5)
    clock.advance(16 * 60)
    await rt.event_sensors.expire_once()
    await rt.drain()
    assert await _status(admin, d["id"], "port_flap", "up")
    assert not rt.flapping.is_flapping(d["id"], 5)
    await features(admin, flapping=False)


# ------------------------------------------------------------------ timeline
async def test_timeline(rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient) -> None:
    from sqlalchemy import text

    await features(admin, timeline=True, audit=True)
    d = await make_device(admin, name="tl", host="10.5.5.5")
    p = await make_sensor(admin, d["id"], "ping")
    now = datetime.now(UTC).replace(microsecond=0)
    async with rt.sessionmaker() as s:
        rows = [
            ("status_change", now - timedelta(minutes=5), p["id"], "up", "down", "x"),
            ("notification_sent", now - timedelta(minutes=4), p["id"], "up", "down",
             'Sent DOWN to hook (rule "All")'),
            ("acknowledge", now - timedelta(minutes=3), p["id"], "down", "down",
             "Acknowledged by admin: on it"),
            ("port_change", now - timedelta(minutes=2), None, "up", "down", "Port 5 down"),
            ("maintenance", now - timedelta(minutes=1), None, None, None, "Maintenance started"),
        ]  # fmt: skip
        for kind, t, sid, old, new, msg in rows:
            await s.execute(
                text(
                    "INSERT INTO events (time, kind, sensor_id, device_id, sensor_name, "
                    "device_name, old_status, new_status, message) VALUES (:t, :k, :s, "
                    ":d, 'Ping', 'tl', :o, :n, :m)"
                ),
                {"t": t, "k": kind, "s": sid, "d": d["id"] if kind != "maintenance" else None,
                 "o": old, "n": new, "m": msg},
            )  # fmt: skip
        await s.commit()
    r = await admin.get(f"/api/devices/{d['id']}/timeline")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    kinds = [i["kind"] for i in items]
    assert [k for k in kinds if k != "audit"] == [
        "maintenance",
        "port",
        "ack",
        "notification",
        "status",
    ]
    assert "audit" in kinds  # device/sensor create by the admin
    ack = next(i for i in items if i["kind"] == "ack")
    assert ack["actor"] == "admin" and ack["ref"]["type"] == "event"
    st = next(i for i in items if i["kind"] == "status")
    assert (st["status"], st["old_status"], st["sensor_id"]) == ("down", "up", p["id"])
    vi = (await viewer.get(f"/api/devices/{d['id']}/timeline")).json()["items"]
    assert "audit" not in {i["kind"] for i in vi}
    # paging with the cursor walks the same order
    seen: list[tuple[str, int]] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = (await admin.get(f"/api/devices/{d['id']}/timeline", params=params)).json()
        seen += [(i["kind"], i["id"]) for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [(i["kind"], i["id"]) for i in items]
    only = (
        await admin.get(f"/api/devices/{d['id']}/timeline", params={"kinds": ["port", "ack"]})
    ).json()["items"]
    assert [i["kind"] for i in only] == ["port", "ack"]
    assert (await admin.get("/api/devices/9999/timeline")).status_code == 404


# ------------------------------------------------------------------ LAG
async def _lag_lab(rt: Runtime, admin: httpx.AsyncClient) -> tuple[int, int]:
    from sqlalchemy import text

    a = await make_device(admin, name="aos86", host="10.0.0.86")
    b = await make_device(admin, name="cw87", host="10.0.0.87")
    ports_ = [  # if_index, name, kind, bridge_port, pvid, mode, untagged, tagged, mac_count, lag
        (1, "1", "physical", 1, 1, "access", [1], [], 1, None, None),
        (3, "3", "physical", None, None, None, [], [], 0, 11, True),
        (4, "4", "physical", None, None, None, [], [], 0, 11, True),
        (11, "Trk1", "lag", 11, 1, "trunk", [1], [10, 20], 2, None, None),
    ]
    async with rt.sessionmaker() as s:
        for idx, name, kind, bp, pvid, mode, unt, tag, macs, lag, att in ports_:
            await s.execute(
                text(
                    "INSERT INTO switch_ports (device_id, if_index, name, descr, kind, "
                    "admin_status, oper_status, bridge_port, pvid, mode, untagged_vlans, "
                    "tagged_vlans, mac_count, lag_if_index, lag_attached, collected_at) VALUES "
                    "(:d, :i, :n, :n, :k, 'up', 'up', :bp, :pv, :m, :u, :t, :mc, :l, :a, now())"
                ),
                {"d": a["id"], "i": idx, "n": name, "k": kind, "bp": bp, "pv": pvid, "m": mode,
                 "u": unt, "t": tag, "mc": macs, "l": lag, "a": att},
            )  # fmt: skip
        for mac, idx in (("00:50:56:86:00:01", 1), ("02:00:00:00:86:01", 11),
                         ("02:00:00:00:86:02", 11)):  # fmt: skip
            await s.execute(
                text(
                    "INSERT INTO port_macs (device_id, if_index, mac, vlan, first_seen, "
                    "last_seen) VALUES (:d, :i, :m, 1, now(), now())"
                ),
                {"d": a["id"], "i": idx, "m": mac},
            )
        await s.execute(
            text(
                "INSERT INTO port_inventory_status (device_id, last_run_at, last_ok_at, ports, "
                "sources) VALUES (:d, now(), now(), 4, CAST(:src AS jsonb))"
            ),
            {"d": a["id"], "src": '{"lag": "ok", "lag_mib": "ieee8023"}'},
        )
        await s.commit()
    r = await admin.post(
        "/api/topology/links",
        json={"a_device_id": b["id"], "a_if_index": 49, "a_port": "49",
              "b_device_id": a["id"], "b_if_index": 3, "b_port": "3"},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    await admin.patch(f"/api/devices/{b['id']}", json={"port_inventory": "on"})
    async with rt.sessionmaker() as s:
        await s.execute(
            text(
                "INSERT INTO port_inventory_status (device_id, detected_class) VALUES (:d, "
                "'switch') ON CONFLICT (device_id) DO UPDATE SET detected_class = 'switch'"
            ),
            {"d": b["id"]},
        )
        await s.commit()
    return a["id"], b["id"]


async def test_lag_ports_view(rt: Runtime, admin: httpx.AsyncClient) -> None:
    a, b = await _lag_lab(rt, admin)
    off = (await admin.get(f"/api/devices/{a}/ports")).json()
    p3 = next(p for p in off["ports"] if p["if_index"] == 3)
    assert p3["lag"] is None and p3["vlans_from_lag"] is False and p3["untagged_vlans"] == []
    assert off["status"]["sources"]["lag"] == "skipped" and off["status"]["lag_mib"] is None
    await features(admin, lag=True)
    on = (await admin.get(f"/api/devices/{a}/ports")).json()
    by = {p["if_index"]: p for p in on["ports"]}
    assert on["status"]["sources"]["lag"] == "ok" and on["status"]["lag_mib"] == "ieee8023"
    p3, p4, p1, trk = by[3], by[4], by[1], by[11]
    assert p3["lag"] == {"if_index": 11, "name": "Trk1", "attached": True}
    assert p3["vlans_from_lag"] and p3["mode"] == "trunk"
    assert (p3["untagged_vlans"], p3["tagged_vlans"]) == ([1], [10, 20])
    assert p3["mac_count"] == 2 and p3["is_uplink"] and p3["uplink_reason"] == "neighbor"
    assert p3["neighbor"]["device_id"] == b and p3["macs"] == []
    assert (
        p4["is_uplink"] and p4["uplink_reason"] == "neighbor" and p4["neighbor"]["device_id"] == b
    )
    assert p1["lag"] is None and p1["mac_count"] == 1 and not p1["is_uplink"]
    assert trk["lag_members"] == [3, 4] and trk["is_uplink"] is True
    det = (await admin.get(f"/api/devices/{a}/ports/4")).json()
    assert det["lag"]["name"] == "Trk1" and det["mac_count"] == 2
    found = (await admin.get("/api/search/mac", params={"q": "02:00:00:00:86:01"})).json()
    assert found["items"][0]["port"] == "Trk1" and found["items"][0]["lag_members"] == ["3", "4"]
    assert found["items"][0]["is_uplink"] is True


# ------------------------------------------------------------------ migration 0006
NEW_TABLES = {"traps", "syslog_messages", "event_rules", "event_conditions", "audit_log",
              "api_tokens"}  # fmt: skip


@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_0006_up_down_up(mode: str, rt: Runtime) -> None:
    from alembic import command

    from netmon.db.migrate import _alembic_config

    assert TEST_DATABASE_URL
    if mode == "timescale" and rt.settings.netmon_disable_timescale:
        pytest.skip("suite runs in forced fallback mode")
    name = f"netmon_test_m6_{secrets.token_hex(3)}"
    adm = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await adm.execute(f'CREATE DATABASE "{name}"')
    engine = make_engine(db_url_for(name).replace("postgresql://", "postgresql+asyncpg://"))
    url = db_url_for(name)

    def alembic(sync_conn: Any, rev: str) -> None:
        cfg = _alembic_config()
        cfg.attributes["connection"] = sync_conn
        command.downgrade(cfg, rev)

    try:
        await run_migrations(engine)
        enabled = await timescale.bootstrap(engine, 90, disabled=(mode == "fallback"))
        if mode == "timescale" and not enabled:
            pytest.skip("TimescaleDB not available")
        await timescale.bootstrap_ingest(engine, enabled, 30, 30)
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0009"
            assert await conn.fetchval("SELECT count(*) FROM event_rules WHERE NOT enabled") == 7
            if mode == "timescale":
                ht = await conn.fetchval(
                    "SELECT count(*) FROM timescaledb_information.hypertables "
                    "WHERE hypertable_name IN ('traps', 'syslog_messages')"
                )
                assert ht == 2
            gid = await conn.fetchval("INSERT INTO groups (name) VALUES ('g') RETURNING id")
            did = await conn.fetchval(
                "INSERT INTO devices (group_id, name, host, source_addresses) VALUES "
                "($1, 'd', 'h', '{10.0.0.1}') RETURNING id",
                gid,
            )
            sid = await conn.fetchval(
                "INSERT INTO sensors (device_id, name, type, config, status) VALUES "
                "($1, 'SNMP traps', 'trap', '{}', 'up') RETURNING id",
                did,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO sensors (device_id, name, type, config) VALUES "
                    "($1, 'x', 'trap', '{}')",
                    did,
                )
            rid = await conn.fetchval("SELECT id FROM event_rules LIMIT 1")
            await conn.execute(
                "INSERT INTO event_conditions (sensor_id, source, rule_id, key, status, first_at, "
                "last_at, message) VALUES ($1, 'rule', $2, '5', 'warning', now(), now(), 'm')",
                sid,
                rid,
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO event_conditions (sensor_id, source, key, status, first_at, "
                    "last_at, message) VALUES ($1, 'rule', '6', 'warning', now(), now(), 'm')",
                    sid,
                )
            await conn.execute(
                "INSERT INTO traps (time, source_ip, version, pdu, trap_oid, summary) VALUES "
                "(now(), '10.0.0.1', 'v2c', 'trap', '1.3', 's')"
            )
            await conn.execute(
                "INSERT INTO syslog_messages (time, source_ip, transport, format, facility, "
                "severity, message) VALUES (now(), '10.0.0.1', 'udp', 'raw', 1, 5, 'm')"
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO syslog_messages (time, source_ip, transport, format, facility, "
                    "severity, message) VALUES (now(), '10.0.0.1', 'udp', 'raw', 24, 5, 'm')"
                )
            await conn.execute("INSERT INTO audit_log (actor_type, action) VALUES ('user', 'x')")
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute("UPDATE audit_log SET action = 'y'")
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute("DELETE FROM audit_log")
            await conn.execute(
                "INSERT INTO users (username, source, display_name) VALUES ('j.doe', 'ldap', 'J')"
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("INSERT INTO users (username) VALUES ('nopass')")
            await conn.execute("INSERT INTO users (username, password_hash) VALUES ('admin', 'x')")
            await conn.execute(
                "INSERT INTO events (kind, message) VALUES ('notification_sent', 'm'), "
                "('port_change', 'p')"
            )
            await conn.execute(
                "INSERT INTO switch_ports (device_id, if_index, kind, admin_status, oper_status, "
                "collected_at, lag_if_index, lag_attached) VALUES ($1, 3, 'physical', 'up', "
                "'up', now(), 11, true)",
                did,
            )
        finally:
            await conn.close()
        async with engine.connect() as c:
            await c.run_sync(lambda sc: alembic(sc, "0005"))
            await c.commit()
        conn = await asyncpg.connect(_plain(url))
        try:
            tables = {r["tablename"] for r in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}  # fmt: skip
            assert not (NEW_TABLES & tables)
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0005"
            assert await conn.fetchval("SELECT count(*) FROM users") == 1
            assert await conn.fetchval("SELECT count(*) FROM sensors") == 0
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM events WHERE kind IN ('notification_sent','port_change')"
                )
                == 0
            )
        finally:
            await conn.close()
        await run_migrations(engine)
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0009"
            assert await conn.fetchval("SELECT count(*) FROM event_rules") == 7
        finally:
            await conn.close()
    finally:
        await engine.dispose()
        await adm.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await adm.close()


async def test_retention_fallback_deletes_old_rows(rt: Runtime, admin: httpx.AsyncClient) -> None:
    from sqlalchemy import text

    from netmon.services import retention

    async with rt.sessionmaker() as s:
        await s.execute(
            text(
                "INSERT INTO syslog_messages (time, source_ip, transport, format, facility, "
                "severity, message) VALUES (now() - interval '40 days', '10.0.0.1', 'udp', "
                "'raw', 1, 5, 'old'), (now(), '10.0.0.1', 'udp', 'raw', 1, 5, 'new')"
            )
        )
        await s.commit()
    n = await retention.delete_old_ingest(rt, "syslog_messages", 30)
    assert n == 1
    await asyncio.sleep(0)
