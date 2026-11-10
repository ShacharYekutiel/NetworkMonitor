"""[3b] AI features against the QA Bedrock mock (developer C, architecture § 14.8): explain,
correlation/incidents, alert notes, config-diff explanation, syslog digest, rule drafts,
template walks/drafts + the user-template save, Telegram /ask, report summaries; toggles,
admin-only, degrade silently, audit (metadata only)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from tests.conftest import make_device, make_sensor, requires_db
from tests.fixtures_3b_agent import configure, mock_settings, parse_sse

pytestmark = requires_db


# --------------------------------------------------------------------------- helpers
async def put_setting(rt: Any, key: str, value: Any) -> None:
    from netmon.services import settings as settings_svc

    async with rt.sessionmaker() as session:
        await settings_svc.put(session, key, value)
        await session.commit()


async def on(rt: Any, *names: str) -> None:
    await rt.features.apply(dict.fromkeys(names, True))


async def q(rt: Any, sql: str, **p: Any) -> list[Any]:
    async with rt.sessionmaker() as session:
        rows = (await session.execute(text(sql), p)).mappings().all()
        await session.commit()
    return [dict(r) for r in rows]


async def ex(rt: Any, sql: str, **p: Any) -> Any:
    async with rt.sessionmaker() as session:
        res = await session.execute(text(sql), p)
        await session.commit()
    return res


async def audit_actions(rt: Any, prefix: str) -> list[dict[str, Any]]:
    return await q(
        rt,
        "SELECT action, changes, actor_type, source FROM audit_log WHERE action LIKE :p ORDER BY "
        "id",
        p=prefix + "%",
    )


async def add_event(
    rt: Any, sensor: dict, device: dict, old: str, new: str, at: Any, msg: str = ""
) -> dict:
    row = (
        await q(
            rt,
            "INSERT INTO events (time, kind, sensor_id, device_id, sensor_name, device_name, "
            "old_status, new_status, message) VALUES (:t, 'status_change', :s, :d, :sn, :dn, :o, "
            ":n, :m) "
            "RETURNING id, time",
            t=at,
            s=sensor["id"],
            d=device["id"],
            sn=sensor["name"],
            dn=device["name"],
            o=old,
            n=new,
            m=msg,
        )
    )[0]
    await ex(
        rt,
        "UPDATE sensors SET status = :st, status_message = :m WHERE id = :s",
        st=new,
        m=msg,
        s=sensor["id"],
    )
    return {
        "id": row["id"],
        "time": row["time"].isoformat(),
        "kind": "status_change",
        "sensor_id": sensor["id"],
        "device_id": device["id"],
        "old_status": old,
        "new_status": new,
        "message": msg,
    }


async def sse(c: httpx.AsyncClient, path: str, body: dict) -> tuple[int, Any]:
    async with c.stream("POST", path, json=body, timeout=60) as r:
        raw = (await r.aread()).decode()
        if r.status_code != 200:
            return r.status_code, json.loads(raw)
        return 200, parse_sse(raw)


# --------------------------------------------------------------------------- gates
async def test_endpoints_admin_only_and_feature_gates(
    rt: Any, admin: httpx.AsyncClient, viewer: httpx.AsyncClient
):
    r = await viewer.get("/api/incidents")
    assert r.status_code == 403
    r = await admin.get("/api/incidents")
    assert r.status_code == 409 and r.json()["error"]["details"] == [{"feature": "assistant"}]
    r = await admin.post(
        "/api/assistant/rule-drafts", json={"kind": "alert_rule", "description": "ping down"}
    )
    assert r.status_code == 409
    # the user-template save is always available (admin only)
    r = await viewer.post("/api/device-templates/user", json={"yaml": "x"})
    assert r.status_code == 403


async def test_feature_off_and_idle(rt: Any, admin: httpx.AsyncClient, asst: Any):
    r = await admin.get("/api/incidents")
    assert r.status_code == 409 and r.json()["error"]["details"] == [{"feature": "ai_correlation"}]
    await on(rt, "ai_config_diff")
    r = await admin.get(
        "/api/assistant/config-diff/flags", params={"device_id": 1, "from": 1, "to": 2}
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "feature_idle"
    needs = r.json()["error"]["details"][0]["needs"]
    assert "config_backup" in needs and "send_config_excerpts" in needs
    await on(rt, "ai_syslog_digest")
    r = await admin.get("/api/assistant/digests")
    assert r.status_code == 409 and r.json()["error"]["details"][0]["needs"] == ["syslog"]


# --------------------------------------------------------------------------- explain (§ 14.8.1)
async def test_explain_alert_stream_cache_and_audit(rt: Any, admin: httpx.AsyncClient, asst: Any):
    await on(rt, "ai_explain_alert")
    dev = await make_device(admin, name="core-sw", host="10.0.0.9")
    s = await make_sensor(admin, dev["id"], "ping")
    now = rt.clock.now()
    await add_event(rt, s, dev, "up", "warning", now - timedelta(minutes=20), "slow")
    ev = await add_event(
        rt, s, dev, "warning", "down", now - timedelta(minutes=5), "Request timed out"
    )
    status, events = await sse(admin, "/api/assistant/explain", {"sensor_id": s["id"]})
    assert status == 200, events
    names = [e for _, e, _ in events]
    assert names[0] == "start" and names[-1] == "done"
    start = events[0][2]
    msg = events[-1][2]["message"]
    assert msg["status"] == "complete" and "What happened" in msg["content"]
    conv = (
        await q(
            rt,
            "SELECT kind, ref_sensor_id, ref_event_id FROM assistant_conversations WHERE id = :i",
            i=start["conversation_id"],
        )
    )[0]
    assert conv == {"kind": "explain", "ref_sensor_id": s["id"], "ref_event_id": ev["id"]}
    reqs = asst.requests("converse_stream")
    assert reqs and reqs[0]["purpose"] == "explain"
    first_user = json.dumps(reqs[0]["body"]["messages"][0])
    assert "Request timed out" in first_user and f"event/{ev['id']}" in first_user
    # cached within 30 min: start + done at once, no new model call
    n = len(asst.requests("converse_stream"))
    status, events = await sse(admin, "/api/assistant/explain", {"sensor_id": s["id"]})
    assert [e for _, e, _ in events] == ["start", "done"] and events[0][2]["cached"] is True
    assert events[0][2]["conversation_id"] == start["conversation_id"]
    assert len(asst.requests("converse_stream")) == n
    # refresh: a new conversation
    status, events = await sse(
        admin, "/api/assistant/explain", {"sensor_id": s["id"], "refresh": True}
    )
    assert events[0][2]["conversation_id"] != start["conversation_id"]
    acts = await audit_actions(rt, "assistant.explain_alert")
    assert len(acts) == 2 and "question" not in json.dumps(acts)
    r = await admin.post("/api/assistant/explain", json={"sensor_id": 999999})
    assert r.status_code == 404


# --------------------------------------------------------------------------- correlation (§ 14.8.2)
async def _star(admin: httpx.AsyncClient, rt: Any) -> dict[str, Any]:
    core = await make_device(admin, name="core", host="10.1.0.1")
    out = {"core": core, "core_s": await make_sensor(admin, core["id"], "ping")}
    for n in ("a", "b", "c", "x"):
        d = await make_device(admin, name=n, host=f"10.1.0.{ord(n)}")
        out[n] = d
        out[n + "_s"] = await make_sensor(admin, d["id"], "ping")
        if n != "x":
            await ex(
                rt,
                "UPDATE devices SET dependency_mode = 'device', dependency_device_id = :p WHERE "
                "id = :d",
                p=core["id"],
                d=d["id"],
            )
    rt.ai_features.correlation.invalidate()
    return out


async def test_correlation_incident_lifecycle_and_ai_summary(
    rt: Any, admin: httpx.AsyncClient, asst: Any
):
    await on(rt, "ai_correlation")
    inv = await _star(admin, rt)
    corr = rt.ai_features.correlation
    t0 = rt.clock.now() - timedelta(minutes=2)
    wsq: asyncio.Queue[dict] = asyncio.Queue()
    rt.hub.listeners.append(wsq)
    e = await add_event(rt, inv["core_s"], inv["core"], "up", "down", t0)
    assert await corr.process(e) is None  # 1 problem: no incident yet
    e = await add_event(rt, inv["a_s"], inv["a"], "up", "unreachable", t0 + timedelta(seconds=30))
    assert await corr.process(e) is None  # 2 problems: no incident yet
    e = await add_event(rt, inv["b_s"], inv["b"], "up", "unreachable", t0 + timedelta(seconds=30))
    inc_id = await corr.process(e)
    assert inc_id is not None
    x = await add_event(rt, inv["x_s"], inv["x"], "up", "down", t0 + timedelta(seconds=40))
    assert await corr.process(x) is None  # unrelated
    c = await add_event(rt, inv["c_s"], inv["c"], "up", "unreachable", t0 + timedelta(seconds=50))
    assert await corr.process(c) == inc_id  # grows
    await corr.idle()
    r = await admin.get(f"/api/incidents/{inc_id}")
    assert r.status_code == 200, r.text
    inc = r.json()
    assert inc["title"] == "core down — 3 devices unreachable" and inc["state"] == "open"
    assert inc["root_device"]["name"] == "core" and inc["member_count"] == 4
    assert inc["down"] == 1 and inc["unreachable"] == 3
    assert {m["is_root"] for m in inc["members"] if m["device_name"] == "core"} == {True}
    assert inc["ai"] is not None and "AI summary from the mock" in inc["ai"]["summary"]
    assert inc["ai"]["root_guess"] == {"device_id": inv["core"]["id"], "confidence": "medium"}
    reqs = asst.requests("converse")
    assert (
        reqs
        and reqs[0]["purpose"] == "incident"
        and reqs[0]["forced_tool"] == "report_incident_summary"
    )
    assert reqs[0]["model_id"]
    types = []
    while not wsq.empty():
        types.append(wsq.get_nowait()["type"])
    assert "incident.update" in types
    r = await admin.get("/api/incidents", params={"state": "open"})
    assert r.json()["total"] == 1 and r.json()["items"][0]["id"] == inc_id
    # recoveries → resolved
    for n in ("core", "a", "b", "c"):
        e = await add_event(rt, inv[n + "_s"], inv[n], "down", "up", rt.clock.now())
        await corr.process(e)
    r = await admin.get(f"/api/incidents/{inc_id}")
    assert r.json()["state"] == "resolved" and r.json()["recovered"] == 4
    acts = await audit_actions(rt, "assistant.incident_summary")
    assert acts and acts[0]["actor_type"] == "system"


async def test_correlation_degrades_without_model(rt: Any, admin: httpx.AsyncClient, asst: Any):
    await on(rt, "ai_correlation")
    asst.mode("unavailable")
    inv = await _star(admin, rt)
    corr = rt.ai_features.correlation
    t0 = rt.clock.now() - timedelta(minutes=1)
    evs = [await add_event(rt, inv["core_s"], inv["core"], "up", "down", t0)]
    for n in ("a", "b"):
        evs.append(await add_event(rt, inv[n + "_s"], inv[n], "up", "unreachable", t0))
    ids = [await corr.process(e) for e in evs]
    await corr.idle()
    inc = (await admin.get(f"/api/incidents/{ids[-1]}")).json()
    assert inc["title"].startswith("core down") and inc["ai"] is None
    # turning the feature OFF marks open incidents stale and stops the listener
    await rt.features.apply({"ai_correlation": False})
    assert (await q(rt, "SELECT state FROM assistant_incidents"))[0]["state"] == "stale"
    assert not corr.on


async def test_correlation_hook_from_published_events(rt: Any, admin: httpx.AsyncClient, asst: Any):
    """events.published → ai_features.on_events (the alert engine's event hook)."""
    from netmon.services import events as events_svc

    await on(rt, "ai_correlation")
    seen: list[dict] = []
    orig = rt.ai_features.correlation.on_events
    rt.ai_features.correlation.on_events = lambda evs: seen.extend(evs)  # type: ignore[method-assign]
    try:
        dev = await make_device(admin, name="d1")
        s = await make_sensor(admin, dev["id"], "ping")
        async with rt.sessionmaker() as session:
            ev = await events_svc.add_event(
                session,
                "status_change",
                "x",
                sensor_id=s["id"],
                device_id=dev["id"],
                old_status="up",
                new_status="down",
            )
            await session.commit()
        events_svc.published(rt, [ev])
    finally:
        rt.ai_features.correlation.on_events = orig  # type: ignore[method-assign]
    assert seen and seen[0]["new_status"] == "down"


# --------------------------------------------------------------------------- alert notes (§ 14.8.3)
class TgRecorder:
    def __init__(self, fail_edit: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_edit = fail_edit

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.calls.append((method, body))
        if method == "editMessageText" and self.fail_edit:
            return httpx.Response(400, json={"ok": False, "description": "message can't be edited"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})


async def _note_setup(
    rt: Any, admin: httpx.AsyncClient, ai_note: bool = True, sfx: str = ""
) -> dict[str, Any]:
    dev = await make_device(admin, name="core-sw" + sfx, host="10.0.0.9")
    s = await make_sensor(admin, dev["id"], "ping")
    tg = (
        await admin.post(
            "/api/notification-channels",
            json={
                "name": "tg" + sfx,
                "type": "telegram",
                "config": {"bot_token": "123456:" + "A" * 30, "chat_id": "-100123"},
            },
        )
    ).json()
    mail = (
        await admin.post(
            "/api/notification-channels",
            json={"name": "mail" + sfx, "type": "email", "config": {"to": ["ops@example.com"]}},
        )
    ).json()
    hook = (
        await admin.post(
            "/api/notification-channels",
            json={
                "name": "hook" + sfx,
                "type": "webhook",
                "config": {"url": "http://127.0.0.1:9/x"},
            },
        )
    ).json()
    r = await admin.post(
        "/api/alert-rules",
        json={
            "name": "r1" + sfx,
            "on_statuses": ["down"],
            "channel_ids": [tg["id"], mail["id"], hook["id"]],
            "ai_note": ai_note,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["ai_note"] is ai_note
    ev = await add_event(
        rt, s, dev, "up", "down", rt.clock.now() - timedelta(seconds=5), "Request timed out"
    )
    return {"dev": dev, "s": s, "tg": tg, "mail": mail, "hook": hook, "rule": r.json(), "ev": ev}


def _sends(st: dict[str, Any]) -> tuple[list, list, dict, dict]:
    send = SimpleNamespace(
        stage="problem",
        ok=[st["tg"]["id"], st["mail"]["id"], st["hook"]["id"]],
        keys={c: ("k", c) for c in (st["tg"]["id"], st["mail"]["id"], st["hook"]["id"])},
        payload={
            "new_status": "down",
            "sensor": {"name": "Ping"},
            "device": {"name": "core-sw"},
            "url": "https://nm.example/sensors/1",
        },
    )
    plan = SimpleNamespace(rule_id=st["rule"]["id"], sends=[send])
    row = SimpleNamespace(
        event_id=st["ev"]["id"], sensor_id=st["s"]["id"], device_id=st["dev"]["id"]
    )
    channels = {
        st["tg"]["id"]: {"type": "telegram", "config": {}},
        st["mail"]["id"]: {"type": "email", "config": {"to": ["ops@example.com"]}},
        st["hook"]["id"]: {"type": "webhook", "config": {}},
    }
    receipts = {
        ("k", st["tg"]["id"]): {
            "chat_id": "-100123",
            "message_id": 42,
            "text": "<b>DOWN</b> core-sw",
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": []},
        },
        ("k", st["mail"]["id"]): {
            "message_id": "<abc@nm>",
            "subject": "[NetworkMonitor] DOWN core-sw",
            "to": ["ops@example.com"],
        },
        ("k", st["hook"]["id"]): None,
    }
    return [plan], [row], channels, receipts


async def test_alert_note_telegram_edit_email_reply_webhook_skipped(
    rt: Any, admin: httpx.AsyncClient, asst: Any
):
    await on(rt, "ai_alert_notes")
    st = await _note_setup(rt, admin)
    notes = rt.ai_features.notes
    rec = TgRecorder()
    notes.transport = httpx.MockTransport(rec.handler)
    mails: list[tuple] = []

    async def fake_mail(smtp, to, subject, text_, html_, headers):  # noqa: ANN001
        mails.append((to, subject, text_, headers))

    notes.email_sender = fake_mail
    try:
        rt.ai_features.after_alert_sends(*_sends(st))
        await notes.idle()
    finally:
        notes.transport = None
        notes.email_sender = None
    assert [m for m, _ in rec.calls] == ["editMessageText"]
    body = rec.calls[0][1]
    assert body["message_id"] == 42 and body["text"].startswith(
        "<b>DOWN</b> core-sw\n\n<i>AI:</i> "
    )
    assert "uplink" in body["text"] and body["reply_markup"] == {"inline_keyboard": []}
    assert mails and mails[0][1] == "Re: [NetworkMonitor] DOWN core-sw"
    assert mails[0][3] == {"In-Reply-To": "<abc@nm>", "References": "<abc@nm>"}
    rows = await q(
        rt, "SELECT channel_type, note_status FROM assistant_note_targets ORDER BY channel_type"
    )
    assert rows == [
        {"channel_type": "email", "note_status": "replied"},
        {"channel_type": "telegram", "note_status": "edited"},
    ]
    art = (
        await q(
            rt, "SELECT key, status, content FROM assistant_artifacts WHERE kind = 'alert_note'"
        )
    )[0]
    assert (
        art["key"] == f"note:{st['ev']['id']}"
        and art["status"] == "ok"
        and len(art["content"]) <= 300
    )
    reqs = asst.requests("converse")
    assert len(reqs) == 1 and reqs[0]["purpose"] == "alert_note"  # one note per problem
    acts = await audit_actions(rt, "assistant.alert_note")
    assert acts[-1]["actor_type"] == "system"
    ch = (
        json.loads(acts[-1]["changes"])
        if isinstance(acts[-1]["changes"], str)
        else acts[-1]["changes"]
    )
    assert ch["status"][1] == "sent" and ch["unsupported"][1] == [st["hook"]["id"]]


async def test_alert_note_reply_fallback_timeout_and_rule_opt_out(
    rt: Any, admin: httpx.AsyncClient, asst: Any
):
    from netmon.assistant.features.notes import NoteJob

    await on(rt, "ai_alert_notes")
    st = await _note_setup(rt, admin)
    notes = rt.ai_features.notes
    rec = TgRecorder(fail_edit=True)
    notes.transport = httpx.MockTransport(rec.handler)
    try:
        tid = (
            await q(
                rt,
                "INSERT INTO assistant_note_targets (event_id, rule_id, channel_id, "
                "channel_type, ref, sent_at) "
                "VALUES (:e, :r, :c, 'telegram', CAST(:ref AS jsonb), now()) RETURNING id",
                e=st["ev"]["id"],
                r=st["rule"]["id"],
                c=st["tg"]["id"],
                ref=json.dumps(
                    {"chat_id": "-100123", "message_id": 42, "text": "x", "parse_mode": "HTML"}
                ),
            )
        )[0]["id"]
        job = NoteJob(
            st["ev"]["id"],
            st["rule"]["id"],
            st["s"]["id"],
            st["dev"]["id"],
            time.monotonic(),
            rt.clock.now(),
            "DOWN core-sw / Ping",
            None,
            [tid],
        )
        assert await notes.process(job) == "sent"
        assert [m for m, _ in rec.calls] == ["editMessageText", "sendMessage"]
        assert rec.calls[1][1]["reply_to_message_id"] == 42
        assert rec.calls[1][1]["text"].startswith("<i>AI note:</i>")
        # too late → skipped (timeout), nothing sent
        rec.calls.clear()
        await ex(rt, "DELETE FROM assistant_artifacts")
        late = NoteJob(
            st["ev"]["id"],
            st["rule"]["id"],
            st["s"]["id"],
            st["dev"]["id"],
            time.monotonic() - 1000,
            rt.clock.now(),
            "t",
            None,
            [tid],
        )
        assert await notes.process(late) == "timeout"
        assert rec.calls == []
        # daily cap 0 → skipped (cap)
        await put_setting(rt, "alert_notes_daily_cap", 0)
        fresh = NoteJob(
            st["ev"]["id"],
            st["rule"]["id"],
            st["s"]["id"],
            st["dev"]["id"],
            time.monotonic(),
            rt.clock.now(),
            "t",
            None,
            [tid],
        )
        assert await notes.process(fresh) == "cap"
    finally:
        notes.transport = None
    # a rule without ai_note: nothing queued
    st2 = await _note_setup(rt, admin, ai_note=False, sfx="2")
    before = len(asst.requests("converse"))
    rt.ai_features.after_alert_sends(*_sends(st2))
    await notes.idle()
    assert len(asst.requests("converse")) == before
    assert not await q(
        rt, "SELECT 1 FROM assistant_note_targets WHERE event_id = :e", e=st2["ev"]["id"]
    )


async def test_alert_engine_receipts(monkeypatch: pytest.MonkeyPatch):
    """send_to_channel returns the delivery receipt the note needs (no DB)."""
    from netmon.alerts import engine

    sent: dict[str, Any] = {}

    async def fake_tg(cfg, text_, parse_mode, markup):  # noqa: ANN001
        sent["tg"] = text_
        return {"message_id": 7, "chat": {"id": -1}}

    async def fake_mail(smtp, to, subject, text_, html_=None, headers=None):  # noqa: ANN001
        return "<m1@nm>"

    monkeypatch.setattr(engine, "send_telegram", fake_tg)
    monkeypatch.setattr(engine, "send_email", fake_mail)
    payload = engine.build_payload(
        {
            "id": 1,
            "time": "2026-11-09T12:00:00Z",
            "old_status": "up",
            "new_status": "down",
            "message": "m",
            "sensor_id": 1,
        },
        {"id": 1, "name": "Ping", "type": "ping"},
        {"id": 1, "name": "core", "host": "h"},
        {"id": 1, "name": "g"},
        {"id": 1, "name": "r"},
        None,
    )
    r = await engine.send_to_channel(
        {"type": "telegram", "config": {"chat_id": "-1"}}, payload, {}, "en"
    )
    assert r == {
        "chat_id": "-1",
        "message_id": 7,
        "text": sent["tg"],
        "parse_mode": "HTML",
        "reply_markup": None,
    }
    r = await engine.send_to_channel(
        {"type": "email", "config": {"to": ["a@b.c"]}}, payload, {}, "en"
    )
    assert r["message_id"] == "<m1@nm>" and r["to"] == ["a@b.c"]


# --------------------------------------------------------------------------- config diff (§ 14.8.4)
OLD_CFG = "hostname core\nntp server 10.0.0.1\nssh server acl 2001\n"
NEW_CFG = "hostname core\nntp server 10.0.0.1\nsnmp-agent community write cipher <secret removed>\n"


async def test_config_diff_flags_and_explanation(rt: Any, admin: httpx.AsyncClient, asst: Any):
    await configure(rt, mock_settings(asst, send_config_excerpts=True))
    await on(rt, "config_backup", "ai_config_diff")
    dev = await make_device(admin, name="core-sw")
    ids = []
    for i, content in enumerate((OLD_CFG, NEW_CFG)):
        ids.append(
            (
                await q(
                    rt,
                    "INSERT INTO config_versions (device_id, collected_at, trigger, driver, "
                    "sha256, content, masked, "
                    "size_bytes, lines, initial, is_latest) VALUES (:d, now() - "
                    "make_interval(hours => :h), "
                    "'schedule', 'hpe_comware', :sha, :c, true, 10, 3, :init, :latest) RETURNING "
                    "id",
                    d=dev["id"],
                    h=2 - i,
                    sha=str(i) * 64,
                    c=content,
                    init=i == 0,
                    latest=i == 1,
                )
            )[0]["id"]
        )
    r = await admin.get(
        "/api/assistant/config-diff/flags",
        params={"device_id": dev["id"], "from": ids[0], "to": ids[1]},
    )
    assert r.status_code == 200, r.text
    flags = {f["rule_id"]: f for f in r.json()["flags"]}
    assert set(flags) == {"snmp_community_added", "mgmt_acl_removed"}
    assert flags["snmp_community_added"]["severity"] == "high"
    asst.add_scenario(
        {
            "name": "diff_qa",
            "purpose": "diff_explain",
            "priority": 50,
            "turns": [
                {
                    "tool_use": [
                        {
                            "name": "report_diff_explanation",
                            "input": {
                                "summary": ["An SNMP write community was added."],
                                "risks": [
                                    {
                                        "severity": "high",
                                        "title": "SNMP",
                                        "explanation": "x",
                                        "lines": [flags["snmp_community_added"]["lines"][0], 999],
                                    }
                                ],
                                "confidence": "medium",
                            },
                        }
                    ]
                }
            ],
        }
    )
    body = {"device_id": dev["id"], "from": ids[0], "to": ids[1]}
    r = await admin.post("/api/assistant/config-diff/explain", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "ok" and out["ai"]["confidence"] == "medium"
    assert out["ai"]["summary"][0].startswith("An SNMP write community")
    assert out["ai"]["risks"][0]["lines"] == flags["snmp_community_added"]["lines"]  # 999 dropped
    req = asst.requests("converse")[-1]
    sent = json.dumps(req["body"])
    assert "snmp-agent community" in sent and req["forced_tool"] == "report_diff_explanation"
    # cached per pair
    n = len(asst.requests("converse"))
    assert (await admin.post("/api/assistant/config-diff/explain", json=body)).json()[
        "status"
    ] == "ok"
    assert len(asst.requests("converse")) == n
    # model failure: flags still returned, status failed
    asst.mode("access_denied")
    r = await admin.post("/api/assistant/config-diff/explain", json={**body, "refresh": True})
    assert r.status_code == 200 and r.json()["status"] == "failed" and r.json()["flags"]
    assert r.json()["error"]["code"] == "bedrock_access_denied"
    acts = await audit_actions(rt, "assistant.diff_explain")
    assert len(acts) == 2


# --------------------------------------------------------------------------- digest (§ 14.8.5)
async def test_syslog_digest_stats_new_types_and_ai(rt: Any, admin: httpx.AsyncClient, asst: Any):
    await put_setting(rt, "digest_time", "23:59")
    await put_setting(rt, "timezone", "UTC")
    await on(rt, "syslog", "ai_syslog_digest")
    dev = await make_device(admin, name="fw1")
    from netmon.assistant.features import common

    async with rt.sessionmaker() as session:
        tz = await common.tz_name(session)
    today = common.local_day(rt, tz)
    day = today - timedelta(days=1)
    from netmon.assistant.features.digest import day_bounds

    async def add(d: Any, n: int, program: str, msg: str, sev: int = 5) -> None:
        start, _ = day_bounds(d, tz)
        await ex(
            rt,
            "INSERT INTO syslog_messages (time, source_ip, device_id, transport, format, "
            "facility, severity, "
            "hostname, program, message) SELECT CAST(:t AS timestamptz) + make_interval(secs => "
            "g), '10.0.0.5', :d, 'udp', 'rfc3164', "
            "4, :sev, 'fw1', :p, :m FROM generate_series(1, :n) g",
            t=start + timedelta(hours=1),
            d=dev["id"],
            sev=sev,
            p=program,
            m=msg,
            n=n,
        )

    for k in range(1, 5):  # baseline days
        await add(day - timedelta(days=k), 10, "sshd", "Accepted publickey for root from 10.0.0.7")
    await add(day, 40, "sshd", "Accepted publickey for root from 10.0.0.7")
    await add(day, 25, "kernel", "Out of memory: Killed process 1234 (java)", 3)
    # the previous day's run fills the signature register (sshd is then known)
    await rt.ai_features.digest.run(day - timedelta(days=1), send=False, user=None)
    out = await rt.ai_features.digest.run(day, send=False, user=None)
    assert out["status"] == "ok", out
    stats = out["stats"]
    assert out["total_messages"] == 65 and out["baseline_days"] == 4
    prog = {r["key"]: r for r in stats["by_program"]}
    assert prog["sshd"]["unusual"] and prog["sshd"]["baseline"] == 10.0
    assert prog["kernel"]["unusual"]  # new program with ≥ 20 messages
    sev3 = {r["key"]: r for r in stats["by_severity"]}["3"]
    assert sev3["unusual"]
    samples = [t["sample"] for t in stats["new_types"]]
    assert samples == ["out of memory: killed process <n> (java)"]
    assert all(t["example_ref"].startswith("syslog/") for t in stats["new_types"])
    assert out["ai_markdown"] and "sshd" in out["ai_markdown"]
    reqs = asst.requests("converse")
    assert reqs[-1]["purpose"] == "digest"
    assert "Accepted publickey" not in json.dumps(reqs[-1]["body"]["messages"])  # stats only
    r = await admin.get(f"/api/assistant/digests/{day.isoformat()}")
    assert r.status_code == 200 and r.json()["unusual_count"] >= 2
    r = await admin.get("/api/assistant/digests")
    assert r.json()["items"][0]["day"] == day.isoformat()
    # the register: rerunning the day keeps the "new" types (first seen that day)
    out2 = await rt.ai_features.digest.run(day, send=False, user=None)
    assert len(out2["stats"]["new_types"]) == len(stats["new_types"])
    sigs = await q(rt, "SELECT count(*) AS n FROM assistant_syslog_signatures")
    assert sigs[0]["n"] >= 2
    # model failure → statistics kept, status failed
    asst.mode("unavailable")
    out3 = await rt.ai_features.digest.run(day, send=False, user=None)
    assert (
        out3["status"] == "failed" and out3["ai_markdown"] is None and out3["total_messages"] == 65
    )
    # manual run endpoint (202) and range check
    asst.mode("normal")
    r = await admin.post(f"/api/assistant/digests/{day.isoformat()}/run", json={"send": False})
    assert r.status_code == 202 and r.json()["status"] == "running"
    await rt.ai_features.digest.idle()
    r = await admin.post(
        f"/api/assistant/digests/{(today - timedelta(days=30)).isoformat()}/run", json={}
    )
    assert r.status_code == 422
    assert await audit_actions(rt, "assistant.digest")


# --------------------------------------------------------------------------- rule drafts (§ 14.8.6)
async def test_rule_drafts_alert_and_event(rt: Any, admin: httpx.AsyncClient, asst: Any):
    await on(rt, "ai_rule_drafts")
    tg = (
        await admin.post(
            "/api/notification-channels",
            json={
                "name": "ops-telegram",
                "type": "telegram",
                "config": {"bot_token": "123456:" + "A" * 30, "chat_id": "-100123"},
            },
        )
    ).json()
    r = await admin.post(
        "/api/assistant/rule-drafts",
        json={
            "kind": "alert_rule",
            "description": "Telegram me when a core switch is down for 2 minutes",
        },
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["kind"] == "alert_rule" and d["draft"]["channel_ids"] == [tg["id"]]
    assert d["draft"]["delay_s"] == 120 and d["validation"] == {"ok": True, "errors": []}
    assert "explanation" not in d["draft"] and d["explanation"] and d["assumptions"]
    assert (
        await q(
            rt, "SELECT kind FROM assistant_conversations WHERE id = :i", i=d["conversation_id"]
        )
    )[0]["kind"] == "rule_draft"
    assert not await q(rt, "SELECT 1 FROM alert_rules")  # nothing saved
    reqs = [x for x in asst.requests("converse") if x["purpose"] == "rule_draft"]
    assert "list_notification_channels" in reqs[0]["offered_tools"]
    # the admin saves through the normal API
    r = await admin.post("/api/alert-rules", json=d["draft"])
    assert r.status_code == 201, r.text
    # event rule: needs traps or syslog
    r = await admin.post(
        "/api/assistant/rule-drafts",
        json={"kind": "event_rule", "description": "syslog login failures"},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "feature_idle"
    await on(rt, "syslog")
    r = await admin.post(
        "/api/assistant/rule-drafts",
        json={"kind": "event_rule", "description": "syslog login failures"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["kind"] == "syslog" and r.json()["validation"]["ok"], r.json()
    acts = await audit_actions(rt, "assistant.rule_draft")
    assert len(acts) == 2 and "login failures" not in json.dumps(acts)  # metadata only


async def test_rule_draft_validation_errors_are_returned(
    rt: Any, admin: httpx.AsyncClient, asst: Any
):
    await on(rt, "ai_rule_drafts")
    asst.add_scenario(
        {
            "name": "bad_draft",
            "purpose": "rule_draft",
            "priority": 50,
            "match": "bogus channel",
            "turns": [
                {
                    "tool_use": [
                        {
                            "name": "propose_alert_rule",
                            "input": {
                                "name": "x",
                                "on_statuses": ["down"],
                                "channel_ids": [424242],
                                "explanation": "e",
                            },
                        }
                    ]
                }
            ],
        }
    )
    r = await admin.post(
        "/api/assistant/rule-drafts",
        json={"kind": "alert_rule", "description": "bogus channel rule"},
    )
    assert r.status_code == 200, r.text
    v = r.json()["validation"]
    assert not v["ok"] and v["errors"][0]["loc"] == ["body", "channel_ids"]
    # one automatic repair round was attempted
    assert len([x for x in asst.requests("converse") if x["purpose"] == "rule_draft"]) == 2


# --------------------------------------------------------------------------- templates (§ 14.8.7)
VALID_TEMPLATE = """schema: 1
id: qa_user_x100
name: {en: QA X100, he: QA X100}
priority: 40
vendor: qa
match:
  - sys_object_id_prefixes: ["1.3.6.1.4.1.99999"]
entries:
  - kind: sensor
    type: ping
    name: {en: Ping, he: Ping}
    confidence: high
"""


async def test_template_walk_draft_and_user_save(
    rt: Any, admin: httpx.AsyncClient, asst: Any, viewer: httpx.AsyncClient
):
    await on(rt, "ai_template_drafts")
    tpl = rt.ai_features.templates
    walked: list[str] = []

    async def fake_walker(host, creds, base, max_rows, deadline):  # noqa: ANN001
        walked.append(base)
        if base == "1.3.6.1.2.1.1":
            return [
                ("1.3.6.1.2.1.1.1.0", "OctetString", "QA X100 v1"),
                ("1.3.6.1.2.1.1.2.0", "ObjectIdentifier", "1.3.6.1.4.1.99999.1"),
            ]
        if base == "1.3.6.1.4.1.99999":
            return [
                ("1.3.6.1.4.1.99999.1.1.0", "Gauge32", "42"),
                ("1.3.6.1.6.3.15.1.2.2.1.3.1", "OctetString", "never"),
            ]
        return []

    async def fake_sysoid(host, creds):  # noqa: ANN001
        return "1.3.6.1.4.1.99999.1"

    tpl.walker, tpl.sys_object_id = fake_walker, fake_sysoid
    dev = await make_device(admin, name="x100")
    r = await admin.post(
        "/api/assistant/template-drafts/walks",
        json={
            "device_id": dev["id"],
            "presets": ["system", "enterprise"],
            "oids": ["1.3.6.1.6.3.15"],
        },
    )
    assert r.status_code == 422 and r.json()["error"]["details"] == [
        {"oid": "1.3.6.1.6.3.15", "reason": "never_walked"}
    ]
    r = await admin.post(
        "/api/assistant/template-drafts/walks",
        json={"device_id": dev["id"], "presets": ["system", "enterprise"]},
    )
    assert r.status_code == 202, r.text
    wid = r.json()["id"]
    await tpl.idle()
    w = (await admin.get(f"/api/assistant/template-drafts/walks/{wid}")).json()
    assert (
        w["status"] == "ok" and w["total"] == 3 and walked == ["1.3.6.1.2.1.1", "1.3.6.1.4.1.99999"]
    )
    assert all(not v[0].startswith("1.3.6.1.6.3.15") for v in w["varbinds"])
    r = await admin.post(
        "/api/assistant/template-drafts", json={"walk_id": wid, "template_id": "qa_draft_x100"}
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["yaml"] and isinstance(d["valid"], bool) and d["conversation_id"]
    assert (
        "id: qa_draft_x100" in d["yaml"]
        and "unverified" not in d["yaml"].split("confidence:")[-1].split("\n")[0]
    )
    req = [x for x in asst.requests("converse") if x["purpose"] == "template_draft"][0]
    assert req["forced_tool"] == "propose_template" and "1.3.6.1.6.3.15" not in json.dumps(
        req["body"]
    )
    assert await audit_actions(rt, "assistant.template_walk") and await audit_actions(
        rt, "assistant.template_draft"
    )
    # the normal save (always available, independent of the AI)
    await rt.features.apply({"ai_template_drafts": False, "assistant": False})
    r = await viewer.post("/api/device-templates/user", json={"yaml": VALID_TEMPLATE})
    assert r.status_code == 403
    r = await admin.post("/api/device-templates/user", json={"yaml": VALID_TEMPLATE})
    assert r.status_code == 201, r.text
    assert (
        r.json()["file"] == "qa/qa_user_x100.yaml" and r.json()["template"]["id"] == "qa_user_x100"
    )
    assert (rt.settings.data_dir / "templates" / "qa" / "qa_user_x100.yaml").is_file()
    assert (await admin.get("/api/device-templates/qa_user_x100")).status_code == 200
    r = await admin.post("/api/device-templates/user", json={"yaml": VALID_TEMPLATE})
    assert r.status_code == 409 and r.json()["error"]["code"] == "template_exists"
    r = await admin.post(
        "/api/device-templates/user", json={"yaml": VALID_TEMPLATE, "overwrite": True}
    )
    assert r.status_code == 201
    r = await admin.post("/api/device-templates/user", json={"yaml": "schema: 1\nid: bad\n"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_template"
    assert await audit_actions(rt, "device_template.create_user")
    (rt.settings.data_dir / "templates" / "qa" / "qa_user_x100.yaml").unlink()
    from netmon.templates import registry

    await registry.reload(rt)


# --------------------------------------------------------------------------- /ask (§ 14.8.9)
class FakePoller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, token: str, method: str, body: dict, read_timeout: float = 15.0) -> Any:
        self.calls.append((method, body))
        return {"message_id": 501}

    async def _send(self, bot: Any, method: str, body: dict) -> None:
        self.calls.append((method, body))


async def test_telegram_ask(rt: Any, admin: httpx.AsyncClient, asst: Any):
    await put_setting(rt, "public_base_url", "https://nm.example")
    await on(rt, "chat_ack", "ai_chat_ask")
    cid = (
        await q(
            rt,
            "INSERT INTO notification_channels (name, type, config, enabled) VALUES "
            "('bot', 'telegram', CAST(:c AS jsonb), true) RETURNING id",
            c=json.dumps({"bot_token": "1:" + "B" * 30, "chat_id": "1", "ai_ask": True}),
        )
    )[0]["id"]
    uid = (await q(rt, "SELECT id FROM users WHERE username = 'admin'"))[0]["id"]
    await ex(
        rt,
        "INSERT INTO chat_identities (user_id, provider, external_id, display, linked_at) "
        "VALUES (:u, 'telegram', '555', '@adm', now())",
        u=uid,
    )
    dev = await make_device(admin, name="core-sw", host="10.0.0.9")
    s = await make_sensor(admin, dev["id"], "ping")
    await ex(rt, "UPDATE sensors SET status = 'down' WHERE id = :s", s=s["id"])
    bot = SimpleNamespace(token="1:" + "B" * 30, channel_ids=[cid], username="NetBot")
    poller = FakePoller()
    ai = rt.ai_features

    def msg(text_: str, sender: int = 555, chat_type: str = "private") -> dict:
        return {"text": text_, "from": {"id": sender}, "chat": {"id": 77, "type": chat_type}}

    assert await ai.chat_ask_handle(poller, bot, msg("/ask which devices are down?"))
    await ai.chatask.idle()
    methods = [m for m, _ in poller.calls]
    assert methods == ["sendMessage", "editMessageText"]
    assert poller.calls[0][1]["text"] == "Working on it…"
    edit = poller.calls[1][1]
    assert edit["message_id"] == 501 and edit["parse_mode"] == "HTML"
    assert "core-sw" in edit["text"] and "https://nm.example/assistant/c/" in edit["text"]
    assert "AI-generated" in edit["text"]
    conv = (await q(rt, "SELECT channel FROM assistant_conversations ORDER BY id DESC LIMIT 1"))[0]
    assert conv["channel"] == "telegram"
    qa = await audit_actions(rt, "assistant.question")
    assert qa[-1]["source"] == "telegram"
    # refusals
    for m_, expect in (
        (msg("/ask hi", chat_type="group"), "Use a private chat with the bot"),
        (msg("/ask hi", sender=999), "not linked"),
        (msg("/ask"), "Write a question"),
    ):
        poller.calls.clear()
        assert await ai.chat_ask_handle(poller, bot, m_)
        assert expect in poller.calls[0][1]["text"]
    # /ask to another bot and other commands are not ours
    assert not await ai.chat_ask_handle(poller, bot, msg("/ask@OtherBot hi"))
    assert not await ai.chat_ask_handle(poller, bot, msg("/link NM-ABCDEFGH"))
    # feature off → "Asking is turned off"
    await rt.features.apply({"ai_chat_ask": False})
    poller.calls.clear()
    assert await ai.chat_ask_handle(poller, bot, msg("/ask hi"))
    assert poller.calls[0][1]["text"] == "Asking is turned off"


# ---------------------------------------------------------------- report summary (§ 14.8.10)
async def test_report_ai_summary(rt: Any, admin: httpx.AsyncClient, asst: Any):
    await on(rt, "reports", "ai_report_summary")
    asst.add_scenario(
        {
            "name": "report_plain",
            "purpose": "report_summary",
            "priority": 50,
            "turns": [{"text": "All monitored devices were available; no notable changes."}],
        }
    )
    await make_device(admin, name="d1")
    r = await admin.post(
        "/api/reports", json={"name": "weekly", "sections": ["availability"], "ai_summary": True}
    )
    assert r.status_code == 201, r.text
    assert r.json()["ai_summary"] is True
    rid = r.json()["id"]
    r = await admin.post(f"/api/reports/{rid}/run", json={})
    assert r.status_code == 202, r.text
    run_id = r.json()["id"]
    for _ in range(100):
        run = (await admin.get(f"/api/reports/runs/{run_id}")).json()
        if run["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert run["ai_summary_status"] == "ok", run
    assert run["data"]["ai_summary"].startswith("All monitored devices")
    html_ = (await admin.get(f"/api/reports/runs/{run_id}/html")).text
    assert "AI-generated summary" in html_
    assert await audit_actions(rt, "assistant.report_summary")
    # numbers not in the data → dropped
    asst.add_scenario(
        {
            "name": "report_bad",
            "purpose": "report_summary",
            "priority": 60,
            "turns": [{"text": "Availability was 12.34 % this week."}],
        }
    )
    run_id = (await admin.post(f"/api/reports/{rid}/run", json={})).json()["id"]
    for _ in range(100):
        run = (await admin.get(f"/api/reports/runs/{run_id}")).json()
        if run["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert run["ai_summary_status"] == "unverified_numbers" and run["data"]["ai_summary"] is None
    # model down → report still rendered, status failed
    asst.mode("unavailable")
    run_id = (await admin.post(f"/api/reports/{rid}/run", json={})).json()["id"]
    for _ in range(200):
        run = (await admin.get(f"/api/reports/runs/{run_id}")).json()
        if run["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert run["status"] == "ok" and run["ai_summary_status"] == "failed"
    # opt-out report: no status
    r = await admin.patch(f"/api/reports/{rid}", json={"ai_summary": False})
    assert r.json()["ai_summary"] is False


# --------------------------------------------------------------------------- caps / background stop
async def test_background_stop_threshold_skips_notes(rt: Any, admin: httpx.AsyncClient, asst: Any):
    from netmon.assistant.features import common

    await put_setting(rt, "assistant_monthly_token_cap", 100000)
    async with rt.sessionmaker() as session:
        tz = await common.tz_name(session)
    await ex(
        rt,
        "INSERT INTO assistant_usage (day, user_id, purpose, input_tokens) VALUES (:d, 0, 'ask', "
        "90000)",
        d=common.local_day(rt, tz),
    )
    with pytest.raises(common.AiError) as ei:
        await common.check_caps(rt, "alert_note")
    assert ei.value.code == "background_stop"
    await common.check_caps(rt, "rule_draft", 1)  # interactive still allowed below 100 %


# --------------------------------------------------------------------------- A's real classes
async def test_usage_through_real_usage_service_and_client(
    rt: Any, admin: httpx.AsyncClient, asst: Any
):
    """The features call developer A's UsageService / BedrockClient (no stubs): one question is
    counted per draft, background purposes are counted for user 0, and /ask redacts IPs."""
    from netmon.assistant.bedrock import BedrockClient
    from netmon.assistant.features import common
    from netmon.assistant.usage import UsageService

    assert isinstance(rt.assistant.usage, UsageService)
    assert isinstance(common.client_of(rt), BedrockClient)
    await on(rt, "ai_rule_drafts", "ai_report_summary", "reports")
    await admin.post(
        "/api/notification-channels",
        json={
            "name": "tg",
            "type": "telegram",
            "config": {"bot_token": "123456:" + "A" * 30, "chat_id": "-100123"},
        },
    )
    r = await admin.post(
        "/api/assistant/rule-drafts",
        json={"kind": "alert_rule", "description": "Telegram me when a core switch is down"},
    )
    assert r.status_code == 200, r.text
    rows = await q(
        rt,
        "SELECT user_id, purpose, questions, model_calls, input_tokens FROM "
        "assistant_usage WHERE purpose = 'rule_draft'",
    )
    assert len(rows) == 1 and rows[0]["questions"] == 1
    assert rows[0]["model_calls"] == 2 and rows[0]["input_tokens"] > 0  # tool round + proposal
    comp = await common.complete(
        rt,
        purpose="report_summary",
        system="[purpose: report_summary]",
        messages=[common.user_message("{}")],
        max_tokens=50,
    )
    assert comp.text and comp.model_id
    bg = await q(
        rt,
        "SELECT user_id, questions, model_calls FROM assistant_usage "
        "WHERE purpose = 'report_summary'",
    )
    assert bg == [{"user_id": 0, "questions": 0, "model_calls": 1}]
    # the per-user daily cap is enforced by A's check_and_reserve
    await put_setting(rt, "assistant_daily_questions_per_user", 1)
    r = await admin.post(
        "/api/assistant/rule-drafts",
        json={"kind": "alert_rule", "description": "Another rule please"},
    )
    assert r.status_code == 429 and r.json()["error"]["details"][0]["cap"] == "user_daily"


async def test_wrong_usage_call_fails_loudly(rt: Any, admin: httpx.AsyncClient, asst: Any):
    from netmon.assistant.features import common

    class Broken:
        async def check(self) -> None:  # wrong signature
            return None

    real = rt.assistant.usage
    rt.assistant.usage = Broken()
    try:
        with pytest.raises(TypeError):
            await common.check_caps(rt, "alert_note")
    finally:
        rt.assistant.usage = real
