"""[profiles] Discovery with connection profiles: SNMP profiles in the credential order, the
import link rules (save_snmp), and the opt-in SSH login check (owner decision Q3) against
tests/ssh_sim.py — default off, one attempt per candidate, stop at the first success, no
command, no host key stored, result recording, import (save_ssh)."""

from __future__ import annotations

import json
import secrets
import socket
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from netmon.runtime import Runtime
from tests.conftest import default_group_id, requires_db
from tests.snmpsim_agents import Agent, snmpsim_exe, start_agent, system
from tests.ssh_sim import Sim
from tests.test_2b3_ingest import features
from tests.test_discovery import wait_scan

pytestmark = requires_db

MARK = "Dk" + secrets.token_hex(6)
PROF_COMM = "profcomm"
PORT = 1161
SSH_PW = f"pw-{MARK}"


def _free(ip: str) -> bool:
    for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
        s = socket.socket(socket.AF_INET, kind)
        try:
            s.bind((ip, PORT))
        except OSError:
            return False
        finally:
            s.close()
    return True


@pytest.fixture(scope="module")
def net() -> Iterator[tuple[str, Agent]]:
    if snmpsim_exe() is None:
        pytest.skip("snmpsim not installed")
    for prefix in ("127.0.16", "127.0.17", "127.0.18"):
        if _free(f"{prefix}.11") and _free(f"{prefix}.13"):
            break
    else:
        pytest.skip("no free loopback block")
    agent = start_agent(
        f"{prefix}.11", PORT, {PROF_COMM: system("Profiled", "1.3.6.1.4.1.9.1.1", "prof-host")}
    )
    if agent is None:
        pytest.skip("snmpsim agent did not start")
    yield prefix, agent
    agent.stop()


@pytest.fixture
async def ssh_host(net: tuple[str, Agent]) -> AsyncIterator[Sim]:
    s = await Sim(password=SSH_PW, host=f"{net[0]}.13").start()
    yield s
    s.stop()


