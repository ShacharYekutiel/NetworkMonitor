"""[1.3] Template apply with interface selection and the Comware IRF entries, against snmpsim
agents on 127.0.13.x."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from netmon.runtime import Runtime
from tests.conftest import requires_db
from tests.snmpsim_agents import Record, interfaces, start_agent, system
from tests.test_2b_hpe import irf_records
from tests.test_13 import _lab

pytestmark = requires_db

SW_IP, IRF_IP, SOLO_IP, SRV_IP = "127.0.13.1", "127.0.13.2", "127.0.13.3", "127.0.13.4"
STACK = "1.3.6.1.4.1.25506.2.91"
COMWARE_OID = "1.3.6.1.4.1.25506.11.1.239"


def switch_records() -> list[Record]:
    recs = system("HPE Comware Platform Software, HPE 5130", COMWARE_OID, "sw13")
    rows = [(i, f"Gi1/0/{i}", 6, 2 if i == 8 else 1) for i in range(1, 9)]
    rows += [(11, "BAGG1", 161, 1), (20, "Vlan-interface1", 136, 1)]
    return recs + interfaces(rows)


def stack_records(members: int) -> list[Record]:
    recs = irf_records()
    recs.append((f"{STACK}.1.2.0", 2, str(members)))
    if members >= 2:
        recs += [
            (f"{STACK}.3.1.1.2", 2, "2"),  # board of member 1: master
            (f"{STACK}.3.1.1.102", 2, "1"),  # member 2: standby
            (f"{STACK}.3.1.2.2", 2, "1"),  # [1.3.1] hh3cStackBoardBelongtoMember
            (f"{STACK}.3.1.2.102", 2, "2"),
            (f"{STACK}.4.1.3.1.1", 2, "1"),  # member 1 IRF-port 1: up
            (f"{STACK}.4.1.3.1.2", 2, "4"),  # member 1 IRF-port 2: disabled (not created)
            (f"{STACK}.4.1.3.2.1", 2, "1"),
            (f"{STACK}.4.1.3.2.2", 2, "4"),
        ]
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
    out: dict[str, int] = {}
    for ip, recs in (
        (SW_IP, switch_records()),
        (IRF_IP, stack_records(2)),
        (SOLO_IP, stack_records(1)),
        (SRV_IP, switch_records()),
    ):
        port = _free_udp_port(ip)
        a = start_agent(ip, port, {"public": recs})
        if a is None:
            for x in started:
                x.stop()
            pytest.skip("snmpsim not available")
        started.append(a)
        out[ip] = port
    yield out
    for a in started:
        a.stop()


def _creds(port: int) -> dict[str, Any]:
    return {"version": "v2c", "port": port, "community": "public", "retries": 0}


async def _apply(admin: httpx.AsyncClient, did: int, **body: Any) -> dict[str, Any]:
    r = await admin.post(
        f"/api/devices/{did}/apply-template", json={"template_id": "hpe_comware", **body}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _ifaces(res: dict[str, Any]) -> list[str]:
    return [c["name"] for c in res["created"] if c["type"] == "snmp_interface"]


async def _impacts(admin: httpx.AsyncClient, did: int) -> dict[int, str]:
    items = (
        await admin.get("/api/sensors", params={"device_id": did, "type": "snmp_interface"})
    ).json()["items"]
    return {s["config"]["if_index"]: s["impact"] for s in items}


async def test_apply_interface_selection(
    rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient, agents: dict[str, int]
) -> None:
    a, _ = await _lab(rt, admin, host=SW_IP)
    await admin.patch(f"/api/devices/{a}", json={"snmp_credentials": _creds(agents[SW_IP])})
    # the dialog's list (inventory present)
    url = f"/api/devices/{a}/apply-template/interfaces"
    assert (await viewer.get(url, params={"template_id": "hpe_comware"})).status_code == 403
    assert (await admin.get(url, params={"template_id": "nope"})).status_code == 422
    assert (await admin.get(url)).status_code == 422
    body = (await admin.get(url, params={"template_id": "hpe_comware"})).json()
    assert body["inventory"] == "ok"
    by = {i["if_index"]: i for i in body["items"]}
    assert sorted(by) == [1, 2, 3, 4, 5, 6, 7, 8, 11]
    assert by[3] == {
        "if_index": 3,
        "name": "Gi1/0/3",
        "alias": "",
        "oper_status": "up",
        "speed_bps": None,
        "kind": "physical",
        "important": True,
        "reasons": ["trunk"],
        "has_sensor": False,
        "sensor_id": None,
    }
    assert not by[1]["important"] and by[11]["reasons"] == ["lag", "trunk"]
    # default "important"
    res = await _apply(admin, a)
    assert _ifaces(res) == ["Gi1/0/3", "Gi1/0/4", "Gi1/0/5", "Gi1/0/6", "Gi1/0/7", "BAGG1"]
    assert res["removed"] == []
    assert set((await _impacts(admin, a)).values()) == {"critical"}
    body = (await admin.get(url, params={"template_id": "hpe_comware"})).json()
    assert {i["if_index"] for i in body["items"] if i["has_sensor"]} == {3, 4, 5, 6, 7, 11}
    # explicit list: no oper_up_only (port 8 is down) / max; unknown and excluded ifIndexes
    res = await _apply(admin, a, interfaces=[1, 8, 20, 99, 3])
    assert _ifaces(res) == ["Gi1/0/1", "Gi1/0/8"]
    reasons = {(s["name"], s["reason"], s["message"]) for s in res["skipped"]}
    assert ("Interface 99", "not_applicable", "Interface 99 not found") in reasons
    assert ("Vlan-interface1", "not_applicable", "Excluded by the template") in reasons
    assert ("Gi1/0/3", "exists", "") in reasons
    imp = await _impacts(admin, a)
    assert imp[1] == "warning" and imp[8] == "warning" and imp[3] == "critical"
    # re-apply "important" removing the unselected ones
    await rt.features.apply({"audit": True})
    res = await _apply(admin, a, remove_unselected_interfaces=True)
    assert sorted(r["name"] for r in res["removed"]) == ["Gi1/0/1", "Gi1/0/8"]
    assert sorted(await _impacts(admin, a)) == [3, 4, 5, 6, 7, 11]
    audit = (await admin.get("/api/audit", params={"action": "device.apply_template"})).json()
    assert any("removed" in (x.get("changes") or {}) for x in audit["items"])
    # "all": every up interface passing the filters (not Vlan-interface1, not the down port)
    res = await _apply(admin, a, interfaces="all")
    assert _ifaces(res) == ["Gi1/0/1", "Gi1/0/2"]
    # "none" + remove: every interface sensor goes
    res = await _apply(admin, a, interfaces="none", remove_unselected_interfaces=True)
    assert len(res["removed"]) == 8 and _ifaces(res) == []
    assert await _impacts(admin, a) == {}


async def test_apply_without_inventory(
    rt: Runtime, admin: httpx.AsyncClient, agents: dict[str, int]
) -> None:
    from tests.conftest import make_device

    # not a switch (port inventory off): "important" keeps the 1.2 selection, impact critical
    srv = await make_device(
        admin,
        name="srv13",
        host=SRV_IP,
        snmp_credentials=_creds(agents[SRV_IP]),
        port_inventory="off",
    )
    url = f"/api/devices/{srv['id']}/apply-template/interfaces"
    body = (await admin.get(url, params={"template_id": "hpe_comware"})).json()
    assert body["inventory"] == "missing"
    by = {i["if_index"]: i for i in body["items"]}
    assert 20 not in by  # Vlan-interface: excluded by the template
    assert by[11]["important"] and by[11]["reasons"] == ["lag"] and by[11]["kind"] == "lag"
    assert not by[1]["important"]
    res = await _apply(admin, srv["id"])
    assert len(_ifaces(res)) == 8  # 7 up physical + BAGG1
    assert set((await _impacts(admin, srv["id"])).values()) == {"critical"}
    # a switch without port inventory data: only LAG interfaces are important
    sw = await make_device(admin, name="sw13b", host=SW_IP, snmp_credentials=_creds(agents[SW_IP]))
    async with rt.sessionmaker() as s:
        await s.execute(
            text(
                "INSERT INTO port_inventory_status (device_id, detected_class, detected_at) "
                "VALUES (:d, 'switch', now())"
            ),
            {"d": sw["id"]},
        )
        await s.commit()
    from netmon.services.impact import interface_impact

    async with rt.sessionmaker() as s:
        assert await interface_impact(s, sw["id"], 1) == "critical"
    res = await _apply(admin, sw["id"])
    assert _ifaces(res) == ["BAGG1"]


async def test_comware_irf_entries(admin: httpx.AsyncClient, agents: dict[str, int]) -> None:
    from tests.conftest import make_device

    d = await make_device(
        admin,
        name="irf13",
        host=IRF_IP,
        snmp_credentials=_creds(agents[IRF_IP]),
        port_inventory="off",
    )
    res = await _apply(admin, d["id"], interfaces="none")
    names = [c["name"] for c in res["created"]]
    for n in ("IRF members", "IRF port 1.1", "IRF port 2.1", "IRF links up"):
        assert n in names, names
    assert "IRF port 1.2" not in names  # disabled IRF port
    roles = [n for n in names if n.startswith("IRF role ")]
    # [1.3.1] labelled by the member number (entPhysicalName is "Board" on every member)
    assert sorted(roles) == ["IRF role · Unit 1", "IRF role · Unit 2"]
    sensors = {
        s["name"]: s
        for s in (await admin.get("/api/sensors", params={"device_id": d["id"]})).json()["items"]
    }
    members = sensors["IRF members"]
    assert members["impact"] == "critical"
    assert members["channels"][0]["down_min"] == 2  # expected = the count at apply time
    links = sensors["IRF links up"]
    assert next(c for c in links["channels"] if c["key"] == "matched")["down_min"] == 1
    out = {}
    for name in ("IRF members", "IRF links up", "IRF port 1.1", *roles):
        r = await admin.post(f"/api/sensors/{sensors[name]['id']}/run")
        assert r.status_code == 200, r.text
        out[name] = r.json()
    assert out["IRF members"]["status"] == "up"
    assert out["IRF links up"]["status"] == "up"
    assert out["IRF port 1.1"]["status"] == "up"
    assert sorted(out[n]["message"].split(": ")[1] for n in roles) == ["master (2)", "standby (1)"]
    # a single switch (hh3cStackMemberNum = 1): the IRF entries are not applicable
    solo = await make_device(
        admin,
        name="solo13",
        host=SOLO_IP,
        snmp_credentials=_creds(agents[SOLO_IP]),
        port_inventory="off",
    )
    res = await _apply(admin, solo["id"], interfaces="none")
    assert not any(c["name"].startswith("IRF") for c in res["created"])
    na = [s for s in res["skipped"] if s["reason"] == "not_applicable"]
    assert len([s for s in na if "25506.2.91.1.2.0" in s["message"]]) == 4


async def test_comware_irf_role_rename_131(
    admin: httpx.AsyncClient, agents: dict[str, int]
) -> None:
    """[1.3.1] Role sensors created by 1.3.0 ("IRF role Board · Unit n", from entPhysicalName)
    are renamed to "IRF role · Unit n" on re-apply; a name the user chose is kept."""
    from tests.conftest import make_device

    d = await make_device(
        admin,
        name="irf131",
        host=IRF_IP,
        snmp_credentials=_creds(agents[IRF_IP]),
        port_inventory="off",
    )
    await _apply(admin, d["id"], interfaces="none")
    items = (await admin.get("/api/sensors", params={"device_id": d["id"]})).json()["items"]
    roles = sorted(
        (s for s in items if s["name"].startswith("IRF role ")),
        key=lambda s: int(s["config"]["oid"].rsplit(".", 1)[1]),
    )
    assert [s["name"] for s in roles] == ["IRF role · Unit 1", "IRF role · Unit 2"]
    # as 1.3.0 named them (one in Hebrew), and one the user renamed meanwhile
    old = ["IRF role Board · Unit 1", "תפקיד IRF Board #2"]
    for s, name in zip(roles, old, strict=True):
        r = await admin.patch(
            f"/api/sensors/{s['id']}", json={"name": name, "config": {"label": "Board"}}
        )
        assert r.status_code == 200, r.text
    res = await _apply(admin, d["id"], interfaces="none")
    renamed = [x for x in res["skipped"] if x["message"] == "Renamed"]
    assert sorted(x["name"] for x in renamed) == ["IRF role · Unit 1", "IRF role · Unit 2"]
    got = {s["id"]: s for s in (await admin.get("/api/sensors", params={"device_id": d["id"]}))
           .json()["items"]}  # fmt: skip
    assert [got[s["id"]]["name"] for s in roles] == ["IRF role · Unit 1", "IRF role · Unit 2"]
    assert got[roles[0]["id"]]["config"]["label"] == "Unit 1"
    r = await admin.patch(f"/api/sensors/{roles[0]['id']}", json={"name": "Core master"})
    assert r.status_code == 200
    res = await _apply(admin, d["id"], interfaces="none")
    assert not [x for x in res["skipped"] if x["message"] == "Renamed"]
    got = (await admin.get(f"/api/sensors/{roles[0]['id']}")).json()
    assert got["name"] == "Core master"


async def test_apply_new_switch_runs_inventory_first(
    rt: Runtime, admin: httpx.AsyncClient, agents: dict[str, int]
) -> None:
    """A switch without SNMP sensors yet (as just imported by discovery): the other sensors
    are created first, then one port inventory run decides the interfaces."""
    from tests.conftest import make_device

    d = await make_device(admin, name="new13", host=SW_IP, snmp_credentials=_creds(agents[SW_IP]))
    async with rt.sessionmaker() as s:
        await s.execute(
            text("UPDATE devices SET identity = CAST(:i AS jsonb) WHERE id = :d"),
            {"i": '{"device_class": "switch", "vendor": "h3c"}', "d": d["id"]},
        )
        await s.commit()
    res = await _apply(admin, d["id"])
    names = [c["name"] for c in res["created"]]
    assert names[:2] == ["Ping", "System"] and _ifaces(res) == ["BAGG1"]
    async with rt.sessionmaker() as s:
        n = (
            await s.execute(
                text("SELECT count(*) FROM switch_ports WHERE device_id = :d"), {"d": d["id"]}
            )
        ).scalar_one()
    assert n >= 9  # the inventory ran inside the apply
    assert (await _impacts(admin, d["id"])) == {11: "critical"}
