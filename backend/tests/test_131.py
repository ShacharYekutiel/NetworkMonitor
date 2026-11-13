"""[1.3.1] IRF physical ports are important (HH3C-STACK-MIB hh3cStackPhyPortInfoBelongtoPort in
the port inventory run, `switch_ports.irf_port`, importance reason `irf`), the IRF role label
(rename of the 1.3.0 names: tests/test_13_apply.py) and migration 0009.

Unit tests need no database; the snmpsim tests use agents on 127.0.31.x."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest

from netmon.poller.snmp_client import SnmpError
from netmon.ports.collect import (
    ENT_ALIAS_MAPPING_ID,
    ENT_PHYSICAL_NAME,
    IRF_PHY_PORT_BELONGTO,
    IrfWalk,
    PortWalks,
    collect_irf,
    map_irf_ports,
    parse_alias_mapping,
    parse_irf_belongto,
)
from netmon.ports.importance import REASONS, DeviceImportance, default_interface_impact
from netmon.ports.snapshot import ALL_SOURCES, build_snapshot, skipped_sources
from tests.conftest import migration_head, requires_db

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------ pure parsers
def test_parse_irf_belongto() -> None:
    col = {"212": 1, "213": 2, "214": 0, "215": b"1", "x.y": 1, "216": None, "1.2": 1}
    assert parse_irf_belongto(col) == {212: 1, 213: 2, 215: 1}
    assert parse_irf_belongto({}) == {}


def test_parse_alias_mapping() -> None:
    col = {
        "1001.0": "1.3.6.1.2.1.2.2.1.1.61",
        "1002.3": "1.3.6.1.2.1.2.2.1.1.70",
        "1002.0": ".1.3.6.1.2.1.2.2.1.1.62",  # logical 0 wins
        "1003.1": "1.3.6.1.2.1.31.1.1.1.1.5",  # not an ifIndex mapping
        "1004.0": "1.3.6.1.2.1.2.2.1.1.0",
        "1005": "1.3.6.1.2.1.2.2.1.1.9",  # malformed suffix
        "1006.2": b"1.3.6.1.2.1.2.2.1.1.66",
    }
    assert parse_alias_mapping(col) == {1001: 61, 1002: 62, 1006: 66}


def test_map_irf_ports() -> None:
    ports = {
        61: ("FortyGigE1/1/1", "FortyGigE1/1/1 Interface"),
        62: ("FortyGigE1/1/2", "FortyGigE1/1/2 Interface"),
        161: ("", "FortyGigE2/1/1"),
        49: ("Ten-GigabitEthernet1/0/49", "Ten-GigabitEthernet1/0/49 Interface"),
        7: ("dup", ""),
        8: ("dup", ""),
    }
    irf = IrfWalk(
        ports={1001: 1, 1002: 2, 2001: 1, 3001: 2, 4001: 1, 5001: 2},
        alias={1001: 61, 5001: 999},  # 999: no such ifIndex -> name fallback
        names={
            1002: "fortygige1/1/2",  # case-insensitive
            2001: "FortyGigE2/1/1",  # by descr (the name is empty)
            3001: "dup",  # ambiguous -> not mapped
            5001: "FortyGigE1/1/1",
        },
    )
    assert map_irf_ports(irf, ports) == {61: 2, 62: 2, 161: 1}
    assert map_irf_ports(IrfWalk(), ports) == {}


# ------------------------------------------------------------------ collect_irf (fake walker)
class FakeWalker:
    def __init__(self, cols: dict[str, Any]) -> None:
        self.cols = cols
        self.calls: list[str] = []

    async def __call__(self, oid: str, cap: int) -> dict[str, Any]:
        self.calls.append(oid)
        v = self.cols.get(oid, {})
        if isinstance(v, Exception):
            raise v
        return dict(v)


async def test_collect_irf_non_comware_is_one_walk() -> None:
    w = FakeWalker({})
    walks = PortWalks()
    await collect_irf(w, walks)
    assert w.calls == [IRF_PHY_PORT_BELONGTO]
    assert walks.sources["irf"] == "empty" and walks.irf is not None and walks.irf.ports == {}
    # only non-IRF rows (belongtoPort 0): empty as well, no entity walks
    w = FakeWalker({IRF_PHY_PORT_BELONGTO: {"300": 0, "301": 0}})
    walks = PortWalks()
    await collect_irf(w, walks)
    assert w.calls == [IRF_PHY_PORT_BELONGTO] and walks.sources["irf"] == "empty"


async def test_collect_irf_alias_and_names() -> None:
    w = FakeWalker(
        {
            IRF_PHY_PORT_BELONGTO: {"1001": 1, "1002": 2, "1003": 0},
            ENT_ALIAS_MAPPING_ID: {"1001.0": "1.3.6.1.2.1.2.2.1.1.61"},
            ENT_PHYSICAL_NAME: {"1001": b"FortyGigE1/1/1", "1002": b"FortyGigE1/1/2", "5": b"x"},
        }
    )
    walks = PortWalks()
    await collect_irf(w, walks)
    assert w.calls == [IRF_PHY_PORT_BELONGTO, ENT_ALIAS_MAPPING_ID, ENT_PHYSICAL_NAME]
    assert walks.sources["irf"] == "ok"
    assert walks.irf == IrfWalk(
        ports={1001: 1, 1002: 2},
        alias={1001: 61},
        names={1001: "FortyGigE1/1/1", 1002: "FortyGigE1/1/2"},
    )
    # every IRF entity has an alias mapping: entPhysicalName is not read
    w = FakeWalker(
        {
            IRF_PHY_PORT_BELONGTO: {"1001": 1},
            ENT_ALIAS_MAPPING_ID: {"1001.0": "1.3.6.1.2.1.2.2.1.1.61"},
        }
    )
    walks = PortWalks()
    await collect_irf(w, walks)
    assert w.calls == [IRF_PHY_PORT_BELONGTO, ENT_ALIAS_MAPPING_ID]
    # the alias walk fails: the names are used
    w = FakeWalker(
        {
            IRF_PHY_PORT_BELONGTO: {"1001": 1},
            ENT_ALIAS_MAPPING_ID: SnmpError("timeout"),
            ENT_PHYSICAL_NAME: {"1001": b"FortyGigE1/1/1"},
        }
    )
    walks = PortWalks()
    await collect_irf(w, walks)
    assert walks.sources["irf"] == "ok" and walks.irf is not None
    assert walks.irf.names == {1001: "FortyGigE1/1/1"}


async def test_collect_irf_errors() -> None:
    walks = PortWalks()
    await collect_irf(FakeWalker({IRF_PHY_PORT_BELONGTO: SnmpError("x")}), walks)
    assert walks.sources["irf"] == "error" and walks.irf is None
    walks = PortWalks()
    await collect_irf(
        FakeWalker({IRF_PHY_PORT_BELONGTO: {"1001": 1}, ENT_PHYSICAL_NAME: SnmpError("x")}), walks
    )
    assert walks.sources["irf"] == "error" and walks.irf is None


# ------------------------------------------------------------------ snapshot
def _walks(irf: IrfWalk | None, state: str | None) -> PortWalks:
    w = PortWalks()
    w.if_cols = {
        "descr": {1: b"GigabitEthernet1/0/1", 61: b"FortyGigE1/1/1", 62: b"FortyGigE1/1/2"},
        "type": {1: 6, 61: 6, 62: 6},
    }
    w.ifx_cols = {
        "name": {1: b"GigabitEthernet1/0/1", 61: b"FortyGigE1/1/1", 62: b"FortyGigE1/1/2"}
    }
    w.sources = {"if_xtable": "ok"}
    if state is not None:
        w.sources["irf"] = state
    w.irf = irf
    return w


def test_snapshot_irf() -> None:
    irf = IrfWalk(ports={1001: 1, 1002: 2}, alias={1001: 61}, names={1002: "FortyGigE1/1/2"})
    snap = build_snapshot({}, _walks(irf, "ok"), NOW)
    assert {r["if_index"]: r["irf_port"] for r in snap.rows} == {1: None, 61: 1, 62: 2}
    assert snap.sources["irf"] == "ok"
    prev = {r["if_index"]: {**r, "collected_at": NOW - timedelta(minutes=5)} for r in snap.rows}
    # the walk failed: the previous values are kept
    snap2 = build_snapshot(prev, _walks(None, "error"), NOW)
    assert {r["if_index"]: r["irf_port"] for r in snap2.rows} == {1: None, 61: 1, 62: 2}
    assert snap2.sources["irf"] == "error"
    # not read at all (e.g. an older runner): kept as well, source skipped
    snap3 = build_snapshot(prev, _walks(None, None), NOW)
    assert {r["if_index"]: r["irf_port"] for r in snap3.rows} == {1: None, 61: 1, 62: 2}
    assert snap3.sources["irf"] == "skipped"
    # the stack was dissolved (no IRF rows any more): cleared
    snap4 = build_snapshot(prev, _walks(IrfWalk(), "empty"), NOW)
    assert all(r["irf_port"] is None for r in snap4.rows) and snap4.sources["irf"] == "empty"
    # IRF rows that match no port: unmapped
    snap5 = build_snapshot(prev, _walks(IrfWalk(ports={9: 1}, names={9: "nope"}), "ok"), NOW)
    assert all(r["irf_port"] is None for r in snap5.rows) and snap5.sources["irf"] == "unmapped"


def test_sources_include_irf() -> None:
    assert "irf" in ALL_SOURCES and skipped_sources()["irf"] == "skipped"


# ------------------------------------------------------------------ importance
def _row(idx: int, **kw: Any) -> dict[str, Any]:
    return {
        "if_index": idx,
        "kind": "physical",
        "mac_count": 0,
        "mode": None,
        "tagged_vlans": [],
        "lag_if_index": None,
        **kw,
    }


def test_importance_irf_reason() -> None:
    from netmon.ports.importance import port_reasons

    assert REASONS == ("uplink", "lag", "trunk", "irf")
    rows = [
        _row(1),
        _row(61, irf_port=1),
        _row(62, irf_port=2, mode="trunk", tagged_vlans=[10, 20]),
        _row(5, irf_port=None),
    ]
    reasons = port_reasons(rows, lambda r: [], 10, 2)
    assert reasons == {1: [], 61: ["irf"], 62: ["trunk", "irf"], 5: []}
    imp = DeviceImportance(reasons)
    # irf stays important when trunks are excluded (link-down scope)
    assert imp.important(62, include_trunks=False) and imp.important(61, include_trunks=False)
    assert imp.important_set(include_trunks=False) == {61, 62}
    assert default_interface_impact(imp, 61) == "critical"
    assert default_interface_impact(imp, 1) == "warning"


# ------------------------------------------------------------------ template option
def test_rename_from_validation() -> None:
    from netmon.templates.library import TemplateError, _table_entry

    base = {
        "kind": "snmp_table",
        "walk_oid": "1.3.6.1.4.1.25506.2.91.3.1.1",
        "name": {"en": "IRF role member {label}"},
        "config": {"value_type": "gauge"},
        "confidence": "high",
    }
    assert "rename_from" not in _table_entry(dict(base), "e")
    out = _table_entry({**base, "rename_from": {"name": {"en": "IRF role {label}"}}}, "e")
    assert out["rename_from"] == {
        "name": {"en": "IRF role {label}", "he": "IRF role {label}"},
        "label_oid": None,
    }
    for bad in ({"label_oid": "1.3.6"}, {"name": {"en": "x"}, "extra": 1}, {"name": "x"}, "x"):
        with pytest.raises(TemplateError):
            _table_entry({**base, "rename_from": bad}, "e")
    with pytest.raises(TemplateError):
        _table_entry({**base, "rename_from": {"name": {"en": "x"}, "label_oid": "not an oid"}}, "e")


def test_legacy_name() -> None:
    from netmon.templates.apply import _legacy_name

    entry = {"rename_from": {"name": {"en": "IRF role {label}", "he": "תפקיד IRF {label}"}}}
    labels = {"212": b"Board"}
    for name in (
        "IRF role Board",
        "IRF role Board · Unit 1",
        "IRF role Board #3",
        "תפקיד IRF Board",
    ):
        assert _legacy_name(name, entry, "212", labels), name
    for name in ("IRF role Boards", "Core master", "IRF role · Unit 1"):
        assert not _legacy_name(name, entry, "212", labels), name
    assert _legacy_name("IRF role 232", entry, "232", labels)  # no legacy label: the index
    assert not _legacy_name("IRF role Board", {}, "212", labels)


# ------------------------------------------------------------------ snmpsim: port inventory
STACK = "1.3.6.1.4.1.25506.2.91"
ENT = "1.3.6.1.2.1.47.1.1.1.1"
ALIAS = "1.3.6.1.2.1.47.1.3.2.1.2"
IRF_IP, PLAIN_IP = "127.0.31.1", "127.0.31.2"


def irf_switch_records(with_irf: bool = True) -> list[Any]:
    """A 5510-HI style IRF member view: FortyGigE1/1/1 + 1/1/2 bound to IRF ports 1 / 2,
    Ten-GigabitEthernet1/0/49 not (belongtoPort 0), FortyGigE2/1/1 bound to IRF port 1 (member
    2). Alias mapping only for the first one (the others map by entPhysicalName)."""
    from tests import ports_sims as sims

    recs = sims.system(sims.COMWARE_DESCR, "1.3.6.1.4.1.25506.11.1.177", "irf-5510")
    for i in range(1, 3):
        recs += sims.port(i, f"GigabitEthernet1/0/{i}")
    recs += sims.port(49, "Ten-GigabitEthernet1/0/49", high=10000, speed=4294967295)
    recs += sims.port(61, "FortyGigE1/1/1", high=40000, speed=4294967295)
    recs += sims.port(62, "FortyGigE1/1/2", high=40000, speed=4294967295)
    recs += sims.port(161, "FortyGigE2/1/1", high=40000, speed=4294967295)
    if with_irf:
        recs += [
            (f"{STACK}.1.2.0", 2, "2"),
            (f"{STACK}.3.1.1.212", 2, "2"),
            (f"{STACK}.3.1.2.212", 2, "1"),
            (f"{STACK}.5.1.1.1001", 2, "1"),
            (f"{STACK}.5.1.1.1002", 2, "2"),
            (f"{STACK}.5.1.1.1003", 2, "0"),
            (f"{STACK}.5.1.1.2001", 2, "1"),
            (f"{ENT}.7.212", 4, "Board"),
            (f"{ENT}.7.1001", 4, "FortyGigE1/1/1"),
            (f"{ENT}.7.1002", 4, "FortyGigE1/1/2"),
            (f"{ENT}.7.1003", 4, "Ten-GigabitEthernet1/0/49"),
            (f"{ENT}.7.2001", 4, "FortyGigE2/1/1"),
            (f"{ALIAS}.1001.0", 6, "1.3.6.1.2.1.2.2.1.1.61"),
        ]
    return recs


def _free_udp_port(ip: str) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((ip, 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def agents() -> Iterator[dict[str, Any]]:
    from tests import ports_sims as sims
    from tests.snmpsim_agents import start_agent

    started: dict[str, Any] = {}
    for ip, recs in ((IRF_IP, irf_switch_records()), (PLAIN_IP, sims.arubaos_records())):
        a = start_agent(ip, _free_udp_port(ip), {"public": recs})
        if a is None:
            for x in started.values():
                x.stop()
            pytest.skip("snmpsim not available")
        started[ip] = a
    yield started
    for a in started.values():
        a.stop()


async def _device(admin: httpx.AsyncClient, agent: Any, name: str) -> dict[str, Any]:
    from tests.conftest import make_device

    return await make_device(
        admin,
        name=name,
        host=agent.ip,
        snmp_credentials={"port": agent.port, "timeout_s": 1, "retries": 0},
        auto_sensors=["ping"],
        port_inventory="on",
    )


async def _refresh(rt: Any, admin: httpx.AsyncClient, did: int) -> dict[str, Any]:
    r = await admin.post(f"/api/devices/{did}/ports/refresh")
    assert r.status_code == 202, r.text
    await rt.ports.idle()
    body = (await admin.get(f"/api/devices/{did}/ports")).json()
    assert body["status"]["error"] is None, body["status"]
    return body


@requires_db
async def test_irf_ports_important(rt: Any, admin: httpx.AsyncClient, agents: Any) -> None:
    from tests.conftest import make_sensor

    dev = await _device(admin, agents[IRF_IP], "irf5510")
    # an interface sensor whose impact the user chose before 1.3.1 knew the IRF ports
    user = await make_sensor(
        admin, dev["id"], "snmp_interface", {"if_index": 62, "if_name": "x"}, impact="warning"
    )
    body = await _refresh(rt, admin, dev["id"])
    ports = {p["if_index"]: p for p in body["ports"]}
    assert body["status"]["sources"]["irf"] == "ok"
    assert (ports[61]["irf_port"], ports[62]["irf_port"], ports[161]["irf_port"]) == (1, 2, 1)
    assert ports[49]["irf_port"] is None and ports[1]["irf_port"] is None
    assert ports[61]["important"] and "irf" in ports[61]["important_reasons"]
    assert ports[161]["important_reasons"][-1] == "irf"
    assert "irf" not in ports[49]["important_reasons"]
    detail = (await admin.get(f"/api/devices/{dev['id']}/ports/61")).json()
    assert detail["irf_port"] == 1 and "irf" in detail["important_reasons"]
    # new interface sensors on IRF ports default to critical
    s61 = await make_sensor(admin, dev["id"], "snmp_interface", {"if_index": 61, "if_name": "x"})
    assert s61["impact"] == "critical"
    # a user's impact is never overridden by the poll
    await _refresh(rt, admin, dev["id"])
    assert (await admin.get(f"/api/sensors/{user['id']}")).json()["impact"] == "warning"
    # apply selection lists the reason
    r = await admin.get(
        f"/api/devices/{dev['id']}/apply-template/interfaces",
        params={"template_id": "hpe_comware"},
    )
    assert r.status_code == 200, r.text
    items = {i["if_index"]: i for i in r.json()["items"]}
    assert "irf" in items[61]["reasons"] and items[61]["important"]
    # the stack is dissolved (no IRF rows): the ports are no longer IRF ports
    agent = agents[IRF_IP]
    agent.stop()
    from tests.snmpsim_agents import start_agent

    a = await asyncio.to_thread(
        start_agent, agent.ip, agent.port, {"public": irf_switch_records(with_irf=False)}
    )
    assert a is not None
    agents[IRF_IP] = a
    body = await _refresh(rt, admin, dev["id"])
    assert body["status"]["sources"]["irf"] == "empty"
    assert all(p["irf_port"] is None for p in body["ports"])


@requires_db
async def test_non_comware_irf_empty(rt: Any, admin: httpx.AsyncClient, agents: Any) -> None:
    dev = await _device(admin, agents[PLAIN_IP], "aruba131")
    body = await _refresh(rt, admin, dev["id"])
    assert body["status"]["sources"]["irf"] == "empty"
    assert body["ports"] and all(p["irf_port"] is None for p in body["ports"])
    assert all("irf" not in p["important_reasons"] for p in body["ports"])


ERR_OID = "1.3.6.1.4.1.25506.2.6.1.1.1.1.19"


# ------------------------------------------------------------------ [1.3.1] named value maps
def test_named_value_map_resolve() -> None:
    from netmon.poller.value_maps import HH3C_ERROR_STATUS, api_value_maps, resolve

    assert resolve("hh3c_error_status", 51, "en") == (
        "down",
        "Power supply fault (psuError, 51)",
        True,
    )
    assert resolve("hh3c_error_status", 51, "he") == ("down", "ספק כוח תקול (psuError, 51)", True)
    assert resolve("hh3c_error_status", 2, "xx")[:2] == ("up", "Normal (normal, 2)")
    assert resolve("hh3c_error_status", 77, "he") == (None, "קוד תקלה לא מוכר (77)", False)
    assert resolve("hh3c_error_status", 77, "en")[1] == "Unknown fault code (77)"
    assert resolve("nope", 5, "en") == (None, "unexpected value (5)", False)
    codes = {3, 11, 21, 22, 23, 31, 32, 33, 41, 51, 61, 71, 81, 91}
    assert codes <= set(HH3C_ERROR_STATUS.codes)
    assert all(HH3C_ERROR_STATUS.codes[c].status == "down" for c in codes)
    api = api_value_maps()["hh3c_error_status"]
    psu = next(x for x in api["codes"] if x["value"] == "51")
    assert psu == {
        "value": "51",
        "status": "down",
        "mib": "psuError",
        "text": {"en": "Power supply fault", "he": "ספק כוח תקול"},
    }


def test_value_map_ref_config() -> None:
    from netmon.api.errors import ApiError
    from netmon.poller.sensor_types import validate_config

    cfg = validate_config(
        "snmp_custom",
        {"oid": f"{ERR_OID}.1", "value_type": "enum", "value_map_ref": "hh3c_error_status"},
    )
    assert cfg["value_map_ref"] == "hh3c_error_status" and cfg["value_map"] == {}
    plain = validate_config("snmp_custom", {"oid": "1.3.6.1.2.1.1.3.0"})
    assert "value_map_ref" not in plain  # stored only when set
    cleared = validate_config("snmp_custom", {"value_map_ref": ""}, base=cfg | {"value_map": {
        "2": {"status": "up", "text": "ok"}}})  # fmt: skip
    assert "value_map_ref" not in cleared
    for bad in (
        {"oid": "1.3.6.1", "value_type": "enum"},  # neither a map nor a named map
        {"oid": "1.3.6.1", "value_type": "enum", "value_map_ref": "nope"},
    ):
        with pytest.raises(ApiError):
            validate_config("snmp_custom", bad)


async def test_enum_check_named_map(monkeypatch: pytest.MonkeyPatch) -> None:
    from netmon.poller.checks import snmp_custom
    from netmon.poller.types import CheckContext

    raw = {"v": 51}

    async def fake_get(host: str, creds: dict[str, Any], oids: list[str]) -> dict[str, Any]:
        return {o: (raw["v"], "Integer32") for o in oids}

    monkeypatch.setattr(snmp_custom, "snmp_get_typed", fake_get)
    cfg = {
        "oid": f"{ERR_OID}.212",
        "value_type": "enum",
        "label": "PSU 2",
        "value_map": {"91": {"status": "warning", "text": "known issue"}},  # a user override
        "value_map_default": "down",
        "value_map_ref": "hh3c_error_status",
    }

    async def run(lang: str) -> Any:
        return await snmp_custom.run(CheckContext(1, "snmp_custom", cfg, "h", {}, lang=lang))

    r = await run("he")
    assert (r.status_override, r.message) == ("down", "PSU 2: ספק כוח תקול (psuError, 51)")
    r = await run("en")
    assert r.message == "PSU 2: Power supply fault (psuError, 51)" and r.values["value"] == 51.0
    raw["v"] = 2
    assert (await run("en")).status_override == "up"
    raw["v"] = 91
    r = await run("en")
    assert (r.status_override, r.message) == ("warning", "PSU 2: known issue (91)")
    raw["v"] = 77
    r = await run("en")
    assert (r.status_override, r.message) == ("down", "PSU 2: Unknown fault code (77)")


def test_label_format_validation() -> None:
    from netmon.templates.library import TemplateError, _table_entry

    base = {
        "kind": "snmp_table",
        "walk_oid": "1.3.6.1.4.1.25506.2.91.3.1.1",
        "name": {"en": "IRF role · {label}"},
        "config": {"value_type": "gauge"},
        "confidence": "high",
    }
    out = _table_entry({**base, "label_format": {"en": "Unit {label}", "he": "יחידה {label}"}}, "e")
    assert out["label_format"] == {"en": "Unit {label}", "he": "יחידה {label}"}
    assert "label_format" not in _table_entry(dict(base), "e")
    with pytest.raises(TemplateError):
        _table_entry({**base, "label_format": {"en": "Unit"}}, "e")


# ------------------------------------------------------------------ migration 0009
@requires_db
@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_0009_up_down_up(mode: str, rt: Any) -> None:
    import secrets

    from alembic import command

    from netmon.db import timescale
    from netmon.db.engine import make_engine
    from netmon.db.migrate import _alembic_config, run_migrations
    from tests.conftest import TEST_DATABASE_URL, _plain, db_url_for

    assert TEST_DATABASE_URL
    name = f"netmon_test_m9_{secrets.token_hex(3)}"
    conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await conn.execute(f'CREATE DATABASE "{name}"')
    await conn.close()
    url = db_url_for(name)
    engine = make_engine(url.replace("postgresql://", "postgresql+asyncpg://"))

    async def columns(c: asyncpg.Connection) -> dict[str, str]:
        rows = await c.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'switch_ports'"
        )
        return {r["column_name"]: f"{r['data_type']} {r['is_nullable']}" for r in rows}

    try:
        await run_migrations(engine)
        await timescale.bootstrap_status(engine, 90, disabled=mode == "fallback")
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == migration_head()
            assert (await columns(c))["irf_port"] == "smallint YES"
        finally:
            await c.close()

        def down(sync_conn: Any) -> None:
            cfg = _alembic_config()
            cfg.attributes["connection"] = sync_conn
            command.downgrade(cfg, "0008")

        async with engine.connect() as ac:
            await ac.run_sync(down)
            await ac.commit()
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == "0008"
            assert "irf_port" not in await columns(c)
            # 1.3.0 hardware-status sensors: as the template made them, edited by a user, other
            gid = await c.fetchval("INSERT INTO groups (name) VALUES ('g') RETURNING id")
            did = await c.fetchval(
                "INSERT INTO devices (group_id, name, host) VALUES ($1, 'd', 'h') RETURNING id",
                gid,
            )
            base = {"oid": f"{ERR_OID}.212", "value_type": "enum", "label": "PSU 1",
                    "value_map": {"2": {"status": "up", "text": "normal"}},
                    "value_map_default": "down"}  # fmt: skip
            edited = {**base, "value_map": {"2": {"status": "up", "text": "ok"}}}
            other = {**base, "oid": "1.3.6.1.4.1.9.9.1.212"}
            ids = []
            for cfg_ in (base, edited, other):
                ids.append(
                    await c.fetchval(
                        "INSERT INTO sensors (device_id, name, type, config) "
                        "VALUES ($1, 's', 'snmp_custom', $2::jsonb) RETURNING id",
                        did,
                        json.dumps(cfg_),
                    )
                )
        finally:
            await c.close()

        async def configs(c: asyncpg.Connection) -> list[dict[str, Any]]:
            rows = await c.fetch("SELECT config FROM sensors WHERE id = ANY($1) ORDER BY id", ids)
            return [json.loads(r["config"]) for r in rows]

        await run_migrations(engine)
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == migration_head()
            assert "irf_port" in await columns(c)
            got = await configs(c)
            assert got[0] == {**base, "value_map": {}, "value_map_ref": "hh3c_error_status"}
            assert got[1:] == [edited, other]  # a user's map and other OIDs are left alone
        finally:
            await c.close()
        async with engine.connect() as ac:
            await ac.run_sync(down)
            await ac.commit()
        c = await asyncpg.connect(url)
        try:
            assert await configs(c) == [base, edited, other]
        finally:
            await c.close()
        await run_migrations(engine)
    finally:
        await engine.dispose()
        c = await asyncpg.connect(_plain(TEST_DATABASE_URL))
        try:
            await c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await c.close()
