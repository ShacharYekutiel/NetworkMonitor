"""[template-switch] Apply template: the sensors the new template replaces (candidates, the
`replaces:` template key, `sensors.origin`, `remove_sensor_ids` on apply).

Agent on 127.0.6.61 (an HPE Comware switch without HOST-RESOURCES-MIB, as the owner's)."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from netmon.templates.library import TemplateError, validate_template
from netmon.templates.replaces import candidates_for, is_failing, produces
from tests.conftest import make_device, make_sensor, requires_db
from tests.snmpsim_agents import start_agent
from tests.test_2b3_ingest import features
from tests.test_2b_hpe import COMWARE_DESCR, HH3C, comware_records

COMWARE_IP = "127.0.6.61"
NOT_FOUND = "HOST-RESOURCES-MIB hrStorageRam not found"


# ---------------------------------------------------------------- pure: candidates
@dataclass
class _S:
    id: int
    type: str
    origin: str | None
    config: dict[str, Any] = field(default_factory=dict)
    name: str = "s"
    status: str = "up"
    status_message: str = ""
    sort_order: int = 0


def _tpl(tid: str, entries: list[dict[str, Any]], replaces: list[str] | None = None) -> dict:
    return {"id": tid, "entries": entries, "replaces": replaces or []}


CPU_TABLE = {"kind": "snmp_table", "walk_oid": "1.3.6.1.4.1.9.9.1.8", "percent_of_column": None}
VENDOR = _tpl(
    "vendor",
    [
        {"kind": "sensor", "type": "ping", "config": {}},
        {"kind": "sensor", "type": "snmp_system", "config": {}},
        CPU_TABLE,
        {"kind": "snmp_interfaces"},
    ],
    ["snmp_cpu", "snmp_memory"],
)
OTHER = _tpl(
    "other",
    [
        {"kind": "sensor", "type": "snmp_custom", "config": {"oid": "1.3.6.1.4.1.11.5.0"}},
        {
            "kind": "snmp_table",
            "walk_oid": "1.3.6.1.4.1.11.7",
            "percent_of_column": "1.3.6.1.4.1.11.6",
        },
    ],
)
LIBRARY = [VENDOR, OTHER]


def _ids(cands: list[dict[str, Any]]) -> dict[int, str]:
    return {c["sensor_id"]: c["reason"] for c in cands}


def test_replaced_types_whatever_their_origin_except_manual() -> None:
    sensors = [
        _S(1, "snmp_cpu", "auto", status="down", status_message=NOT_FOUND),
        _S(2, "snmp_memory", None),
        _S(3, "snmp_cpu", "template:generic_snmp"),
        _S(4, "snmp_memory", "manual"),  # by hand: never
        _S(5, "snmp_disk", "auto"),  # not in replaces, not from a template
    ]
    out = candidates_for(sensors, VENDOR, LIBRARY)
    assert _ids(out) == {1: "replaced", 2: "replaced", 3: "replaced"}
    by_id = {c["sensor_id"]: c for c in out}
    assert by_id[1]["failing"] is True and by_id[2]["failing"] is False
    assert by_id[3]["from_template"] == "generic_snmp" and by_id[1]["from_template"] is None


def test_other_template_recorded_and_inferred() -> None:
    sensors = [
        # recorded: created by another template, not produced by the new one
        _S(1, "snmp_custom", "template:other", {"oid": "1.3.6.1.4.1.11.5.0"}),
        # recorded, but the new template produces it (a row of its CPU table): kept
        _S(2, "snmp_custom", "template:other", {"oid": "1.3.6.1.4.1.9.9.1.8.3"}),
        # origin unknown (before migration 0016): inferred from the library
        _S(
            3,
            "snmp_custom",
            None,
            {"oid": "1.3.6.1.4.1.11.7.2", "percent_of_oid": "1.3.6.1.4.1.11.6.2"},
        ),
        # unknown and no template produces it (a custom OID): never
        _S(4, "snmp_custom", None, {"oid": "1.3.6.1.4.1.99.1.0"}),
        # table row without the template's percent_of_column: not that template's sensor
        _S(5, "snmp_custom", None, {"oid": "1.3.6.1.4.1.11.7.2"}),
        # created by this very template (an earlier version): never
        _S(6, "snmp_custom", "template:vendor", {"oid": "1.3.6.1.4.1.11.5.0"}),
        # a hand-made sensor with another template's OID: never
        _S(7, "snmp_custom", "manual", {"oid": "1.3.6.1.4.1.11.5.0"}),
    ]
    out = candidates_for(sensors, VENDOR, LIBRARY)
    assert _ids(out) == {1: "other_template", 3: "other_template"}
    assert [c["from_template"] for c in out] == ["other", "other"]


def test_never_candidates_ping_system_interfaces_events() -> None:
    sensors = [
        _S(1, "ping", "template:other"),
        _S(2, "snmp_system", "template:other"),
        _S(3, "snmp_interface", "template:other", {"if_index": 9}),
        _S(4, "trap", "auto"),
        _S(5, "syslog", None),
    ]
    assert candidates_for(sensors, VENDOR, LIBRARY) == []
    # a template without `replaces` leaves the generic CPU / memory alone
    assert candidates_for([_S(1, "snmp_cpu", "auto"), _S(2, "snmp_memory", None)], OTHER, []) == []


def test_failing_needs_down_and_a_not_supported_message() -> None:
    assert is_failing("down", NOT_FOUND)
    assert is_failing("down", "HOST-RESOURCES-MIB hrProcessorLoad not supported")
    assert is_failing("down", "OID not found: 1.3.6.1.2.1.25.3.3.1.2")
    assert is_failing("down", "SNMP: noSuchObject")
    assert not is_failing("down", "CPU 97 % above 95")  # works, just high
    assert not is_failing("warning", NOT_FOUND)
    assert not is_failing("up", "")


def test_produces_static_and_table_rows() -> None:
    f = produces(VENDOR)
    assert f("ping", {}) and f("snmp_system", {})
    assert f("snmp_custom", {"oid": ".1.3.6.1.4.1.9.9.1.8.1"})
    assert not f("snmp_custom", {"oid": "1.3.6.1.4.1.9.9.1.80"})  # not a row of the column
    assert not f("snmp_cpu", {})


def _yaml(**extra: Any) -> dict[str, Any]:
    return {
        "schema": 1,
        "id": "t_x",
        "name": {"en": "X"},
        "priority": 1,
        "match": [{"requires_snmp": True}],
        "entries": [{"kind": "sensor", "type": "ping", "name": {"en": "Ping"}}],
        **extra,
    }


def test_template_replaces_key_is_validated() -> None:
    assert validate_template(_yaml(), "t_x")["replaces"] == []
    ok = validate_template(_yaml(replaces=["snmp_cpu", "snmp_memory"]), "t_x")
    assert ok["replaces"] == ["snmp_cpu", "snmp_memory"]
    for bad, msg in [
        (["ping"], "must be one of"),
        (["snmp_cpu", "snmp_cpu"], "duplicate"),
        ("snmp_cpu", "list"),
    ]:
        with pytest.raises(TemplateError, match=msg):
            validate_template(_yaml(replaces=bad), "t_x")
    own = _yaml(
        replaces=["snmp_cpu"],
        entries=[{"kind": "sensor", "type": "snmp_cpu", "name": {"en": "CPU"}}],
    )
    with pytest.raises(TemplateError, match="creates snmp_cpu sensors itself"):
        validate_template(own, "t_x")


def test_library_templates_with_replaces() -> None:
    from pathlib import Path

    from netmon.templates.library import load_dir

    root = Path(__file__).resolve().parents[1] / "netmon" / "templates" / "library"
    templates, errors = load_dir(root, "library")
    assert errors == []
    got = {t["id"]: t["replaces"] for t in templates if t["replaces"]}
    assert got == {
        "checkpoint_gaia": ["snmp_cpu", "snmp_memory"],
        "cisco_ios": ["snmp_cpu", "snmp_memory"],
        "fortinet_fortigate": ["snmp_cpu", "snmp_memory"],
        "hpe_arubaos_switch": ["snmp_cpu", "snmp_memory"],
        "hpe_comware": ["snmp_cpu", "snmp_memory"],
    }


# ---------------------------------------------------------------- API against snmpsim
@pytest.fixture(scope="module")
def agent() -> Iterator[int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((COMWARE_IP, 0))
    port = s.getsockname()[1]
    s.close()
    a = start_agent(COMWARE_IP, port, {"public": comware_records()})
    if a is None:
        pytest.skip("snmpsim not available")
    yield port
    a.stop()


async def _comware_with_defaults(admin: httpx.AsyncClient, port: int) -> dict[str, Any]:
    """The owner's case: an HPE Comware switch added with the default sensors."""
    assert "Comware" in COMWARE_DESCR
    dev = await make_device(
        admin,
        name="access-1",
        host=COMWARE_IP,
        snmp_credentials={"version": "v2c", "port": port, "community": "public", "retries": 0},
        auto_sensors=["ping", "snmp_system", "snmp_cpu", "snmp_memory"],
    )
    sensors = (await admin.get("/api/sensors", params={"device_id": dev["id"]})).json()["items"]
    for s in sensors:
        if s["type"] in ("snmp_cpu", "snmp_memory"):
            r = await admin.post(f"/api/sensors/{s['id']}/run")
            assert r.status_code == 200, r.text
    return dev


