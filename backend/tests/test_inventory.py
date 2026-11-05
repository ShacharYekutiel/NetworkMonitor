from __future__ import annotations

import httpx

from tests.conftest import default_group_id, make_device, make_sensor, requires_db

pytestmark = requires_db

GROUP_KEYS = {
    "id",
    "parent_id",
    "name",
    "description",
    "snmp_credentials",
    "sort_order",
    "status",
    "status_counts",
    "created_at",
    "updated_at",
    "dependency",
}
DEVICE_KEYS = {
    "id",
    "group_id",
    "name",
    "host",
    "description",
    "enabled",
    "snmp_credentials",
    "effective_snmp_source",
    "effective_snmp_group_id",
    "sort_order",
    "status",
    "status_counts",
    "created_at",
    "updated_at",
    "dependency",
    "effective_dependency",
    "in_maintenance",
    "maintenance",
    "identity",
}
SENSOR_KEYS = {
    "id",
    "device_id",
    "device_name",
    "group_id",
    "name",
    "type",
    "config",
    "interval_s",
    "enabled",
    "status",
    "status_message",
    "last_check_at",
    "last_status_change_at",
    "primary_channel",
    "channels",
    "sort_order",
    "created_at",
    "updated_at",
    "acknowledgement",
    "in_maintenance",
}


