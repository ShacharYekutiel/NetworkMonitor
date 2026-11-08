"""[2b-3] Audit log: written with the change (same transaction), secrets redacted, OFF writes
nothing, admin list/filters, CSV export, append-only trigger, retention."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

import asyncpg
import httpx
import pytest

from tests.conftest import ADMIN, _plain, make_device, requires_db

pytestmark = requires_db


async def _rows(rt: Any, where: str = "TRUE", *args: Any) -> list[Any]:
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        rows = await conn.fetch(f"SELECT * FROM audit_log WHERE {where} ORDER BY id", *args)
    finally:
        await conn.close()
    return rows


def _changes(row: Any) -> dict:
    c = row["changes"]
    return json.loads(c) if isinstance(c, str) else c


@pytest.fixture
async def audit_on(admin: httpx.AsyncClient, feats: Any) -> httpx.AsyncClient:
    feats(audit=True)
    return admin


async def test_off_writes_nothing_and_409(admin: httpx.AsyncClient, feats: Any, rt: Any) -> None:
    feats(audit=False)
    await make_device(admin, name="d-off", snmp_credentials={"community": "s3cret"})
    await admin.post("/api/auth/login", json={"username": "admin", "password": "wrong-pass"})
    assert await _rows(rt) == []
    r = await admin.get("/api/audit")
    assert r.status_code == 409
    assert r.json()["error"] == {
        "code": "feature_disabled",
        "message": r.json()["error"]["message"],
        "details": [{"feature": "audit"}],
    }
    assert (await admin.get("/api/audit/export.csv")).status_code == 409


async def test_device_lifecycle_redacted(audit_on: httpx.AsyncClient, rt: Any) -> None:
    admin = audit_on
    d = await make_device(
        admin, name="core-sw", host="10.0.0.2", snmp_credentials={"community": "s3cret-comm"}
    )
    r = await admin.patch(
        f"/api/devices/{d['id']}",
        json={"name": "core-sw2", "snmp_credentials": {"community": "other-secret"}},
    )
    assert r.status_code == 200
    assert (
        await admin.patch(f"/api/devices/{d['id']}", json={"name": "core-sw2"})
    ).status_code == 200
    assert (await admin.delete(f"/api/devices/{d['id']}")).status_code == 204
    rows = await _rows(rt, "entity_type = 'device'")
    assert [r["action"] for r in rows] == ["device.create", "device.update", "device.delete"]
    for row in rows:
        assert row["entity_id"] == d["id"] and row["device_id"] == d["id"]
        assert row["actor_type"] == "user" and row["actor_name"] == "admin"
        assert row["ip"]
    created, updated, deleted = (_changes(r) for r in rows)
    assert created["name"] == [None, "core-sw"]
    assert created["snmp_credentials.community"] == [None, "***"]
    assert updated == {
        "name": ["core-sw", "core-sw2"],
        "snmp_credentials.community": ["***", "***"],
    }
    assert "snmp_credentials.community" not in deleted and deleted["name"] == ["core-sw2", None]
    raw = json.dumps([_changes(r) for r in rows])
    assert "s3cret-comm" not in raw and "other-secret" not in raw


async def test_groups_sensors_ack_rules_channels_maintenance(
    audit_on: httpx.AsyncClient, rt: Any
) -> None:
    admin = audit_on
    r = await admin.post("/api/groups", json={"name": "Branch"})
    gid = r.json()["id"]
    await admin.patch(f"/api/groups/{gid}", json={"description": "x"})
    d = await make_device(admin, name="dev1")
    r = await admin.post(
        "/api/sensors", json={"device_id": d["id"], "type": "ping", "config": {}, "name": "Ping"}
    )
    sid = r.json()["id"]
    await admin.patch(f"/api/sensors/{sid}", json={"interval_s": 120})
    # acknowledge needs a down/warning sensor
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        await conn.execute("UPDATE sensors SET status = 'down' WHERE id = $1", sid)
    finally:
        await conn.close()
    r = await admin.post(f"/api/sensors/{sid}/acknowledge", json={"comment": "on it"})
    assert r.status_code == 200, r.text
    assert (await admin.delete(f"/api/sensors/{sid}/acknowledge")).status_code == 200
    r = await admin.post(
        "/api/notification-channels",
        json={
            "name": "Ops",
            "type": "telegram",
            "config": {"bot_token": "123456:" + "A" * 30, "chat_id": "-100123"},
        },
    )
    cid = r.json()["id"]
    r = await admin.post(
        "/api/alert-rules",
        json={"name": "All down", "on_statuses": ["down"], "channel_ids": [cid]},
    )
    rid = r.json()["id"]
    await admin.patch(f"/api/alert-rules/{rid}", json={"delay_s": 60})
    await admin.delete(f"/api/alert-rules/{rid}")
    await admin.delete(f"/api/notification-channels/{cid}")
    r = await admin.post(
        "/api/maintenance-windows",
        json={
            "name": "Night",
            "scope": "device",
            "device_id": d["id"],
            "kind": "weekly",
            "weekdays": [1],
            "start_time": "02:00",
            "duration_min": 60,
        },
    )
    assert r.status_code == 201, r.text
    wid = r.json()["id"]
    await admin.patch(f"/api/maintenance-windows/{wid}", json={"comment": "c"})
    await admin.delete(f"/api/maintenance-windows/{wid}")
    await admin.delete(f"/api/sensors/{sid}")
    await admin.delete(f"/api/groups/{gid}")
    actions = [r["action"] for r in await _rows(rt)]
    for a in (
        "group.create",
        "group.update",
        "group.delete",
        "device.create",
        "sensor.create",
        "sensor.update",
        "sensor.acknowledge",
        "sensor.unacknowledge",
        "sensor.delete",
        "notification_channel.create",
        "notification_channel.delete",
        "alert_rule.create",
        "alert_rule.update",
        "alert_rule.delete",
        "maintenance_window.create",
        "maintenance_window.update",
        "maintenance_window.delete",
    ):
        assert a in actions, a
    ack = (await _rows(rt, "action = 'sensor.acknowledge'"))[0]
    assert ack["device_id"] == d["id"] and _changes(ack)["comment"] == [None, "on it"]
    ch = (await _rows(rt, "action = 'notification_channel.create'"))[0]
    assert _changes(ch)["config.bot_token"] == [None, "***"]
    assert "AAAA" not in json.dumps(_changes(ch))


async def test_settings_and_users_and_logins(
    app: Any, audit_on: httpx.AsyncClient, rt: Any
) -> None:
    admin = audit_on
    r = await admin.put(
        "/api/settings",
        json={"smtp": {"host": "mail.x", "password": "smtp-secret"}, "retention_days": 30},
    )
    assert r.status_code == 200
    row = (await _rows(rt, "action = 'settings.update'"))[-1]
    ch = _changes(row)
    assert row["entity_type"] is None
    assert ch["smtp.password"] == [None, "***"] and ch["smtp.host"] == ["", "mail.x"]
    assert ch["retention_days"] == [90, 30]
    assert "smtp-secret" not in json.dumps(ch)
    # no-op save: no record
    n = len(await _rows(rt))
    await admin.put("/api/settings", json={"retention_days": 30})
    assert len(await _rows(rt)) == n
    # users
    r = await admin.post("/api/users", json={"username": "carol", "password": "carol-pass-1"})
    uid = r.json()["id"]
    await admin.patch(f"/api/users/{uid}", json={"role": "admin", "password": "carol-pass-2"})
    await admin.delete(f"/api/users/{uid}")
    users = await _rows(rt, "entity_type = 'user' AND action LIKE 'user.%'")
    assert [r["action"] for r in users] == ["user.create", "user.update", "user.delete"]
    assert _changes(users[1]) == {"role": ["viewer", "admin"], "password": ["***", "***"]}
    # logins / logouts
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "nope-nope"})
        assert r.status_code == 401
        r = await c.post("/api/auth/login", json={"username": "ghost", "password": "nope-nope"})
        r = await c.post("/api/auth/login", json={**ADMIN})
        assert r.status_code == 200
        assert (await c.post("/api/auth/logout")).status_code == 204
    failed = await _rows(rt, "action = 'auth.login_failed'")
    assert [(r["actor_name"], r["reason"], r["actor_type"]) for r in failed] == [
        ("admin", "bad_password", "anonymous"),
        ("ghost", "unknown_user", "anonymous"),
    ]
    ok = await _rows(rt, "action = 'auth.login'")
    assert ok[-1]["source"] == "local" and ok[-1]["actor_user_id"] is not None
    assert (await _rows(rt, "action = 'auth.logout'"))[-1]["actor_name"] == "admin"


async def test_same_transaction(audit_on: httpx.AsyncClient, rt: Any) -> None:
    admin = audit_on
    assert (await admin.post("/api/groups", json={"name": "Dup"})).status_code == 201
    r = await admin.post("/api/groups", json={"name": "Dup"})
    assert r.status_code == 409
    assert len(await _rows(rt, "action = 'group.create'")) == 1
    # a request that fails after the record was added leaves no record
    r = await admin.post(
        "/api/notification-channels",
        json={"name": "X", "type": "webhook", "config": {"url": "https://example.com/h"}},
    )
    assert r.status_code == 201
    r = await admin.post(
        "/api/notification-channels",
        json={"name": "x", "type": "webhook", "config": {"url": "https://example.com/h"}},
    )
    assert r.status_code == 409
    assert len(await _rows(rt, "action = 'notification_channel.create'")) == 1


async def test_feature_toggle_records(admin: httpx.AsyncClient, feats: Any, rt: Any) -> None:
    from netmon.services import audit as audit_svc

    feats(audit=False)
    ctx = audit_svc.AuditCtx(actor_type="user", actor_name="admin", enabled=False)
    async with rt.sessionmaker() as s:
        await audit_svc.record_settings(
            s,
            ctx,
            {"feature_audit_enabled": False, "feature_traps_enabled": False},
            {"feature_audit_enabled": True, "feature_traps_enabled": True},
        )
        await s.commit()
    rows = await _rows(rt)
    # turning the audit log on is recorded; the rest of that change happened while it was off
    assert [(r["action"], r["entity_name"]) for r in rows] == [("feature.enable", "audit")]
    ctx = audit_svc.AuditCtx(actor_type="user", actor_name="admin", enabled=True)
    async with rt.sessionmaker() as s:
        await audit_svc.record_settings(
            s,
            ctx,
            {"feature_audit_enabled": True, "feature_traps_enabled": True, "x": 1},
            {"feature_audit_enabled": False, "feature_traps_enabled": False, "x": 2},
        )
        await s.commit()
    rows = await _rows(rt)
    assert [(r["action"], r["entity_name"]) for r in rows[1:]] == [
        ("feature.disable", "audit"),
        ("feature.disable", "traps"),
        ("settings.update", None),
    ]


async def test_list_filters_and_paging(audit_on: httpx.AsyncClient, rt: Any) -> None:
    admin = audit_on
    d1 = await make_device(admin, name="alpha")
    await make_device(admin, name="beta")
    await admin.post("/api/groups", json={"name": "G1"})
    r = await admin.get("/api/audit")
    body = r.json()
    assert body["total"] == 3 and [i["action"] for i in body["items"]] == [
        "group.create",
        "device.create",
        "device.create",
    ]
    item = body["items"][-1]
    assert set(item) == {
        "id",
        "time",
        "actor_type",
        "actor_user_id",
        "actor_name",
        "token_id",
        "ip",
        "action",
        "entity_type",
        "entity_id",
        "entity_name",
        "device_id",
        "source",
        "reason",
        "changes",
    }
    assert (await admin.get("/api/audit", params={"action": "device."})).json()["total"] == 2
    assert (await admin.get("/api/audit", params={"action": "device"})).json()["total"] == 0
    assert (await admin.get("/api/audit", params={"device_id": d1["id"]})).json()["total"] == 1
    q = {"entity_type": "device", "entity_id": d1["id"]}
    assert (await admin.get("/api/audit", params=q)).json()["items"][0]["entity_name"] == "alpha"
    assert (await admin.get("/api/audit", params={"actor": "ADM"})).json()["total"] == 3
    assert (await admin.get("/api/audit", params={"actor": "%"})).json()["total"] == 0
    p = (await admin.get("/api/audit", params={"page_size": 2, "page": 2})).json()
    assert p["total"] == 3 and len(p["items"]) == 1
    r = await admin.get("/api/audit", params={"since": "2030-01-01T00:00:00Z"})
    assert r.json()["total"] == 0


async def test_viewer_forbidden(viewer: httpx.AsyncClient, feats: Any) -> None:
    feats(audit=True)
    assert (await viewer.get("/api/audit")).status_code == 403
    assert (await viewer.get("/api/audit/export.csv")).status_code == 403


async def test_csv_export(
    audit_on: httpx.AsyncClient, rt: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = audit_on
    await make_device(admin, name='=evil, "quoted"')
    await make_device(admin, name="b")
    r = await admin.get("/api/audit/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "charset=utf-8" in r.headers["content-type"]
    disp = r.headers["content-disposition"]
    assert disp.startswith('attachment; filename="netmon-audit-') and disp.endswith('.csv"')
    assert "x-netmon-truncated" not in r.headers
    raw = r.content
    assert raw.startswith("﻿".encode())
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    assert rows[0] == [
        "time",
        "actor_type",
        "actor_name",
        "ip",
        "action",
        "entity_type",
        "entity_id",
        "entity_name",
        "source",
        "reason",
        "changes",
    ]
    assert len(rows) == 3
    assert rows[2][7] == '\'=evil, "quoted"'  # formula guard, CSV quoting
    assert json.loads(rows[1][10])["name"] == [None, "b"]
    from netmon.services import audit as audit_svc

    monkeypatch.setattr(audit_svc, "EXPORT_MAX_ROWS", 1)
    r = await admin.get("/api/audit/export.csv", params={"action": "device."})
    assert r.headers["x-netmon-truncated"] == "true"
    assert len(list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))) == 2


async def test_append_only_and_retention(audit_on: httpx.AsyncClient, rt: Any) -> None:
    from netmon.services import audit as audit_svc

    await make_device(audit_on, name="x")
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute("UPDATE audit_log SET action = 'tampered'")
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute("DELETE FROM audit_log")
        # the retention job may delete old rows (only with the purge flag)
        async with conn.transaction():
            await conn.execute("SET LOCAL netmon.audit_purge = 'on'")
            await conn.execute("DELETE FROM audit_log")  # noqa: S608 - test setup
        await conn.execute(
            "INSERT INTO audit_log (time, actor_type, action) VALUES "
            "(now() - interval '400 days', 'system', 'old.one'), "
            "(now() - interval '10 days', 'system', 'new.one')"
        )
    finally:
        await conn.close()
    assert await audit_svc.purge_old(rt, 365) == 1
    assert [r["action"] for r in await _rows(rt)] == ["new.one"]
    # the hourly retention run includes the audit log (also while the feature is OFF)
    from netmon.services import retention

    out = await retention.run_once(rt)
    assert out["audit"] == 0 and "api_tokens" in out