async def _sensors(admin: httpx.AsyncClient, did: int) -> dict[str, dict[str, Any]]:
    items = (await admin.get("/api/sensors", params={"device_id": did})).json()["items"]
    return {s["name"]: s for s in items}


async def _origins(rt: Any, did: int) -> dict[str, str | None]:
    async with rt.sessionmaker() as s:
        rows = await s.execute(
            text("SELECT name, origin FROM sensors WHERE device_id = :d"), {"d": did}
        )
        return {r.name: r.origin for r in rows}


@requires_db
async def test_comware_replaces_failing_generic_cpu_memory(
    admin: httpx.AsyncClient, viewer: httpx.AsyncClient, rt: Any, agent: int
) -> None:
    await features(admin, audit=True)
    dev = await _comware_with_defaults(admin, agent)
    did = dev["id"]
    before = await _sensors(admin, did)
    cpu, mem = before["CPU Load"], before["Memory"]
    assert cpu["status"] == "down" and "not supported" in cpu["status_message"]
    assert mem["status"] == "down" and mem["status_message"] == NOT_FOUND
    assert (await _origins(rt, did)) == {
        "Ping": "auto",
        "System": "auto",
        "CPU Load": "auto",
        "Memory": "auto",
    }
    url = f"/api/devices/{did}/apply-template/replaces"
    # viewer: neither the list nor the apply option
    assert (await viewer.get(url, params={"template_id": "hpe_comware"})).status_code == 403
    r = await viewer.post(
        f"/api/devices/{did}/apply-template",
        json={"template_id": "hpe_comware", "remove_sensor_ids": [cpu["id"]]},
    )
    assert r.status_code == 403
    assert (await admin.get(url, params={"template_id": "nope"})).status_code == 422
    assert (
        await admin.get(
            "/api/devices/99999/apply-template/replaces", params={"template_id": "hpe_comware"}
        )
    ).status_code == 404
    # the generic SNMP template creates them itself: nothing to replace
    r = await admin.get(url, params={"template_id": "generic_snmp"})
    assert r.status_code == 200 and r.json() == {"items": []}
    r = await admin.get(url, params={"template_id": "hpe_comware"})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [(i["sensor_id"], i["type"], i["reason"], i["failing"]) for i in items] == [
        (cpu["id"], "snmp_cpu", "replaced", True),
        (mem["id"], "snmp_memory", "replaced", True),
    ]
    assert items[1]["status"] == "down" and items[1]["status_message"] == NOT_FOUND
    assert items[0]["from_template"] is None and items[0]["name"] == "CPU Load"

    # a non-candidate id (ping) -> 422, nothing applied or deleted
    r = await admin.post(
        f"/api/devices/{did}/apply-template",
        json={"template_id": "hpe_comware", "remove_sensor_ids": [cpu["id"], before["Ping"]["id"]]},
    )
    assert r.status_code == 422 and str(before["Ping"]["id"]) in r.text
    assert set(await _sensors(admin, did)) == set(before)
    for body in ({"remove_sensor_ids": list(range(1, 1002))}, {"remove_sensor_ids": ["x"]}):
        r = await admin.post(
            f"/api/devices/{did}/apply-template", json={"template_id": "hpe_comware", **body}
        )
        assert r.status_code == 422

    # history of the sensors that go
    async with rt.sessionmaker() as s:
        await s.execute(
            text(
                "INSERT INTO measurements (sensor_id, time, channel, value) "
                "VALUES (:i, now(), 'cpu_pct', 1)"
            ),
            {"i": cpu["id"]},
        )
        await s.commit()
    removed_from_scheduler: list[int] = []

    class _Sched:
        def remove(self, sensor_id: int) -> None:
            removed_from_scheduler.append(sensor_id)

        async def upsert(self, sensor_id: int) -> None:
            return None

    prev = rt.scheduler
    rt.scheduler = _Sched()
    try:
        r = await admin.post(
            f"/api/devices/{did}/apply-template",
            json={
                "template_id": "hpe_comware",
                "remove_sensor_ids": [mem["id"], cpu["id"], cpu["id"]],
            },
        )
    finally:
        rt.scheduler = prev
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["replaced"] == [
        {"sensor_id": cpu["id"], "name": "CPU Load", "type": "snmp_cpu"},
        {"sensor_id": mem["id"], "name": "Memory", "type": "snmp_memory"},
    ]
    assert res["removed"] == []
    assert sorted(removed_from_scheduler) == sorted([cpu["id"], mem["id"]])
    created = [c["name"] for c in res["created"]]
    assert "CPU Board" in created and "Memory Board" in created
    after = await _sensors(admin, did)
    assert "CPU Load" not in after and "Memory" not in after
    assert {"Ping", "System", "CPU Board", "Memory Board"} <= set(after)
    for sid in (cpu["id"], mem["id"]):
        assert (await admin.get(f"/api/sensors/{sid}")).status_code == 404
    assert after["CPU Board"]["config"]["oid"] == f"{HH3C}.6.2"
    origins = await _origins(rt, did)
    assert origins["CPU Board"] == "template:hpe_comware" and origins["Ping"] == "auto"
    # one sensor.delete audit row per removed sensor, as DELETE /api/sensors/{id}
    audit = (await admin.get("/api/audit", params={"action": "sensor.delete"})).json()["items"]
    rows = {a["entity_id"]: a for a in audit}
    assert {cpu["id"], mem["id"]} <= set(rows)
    assert rows[cpu["id"]]["entity_name"] == "CPU Load"
    applied = (await admin.get("/api/audit", params={"action": "device.apply_template"})).json()
    assert applied["items"][0]["changes"]["replaced"] == [
        None,
        [f"CPU Load (#{cpu['id']})", f"Memory (#{mem['id']})"],
    ]

    async def no_history() -> bool:
        async with rt.sessionmaker() as s:
            n = (
                await s.execute(
                    text("SELECT count(*) FROM measurements WHERE sensor_id = :i"), {"i": cpu["id"]}
                )
            ).scalar_one()
        return n == 0

    for _ in range(50):
        if await no_history():
            break
        await asyncio.sleep(0.05)
    assert await no_history()
    # nothing left to replace; the template's own sensors are never candidates
    r = await admin.get(url, params={"template_id": "hpe_comware"})
    assert r.json() == {"items": []}