async def test_groups_crud_cycle_conflict(admin: httpx.AsyncClient) -> None:
    root = await default_group_id(admin)
    r = await admin.post("/api/groups", json={"name": "Branch TLV", "parent_id": root})
    assert r.status_code == 201, r.text
    g1 = r.json()
    assert set(g1) == GROUP_KEYS
    assert g1["status"] == "unknown"
    assert g1["status_counts"] == {
        "up": 0,
        "warning": 0,
        "down": 0,
        "paused": 0,
        "unknown": 0,
        "unreachable": 0,
        "total": 0,
    }
    assert g1["created_at"].endswith("Z")
    r = await admin.post("/api/groups", json={"name": "branch tlv", "parent_id": root})
    assert r.status_code == 409 and r.json()["error"]["details"] == [{"field": "name"}]
    g2 = (await admin.post("/api/groups", json={"name": "Floor 1", "parent_id": g1["id"]})).json()
    r = await admin.patch(f"/api/groups/{g1['id']}", json={"parent_id": g2["id"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "group_cycle"
    r = await admin.patch(f"/api/groups/{g1['id']}", json={"parent_id": g1["id"]})
    assert r.status_code == 400
    r = await admin.post("/api/groups", json={"name": "Orphan", "parent_id": 9999})
    assert r.status_code == 422
    r = await admin.get("/api/groups", params={"parent_id": g1["id"]})
    assert [g["name"] for g in r.json()["items"]] == ["Floor 1"]
    r = await admin.get("/api/groups", params={"parent_id": "root"})
    assert r.json()["total"] == 1
    r = await admin.patch(f"/api/groups/{g2['id']}", json={"name": "Floor 2", "description": "x"})
    assert r.json()["name"] == "Floor 2" and r.json()["description"] == "x"
    dev = await make_device(admin, group_id=g2["id"], name="sw")
    await make_sensor(admin, dev["id"], "ping")
    assert (await admin.delete(f"/api/groups/{g1['id']}")).status_code == 204
    assert (await admin.get(f"/api/groups/{g2['id']}")).status_code == 404
    assert (await admin.get(f"/api/devices/{dev['id']}")).status_code == 404
    assert (await admin.get("/api/sensors")).json()["total"] == 0


async def test_snmp_credentials_inheritance_and_secrets(admin: httpx.AsyncClient) -> None:
    root = await default_group_id(admin)
    creds = {
        "version": "v3",
        "v3_username": "mon",
        "v3_security_level": "authPriv",
        "v3_auth_protocol": "SHA256",
        "v3_auth_key": "authkey123",
        "v3_priv_protocol": "AES128",
        "v3_priv_key": "privkey123",
    }
    r = await admin.patch(f"/api/groups/{root}", json={"snmp_credentials": creds})
    assert r.status_code == 200, r.text
    sc = r.json()["snmp_credentials"]
    assert sc["v3_auth_key"] is None and sc["v3_auth_key_set"] is True
    assert sc["v3_priv_key"] is None and sc["v3_priv_key_set"] is True
    assert sc["port"] == 161 and sc["timeout_s"] == 2
    dev = await make_device(admin)
    assert dev["snmp_credentials"] is None
    assert dev["effective_snmp_source"] == "group" and dev["effective_snmp_group_id"] == root
    # keep secrets when omitted / null, clear with ""
    r = await admin.patch(
        f"/api/groups/{root}",
        json={
            "snmp_credentials": {
                "version": "v3",
                "v3_username": "mon",
                "v3_security_level": "authPriv",
                "v3_auth_key": None,
                "v3_auth_key_set": False,
            }
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["snmp_credentials"]["v3_auth_key_set"] is True
    assert r.json()["snmp_credentials"]["v3_priv_key_set"] is True
    r = await admin.patch(
        f"/api/groups/{root}",
        json={
            "snmp_credentials": {
                "version": "v3",
                "v3_username": "mon",
                "v3_security_level": "authNoPriv",
                "v3_priv_key": "",
            }
        },
    )
    assert r.json()["snmp_credentials"]["v3_priv_key_set"] is False
    r = await admin.patch(
        f"/api/groups/{root}",
        json={
            "snmp_credentials": {
                "version": "v3",
                "v3_username": "mon",
                "v3_security_level": "authPriv",
            }
        },
    )
    assert r.status_code == 422  # priv key missing
    r = await admin.patch(
        f"/api/devices/{dev['id']}", json={"snmp_credentials": {"community": "private"}}
    )
    assert r.json()["effective_snmp_source"] == "device"
    assert r.json()["snmp_credentials"]["community"] is None  # write-only
    assert r.json()["snmp_credentials"]["community_set"] is True
    assert r.json()["snmp_credentials"]["version"] == "v2c"
    r = await admin.patch(f"/api/groups/{root}", json={"snmp_credentials": None})
    assert r.json()["snmp_credentials"] is None
    r = await admin.patch(f"/api/devices/{dev['id']}", json={"snmp_credentials": None})
    assert r.json()["effective_snmp_source"] == "default"


async def test_devices_crud_and_auto_sensors(admin: httpx.AsyncClient) -> None:
    root = await default_group_id(admin)
    r = await admin.post(
        "/api/devices",
        json={
            "group_id": root,
            "name": "core-sw",
            "host": "10.0.0.2",
            "auto_sensors": ["ping", "snmp_system"],
        },
    )
    assert r.status_code == 201, r.text
    dev = r.json()
    assert set(dev) == DEVICE_KEYS
    assert dev["status_counts"]["total"] == 2 and dev["status"] == "unknown"
    r = await admin.post(
        "/api/devices", json={"group_id": root, "name": "x", "host": "h", "auto_sensors": ["http"]}
    )
    assert r.status_code == 422
    r = await admin.post("/api/devices", json={"group_id": root, "name": "x", "host": "bad host"})
    assert r.status_code == 422
    await make_device(admin, name="web-1", host="192.168.1.10")
    r = await admin.get("/api/devices", params={"q": "192.168"})
    assert [d["name"] for d in r.json()["items"]] == ["web-1"]
    r = await admin.get("/api/devices", params={"group_id": root})
    assert [d["name"] for d in r.json()["items"]] == ["core-sw", "web-1"]
    r = await admin.get("/api/devices", params={"status": "unknown", "page_size": 1})
    assert r.json()["total"] == 2 and len(r.json()["items"]) == 1
    sensors = (await admin.get("/api/sensors", params={"device_id": dev["id"]})).json()["items"]
    assert sorted(s["type"] for s in sensors) == ["ping", "snmp_system"]

    # pause the device -> sensors paused, events written
    r = await admin.patch(f"/api/devices/{dev['id']}", json={"enabled": False})
    assert r.json()["status"] == "paused" and r.json()["status_counts"]["paused"] == 2
    events = (await admin.get("/api/events", params={"device_id": dev["id"]})).json()["items"]
    assert {(e["old_status"], e["new_status"]) for e in events} == {("unknown", "paused")}
    r = await admin.patch(f"/api/devices/{dev['id']}", json={"enabled": True})
    assert r.json()["status"] == "unknown"
    assert (await admin.delete(f"/api/devices/{dev['id']}")).status_code == 204
    # events keep snapshots
    events = (await admin.get("/api/events", params={"kind": "status_change"})).json()["items"]
    assert events and events[0]["device_name"] == "core-sw" and events[0]["device_id"] is None


async def test_sensor_crud_validation(admin: httpx.AsyncClient) -> None:
    dev = await make_device(admin, name="web")
    r = await admin.post("/api/sensors", json={"device_id": dev["id"], "type": "bogus"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_sensor_type"
    r = await admin.post(
        "/api/sensors", json={"device_id": dev["id"], "type": "http", "config": {"keyword": "x"}}
    )
    assert r.status_code == 422
    assert ["body", "config", "url"] in [d["loc"] for d in r.json()["error"]["details"]]
    r = await admin.post(
        "/api/sensors", json={"device_id": dev["id"], "type": "ping", "config": {"nope": 1}}
    )
    assert r.status_code == 422
    r = await admin.post(
        "/api/sensors", json={"device_id": dev["id"], "type": "ping", "config": {"count": 99}}
    )
    assert r.status_code == 422
    r = await admin.post(
        "/api/sensors", json={"device_id": dev["id"], "type": "ping", "interval_s": 5}
    )
    assert r.status_code == 422
    r = await admin.post(
        "/api/sensors",
        json={
            "device_id": dev["id"],
            "name": "Web",
            "type": "http",
            "config": {"url": "https://{host}/", "keyword": "Welcome"},
            "interval_s": 60,
            "channels": [{"key": "response_time_ms", "warning_max": 800, "down_max": 3000}],
        },
    )
    assert r.status_code == 201, r.text
    s = r.json()
    assert set(s) == SENSOR_KEYS
    assert s["config"] == {
        "url": "https://{host}/",
        "method": "GET",
        "timeout_s": 10,
        "expected_status": None,
        "keyword": "Welcome",
        "keyword_must_exist": True,
        "verify_tls": True,
        "follow_redirects": True,
        "headers": {},
    }
    assert [c["key"] for c in s["channels"]] == ["response_time_ms", "status_code", "ssl_days_left"]
    rt_ch = s["channels"][0]
    assert rt_ch["warning_max"] == 800 and rt_ch["down_max"] == 3000
    assert s["channels"][2]["warning_min"] == 14 and s["channels"][2]["down_min"] == 3
    assert s["primary_channel"] == "response_time_ms" and s["device_name"] == "web"
    r = await admin.post(
        "/api/sensors", json={"device_id": dev["id"], "type": "ping", "channels": [{"key": "nope"}]}
    )
    assert r.status_code == 422

    sid = s["id"]
    r = await admin.patch(f"/api/sensors/{sid}", json={"type": "ping"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_sensor_type"
    r = await admin.patch(
        f"/api/sensors/{sid}",
        json={
            "config": {"timeout_s": 5},
            "channels": [{"key": "response_time_ms", "warning_max": None}],
        },
    )
    assert r.status_code == 200, r.text
    s2 = r.json()
    assert s2["config"]["timeout_s"] == 5 and s2["config"]["keyword"] == "Welcome"
    assert s2["channels"][0]["warning_max"] is None and s2["channels"][0]["down_max"] == 3000
    r = await admin.patch(f"/api/sensors/{sid}", json={"enabled": False})
    assert r.json()["status"] == "paused"
    r = await admin.patch(f"/api/sensors/{sid}", json={"enabled": True})
    assert r.json()["status"] == "unknown"
    ev = (await admin.get(f"/api/sensors/{sid}/events")).json()["items"]
    assert [(e["old_status"], e["new_status"]) for e in ev] == [
        ("paused", "unknown"),
        ("unknown", "paused"),
    ]
    # interface name default
    r = await admin.post(
        "/api/sensors",
        json={
            "device_id": dev["id"],
            "type": "snmp_interface",
            "config": {"if_index": 2, "if_name": "Gi0/1"},
        },
    )
    assert r.json()["name"] == "Gi0/1"
    r = await admin.get("/api/sensors", params={"type": "snmp_interface"})
    assert r.json()["total"] == 1
    assert (await admin.delete(f"/api/sensors/{sid}")).status_code == 204
    assert (await admin.get(f"/api/sensors/{sid}")).status_code == 404


async def test_sensor_types_endpoint(viewer: httpx.AsyncClient) -> None:
    r = await viewer.get("/api/sensor-types")
    items = r.json()["items"]
    assert [i["type"] for i in items] == [
        "ping",
        "snmp_system",
        "snmp_cpu",
        "snmp_memory",
        "snmp_interface",
        "http",
        "tcp_port",
        "dns",
        "snmp_custom",
        "snmp_disk",
    ]
    ping = items[0]
    assert ping == {
        "type": "ping",
        "requires_snmp": False,
        "primary_channel": "latency_ms",
        "config_fields": [
            {
                "key": "count",
                "type": "int",
                "required": False,
                "default": 4,
                "min": 1,
                "max": 20,
                "options": None,
            },
            {
                "key": "timeout_ms",
                "type": "int",
                "required": False,
                "default": 1000,
                "min": 100,
                "max": 10000,
                "options": None,
            },
            {
                "key": "packet_size",
                "type": "int",
                "required": False,
                "default": 56,
                "min": 16,
                "max": 1472,
                "options": None,
            },
        ],
        "channels": [
            {
                "key": "latency_ms",
                "unit": "ms",
                "warning_min": None,
                "warning_max": 100,
                "down_min": None,
                "down_max": 500,
            },
            {
                "key": "packet_loss_pct",
                "unit": "%",
                "warning_min": None,
                "warning_max": 10,
                "down_min": None,
                "down_max": 50,
            },
        ],
    }


async def test_tree(admin: httpx.AsyncClient) -> None:
    root = await default_group_id(admin)
    sub = (await admin.post("/api/groups", json={"name": "Sub", "parent_id": root})).json()
    dev = await make_device(admin, group_id=sub["id"], name="b-dev")
    await make_device(admin, group_id=root, name="a-dev")
    await make_sensor(admin, dev["id"], "ping", name="Ping")
    r = await admin.get("/api/tree")
    tree = r.json()
    assert len(tree["groups"]) == 1
    g = tree["groups"][0]
    assert g["name"] == "Default" and g["status_counts"]["total"] == 1
    assert [d["name"] for d in g["devices"]] == ["a-dev"]
    child = g["children"][0]
    assert child["name"] == "Sub" and child["parent_id"] == root
    sensor = child["devices"][0]["sensors"][0]
    assert sensor == {
        "id": sensor["id"],
        "name": "Ping",
        "type": "ping",
        "enabled": True,
        "status": "unknown",
        "status_message": "",
        "primary_channel": "latency_ms",
        "primary_value": None,
        "primary_unit": "ms",
        "last_check_at": None,
        "acknowledged": False,
    }


async def test_settings(admin: httpx.AsyncClient, rt) -> None:  # type: ignore[no-untyped-def]
    r = await admin.get("/api/settings")
    s = r.json()
    assert s["smtp"]["password"] is None and s["smtp"]["password_set"] is False
    assert s["retention_days"] == 90 and s["default_interval_s"] == 60
    r = await admin.put(
        "/api/settings",
        json={
            "smtp": {"host": "smtp.example.com", "password": "pw", "username": "u"},
            "retention_days": 30,
            "default_interval_s": 120,
            "public_base_url": "http://netmon.local:8080/",
        },
    )
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["smtp"]["host"] == "smtp.example.com" and s["smtp"]["port"] == 587
    assert s["smtp"]["password"] is None and s["smtp"]["password_set"] is True
    assert s["public_base_url"] == "http://netmon.local:8080"
    r = await admin.put("/api/settings", json={"smtp": {"port": 25}})
    assert (
        r.json()["smtp"]["password_set"] is True and r.json()["smtp"]["host"] == "smtp.example.com"
    )
    r = await admin.put("/api/settings", json={"smtp": {"password": ""}})
    assert r.json()["smtp"]["password_set"] is False
    r = await admin.put("/api/settings", json={"retention_days": 0})
    assert r.status_code == 422
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "ping")
    assert s["interval_s"] == 120
    if rt.timescale_enabled:
        async with rt.engine.connect() as conn:
            from sqlalchemy import text

            row = (
                await conn.execute(
                    text(
                        "SELECT config->>'drop_after' FROM timescaledb_information.jobs "
                        "WHERE proc_name = 'policy_retention' AND hypertable_name = 'measurements'"
                    )
                )
            ).scalar()
        assert row is not None and "30 days" in row
