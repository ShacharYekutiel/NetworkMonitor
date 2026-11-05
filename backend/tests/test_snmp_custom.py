"""[2a] snmp_custom and snmp_disk sensors: plugins against a real snmpsim agent, and the API."""

from __future__ import annotations

import socket
from collections.abc import Iterator

import httpx
import pytest

from netmon.poller.checks import snmp_custom, snmp_disk
from netmon.poller.checks.snmp_custom import counter_rate, parse_numeric
from netmon.poller.checks.snmp_disk import match_storage
from netmon.poller.sensor_types import validate_config
from netmon.poller.types import CheckContext
from tests.conftest import make_device, requires_db
from tests.snmpsim_agents import FIXED, RAM, VIRTUAL, host_resources, start_agent, system

E = "1.3.6.1.4.1.99999"


def _records(c32: int, c64: int) -> list:
    return (
        system("Linux custom-agent", "1.3.6.1.4.1.8072.3.2.10", "custom-agent")
        + [
            (f"{E}.1.0", 2, "42"),
            (f"{E}.2.0", 4, "0.15"),
            (f"{E}.3.0", 4, "abc"),
            (f"{E}.4.0", 65, str(c32)),
            (f"{E}.5.0", 70, str(c64)),
            (f"{E}.6.0", 4, "running"),
            (f"{E}.7.0", 2, "40"),
            (f"{E}.8.0", 2, "200"),
            (f"{E}.9.0", 2, "-3"),
            (f"{E}.10.0", 66, "7"),
        ]
        + host_resources(
            [
                (1, RAM, "Physical memory", 1024, 1000, 500),
                (2, VIRTUAL, "/", 1024, 1000, 999),  # same descr, but virtual memory: ignored
                (31, FIXED, "/", 4096, 1000, 250),
                (32, FIXED, "/boot", 1024, 100, 50),
                (33, FIXED, "C:\\ Label:System  Serial Number 1a2b3c", 4096, 2000, 1000),
                (34, FIXED, "/big", 65536, -100, 1000),
                (35, FIXED, "/zero", 4096, 0, 0),
            ]
        )
    )


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def agent() -> Iterator[int]:
    port = _free_udp_port()
    a = start_agent(
        "127.0.0.1",
        port,
        {"public": _records(4294967000, 10_000), "later": _records(1000, 5_000)},
    )
    if a is None:
        pytest.skip("snmpsim not available")
    yield port
    a.stop()


def ctx(port: int, type_: str, cfg: dict, community: str = "public", **kw) -> CheckContext:  # type: ignore[no-untyped-def]
    creds = {"version": "v2c", "port": port, "timeout_s": 1, "retries": 0, "community": community}
    return CheckContext(1, type_, validate_config(type_, cfg), "127.0.0.1", creds, **kw)


def test_parse_numeric_and_rates() -> None:
    assert parse_numeric(5) == 5.0
    assert parse_numeric(b"0.15\x00 ") == 0.15
    assert parse_numeric(b"  -12.5 load") == -12.5
    assert parse_numeric(b"n/a") is None
    # first poll: baseline only
    v, st = counter_rate({}, 100.0, 50, 32, 1.0)
    assert v is None and st == {"t": 100.0, "v": 50, "bits": 32}
    v, _ = counter_rate(st, 110.0, 150, 32, 8.0)
    assert v == 80.0
    v, _ = counter_rate({"t": 0.0, "v": 2**32 - 10, "bits": 32}, 10.0, 10, 32, 1.0)
    assert v == 2.0  # 32-bit wrap
    v, st = counter_rate({"t": 0.0, "v": 1000, "bits": 64}, 10.0, 10, 64, 1.0)
    assert v is None and st["v"] == 10  # 64-bit reset -> new baseline
    v, _ = counter_rate({"t": 0.0, "v": 1, "bits": 64}, 10.0, 11, 32, 1.0)
    assert v is None  # bits changed -> new baseline


def test_match_storage() -> None:
    descrs = {"1": "Physical memory", "2": "/", "31": "/", "32": "/boot", "33": "C:\\ Label:X"}
    types = {"1": RAM, "2": VIRTUAL, "31": FIXED, "32": FIXED, "33": FIXED}
    assert match_storage("/", descrs, types) == "31"
    assert match_storage("/bo", descrs, types) is None  # no prefix match outside drives
    assert match_storage("C:\\", descrs, types) == "33"
    assert match_storage("c:", descrs, types) is None
    assert match_storage("Physical memory", descrs, types) is None  # RAM never matches