@requires_db
async def test_nothing_ticked_nothing_removed_and_manual_never_offered(
    admin: httpx.AsyncClient, rt: Any, agent: int
) -> None:
    await features(admin, audit=True)
    dev = await _comware_with_defaults(admin, agent)
    did = dev["id"]
    before = await _sensors(admin, did)
    hand = await make_sensor(admin, did, "snmp_memory", name="My memory")
    assert (await _origins(rt, did))["My memory"] == "manual"
    url = f"/api/devices/{did}/apply-template/replaces"
    items = (await admin.get(url, params={"template_id": "hpe_comware"})).json()["items"]
    assert [i["name"] for i in items] == ["CPU Load", "Memory"]  # not "My memory"
    r = await admin.post(
        f"/api/devices/{did}/apply-template",
        json={"template_id": "hpe_comware", "remove_sensor_ids": [hand["id"]]},
    )
    assert r.status_code == 422
    res = (
        await admin.post(f"/api/devices/{did}/apply-template", json={"template_id": "hpe_comware"})
    ).json()
    assert res["replaced"] == [] and res["removed"] == []
    after = await _sensors(admin, did)
    assert set(before) | {"My memory"} <= set(after)
    applied = (await admin.get("/api/audit", params={"action": "device.apply_template"})).json()
    assert "replaced" not in applied["items"][0]["changes"]


