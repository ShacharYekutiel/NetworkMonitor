"""[2b-2] The new vendor templates against snmpsim.

1. Synthetic agents (127.0.7.80+): a generator builds one recording from each template's own
   OIDs (scalars get a value that passes probes / `when`, tables get rows with filter-passing
   values, label columns get text); applying the template must leave no entry skipped as
   probe_failed / no_rows / snmp_error / not_applicable (mechanics only, not OID correctness).
2. Check Point appliance (.71), ClusterXL member VM (.72) and Aruba Instant (.73) as in
   architecture.md § 11.6: classification, created / skipped / `when` / not_applicable and the
   resulting statuses."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from netmon.templates.builtin import get_template
from tests.conftest import make_device, requires_db
from tests.snmpsim_agents import (
    FIXED,
    RAM,
    Record,
    host_resources,
    interfaces,
    start_agent,
    system,
)

pytestmark = requires_db

NEW = [
    "aruba_cx",
    "aruba_wifi",
    "checkpoint_gaia",
    "fortinet_fortigate",
    "cisco_ios",
    "paloalto_panos",
    "mikrotik_routeros",
    "apc_ups",
    "hpe_ilo",
    "dell_idrac",
]
BAD = {"probe_failed", "no_rows", "snmp_error", "not_applicable"}


def _free_udp_port(ip: str) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((ip, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _value(config: dict[str, Any]) -> tuple[int | str, str]:
    """A probe-passing value for an entry config -> (value, snmprec type)."""
    vt = config.get("value_type", "gauge")
    if vt == "enum":
        return int(next(iter(config["value_map"]))), "2"
    if vt == "string":
        return str(config.get("expected") or "active"), "4"
    return 40, "2"


def synthetic_records(template: dict[str, Any]) -> list[Record]:
    recs: dict[str, tuple[str, Any]] = {}

    def put(oid: str, typ: Any, v: Any) -> None:
        recs.setdefault(oid, (typ, v))

    # filter OID -> required values per row group (several entries may filter one column)
    groups: dict[str, list[int]] = {}
    for e in template["entries"]:
        for f in e.get("filters", []):
            vals = groups.setdefault(f["oid"], [])
            if f["in"]:
                if f["in"][0] not in vals:
                    vals.append(f["in"][0])
    n_groups = max([len(v) for v in groups.values()] + [1])
    suffixes = [str(i) for i in range(1, 2 * n_groups + 1)]
    for e in template["entries"]:
        if e["kind"] == "sensor":
            cfg = e["config"]
            if e["type"] == "snmp_custom":
                v, t = _value(cfg)
                put(cfg["oid"], t, v)
                if cfg.get("percent_of_oid"):
                    put(cfg["percent_of_oid"], "2", 100)
            elif e["type"] == "snmp_walk_count":
                v = (cfg.get("count_values") or [1])[0]
                for s in ("1", "2"):
                    put(f"{cfg['oid']}.{s}", "2", v)
            for f in e["when"]:
                put(f["oid"], "2", (f["in"] or [1])[0])
        elif e["kind"] == "snmp_table":
            v, t = _value(e["config"])
            for s in suffixes:
                put(f"{e['walk_oid']}.{s}", t, v)
                if e["percent_of_column"]:
                    put(f"{e['percent_of_column']}.{s}", "2", 100)
                for col in (e["label_oid"], e["label2_oid"]):
                    if col:
                        put(f"{col}.{s}", "4", f"row{s}")
            for f in e["filters"]:
                for i, s in enumerate(suffixes):
                    if f["in"]:
                        vals = groups[f["oid"]]
                        want = vals[(i // 2) % len(vals)]
                    else:
                        want = next(x for x in range(1, 100) if x not in (f["not_in"] or []))
                    if f["oid"] == e["walk_oid"]:
                        continue  # the walked column already has a passing value
                    put(f"{f['oid']}.{s}", "2", want)
    out: list[Record] = [(o, t, str(v)) for o, (t, v) in recs.items()]
    out += system("Synthetic", "1.3.6.1.4.1.99999.1", template["id"])
    out += interfaces([(1, "eth0", 6, 1)])
    out += host_resources(
        [
            (1, RAM, "Physical memory", 1024, 1000, 400),
            (31, FIXED, "/", 4096, 1000, 100),
            (32, FIXED, "/var/log", 4096, 1000, 100),
        ]
    )
    unique: dict[str, Record] = {}
    for r in out:
        unique.setdefault(r[0], r)  # the template's own value wins
    return list(unique.values())


@pytest.fixture(scope="module")
def synthetic() -> Iterator[dict[str, Any]]:
    agents: dict[str, Any] = {}
    for i, tid in enumerate(NEW):
        t = get_template(tid)
        assert t is not None
        ip = f"127.0.7.{80 + i}"
        a = start_agent(ip, _free_udp_port(ip), {"public": synthetic_records(t)})
        if a is None:
            for x in agents.values():
                x.stop()
            pytest.skip("snmpsim not available")
        agents[tid] = a
    yield agents
    for a in agents.values():
        a.stop()


async def _apply(admin: httpx.AsyncClient, agent: Any, tid: str, name: str) -> dict[str, Any]:
    dev = await make_device(
        admin,
        name=name,
        host=agent.ip,
        snmp_credentials={"port": agent.port, "timeout_s": 1, "retries": 0},
    )
    r = await admin.post(f"/api/devices/{dev['id']}/apply-template", json={"template_id": tid})
    assert r.status_code == 200, r.text
    return {"device": dev, **r.json()}


@pytest.mark.parametrize("tid", NEW)
async def test_synthetic_template_mechanics(tid: str, admin: httpx.AsyncClient, synthetic) -> None:  # type: ignore[no-untyped-def]
    res = await _apply(admin, synthetic[tid], tid, tid)
    bad = [s for s in res["skipped"] if s["reason"] in BAD and s["type"] != "http"]
    assert bad == [], bad
    assert res["created"], res
    template = get_template(tid)
    assert template is not None
    n_sensor = sum(1 for e in template["entries"] if e["kind"] == "sensor" and e["type"] != "http")
    types = [c["type"] for c in res["created"]]
    assert len(types) >= n_sensor
    # every configured sensor really runs ok against the agent
    for c in res["created"]:
        if c["type"] == "http":
            continue
        r = await admin.post(f"/api/sensors/{c['sensor_id']}/run")
        assert r.status_code == 200 and r.json()["status"] != "unknown", (c, r.json())


# ------------------------------------------------------------------ realistic sims
CP = "1.3.6.1.4.1.2620"


def _cp_common(proc: int) -> list[Record]:
    return [
        (f"{CP}.1.6.7.2.4.0", 66, str(proc)),
        (f"{CP}.1.6.7.4.3.0", 70, "16000000000"),
        (f"{CP}.1.6.7.4.4.0", 70, "4000000000"),
        (f"{CP}.1.1.25.3.0", 66, "12345"),
        (f"{CP}.1.1.25.10.0", 66, "0"),
    ]


def checkpoint_records() -> list[Record]:
    recs = system(
        "Linux gw-hq 3.10.0-1160.15.2cpx86_64 #1 SMP Tue Jan 1 00:00:00 IST 2024 x86_64",
        f"{CP}.1.6.123.1.49",
        "gw-hq",
    )
    recs += _cp_common(23)
    recs += [(f"{CP}.1.5.2.0", 2, "0")]
    recs += [(f"{CP}.500.9002.1.3.{i}", 2, str(v)) for i, v in ((1, 3), (2, 3), (3, 131))]
    recs += [
        (f"{CP}.1.6.7.8.1.1.2.1", 4, "CPU"),
        (f"{CP}.1.6.7.8.1.1.2.2", 4, "Intake"),
        (f"{CP}.1.6.7.8.1.1.3.1", 4, "44.0"),
        (f"{CP}.1.6.7.8.1.1.3.2", 4, "38.5"),
        (f"{CP}.1.6.7.8.2.1.2.1", 4, "Fan 1"),
        (f"{CP}.1.6.7.8.2.1.2.2", 4, "Fan 2"),
        (f"{CP}.1.6.7.8.2.1.6.1", 2, "0"),
        (f"{CP}.1.6.7.8.2.1.6.2", 2, "1"),
        (f"{CP}.1.6.7.9.1.1.1.1", 2, "1"),
        (f"{CP}.1.6.7.9.1.1.1.2", 2, "2"),
        (f"{CP}.1.6.7.9.1.1.2.1", 4, "Up"),
        (f"{CP}.1.6.7.9.1.1.2.2", 4, "Down"),
    ]
    recs += host_resources(
        [
            (1, RAM, "Physical memory", 1024, 1000, 400),
            (31, FIXED, "/", 4096, 1000, 100),
            (32, FIXED, "/var/log", 4096, 1000, 100),
        ]
    )
    recs += interfaces([(1, "eth0", 6, 1), (2, "eth1", 6, 1), (3, "eth2", 6, 2), (4, "lo", 24, 1)])
    return recs


def checkpoint_cluster_records() -> list[Record]:
    recs = system("Linux gw-b 3.10.0-1160 #1 SMP x86_64", "1.3.6.1.4.1.8072.3.2.10", "gw-b")
    recs += _cp_common(41)
    recs += [
        (f"{CP}.1.5.2.0", 2, "1"),
        (f"{CP}.1.5.6.0", 4, "active"),
        (f"{CP}.1.5.101.0", 2, "0"),
    ]
    recs += host_resources([(1, RAM, "Physical memory", 1024, 1000, 400)])
    recs += interfaces([(1, "eth0", 6, 1)])
    return recs


AI = "1.3.6.1.4.1.14823.2.3.3.1.2"


def aruba_instant_records() -> list[Record]:
    recs = system(
        "ArubaOS (MODEL: 515), Version 8.10.0.6", "1.3.6.1.4.1.14823.1.2.111", "instant-vc"
    )
    for mac, name, status in (
        ("0.11.134.1.1.1", "AP-Lobby", 1),
        ("0.11.134.1.1.2", "AP-Office", 1),
        ("0.11.134.1.1.3", "AP-Store", 2),
    ):
        recs += [(f"{AI}.1.1.2.{mac}", 4, name), (f"{AI}.1.1.11.{mac}", 2, str(status))]
    recs += [(f"{AI}.4.1.1.{i}.0.0.0.0.{i}", "4x", f"00000000000{i}") for i in range(1, 6)]
    recs += interfaces([(1, "bond0", 6, 1), (2, "eth0", 6, 1)])
    return recs


@pytest.fixture(scope="module")
def vendor_agents() -> Iterator[dict[str, Any]]:
    agents: dict[str, Any] = {}
    for key, ip, recs in (
        ("checkpoint", "127.0.7.71", checkpoint_records()),
        ("cluster", "127.0.7.72", checkpoint_cluster_records()),
        ("instant", "127.0.7.73", aruba_instant_records()),
    ):
        a = start_agent(ip, _free_udp_port(ip), {"public": recs})
        if a is None:
            for x in agents.values():
                x.stop()
            pytest.skip("snmpsim not available")
        agents[key] = a
    yield agents
    for a in agents.values():
        a.stop()


async def _identify_apply(admin: httpx.AsyncClient, agent: Any, name: str) -> tuple[dict, dict]:
    dev = await make_device(
        admin,
        name=name,
        host=agent.ip,
        snmp_credentials={"port": agent.port, "timeout_s": 1, "retries": 0},
    )
    ident = (await admin.post(f"/api/devices/{dev['id']}/identify")).json()
    r = await admin.post(
        f"/api/devices/{dev['id']}/apply-template",
        json={"template_id": ident["suggested_template"]},
    )
    assert r.status_code == 200, r.text
    return ident, r.json()


async def _statuses(admin: httpx.AsyncClient, created: list[dict]) -> dict[str, dict]:
    out = {}
    for c in created:
        if c["type"] == "http":
            continue
        out[c["name"]] = (await admin.post(f"/api/sensors/{c['sensor_id']}/run")).json()
    return out


async def test_checkpoint_appliance(admin: httpx.AsyncClient, vendor_agents) -> None:  # type: ignore[no-untyped-def]
    ident, res = await _identify_apply(admin, vendor_agents["checkpoint"], "gw-hq")
    assert (
        ident["device_class"],
        ident["vendor"],
        ident["classified_by"],
        ident["confidence"],
    ) == (
        "router",
        "checkpoint",
        "snmp",
        "high",
    )
    assert ident["suggested_template"] == "checkpoint_gaia"
    names = [c["name"] for c in res["created"]]
    assert names == [
        "Ping",
        "System",
        "CPU",
        "Memory",
        "Concurrent connections",
        "Connection table usage",
        "VPN tunnels up",
        "Temperature CPU",
        "Temperature Intake",
        "Fan 1",
        "Fan 2",
        "Power supply 1",
        "Power supply 2",
        "Disk /",
        "Disk /var/log",
        "eth0",
        "eth1",
    ]
    skipped = {s["name"]: s for s in res["skipped"]}
    assert skipped["ClusterXL state"]["reason"] == "not_applicable"
    assert skipped["ClusterXL state"]["message"] == "Condition not met: 1.3.6.1.4.1.2620.1.5.2.0"
    assert skipped["ClusterXL status"]["reason"] == "not_applicable"
    st = await _statuses(admin, res["created"])

    def val(name: str, key: str = "value") -> Any:
        return next(c["value"] for c in st[name]["channels"] if c["key"] == key)

    assert (st["CPU"]["status"], val("CPU")) == ("up", 23)
    assert (st["Memory"]["status"], val("Memory")) == ("up", 25)
    assert val("Concurrent connections") == 12345
    assert st["Connection table usage"]["status"] == "up" and val("Connection table usage") is None
    vpn = st["VPN tunnels up"]
    assert vpn["status"] == "warning"
    assert (val("VPN tunnels up", "rows"), val("VPN tunnels up", "matched")) == (3, 2)
    assert val("VPN tunnels up", "unmatched") == 1
    assert val("Temperature CPU") == 44 and val("Temperature Intake") == 38.5
    assert (st["Fan 1"]["status"], st["Fan 2"]["status"]) == ("up", "down")
    assert (st["Power supply 1"]["status"], st["Power supply 2"]["status"]) == ("up", "down")


async def test_checkpoint_cluster_member(admin: httpx.AsyncClient, vendor_agents) -> None:  # type: ignore[no-untyped-def]
    ident, res = await _identify_apply(admin, vendor_agents["cluster"], "gw-b")
    assert (ident["device_class"], ident["vendor"], ident["suggested_template"]) == (
        "router",
        "checkpoint",
        "checkpoint_gaia",
    )
    names = [c["name"] for c in res["created"]]
    assert {"CPU", "ClusterXL state", "ClusterXL status"} <= set(names)
    skipped = {s["name"]: s for s in res["skipped"]}
    assert (skipped["VPN tunnels up"]["reason"], skipped["VPN tunnels up"]["message"]) == (
        "probe_failed",
        "No rows",
    )
    assert skipped["Temperature"]["reason"] == "no_rows"
    st = await _statuses(admin, res["created"])
    assert st["ClusterXL state"]["status"] == "up"
    assert st["ClusterXL state"]["message"] == "ClusterXL: active"
    assert st["ClusterXL status"]["status"] == "up"
    assert st["ClusterXL status"]["message"] == "ClusterXL: OK (0)"
    assert next(c["value"] for c in st["CPU"]["channels"]) == 41


async def test_aruba_instant(admin: httpx.AsyncClient, vendor_agents) -> None:  # type: ignore[no-untyped-def]
    ident, res = await _identify_apply(admin, vendor_agents["instant"], "instant-vc")
    # [2b-2 fix HIGH-4] enterprise 14823: ArubaOS is wireless unless a Mobility Access Switch
    assert (ident["device_class"], ident["vendor"], ident["suggested_template"]) == (
        "access_point",
        "aruba",
        "aruba_wifi",
    )
    names = [c["name"] for c in res["created"]]
    assert names[:7] == [
        "Ping",
        "System",
        "APs up",
        "AP AP-Lobby",
        "AP AP-Office",
        "AP AP-Store",
        "Wi-Fi clients",
    ]
    skipped = [(s["name"], s["reason"]) for s in res["skipped"]]
    assert ("CPU Load", "probe_failed") in skipped and ("Memory", "probe_failed") in skipped
    assert ("APs up", "probe_failed") in skipped  # the controller row
    assert ("AP", "no_rows") in skipped
    assert ("Wi-Fi clients", "probe_failed") in skipped
    st = await _statuses(admin, res["created"])
    aps = {c["key"]: c["value"] for c in st["APs up"]["channels"]}
    assert st["APs up"]["status"] == "warning"  # message = the breach "unmatched 1 > 0"
    assert aps == {"rows": 3, "matched": 2, "unmatched": 1}
    assert (st["AP AP-Lobby"]["status"], st["AP AP-Store"]["status"]) == ("up", "down")
    clients = st["Wi-Fi clients"]
    assert next(c["value"] for c in clients["channels"] if c["key"] == "matched") == 5
