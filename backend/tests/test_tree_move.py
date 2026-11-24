"""[tree-move] POST /api/tree/move: single and bulk moves, group with its content, the top level,
cycles, unknown ids, permissions, all-or-nothing, name clashes, audit rows, inheritance."""

from __future__ import annotations

import json
from typing import Any

import asyncpg
import httpx
import pytest

from tests.conftest import _plain, default_group_id, make_device, requires_db

pytestmark = requires_db


async def _group(c: httpx.AsyncClient, name: str, parent_id: int | None, **kw: Any) -> int:
    r = await c.post("/api/groups", json={"name": name, "parent_id": parent_id, **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def _move(c: httpx.AsyncClient, **body: Any) -> httpx.Response:
    return await c.post("/api/tree/move", json=body)


async def _parents(c: httpx.AsyncClient) -> dict[str, Any]:
    """{"g<id>": parent_id, "d<id>": group_id} from the tree."""
    out: dict[str, Any] = {}

    def walk(g: dict[str, Any]) -> None:
        out[f"g{g['id']}"] = g["parent_id"]
        for d in g["devices"]:
            out[f"d{d['id']}"] = g["id"]
        for ch in g["children"]:
            walk(ch)

    for g in (await c.get("/api/tree")).json()["groups"]:
        walk(g)
    return out


async def _audit(rt: Any, prefix: str) -> list[Any]:
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        return await conn.fetch(
            "SELECT * FROM audit_log WHERE action LIKE $1 ORDER BY id", prefix + "%"
        )
    finally:
        await conn.close()


def _changes(row: Any) -> dict:
    c = row["changes"]
    return json.loads(c) if isinstance(c, str) else c


@pytest.fixture
async def layout(admin: httpx.AsyncClient) -> dict[str, int]:
    """root ─ A ─ A1 (device a1d) ; root ─ B ; devices d1, d2 in root."""
    root = await default_group_id(admin)
    a = await _group(admin, "A", root)
    a1 = await _group(admin, "A1", a)
    b = await _group(admin, "B", root)
    d1 = (await make_device(admin, name="d1", group_id=root))["id"]
    d2 = (await make_device(admin, name="d2", group_id=root))["id"]
    a1d = (await make_device(admin, name="a1d", group_id=a1))["id"]
    return {"root": root, "a": a, "a1": a1, "b": b, "d1": d1, "d2": d2, "a1d": a1d}


async def test_single_device_move(admin: httpx.AsyncClient, layout: dict[str, int]) -> None:
    r = await _move(admin, device_ids=[layout["d1"]], target_group_id=layout["b"])
    assert r.status_code == 200, r.text
    assert r.json() == {
        "target_group_id": layout["b"],
        "moved": {"device_ids": [layout["d1"]], "group_ids": []},
        "skipped": {"device_ids": [], "group_ids": []},
    }
    assert (await admin.get(f"/api/devices/{layout['d1']}")).json()["group_id"] == layout["b"]


async def test_bulk_move_group_with_content(
    admin: httpx.AsyncClient, layout: dict[str, int]
) -> None:
    r = await _move(
        admin,
        device_ids=[layout["d1"], layout["d2"], layout["a1d"]],
        group_ids=[layout["a"], layout["a1"]],
        target_group_id=layout["b"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["moved"] == {"device_ids": [layout["d1"], layout["d2"]], "group_ids": [layout["a"]]}
    # A1 and its device travel with A (they are not torn out of it)
    assert body["skipped"] == {"device_ids": [layout["a1d"]], "group_ids": [layout["a1"]]}
    p = await _parents(admin)
    assert p[f"g{layout['a']}"] == layout["b"]
    assert p[f"g{layout['a1']}"] == layout["a"]
    assert p[f"d{layout['a1d']}"] == layout["a1"]
    assert p[f"d{layout['d1']}"] == p[f"d{layout['d2']}"] == layout["b"]


async def test_group_to_top_level_but_not_devices(
    admin: httpx.AsyncClient, layout: dict[str, int]
) -> None:
    r = await _move(admin, group_ids=[layout["a1"]], target_group_id=None)
    assert r.status_code == 200, r.text
    assert (await admin.get(f"/api/groups/{layout['a1']}")).json()["parent_id"] is None
    r = await _move(admin, device_ids=[layout["d1"]], target_group_id=None)
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["loc"] == ["body", "target_group_id"]
    # target_group_id is required (null = top level must be explicit)
    r = await admin.post("/api/tree/move", json={"group_ids": [layout["a1"]]})
    assert r.status_code == 422


async def test_cycle_refused_and_nothing_moves(
    admin: httpx.AsyncClient, layout: dict[str, int]
) -> None:
    before = await _parents(admin)
    for target in (layout["a"], layout["a1"]):  # itself, its own child
        r = await _move(
            admin, device_ids=[layout["d1"]], group_ids=[layout["a"]], target_group_id=target
        )
        assert r.status_code == 400, r.text
        assert r.json()["error"]["code"] == "group_cycle"
        assert r.json()["error"]["details"] == [{"group_id": layout["a"]}]
    assert await _parents(admin) == before


async def test_unknown_ids_fail_everything(
    admin: httpx.AsyncClient, layout: dict[str, int]
) -> None:
    before = await _parents(admin)
    r = await _move(
        admin,
        device_ids=[layout["d1"], 999999],
        group_ids=[888888],
        target_group_id=layout["b"],
    )
    assert r.status_code == 422, r.text
    locs = [d["loc"] for d in r.json()["error"]["details"]]
    assert locs == [["body", "device_ids", 1], ["body", "group_ids", 0]]
    r = await _move(admin, device_ids=[layout["d1"]], target_group_id=777777)
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["loc"] == ["body", "target_group_id"]
    r = await _move(admin, device_ids=[], group_ids=[], target_group_id=layout["b"])
    assert r.status_code == 422
    assert await _parents(admin) == before


async def test_name_clash_is_all_or_nothing(
    admin: httpx.AsyncClient, layout: dict[str, int]
) -> None:
    await _group(admin, "a", layout["b"])  # same name as A (case-insensitive)
    before = await _parents(admin)
    r = await _move(
        admin, device_ids=[layout["d1"]], group_ids=[layout["a"]], target_group_id=layout["b"]
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "conflict"
    assert r.json()["error"]["details"] == [{"field": "name", "group_id": layout["a"]}]
    assert await _parents(admin) == before  # d1 did not move either


async def test_noop_items_are_skipped(admin: httpx.AsyncClient, layout: dict[str, int]) -> None:
    r = await _move(admin, device_ids=[layout["d1"], layout["d1"]], target_group_id=layout["root"])
    assert r.status_code == 200
    assert r.json()["moved"] == {"device_ids": [], "group_ids": []}
    assert r.json()["skipped"]["device_ids"] == [layout["d1"]]


async def test_viewer_forbidden(
    admin: httpx.AsyncClient, viewer: httpx.AsyncClient, layout: dict[str, int]
) -> None:
    before = await _parents(admin)
    r = await _move(viewer, device_ids=[layout["d1"]], target_group_id=layout["b"])
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"
    assert await _parents(admin) == before


async def test_audit_rows(
    admin: httpx.AsyncClient, layout: dict[str, int], feats: Any, rt: Any
) -> None:
    feats(audit=True)
    r = await _move(
        admin, device_ids=[layout["d1"]], group_ids=[layout["a"]], target_group_id=layout["b"]
    )
    assert r.status_code == 200, r.text
    rows = await _audit(rt, "group.move")
    assert [(x["entity_type"], x["entity_id"], x["entity_name"]) for x in rows] == [
        ("group", layout["a"], "A")
    ]
    assert _changes(rows[0]) == {
        "parent_id": [layout["root"], layout["b"]],
        "parent": ["Default", "B"],
    }
    assert rows[0]["actor_name"] == "admin"
    rows = await _audit(rt, "device.move")
    assert [(x["entity_id"], x["device_id"]) for x in rows] == [(layout["d1"], layout["d1"])]
    assert _changes(rows[0]) == {
        "group_id": [layout["root"], layout["b"]],
        "group": ["Default", "B"],
    }
    # a refused move writes nothing
    await _move(admin, group_ids=[layout["a"]], target_group_id=layout["a1"])
    assert len(await _audit(rt, "group.move")) == 1


async def test_inherited_settings_follow(admin: httpx.AsyncClient, layout: dict[str, int]) -> None:
    """Credentials and dependency set on a group apply to what moves into it, and stop applying
    to what moves out; settings on the device itself are kept."""
    gw = (await make_device(admin, name="gw", group_id=layout["root"]))["id"]
    r = await admin.patch(
        f"/api/groups/{layout['b']}",
        json={
            "snmp_credentials": {"version": "v2c", "community": "bee"},
            "dependency": {"mode": "device", "device_id": gw},
        },
    )
    assert r.status_code == 200, r.text
    r = await _move(admin, device_ids=[layout["d1"]], target_group_id=layout["b"])
    assert r.status_code == 200
    d = (await admin.get(f"/api/devices/{layout['d1']}")).json()
    assert d["snmp_credentials"] is None  # nothing copied onto the device
    tree = (await admin.get("/api/tree")).json()

    def find(groups: list[dict], did: int) -> dict | None:
        for g in groups:
            for x in g["devices"]:
                if x["id"] == did:
                    return x
            hit = find(g["children"], did)
            if hit:
                return hit
        return None

    assert find(tree["groups"], layout["d1"])["parent_device_id"] == gw
    r = await _move(admin, device_ids=[layout["d1"]], target_group_id=layout["root"])
    tree = (await admin.get("/api/tree")).json()
    assert find(tree["groups"], layout["d1"])["parent_device_id"] is None


async def test_dependency_cycle_through_inheritance_refused(
    admin: httpx.AsyncClient, layout: dict[str, int]
) -> None:
    """d1 depends on bx; B's devices inherit "depends on d1": moving bx into B closes a loop."""
    bx = (await make_device(admin, name="bx", group_id=layout["root"]))["id"]
    r = await admin.patch(
        f"/api/devices/{layout['d1']}", json={"dependency": {"mode": "device", "device_id": bx}}
    )
    assert r.status_code == 200, r.text
    r = await admin.patch(
        f"/api/groups/{layout['b']}",
        json={"dependency": {"mode": "device", "device_id": layout["d1"]}},
    )
    assert r.status_code == 200, r.text
    before = await _parents(admin)
    r = await _move(admin, device_ids=[layout["d2"], bx], target_group_id=layout["b"])
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "dependency_cycle"
    assert await _parents(admin) == before  # d2 did not move either


# ------------------------------------------------------------------ preview (dry run)


async def _preview(c: httpx.AsyncClient, **body: Any) -> httpx.Response:
    return await c.post("/api/tree/move/preview", json=body)


async def test_preview_snmp_fallback_listed_own_settings_not(
    admin: httpx.AsyncClient, layout: dict[str, int], feats: Any, rt: Any
) -> None:
    """The incident: devices inheriting SNMP from their group fall back to the defaults in a
    group without SNMP details. A device with its own SNMP settings is not affected."""
    feats(audit=True)
    r = await admin.patch(
        f"/api/groups/{layout['a']}",
        json={"snmp_credentials": {"version": "v2c", "community": "x"}},
    )
    assert r.status_code == 200, r.text
    own = (
        await make_device(
            admin, name="own", group_id=layout["a1"], snmp_credentials={"community": "mine"}
        )
    )["id"]
    sensor = await admin.post(
        "/api/sensors",
        json={"device_id": layout["a1d"], "type": "snmp_system", "config": {}},
    )
    assert sensor.status_code == 201, sensor.text
    before = await _parents(admin)
    r = await _preview(admin, group_ids=[layout["a1"]], target_group_id=layout["b"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["moved"] == {"device_ids": [], "group_ids": [layout["a1"]]}
    assert body["devices_affected"] == 2  # a1d and own (inside the moved group)
    assert body["effects"] == [
        {
            "setting": "snmp",
            "before": {"source": "group", "group_id": layout["a"], "group_name": "A"},
            "after": {"source": "default", "group_id": None, "group_name": None},
            "devices": [{"id": layout["a1d"], "name": "a1d", "snmp_sensors": 1}],
        }
    ]
    assert own not in [d["id"] for e in body["effects"] for d in e["devices"]]
    # a dry run: nothing moved, nothing audited
    assert await _parents(admin) == before
    assert await _audit(rt, "group.move") == []


async def test_preview_no_change_and_other_settings(
    admin: httpx.AsyncClient, layout: dict[str, int]
) -> None:
    r = await admin.patch(
        f"/api/groups/{layout['a']}",
        json={"snmp_credentials": {"version": "v2c", "community": "x"}},
    )
    assert r.status_code == 200
    # d1 (root, no SNMP) into A: it gains A's settings; a1d (in A1) into A: no change
    r = await _preview(admin, device_ids=[layout["d1"]], target_group_id=layout["a"])
    assert r.status_code == 200
    first = r.json()
    assert first["effects"][0]["setting"] == "snmp"  # default -> group A (gains settings)
    assert first["effects"][0]["before"]["source"] == "default"
    r = await _preview(admin, device_ids=[layout["a1d"]], target_group_id=layout["a"])
    assert r.json()["effects"] == []
    # SSH and the dependency are compared too
    gw = (await make_device(admin, name="gw", group_id=layout["root"]))["id"]
    r = await admin.patch(
        f"/api/groups/{layout['b']}",
        json={
            "ssh_credentials": {"username": "u", "password": "p"},
            "dependency": {"mode": "device", "device_id": gw},
        },
    )
    assert r.status_code == 200, r.text
    r = await _preview(admin, device_ids=[layout["d2"]], target_group_id=layout["b"])
    effects = {e["setting"]: e for e in r.json()["effects"]}
    assert set(effects) == {"ssh", "dependency"}
    assert effects["ssh"]["before"]["source"] == "none"
    assert effects["ssh"]["after"] == {
        "source": "group",
        "group_id": layout["b"],
        "group_name": "B",
    }
    assert effects["dependency"]["after"]["device_id"] == gw
    assert effects["dependency"]["after"]["device_name"] == "gw"
    assert effects["dependency"]["before"]["source"] == "none"


async def test_preview_same_errors_and_viewer(
    admin: httpx.AsyncClient, viewer: httpx.AsyncClient, layout: dict[str, int]
) -> None:
    r = await _preview(admin, group_ids=[layout["a"]], target_group_id=layout["a1"])
    assert r.status_code == 400 and r.json()["error"]["code"] == "group_cycle"
    r = await _preview(admin, device_ids=[999999], target_group_id=layout["b"])
    assert r.status_code == 422
    r = await _preview(viewer, device_ids=[layout["d1"]], target_group_id=layout["b"])
    assert r.status_code == 403
