"""[2a] Review fix round: import credentials and budget, SNMP stage deadline with partial
results, cancel, target rejections, LIKE escaping, per-device apply lock, scheduler, sockets."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from netmon.discovery import importer, probe
from netmon.discovery import runner as runner_mod
from netmon.discovery.options import defaults
from netmon.discovery.scheduler import DiscoveryScheduler
from netmon.discovery.targets import TargetError, rejected_reason, validate_targets
from netmon.poller.checks import ping as ping_mod
from netmon.runtime import raise_nofile_limit
from tests.conftest import default_group_id, make_device, requires_db
from tests.snmpsim_agents import cisco_records, start_agent
from tests.test_discovery import results, wait_scan


def _free_udp() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def corp_agent() -> Iterator[int]:
    """An agent on 127.0.0.1 answering only the community `corp-secret`."""
    port = _free_udp()
    a = start_agent("127.0.0.1", port, {"corp-secret": cisco_records()})
    if a is None:
        pytest.skip("snmpsim not available")
    yield port
    a.stop()


@pytest.fixture
async def tcp_listener() -> AsyncIterator[int]:
    async def handle(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()


# ------------------------------------------------------------------ L1 targets
@pytest.mark.parametrize("bad", ["fe80::1%eth0", "::ffff:10.0.0.1", "::ffff:224.0.0.1"])
def test_scope_and_mapped_rejected(bad: str) -> None:
    with pytest.raises(TargetError):
        validate_targets([bad])


def test_resolved_address_rejections() -> None:
    assert rejected_reason(ipaddress.ip_address("fe80::1%eth0"))
    assert rejected_reason(ipaddress.ip_address("::ffff:192.168.1.1"))
    assert rejected_reason(ipaddress.ip_address("fe80::1")) is None
    assert rejected_reason(ipaddress.ip_address("127.0.0.1")) is None


# ------------------------------------------------------------------ H1 credentials
def test_device_credentials_rule() -> None:
    class R:
        snmp_ok = True
        snmp_credential_index = 0

    group_a = {"version": "v2c", "port": 161, "community": "corp", "timeout_s": 2, "retries": 1}
    scan_creds = [{**group_a, "timeout_s": 1, "retries": 0}, {"version": "v2c", "community": "x"}]
    assert importer.device_credentials(R(), scan_creds, group_a) is None
    own = importer.device_credentials(R(), scan_creds, {**group_a, "community": "public"})
    assert own is not None and own["community"] == "corp"
    assert own["timeout_s"] == 2 and own["retries"] == 1
    assert importer.device_credentials(R(), scan_creds, {**group_a, "port": 1161}) is not None
    R.snmp_credential_index = 1
    assert importer.device_credentials(R(), scan_creds, scan_creds[1])["community"] == "x"
    R.snmp_ok = False
    assert importer.device_credentials(R(), scan_creds, group_a) is None


@requires_db
async def test_import_into_other_group_gets_own_credentials(
    rt,
    admin: httpx.AsyncClient,
    corp_agent: int,  # type: ignore[no-untyped-def]
) -> None:
    corp = {"version": "v2c", "port": corp_agent, "community": "corp-secret", "timeout_s": 1}
    a = (await admin.post("/api/groups", json={"name": "A", "snmp_credentials": corp})).json()
    b = (
        await admin.post(
            "/api/groups",
            json={"name": "B", "snmp_credentials": {**corp, "community": "public"}},
        )
    ).json()
    r = await admin.post(
        "/api/discovery/scans",
        json={
            "targets": ["127.0.0.1"],
            "group_id": a["id"],
            "options": {"ping": False, "tcp": False, "dns": False},
        },
    )
    assert r.status_code == 202, r.text
    scan = await wait_scan(admin, r.json()["id"])
    [res] = await results(admin, scan["id"])
    assert res["snmp_ok"] and res["snmp_credential"] == "group"
    item = {"items": [{"result_id": res["id"], "template_id": "generic_ping"}]}
    r = await admin.post(
        f"/api/discovery/scans/{scan['id']}/import", json={**item, "group_id": b["id"]}
    )
    dev_id = r.json()["created"][0]["device_id"]
    async with rt.sessionmaker() as session:
        creds = (
            await session.execute(
                text("SELECT snmp_credentials FROM devices WHERE id = :id"), {"id": dev_id}
            )
        ).scalar_one()
    assert creds["community_enc"].startswith("v1:")  # [1.4.2] encrypted copy
    assert creds["community"] == "corp-secret" and creds["port"] == corp_agent
    assert creds["timeout_s"] == 2 and creds["retries"] == 1
    assert "corp-secret" not in r.text
    # into the scan's own group (same credentials): inherit
    assert (await admin.delete(f"/api/devices/{dev_id}")).status_code == 204
    r = await admin.post(f"/api/discovery/scans/{scan['id']}/import", json=item)
    dev = (await admin.get(f"/api/devices/{r.json()['created'][0]['device_id']}")).json()
    assert dev["snmp_credentials"] is None and dev["effective_snmp_source"] == "group"


# ------------------------------------------------------------------ M1 / M2 import
@requires_db
async def test_import_budget_and_internal_errors(
    rt,  # type: ignore[no-untyped-def]
    admin: httpx.AsyncClient,
    corp_agent: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    gid = await default_group_id(admin)
    r = await admin.post(
        "/api/discovery/scans",
        json={
            "targets": ["127.0.0.1", "127.0.0.2"],
            "group_id": gid,
            "options": {
                "ping": True,
                "tcp": False,
                "dns": False,
                "snmp_port": corp_agent,
                "extra_communities": ["corp-secret"],
            },
        },
    )
    scan = await wait_scan(admin, r.json()["id"])
    rows = await results(admin, scan["id"])
    assert len(rows) == 2
    url = f"/api/discovery/scans/{scan['id']}/import"
    items = [{"result_id": x["id"], "template_id": "generic_ping"} for x in rows]

    # budget already used up: nothing is attempted
    monkeypatch.setattr(importer, "IMPORT_BUDGET_S", 0.0)
    r = await admin.post(url, json={"items": items})
    assert r.status_code == 200
    assert r.json()["created"] == []
    assert r.json()["errors"] == [
        {
            "result_id": x["id"],
            "code": "internal_error",
            "message": "Import time budget exceeded; retry the remaining hosts",
            "device_id": None,
        }
        for x in rows
    ]
    # one slow item eats the budget: the next one is reported, the first stays imported
    real_apply = importer.apply_template

    async def slow_apply(*a: Any, **kw: Any) -> dict[str, Any]:
        assert kw["budget_s"] <= 0.4
        await asyncio.sleep(0.5)
        return await real_apply(*a, **kw)

    monkeypatch.setattr(importer, "IMPORT_BUDGET_S", 0.4)
    monkeypatch.setattr(importer, "CONCURRENCY", 1)
    monkeypatch.setattr(importer, "apply_template", slow_apply)
    r = await admin.post(url, json={"items": items})
    out = r.json()
    assert [c["result_id"] for c in out["created"]] == [rows[0]["id"]]
    assert out["errors"][0]["result_id"] == rows[1]["id"]
    assert out["errors"][0]["message"].startswith("Import time budget exceeded")
    first_dev = out["created"][0]["device_id"]
    assert (await results(admin, scan["id"]))[0]["imported_device_id"] == first_dev

    # an unexpected DB error carrying a secret: fixed message, nothing leaks
    async def failing_apply(rt_: Any, device_id: int, *a: Any, **kw: Any) -> dict[str, Any]:
        async with rt_.sessionmaker() as session:
            await session.execute(text("SELECT CAST(:c AS integer)"), {"c": "corp-secret"})
        raise AssertionError("unreachable")

    monkeypatch.setattr(importer, "IMPORT_BUDGET_S", 90.0)
    monkeypatch.setattr(importer, "apply_template", failing_apply)
    caplog.set_level(logging.DEBUG)
    r = await admin.post(url, json={"items": items})
    body = r.json()
    assert body["errors"][0] == {
        "result_id": rows[0]["id"],
        "code": "device_exists",
        "message": "A device with this host already exists",
        "device_id": first_dev,
    }
    assert body["errors"][1] == {
        "result_id": rows[1]["id"],
        "code": "internal_error",
        "message": "Internal error",
        "device_id": None,
    }
    assert "corp-secret" not in r.text
    assert "corp-secret" not in caplog.text
    assert "DBAPIError" in caplog.text or "DataError" in caplog.text
    # the device of the failed item exists and points back: a retry says device_exists
    r = await admin.post(url, json={"items": items[1:]})
    assert r.json()["errors"][0]["code"] == "device_exists"


def test_engine_hides_parameters(rt) -> None:  # type: ignore[no-untyped-def]
    assert rt.engine.sync_engine.hide_parameters is True


# ------------------------------------------------------------------ H2 partial results
async def test_snmp_deadline_keeps_tcp(tcp_listener: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "SNMP_DEADLINE_S", 1.0)
    opts = {**defaults(), "ping": False, "dns": False, "tcp_ports": [tcp_listener]}
    port = _free_udp()
    creds = [
        {"version": "v2c", "port": port, "timeout_s": 10, "retries": 0, "community": c}
        for c in ("a", "b", "c")
    ]
    t0 = time.monotonic()
    res = await probe.probe_host("127.0.0.1", opts, creds)
    assert time.monotonic() - t0 < 4
    assert res.found and res.open_ports == [tcp_listener] and res.snmp_ok is False


@requires_db
async def test_backstop_keeps_partial_results(
    rt,  # type: ignore[no-untyped-def]
    admin: httpx.AsyncClient,
    tcp_listener: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gid = await default_group_id(admin)
    monkeypatch.setattr(rt.discovery, "host_timeout_s", 1.0)  # backstop before the SNMP deadline
    r = await admin.post(
        "/api/discovery/scans",
        json={
            "targets": ["127.0.0.1"],
            "group_id": gid,
            "options": {
                "ping": True,
                "dns": False,
                "tcp_ports": [tcp_listener],
                "snmp_port": _free_udp(),
                "snmp_timeout_s": 10,
                "extra_communities": ["x", "y"],
            },
        },
    )
    done = await wait_scan(admin, r.json()["id"])
    assert done["found_hosts"] == 1 and done["scanned_hosts"] == 1
    [res] = await results(admin, done["id"])
    assert res["open_ports"] == [tcp_listener] and res["ping_ok"] and res["snmp_ok"] is False


# ------------------------------------------------------------------ L2 cancel
@requires_db
async def test_cancel_answers_cancelled_while_task_finishes(
    rt,  # type: ignore[no-untyped-def]
    admin: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gid = await default_group_id(admin)
    real_finish = rt.discovery._finish  # noqa: SLF001
    gate = asyncio.Event()

    async def slow_finish(scan: Any, status: str, error: str | None) -> None:
        await gate.wait()
        await real_finish(scan, "failed", "boom")  # even a failure cannot undo the cancel

    monkeypatch.setattr(rt.discovery, "_finish", slow_finish)
    monkeypatch.setattr(runner_mod, "CANCEL_WAIT_S", 0.3)
    r = await admin.post(
        "/api/discovery/scans",
        json={
            "targets": ["127.0.9.1-20"],
            "group_id": gid,
            "options": {"ping": False, "tcp": False, "snmp_port": 1, "snmp_timeout_s": 5},
        },
    )
    sid = r.json()["id"]
    t0 = time.monotonic()
    r = await admin.post(f"/api/discovery/scans/{sid}/cancel")
    assert time.monotonic() - t0 < 5
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert r.json()["finished_at"] is not None
    assert rt.discovery.busy  # still winding down
    gate.set()
    await rt.discovery.wait(10)
    r = await admin.get(f"/api/discovery/scans/{sid}")
    assert r.json()["status"] == "cancelled" and r.json()["error"] is None


# ------------------------------------------------------------------ L4 LIKE escaping
@requires_db
async def test_results_q_is_literal(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    async with rt.sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO discovery_scans (trigger, status, targets, total_hosts) "
                "VALUES ('manual', 'completed', '{x}', 2)"
            )
        )
        for ip, name in (("10.0.0.1", "a_b%c"), ("10.0.0.2", "axbyc")):
            await session.execute(
                text(
                    "INSERT INTO discovery_results (scan_id, ip, sys_name, ping_ok, snmp_ok, "
                    "device_class, suggested_template, is_new) VALUES (1, CAST(:ip AS inet), :n, "
                    "true, false, 'unknown', 'generic_ping', true)"
                ),
                {"ip": ip, "n": name},
            )
        await session.commit()
    assert [x["ip"] for x in await results(admin, 1, q="a_b")] == ["10.0.0.1"]
    assert [x["ip"] for x in await results(admin, 1, q="b%c")] == ["10.0.0.1"]
    assert len(await results(admin, 1, q="%")) == 1  # a literal %
    assert len(await results(admin, 1, q="x")) == 1


# ------------------------------------------------------------------ L7 per-device lock
@requires_db
async def test_concurrent_apply_creates_no_duplicates(admin: httpx.AsyncClient) -> None:
    dev = await make_device(admin, host="127.0.0.1")
    url = f"/api/devices/{dev['id']}/apply-template"
    r1, r2 = await asyncio.gather(
        admin.post(url, json={"template_id": "generic_ping"}),
        admin.post(url, json={"template_id": "generic_ping"}),
    )
    created = len(r1.json()["created"]) + len(r2.json()["created"])
    assert created == 1
    r = await admin.get("/api/sensors", params={"device_id": dev["id"]})
    assert r.json()["total"] == 1


# ------------------------------------------------------------------ L8 scheduler
@requires_db
async def test_set_next_respects_disabled(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    gid = await default_group_id(admin)
    p = (
        await admin.post(
            "/api/discovery/profiles",
            json={"name": "p", "targets": ["10.0.0.1"], "group_id": gid},
        )
    ).json()
    from datetime import UTC, datetime

    await DiscoveryScheduler(rt)._set_next(p["id"], datetime.now(UTC))  # noqa: SLF001
    assert (await admin.get(f"/api/discovery/profiles/{p['id']}")).json()["next_run_at"] is None


# ------------------------------------------------------------------ sockets
def test_fd_exhaustion_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    import icmplib

    assert ping_mod.is_fd_exhaustion(OSError(24, "Too many open files"))
    assert ping_mod.is_fd_exhaustion(icmplib.ICMPSocketError("[Errno 24] Too many open files"))
    assert not ping_mod.is_fd_exhaustion(PermissionError(1, "Operation not permitted"))

    async def emfile(*a: Any, **kw: Any) -> Any:
        raise icmplib.ICMPSocketError("[Errno 24] Too many open files")

    monkeypatch.setattr(icmplib, "async_ping", emfile)
    monkeypatch.setattr(ping_mod, "_mode", "unprivileged")

    async def run() -> float | None:
        return await ping_mod.ping_once("127.0.0.1", 200)

    assert asyncio.run(run()) is None
    assert ping_mod._mode == "unprivileged"  # noqa: SLF001  (not switched to binary)


async def test_tcp_connect_cap_and_nofile() -> None:
    assert probe.TCP_CONNECT_LIMIT == 256
    assert probe._tcp_semaphore()._value == 256  # noqa: SLF001
    out = raise_nofile_limit()
    try:
        import resource
    except ImportError:
        assert out is None
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = 65536 if hard == resource.RLIM_INFINITY else min(hard, 65536)
    assert soft == resource.RLIM_INFINITY or soft >= want