@requires_db
async def test_other_template_sensors_and_legacy_origin(
    admin: httpx.AsyncClient, rt: Any, agent: int
) -> None:
    """A device on the ArubaOS-Switch template, re-templated to Comware: the ArubaOS CPU
    sensor is offered (recorded origin); a sensor from before 0016 is matched by its OID."""
    dev = await make_device(
        admin,
        name="sw",
        host=COMWARE_IP,
        snmp_credentials={"version": "v2c", "port": agent, "community": "public", "retries": 0},
    )
    did = dev["id"]
    aruba_cpu = await make_sensor(
        admin,
        did,
        "snmp_custom",
        {"oid": "1.3.6.1.4.1.11.2.14.11.5.1.9.6.1.0", "value_type": "gauge", "unit": "%"},
        name="CPU",
    )
    legacy = await make_sensor(
        admin,
        did,
        "snmp_custom",
        {
            "oid": "1.3.6.1.4.1.11.2.14.11.5.1.1.2.1.1.1.7.1",
            "percent_of_oid": "1.3.6.1.4.1.11.2.14.11.5.1.1.2.1.1.1.5.1",
            "value_type": "gauge",
            "unit": "%",
        },
        name="Memory 1",
    )
    async with rt.sessionmaker() as s:
        await s.execute(
            text("UPDATE sensors SET origin = 'template:hpe_arubaos_switch' WHERE id = :i"),
            {"i": aruba_cpu["id"]},
        )
        await s.execute(text("UPDATE sensors SET origin = NULL WHERE id = :i"), {"i": legacy["id"]})
        await s.commit()
    url = f"/api/devices/{did}/apply-template/replaces"
    items = (await admin.get(url, params={"template_id": "hpe_comware"})).json()["items"]
    assert [(i["name"], i["reason"], i["from_template"], i["failing"]) for i in items] == [
        ("CPU", "other_template", "hpe_arubaos_switch", False),
        ("Memory 1", "other_template", "hpe_arubaos_switch", False),
    ]
    # re-applying the ArubaOS template itself offers nothing
    items = (await admin.get(url, params={"template_id": "hpe_arubaos_switch"})).json()["items"]
    assert items == []
    r = await admin.post(
        f"/api/devices/{did}/apply-template",
        json={"template_id": "hpe_comware", "remove_sensor_ids": [legacy["id"]]},
    )
    assert r.status_code == 200, r.text
    assert [x["sensor_id"] for x in r.json()["replaced"]] == [legacy["id"]]
    left = await _sensors(admin, did)
    assert "CPU" in left and "Memory 1" not in left


