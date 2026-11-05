"""[2a] Discovery end to end: scans against real snmpsim agents (one process per simulated device,
each on its own loopback address), import with templates, profiles, scheduler, retention."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from netmon.discovery.scheduler import DiscoveryScheduler
from netmon.services.serialize import now_utc
from tests.conftest import default_group_id, requires_db
from tests.snmpsim_agents import (
    Agent,
    cisco_records,
    opnsense_records,
    printer_records,
    snmpsim_exe,
    start_agent,
    unifi_records,
)

pytestmark = requires_db

SNMP_PORT = 1161
HTTP_PORT = 8080


def _udp_free(ip: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _tcp_free(ip: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((ip, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _pick_net() -> str | None:
    """127.0.0.x as in the QA layout; when another lab already holds those ports (e.g. QA's
    tests/sim/lab.sh), the same layout on 127.0.2.x / 127.0.3.x."""
    for net in ("127.0.0", "127.0.2", "127.0.3"):
        if all(_udp_free(f"{net}.{i}", SNMP_PORT) for i in range(20, 31)) and all(
            _tcp_free(f"{net}.{i}", p) for i in range(20, 31) for p in (HTTP_PORT, 80, 443, 8443)
        ):
            return net
    return None


class _Http(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"<html>ok</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


class Lab:
    def __init__(self, net: str) -> None:
        self.net = net
        self.agents: list[Agent] = []
        self.http: ThreadingHTTPServer | None = None

    def ip(self, last: int) -> str:
        return f"{self.net}.{last}"

    @property
    def range(self) -> str:
        return f"{self.net}.20-30"


@pytest.fixture(scope="module")
def lab() -> Iterator[Lab]:
    if snmpsim_exe() is None:
        pytest.skip("snmpsim not installed")
    net = _pick_net()
    if net is None:
        pytest.skip("no free loopback block for the discovery lab")
    lab = Lab(net)
    for last, files in (
        (21, {"public": opnsense_records()}),
        (22, {"public": unifi_records()}),
        (23, {"printers": printer_records()}),
        (24, {"public": cisco_records()}),
    ):
        agent = start_agent(lab.ip(last), SNMP_PORT, files)
        if agent is None:
            for a in lab.agents:
                a.stop()
            pytest.skip("snmpsim agent did not start")
        lab.agents.append(agent)
    lab.http = ThreadingHTTPServer((lab.ip(25), HTTP_PORT), _Http)
    threading.Thread(target=lab.http.serve_forever, daemon=True).start()
    yield lab
    lab.http.shutdown()
    lab.http.server_close()
    for a in lab.agents:
        a.stop()


def scan_options(**kw: Any) -> dict[str, Any]:
    return {
        "ping": False,
        "snmp_port": SNMP_PORT,
        "snmp_timeout_s": 1,
        "tcp_ports": [HTTP_PORT, 9100],
        "tcp_timeout_ms": 500,
        "extra_communities": ["printers"],
        "dns": False,
        **kw,
    }


async def wait_scan(c: httpx.AsyncClient, scan_id: int, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await c.get(f"/api/discovery/scans/{scan_id}")
        if r.json()["status"] != "running":
            return r.json()
        await asyncio.sleep(0.2)
    raise AssertionError("scan did not finish")


async def results(c: httpx.AsyncClient, scan_id: int, **params: Any) -> list[dict[str, Any]]:
    r = await c.get(f"/api/discovery/scans/{scan_id}/results", params=params)
    assert r.status_code == 200, r.text
    return r.json()["items"]


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, msg: str) -> None:
        self.sent.append(json.loads(msg))

    async def close(self, code: int = 1000) -> None:
        pass


# ------------------------------------------------------------------ end to end
async def test_scan_import_end_to_end(rt, admin: httpx.AsyncClient, lab: Lab) -> None:  # type: ignore[no-untyped-def]
    gid = await default_group_id(admin)
    admin_ws, viewer_ws = FakeWS(), FakeWS()
    ca = rt.hub.register(admin_ws, user_id=1, is_admin=True)
    cv = rt.hub.register(viewer_ws, user_id=2, is_admin=False)
    try:
        r = await admin.post(
            "/api/discovery/scans",
            json={"targets": [lab.range], "group_id": gid, "options": scan_options()},
        )
        assert r.status_code == 202, r.text
        scan = r.json()
        assert scan["status"] == "running" and scan["profile_id"] is None
        assert scan["trigger"] == "manual" and scan["created_by"] == "admin"
        assert scan["total_hosts"] == 11
        assert scan["options"]["extra_communities"] is None
        assert scan["options"]["extra_communities_count"] == 1
        assert "printers" not in r.text
        # only one running scan
        r = await admin.post("/api/discovery/scans", json={"targets": [lab.ip(1)], "group_id": gid})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "scan_running"
        assert r.json()["error"]["details"] == [{"scan_id": scan["id"]}]
        r = await admin.delete(f"/api/discovery/scans/{scan['id']}")
        assert r.status_code == 409 and r.json()["error"]["code"] == "scan_running"
        r = await admin.post(
            f"/api/discovery/scans/{scan['id']}/import",
            json={"items": [{"result_id": 1, "template_id": "generic_ping"}]},
        )
        assert r.status_code == 409
        done = await wait_scan(admin, scan["id"])
        await asyncio.sleep(0.1)
    finally:
        await rt.hub.unregister(ca)
        await rt.hub.unregister(cv)
    assert done["status"] == "completed", done
    assert (done["scanned_hosts"], done["found_hosts"], done["new_hosts"]) == (11, 5, 5)
    assert done["finished_at"] is not None and done["error"] is None

    # WS: admins only, progress -> results -> finished; no secrets
    types = [m["type"] for m in admin_ws.sent if m["type"].startswith("discovery.")]
    assert types[0] == "discovery.progress" and types[-1] == "discovery.finished"
    assert types.count("discovery.result") == 5 and types.count("discovery.finished") == 1
    assert not [m for m in viewer_ws.sent if m["type"].startswith("discovery.")]
    fin = next(m for m in admin_ws.sent if m["type"] == "discovery.finished")["data"]
    assert fin == {
        "scan_id": scan["id"],
        "status": "completed",
        "total_hosts": 11,
        "scanned_hosts": 11,
        "found_hosts": 5,
        "new_hosts": 5,
        "error": None,
    }
    assert "printers" not in json.dumps(admin_ws.sent)

    items = await results(admin, scan["id"])
    by_ip = {i["ip"]: i for i in items}
    assert [i["ip"] for i in items] == [lab.ip(n) for n in (21, 22, 23, 24, 25)]
    expect = {
        21: ("router", "opnsense", "opnsense", "group"),
        22: ("access_point", "ubiquiti", "unifi_ap", "group"),
        23: ("printer", "brother", "printer", "extra"),
        24: ("switch", "cisco", "generic_snmp", "group"),
        25: ("unknown", None, "http_server", None),
    }
    for last, (cls, vendor, tpl, cred) in expect.items():
        it = by_ip[lab.ip(last)]
        got = (it["device_class"], it["vendor"], it["suggested_template"], it["snmp_credential"])
        assert got == (cls, vendor, tpl, cred), it
        assert it["is_new"] is True and it["existing_device_id"] is None
    opn = by_ip[lab.ip(21)]
    assert opn["sys_name"] == "OPNsense.localdomain" and opn["snmp_version"] == "v2c"
    assert opn["sys_object_id"] == "1.3.6.1.4.1.8072.3.2.8" and opn["ping_ok"] is False
    assert by_ip[lab.ip(25)]["open_ports"] == [HTTP_PORT]
    assert by_ip[lab.ip(25)]["snmp_ok"] is False
    assert set(opn) == {
        "id",
        "scan_id",
        "ip",
        "hostname",
        "mac",
        "ping_ok",
        "ping_rtt_ms",
        "snmp_ok",
        "snmp_version",
        "snmp_credential",
        "sys_name",
        "sys_descr",
        "sys_object_id",
        "open_ports",
        "device_class",
        "vendor",
        "suggested_template",
        "existing_device_id",
        "is_new",
        "imported_device_id",
        "fingerprint",
        "model",
        "classified_by",
        "confidence",
    }
    # filters
    assert [i["ip"] for i in await results(admin, scan["id"], device_class="printer,router")] == [
        lab.ip(21),
        lab.ip(23),
    ]
    assert [i["ip"] for i in await results(admin, scan["id"], q="opnsense")] == [lab.ip(21)]
    assert len(await results(admin, scan["id"], only_new="true")) == 5
    r = await admin.get(f"/api/discovery/scans/{scan['id']}/results?device_class=toaster")
    assert r.status_code == 422
    # the credential snapshot is stored internally but never returned
    async with rt.sessionmaker() as session:
        creds = (
            await session.execute(
                text("SELECT credentials FROM discovery_scans WHERE id = :id"), {"id": scan["id"]}
            )
        ).scalar_one()
    assert creds[1]["community"] == "printers"
    r = await admin.get("/api/discovery/scans")
    assert "printers" not in r.text and r.json()["items"][0]["id"] == scan["id"]

    # ---------------------------------------------------------------- import
    ids = {last: by_ip[lab.ip(last)]["id"] for last in expect}
    r = await admin.post(
        f"/api/discovery/scans/{scan['id']}/import",
        json={"items": [{"result_id": ids[22], "template_id": "nope"}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["loc"] == ["body", "items", 0, "template_id"]
    r = await admin.post(
        f"/api/discovery/scans/{scan['id']}/import",
        json={
            "items": [
                {"result_id": ids[22], "template_id": "unifi_ap"},
                {"result_id": ids[23], "template_id": "printer"},
                {"result_id": ids[21], "template_id": "opnsense", "name": "Firewall"},
                {"result_id": ids[24], "template_id": "generic_snmp"},
                {"result_id": ids[25], "template_id": "http_server"},
                {"result_id": 999999, "template_id": "generic_ping"},
            ]
        },
        timeout=120,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["errors"] == [
        {
            "result_id": 999999,
            "code": "not_found",
            "message": "Result not found in this scan",
            "device_id": None,
        }
    ]
    created = {c["result_id"]: c for c in out["created"]}
    assert [c["result_id"] for c in out["created"]] == [ids[22], ids[23], ids[21], ids[24], ids[25]]
    assert created[ids[21]]["device_name"] == "Firewall"
    assert created[ids[22]]["device_name"] == "U6-Lite-Office"

    async def sensors_of(device_id: int) -> dict[str, dict[str, Any]]:
        r = await admin.get("/api/sensors", params={"device_id": device_id})
        return {s["name"]: s for s in r.json()["items"]}

    ap = await sensors_of(created[ids[22]]["device_id"])
    assert set(ap) == {
        "Ping",
        "System",
        "CPU Load",
        "Memory",
        "eth0",
        "Clients HomeWiFi (ng)",
        "Clients HomeWiFi (na)",
    }
    assert created[ids[22]]["sensors_created"] == 7
    assert ap["Clients HomeWiFi (na)"]["config"]["oid"] == "1.3.6.1.4.1.41112.1.6.1.2.1.8.2"
    assert ap["Clients HomeWiFi (na)"]["channels"][0]["unit"] == "clients"
    # the AP inherits the group credentials? no: the scan overrode the port -> own credentials
    dev = (await admin.get(f"/api/devices/{created[ids[22]]['device_id']}")).json()
    assert dev["effective_snmp_source"] == "device"
    assert dev["snmp_credentials"]["port"] == SNMP_PORT
    assert dev["snmp_credentials"]["timeout_s"] == 2 and dev["snmp_credentials"]["retries"] == 1
    assert dev["host"] == lab.ip(22) and dev["description"].startswith("Linux U6-Lite")
    # the printer got its own credentials (community printers, port 1161)
    pdev = created[ids[23]]["device_id"]
    async with rt.sessionmaker() as session:
        pcreds = (
            await session.execute(
                text("SELECT snmp_credentials FROM devices WHERE id = :id"), {"id": pdev}
            )
        ).scalar_one()
    assert pcreds["community"] == "printers" and pcreds["port"] == SNMP_PORT
    pr = await sensors_of(pdev)
    assert {
        "Ping",
        "System",
        "Supply Black Toner Cartridge",
        "Supply Drum Unit",
        "Page counter",
    } <= set(pr)
    supply = pr["Supply Black Toner Cartridge"]
    assert supply["config"]["percent_of_oid"] == "1.3.6.1.2.1.43.11.1.1.8.1.1"
    assert supply["channels"][0]["warning_min"] == 15 and supply["channels"][0]["down_min"] == 5
    assert pr["Page counter"]["interval_s"] == 3600
    skipped_p = {s["name"]: s for s in created[ids[23]]["sensors_skipped"]}
    assert skipped_p["HTTP"]["reason"] == "probe_failed"
    # switch: CPU/memory probes fail without HOST-RESOURCES; 2 up interfaces
    sw = created[ids[24]]
    reasons = {(s["name"], s["reason"]) for s in sw["sensors_skipped"]}
    assert ("CPU Load", "probe_failed") in reasons and ("Memory", "probe_failed") in reasons
    assert {"Gi1/0/1", "Gi1/0/2", "Ping", "System"} == set(await sensors_of(sw["device_id"]))
    # firewall: interfaces igb0 + igb1 only (lo0 loopback, pflog0 excluded, enc0 down)
    fw = await sensors_of(created[ids[21]]["device_id"])
    assert {"Ping", "System", "CPU Load", "Memory", "Disk /", "igb0", "igb1"} == set(fw)
    assert fw["Disk /"]["config"]["storage_index"] == 31  # filled by the probe
    assert {s["name"]: s["reason"] for s in created[ids[21]]["sensors_skipped"]} == {
        "Web GUI": "probe_failed"
    }
    # HTTP-only host
    web = await sensors_of(created[ids[25]]["device_id"])
    assert set(web) == {"Ping", "HTTP 8080"}
    assert {s["name"] for s in created[ids[25]]["sensors_skipped"]} == {
        "HTTP",
        "HTTPS",
        "HTTPS 8443",
    }

    # sensors get data
    for sid, check in (
        (ap["Clients HomeWiFi (na)"]["id"], lambda v: v == 12.0),
        (ap["Clients HomeWiFi (ng)"]["id"], lambda v: v == 5.0),
        (supply["id"], lambda v: v == 40.0),
        (pr["Supply Drum Unit"]["id"], lambda v: v is None),
        (pr["Page counter"]["id"], lambda v: v == 12345.0),
        (fw["Disk /"]["id"], lambda v: v is not None),
        (web["HTTP 8080"]["id"], lambda v: v is not None),
    ):
        r = await admin.post(f"/api/sensors/{sid}/run")
        res = r.json()
        assert res["ok"] is True and res["status"] == "up", res
        assert check(res["channels"][0]["value"]), res
    r = await admin.get(f"/api/sensors/{pr['Supply Drum Unit']['id']}")
    assert r.json()["status_message"] == "Drum Unit: level not reported (raw=-3)"

    # result rows now point at the devices; re-import -> device_exists
    items = {i["ip"]: i for i in await results(admin, scan["id"])}
    assert items[lab.ip(22)]["imported_device_id"] == created[ids[22]]["device_id"]
    assert items[lab.ip(22)]["existing_device_id"] == created[ids[22]]["device_id"]
    assert await results(admin, scan["id"], hide_existing="true") == []
    r = await admin.post(
        f"/api/discovery/scans/{scan['id']}/import",
        json={"items": [{"result_id": ids[22], "template_id": "unifi_ap"}]},
    )
    assert r.json() == {
        "created": [],
        "errors": [
            {
                "result_id": ids[22],
                "code": "device_exists",
                "message": "A device with this host already exists",
                "device_id": created[ids[22]]["device_id"],
            }
        ],
    }

    # apply-template is idempotent
    r = await admin.post(
        f"/api/devices/{created[ids[22]]['device_id']}/apply-template",
        json={"template_id": "unifi_ap"},
    )
    assert r.status_code == 200, r.text
    again = r.json()
    assert again["created"] == [] and again["template_id"] == "unifi_ap"
    assert {s["reason"] for s in again["skipped"]} == {"exists"}
    assert len(again["skipped"]) == 7
    r = await admin.post(
        f"/api/devices/{created[ids[22]]['device_id']}/apply-template",
        json={"template_id": "zzz"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["loc"] == ["body", "template_id"]
    # nas_server on the AP: only missing sensors are added; the disks do not exist
    r = await admin.post(
        f"/api/devices/{created[ids[22]]['device_id']}/apply-template",
        json={"template_id": "nas_server"},
    )
    res = r.json()
    assert [c["name"] for c in res["created"]] == ["br0"]  # br0 is not in IF_EXCLUDE
    sk = {s["name"]: s for s in res["skipped"]}
    assert sk["Disk /"]["reason"] == "probe_failed"
    assert sk["Disk /"]["message"] == "Storage '/' not found"
    assert sk["Ping"]["reason"] == "exists" and sk["eth0"]["reason"] == "exists"

    # a second scan: everything is known now
    r = await admin.post(
        "/api/discovery/scans",
        json={"targets": [lab.range], "group_id": gid, "options": scan_options()},
    )
    second = await wait_scan(admin, r.json()["id"])
    assert (second["found_hosts"], second["new_hosts"]) == (5, 0)
    items = await results(admin, second["id"])
    assert all(i["is_new"] is False and i["existing_device_id"] for i in items)
    # delete a finished scan
    assert (await admin.delete(f"/api/discovery/scans/{scan['id']}")).status_code == 204
    assert (await admin.get(f"/api/discovery/scans/{scan['id']}")).status_code == 404


async def test_scan_with_ping(admin: httpx.AsyncClient, lab: Lab) -> None:
    gid = await default_group_id(admin)
    r = await admin.post(
        "/api/discovery/scans",
        json={
            "targets": [lab.range],
            "group_id": gid,
            "options": {"ping": True, "snmp": False, "tcp": False, "dns": False},
        },
    )
    assert r.status_code == 202, r.text
    done = await wait_scan(admin, r.json()["id"])
    # ICMP to loopback answers for every address
    assert done["found_hosts"] == 11
    items = await results(admin, done["id"])
    assert all(i["ping_ok"] and i["ping_rtt_ms"] is not None for i in items)
    assert all(
        i["device_class"] == "unknown" and i["suggested_template"] == "generic_ping" for i in items
    )


async def test_cancel_and_loop_responsive(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    gid = await default_group_id(admin)
    # nothing answers SNMP here: every host waits for its credential timeouts
    r = await admin.post(
        "/api/discovery/scans",
        json={
            "targets": ["127.0.9.1-250"],
            "group_id": gid,
            "options": {
                "ping": False,
                "tcp": False,
                "snmp_port": 1,
                "snmp_timeout_s": 2,
                "extra_communities": ["a", "b"],
            },
        },
    )
    assert r.status_code == 202, r.text
    scan_id = r.json()["id"]
    lag = 0.0
    for _ in range(10):
        t0 = time.perf_counter()
        await asyncio.sleep(0.05)
        lag = max(lag, time.perf_counter() - t0 - 0.05)
    assert lag < 0.25, f"event loop blocked for {lag:.3f}s"
    # the API stays responsive while the scan runs
    t0 = time.perf_counter()
    assert (await admin.get("/api/health")).status_code == 200
    assert time.perf_counter() - t0 < 1.0
    t0 = time.perf_counter()
    r = await admin.post(f"/api/discovery/scans/{scan_id}/cancel")
    assert time.perf_counter() - t0 < 5.0
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "cancelled" and body["finished_at"] is not None
    assert body["scanned_hosts"] < body["total_hosts"] == 250
    r = await admin.post(f"/api/discovery/scans/{scan_id}/cancel")
    assert r.status_code == 409 and r.json()["error"]["code"] == "scan_not_running"
    assert rt.discovery.busy is False
    # a new scan can start right away
    r = await admin.post(
        "/api/discovery/scans",
        json={
            "targets": ["127.0.9.1"],
            "group_id": gid,
            "options": {"ping": False, "tcp": False, "snmp_port": 1},
        },
    )
    assert r.status_code == 202
    await wait_scan(admin, r.json()["id"])


async def test_scan_validation(admin: httpx.AsyncClient, viewer: httpx.AsyncClient) -> None:
    gid = await default_group_id(admin)
    r = await admin.post("/api/discovery/scans", json={"targets": ["10.0.0.0/16"], "group_id": gid})
    assert r.status_code == 422
    assert r.json()["error"] == {
        "code": "too_many_hosts",
        "message": "The targets expand to 65534 hosts (max 4096)",
        "details": [{"count": 65534, "max": 4096}],
    }
    r = await admin.post(
        "/api/discovery/scans", json={"targets": ["10.0.0.1", "224.0.0.5"], "group_id": gid}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert r.json()["error"]["details"][0]["loc"] == ["body", "targets", 1]
    for bad in (
        {"ping": False, "snmp": False, "tcp": False},
        {"bogus": 1},
        {"tcp_ports": []},
        {"tcp_ports": [80, 80]},
        {"tcp_ports": list(range(1, 34))},
        {"extra_communities": ["x"] * 6},
        {"snmp_timeout_s": 11},
        {"extra_v3": [{"v3_username": "u", "v3_security_level": "authPriv"}]},
    ):
        r = await admin.post(
            "/api/discovery/scans", json={"targets": ["10.0.0.1"], "group_id": gid, "options": bad}
        )
        assert r.status_code == 422, bad
    r = await admin.post("/api/discovery/scans", json={"targets": ["10.0.0.1"], "group_id": 9999})
    assert r.status_code == 422
    r = await admin.post("/api/discovery/scans", json={"targets": [], "group_id": gid})
    assert r.status_code == 422
    # admin only
    assert (await viewer.get("/api/discovery/scans")).status_code == 403
    assert (await viewer.get("/api/discovery/profiles")).status_code == 403
    assert (await viewer.get("/api/device-templates")).status_code == 200
    r = await viewer.get("/api/device-templates/unifi_ap")
    assert r.status_code == 200 and r.json()["priority"] == 80
    assert (await viewer.get("/api/device-templates/nope")).status_code == 404
    assert (await admin.get("/api/discovery/scans/12345")).status_code == 404
    assert (await admin.post("/api/discovery/scans/12345/cancel")).status_code == 404


# ------------------------------------------------------------------ profiles
async def test_profiles_crud(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    gid = await default_group_id(admin)
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    rt.hub.listeners.append(q)
    body = {
        "name": "LAN",
        "targets": ["192.168.1.0/24", "192.168.10.1-30"],
        "group_id": gid,
        "options": {
            "snmp_port": None,
            "extra_communities": ["monitor"],
            "extra_v3": [
                {
                    "v3_username": "mon",
                    "v3_security_level": "authPriv",
                    "v3_auth_key": "authpass1",
                    "v3_priv_key": "privpass1",
                }
            ],
            "tcp_ports": [22, 80, 443, 9100],
        },
        "schedule_enabled": True,
        "schedule_every_hours": 24,
        "notify_new_hosts": True,
        "notify_channel_ids": [],
    }
    r = await admin.post("/api/discovery/profiles", json=body)
    assert r.status_code == 201, r.text
    p = r.json()
    for secret in ("monitor", "authpass1", "privpass1"):
        assert secret not in r.text
    assert p["options"]["extra_communities"] is None
    assert p["options"]["extra_communities_count"] == 1 and p["options"]["extra_v3_count"] == 1
    assert p["options"]["extra_v3"][0] == {
        "v3_username": "mon",
        "v3_security_level": "authPriv",
        "v3_auth_protocol": "SHA",
        "v3_auth_key": None,
        "v3_auth_key_set": True,
        "v3_priv_protocol": "AES128",
        "v3_priv_key": None,
        "v3_priv_key_set": True,
    }
    assert p["options"]["ping"] is True and p["options"]["tcp_ports"] == [22, 80, 443, 9100]
    assert p["next_run_at"] is not None and p["last_run_at"] is None
    assert set(p) == {
        "id",
        "name",
        "targets",
        "group_id",
        "options",
        "schedule_enabled",
        "schedule_every_hours",
        "notify_new_hosts",
        "notify_channel_ids",
        "last_run_at",
        "next_run_at",
        "last_scan_id",
        "created_at",
        "updated_at",
    }
    assert (
        await admin.post("/api/discovery/profiles", json={**body, "name": "lan"})
    ).status_code == 409
    r = await admin.post("/api/discovery/profiles", json={**body, "name": "x", "group_id": 999})
    assert r.status_code == 422
    r = await admin.post(
        "/api/discovery/profiles", json={**body, "name": "x", "notify_channel_ids": [77]}
    )
    assert r.status_code == 422
    r = await admin.post(
        "/api/discovery/profiles", json={**body, "name": "x", "targets": ["10.0.0.0/8"]}
    )
    assert r.json()["error"]["code"] == "too_many_hosts"
    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    rt.hub.listeners.remove(q)
    assert {
        "type": "config.changed",
        "admin_only": False,
        "data": {"entity": "discovery_profile", "id": p["id"], "action": "created"},
    } in msgs
    # PATCH: options merge keeps secrets; schedule off clears next_run_at
    r = await admin.patch(
        f"/api/discovery/profiles/{p['id']}",
        json={"options": {"dns": False}, "schedule_enabled": False},
    )
    assert r.status_code == 200, r.text
    p2 = r.json()
    assert p2["options"]["dns"] is False and p2["options"]["extra_communities_count"] == 1
    assert p2["options"]["tcp_ports"] == [22, 80, 443, 9100] and p2["next_run_at"] is None
    async with rt.sessionmaker() as session:
        stored = (
            await session.execute(
                text("SELECT options FROM discovery_profiles WHERE id = :id"), {"id": p["id"]}
            )
        ).scalar_one()
    assert stored["extra_communities"] == ["monitor"]
    assert stored["extra_v3"][0]["v3_priv_key"] == "privpass1"
    r = await admin.patch(
        f"/api/discovery/profiles/{p['id']}",
        json={"options": {"extra_communities": [], "extra_v3": None}, "schedule_enabled": True},
    )
    assert r.json()["options"]["extra_communities_count"] == 0
    assert r.json()["options"]["extra_v3_count"] == 1 and r.json()["next_run_at"] is not None
    r = await admin.patch(f"/api/discovery/profiles/{p['id']}", json={"targets": ["239.0.0.1"]})
    assert r.status_code == 422
    r = await admin.get("/api/discovery/profiles")
    assert r.json()["total"] == 1 and r.json()["items"][0]["name"] == "LAN"
    assert (await admin.delete(f"/api/discovery/profiles/{p['id']}")).status_code == 204
    assert (await admin.get(f"/api/discovery/profiles/{p['id']}")).status_code == 404


async def test_profile_run_and_scheduler(rt, admin: httpx.AsyncClient, lab: Lab) -> None:  # type: ignore[no-untyped-def]
    gid = await default_group_id(admin)
    sent: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def fake_sender(channel, payload, smtp, lang):  # type: ignore[no-untyped-def]
        sent.append((channel, payload))

    rt.alerts.sender = fake_sender
    r = await admin.post(
        "/api/notification-channels",
        json={"name": "hook", "type": "webhook", "config": {"url": "http://127.0.0.1:9/x"}},
    )
    cid = r.json()["id"]
    r = await admin.post(
        "/api/discovery/profiles",
        json={
            "name": "Lab",
            "targets": [lab.ip(21)],
            "group_id": gid,
            "options": scan_options(tcp=False),
            "schedule_enabled": True,
            "schedule_every_hours": 6,
            "notify_new_hosts": True,
            "notify_channel_ids": [cid],
        },
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    sched = DiscoveryScheduler(rt)

    async def make_due() -> None:
        async with rt.sessionmaker() as session:
            await session.execute(
                text("UPDATE discovery_profiles SET next_run_at = now() - interval '1 minute'")
            )
            await session.commit()

    # not due yet (next_run_at = now + 1 minute)
    assert await sched.run_due() == []
    await make_due()
    started = await sched.run_due()
    assert len(started) == 1
    first = await wait_scan(admin, started[0])
    await rt.drain()
    assert first["trigger"] == "scheduled" and first["created_by"] is None
    assert first["profile_id"] == pid and first["profile_name"] == "Lab"
    assert first["new_hosts"] == 1
    assert sent == []  # baseline run: no new-host notification
    p = (await admin.get(f"/api/discovery/profiles/{pid}")).json()
    assert p["last_scan_id"] == first["id"] and p["last_run_at"] is not None
    nxt = p["next_run_at"]
    # next run ~6 h later
    async with rt.sessionmaker() as session:
        delta = (
            await session.execute(
                text("SELECT extract(epoch FROM next_run_at - now()) FROM discovery_profiles")
            )
        ).scalar_one()
    assert 5.9 * 3600 < float(delta) <= 6 * 3600, nxt
    r = await admin.get("/api/events", params={"kind": "discovery"})
    assert r.json()["total"] == 0

    # second run with one more host: only the new one is notified
    await admin.patch(
        f"/api/discovery/profiles/{pid}",
        # .30 does not answer: SNMP timeouts keep the scan running for ~2 s
        json={"targets": [lab.ip(21), lab.ip(22), lab.ip(30)]},
    )
    await make_due()
    started = await sched.run_due()
    second = await wait_scan(admin, started[0])
    await rt.drain()
    assert (second["found_hosts"], second["new_hosts"]) == (2, 1)
    assert len(sent) == 1
    channel, payload = sent[0]
    assert channel["id"] == cid
    assert payload["event"] == "discovery.new_hosts"
    assert payload["message"] == 'Discovery "Lab": 1 new host'
    assert payload["hosts"] == [
        {"ip": lab.ip(22), "hostname": None, "device_class": "access_point", "vendor": "ubiquiti"}
    ]
    assert payload["profile"] == {"id": pid, "name": "Lab"} and payload["scan_id"] == second["id"]
    assert payload["group"]["id"] == gid and payload["url"] is None
    assert set(payload) == {
        "event",
        "event_id",
        "time",
        "old_status",
        "new_status",
        "message",
        "sensor",
        "device",
        "group",
        "rule",
        "profile",
        "scan_id",
        "hosts",
        "url",
    }
    r = await admin.get("/api/events", params={"kind": "discovery"})
    ev = r.json()["items"]
    assert len(ev) == 1 and ev[0]["id"] == payload["event_id"]
    assert ev[0]["message"] == f'Discovery "Lab": 1 new host: {lab.ip(22)} (access_point, ubiquiti)'
    assert ev[0]["sensor_id"] is None and ev[0]["device_id"] is None

    # a manual run of the profile
    r = await admin.post(f"/api/discovery/profiles/{pid}/run")
    assert r.status_code == 202 and r.json()["trigger"] == "manual"
    assert r.json()["profile_id"] == pid
    # busy: a due profile is postponed by 10 minutes
    await make_due()
    assert await sched.run_due() == []
    async with rt.sessionmaker() as session:
        delta = (
            await session.execute(
                text("SELECT extract(epoch FROM next_run_at - now()) FROM discovery_profiles")
            )
        ).scalar_one()
    assert 9 * 60 < float(delta) <= 10 * 60
    r2 = await admin.post(f"/api/discovery/profiles/{pid}/run")
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "scan_running"
    await wait_scan(admin, r.json()["id"])
    await rt.drain()
    assert len(sent) == 1  # manual runs never notify
    # deleting the profile keeps its scans
    assert (await admin.delete(f"/api/discovery/profiles/{pid}")).status_code == 204
    scans = (await admin.get("/api/discovery/scans")).json()["items"]
    assert len(scans) == 3 and all(s["profile_id"] is None for s in scans)
    assert scans[0]["profile_name"] == "Lab"


# ------------------------------------------------------------------ retention / restart
async def test_retention_and_restart(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    async with rt.sessionmaker() as session:
        for i in range(25):
            await session.execute(
                text(
                    "INSERT INTO discovery_scans (trigger, status, targets, total_hosts, "
                    "started_at, finished_at) VALUES ('manual', 'completed', '{10.0.0.1}', 1, "
                    "now() - make_interval(mins => :m), now())"
                ),
                {"m": 100 - i},
            )
        await session.execute(
            text(
                "INSERT INTO discovery_results (scan_id, ip, ping_ok, snmp_ok, device_class, "
                "suggested_template, is_new) VALUES (1, '10.0.0.1', true, false, 'unknown', "
                "'generic_ping', true)"
            )
        )
        await session.commit()
    removed = await rt.discovery.prune()
    assert removed == 5
    r = await admin.get("/api/discovery/scans", params={"page_size": 100})
    ids = [s["id"] for s in r.json()["items"]]
    assert ids == list(range(25, 5, -1))
    async with rt.sessionmaker() as session:
        n = (await session.execute(text("SELECT count(*) FROM discovery_results"))).scalar_one()
        assert n == 0  # results cascade
        # a scan left running by a crash
        await session.execute(
            text(
                "INSERT INTO discovery_scans (trigger, status, targets, total_hosts) "
                "VALUES ('manual', 'running', '{10.0.0.1}', 1)"
            )
        )
        await session.commit()
    # the partial unique index allows only one running scan
    async with rt.sessionmaker() as session:
        with pytest.raises(Exception):  # noqa: B017
            await session.execute(
                text(
                    "INSERT INTO discovery_scans (trigger, status, targets, total_hosts) "
                    "VALUES ('manual', 'running', '{10.0.0.2}', 1)"
                )
            )
            await session.commit()
    gid = await default_group_id(admin)
    r = await admin.post("/api/discovery/scans", json={"targets": ["10.0.0.9"], "group_id": gid})
    assert r.status_code == 409 and r.json()["error"]["code"] == "scan_running"
    assert r.json()["error"]["details"] == [{"scan_id": 26}]
    assert await rt.discovery.prune(keep=1) == 20  # the running one (newest) is kept
    assert await rt.discovery.mark_interrupted() == 1
    r = await admin.get("/api/discovery/scans/26")
    assert r.json()["status"] == "failed" and r.json()["error"] == "Interrupted by restart"
    assert r.json()["finished_at"] is not None


async def test_scan_run_due_skips_invalid(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    """A due profile whose stored targets became invalid does not break the loop."""
    gid = await default_group_id(admin)
    async with rt.sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO discovery_profiles (name, targets, group_id, options, "
                "schedule_enabled, next_run_at) VALUES ('bad', '{224.0.0.1}', :g, '{}', true, "
                ":t)"
            ),
            {"g": gid, "t": now_utc() - timedelta(minutes=1)},
        )
        await session.commit()
    assert await DiscoveryScheduler(rt).run_due() == []
    p = (await admin.get("/api/discovery/profiles")).json()["items"][0]
    assert p["next_run_at"] is not None and p["last_run_at"] is None
