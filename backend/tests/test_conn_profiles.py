"""[profiles] Connection profiles over the API and the consumers: CRUD, secrets, permissions,
reference semantics, kind mismatch, delete / usage, request-mode table incl. dormant manual
data (Q2), copy from profile, read-only connection tests (snmpsim, tests/ssh_sim.py), the
consumers (trap v3 users, config backup, tree move, topology, port inventory), rekey and
transform_all."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy import text

from netmon.models import Device
from netmon.poller.snmp_client import SnmpError, snmp_get
from netmon.runtime import Runtime
from netmon.services import conn_profiles, secretbox
from netmon.services.sensors import effective_credentials
from tests.conftest import _plain, default_group_id, make_device, make_sensor, requires_db
from tests.ingest_helpers import LOOPBACK_ALIASES, send_udp, v3_trap, wait_for
from tests.snmpsim_agents import snmpsim_exe, system, write_snmprec
from tests.ssh_sim import Sim
from tests.test_2b3_ingest import features

pytestmark = requires_db

MARK = "Mk" + secrets.token_hex(6)  # secret marker: must appear in no response / audit / log
COMM = f"comm-{MARK}"
V3_USER, V3_KEY = "profv3", "ProfAuthKey2026"
SNMP_IP, SNMP_PORT = "127.0.15.10", 1161
SSH_PW = f"pw-{MARK}"


# ------------------------------------------------------------------ simulators
@pytest.fixture(scope="module")
def agent() -> Iterator[str]:
    """v2c community COMM (and `public` for the readiness check) plus a v3 authNoPriv user."""
    exe = snmpsim_exe()
    if exe is None:
        pytest.skip("snmpsim not installed")
    data = Path(tempfile.mkdtemp(prefix="netmon-profiles-"))
    data.chmod(0o755)
    recs = system("Profile test device", "1.3.6.1.4.1.25506.11.1.42", "prof-sw")
    for community in (COMM, "public", "self"):
        write_snmprec(data / f"{community}.snmprec", recs)
    cache = data / "cache"
    cache.mkdir()
    args = [
        exe, f"--data-dir={data}", f"--cache-dir={cache}",
        f"--agent-udpv4-endpoint={SNMP_IP}:{SNMP_PORT}",
        f"--v3-user={V3_USER}", "--v3-auth-proto=SHA", f"--v3-auth-key={V3_KEY}",
    ]  # fmt: skip
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        shutil.chown(cache, "nobody")
        args += ["--process-user=nobody", "--process-group=nogroup"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603

    async def ready() -> bool:
        creds = {"version": "v2c", "port": SNMP_PORT, "timeout_s": 1, "retries": 0}
        try:
            await snmp_get(SNMP_IP, {**creds, "community": "public"}, ["1.3.6.1.2.1.1.5.0"])
            return True
        except SnmpError:
            return False

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and proc.poll() is None:
        if asyncio.run(ready()):
            break
    else:
        proc.kill()
        shutil.rmtree(data, ignore_errors=True)
        pytest.skip("snmpsim agent did not start")
    yield SNMP_IP
    proc.terminate()
    proc.wait(10)
    shutil.rmtree(data, ignore_errors=True)


@pytest.fixture
async def sim() -> AsyncIterator[Sim]:
    s = await Sim(password=SSH_PW).start()
    yield s
    s.stop()


# ------------------------------------------------------------------ helpers
def snmp_creds(**over: Any) -> dict[str, Any]:
    return {
        "version": "v2c", "port": SNMP_PORT, "timeout_s": 1, "retries": 0, "community": COMM,
        **over,
    }  # fmt: skip


def v3_creds(**over: Any) -> dict[str, Any]:
    return {
        "version": "v3", "port": SNMP_PORT, "timeout_s": 1, "retries": 0,
        "v3_username": V3_USER, "v3_security_level": "authNoPriv",
        "v3_auth_protocol": "SHA", "v3_auth_key": V3_KEY, **over,
    }  # fmt: skip


async def profile(c: httpx.AsyncClient, kind: str, name: str, creds: dict[str, Any]) -> dict:
    r = await c.post(
        "/api/connection-profiles", json={"kind": kind, "name": name, "credentials": creds}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def group(c: httpx.AsyncClient, name: str, parent: int | None = None, **kw: Any) -> dict:
    r = await c.post(
        "/api/groups",
        json={"name": name, "parent_id": parent or await default_group_id(c), **kw},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def audit_rows(rt: Runtime, action: str | None = None) -> list[Any]:
    async with rt.sessionmaker() as s:
        q = "SELECT action, entity_type, entity_id, entity_name, reason, changes FROM audit_log"
        params: dict[str, Any] = {}
        if action:
            q += " WHERE action = :a"
            params["a"] = action
        return list((await s.execute(text(q + " ORDER BY id"), params)).all())


async def all_audit_text(rt: Runtime) -> str:
    async with rt.sessionmaker() as s:
        return json.dumps(
            [list(r) for r in (await s.execute(text("SELECT * FROM audit_log"))).all()],
            default=str,
        )


async def device_creds(rt: Runtime, device_id: int) -> dict[str, Any]:
    async with rt.sessionmaker() as s:
        d = await s.get(Device, device_id)
        assert d is not None
        return await effective_credentials(s, d)


# ------------------------------------------------------------------ CRUD and secrets
async def test_crud_names_kind_secrets(
    rt: Runtime, admin: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await features(admin, audit=True)
    seen: list[str] = []
    snmp = await profile(admin, "snmp", "Core", snmp_creds())
    seen.append(json.dumps(snmp))
    assert snmp["kind"] == "snmp" and snmp["name"] == "Core" and snmp["description"] == ""
    assert snmp["credentials"]["community"] is None and snmp["credentials"]["community_set"]
    assert snmp["credentials"]["credentials_unreadable"] is False
    assert snmp["usage"] == {
        "groups": 0, "devices": 0, "discovery_profiles": 0, "devices_effective": 0,
    }  # fmt: skip
    # the stored SNMP data is encrypted only (no plaintext copy)
    async with rt.sessionmaker() as s:
        data = (
            await s.execute(text("SELECT data FROM connection_profiles WHERE id = :i"),
                            {"i": snmp["id"]})
        ).scalar_one()  # fmt: skip
    assert "community" not in data and data["community_enc"] and MARK not in json.dumps(data)
    # same name in the same kind (case-insensitive) -> 409; in the other kind -> ok
    r = await admin.post(
        "/api/connection-profiles",
        json={"kind": "snmp", "name": "core", "credentials": snmp_creds()},
    )
    assert r.status_code == 409 and r.json()["error"]["details"] == [{"field": "name"}]
    ssh = await profile(admin, "ssh", "CORE", {"username": "netmon", "password": SSH_PW})
    seen.append(json.dumps(ssh))
    assert ssh["credentials"]["password"] is None and ssh["credentials"]["password_set"]
    assert ssh["credentials"]["private_key_set"] is False
    # unknown kind / bad credentials
    r = await admin.post(
        "/api/connection-profiles", json={"kind": "wmi", "name": "x", "credentials": {}}
    )
    assert r.status_code == 422 and r.json()["error"]["details"][0]["loc"] == ["body", "kind"]
    r = await admin.post(
        "/api/connection-profiles",
        json={"kind": "snmp", "name": "v3", "credentials": {"version": "v3"}},
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["loc"] == ["body", "credentials", "v3_username"]
    r = await admin.post(
        "/api/connection-profiles",
        json={"kind": "ssh", "name": "k", "credentials": {"username": "u", "private_key": "x"}},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_private_key"
    r = await admin.post(
        "/api/connection-profiles", json={"kind": "ssh", "name": "n", "credentials": {"x": 1}}
    )
    assert r.status_code == 422
    # list / filter / q
    lst = (await admin.get("/api/connection-profiles")).json()
    seen.append(json.dumps(lst))
    assert [(p["kind"], p["name"]) for p in lst["items"]] == [("snmp", "Core"), ("ssh", "CORE")]
    assert (await admin.get("/api/connection-profiles", params={"kind": "ssh"})).json()[
        "total"
    ] == 1
    assert (await admin.get("/api/connection-profiles", params={"q": "%"})).json()["total"] == 0
    assert (await admin.get("/api/connection-profiles", params={"q": "or"})).json()["total"] == 2
    # PATCH: kind immutable, rename (case only), merge of credentials (secret kept)
    r = await admin.patch(f"/api/connection-profiles/{snmp['id']}", json={"kind": "ssh"})
    assert r.status_code == 422
    r = await admin.patch(f"/api/connection-profiles/{snmp['id']}", json={"kind": "snmp"})
    assert r.status_code == 200
    r = await admin.patch(
        f"/api/connection-profiles/{snmp['id']}",
        json={"name": "CORE", "description": "d", "credentials": {"port": 2161}},
    )
    assert r.status_code == 200, r.text
    seen.append(r.text)
    assert r.json()["name"] == "CORE" and r.json()["credentials"]["port"] == 2161
    assert r.json()["credentials"]["community_set"] is True
    r = await admin.patch(
        f"/api/connection-profiles/{snmp['id']}", json={"credentials": {"community": ""}}
    )
    assert r.json()["credentials"]["community_set"] is False
    r = await admin.patch(
        f"/api/connection-profiles/{snmp['id']}",
        json={"credentials": {"community": COMM, "port": SNMP_PORT}},
    )
    assert r.json()["credentials"]["community_set"] is True
    r = await admin.patch(f"/api/connection-profiles/{ssh['id']}", json={"name": "core"})
    assert r.status_code == 200  # the SNMP "CORE" does not collide with an SSH profile
    assert (await admin.get("/api/connection-profiles/999")).status_code == 404
    # audit: create / update with masked secrets, never a value
    rows = await audit_rows(rt)
    actions = [r.action for r in rows]
    assert actions.count("connection_profile.create") == 2
    assert "connection_profile.update" in actions
    upd = [r for r in rows if r.action == "connection_profile.update"]
    assert any(r.changes.get("name") == ["Core", "CORE"] for r in upd)
    assert any(r.changes.get("credentials.community") == ["***", None] for r in upd)
    audit_text = await all_audit_text(rt)
    for blob in (*seen, audit_text, caplog.text):
        assert MARK not in blob


async def test_unreadable_after_key_change(rt: Runtime, admin: httpx.AsyncClient) -> None:
    p = await profile(admin, "snmp", "K", snmp_creds())
    s = await profile(admin, "ssh", "S", {"username": "u", "password": "p"})
    other = secrets.token_urlsafe(32)
    sealed = secretbox.seal_with(
        other, {"version": "v2c", "community": "x"}, secretbox.SNMP_FIELDS,
        secretbox.conn_profile_owner(p["id"]), False,
    )  # fmt: skip
    from netmon.services import crypto

    ssh_data = {
        "username": "u",
        "port": 22,
        "password_enc": crypto.encrypt(other, "p", b"ssh.password"),
    }
    async with rt.sessionmaker() as session:
        for pid, data in ((p["id"], sealed), (s["id"], ssh_data)):
            await session.execute(
                text("UPDATE connection_profiles SET data = CAST(:d AS jsonb) WHERE id = :i"),
                {"d": json.dumps(data), "i": pid},
            )
        await session.commit()
    got = (await admin.get(f"/api/connection-profiles/{p['id']}")).json()["credentials"]
    assert got["credentials_unreadable"] is True and got["community_set"] is False
    got = (await admin.get(f"/api/connection-profiles/{s['id']}")).json()["credentials"]
    assert got["credentials_unreadable"] is True
    # a device using the unreadable SNMP profile never falls back to the default community
    d = await make_device(admin, snmp_profile_id=p["id"])
    assert (await device_creds(rt, d["id"]))["community"] is None
    # re-entering the secret repairs it
    r = await admin.patch(
        f"/api/connection-profiles/{p['id']}", json={"credentials": {"community": "fixed"}}
    )
    assert r.json()["credentials"]["credentials_unreadable"] is False
    assert (await device_creds(rt, d["id"]))["community"] == "fixed"


async def test_permissions(app: Any, admin: httpx.AsyncClient, viewer: httpx.AsyncClient) -> None:
    p = await profile(admin, "snmp", "Viewer test", snmp_creds())
    g = await group(admin, "G", snmp_profile_id=p["id"])
    d = await make_device(admin, group_id=g["id"], name="vd")
    tok = (await admin.post("/api/tokens", json={"name": "ro", "role": "viewer"})).json()["token"]
    pid = p["id"]
    calls = [
        ("GET", "/api/connection-profiles", None),
        ("POST", "/api/connection-profiles", {"kind": "snmp", "name": "x", "credentials": {}}),
        ("GET", f"/api/connection-profiles/{pid}", None),
        ("PATCH", f"/api/connection-profiles/{pid}", {"name": "y"}),
        ("DELETE", f"/api/connection-profiles/{pid}", None),
        ("GET", f"/api/connection-profiles/{pid}/usage", None),
        ("POST", f"/api/connection-profiles/{pid}/test", {"host": "127.0.0.1"}),
        ("POST", "/api/connection-profiles/test", {"kind": "snmp", "credentials": {}, "host": "h"}),
        ("POST", f"/api/groups/{g['id']}/credentials/snmp/copy-from-profile", {}),
        ("POST", f"/api/devices/{d['id']}/credentials/snmp/copy-from-profile", {}),
        ("PATCH", f"/api/devices/{d['id']}", {"snmp_profile_id": pid}),
        ("PATCH", f"/api/groups/{g['id']}", {"snmp_profile_id": None}),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {tok}"},
    ) as token_client:
        for c in (viewer, token_client):
            for method, path, body in calls:
                r = await c.request(method, path, json=body)
                assert r.status_code == 403, (method, path, r.text)
                assert r.json()["error"]["code"] == "forbidden"
            # a viewer sees the names only, never a field of the profile's data
            gv = (await c.get(f"/api/groups/{g['id']}")).json()
            assert gv["snmp_mode"] == "profile" and gv["snmp_profile_name"] == "Viewer test"
            assert gv["snmp_credentials"] is None
            dv = (await c.get(f"/api/devices/{d['id']}")).json()
            assert dv["effective_snmp"]["profile_name"] == "Viewer test"
            assert dv["snmp_credentials"] is None and "community" not in json.dumps(
                dv["effective_snmp"]
            )
    assert (await admin.get(f"/api/connection-profiles/{pid}")).json()["name"] == "Viewer test"


# ------------------------------------------------------------------ reference semantics
async def test_reference_semantics_and_origin(rt: Runtime, admin: httpx.AsyncClient) -> None:
    p = await profile(admin, "snmp", "Ref", snmp_creds())
    top = await group(admin, "Top")
    mid = await group(admin, "Mid", top["id"])
    d = await make_device(admin, group_id=mid["id"], name="leaf")
    sensor = await make_sensor(admin, d["id"], "snmp_system")
    r = await admin.patch(f"/api/groups/{top['id']}", json={"snmp_profile_id": p["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["snmp_mode"] == "profile" and r.json()["snmp_profile_id"] == p["id"]
    assert r.json()["effective_snmp"]["source"] == "self"
    dev = (await admin.get(f"/api/devices/{d['id']}")).json()
    assert dev["snmp_mode"] == "inherit" and dev["effective_snmp_source"] == "group"
    assert dev["effective_snmp_group_id"] == top["id"]
    assert dev["effective_snmp"] == {
        "source": "group", "group_id": top["id"], "group_name": "Top", "mode": "profile",
        "profile_id": p["id"], "profile_name": "Ref",
    }  # fmt: skip
    from netmon.models import Sensor
    from netmon.services.sensors import build_context

    async def ctx_snmp() -> dict[str, Any]:
        async with rt.sessionmaker() as s:
            sen = await s.get(Sensor, sensor["id"])
            dv = await s.get(Device, d["id"])
            assert sen is not None and dv is not None
            return (await build_context(s, sen, dv)).snmp

    assert (await ctx_snmp())["community"] == COMM
    # edit the profile: the next build_context uses the new values, without any reload call
    await admin.patch(
        f"/api/connection-profiles/{p['id']}", json={"credentials": {"community": "changed"}}
    )
    assert (await ctx_snmp())["community"] == "changed"
    usage = (await admin.get(f"/api/connection-profiles/{p['id']}")).json()["usage"]
    assert usage == {"groups": 1, "devices": 0, "discovery_profiles": 0, "devices_effective": 1}
    # rename: references by id
    await admin.patch(f"/api/connection-profiles/{p['id']}", json={"name": "Renamed"})
    dev = (await admin.get(f"/api/devices/{d['id']}")).json()
    assert dev["effective_snmp"]["profile_name"] == "Renamed"
    # moves: inherit follows the new ancestors
    other = await group(admin, "Other")
    await admin.patch(f"/api/devices/{d['id']}", json={"group_id": other["id"]})
    assert (await device_creds(rt, d["id"]))["community"] == "public"


async def test_kind_mismatch(rt: Runtime, admin: httpx.AsyncClient) -> None:
    sp = await profile(admin, "snmp", "S", snmp_creds())
    hp = await profile(admin, "ssh", "H", {"username": "u", "password": "p"})
    gid = await default_group_id(admin)
    r = await admin.patch(f"/api/groups/{gid}", json={"ssh_profile_id": sp["id"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "profile_kind_mismatch"
    assert r.json()["error"]["details"] == [
        {"loc": ["body", "ssh_profile_id"], "expected": "ssh", "actual": "snmp"}
    ]
    r = await admin.post(
        "/api/devices",
        json={"group_id": gid, "name": "x", "host": "h", "snmp_profile_id": hp["id"]},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "profile_kind_mismatch"
    assert (await admin.get("/api/devices")).json()["total"] == 0  # nothing created
    r = await admin.patch(f"/api/groups/{gid}", json={"snmp_profile_id": 9999})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["msg"] == "unknown profile"
    r = await admin.post(
        "/api/discovery/scans",
        json={"targets": ["127.0.0.1"], "group_id": gid,
              "options": {"snmp_profile_ids": [hp["id"]]}},
    )  # fmt: skip
    assert r.status_code == 422 and r.json()["error"]["code"] == "profile_kind_mismatch"
    assert r.json()["error"]["details"][0]["loc"] == ["body", "options", "snmp_profile_ids", 0]
    r = await admin.post(
        "/api/discovery/profiles",
        json={"name": "dp", "targets": ["127.0.0.1"], "group_id": gid,
              "options": {"ssh_profile_ids": [sp["id"]]}},
    )  # fmt: skip
    assert r.status_code == 422 and r.json()["error"]["code"] == "profile_kind_mismatch"
    r = await admin.post(
        "/api/discovery/profiles",
        json={"name": "dp", "targets": ["127.0.0.1"], "group_id": gid,
              "options": {"snmp_profile_ids": [12345]}},
    )  # fmt: skip
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    # the database itself refuses a mismatched assignment
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        with pytest.raises(asyncpg.ForeignKeyViolationError, match="fk_cpa_profile_kind"):
            await conn.execute(
                "INSERT INTO connection_profile_assignments (kind, profile_id, group_id) "
                "VALUES ('ssh', $1, $2)",
                sp["id"],
                gid,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO connection_profile_assignments (kind, profile_id) VALUES ('snmp', $1)",
                sp["id"],
            )
    finally:
        await conn.close()


async def test_delete_usage_cascade(
    rt: Runtime, admin: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await features(admin, audit=True)
    p = await profile(admin, "snmp", "Used", snmp_creds())
    s = await profile(admin, "ssh", "UsedSSH", {"username": "u", "password": "p"})
    g = await group(admin, "UG", snmp_profile_id=p["id"])
    sub = await group(admin, "Sub", g["id"])
    d1 = await make_device(admin, group_id=sub["id"], name="d1", snmp_profile_id=p["id"])
    await make_device(admin, group_id=sub["id"], name="d2")  # inherits through UG
    dp = await admin.post(
        "/api/discovery/profiles",
        json={"name": "DP", "targets": ["127.0.0.1"], "group_id": g["id"],
              "options": {"snmp_profile_ids": [p["id"]], "ssh_profile_ids": [s["id"]]}},
    )  # fmt: skip
    assert dp.status_code == 201, dp.text
    assert dp.json()["options"]["snmp_profile_ids"] == [p["id"]]
    r = await admin.delete(f"/api/connection-profiles/{p['id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "profile_in_use"
    assert r.json()["error"]["details"] == [{"groups": 1, "devices": 1, "discovery_profiles": 1}]
    r = await admin.delete(f"/api/connection-profiles/{s['id']}")
    assert r.json()["error"]["details"] == [{"groups": 0, "devices": 0, "discovery_profiles": 1}]
    u = (await admin.get(f"/api/connection-profiles/{p['id']}/usage")).json()
    root = (await admin.get(f"/api/groups/{await default_group_id(admin)}")).json()["name"]
    assert u == {
        "groups": [{"id": g["id"], "name": "UG", "path": [root, "UG"]}],
        "devices": [{"id": d1["id"], "name": "d1", "host": "127.0.0.1",
                     "group_id": sub["id"], "group_name": "Sub"}],
        "discovery_profiles": [{"id": dp.json()["id"], "name": "DP"}],
        "devices_effective": 2,
        "truncated": False,
    }  # fmt: skip
    monkeypatch.setattr(conn_profiles, "USAGE_LIST_LIMIT", 0)
    assert (await admin.get(f"/api/connection-profiles/{p['id']}/usage")).json()["truncated"]
    monkeypatch.undo()
    # remove the uses: discovery profile, device; deleting the group cascades its assignment
    await admin.patch(
        f"/api/discovery/profiles/{dp.json()['id']}",
        json={"options": {"snmp_profile_ids": [], "ssh_profile_ids": []}},
    )
    await admin.patch(f"/api/devices/{d1['id']}", json={"snmp_profile_id": None})
    assert (await admin.delete(f"/api/connection-profiles/{p['id']}")).status_code == 409
    assert (await admin.delete(f"/api/groups/{g['id']}")).status_code == 204
    assert (await admin.delete(f"/api/connection-profiles/{p['id']}")).status_code == 204
    assert (await admin.delete(f"/api/connection-profiles/{s['id']}")).status_code == 204
    assert (await admin.get(f"/api/connection-profiles/{p['id']}")).status_code == 404
    assert [r.action for r in await audit_rows(rt, "connection_profile.delete")] == [
        "connection_profile.delete"
    ] * 2


# ------------------------------------------------------------------ request-mode table
async def test_backward_compatibility_and_modes(rt: Runtime, admin: httpx.AsyncClient) -> None:
    await features(admin, audit=True)
    p = await profile(admin, "snmp", "BC", snmp_creds())
    q = await profile(admin, "snmp", "BC2", snmp_creds(community="other"))
    d = await make_device(admin, name="bc", snmp_profile_id=p["id"])
    assert d["snmp_mode"] == "profile" and d["snmp_has_saved_manual"] is False
    did = d["id"]
    # PATCH without the new fields keeps the profile
    r = await admin.patch(f"/api/devices/{did}", json={"description": "x"})
    assert r.json()["snmp_mode"] == "profile"
    # both fields -> 422
    r = await admin.patch(
        f"/api/devices/{did}", json={"snmp_profile_id": p["id"], "snmp_credentials": snmp_creds()}
    )
    assert r.status_code == 422 and r.json()["error"]["details"][0]["loc"] == [
        "body",
        "snmp_profile_id",
    ]
    # snmp_credentials object -> manual (new object), reference gone
    r = await admin.patch(f"/api/devices/{did}", json={"snmp_credentials": {"community": "m1"}})
    j = r.json()
    assert j["snmp_mode"] == "manual" and j["snmp_profile_id"] is None
    assert (
        j["snmp_credentials"]["community_set"]
        and (await device_creds(rt, did))["community"] == "m1"
    )
    # snmp_profile_id: null on a manual node changes nothing
    r = await admin.patch(f"/api/devices/{did}", json={"snmp_profile_id": None})
    assert r.json()["snmp_mode"] == "manual"
    # null -> inherit
    r = await admin.patch(f"/api/devices/{did}", json={"snmp_credentials": None})
    assert r.json()["snmp_mode"] == "inherit" and r.json()["snmp_credentials"] is None
    # profile then null only -> inherit (no saved data)
    await admin.patch(f"/api/devices/{did}", json={"snmp_profile_id": q["id"]})
    r = await admin.patch(f"/api/devices/{did}", json={"snmp_profile_id": None})
    assert r.json()["snmp_mode"] == "inherit"
    # audit keys snmp_mode / snmp_profile (profiles by name)
    rows = [r.changes for r in await audit_rows(rt, "device.update")]
    pairs = [(c.get("snmp_mode"), c.get("snmp_profile")) for c in rows]
    assert (["profile", "manual"], ["BC", None]) in pairs
    assert (["inherit", "profile"], [None, "BC2"]) in pairs
    created = (await audit_rows(rt, "device.create"))[0].changes
    assert created["snmp_mode"] == [None, "profile"] and created["snmp_profile"] == [None, "BC"]
    assert MARK not in await all_audit_text(rt)
    # SSH: the same table on groups
    s = await profile(admin, "ssh", "SSHP", {"username": "u", "password": SSH_PW})
    g = await group(admin, "SG", ssh_profile_id=s["id"])
    assert g["ssh_mode"] == "profile" and g["ssh_credentials"] is None
    r = await admin.patch(
        f"/api/groups/{g['id']}", json={"ssh_credentials": {"username": "m", "password": "x"}}
    )
    assert r.json()["ssh_mode"] == "manual" and r.json()["ssh_credentials"]["username"] == "m"
    assert (await admin.patch(f"/api/groups/{g['id']}", json={"ssh_profile_id": None})).json()[
        "ssh_mode"
    ] == "manual"
    r = await admin.patch(f"/api/groups/{g['id']}", json={"ssh_credentials": None})
    assert r.json()["ssh_mode"] == "inherit"


async def test_dormant_manual_data(rt: Runtime, admin: httpx.AsyncClient, agent: str) -> None:
    """Owner decision Q2: manual -> profile keeps the manual data dormant; it is restored by
    {"K_profile_id": null}, discarded by inherit or `K_credentials: null` with a profile."""
    await features(admin, audit=True)
    p = await profile(admin, "snmp", "Dorm", snmp_creds(community="prof-comm"))
    d = await make_device(admin, host=agent, name="dorm", snmp_credentials=snmp_creds())
    did = d["id"]
    r = await admin.patch(f"/api/devices/{did}", json={"snmp_profile_id": p["id"]})
    j = r.json()
    assert j["snmp_mode"] == "profile" and j["snmp_has_saved_manual"] is True
    assert j["snmp_credentials"] is None  # nothing of the dormant data leaves
    assert (await device_creds(rt, did))["community"] == "prof-comm"  # the profile decides
    async with rt.sessionmaker() as s:
        col = (await s.get(Device, did)).snmp_credentials  # type: ignore[union-attr]
    assert col is not None and col.get("community_enc")  # still stored, still encrypted
    # restore: {"snmp_profile_id": null}
    r = await admin.patch(f"/api/devices/{did}", json={"snmp_profile_id": None})
    j = r.json()
    assert j["snmp_mode"] == "manual" and j["snmp_has_saved_manual"] is False
    creds = await device_creds(rt, did)
    assert creds["community"] == COMM
    assert (await snmp_get(agent, creds, ["1.3.6.1.2.1.1.5.0"]))["1.3.6.1.2.1.1.5.0"] == b"prof-sw"
    rows = [r.changes for r in await audit_rows(rt, "device.update")]
    assert any(c.get("snmp_mode") == ["manual", "profile"] and "snmp_credentials.community"
               not in c for c in rows)  # fmt: skip
    assert any(c.get("snmp_mode") == ["profile", "manual"] for c in rows)
    # a credentials object from profile mode replaces the dormant data (a new object)
    await admin.patch(f"/api/devices/{did}", json={"snmp_profile_id": p["id"]})
    r = await admin.patch(f"/api/devices/{did}", json={"snmp_credentials": {"version": "v2c"}})
    assert r.json()["snmp_mode"] == "manual"
    assert (await device_creds(rt, did))["community"] == "public"  # not taken from dormant data
    # profile + snmp_credentials: null -> dormant data discarded
    await admin.patch(f"/api/devices/{did}", json={"snmp_credentials": snmp_creds()})
    r = await admin.patch(
        f"/api/devices/{did}", json={"snmp_profile_id": p["id"], "snmp_credentials": None}
    )
    assert r.json()["snmp_mode"] == "profile" and r.json()["snmp_has_saved_manual"] is False
    # inherit discards dormant data
    await admin.patch(f"/api/devices/{did}", json={"snmp_profile_id": None})
    await admin.patch(f"/api/devices/{did}", json={"snmp_credentials": snmp_creds()})
    await admin.patch(f"/api/devices/{did}", json={"snmp_profile_id": p["id"]})
    r = await admin.patch(f"/api/devices/{did}", json={"snmp_credentials": None})
    assert r.json()["snmp_mode"] == "inherit" and r.json()["snmp_has_saved_manual"] is False
    async with rt.sessionmaker() as s:
        assert (await s.get(Device, did)).snmp_credentials is None  # type: ignore[union-attr]
    assert MARK not in await all_audit_text(rt)


async def test_copy_from_profile(rt: Runtime, admin: httpx.AsyncClient, agent: str) -> None:
    await features(admin, audit=True)
    p = await profile(admin, "snmp", "Copy", snmp_creds())
    d = await make_device(admin, host=agent, name="cp", snmp_profile_id=p["id"])
    did = d["id"]
    r = await admin.post(f"/api/devices/{did}/credentials/snmp/copy-from-profile", json={})
    assert r.status_code == 200, r.text
    assert r.json()["snmp_mode"] == "manual" and r.json()["snmp_profile_id"] is None
    assert r.json()["snmp_credentials"]["community_set"] is True
    async with rt.sessionmaker() as s:
        col = (await s.get(Device, did)).snmp_credentials  # type: ignore[union-attr]
    # re-sealed for the device owner (the AAD names the device, not the profile)
    from netmon.services import crypto

    key = secretbox.state()[0]
    assert secretbox._dec(key, col["community_enc"], f"device:{did}", "community") == COMM  # noqa: SLF001
    with pytest.raises(crypto.DecryptError):
        secretbox._dec(  # noqa: SLF001
            key, col["community_enc"], secretbox.conn_profile_owner(p["id"]), "community"
        )
    creds = await device_creds(rt, did)
    assert (await snmp_get(agent, creds, ["1.3.6.1.2.1.1.5.0"]))["1.3.6.1.2.1.1.5.0"] == b"prof-sw"
    # a later profile edit does not affect the device
    await admin.patch(
        f"/api/connection-profiles/{p['id']}", json={"credentials": {"community": "z"}}
    )
    assert (await device_creds(rt, did))["community"] == COMM
    rows = await audit_rows(rt, "device.update")
    assert rows[-1].reason == "copy_from_profile"
    assert rows[-1].changes["snmp_mode"] == ["profile", "manual"]
    # rules: {} needs mode profile; explicit profile id; kind mismatch; unknown kind; overwrite
    r = await admin.post(f"/api/devices/{did}/credentials/snmp/copy-from-profile", json={})
    assert r.status_code == 422
    r = await admin.post(
        f"/api/devices/{did}/credentials/snmp/copy-from-profile", json={"profile_id": p["id"]}
    )
    assert r.status_code == 200 and (await device_creds(rt, did))["community"] == "z"
    s = await profile(admin, "ssh", "CopySSH", {"username": "cu", "password": "p"})
    r = await admin.post(
        f"/api/devices/{did}/credentials/snmp/copy-from-profile", json={"profile_id": s["id"]}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "profile_kind_mismatch"
    assert (
        await admin.post(f"/api/devices/{did}/credentials/wmi/copy-from-profile", json={})
    ).status_code == 404
    gid = await default_group_id(admin)
    await admin.patch(
        f"/api/groups/{gid}", json={"ssh_credentials": {"username": "old", "password": "x"}}
    )
    await admin.patch(f"/api/groups/{gid}", json={"ssh_profile_id": s["id"]})  # dormant "old"
    r = await admin.post(f"/api/groups/{gid}/credentials/ssh/copy-from-profile", json={})
    assert r.status_code == 200 and r.json()["ssh_mode"] == "manual"
    assert r.json()["ssh_credentials"]["username"] == "cu"  # dormant data overwritten


# ------------------------------------------------------------------ connection tests
async def test_snmp_profile_test(rt: Runtime, admin: httpx.AsyncClient, agent: str) -> None:
    await features(admin, audit=True)
    p = await profile(admin, "snmp", "T", snmp_creds())
    d = await make_device(admin, host=agent, name="t")
    r = await admin.post(f"/api/connection-profiles/{p['id']}/test", json={"device_id": d["id"]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["kind"] == "snmp" and j["host"] == agent and j["version"] == "v2c"
    assert j["sys_name"] == "prof-sw" and j["sys_descr"] == "Profile test device"
    assert j["sys_object_id"] == "1.3.6.1.4.1.25506.11.1.42" and j["ms"] >= 0
    # wrong community (unsaved edit merged over the profile) -> 502, nothing saved
    r = await admin.post(
        f"/api/connection-profiles/{p['id']}/test",
        json={"host": agent, "credentials": {"community": "wrong"}},
    )
    assert r.status_code == 502 and r.json()["error"]["code"] == "snmp_error"
    assert (await device_creds(rt, d["id"]))["community"] == "public"
    got = (await admin.get(f"/api/connection-profiles/{p['id']}")).json()
    assert got["credentials"]["community_set"]
    # v3: right key ok, wrong key 502
    r = await admin.post(
        "/api/connection-profiles/test",
        json={"kind": "snmp", "credentials": v3_creds(), "host": agent},
    )
    assert r.status_code == 200 and r.json()["version"] == "v3", r.text
    r = await admin.post(
        "/api/connection-profiles/test",
        json={"kind": "snmp", "credentials": v3_creds(v3_auth_key="WrongKey2026"), "host": agent},
    )
    assert r.status_code == 502 and r.json()["error"]["code"] == "snmp_error"
    # host rules
    for bad in ({"host": "10.0.0.0/24"}, {"host": "10.0.0.1-5"}, {}, {"host": "h", "device_id": 1}):
        r = await admin.post(f"/api/connection-profiles/{p['id']}/test", json=bad)
        assert r.status_code == 422, bad
    r = await admin.post(f"/api/connection-profiles/{p['id']}/test", json={"device_id": 999})
    assert r.status_code == 404
    rows = await audit_rows(rt, "connection_profile.test")
    assert rows[0].entity_id == p["id"] and rows[0].changes == {
        "target": [None, f"device:{d['id']}"], "ok": [None, True],
    }  # fmt: skip
    assert rows[1].changes["ok"] == [None, False]
    assert any(r.entity_id is None and r.changes["target"] == [None, f"host:{agent}"] for r in rows)


async def test_ssh_profile_test(rt: Runtime, admin: httpx.AsyncClient, sim: Sim) -> None:
    await features(admin, audit=True)
    p = await profile(
        admin, "ssh", "SSH T", {"username": "netmon", "password": SSH_PW, "port": sim.port}
    )
    # host target, no driver: login only
    r = await admin.post(f"/api/connection-profiles/{p['id']}/test", json={"host": "127.0.0.1"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["kind"] == "ssh" and j["driver"] is None
    assert [s["step"] for s in j["steps"]] == ["connect", "host_key", "auth"]
    assert sim.commands == []
    # with a driver: prompt and the session-only setup command; no backup command
    r = await admin.post(
        f"/api/connection-profiles/{p['id']}/test",
        json={"host": "127.0.0.1", "driver": "hpe_comware"},
    )
    assert r.status_code == 200, r.text
    assert [s["step"] for s in r.json()["steps"]] == [
        "connect",
        "host_key",
        "auth",
        "prompt",
        "setup",
    ]
    assert sim.commands == ["screen-length disable", "exit"]
    assert "display current-configuration" not in sim.commands
    # wrong password -> ssh_error step auth
    r = await admin.post(
        f"/api/connection-profiles/{p['id']}/test",
        json={"host": "127.0.0.1", "credentials": {"password": "bad"}},
    )
    assert r.status_code == 502 and r.json()["error"]["code"] == "ssh_error"
    assert r.json()["error"]["details"][0]["step"] == "auth" and "bad" not in r.text
    # a device: no pin is written by a profile test
    d = await make_device(admin, name="ssh-dev", host="127.0.0.1")
    r = await admin.post(f"/api/connection-profiles/{p['id']}/test", json={"device_id": d["id"]})
    assert r.status_code == 200
    async with rt.sessionmaker() as s:
        assert (await s.execute(text("SELECT count(*) FROM ssh_host_keys"))).scalar() == 0
        # a device with a pinned key that differs: step host_key fails
        await s.execute(
            text(
                "INSERT INTO ssh_host_keys (device_id, algorithm, fingerprint, public_key, state, "
                "pinned_at) VALUES (:d, 'ssh-ed25519', 'SHA256:other', 'x', 'pinned', now())"
            ),
            {"d": d["id"]},
        )
        await s.commit()
    r = await admin.post(f"/api/connection-profiles/{p['id']}/test", json={"device_id": d["id"]})
    assert r.status_code == 502 and r.json()["error"]["details"][0]["step"] == "host_key"
    async with rt.sessionmaker() as s:
        fp = (await s.execute(text("SELECT fingerprint FROM ssh_host_keys"))).scalar()
    assert fp == "SHA256:other"  # unchanged
    # unsaved profile; snmp-only driver field rejected for SNMP
    r = await admin.post(
        "/api/connection-profiles/test",
        json={"kind": "ssh", "host": "127.0.0.1",
              "credentials": {"username": "netmon", "password": SSH_PW, "port": sim.port}},
    )  # fmt: skip
    assert r.status_code == 200 and r.json()["ok"]
    r = await admin.post(
        "/api/connection-profiles/test",
        json={"kind": "snmp", "host": "127.0.0.1", "credentials": {}, "driver": "hpe_comware"},
    )
    assert r.status_code == 422
    assert len(await audit_rows(rt, "connection_profile.test")) == 6
    assert MARK not in await all_audit_text(rt)


# ------------------------------------------------------------------ consumers
async def test_trap_v3_user_from_profile(rt: Runtime, admin: httpx.AsyncClient) -> None:
    if not LOOPBACK_ALIASES:
        pytest.skip("needs 127.0.0.x source addresses")
    creds = {"version": "v3", "v3_username": "profuser", "v3_security_level": "authPriv",
             "v3_auth_protocol": "SHA", "v3_auth_key": "authpass321",
             "v3_priv_protocol": "AES128", "v3_priv_key": "privpass321"}  # fmt: skip
    p = await profile(admin, "snmp", "Trap", creds)
    await make_device(admin, name="trapdev", host="127.0.0.87")
    await features(admin, traps=True)
    tp = rt.ingest.trap_rx.port
    keys = {"auth_key": "authpass321", "priv_key": "privpass321"}
    link_down = "1.3.6.1.6.3.1.1.5.3"

    async def has(n: int) -> bool:
        return len((await admin.get("/api/traps")).json()["items"]) >= n

    # every SNMP profile's v3 user is known (the rebuild runs on config.changed)
    await admin.patch(
        f"/api/groups/{await default_group_id(admin)}", json={"snmp_profile_id": p["id"]}
    )
    await asyncio.sleep(1.0)
    await rt.ingest.rebuild_sources()
    assert rt.ingest.v3.device_users  # the device's effective user comes from the profile
    send_udp(tp, await v3_trap("8000000001020399", "profuser", link_down, **keys), "127.0.0.87")
    await wait_for(lambda: has(1))
    items = (await admin.get("/api/traps")).json()["items"]
    assert items[0]["security_name"] == "profuser" and items[0]["device_name"] == "trapdev"


async def test_config_backup_through_ssh_profile(
    rt: Runtime, admin: httpx.AsyncClient, sim: Sim
) -> None:
    from netmon.configbackup import service

    p = await profile(
        admin, "ssh", "Backup", {"username": "netmon", "password": SSH_PW, "port": sim.port}
    )
    g = await group(admin, "BG", ssh_profile_id=p["id"])
    d = await make_device(
        admin, group_id=g["id"], name="sw", host="127.0.0.1", config_backup_driver="hpe_comware"
    )
    assert d["effective_ssh_source"] == "group" and d["effective_ssh"]["profile_name"] == "Backup"
    async with rt.sessionmaker() as s:
        (ctx,) = await service.load_contexts(s, rt.secret_key, [d["id"]])
    assert ctx.ssh is not None and ctx.ssh["username"] == "netmon" and ctx.eligible == (True, "ok")
    assert ctx.ssh_source == "group" and ctx.ssh_group_id == g["id"]
    # the device SSH test merges over the profile-aware effective credentials
    await features(admin, config_backup=True)
    r = await admin.post(f"/api/devices/{d['id']}/ssh/test", json={})
    assert r.status_code == 200, r.text
    # an unreadable SSH profile makes its devices ineligible (no_credentials)
    async with rt.sessionmaker() as s:
        await s.execute(
            text("UPDATE connection_profiles SET data = jsonb_set(data, '{password_enc}', "
                 "'\"garbage\"') WHERE id = :i"),
            {"i": p["id"]},
        )  # fmt: skip
        await s.commit()
    async with rt.sessionmaker() as s:
        (ctx,) = await service.load_contexts(s, rt.secret_key, [d["id"]])
    assert ctx.eligible == (False, "no_credentials")


async def test_tree_move_preview_profiles(admin: httpx.AsyncClient) -> None:
    a = await profile(admin, "snmp", "PA", snmp_creds())
    b = await profile(admin, "snmp", "PB", snmp_creds(community="b"))
    ga = await group(admin, "GA", snmp_profile_id=a["id"])
    gb = await group(admin, "GB", snmp_profile_id=b["id"])
    d = await make_device(admin, group_id=ga["id"], name="mv")
    r = await admin.post(
        "/api/tree/move/preview", json={"device_ids": [d["id"]], "target_group_id": gb["id"]}
    )
    assert r.status_code == 200, r.text
    (eff,) = [e for e in r.json()["effects"] if e["setting"] == "snmp"]
    assert eff["before"] == {"source": "group", "group_id": ga["id"], "group_name": "GA",
                             "profile_id": a["id"], "profile_name": "PA"}  # fmt: skip
    assert eff["after"]["profile_name"] == "PB" and eff["after"]["group_id"] == gb["id"]
    # a profile-only change of the same decider is a change; the same profile is not
    await admin.patch(f"/api/groups/{gb['id']}", json={"snmp_profile_id": a["id"]})
    r = await admin.post(
        "/api/tree/move/preview", json={"device_ids": [d["id"]], "target_group_id": gb["id"]}
    )
    (eff,) = [e for e in r.json()["effects"] if e["setting"] == "snmp"]
    assert eff["after"]["profile_id"] == eff["before"]["profile_id"] == a["id"]  # group differs


async def test_topology_and_ports_use_profile(
    rt: Runtime, admin: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = await profile(admin, "snmp", "Topo", snmp_creds())
    d = await make_device(admin, name="topo", host="127.0.0.99", snmp_profile_id=p["id"])
    await make_sensor(admin, d["id"], "snmp_system")
    from netmon.ports import runner as ports_runner
    from netmon.topology import refresh

    got = await ports_runner._credentials(rt, d["id"])  # noqa: SLF001
    assert got is not None and got[1]["community"] == COMM
    seen: dict[str, Any] = {}

    async def fake_collect(host: str, creds: dict[str, Any], budget: float) -> Any:
        seen[host] = creds
        raise RuntimeError("stop")

    monkeypatch.setattr(refresh, "collect_with_budget", fake_collect)
    with pytest.raises(RuntimeError):
        await rt.topology.refresh({})
    assert seen["127.0.0.99"]["community"] == COMM


# ------------------------------------------------------------------ secrets tooling
async def test_rekey_and_transform_all(rt: Runtime, admin: httpx.AsyncClient) -> None:
    from netmon.configbackup import credentials
    from netmon.services import crypto, rekey

    p = await profile(admin, "snmp", "RK", snmp_creds())
    s = await profile(admin, "ssh", "RKS", {"username": "u", "password": SSH_PW})
    # dormant manual SNMP data is covered too
    d = await make_device(admin, name="rk", snmp_credentials=snmp_creds(community="dormant"))
    await admin.patch(f"/api/devices/{d['id']}", json={"snmp_profile_id": p["id"]})
    # transform_all in every mode leaves profiles encrypted-only (the 1.4.1 form: untouched)
    for keep in (True, False, None):
        async with rt.engine.begin() as conn:  # type: ignore[union-attr]
            await conn.run_sync(
                lambda c, k=keep: secretbox.transform_all(c, rt.secret_key, k)  # type: ignore[misc]
            )
            data = (
                await conn.execute(text("SELECT data FROM connection_profiles WHERE id = :i"),
                                   {"i": p["id"]})
            ).scalar_one()  # fmt: skip
            assert "community" not in data and data["community_enc"], keep
            await conn.rollback()
    new_key = secrets.token_urlsafe(48)
    async with rt.engine.connect() as conn:  # type: ignore[union-attr]
        n = await conn.run_sync(lambda c: rekey.rekey_all(c, rt.secret_key, new_key))
        assert n >= 3
        rows = {
            r.id: r.data
            for r in (await conn.execute(text("SELECT id, data FROM connection_profiles"))).all()
        }
        dev = (
            await conn.execute(text("SELECT snmp_credentials FROM devices WHERE id = :i"),
                               {"i": d["id"]})
        ).scalar_one()  # fmt: skip
        await conn.rollback()  # leave the suite's key in place
    plain, bad = secretbox.open_with(
        new_key, rows[p["id"]], secretbox.SNMP_FIELDS, secretbox.conn_profile_owner(p["id"])
    )
    assert not bad and plain["community"] == COMM
    assert credentials.decrypt(rows[s["id"]], new_key).password == SSH_PW
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(rt.secret_key, rows[s["id"]]["password_enc"], b"ssh.password")
    dplain, dbad = secretbox.open_with(
        new_key, {k: v for k, v in dev.items() if k != "community"},
        secretbox.SNMP_FIELDS, f"device:{d['id']}",
    )  # fmt: skip
    assert not dbad and dplain["community"] == "dormant"


async def test_ws_and_logs_never_carry_secrets(
    rt: Runtime, admin: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.test_discovery import FakeWS

    caplog.set_level(logging.DEBUG)
    ws = FakeWS()
    cid = rt.hub.register(ws, user_id=1, is_admin=True)  # type: ignore[arg-type]
    try:
        p = await profile(admin, "snmp", "WS", snmp_creds())
        await admin.patch(f"/api/connection-profiles/{p['id']}", json={"name": "WS2"})
        g = await default_group_id(admin)
        await admin.patch(f"/api/groups/{g}", json={"snmp_profile_id": p["id"]})
        await admin.patch(f"/api/groups/{g}", json={"snmp_profile_id": None})
        await admin.delete(f"/api/connection-profiles/{p['id']}")
        await asyncio.sleep(0.2)
    finally:
        await rt.hub.unregister(cid)
    msgs = [m for m in ws.sent if m["type"] == "config.changed"]
    prof = [m["data"] for m in msgs if m["data"]["entity"] == "connection_profile"]
    assert [m["action"] for m in prof] == ["created", "updated", "deleted"]
    assert prof[0] == {"entity": "connection_profile", "id": p["id"], "action": "created"}
    assert any(m["data"]["entity"] == "group" for m in msgs)
    assert MARK not in json.dumps(ws.sent) and MARK not in caplog.text


async def test_device_list_does_not_query_per_device(admin: httpx.AsyncClient, rt: Runtime) -> None:
    """Inventory loads assignments and profiles in one query each."""
    p = await profile(admin, "snmp", "Many", snmp_creds())
    for i in range(5):
        await make_device(admin, name=f"m{i}", snmp_profile_id=p["id"] if i % 2 else None)
    from sqlalchemy import event

    statements: list[str] = []

    def count(*args: Any) -> None:
        statements.append(str(args[2]))

    engine = rt.engine.sync_engine  # type: ignore[union-attr]
    event.listen(engine, "before_cursor_execute", count)
    try:
        r = await admin.get("/api/devices")
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert r.json()["total"] == 5
    assert sum("connection_profile_assignments" in s for s in statements) == 1
    assert sum("FROM connection_profiles" in s for s in statements) == 1
    names = {d["name"]: d["snmp_profile_name"] for d in r.json()["items"]}
    assert names == {"m0": None, "m1": "Many", "m2": None, "m3": "Many", "m4": None}
