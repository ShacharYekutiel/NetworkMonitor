"""SNMP sensors against a real agent (snmpsim) with generated .snmprec recordings."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from netmon.poller.checks import snmp_cpu, snmp_interface, snmp_memory, snmp_system
from netmon.poller.snmp_client import SnmpError, snmp_get
from netmon.poller.types import CheckContext
from tests.conftest import make_device, requires_db

IF_HC_IN_1 = 1_000_000
IF_HC_OUT_1 = 2_000_000


def _records(hc_in: int, hc_out: int, in32: int, errors: int) -> list[tuple[str, object, str]]:
    r: list[tuple[str, object, str]] = [
        ("1.3.6.1.2.1.1.1.0", 4, "Linux test-agent 6.1.0"),
        ("1.3.6.1.2.1.1.2.0", 6, "1.3.6.1.4.1.8072.3.2.10"),
        ("1.3.6.1.2.1.1.3.0", 67, "123456"),
        ("1.3.6.1.2.1.1.5.0", 4, "test-agent"),
        # ifTable: 1 lo, 2 eth0 (HC), 3 old0 (no HC, down)
        ("1.3.6.1.2.1.2.2.1.1.1", 2, "1"),
        ("1.3.6.1.2.1.2.2.1.1.2", 2, "2"),
        ("1.3.6.1.2.1.2.2.1.1.3", 2, "3"),
        ("1.3.6.1.2.1.2.2.1.2.1", 4, "lo"),
        ("1.3.6.1.2.1.2.2.1.2.2", 4, "eth0"),
        ("1.3.6.1.2.1.2.2.1.2.3", 4, "old0"),
        ("1.3.6.1.2.1.2.2.1.3.1", 2, "24"),
        ("1.3.6.1.2.1.2.2.1.3.2", 2, "6"),
        ("1.3.6.1.2.1.2.2.1.3.3", 2, "6"),
        ("1.3.6.1.2.1.2.2.1.5.1", 66, "10000000"),
        ("1.3.6.1.2.1.2.2.1.5.2", 66, "1000000000"),
        ("1.3.6.1.2.1.2.2.1.5.3", 66, "100000000"),
        ("1.3.6.1.2.1.2.2.1.6.2", "4x", "001122334455"),
        ("1.3.6.1.2.1.2.2.1.7.1", 2, "1"),
        ("1.3.6.1.2.1.2.2.1.7.2", 2, "1"),
        ("1.3.6.1.2.1.2.2.1.7.3", 2, "2"),
        ("1.3.6.1.2.1.2.2.1.8.1", 2, "1"),
        ("1.3.6.1.2.1.2.2.1.8.2", 2, "1"),
        ("1.3.6.1.2.1.2.2.1.8.3", 2, "2"),
        ("1.3.6.1.2.1.2.2.1.10.1", 65, "100"),
        ("1.3.6.1.2.1.2.2.1.10.2", 65, str(hc_in % 2**32)),
        ("1.3.6.1.2.1.2.2.1.10.3", 65, str(in32)),
        ("1.3.6.1.2.1.2.2.1.14.1", 65, "0"),
        ("1.3.6.1.2.1.2.2.1.14.2", 65, str(errors)),
        ("1.3.6.1.2.1.2.2.1.14.3", 65, "0"),
        ("1.3.6.1.2.1.2.2.1.16.1", 65, "100"),
        ("1.3.6.1.2.1.2.2.1.16.2", 65, str(hc_out % 2**32)),
        ("1.3.6.1.2.1.2.2.1.16.3", 65, "0"),
        ("1.3.6.1.2.1.2.2.1.20.1", 65, "0"),
        ("1.3.6.1.2.1.2.2.1.20.2", 65, "0"),
        ("1.3.6.1.2.1.2.2.1.20.3", 65, "0"),
        # hrStorage: 1 = RAM, 2 = fixed disk
        ("1.3.6.1.2.1.25.2.3.1.1.1", 2, "1"),
        ("1.3.6.1.2.1.25.2.3.1.1.2", 2, "2"),
        ("1.3.6.1.2.1.25.2.3.1.2.1", 6, "1.3.6.1.2.1.25.2.1.2"),
        ("1.3.6.1.2.1.25.2.3.1.2.2", 6, "1.3.6.1.2.1.25.2.1.4"),
        ("1.3.6.1.2.1.25.2.3.1.3.1", 4, "Physical memory"),
        ("1.3.6.1.2.1.25.2.3.1.3.2", 4, "/"),
        ("1.3.6.1.2.1.25.2.3.1.4.1", 2, "1024"),
        ("1.3.6.1.2.1.25.2.3.1.4.2", 2, "4096"),
        ("1.3.6.1.2.1.25.2.3.1.5.1", 2, "8000000"),
        ("1.3.6.1.2.1.25.2.3.1.5.2", 2, "1000"),
        ("1.3.6.1.2.1.25.2.3.1.6.1", 2, "6000000"),
        ("1.3.6.1.2.1.25.2.3.1.6.2", 2, "100"),
        # hrProcessorLoad
        ("1.3.6.1.2.1.25.3.3.1.2.196608", 2, "20"),
        ("1.3.6.1.2.1.25.3.3.1.2.196609", 2, "40"),
        # ifXTable (only eth0 / lo)
        ("1.3.6.1.2.1.31.1.1.1.1.1", 4, "lo"),
        ("1.3.6.1.2.1.31.1.1.1.1.2", 4, "eth0"),
        ("1.3.6.1.2.1.31.1.1.1.6.2", 70, str(hc_in)),
        ("1.3.6.1.2.1.31.1.1.1.10.2", 70, str(hc_out)),
        ("1.3.6.1.2.1.31.1.1.1.15.1", 66, "10"),
        ("1.3.6.1.2.1.31.1.1.1.15.2", 66, "1000"),
        ("1.3.6.1.2.1.31.1.1.1.18.2", 4, "uplink"),
    ]
    return sorted(r, key=lambda x: tuple(int(p) for p in x[0].split(".")))


def _write(path: Path, records: list[tuple[str, object, str]]) -> None:
    path.write_text("".join(f"{o}|{t}|{v}\n" for o, t, v in records))


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def agent() -> Iterator[int]:
    exe = shutil.which("snmpsim-command-responder") or str(
        Path(sys.executable).parent / "snmpsim-command-responder"
    )
    if not Path(exe).exists():
        pytest.skip("snmpsim not installed")
    data = Path(tempfile.mkdtemp(prefix="netmon-snmpsim-"))
    data.chmod(0o755)
    _write(data / "public.snmprec", _records(IF_HC_IN_1, IF_HC_OUT_1, 4294967000, 0))
    # 10 s later: eth0 in +10 Mbit/s, out +1 Mbit/s, 10 errors; old0 32-bit counter wrapped
    _write(
        data / "later.snmprec", _records(IF_HC_IN_1 + 12_500_000, IF_HC_OUT_1 + 1_250_000, 1000, 10)
    )
    cache = data / "cache"
    cache.mkdir()
    args = [
        exe,
        f"--data-dir={data}",
        f"--cache-dir={cache}",
        f"--agent-udpv4-endpoint=127.0.0.1:{(port := _free_udp_port())}",
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        shutil.chown(cache, "nobody")
        args += ["--process-user=nobody", "--process-group=nogroup"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    creds = {"version": "v2c", "port": port, "timeout_s": 1, "retries": 0, "community": "public"}
    ok = False
    for _ in range(50):
        try:
            asyncio.run(snmp_get("127.0.0.1", creds, ["1.3.6.1.2.1.1.5.0"]))
            ok = True
            break
        except SnmpError:
            if proc.poll() is not None:
                break
    if not ok:
        proc.kill()
        pytest.skip("snmpsim agent did not start")
    yield port
    proc.terminate()
    proc.wait(10)
    shutil.rmtree(data, ignore_errors=True)


def snmp_ctx(
    type_: str,
    port: int,
    config: dict | None = None,
    community: str = "public",
    version: str = "v2c",
    state: dict | None = None,
    now: float | None = None,
) -> CheckContext:
    creds = {"version": version, "port": port, "timeout_s": 1, "retries": 1, "community": community}
    return CheckContext(1, type_, config or {}, "127.0.0.1", creds, state or {}, 30, now)


async def test_snmp_system(agent: int) -> None:
    r = await snmp_system.run(snmp_ctx("snmp_system", agent))
    assert r.ok, r.message
    assert r.values == {"uptime_s": 1234.56}
    assert r.message == "test-agent — Linux test-agent 6.1.0"
    r = await snmp_system.run(snmp_ctx("snmp_system", agent, version="v1"))
    assert r.ok, r.message
    r = await snmp_system.run(snmp_ctx("snmp_system", agent, community="wrong"))
    assert not r.ok and "SNMP" in r.message


async def test_snmp_cpu_memory(agent: int) -> None:
    r = await snmp_cpu.run(snmp_ctx("snmp_cpu", agent))
    assert r.ok and r.values == {"cpu_load_pct": 30.0}
    r = await snmp_memory.run(
        snmp_ctx("snmp_memory", agent, {"storage_index": None, "storage_descr": ""})
    )
    assert r.ok, r.message
    assert r.values == {
        "mem_used_pct": 75.0,
        "mem_used_bytes": 6000000 * 1024.0,
        "mem_total_bytes": 8000000 * 1024.0,
    }
    assert r.config_update == {"storage_descr": "Physical memory"}
    r = await snmp_memory.run(
        snmp_ctx("snmp_memory", agent, {"storage_index": 2, "storage_descr": "/"})
    )
    assert r.ok and r.values["mem_used_pct"] == 10.0 and r.config_update is None


async def test_snmp_interface_counters(agent: int) -> None:
    cfg = {"if_index": 2, "if_name": "eth0", "speed_bps_override": None}
    r1 = await snmp_interface.run(snmp_ctx("snmp_interface", agent, cfg, now=1000.0))
    assert r1.ok, r1.message
    assert r1.values == {"oper_status": 1.0}  # first poll: baseline only
    assert r1.new_state == {
        "t": 1000.0,
        "hc": True,
        "in_octets": IF_HC_IN_1,
        "out_octets": IF_HC_OUT_1,
        "in_errors": 0,
        "out_errors": 0,
    }
    r2 = await snmp_interface.run(
        snmp_ctx("snmp_interface", agent, cfg, community="later", state=r1.new_state, now=1010.0)
    )
    assert r2.ok, r2.message
    assert r2.values["in_bps"] == 10_000_000.0 and r2.values["out_bps"] == 1_000_000.0
    assert r2.values["in_util_pct"] == 1.0 and r2.values["out_util_pct"] == 0.1  # 1 Gbit/s
    assert r2.values["in_errors_ps"] == 1.0 and r2.values["oper_status"] == 1.0
    # interface without ifHC* -> 32-bit counters with wrap handling
    cfg3 = {"if_index": 3, "if_name": "old0", "speed_bps_override": None}
    r1 = await snmp_interface.run(snmp_ctx("snmp_interface", agent, cfg3, now=0.0))
    assert r1.new_state["hc"] is False
    r2 = await snmp_interface.run(
        snmp_ctx("snmp_interface", agent, cfg3, community="later", state=r1.new_state, now=10.0)
    )
    assert r2.values["in_bps"] == (2**32 - 4294967000 + 1000) * 8 / 10
    assert r2.values["oper_status"] == 2.0
    assert r2.values["in_util_pct"] == round(r2.values["in_bps"] / 1e8 * 100, 3)
    r = await snmp_interface.run(snmp_ctx("snmp_interface", agent, {"if_index": 99}))
    assert not r.ok


@requires_db
async def test_snmp_api_enumeration_and_sensor(agent: int, admin: httpx.AsyncClient) -> None:
    dev = await make_device(
        admin,
        host="127.0.0.1",
        snmp_credentials={
            "version": "v2c",
            "port": agent,
            "timeout_s": 1,
            "retries": 1,
            "community": "public",
        },
    )
    r = await admin.post(
        "/api/sensors",
        json={
            "device_id": dev["id"],
            "type": "snmp_interface",
            "config": {"if_index": 2, "if_name": "eth0"},
        },
    )
    sensor = r.json()
    r = await admin.get(f"/api/devices/{dev['id']}/snmp/interfaces")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["if_index"] for i in items] == [1, 2, 3]
    eth0 = items[1]
    assert eth0 == {
        "if_index": 2,
        "name": "eth0",
        "descr": "eth0",
        "alias": "uplink",
        "type": 6,
        "speed_bps": 1_000_000_000,
        "admin_status": "up",
        "oper_status": "up",
        "mac": "00:11:22:33:44:55",
        "existing_sensor_id": sensor["id"],
    }
    assert items[2]["name"] == "old0" and items[2]["oper_status"] == "down"
    assert items[2]["speed_bps"] == 100_000_000 and items[2]["existing_sensor_id"] is None
    # run-now persists state and status
    r = await admin.post(f"/api/sensors/{sensor['id']}/run")
    assert r.json()["ok"] is True and r.json()["status"] == "up"
    chans = {c["key"]: c for c in r.json()["channels"]}
    assert chans["oper_status"]["value"] == 1.0 and chans["in_bps"]["value"] is None
    # test with explicit wrong credentials for an unsaved device
    r = await admin.post(
        "/api/sensors/test",
        json={
            "host": "127.0.0.1",
            "type": "snmp_system",
            "config": {},
            "snmp_credentials": {"port": agent, "community": "nope", "timeout_s": 1, "retries": 0},
        },
    )
    assert r.json()["ok"] is False
    r = await admin.patch(
        f"/api/devices/{dev['id']}",
        json={
            "snmp_credentials": {"port": agent, "community": "nope", "timeout_s": 1, "retries": 0}
        },
    )
    r = await admin.get(f"/api/devices/{dev['id']}/snmp/interfaces")
    assert r.status_code == 502 and r.json()["error"]["code"] == "snmp_error"