def test_snmp_custom_validation() -> None:
    from netmon.api.errors import ApiError

    cfg = validate_config("snmp_custom", {"oid": ".1.3.6.1.2.1.1.3.0"})
    assert cfg == {
        "oid": "1.3.6.1.2.1.1.3.0",
        "value_type": "gauge",
        "unit": "",
        "multiplier": 1.0,
        "label": "",
        "percent_of_oid": None,
        "expected": None,
        "match": "equals",
        "value_map": {},
        "value_map_default": "warning",
    }
    for bad in (
        {"oid": "sysUpTime.0"},
        {"oid": "1.3.6.1", "multiplier": 0},
        {"oid": "1.3.6.1", "value_type": "counter", "percent_of_oid": "1.3.6.2"},
        {"oid": "1.3.6.1", "unit": "x" * 17},
        {"oid": "1.3.6.1", "label": "x" * 65},
        {"oid": "1.3.6.1", "multiplier": 2e12},
        {"oid": "1.3.6.1", "multiplier": True},
        {"oid": "1.3.6.1", "percent_of_oid": "abc"},
        {"oid": "1.3.6.1", "nope": 1},
    ):
        with pytest.raises(ApiError):
            validate_config("snmp_custom", bad)
    assert validate_config("snmp_custom", {"oid": "1.3", "multiplier": 8})["multiplier"] == 8.0
    with pytest.raises(ApiError):
        validate_config("snmp_disk", {})
    with pytest.raises(ApiError):
        validate_config("snmp_disk", {"storage_descr": "x" * 256})


async def test_snmp_custom_gauge(agent: int) -> None:
    r = await snmp_custom.run(ctx(agent, "snmp_custom", {"oid": f"{E}.1.0", "multiplier": 0.5}))
    assert r.ok and r.values == {"value": 21.0} and r.message == ""
    r = await snmp_custom.run(
        ctx(agent, "snmp_custom", {"oid": f"{E}.2.0", "label": "Load", "unit": "x"})
    )
    assert r.ok and r.values == {"value": 0.15} and r.message == "Load: 0.15 x"
    r = await snmp_custom.run(ctx(agent, "snmp_custom", {"oid": f"{E}.3.0"}))
    assert not r.ok and r.message == "Value is not numeric: 'abc'"
    r = await snmp_custom.run(ctx(agent, "snmp_custom", {"oid": f"{E}.77.0"}))
    assert not r.ok and r.message == f"OID not found: {E}.77.0"
    # percent_of_oid: both in one GET
    r = await snmp_custom.run(
        ctx(agent, "snmp_custom", {"oid": f"{E}.7.0", "percent_of_oid": f"{E}.8.0"})
    )
    assert r.ok and r.values == {"value": 20.0}
    r = await snmp_custom.run(
        ctx(
            agent,
            "snmp_custom",
            {"oid": f"{E}.9.0", "percent_of_oid": f"{E}.8.0", "label": "Drum Unit"},
        )
    )
    assert r.ok and r.values == {"value": None}
    assert r.message == "Drum Unit: level not reported (raw=-3)"
    r = await snmp_custom.run(
        ctx(agent, "snmp_custom", {"oid": f"{E}.7.0", "percent_of_oid": f"{E}.78.0"})
    )
    assert not r.ok and r.message == f"OID not found: {E}.78.0"
    # wrong community -> SNMP error
    r = await snmp_custom.run(ctx(agent, "snmp_custom", {"oid": f"{E}.1.0"}, community="x"))
    assert not r.ok and r.message.startswith("SNMP:")


