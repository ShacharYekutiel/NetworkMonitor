"""[premium] API: connected device kind in the ports API and its manual override; the home
network map and its setting."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from sqlalchemy import text

from netmon.runtime import Runtime
from tests.conftest import default_group_id, make_device, make_sensor, requires_db

pytestmark = requires_db

# if_index, name, kind, mode, mac_count, lag, oper
PORTS = [
    (1, "Gi1/0/1", "physical", "access", 1, None, "up"),  # printer by OUI
    (2, "Gi1/0/2", "physical", "access", 1, None, "up"),  # PC (no OUI match)
    (3, "Gi1/0/3", "physical", "access", 2, None, "up"),  # phone + PC
    (4, "Gi1/0/4", "physical", "access", 0, None, "up"),  # LLDP phone (unmanaged)
    (5, "Gi1/0/5", "physical", "access", 0, None, "up"),  # nothing learned
    (6, "Gi1/0/6", "physical", "access", 0, None, "down"),  # no link
    (7, "Gi1/0/7", "physical", "trunk", 0, None, "up"),  # managed core switch (manual link)
    (8, "Gi1/0/8", "physical", "access", 3, None, "up"),  # 3 unknown MACs
    (9, "Gi1/0/9", "physical", None, 0, 11, "up"),  # LAG member
    (11, "BAGG1", "lag", "trunk", 0, None, "up"),  # LAG to a managed unknown-class device
]
MACS = [
    ("00:1b:a9:00:00:01", 1),  # Brother
    ("00:11:22:33:44:55", 2),
    ("80:5e:c0:00:00:01", 3),  # Yealink
    ("00:11:22:33:44:56", 3),
    ("00:11:22:33:44:57", 8),
    ("00:11:22:33:44:58", 8),
    ("00:11:22:33:44:59", 8),
]


async def _lab(rt: Runtime, admin: httpx.AsyncClient) -> dict[str, int]:
    sw = await make_device(admin, name="access-1", host="10.99.0.1")
    core = await make_device(admin, name="core-1", host="10.99.0.2")
    other = await make_device(admin, name="nas-1", host="10.99.0.3")
    async with rt.sessionmaker() as s:
        for idx, name, kind, mode, macs, lag, oper in PORTS:
            await s.execute(
                text(
                    "INSERT INTO switch_ports (device_id, if_index, name, descr, alias, kind, "
                    "admin_status, oper_status, bridge_port, pvid, mode, untagged_vlans, "
                    "tagged_vlans, mac_count, lag_if_index, lag_attached, collected_at) VALUES "
                    "(:d, :i, :n, :n, '', :k, 'up', :o, :i, 1, :m, '{1}', '{}', :mc, :l, :a, "
                    "now())"
                ),
                {"d": sw["id"], "i": idx, "n": name, "k": kind, "o": oper, "m": mode,
                 "mc": macs, "l": lag, "a": True if lag else None},
            )  # fmt: skip
        for mac, idx in MACS:
            await s.execute(
                text(
                    "INSERT INTO port_macs (device_id, if_index, mac, vlan, first_seen, "
                    "last_seen) VALUES (:d, :i, :m, 1, now(), now())"
                ),
                {"d": sw["id"], "i": idx, "m": mac},
            )
        await s.execute(
            text(
                "INSERT INTO port_inventory_status (device_id, detected_class, last_run_at, "
                "last_ok_at, ports) VALUES (:d, 'switch', now(), now(), 10), "
                "(:c, 'switch', now(), now(), 0)"
            ),
            {"d": sw["id"], "c": core["id"]},
        )
        nb = (
            await s.execute(
                text(
                    "INSERT INTO topology_neighbors (chassis_id, sys_name, sys_descr, "
                    "capabilities) VALUES ('80:5e:c0:00:00:99', 'SIP-T46U', 'Yealink SIP-T46U', "
                    "'{bridge,telephone}') RETURNING id"
                )
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO topology_links (source, a_device_id, a_if_index, a_port, "
                "b_neighbor_id, b_port) VALUES ('lldp', :d, 4, 'Gi1/0/4', :nb, 'WAN')"
            ),
            {"d": sw["id"], "nb": nb},
        )
        await s.commit()
    for body in (
        {"a_device_id": core["id"], "a_if_index": 49, "a_port": "49",
         "b_device_id": sw["id"], "b_if_index": 7, "b_port": "Gi1/0/7"},
        {"a_device_id": sw["id"], "a_if_index": 11, "a_port": "BAGG1",
         "b_device_id": other["id"], "b_if_index": 1, "b_port": "1"},
    ):  # fmt: skip
        r = await admin.post("/api/topology/links", json=body)
        assert r.status_code == 201, r.text
    return {"sw": sw["id"], "core": core["id"], "other": other["id"]}


def _by_index(body: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {p["if_index"]: p for p in body["ports"]}


async def test_ports_connected_kind_derivation(rt: Runtime, admin: httpx.AsyncClient) -> None:
    ids = await _lab(rt, admin)
    r = await admin.get(f"/api/devices/{ids['sw']}/ports")
    assert r.status_code == 200, r.text
    p = _by_index(r.json())
    got = {i: (p[i]["connected_kind"], p[i]["connected_kind_source"]) for i in p}
    assert got == {
        1: ("printer", "oui"),
        2: ("computer", "oui"),
        3: ("phone", "oui"),
        4: ("phone", "lldp"),
        5: (None, None),
        6: (None, None),
        7: ("switch", "manual"),  # manual topology link to a managed switch
        8: ("unknown", "oui"),
        9: (None, None),  # LAG member: nothing of its own while the `lag` feature is off
        11: ("switch", "manual"),
    }
    assert all(p[i]["connected_kind_override"] is None for i in p)
    # with the `lag` feature on, a member inherits its LAG's neighbour
    r = await admin.put("/api/settings", json={"feature_lag_enabled": True})
    assert r.status_code == 200, r.text
    p = _by_index((await admin.get(f"/api/devices/{ids['sw']}/ports")).json())
    assert (p[9]["connected_kind"], p[9]["connected_kind_source"]) == ("switch", "manual")
    # the port detail carries the same fields
    r = await admin.get(f"/api/devices/{ids['sw']}/ports/1")
    assert r.status_code == 200, r.text
    assert (r.json()["connected_kind"], r.json()["connected_kind_source"]) == ("printer", "oui")


async def test_connected_kind_override(rt: Runtime, admin: httpx.AsyncClient, viewer) -> None:  # type: ignore[no-untyped-def]
    ids = await _lab(rt, admin)
    await admin.put("/api/settings", json={"feature_audit_enabled": True})
    sw = ids["sw"]
    url = f"/api/devices/{sw}/ports/2/connected-kind"
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    rt.hub.listeners.append(queue)
    r = await admin.put(url, json={"kind": "camera"})
    assert r.status_code == 200, r.text
    port = r.json()
    assert (port["if_index"], port["connected_kind"], port["connected_kind_source"]) == (
        2,
        "camera",
        "manual",
    )
    assert port["connected_kind_override"] == "camera"
    p = _by_index((await admin.get(f"/api/devices/{sw}/ports")).json())
    assert (p[2]["connected_kind"], p[2]["connected_kind_source"]) == ("camera", "manual")
    # an override on a port that is down is still shown
    r = await admin.put(f"/api/devices/{sw}/ports/6/connected-kind", json={"kind": "printer"})
    assert (r.json()["connected_kind"], r.json()["connected_kind_source"]) == ("printer", "manual")
    # viewers cannot write; bad values / unknown ports / devices are refused
    assert (await viewer.put(url, json={"kind": "phone"})).status_code == 403
    assert (await admin.put(url, json={"kind": "toaster"})).status_code == 422
    assert (await admin.put(url, json={})).status_code == 422
    r = await admin.put(f"/api/devices/{sw}/ports/77/connected-kind", json={"kind": "phone"})
    assert r.status_code == 404
    r = await admin.put("/api/devices/999999/ports/2/connected-kind", json={"kind": "phone"})
    assert r.status_code == 404
    # clear: the derived kind applies again
    r = await admin.put(url, json={"kind": None})
    assert r.status_code == 200, r.text
    assert (r.json()["connected_kind"], r.json()["connected_kind_source"]) == ("computer", "oui")
    assert r.json()["connected_kind_override"] is None
    # audit: one row per change, entity switch_port with the device
    rows = (await admin.get("/api/audit", params={"action": "switch_port.update"})).json()
    items = sorted(rows["items"], key=lambda x: x["id"])
    assert [(x["entity_id"], x["changes"]) for x in items] == [
        (2, {"connected_kind": [None, "camera"]}),
        (6, {"connected_kind": [None, "printer"]}),
        (2, {"connected_kind": ["camera", None]}),
    ]
    assert all(x["device_id"] == sw and x["entity_type"] == "switch_port" for x in items)
    assert items[0]["entity_name"] == "access-1 Gi1/0/2"
    msgs = [queue.get_nowait() for _ in range(queue.qsize())]
    assert (
        sum(1 for m in msgs if m["type"] == "ports.updated" and m["data"]["device_id"] == sw) == 3
    )
    # stored persistently; removed with the device
    async with rt.sessionmaker() as s:
        n = (await s.execute(text("SELECT count(*) FROM port_kind_overrides"))).scalar()
        assert n == 1
    assert (await admin.delete(f"/api/devices/{sw}")).status_code == 204
    async with rt.sessionmaker() as s:
        n = (await s.execute(text("SELECT count(*) FROM port_kind_overrides"))).scalar()
        assert n == 0


# ------------------------------------------------------------------ home map
async def _group(admin: httpx.AsyncClient, name: str, parent: int | None = None) -> int:
    r = await admin.post("/api/groups", json={"name": name, "parent_id": parent})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def _status(rt: Runtime, sensor_id: int, status: str, msg: str = "") -> None:
    async with rt.sessionmaker() as s:
        await s.execute(
            text(
                "UPDATE sensors SET status = :st, status_message = :m, "
                "last_status_change_at = now() WHERE id = :i"
            ),
            {"st": status, "m": msg, "i": sensor_id},
        )
        await s.commit()


async def _map_lab(rt: Runtime, admin: httpx.AsyncClient) -> dict[str, Any]:
    root = await default_group_id(admin)
    access = await _group(admin, "Access", root)
    servers = await _group(admin, "Servers", root)
    wifi = await _group(admin, "WiFi", root)
    fw = await make_device(admin, name="fw-1", host="10.98.0.1", group_id=root)
    core = await make_device(admin, name="core-1", host="10.98.0.2", group_id=root)
    sw1 = await make_device(admin, name="sw-1", host="10.98.1.1", group_id=access)
    sw2 = await make_device(admin, name="sw-2", host="10.98.1.2", group_id=access)
    srv = await make_device(admin, name="srv-1", host="10.98.2.1", group_id=servers)
    ap = await make_device(admin, name="ap-1", host="10.98.3.1", group_id=wifi)
    sensors = {}
    for d in (fw, core, sw1, sw2, srv, ap):
        sensors[d["name"]] = (await make_sensor(admin, d["id"], "ping", name="Ping"))["id"]
        await _status(rt, sensors[d["name"]], "up")
    disk = (await make_sensor(admin, srv["id"], "ping", name="Disk C:"))["id"]
    await _status(rt, disk, "up")
    return {
        "root": root, "access": access, "servers": servers, "wifi": wifi,
        "fw": fw["id"], "core": core["id"], "sw1": sw1["id"], "srv": srv["id"],
        "sensors": sensors, "disk": disk,
    }  # fmt: skip


def _nodes(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in body["nodes"]}


async def test_home_map_auto(rt: Runtime, admin: httpx.AsyncClient, viewer) -> None:  # type: ignore[no-untyped-def]
    lab = await _map_lab(rt, admin)
    r = await viewer.get("/api/overview/map")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == {"group_id": None, "name": ""}
    assert body["configured"] is True
    n = _nodes(body)
    assert list(n) == ["root", f"g{lab['root']}"]  # one top-level group
    assert n["root"]["kind"] == "group" and n["root"]["parent_id"] is None
    assert n["root"]["status"] == "up" and n["root"]["problem"] is None
    assert n["root"]["counts"] == {
        "devices": 6, "sensors": 7, "down": 0, "warning": 0, "unreachable": 0,
    }  # fmt: skip
    # scoped to the top-level group: its child groups and its own devices as leaves
    body = (await viewer.get("/api/overview/map", params={"group_id": lab["root"]})).json()
    n = _nodes(body)
    assert body["scope"]["group_id"] == lab["root"]
    assert [x["id"] for x in body["nodes"]] == [
        "root", f"g{lab['access']}", f"g{lab['servers']}", f"g{lab['wifi']}",
        f"d{lab['core']}", f"d{lab['fw']}",
    ]  # fmt: skip
    g = n[f"g{lab['access']}"]
    assert (g["kind"], g["role"], g["name"], g["subtitle"]) == (
        "group",
        "group",
        "Access",
        "2 devices",
    )
    assert g["ref"] == {"type": "group", "id": lab["access"]} and g["parent_id"] == "root"
    assert {(x["from"], x["to"], x["status"]) for x in body["links"]} == {
        ("root", k, "up") for k in list(n)[1:]
    }
    # a problem: worst status and the most severe open problem inside
    await _status(rt, lab["disk"], "warning", "Disk 95 % full")
    await _status(rt, lab["sensors"]["sw-1"], "down", "Timeout")
    body = (await viewer.get("/api/overview/map", params={"group_id": lab["root"]})).json()
    n = _nodes(body)
    assert n["root"]["status"] == "down" and n["root"]["problem"] == "sw-1 · Ping"
    acc, srv = n[f"g{lab['access']}"], n[f"g{lab['servers']}"]
    assert (acc["status"], acc["problem"], acc["counts"]["down"]) == ("down", "sw-1 · Ping", 1)
    assert (srv["status"], srv["problem"], srv["counts"]["warning"]) == (
        "warning",
        "srv-1 · Disk C:",
        1,
    )
    links = {x["to"]: x["status"] for x in body["links"]}
    assert links[f"g{lab['access']}"] == "down" and links[f"g{lab['servers']}"] == "warning"
    assert links[f"g{lab['wifi']}"] == "up"
    # unknown group
    assert (await viewer.get("/api/overview/map", params={"group_id": 999999})).status_code == 404
    # max_groups: problem nodes are kept, the rest summed in "+N"
    r = await admin.put("/api/settings/home-map", json={"max_groups": 2})
    assert r.status_code == 200, r.text
    body = (await viewer.get("/api/overview/map", params={"group_id": lab["root"]})).json()
    ids = [x["id"] for x in body["nodes"]]
    assert ids == ["root", f"g{lab['access']}", "more"]
    more = _nodes(body)["more"]
    assert (more["name"], more["ref"], more["status"]) == ("+4", None, "warning")
    assert more["counts"]["devices"] == 4


async def test_home_map_auto_suggests_backbone(rt: Runtime, admin: httpx.AsyncClient) -> None:
    lab = await _map_lab(rt, admin)
    for b in (lab["sw1"], lab["srv"]):
        r = await admin.post(
            "/api/topology/links",
            json={"a_device_id": lab["core"], "a_port": f"p{b}", "b_device_id": b, "b_port": "1"},
        )
        assert r.status_code == 201, r.text
    body = (await admin.get("/api/overview/map", params={"group_id": lab["root"]})).json()
    assert body["configured"] is False  # core-1 links Access and Servers: not guessed
    assert "root" in _nodes(body)


async def test_home_map_backbone(rt: Runtime, admin: httpx.AsyncClient, viewer) -> None:  # type: ignore[no-untyped-def]
    lab = await _map_lab(rt, admin)
    r = await admin.get("/api/settings/home-map")
    assert r.json() == {
        "mode": "auto", "show_internet": True, "backbone_device_ids": [], "max_groups": 12,
    }  # fmt: skip
    # validation and roles
    assert (
        await viewer.put("/api/settings/home-map", json={"mode": "backbone"})
    ).status_code == 403
    for bad in (
        {"mode": "ring"},
        {"backbone_device_ids": [lab["fw"], lab["fw"]]},
        {"backbone_device_ids": [999999]},
        {"max_groups": 0},
        {"other": 1},
    ):
        assert (await admin.put("/api/settings/home-map", json=bad)).status_code == 422, bad
    r = await admin.put(
        "/api/settings/home-map",
        json={"mode": "backbone", "backbone_device_ids": [lab["fw"], lab["core"]]},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "mode": "backbone", "show_internet": True,
        "backbone_device_ids": [lab["fw"], lab["core"]], "max_groups": 12,
    }  # fmt: skip
    async with rt.sessionmaker() as s:
        await s.execute(
            text("UPDATE devices SET identity = :i WHERE id = :d"),
            {"i": '{"vendor": "fortinet", "model": "FortiGate 60F"}', "d": lab["fw"]},
        )
        await s.commit()
    await _status(rt, lab["sensors"]["core-1"], "down", "Timeout")
    body = (await viewer.get("/api/overview/map")).json()
    assert body["configured"] is True
    n = _nodes(body)
    assert [x["id"] for x in body["nodes"]] == [
        "internet", f"d{lab['fw']}", f"d{lab['core']}", f"g{lab['root']}",
    ]  # fmt: skip
    assert (n["internet"]["kind"], n["internet"]["role"], n["internet"]["ref"]) == (
        "internet",
        "internet",
        None,
    )
    fw, core = n[f"d{lab['fw']}"], n[f"d{lab['core']}"]
    assert (fw["role"], fw["subtitle"], fw["parent_id"]) == (
        "firewall",
        "FortiGate 60F",
        "internet",
    )
    assert (core["role"], core["parent_id"], core["status"]) == ("core", fw["id"], "down")
    assert core["problem"] == "Ping: Timeout" and core["ref"] == {
        "type": "device",
        "id": lab["core"],
    }
    assert core["counts"] == {"devices": 1, "sensors": 1, "down": 1, "warning": 0, "unreachable": 0}
    assert n[f"g{lab['root']}"]["parent_id"] == core["id"]
    assert {(x["from"], x["to"]): x["status"] for x in body["links"]} == {
        ("internet", fw["id"]): "up",
        (fw["id"], core["id"]): "down",
        (core["id"], f"g{lab['root']}"): "down",
    }
    # scoped to the group: the backbone devices are its own devices, the child groups hang
    # below the last one; show_internet off
    await admin.put("/api/settings/home-map", json={"show_internet": False})
    body = (await viewer.get("/api/overview/map", params={"group_id": lab["root"]})).json()
    assert [x["id"] for x in body["nodes"]] == [
        f"d{lab['fw']}", f"d{lab['core']}",
        f"g{lab['access']}", f"g{lab['servers']}", f"g{lab['wifi']}",
    ]  # fmt: skip
    assert _nodes(body)[f"d{lab['fw']}"]["parent_id"] is None
    # a scope without any backbone device falls back to the auto tree
    body = (await viewer.get("/api/overview/map", params={"group_id": lab["access"]})).json()
    assert body["nodes"][0]["id"] == "root" and body["scope"]["name"] == "Access"
    # a deleted backbone device is skipped
    assert (await admin.delete(f"/api/devices/{lab['fw']}")).status_code == 204
    body = (await viewer.get("/api/overview/map")).json()
    assert body["nodes"][0]["id"] == f"d{lab['core']}"
    assert body["nodes"][0]["role"] == "core"  # not a firewall by name / identity


async def test_home_map_requires_login(client: httpx.AsyncClient, admin) -> None:  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=client._transport,  # noqa: SLF001
        base_url="http://testserver",
    ) as anon:
        assert (await anon.get("/api/overview/map")).status_code == 401
        assert (await anon.get("/api/settings/home-map")).status_code == 401