# ---------------------------------------------------------------- migration (sensors.origin)
@requires_db
@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_sensor_origin_up_down_up(mode: str, rt: Any) -> None:
    import secrets

    import asyncpg
    from alembic import command
    from alembic.script import ScriptDirectory

    from netmon.db import timescale
    from netmon.db.engine import make_engine
    from netmon.db.migrate import _alembic_config, migration_head, run_migrations
    from tests.conftest import TEST_DATABASE_URL, _plain, db_url_for

    assert TEST_DATABASE_URL
    script = next(
        sc
        for sc in ScriptDirectory.from_config(_alembic_config()).walk_revisions()
        if "sensor_origin" in str(sc.path)
    )
    down = str(script.down_revision)
    name = f"netmon_test_mso_{secrets.token_hex(3)}"
    conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await conn.execute(f'CREATE DATABASE "{name}"')
    await conn.close()
    url = db_url_for(name)
    engine = make_engine(url.replace("postgresql://", "postgresql+asyncpg://"))

    async def origin_column(c: asyncpg.Connection) -> str | None:
        row = await c.fetchrow(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'sensors' AND column_name = 'origin'"
        )
        return None if row is None else f"{row['data_type']} {row['is_nullable']}"

    def migrate(action: str, rev: str) -> Any:
        def run(sync_conn: Any) -> None:
            cfg = _alembic_config()
            cfg.attributes["connection"] = sync_conn
            getattr(command, action)(cfg, rev)

        return run

    try:
        await run_migrations(engine)
        await timescale.bootstrap_status(engine, 90, disabled=mode == "fallback")
        c = await asyncpg.connect(url)
        try:
            assert await origin_column(c) == "text YES"
        finally:
            await c.close()
        async with engine.connect() as ac:
            await ac.run_sync(migrate("downgrade", down))
            await ac.commit()
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == down
            assert await origin_column(c) is None
        finally:
            await c.close()
        await run_migrations(engine)
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == migration_head()
            assert await origin_column(c) == "text YES"
        finally:
            await c.close()
    finally:
        await engine.dispose()
        conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.close()
