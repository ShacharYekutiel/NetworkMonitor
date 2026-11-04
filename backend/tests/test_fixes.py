"""Regression tests for the phase-1 review findings."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy import text

from netmon import secrets as secrets_mod
from netmon.db import timescale
from netmon.poller.checks import ping as ping_check
from netmon.poller.checks.ping import parse_ping_output
from netmon.poller.processor import persist
from netmon.poller.sensor_types import validate_config
from netmon.poller.types import CheckContext, CheckResult, breach_message
from netmon.runtime import Runtime
from netmon.services import retention
from tests.conftest import _plain, make_device, make_sensor, make_settings, requires_db

UP = CheckResult(True, "", {"latency_ms": 1.0, "packet_loss_pct": 0.0})
DOWN = CheckResult(False, "Host unreachable")


# ------------------------------------------------------------------ pure unit tests
def test_ping_parse_locales() -> None:
    english = (
        "Pinging 10.0.0.1 with 32 bytes of data:\r\n"
        "Reply from 10.0.0.1: bytes=32 time=3ms TTL=64\r\n"
        "Reply from 10.0.0.1: bytes=32 time<1ms TTL=64\r\n"
        "Request timed out.\r\n"
        "    Minimum = 0ms, Maximum = 3ms, Average = 1ms\r\n"
    )
    hebrew = (
        "תשובה מאת 10.0.0.1: בתים=32 זמן=12ms TTL=118\r\n"
        "תשובה מאת 10.0.0.1: בתים=32 זמן<1ms TTL=118\r\n"
        "    מינימום = 1ms, מקסימום = 12ms, ממוצע = 6ms\r\n"
    )
    german = (
        "Antwort von 10.0.0.1: Bytes=32 Zeit=7ms TTL=57\r\n"
        "Antwort von 10.0.0.1: Bytes=32 Zeit<1ms TTL=57\r\n"
        "Zeitüberschreitung der Anforderung.\r\n"
        "    Minimum = 0ms, Maximum = 7ms, Mittelwert = 3ms\r\n"
    )
    linux = (
        "64 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=0.045 ms\n"
        "rtt min/avg/max/mdev = 0.045/0.045/0.045/0.000 ms\n"
    )
    assert parse_ping_output(english) == [3.0, 1.0]
    assert parse_ping_output(hebrew) == [12.0, 1.0]
    assert parse_ping_output(german) == [7.0, 1.0]
    assert parse_ping_output(linux) == [0.045]
    assert parse_ping_output("Destination host unreachable.\r\n") == []


def test_threshold_message_significant_digits() -> None:
    assert breach_message("x", 0.0042, {"down_max": 0.001}, "down") == "x 0.0042 > 0.001"
    assert breach_message("latency_ms", 612, {"down_max": 500}, "down") == "latency_ms 612 > 500"
    assert breach_message("y", 1234567.891, {"warning_max": 1e6}, "warning") == (
        "y 1.23457e+06 > 1000000"
    )


def test_windows_acl_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class Res:
        returncode = 0

    monkeypatch.setattr(secrets_mod.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Res())
    monkeypatch.setenv("USERNAME", "svc")
    monkeypatch.setenv("USERDOMAIN", "HOST")
    assert secrets_mod.restrict_windows_acl(tmp_path / "secret.key") is True
    assert calls[0] == [
        "icacls",
        str(tmp_path / "secret.key"),
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-18:F",
        "*S-1-5-32-544:F",
        "HOST\\svc:F",
    ]
    Res.returncode = 5
    assert secrets_mod.restrict_windows_acl(tmp_path / "secret.key") is False
    monkeypatch.setattr(sys, "platform", "win32")
    secrets_mod.load_or_create_secret(tmp_path / "d")
    assert calls[-1][1].endswith("secret.key")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell fake")
async def test_ping_subprocess_not_implemented(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = tmp_path / "ping"
    fake.write_text("#!/bin/sh\necho '64 bytes from 1.2.3.4: icmp_seq=1 ttl=64 time=2.5 ms'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    async def not_impl(*a: Any, **kw: Any) -> Any:
        raise NotImplementedError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", not_impl)
    rtts, sent = await ping_check._ping_binary("1.2.3.4", 1, 500, 56)  # noqa: SLF001
    assert rtts == [2.5] and sent == 1


async def test_webhook_url_validation_and_invalid_url_send() -> None:
    from netmon.alerts.webhook import WebhookError, send_webhook
    from netmon.schemas import validate_http_url

    for bad in ("http://", "http://host:99999/x", "http://[::1/x", "ftp://x"):
        with pytest.raises(ValueError):
            validate_http_url(bad)
    assert validate_http_url("https://hooks.example.com/x?a=1") == "https://hooks.example.com/x?a=1"
    with pytest.raises(WebhookError):
        await send_webhook({"url": "http://[::1/x"}, {"event": "test"})


# ------------------------------------------------------------------ API / DB tests
@requires_db
async def test_auto_and_default_sensor_names(admin: httpx.AsyncClient) -> None:
    dev = await make_device(admin, auto_sensors=["ping", "snmp_system", "snmp_cpu", "snmp_memory"])
    r = await admin.get("/api/sensors", params={"device_id": dev["id"]})
    assert sorted(s["name"] for s in r.json()["items"]) == ["CPU Load", "Memory", "Ping", "System"]
    s = await make_sensor(admin, dev["id"], "tcp_port", {"port": 22})
    assert s["name"] == "TCP 22"


@requires_db
async def test_community_write_only_and_headers_masked(
    admin: httpx.AsyncClient, viewer: httpx.AsyncClient
) -> None:
    dev = await make_device(admin, snmp_credentials={"community": "s3cret"})
    sc = dev["snmp_credentials"]
    assert sc["community"] is None and sc["community_set"] is True
    keys = list(sc)
    assert keys.index("community_set") == keys.index("community") + 1
    # omitted -> kept; "" -> cleared
    r = await admin.patch(f"/api/devices/{dev['id']}", json={"snmp_credentials": {"port": 1161}})
    assert r.json()["snmp_credentials"]["community_set"] is True
    r = await admin.get(f"/api/devices/{dev['id']}")
    assert "s3cret" not in r.text
    r = await viewer.get(f"/api/devices/{dev['id']}")
    assert "s3cret" not in r.text and r.json()["snmp_credentials"]["community_set"] is True
    # a fresh credential object without community gets the default "public"
    d2 = await make_device(admin, name="d2", snmp_credentials={"version": "v2c"})
    assert d2["snmp_credentials"]["community_set"] is True
    r = await admin.patch(f"/api/devices/{d2['id']}", json={"snmp_credentials": {"community": ""}})
    assert r.json()["snmp_credentials"]["community_set"] is False

    s = await make_sensor(
        admin,
        dev["id"],
        "http",
        {"url": "http://{host}/", "headers": {"Authorization": "Bearer x"}},
    )
    assert s["config"]["headers"] == {"Authorization": "Bearer x"}
    r = await viewer.get(f"/api/sensors/{s['id']}")
    assert r.json()["config"]["headers"] == {"Authorization": "********"}
    r = await viewer.get("/api/sensors", params={"type": "http"})
    assert r.json()["items"][0]["config"]["headers"] == {"Authorization": "********"}
    r = await admin.get("/api/sensors", params={"type": "http"})
    assert r.json()["items"][0]["config"]["headers"] == {"Authorization": "Bearer x"}


@requires_db
async def test_login_throttle_ignores_spoofed_forwarded_for(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    rt.settings.netmon_trust_proxy = True
    try:
        for i in range(10):
            r = await admin.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong-pass"},
                headers={"X-Forwarded-For": f"10.9.9.{i}, 203.0.113.7"},
            )
            assert r.status_code == 401
        r = await admin.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong-pass"},
            headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.7"},
        )
        assert r.status_code == 429
        r = await admin.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong-pass"},
            headers={"X-Forwarded-For": "203.0.113.8"},
        )
        assert r.status_code == 401  # a different real client
    finally:
        rt.settings.netmon_trust_proxy = False


@requires_db
async def test_run_now_paused_conflict(admin: httpx.AsyncClient) -> None:
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "ping", enabled=False)
    r = await admin.post(f"/api/sensors/{s['id']}/run")
    assert r.status_code == 409 and r.json()["error"]["code"] == "sensor_paused"
    await admin.patch(f"/api/sensors/{s['id']}", json={"enabled": True})
    await admin.patch(f"/api/devices/{dev['id']}", json={"enabled": False})
    r = await admin.post(f"/api/sensors/{s['id']}/run")
    assert r.status_code == 409


@requires_db
async def test_interface_state_kept_unless_index_or_host_changes(
    rt, admin: httpx.AsyncClient
) -> None:  # type: ignore[no-untyped-def]
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "snmp_interface", {"if_index": 2, "if_name": "a"})

    async def set_state() -> None:
        async with rt.sessionmaker() as session:
            await session.execute(
                text("UPDATE sensors SET state = CAST(:s AS jsonb) WHERE id=:i"),
                {"s": json.dumps({"t": 1.0, "hc": True}), "i": s["id"]},
            )
            await session.commit()

    async def state() -> dict:
        async with rt.sessionmaker() as session:
            return (
                await session.execute(text("SELECT state FROM sensors WHERE id=:i"), {"i": s["id"]})
            ).scalar()

    await set_state()
    await admin.patch(f"/api/sensors/{s['id']}", json={"config": {"if_name": "renamed"}})
    assert await state() == {"t": 1.0, "hc": True}
    await admin.patch(f"/api/sensors/{s['id']}", json={"config": {"if_index": 3}})
    assert await state() == {}
    await set_state()
    await admin.patch(f"/api/devices/{dev['id']}", json={"description": "x"})
    assert await state() != {}
    await admin.patch(f"/api/devices/{dev['id']}", json={"host": "10.1.1.1"})
    assert await state() == {}


@requires_db
async def test_notification_test_invalid_url_is_502(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    r = await admin.post(
        "/api/notification-channels",
        json={"name": "bad", "type": "webhook", "config": {"url": "http://host:99999/"}},
    )
    assert r.status_code == 422
    r = await admin.post(
        "/api/notification-channels",
        json={"name": "ok", "type": "webhook", "config": {"url": "http://example.invalid/"}},
    )
    cid = r.json()["id"]
    async with rt.sessionmaker() as session:  # simulate a legacy row with a broken URL
        await session.execute(
            text(
                "UPDATE notification_channels SET config = jsonb_set(config, '{url}', "
                "'\"http://[::1/x\"') WHERE id = :i"
            ),
            {"i": cid},
        )
        await session.commit()
    r = await admin.post(f"/api/notification-channels/{cid}/test")
    assert r.status_code == 502 and r.json()["error"]["code"] == "webhook_error"


# ------------------------------------------------------------------ alerting
async def _fake(rt: Any) -> list[dict]:
    sent: list[dict] = []

    async def sender(channel, payload, smtp, lang):  # type: ignore[no-untyped-def]
        sent.append(payload)

    rt.alerts.sender = sender
    return sent


async def _run(rt: Any, sid: int, raw: CheckResult) -> None:
    await persist(rt, sid, raw, 1.0, datetime.now(UTC))
    await rt.drain()


@requires_db
async def test_delayed_rule_no_orphan_up_and_rule_recheck(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    sent = await _fake(rt)
    ch = (
        await admin.post(
            "/api/notification-channels",
            json={"name": "h", "type": "webhook", "config": {"url": "http://x.invalid/"}},
        )
    ).json()
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "ping")
    rule = (
        await admin.post(
            "/api/alert-rules",
            json={
                "name": "delayed",
                "on_statuses": ["down", "up"],
                "delay_s": 1,
                "channel_ids": [ch["id"]],
            },
        )
    ).json()
    await _run(rt, s["id"], UP)
    await _run(rt, s["id"], DOWN)
    await _run(rt, s["id"], UP)  # recovered within the delay
    await asyncio.sleep(1.4)
    assert sent == []  # neither DOWN nor an orphan UP
    # a rule disabled while its timer runs does not send
    await _run(rt, s["id"], DOWN)
    await admin.patch(f"/api/alert-rules/{rule['id']}", json={"enabled": False})
    await asyncio.sleep(1.4)
    assert sent == []
    # and a deleted rule neither
    await admin.patch(f"/api/alert-rules/{rule['id']}", json={"enabled": True})
    await _run(rt, s["id"], UP)
    await _run(rt, s["id"], DOWN)
    await admin.delete(f"/api/alert-rules/{rule['id']}")
    await asyncio.sleep(1.4)
    assert sent == []


@requires_db
async def test_up_after_restart_and_pause(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    sent = await _fake(rt)
    ch = (
        await admin.post(
            "/api/notification-channels",
            json={"name": "h", "type": "webhook", "config": {"url": "http://x.invalid/"}},
        )
    ).json()
    dev = await make_device(admin)
    s = await make_sensor(admin, dev["id"], "ping")
    ok = await make_sensor(admin, dev["id"], "ping", name="ok")
    await admin.post(
        "/api/alert-rules",
        json={"name": "r", "on_statuses": ["down", "up"], "channel_ids": [ch["id"]]},
    )
    await _run(rt, s["id"], UP)
    await _run(rt, ok["id"], UP)
    await _run(rt, s["id"], DOWN)
    assert [p["new_status"] for p in sent] == ["down"]
    # "restart" after a long downtime: problem sensors keep their status, up ones go unknown
    async with rt.sessionmaker() as session:
        await session.execute(text("UPDATE sensors SET last_check_at = now() - interval '1 day'"))
        await session.commit()
    await rt._mark_stale_sensors()  # noqa: SLF001
    rt.alerts._last_change.clear()  # noqa: SLF001 - in-memory state is lost on restart
    assert (await admin.get(f"/api/sensors/{s['id']}")).json()["status"] == "down"
    assert (await admin.get(f"/api/sensors/{ok['id']}")).json()["status"] == "unknown"
    await _run(rt, s["id"], DOWN)  # still down: no repeated DOWN
    assert len(sent) == 1
    await _run(rt, s["id"], UP)  # recovery is reported
    assert [p["new_status"] for p in sent] == ["down", "up"]
    await _run(rt, ok["id"], UP)  # unknown -> up without a prior problem: nothing
    assert len(sent) == 2
    # down notified, then paused/resumed, then up -> UP is still sent
    await _run(rt, s["id"], DOWN)
    await admin.patch(f"/api/sensors/{s['id']}", json={"enabled": False})
    await admin.patch(f"/api/sensors/{s['id']}", json={"enabled": True})
    await _run(rt, s["id"], UP)
    assert [p["new_status"] for p in sent] == ["down", "up", "down", "up"]


# ------------------------------------------------------------------ WebSocket revocation
@requires_db
async def test_ws_closed_on_revocation(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    closed: list[int] = []

    class FakeWS:
        async def send_text(self, _: str) -> None:
            pass

        async def close(self, code: int = 1000) -> None:
            closed.append(code)

    me = (await admin.get("/api/auth/me")).json()["user"]
    v = (
        await admin.post(
            "/api/users", json={"username": "vic", "password": "vic-pass1", "role": "viewer"}
        )
    ).json()
    c1 = rt.hub.register(FakeWS(), user_id=v["id"])
    await admin.patch(f"/api/users/{v['id']}", json={"language": "he"})  # no bump
    assert closed == []
    await admin.patch(f"/api/users/{v['id']}", json={"is_active": False})
    assert closed == [4401]
    await rt.hub.unregister(c1)
    rt.hub.register(FakeWS(), user_id=me["id"])
    r = await admin.post(
        "/api/auth/change-password",
        json={"current_password": "S3cret-pass", "new_password": "N3w-pass-1"},
    )
    assert r.status_code == 204
    assert closed == [4401, 4000]  # own socket: reconnect with the new cookie
    rt.hub.register(FakeWS(), user_id=v["id"])
    await admin.delete(f"/api/users/{v['id']}")
    assert closed == [4401, 4000, 4401]


# ------------------------------------------------------------------ retention without policy
@requires_db
async def test_timescale_retention_without_policy(rt, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    if not rt.timescale_enabled:
        pytest.skip("TimescaleDB not enabled")

    async def no_policy(conn: Any, days: int) -> None:
        raise RuntimeError("function add_retention_policy does not exist (Apache license)")

    monkeypatch.setattr(timescale, "apply_retention_policy", no_policy)
    status = await timescale.bootstrap_status(rt.engine, 90)
    assert status.enabled is True and status.retention_policy is False
    assert await timescale.reapply_retention(rt.engine, 90) is False

    async with rt.sessionmaker() as session:
        await session.execute(text("TRUNCATE measurements"))
        await session.execute(
            text(
                "INSERT INTO measurements (time, sensor_id, channel, value) VALUES "
                "(now() - interval '200 days', 1, 'x', 1), "
                "(now() - interval '120 days', 1, 'x', 2),"
                "(now(), 1, 'x', 3)"
            )
        )
        await session.commit()
    prev = rt.retention_policy_ok
    rt.retention_policy_ok = False
    try:
        await retention.run_once(rt)
    finally:
        rt.retention_policy_ok = prev
    async with rt.sessionmaker() as session:
        values = (await session.execute(text("SELECT value FROM measurements"))).scalars().all()
    assert values == [3.0]


@requires_db
async def test_retention_drop_chunks_failure_falls_back(
    rt, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    if not rt.timescale_enabled:
        pytest.skip("TimescaleDB not enabled")

    async def broken(engine: Any, days: int) -> None:
        raise RuntimeError("drop_chunks failed")

    monkeypatch.setattr(timescale, "drop_old_chunks", broken)
    async with rt.sessionmaker() as session:
        await session.execute(text("TRUNCATE measurements"))
        await session.execute(
            text(
                "INSERT INTO measurements (time, sensor_id, channel, value) VALUES "
                "(now() - interval '200 days', 1, 'x', 1), (now(), 1, 'x', 3)"
            )
        )
        await session.commit()
    prev, rt.retention_policy_ok = rt.retention_policy_ok, False
    try:
        res = await retention.run_once(rt)
    finally:
        rt.retention_policy_ok = prev
    assert res["measurements"] == 1


# ------------------------------------------------------------------ advisory lock watchdog
@requires_db
async def test_lock_watchdog(databases, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from netmon.cli import lock_available
    from netmon.runtime import ADVISORY_LOCK_ID

    settings = make_settings(databases["fallback"], tmp_path, NETMON_DISABLE_TIMESCALE="1")
    rt = Runtime(settings)
    await rt.start(run_poller=False, maintenance=False)
    lost: list[bool] = []
    rt.on_lock_lost = lambda: lost.append(True)
    url = _plain(settings.database_url)
    try:
        assert await rt.check_lock() is True
        assert await lock_available(settings.async_database_url) is False

        async def kill_lock_backend() -> None:
            conn = await asyncpg.connect(url)
            try:
                await conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype='advisory' "
                    "AND objid = $1",
                    ADVISORY_LOCK_ID,
                )
            finally:
                await conn.close()

        await kill_lock_backend()  # simulates a PostgreSQL restart
        assert await rt.check_lock() is True  # re-acquired
        assert lost == []
        await kill_lock_backend()
        other = await asyncpg.connect(url)  # another instance grabs the lock meanwhile
        try:
            assert await other.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_ID)
            assert await rt.check_lock() is False
            assert lost == [True]
        finally:
            await other.close()
    finally:
        await rt.stop()
    assert await lock_available(settings.async_database_url) is True


def test_validate_config_still_complete() -> None:
    ctx = CheckContext(1, "ping", validate_config("ping", {}), "h", {})
    assert ctx.config["count"] == 4
    _ = timedelta