async def test_snmp_custom_counter(agent: int) -> None:
    cfg = {"oid": f"{E}.4.0", "value_type": "counter", "multiplier": 8}
    r1 = await snmp_custom.run(ctx(agent, "snmp_custom", cfg, now=1000.0))
    assert r1.ok and r1.values == {"value": None}
    assert r1.new_state == {"t": 1000.0, "v": 4294967000, "bits": 32}
    r2 = await snmp_custom.run(
        ctx(agent, "snmp_custom", cfg, community="later", state=r1.new_state, now=1010.0)
    )
    assert r2.ok and r2.values == {"value": round((2**32 - 4294967000 + 1000) / 10 * 8, 3)}
    # Counter64 going backwards = reset
    cfg64 = {"oid": f"{E}.5.0", "value_type": "counter"}
    r1 = await snmp_custom.run(ctx(agent, "snmp_custom", cfg64, now=0.0))
    assert r1.new_state["bits"] == 64
    r2 = await snmp_custom.run(
        ctx(agent, "snmp_custom", cfg64, community="later", state=r1.new_state, now=10.0)
    )
    assert r2.ok and r2.values == {"value": None} and r2.new_state["v"] == 5000
    # Gauge32 counted as a 64-bit counter; a string is not a counter
    r = await snmp_custom.run(
        ctx(agent, "snmp_custom", {"oid": f"{E}.10.0", "value_type": "counter"}, now=0.0)
    )
    assert r.ok and r.new_state["bits"] == 64
    r = await snmp_custom.run(
        ctx(agent, "snmp_custom", {"oid": f"{E}.6.0", "value_type": "counter"})
    )
    assert not r.ok and r.message == "Value is not a counter"


async def test_snmp_custom_string(agent: int) -> None:
    base = {"oid": f"{E}.6.0", "value_type": "string"}
    r = await snmp_custom.run(ctx(agent, "snmp_custom", base))
    assert r.ok and r.values == {"value": 1.0} and r.message == "running"
    r = await snmp_custom.run(
        ctx(agent, "snmp_custom", {**base, "expected": "running", "label": "State"})
    )
    assert r.ok and r.message == "State: running"
    r = await snmp_custom.run(
        ctx(agent, "snmp_custom", {**base, "expected": "unn", "match": "contains"})
    )
    assert r.ok
    r = await snmp_custom.run(ctx(agent, "snmp_custom", {**base, "expected": "stopped"}))
    assert not r.ok and r.message == f"{E}.6.0: 'running' does not match 'stopped'"
    r = await snmp_custom.run(ctx(agent, "snmp_custom", {**base, "expected": "Running"}))
    assert not r.ok  # case-sensitive
    r = await snmp_custom.run(
        ctx(agent, "snmp_custom", {"oid": f"{E}.1.0", "value_type": "string"})
    )
    assert r.ok and r.message == "42"


async def test_snmp_disk(agent: int) -> None:
    r = await snmp_disk.run(ctx(agent, "snmp_disk", {"storage_descr": "/"}))
    assert r.ok, r.message
    assert r.values == {
        "disk_used_pct": 25.0,
        "disk_used_bytes": 250 * 4096.0,
        "disk_free_bytes": 750 * 4096.0,
        "disk_total_bytes": 1000 * 4096.0,
    }
    assert r.config_update == {"storage_index": 31}
    r = await snmp_disk.run(ctx(agent, "snmp_disk", {"storage_descr": "/", "storage_index": 31}))
    assert r.config_update is None
    r = await snmp_disk.run(ctx(agent, "snmp_disk", {"storage_descr": "C:\\"}))
    assert r.ok and r.values["disk_used_pct"] == 50.0 and r.config_update == {"storage_index": 33}
    r = await snmp_disk.run(ctx(agent, "snmp_disk", {"storage_descr": "/bo"}))
    assert not r.ok and r.message == "Storage '/bo' not found"
    r = await snmp_disk.run(ctx(agent, "snmp_disk", {"storage_descr": "/zero"}))
    assert not r.ok and r.message == "Storage '/zero' reports size 0"
    r = await snmp_disk.run(ctx(agent, "snmp_disk", {"storage_descr": "/big"}))
    assert r.ok and r.values["disk_total_bytes"] == (2**32 - 100) * 65536.0