async def profile(c: httpx.AsyncClient, kind: str, name: str, creds: dict[str, Any]) -> dict:
    r = await c.post(
        "/api/connection-profiles", json={"kind": kind, "name": name, "credentials": creds}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def scan(c: httpx.AsyncClient, target: str, gid: int, **opts: Any) -> dict[str, Any]:
    options = {"ping": False, "dns": False, "fingerprint": False, "snmp_timeout_s": 1, **opts}
    r = await c.post(
        "/api/discovery/scans", json={"targets": [target], "group_id": gid, "options": options}
    )
    assert r.status_code == 202, r.text
    done = await wait_scan(c, r.json()["id"])
    assert done["status"] == "completed", done
    return done


async def results(c: httpx.AsyncClient, scan_id: int) -> list[dict[str, Any]]:
    r = await c.get(f"/api/discovery/scans/{scan_id}/results")
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def do_import(
    c: httpx.AsyncClient, scan_id: int, rid: int, gid: int | None = None, **item: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {"items": [{"result_id": rid, "template_id": "generic_ping", **item}]}
    if gid is not None:
        body["group_id"] = gid
    r = await c.post(f"/api/discovery/scans/{scan_id}/import", json=body)
    assert r.status_code == 200, r.text
    assert not r.json()["errors"], r.json()
    (created,) = r.json()["created"]
    return created


async def device_creds(rt: Runtime, did: int) -> dict[str, Any]:
    from netmon.models import Device
    from netmon.services.sensors import effective_credentials

    async with rt.sessionmaker() as s:
        d = await s.get(Device, did)
        assert d is not None
        return await effective_credentials(s, d)


# ------------------------------------------------------------------ SNMP profiles
async def test_scan_with_snmp_profiles_and_import_rules(
    rt: Runtime, admin: httpx.AsyncClient, net: tuple[str, Agent]
) -> None:
    host = f"{net[0]}.11"
    gid = await default_group_id(admin)
    # the group's own credentials do not answer; the second profile does
    await admin.patch(
        f"/api/groups/{gid}", json={"snmp_credentials": {"community": "grp", "port": PORT}}
    )
    bad = await profile(admin, "snmp", "Bad", {"community": "nope", "port": PORT})
    good = await profile(admin, "snmp", "Good", {"community": PROF_COMM, "port": PORT})
    done = await scan(admin, host, gid, tcp=False, snmp_profile_ids=[bad["id"], good["id"]])
    assert done["options"]["snmp_profile_ids"] == [bad["id"], good["id"]]
    (res,) = await results(admin, done["id"])
    assert res["snmp_ok"] and res["snmp_credential"] == "profile"
    assert res["snmp_profile_id"] == good["id"] and res["snmp_profile_name"] == "Good"
    assert res["ssh_attempted"] is False and res["ssh_ok"] is False
    async with rt.sessionmaker() as s:
        row = (
            await s.execute(text("SELECT snmp_credential_index, snmp_profile_id FROM "
                                 "discovery_results WHERE id = :i"), {"i": res["id"]})
        ).one()  # fmt: skip
    assert tuple(row) == (2, good["id"])  # group 0, Bad 1, Good 2
    # import: linked to the profile that answered
    created = await do_import(admin, done["id"], res["id"])
    assert created["snmp_mode"] == "profile" and created["snmp_profile_id"] == good["id"]
    assert created["ssh_mode"] is None
    dev = (await admin.get(f"/api/devices/{created['device_id']}")).json()
    assert dev["snmp_mode"] == "profile" and dev["snmp_profile_name"] == "Good"
    assert (await device_creds(rt, dev["id"]))["community"] == PROF_COMM
    # save_snmp false: nothing stored, the device inherits from the import group
    await admin.delete(f"/api/devices/{dev['id']}")
    created = await do_import(admin, done["id"], res["id"], save_snmp=False)
    assert created["snmp_mode"] == "inherit" and created["snmp_profile_id"] is None
    dev = (await admin.get(f"/api/devices/{created['device_id']}")).json()
    assert dev["snmp_mode"] == "inherit" and dev["snmp_credentials"] is None
    # an import group that already inherits that very profile -> inherit
    await admin.delete(f"/api/devices/{dev['id']}")
    sub = (
        await admin.post(
            "/api/groups",
            json={"name": "UsesGood", "parent_id": gid, "snmp_profile_id": good["id"]},
        )
    ).json()
    created = await do_import(admin, done["id"], res["id"], gid=sub["id"])
    assert created["snmp_mode"] == "inherit"
    await admin.delete(f"/api/devices/{created['device_id']}")
    await admin.delete(f"/api/groups/{sub['id']}")
    # the profile deleted between scan and import -> a manual copy of what answered
    assert (await admin.delete(f"/api/connection-profiles/{good['id']}")).status_code == 204
    created = await do_import(admin, done["id"], res["id"])
    assert created["snmp_mode"] == "manual" and created["snmp_profile_id"] is None
    assert (await device_creds(rt, created["device_id"]))["community"] == PROF_COMM
    res = (await results(admin, done["id"]))[0]
    assert res["snmp_profile_id"] == good["id"] and res["snmp_profile_name"] is None


async def test_port_override_gives_manual_copy(
    rt: Runtime, admin: httpx.AsyncClient, net: tuple[str, Agent]
) -> None:
    gid = await default_group_id(admin)
    other_port = await profile(admin, "snmp", "P2161", {"community": PROF_COMM, "port": 2161})
    done = await scan(
        admin, f"{net[0]}.11", gid, tcp=False, snmp_port=PORT, snmp_profile_ids=[other_port["id"]]
    )
    (res,) = await results(admin, done["id"])
    assert res["snmp_credential"] == "profile" and res["snmp_profile_id"] == other_port["id"]
    created = await do_import(admin, done["id"], res["id"])
    assert created["snmp_mode"] == "manual"  # the profile's own port would not work
    creds = await device_creds(rt, created["device_id"])
    assert creds["port"] == PORT and creds["community"] == PROF_COMM


# ------------------------------------------------------------------ SSH login check
async def test_ssh_is_opt_in(
    rt: Runtime, admin: httpx.AsyncClient, ssh_host: Sim, net: tuple[str, Agent]
) -> None:
    gid = await default_group_id(admin)
    done = await scan(admin, ssh_host.host, gid, snmp=False, tcp_ports=[ssh_host.port])
    (res,) = await results(admin, done["id"])
    assert res["open_ports"] == [ssh_host.port]
    assert res["ssh_attempted"] is False and res["ssh_ok"] is False
    assert res["ssh_credential"] is None and done["options"]["ssh_credentials_set"] is False
    assert ssh_host.logins == [] and ssh_host.commands == []
    created = await do_import(admin, done["id"], res["id"])
    assert created["ssh_mode"] is None and created["snmp_mode"] is None


async def test_ssh_order_stop_no_command_and_import(
    rt: Runtime, admin: httpx.AsyncClient, ssh_host: Sim, net: tuple[str, Agent]
) -> None:
    await features(admin, audit=True)
    gid = await default_group_id(admin)
    port = ssh_host.port
    wrong = await profile(
        admin, "ssh", "Wrong", {"username": "netmon", "password": "x", "port": port}
    )
    closed = await profile(
        admin, "ssh", "Closed", {"username": "netmon", "password": "x", "port": 9}
    )
    good = {"username": "netmon", "password": SSH_PW, "port": port}
    right = await profile(admin, "ssh", "Right", good)
    typed = {"username": "netmon", "password": SSH_PW, "port": port}
    done = await scan(
        admin, ssh_host.host, gid, snmp=False, tcp_ports=[port],
        ssh_profile_ids=[closed["id"], wrong["id"], right["id"]], ssh_credentials=typed,
    )  # fmt: skip
    opts = done["options"]
    assert opts["ssh_profile_ids"] == [closed["id"], wrong["id"], right["id"]]
    assert opts["ssh_credentials"] is None and opts["ssh_credentials_set"] is True
    assert opts["ssh_username"] == "netmon"
    (res,) = await results(admin, done["id"])
    assert res["ssh_attempted"] and res["ssh_ok"] and res["ssh_credential"] == "profile"
    assert res["ssh_profile_id"] == right["id"] and res["ssh_profile_name"] == "Right"
    # Closed: port not open, not tried; Wrong: one attempt; Right: success; typed: not tried
    assert ssh_host.logins == ["netmon", "netmon"]
    assert ssh_host.commands == []  # login and disconnect only
    async with rt.sessionmaker() as s:
        assert (await s.execute(text("SELECT count(*) FROM ssh_host_keys"))).scalar() == 0
        snap = (
            await s.execute(text("SELECT ssh_candidates, options FROM discovery_scans "
                                 "WHERE id = :i"), {"i": done["id"]})
        ).one()  # fmt: skip
    assert [c["_origin"] for c in snap.ssh_candidates] == ["profile"] * 3 + ["typed"]
    assert SSH_PW not in json.dumps(snap.ssh_candidates)  # encrypted ([3] form)
    assert SSH_PW not in json.dumps(snap.options)
    # import: the profile that logged in is assigned
    created = await do_import(admin, done["id"], res["id"])
    assert created["ssh_mode"] == "profile" and created["ssh_profile_id"] == right["id"]
    dev = (await admin.get(f"/api/devices/{created['device_id']}")).json()
    assert dev["ssh_mode"] == "profile" and dev["ssh_profile_name"] == "Right"
    # declined -> inherit
    await admin.delete(f"/api/devices/{dev['id']}")
    created = await do_import(admin, done["id"], res["id"], save_ssh=False)
    assert created["ssh_mode"] == "inherit"
    dev = (await admin.get(f"/api/devices/{created['device_id']}")).json()
    assert dev["ssh_mode"] == "inherit" and dev["ssh_credentials"] is None
    # audit: the scan's SSH use and the resulting assignment
    async with rt.sessionmaker() as s:
        rows = (
            await s.execute(text("SELECT action, reason, changes FROM audit_log ORDER BY id"))
        ).all()
    scan_rows = [r for r in rows if r.action == "discovery_scan.ssh"]
    assert scan_rows and scan_rows[0].changes["ssh_profile_ids"] == [
        None, [closed["id"], wrong["id"], right["id"]],
    ]  # fmt: skip
    assert scan_rows[0].changes["ssh_typed"] == [None, True]
    imp = [r for r in rows if r.reason == "discovery_import"]
    assert imp and imp[0].changes["ssh_profile"] == [None, "Right"]
    assert MARK not in json.dumps([list(r) for r in rows], default=str)


async def test_ssh_typed_saved_as_manual_and_validation(
    rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient, ssh_host: Sim
) -> None:
    gid = await default_group_id(admin)
    typed = {"username": "netmon", "password": SSH_PW, "port": ssh_host.port}
    done = await scan(
        admin, ssh_host.host, gid, snmp=False, tcp_ports=[ssh_host.port], ssh_credentials=typed
    )
    (res,) = await results(admin, done["id"])
    assert res["ssh_ok"] and res["ssh_credential"] == "typed" and res["ssh_profile_id"] is None
    assert ssh_host.commands == []
    created = await do_import(admin, done["id"], res["id"])
    assert created["ssh_mode"] == "manual" and created["ssh_profile_id"] is None
    dev = (await admin.get(f"/api/devices/{created['device_id']}")).json()
    assert dev["ssh_mode"] == "manual" and dev["ssh_credentials"]["username"] == "netmon"
    assert dev["ssh_credentials"]["password_set"] is True
    from netmon.configbackup import service

    async with rt.sessionmaker() as s:
        (ctx,) = await service.load_contexts(s, rt.secret_key, [dev["id"]])
    from netmon.configbackup import credentials

    assert credentials.decrypt(ctx.ssh or {}, rt.secret_key).password == SSH_PW
    # a viewer cannot see discovery at all; the device shows no secret
    assert (await viewer.get(f"/api/discovery/scans/{done['id']}/results")).status_code == 403
    assert SSH_PW not in (await viewer.get(f"/api/devices/{dev['id']}")).text
    # validation: kind, count, duplicates, existence
    snmp = await profile(admin, "snmp", "NotSSH", {"community": "c"})
    sshp = [
        (await profile(admin, "ssh", f"S{i}", {"username": "u", "password": "p"}))["id"]
        for i in range(4)
    ]
    for opts, code in (
        ({"ssh_profile_ids": [snmp["id"]]}, "profile_kind_mismatch"),
        ({"ssh_profile_ids": sshp}, "validation_error"),
        ({"ssh_profile_ids": [sshp[0], sshp[0]]}, "validation_error"),
        ({"ssh_profile_ids": [99999]}, "validation_error"),
        ({"ssh_credentials": {"username": "u"}}, "validation_error"),
    ):
        r = await admin.post(
            "/api/discovery/scans",
            json={"targets": [ssh_host.host], "group_id": gid, "options": opts},
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == code, (opts, r.text)
    # saved discovery profile: typed SSH credentials are write-only, kept on PATCH, removable
    r = await admin.post(
        "/api/discovery/profiles",
        json={"name": "SSHDP", "targets": [ssh_host.host], "group_id": gid,
              "options": {"ssh_credentials": typed, "ssh_profile_ids": [sshp[0]]}},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    assert SSH_PW not in r.text and r.json()["options"]["ssh_credentials_set"] is True
    dp = r.json()["id"]
    r = await admin.patch(f"/api/discovery/profiles/{dp}", json={"options": {"tcp": True}})
    assert r.json()["options"]["ssh_credentials_set"] is True
    r = await admin.delete(f"/api/connection-profiles/{sshp[0]}")
    assert r.status_code == 409 and r.json()["error"]["details"][0]["discovery_profiles"] == 1
    r = await admin.patch(
        f"/api/discovery/profiles/{dp}", json={"options": {"ssh_credentials": None}}
    )
    assert r.json()["options"]["ssh_credentials_set"] is False
