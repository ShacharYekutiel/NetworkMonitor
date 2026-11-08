"""[2b-1] HPE templates (ArubaOS-Switch, Comware) against snmpsim recordings, plus re-identify.

Agents on 127.0.6.51 / .52 (outside QA's 127.0.0.4x/5x lab)."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from tests.conftest import make_device, requires_db
from tests.snmpsim_agents import Record, interfaces, start_agent, system

pytestmark = requires_db

ARUBA_IP, COMWARE_IP = "127.0.6.51", "127.0.6.52"
ARUBA_DESCR = (
    "Aruba JL256A 2930F-48G-4SFP+ Switch, revision WC.16.11.0012, ROM WC.16.01.0010 "
    "(/ws/swbuildm/rel_orlando_qaoff/code/build/bom(swbuildm_rel_orlando_qaoff_rel_orlando))"
)
COMWARE_DESCR = (
    "HPE Comware Platform Software, Software Version 7.1.070, Release 6328 "
    "HPE 1950-24G-2SFP+-2XGT Switch"
)
SENSOR = "1.3.6.1.4.1.11.2.14.11.1.2.6.1"
MEM = "1.3.6.1.4.1.11.2.14.11.5.1.1.2.1.1.1"
PETH = "1.3.6.1.2.1.105.1.3.1.1"
ENT = "1.3.6.1.2.1.47.1.1.1.1"
HH3C = "1.3.6.1.4.1.25506.2.6.1.1.1.1"


def aruba_records() -> list[Record]:
    recs: list[Record] = system(ARUBA_DESCR, "1.3.6.1.4.1.11.2.3.7.11.181", "core")
    recs += interfaces(
        [(1, "1", 6, 1), (2, "2", 6, 1), (3, "3", 6, 2), (4, "4", 6, 2), (5, "DEFAULT_VLAN", 53, 1)]
    )
    recs += [
        ("1.3.6.1.4.1.11.2.14.11.5.1.9.6.1.0", 2, "7"),
        (f"{MEM}.5.1", 66, "330000000"),
        (f"{MEM}.6.1", 66, "200000000"),
        (f"{MEM}.7.1", 66, "130000000"),
        (f"{PETH}.2.1", 66, "370"),
        (f"{PETH}.3.1", 2, "1"),
        (f"{PETH}.4.1", 66, "45"),
    ]
    for idx, descr, status in [
        (1, "Fan Sensor", 4),
        (2, "Power Supply Sensor", 2),
        (3, "Over-temperature Sensor", 4),
        (4, "Redundant Power Supply Sensor", 5),
    ]:
        recs += [(f"{SENSOR}.4.{idx}", 2, str(status)), (f"{SENSOR}.7.{idx}", 4, descr)]
    return recs


def comware_records() -> list[Record]:
    recs: list[Record] = system(COMWARE_DESCR, "1.3.6.1.4.1.25506.11.1.177", "access-1")
    recs += interfaces(
        [
            (1, "GigabitEthernet1/0/1", 6, 1),
            (2, "GigabitEthernet1/0/2", 6, 1),
            (3, "GigabitEthernet1/0/3", 6, 2),
            (4, "GigabitEthernet1/0/4", 6, 2),
            (5, "Vlan-interface1", 136, 1),
            (6, "NULL0", 1, 1),
            (7, "InLoopBack0", 24, 1),
        ]
    )
    for idx, cls, name in [
        (1, 3, "Chassis"),
        (2, 9, "Board"),
        (3, 7, "Fan 1"),
        (4, 7, "Fan 2"),
        (5, 6, "PSU 1"),
    ]:
        recs += [(f"{ENT}.5.{idx}", 2, str(cls)), (f"{ENT}.7.{idx}", 4, name)]
    for col, vals in {
        6: {1: 0, 2: 12, 3: 0, 4: 0, 5: 0},
        8: {1: 0, 2: 41},
        12: {1: 0, 2: 43},
        19: {1: 1, 2: 2, 3: 2, 4: 41, 5: 2},
    }.items():
        recs += [(f"{HH3C}.{col}.{i}", 2, str(v)) for i, v in vals.items()]
    recs += [(f"{PETH}.2.1", 66, "0"), (f"{PETH}.3.1", 2, "2"), (f"{PETH}.4.1", 66, "0")]
    return recs


def _free_udp_port(ip: str) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((ip, 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def agents() -> Iterator[dict[str, int]]:
    started = []
    ports: dict[str, int] = {}
    for ip, recs in ((ARUBA_IP, aruba_records()), (COMWARE_IP, comware_records())):
        port = _free_udp_port(ip)
        a = start_agent(ip, port, {"public": recs})
        if a is None:
            for x in started:
                x.stop()
            pytest.skip("snmpsim not available")
        started.append(a)
        ports[ip] = port
    yield ports
    for a in started:
        a.stop()


async def _device(admin: httpx.AsyncClient, ip: str, port: int, name: str) -> dict[str, Any]:
    return await make_device(
        admin,
        name=name,
        host=ip,
        snmp_credentials={"version": "v2c", "port": port, "community": "public", "retries": 0},
    )


async def _apply(admin: httpx.AsyncClient, did: int, template: str) -> dict[str, Any]:
    r = await admin.post(f"/api/devices/{did}/apply-template", json={"template_id": template})
    assert r.status_code == 200, r.text
    return r.json()


async def _run_all(admin: httpx.AsyncClient, created: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for c in created:
        if c["type"] == "ping":
            continue
        r = await admin.post(f"/api/sensors/{c['sensor_id']}/run")
        assert r.status_code == 200, r.text
        out[c["name"]] = (await admin.get(f"/api/sensors/{c['sensor_id']}")).json()
    return out


async def test_arubaos_template(admin: httpx.AsyncClient, agents: dict[str, int]) -> None:
    dev = await _device(admin, ARUBA_IP, agents[ARUBA_IP], "core")
    res = await _apply(admin, dev["id"], "hpe_arubaos_switch")
    names = [c["name"] for c in res["created"]]
    assert names == [
        "Ping",
        "System",
        "CPU",
        "Memory 1",
        "Fan Sensor",
        "Power Supply Sensor",
        "Over-temperature Sensor",
        "PoE 1 usage",
        "PoE 1 power",
        "PoE 1 budget",
        "1",
        "2",
    ]
    assert "Redundant Power Supply Sensor" not in names  # notPresent filtered
    sensors = await _run_all(admin, res["created"])
    assert sensors["CPU"]["channels"][0]["last_value"] == 7
    assert sensors["CPU"]["channels"][0]["warning_max"] == 80
    assert sensors["Memory 1"]["channels"][0]["last_value"] == pytest.approx(39.39, abs=0.01)
    assert sensors["Fan Sensor"]["status"] == "up"
    assert sensors["Fan Sensor"]["status_message"] == "Fan Sensor: good (4)"
    psu = sensors["Power Supply Sensor"]
    assert psu["status"] == "down" and psu["status_message"] == "Power Supply Sensor: bad (2)"
    assert psu["interval_s"] == 300 and psu["config"]["value_type"] == "enum"
    assert sensors["Over-temperature Sensor"]["status"] == "up"
    usage = sensors["PoE 1 usage"]
    assert usage["channels"][0]["last_value"] == pytest.approx(12.16, abs=0.01)
    assert usage["channels"][0]["unit"] == "%"
    assert sensors["PoE 1 power"]["channels"][0]["last_value"] == 45
    assert sensors["PoE 1 power"]["channels"][0]["unit"] == "W"
    budget = sensors["PoE 1 budget"]
    assert budget["channels"][0]["last_value"] == 370 and budget["interval_s"] == 3600
    # idempotent
    again = await _apply(admin, dev["id"], "hpe_arubaos_switch")
    assert again["created"] == [] and {s["reason"] for s in again["skipped"]} == {"exists"}
    # identify -> switch / hp / suggests the ArubaOS template
    r = await admin.post(f"/api/devices/{dev['id']}/identify")
    assert r.status_code == 200, r.text
    ident = r.json()
    assert (ident["device_class"], ident["vendor"], ident["suggested_template"]) == (
        "switch",
        "hp",
        "hpe_arubaos_switch",
    )
    assert ident["classified_by"] == "snmp" and ident["confidence"] == "high"
    assert ident["snmp_ok"] is True and ident["sys_object_id"] == "1.3.6.1.4.1.11.2.3.7.11.181"
    assert (await admin.get(f"/api/devices/{dev['id']}")).json()["identity"] == ident


async def test_comware_template(admin: httpx.AsyncClient, agents: dict[str, int]) -> None:
    dev = await _device(admin, COMWARE_IP, agents[COMWARE_IP], "access-1")
    res = await _apply(admin, dev["id"], "hpe_comware")
    names = [c["name"] for c in res["created"]]
    assert names == [
        "Ping",
        "System",
        "CPU Board",
        "Memory Board",
        "Temperature Board",
        "Fan 1",
        "Fan 2",
        "PSU 1",
        "GigabitEthernet1/0/1",
        "GigabitEthernet1/0/2",
    ]
    skipped = {(s["name"], s["reason"]) for s in res["skipped"]}
    assert [r for n, r in skipped if n.startswith("PoE")] == ["no_rows"] * 3
    sensors = await _run_all(admin, res["created"])
    assert sensors["CPU Board"]["channels"][0]["last_value"] == 12
    assert sensors["CPU Board"]["config"]["oid"] == f"{HH3C}.6.2"
    assert sensors["Memory Board"]["channels"][0]["last_value"] == 41
    temp = sensors["Temperature Board"]
    assert temp["channels"][0]["last_value"] == 43 and temp["channels"][0]["unit"] == "°C"
    assert temp["channels"][0]["warning_max"] == 65 and temp["status"] == "up"
    assert (
        sensors["Fan 1"]["status"] == "up"
        and sensors["Fan 1"]["status_message"] == "Fan 1: normal (2)"
    )
    fan2 = sensors["Fan 2"]
    assert fan2["status"] == "down" and fan2["status_message"] == "Fan 2: unexpected value (41)"
    assert sensors["PSU 1"]["status"] == "up"
    r = await admin.post(f"/api/devices/{dev['id']}/identify")
    ident = r.json()
    assert (ident["device_class"], ident["vendor"], ident["suggested_template"]) == (
        "switch",
        "h3c",
        "hpe_comware",
    )