@requires_db
async def test_custom_disk_api(agent: int, admin: httpx.AsyncClient) -> None:
    dev = await make_device(
        admin,
        snmp_credentials={"port": agent, "community": "public", "timeout_s": 1, "retries": 0},
    )
    r = await admin.post(
        "/api/sensors",
        json={
            "device_id": dev["id"],
            "type": "snmp_custom",
            "config": {"oid": f".{E}.1.0", "unit": "clients", "label": "Clients"},
            "channels": [{"key": "value", "warning_max": 40}],
        },
    )
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["name"] == "Clients" and s["config"]["oid"] == f"{E}.1.0"
    assert s["primary_channel"] == "value"
    assert s["channels"][0]["unit"] == "clients" and s["channels"][0]["warning_max"] == 40
    r = await admin.post(f"/api/sensors/{s['id']}/run")
    assert r.json()["status"] == "warning" and r.json()["channels"][0]["value"] == 42.0
    assert r.json()["channels"][0]["unit"] == "clients"
    # PATCH unit -> channel unit follows
    r = await admin.patch(f"/api/sensors/{s['id']}", json={"config": {"unit": "users"}})
    assert r.json()["channels"][0]["unit"] == "users"
    # default names
    r = await admin.post(
        "/api/sensors",
        json={"device_id": dev["id"], "type": "snmp_custom", "config": {"oid": f"{E}.4.0"}},
    )
    assert r.json()["name"] == f"SNMP {E}.4.0"
    cid = r.json()["id"]
    r = await admin.patch(f"/api/sensors/{cid}", json={"config": {"value_type": "counter"}})
    assert r.status_code == 200
    r = await admin.post(f"/api/sensors/{cid}/run")
    assert r.json()["ok"] is True
    r = await admin.patch(f"/api/sensors/{cid}", json={"config": {"percent_of_oid": f"{E}.8.0"}})
    assert r.status_code == 422  # counter + percent_of_oid
    r = await admin.post(
        "/api/sensors",
        json={"device_id": dev["id"], "type": "snmp_disk", "config": {"storage_descr": "/"}},
    )
    assert r.status_code == 201 and r.json()["name"] == "Disk /"
    disk = r.json()
    r = await admin.post(f"/api/sensors/{disk['id']}/run")
    assert r.json()["ok"] is True
    r = await admin.get(f"/api/sensors/{disk['id']}")
    assert r.json()["config"]["storage_index"] == 31
    # test endpoint with the custom unit
    r = await admin.post(
        "/api/sensors/test",
        json={
            "device_id": dev["id"],
            "type": "snmp_custom",
            "config": {"oid": f"{E}.7.0", "percent_of_oid": f"{E}.8.0", "unit": "%"},
        },
    )
    assert r.json()["ok"] is True and r.json()["channels"] == [
        {"key": "value", "unit": "%", "value": 20.0, "status": "up"}
    ]
    # storage picker
    r = await admin.get(f"/api/devices/{dev['id']}/snmp/storage")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["index"] for i in items] == [1, 2, 31, 32, 33, 34, 35]
    root = next(i for i in items if i["index"] == 31)
    assert root == {
        "index": 31,
        "descr": "/",
        "type": "fixed_disk",
        "size_bytes": 4096 * 1000,
        "used_bytes": 4096 * 250,
        "existing_sensor_id": disk["id"],
    }
    assert items[0]["type"] == "ram" and items[1]["type"] == "virtual_memory"
    assert items[1]["existing_sensor_id"] is None or items[1]["descr"] == "/"
    # sensor-types metadata
    r = await admin.get("/api/sensor-types")
    types = {t["type"]: t for t in r.json()["items"]}
    custom = types["snmp_custom"]
    assert custom["requires_snmp"] is True and custom["primary_channel"] == "value"
    mult = next(f for f in custom["config_fields"] if f["key"] == "multiplier")
    assert mult["type"] == "float" and mult["default"] == 1.0
    assert custom["channels"] == [
        {
            "key": "value",
            "unit": "",
            "warning_min": None,
            "warning_max": None,
            "down_min": None,
            "down_max": None,
        }
    ]
    assert [c["key"] for c in types["snmp_disk"]["channels"]] == [
        "disk_used_pct",
        "disk_used_bytes",
        "disk_free_bytes",
        "disk_total_bytes",
    ]
    # bad credentials -> 502
    await admin.patch(
        f"/api/devices/{dev['id']}",
        json={"snmp_credentials": {"port": agent, "community": "zz", "timeout_s": 1, "retries": 0}},
    )
    r = await admin.get(f"/api/devices/{dev['id']}/snmp/storage")
    assert r.status_code == 502 and r.json()["error"]["code"] == "snmp_error"
