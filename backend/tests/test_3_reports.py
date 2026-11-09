"""[3] Reports against the database: CRUD + validation, on-demand runs with the availability
maths over stored events (maintenance excluded), deliveries (email / Teams / Telegram /
webhook, mocked senders), resend, scheduler (due run, missed > 24 h skipped), HTML download,
feature gating."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest

from tests.conftest import _plain, make_device, make_sensor, requires_db

pytestmark = requires_db

START = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
END = START + timedelta(hours=168)


def h(n: float) -> datetime:
    return START + timedelta(hours=n)


async def _db(rt: Any) -> asyncpg.Connection:
    return await asyncpg.connect(_plain(rt.settings.database_url))


async def _event(
    conn: asyncpg.Connection,
    t: datetime,
    kind: str,
    sensor_id: int | None,
    device_id: int | None,
    old: str | None = None,
    new: str | None = None,
    message: str = "",
    in_maint: bool = False,
    group_id: int | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO events (time, kind, sensor_id, device_id, group_id, sensor_name, "
        "device_name, old_status, new_status, message, in_maintenance) "
        "VALUES ($1, $2, $3, $4, $5, 's', 'd', $6, $7, $8, $9)",
        t,
        kind,
        sensor_id,
        device_id,
        group_id,
        old,
        new,
        message,
        in_maint,
    )


@pytest.fixture
async def rep(rt: Any, admin: httpx.AsyncClient, feats: Any) -> Any:
    feats(reports=True, audit=True)
    return rt.reports


async def _devices(admin: httpx.AsyncClient, rt: Any) -> dict[str, Any]:
    """dev1: vector 1 + maintenance (vector 2); dev2: vector 4; dev3: not measured."""
    d1 = await make_device(admin, name="core-sw")
    d2 = await make_device(admin, name="edge-sw")
    d3 = await make_device(admin, name="printer")
    s1 = await make_sensor(admin, d1["id"], "ping", {}, enabled=True)
    s2 = await make_sensor(admin, d2["id"], "ping", {})
    conn = await _db(rt)
    try:
        await conn.execute("DELETE FROM events WHERE sensor_id = ANY($1)", [s1["id"], s2["id"]])
        await _event(
            conn, START - timedelta(days=3), "status_change", s1["id"], d1["id"], "unknown", "up"
        )
        await _event(conn, h(10), "status_change", s1["id"], d1["id"], "up", "down")
        await _event(conn, h(12), "status_change", s1["id"], d1["id"], "down", "up", in_maint=True)
        await _event(conn, h(11), "maintenance", None, d1["id"], message='Maintenance "fw" started')
        await _event(conn, h(13), "maintenance", None, d1["id"], message='Maintenance "fw" ended')
        await _event(conn, h(100), "status_change", s2["id"], d2["id"], "unknown", "up")
        await _event(conn, h(20), "notification_sent", s1["id"], d1["id"], "up", "down")
        await _event(conn, h(21), "notification_sent", s1["id"], d1["id"], "up", "down")
    finally:
        await conn.close()
    return {"d1": d1, "d2": d2, "d3": d3, "s1": s1, "s2": s2}


BODY = {
    "name": "Weekly ops",
    "language": "en",
    "sections": ["availability", "top_alerts", "config_changes", "top_talkers"],
    "schedule": "weekly",
    "weekday": 1,
    "send_time": "07:47",
    "email_to": ["ops@mnidom.net"],
}


async def test_gating_and_validation(admin: httpx.AsyncClient, feats: Any) -> None:
    r = await admin.get("/api/reports")
    assert r.status_code == 409 and r.json()["error"]["code"] == "feature_disabled"
    feats(reports=True)
    for bad in (
        {**BODY, "sections": []},
        {**BODY, "sections": ["availability", "availability"]},
        {**BODY, "weekday": None},
        {**BODY, "schedule": "monthly", "month_day": 29},
        {**BODY, "send_time": "7:47"},
        {**BODY, "email_to": ["not-an-address"]},
        {**BODY, "email_to": [f"u{i}@x.org" for i in range(51)]},
        {**BODY, "channel_ids": [999]},
        {**BODY, "scope_group_ids": [999]},
    ):
        r = await admin.post("/api/reports", json=bad)
        assert r.status_code == 422, (bad, r.text)
    r = await admin.post("/api/reports", json=BODY)
    assert r.status_code == 201, r.text
    rep = r.json()
    assert rep["next_run_at"] and rep["last_run"] is None
    assert (
        await admin.post("/api/reports", json={**BODY, "name": "weekly OPS"})
    ).status_code == 409
    r = await admin.patch(f"/api/reports/{rep['id']}", json={"schedule": "none"})
    assert r.status_code == 200
    out = r.json()
    assert out["next_run_at"] is None and out["weekday"] is None and out["send_time"] is None
    r = await admin.patch(
        f"/api/reports/{rep['id']}",
        json={"schedule": "monthly", "month_day": 1, "send_time": "07:47"},
    )
    assert r.status_code == 200 and r.json()["next_run_at"]
    items = (await admin.get("/api/reports")).json()["items"]
    assert [i["name"] for i in items] == ["Weekly ops"]
    assert (await admin.delete(f"/api/reports/{rep['id']}")).status_code == 204
    assert (await admin.get(f"/api/reports/{rep['id']}")).status_code == 404


async def test_run_availability_and_html(
    admin: httpx.AsyncClient, viewer: httpx.AsyncClient, rt: Any, rep: Any
) -> None:
    d = await _devices(admin, rt)
    r = await admin.post("/api/reports", json={**BODY, "email_to": []})
    rid = r.json()["id"]
    r = await admin.post(
        f"/api/reports/{rid}/run",
        json={"period_start": START.isoformat(), "period_end": END.isoformat(), "send": False},
    )
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["status"] == "running" and run["trigger"] == "manual"
    await rep.idle()
    r = await viewer.get(f"/api/reports/runs/{run['id']}")
    assert r.status_code == 200
    full = r.json()
    assert full["status"] == "ok", full
    data = full["data"]
    av = data["availability"]
    rows = {x["device_name"]: x for x in av["devices"]}
    core = rows["core-sw"]
    assert core["availability"] == 99.40  # vector 2: maintenance excluded
    assert core["measured_s"] == 166 * 3600 and core["downtime_s"] == 3600
    assert core["outages"] == 1
    assert rows["edge-sw"]["availability"] == 100.0
    assert rows["edge-sw"]["measured_s"] == 68 * 3600
    assert av["not_measured"] == [{"device_id": d["d3"]["id"], "device_name": "printer"}]
    assert av["devices"][0]["device_name"] == "core-sw"  # worst first
    assert av["overall"] == round(100 * (165 + 68) / (166 + 68), 2)
    assert data["top_alerts"][0]["notifications"] == 2
    assert data["top_alerts"][0]["problem_s"] == 2 * 3600
    notes = {n["section"]: n["note"] for n in data["notes"]}
    assert notes["config_changes"] == "feature_off" and notes["top_talkers"] == "feature_off"
    r = await viewer.get(f"/api/reports/runs/{run['id']}/html")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "attachment" in r.headers["content-disposition"]
    assert "netmon-report-Weekly-ops-20261102.html" in r.headers["content-disposition"]
    assert "core-sw" in r.text and "99.40 %" in r.text
    assert "<script" not in r.text and "http://" not in r.text.replace("http://adaptivecards", "")
    runs = (await viewer.get("/api/reports/runs", params={"report_id": rid})).json()
    assert runs["total"] == 1
    last = (await viewer.get(f"/api/reports/{rid}")).json()["last_run"]
    assert last["id"] == run["id"] and last["status"] == "ok"
    # period longer than 92 days
    r = await admin.post(
        f"/api/reports/{rid}/run",
        json={
            "period_start": START.isoformat(),
            "period_end": (START + timedelta(days=93)).isoformat(),
        },
    )
    assert r.status_code == 422
    assert (await viewer.post(f"/api/reports/{rid}/run")).status_code == 403


async def test_hebrew_rtl_preview(admin: httpx.AsyncClient, rt: Any, rep: Any) -> None:
    await _devices(admin, rt)
    r = await admin.post(
        "/api/reports/preview",
        json={
            "report": {"name": "תצוגה", "language": "he", "sections": ["availability"]},
            "period_start": START.isoformat(),
            "period_end": END.isoformat(),
        },
    )
    assert r.status_code == 202, r.text
    assert r.json()["report_id"] is None
    await rep.idle()
    r = await admin.get(f"/api/reports/runs/{r.json()['id']}/html")
    assert 'dir="rtl"' in r.text and "זמינות" in r.text


async def test_deliveries_and_resend(
    admin: httpx.AsyncClient, rt: Any, rep: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _devices(admin, rt)
    sent: list[tuple[str, Any]] = []
    fail = {"email": True}

    async def fake_email(
        smtp: Any, to: list[str], subject: str, text: str, html: Any = None
    ) -> None:
        if fail["email"]:
            raise RuntimeError("SMTP down")
        sent.append(("email", (to, subject, len(html or ""))))

    async def fake_webhook(cfg: Any, payload: Any) -> None:
        sent.append(("webhook", payload))

    async def fake_telegram(
        cfg: Any, text: str, parse_mode: Any = None, reply_markup: Any = None
    ) -> None:
        sent.append(("telegram", text))

    async def fake_teams(cfg: Any, body: Any) -> None:
        sent.append(("teams", body))

    monkeypatch.setattr("netmon.alerts.email.send_email", fake_email)
    monkeypatch.setattr("netmon.alerts.webhook.send_webhook", fake_webhook)
    monkeypatch.setattr("netmon.alerts.telegram.send_telegram", fake_telegram)
    monkeypatch.setattr("netmon.alerts.teams.send_teams", fake_teams)
    chans = []
    for body in (
        {"name": "hook", "type": "webhook", "config": {"url": "http://127.0.0.1:9/x"}},
        {
            "name": "tg",
            "type": "telegram",
            "config": {"bot_token": "123456:TEST_TOKEN_valid_0123456789abcdef", "chat_id": "5"},
        },
    ):
        r = await admin.post("/api/notification-channels", json=body)
        assert r.status_code == 201, r.text
        chans.append(r.json()["id"])
    r = await admin.put("/api/settings", json={"public_base_url": "https://nm.example"})
    assert r.status_code == 200
    r = await admin.post("/api/reports", json={**BODY, "channel_ids": chans})
    rid = r.json()["id"]
    r = await admin.post(
        f"/api/reports/{rid}/run",
        json={"period_start": START.isoformat(), "period_end": END.isoformat(), "send": True},
    )
    run_id = r.json()["id"]
    await rep.idle()
    run = (await admin.get(f"/api/reports/runs/{run_id}")).json()
    assert run["status"] == "partial"
    by = {d["target"]: d for d in run["deliveries"]}
    assert by["email:ops@mnidom.net"]["ok"] is False
    assert "SMTP down" in by["email:ops@mnidom.net"]["error"]
    assert by[f"channel:{chans[0]}"]["ok"] and by[f"channel:{chans[1]}"]["ok"]
    hook = next(p for k, p in sent if k == "webhook")
    assert hook["event"] == "report" and hook["run"]["id"] == run_id
    assert hook["run"]["url"] == f"https://nm.example/reports/runs/{run_id}"
    assert hook["data"]["availability"]["devices"]
    tg_text = next(p for k, p in sent if k == "telegram")
    assert "Overall availability" in tg_text and len(tg_text) <= 4096
    assert f"https://nm.example/reports/runs/{run_id}" in tg_text
    # resend the failed one (default targets)
    fail["email"] = False
    r = await admin.post(f"/api/reports/runs/{run_id}/resend")
    assert r.status_code == 202, r.text
    await rep.idle()
    run = (await admin.get(f"/api/reports/runs/{run_id}")).json()
    assert run["status"] == "ok"
    to, subject, size = next(p for k, p in sent if k == "email")
    assert to == ["ops@mnidom.net"]
    assert subject == "[NetworkMonitor] Weekly report: Weekly ops (02.11–08.11.2026)" or (
        subject.startswith("[NetworkMonitor] Weekly report: Weekly ops (")
    )
    assert 0 < size <= 1_000_000
    r = await admin.post(f"/api/reports/runs/{run_id}/resend")
    assert r.status_code == 422  # nothing failed


async def test_scheduler_due_and_missed(admin: httpx.AsyncClient, rt: Any, rep: Any) -> None:
    await _devices(admin, rt)
    r = await admin.post("/api/reports", json={**BODY, "email_to": []})
    rid = r.json()["id"]
    r2 = await admin.post("/api/reports", json={**BODY, "name": "Old", "email_to": []})
    old_id = r2.json()["id"]
    now = rt.clock.now()
    conn = await _db(rt)
    try:
        await conn.execute(
            "UPDATE reports SET next_run_at = $1 WHERE id = $2", now - timedelta(minutes=5), rid
        )
        await conn.execute(
            "UPDATE reports SET next_run_at = $1 WHERE id = $2", now - timedelta(days=3), old_id
        )
    finally:
        await conn.close()
    await rep._handle_missed()  # noqa: SLF001
    n = await rep.run_due()
    assert n == 1
    await rep.idle()
    runs = (await admin.get("/api/reports/runs")).json()["items"]
    assert [(x["report_id"], x["trigger"]) for x in runs] == [(rid, "schedule")]
    rr = (await admin.get(f"/api/reports/{rid}")).json()
    assert datetime.fromisoformat(rr["next_run_at"].replace("Z", "+00:00")) > now
    old = (await admin.get(f"/api/reports/{old_id}")).json()
    assert datetime.fromisoformat(old["next_run_at"].replace("Z", "+00:00")) > now
    ev = (await admin.get("/api/events", params={"kind": "system"})).json()["items"]
    assert any("was not sent" in e["message"] and "Old" in e["message"] for e in ev)
    # the period of a weekly run is the previous full ISO week
    p_start = datetime.fromisoformat(runs[0]["period_start"].replace("Z", "+00:00"))
    p_end = datetime.fromisoformat(runs[0]["period_end"].replace("Z", "+00:00"))
    assert p_end - p_start in (
        timedelta(days=7),
        timedelta(days=7, hours=1),
        timedelta(days=6, hours=23),
    )


async def test_retention_purge(admin: httpx.AsyncClient, rt: Any, rep: Any) -> None:
    conn = await _db(rt)
    try:
        await conn.execute(
            "INSERT INTO report_runs (report_name, trigger, period_start, period_end, started_at, "
            "status, language, data) VALUES ('x', 'manual', now(), now(), now() - interval "
            "'500 days', 'ok', 'en', '{}'), ('y', 'manual', now(), now(), now(), 'ok', 'en', '{}')"
        )
    finally:
        await conn.close()
    assert await rep.purge() == 1
    items = (await admin.get("/api/reports/runs")).json()["items"]
    assert [i["report_name"] for i in items] == ["y"]
    assert json.loads(json.dumps(items[0]))["deliveries"] == []


async def test_flap_episodes_and_section(
    admin: httpx.AsyncClient, rt: Any, rep: Any, feats: Any
) -> None:
    from netmon.reports.episodes import EpisodeRecorder

    d = await make_device(admin, name="acc-sw")
    rec = EpisodeRecorder(rt)
    t0 = h(5).timestamp()
    await rec.change("start", (d["id"], 5), 5, t0, "Gi1/0/5")
    for i in range(3):
        await rec.change("update", (d["id"], 5), 6 + i, t0 + i)
    await rec.end([(d["id"], 5)])
    await rec.change("start", (d["id"], 5), 5, t0 + 3600, "Gi1/0/5")
    await rec.end(None)
    conn = await _db(rt)
    try:
        rows = await conn.fetch("SELECT * FROM port_flap_episodes ORDER BY id")
    finally:
        await conn.close()
    assert [(r["transitions"], r["ended_at"] is not None) for r in rows] == [(8, True), (5, True)]
    body = {**BODY, "name": "Flaps", "sections": ["flapping_ports"], "email_to": []}
    rid = (await admin.post("/api/reports", json=body)).json()["id"]

    async def run() -> dict[str, Any]:
        r = await admin.post(
            f"/api/reports/{rid}/run",
            json={"period_start": START.isoformat(), "period_end": END.isoformat()},
        )
        await rep.idle()
        return (await admin.get(f"/api/reports/runs/{r.json()['id']}")).json()["data"]

    data = await run()
    assert data["notes"] == [{"section": "flapping_ports", "note": "feature_off"}]
    feats(reports=True, flapping=True)
    data = await run()
    assert data["flapping_ports"] == [
        {
            "device_id": d["id"],
            "device_name": "acc-sw",
            "if_index": 5,
            "port_name": "Gi1/0/5",
            "episodes": 2,
            "transitions": 13,
        }
    ]


async def test_top_talkers_section(admin: httpx.AsyncClient, rt: Any, rep: Any, feats: Any) -> None:
    feats(reports=True, flows=True)
    now = rt.clock.now()
    t = (now - timedelta(minutes=90)).replace(second=0, microsecond=0)
    conn = await _db(rt)
    try:
        eid = await conn.fetchval(
            "INSERT INTO flow_exporters (address, state, first_seen, last_seen) "
            "VALUES ('10.9.9.9', 'accepted', now(), now()) RETURNING id"
        )
        await conn.execute(
            "INSERT INTO flow_host_1m (time, exporter_id, ip, direction, other, bytes, packets, "
            "flows) VALUES ($1, $2, '10.0.0.5', 'src', false, 9000, 10, 2), "
            "($1, $2, '10.0.0.6', 'dst', false, 1000, 5, 1)",
            t,
            eid,
        )
        await conn.execute(
            "INSERT INTO flow_app_1m (time, exporter_id, protocol, port, bytes, packets, flows) "
            "VALUES ($1, $2, 6, 443, 8000, 10, 2), ($1, $2, 17, 53, 2000, 5, 1)",
            t,
            eid,
        )
        if getattr(rt, "flows_rollup", None) == "cagg":
            # rows inserted below the continuous aggregates' watermark (an earlier flows test
            # may have refreshed past this hour) appear only after a refresh, as the
            # production refresh policy (every 15 min, last 6 h) does
            for fam in ("host", "app"):
                await conn.execute(
                    f"CALL refresh_continuous_aggregate('flow_{fam}_1h', $1::timestamptz, "
                    "$2::timestamptz)",
                    t - timedelta(hours=1),
                    t + timedelta(hours=2),
                )
    finally:
        await conn.close()
    body = {**BODY, "name": "Flows", "sections": ["top_talkers"], "email_to": []}
    rid = (await admin.post("/api/reports", json=body)).json()["id"]
    r = await admin.post(
        f"/api/reports/{rid}/run",
        json={
            "period_start": (now - timedelta(hours=3)).isoformat(),
            "period_end": now.isoformat(),
        },
    )
    await rep.idle()
    data = (await admin.get(f"/api/reports/runs/{r.json()['id']}")).json()["data"]
    assert data["notes"] == [], data
    hosts = data["top_talkers"]["hosts"]
    assert hosts[0]["ip"] == "10.0.0.5" and hosts[0]["bytes"] == 9000
    apps = data["top_talkers"]["applications"]
    assert apps[0]["port"] == 443 and apps[0]["bytes"] == 8000


async def test_maintenance_ended_before_period_and_orphans_do_not_cover(rt, admin) -> None:  # type: ignore[no-untyped-def]
    """QA spec 58: a window that started AND ended before the period was taken as still open
    (DISTINCT ON included the verb), and events of deleted devices / unknown "#<id>" windows
    were read as GLOBAL windows - every device then looked 'in maintenance' all period."""
    from netmon.reports.data import _maintenance_by_device

    dev = await make_device(admin, name="mw-dev", host="10.9.9.1")
    conn = await _db(rt)
    try:
        ins = (
            "INSERT INTO events (time, kind, device_id, group_id, device_name, message) "
            "VALUES ($1, 'maintenance', $2, NULL, $3, $4)"
        )
        # device window fully before the period: started, then ended
        await conn.execute(ins, h(-5), dev["id"], "mw-dev", 'Maintenance "night" started')
        await conn.execute(ins, h(-4), dev["id"], "mw-dev", 'Maintenance "night" ended')
        # a deleted device's window (device_id NULL, device_name kept) left open
        await conn.execute(ins, h(-3), None, "gone-dev", 'Maintenance "old" started')
        # an unknown window written as "#<id>" with global scope
        await conn.execute(ins, h(-2), None, None, 'Maintenance "#42" started')
        # a real global window inside the period: 2 h
        await conn.execute(ins, h(10), None, None, 'Maintenance "global" started')
        await conn.execute(ins, h(12), None, None, 'Maintenance "global" ended')
    finally:
        await conn.close()

    class D:
        id = dev["id"]
        group_id = dev["group_id"]

    async with rt.sessionmaker() as session:
        out = await _maintenance_by_device(session, [D], START, END)
    ivs = out.get(dev["id"], [])
    covered = sum((b - a).total_seconds() for a, b in ivs)
    assert covered == 2 * 3600, ivs
