"""[severity] Alert severity end to end: the feature toggle, resolution when events are written
(override > event rule > mapping), the edited mapping, history stability, re-resolution of
active problems (with audit), notification-rule severities, payloads, filters / sort, the
dashboard / kiosk counts, incidents, reports, assistant tools, bulk override, permissions and
migration 0015 up / down / up in both database modes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy import text

from netmon.clock import FakeClock
from netmon.db.migrate import migration_head
from netmon.poller.processor import persist
from netmon.poller.types import CheckResult
from netmon.runtime import Runtime
from tests.conftest import _plain, make_device, make_sensor, requires_db

pytestmark = requires_db

UP = CheckResult(True, "", {"latency_ms": 1.0, "packet_loss_pct": 0.0})
DOWN = CheckResult(False, "Host unreachable")
WARN = CheckResult(True, "", {"latency_ms": 150.0, "packet_loss_pct": 0.0})


# ---------------------------------------------------------------- helpers
@pytest.fixture
async def clock(rt: Runtime, admin: httpx.AsyncClient) -> AsyncIterator[FakeClock]:
    old = rt.clock
    fake = FakeClock(datetime.now(UTC).replace(microsecond=0))
    rt.clock = fake
    yield fake
    rt.clock = old


class Outbox:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, channel, payload, smtp, lang):  # type: ignore[no-untyped-def]
        self.items.append((channel["name"], payload))

    def names(self) -> list[str]:
        return sorted(c for c, _ in self.items)


@pytest.fixture
async def outbox(rt: Runtime, admin: httpx.AsyncClient) -> Outbox:
    box = Outbox()
    rt.alerts.sender = box
    return box


async def feature(c: httpx.AsyncClient, on: bool, *, audit: bool | None = None) -> None:
    body: dict[str, Any] = {"feature_alert_severity_enabled": on}
    if audit is not None:
        body["feature_audit_enabled"] = audit
    r = await c.put("/api/settings", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["feature_alert_severity_enabled"] is on


async def channel(c: httpx.AsyncClient, name: str) -> int:
    r = await c.post(
        "/api/notification-channels",
        json={"name": name, "type": "webhook", "config": {"url": f"http://{name}.invalid/"}},
    )
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def rule(c: httpx.AsyncClient, name: str, chan: int, **kw: Any) -> dict[str, Any]:
    body = {"name": name, "on_statuses": ["down", "warning", "up"], "channel_ids": [chan], **kw}
    r = await c.post("/api/alert-rules", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def run(rt: Runtime, clock: FakeClock, sid: int, raw: CheckResult) -> None:
    await persist(rt, sid, raw, 1.0, clock.now())
    await rt.drain()
    await rt.alerts.idle()


async def q(rt: Runtime, sql: str, **p: Any) -> list[dict[str, Any]]:
    async with rt.sessionmaker() as session:
        rows = (await session.execute(text(sql), p)).mappings().all()
        await session.commit()
    return [dict(r) for r in rows]


async def sensor_sev(c: httpx.AsyncClient, sid: int) -> str | None:
    r = await c.get(f"/api/sensors/{sid}")
    assert r.status_code == 200
    return r.json()["severity"]


async def status_events(rt: Runtime, sid: int) -> list[tuple[str, str | None]]:
    rows = await q(
        rt,
        "SELECT new_status, severity FROM events WHERE sensor_id = :s AND kind = 'status_change' "
        "ORDER BY id",
        s=sid,
    )
    return [(r["new_status"], r["severity"]) for r in rows]


async def ping(c: httpx.AsyncClient, name: str = "dev1", **kw: Any) -> dict[str, Any]:
    dev = await make_device(c, name=name, host="10.9.9." + str(len(name)))
    return await make_sensor(c, dev["id"], "ping", name=f"Ping {name}", **kw)


# ---------------------------------------------------------------- feature toggle
async def test_feature_registered_off_by_default(admin: httpx.AsyncClient, viewer: Any) -> None:
    r = await admin.get("/api/settings")
    assert r.json()["feature_alert_severity_enabled"] is False
    pub = (await viewer.get("/api/settings/public")).json()["features"]
    assert pub["alert_severity"] is False
    await feature(admin, True)
    assert (await viewer.get("/api/settings/public")).json()["features"]["alert_severity"] is True


async def test_feature_off_nothing_changes(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    """OFF: no severity resolved or stored, payloads without the key, a rule's severities are
    not looked at (matching exactly as before), counts null."""
    ch = await channel(admin, "ops")
    await rule(admin, "only minor", ch, severities=["minor"])
    s = await ping(admin)
    await run(rt, clock, s["id"], UP)
    await run(rt, clock, s["id"], DOWN)
    assert outbox.names() == ["ops"]  # the minor-only rule still fires (feature off)
    payload = outbox.items[0][1]
    assert "severity" not in payload
    assert await status_events(rt, s["id"]) == [("up", None), ("down", None)]
    sensor = (await admin.get(f"/api/sensors/{s['id']}")).json()
    assert sensor["severity"] is None
    assert sensor["severity_down"] is None and sensor["severity_warning"] is None
    dash = (await admin.get("/api/dashboard")).json()
    assert dash["severity_counts"] is None and dash["severity_counts_unacknowledged"] is None
    events = (await admin.get("/api/events", params={"kind": "status_change"})).json()["items"]
    assert all(e["severity"] is None for e in events)
    sent = await q(rt, "SELECT severity FROM events WHERE kind = 'notification_sent'")
    assert sent == [{"severity": None}]


async def test_turning_on_resolves_active_problems_once_and_off_clears(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    s = await ping(admin)
    await run(rt, clock, s["id"], DOWN)
    assert await sensor_sev(admin, s["id"]) is None
    await feature(admin, True)
    assert await sensor_sev(admin, s["id"]) == "critical"
    # the opening event is history: no backfill
    assert await status_events(rt, s["id"]) == [("down", None)]
    await feature(admin, False)
    assert await sensor_sev(admin, s["id"]) is None


# ---------------------------------------------------------------- resolution
async def test_default_mapping_on_events_and_current_problem(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    await feature(admin, True)
    crit = await ping(admin, "a")
    warn = await ping(admin, "bb", impact="warning")
    none = await ping(admin, "ccc", impact="none")
    for s in (crit, warn, none):
        await run(rt, clock, s["id"], UP)
        await run(rt, clock, s["id"], DOWN)
    assert await sensor_sev(admin, crit["id"]) == "critical"
    assert await sensor_sev(admin, warn["id"]) == "major"
    assert await sensor_sev(admin, none["id"]) == "minor"
    await run(rt, clock, crit["id"], WARN)
    assert await sensor_sev(admin, crit["id"]) == "warning"
    await run(rt, clock, crit["id"], UP)
    assert await sensor_sev(admin, crit["id"]) is None  # no problem: null
    assert await status_events(rt, crit["id"]) == [
        ("up", "info"),
        ("down", "critical"),
        ("warning", "warning"),
        ("up", "info"),  # recovery
    ]
    # pausing is a status change that is not a problem
    r = await admin.patch(f"/api/sensors/{none['id']}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["severity"] is None
    assert (await status_events(rt, none["id"]))[-1] == ("paused", "info")


async def test_override_beats_mapping(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    await feature(admin, True)
    s = await ping(admin, severity_down="minor")
    assert s["severity_down"] == "minor" and s["severity_warning"] is None
    await run(rt, clock, s["id"], DOWN)
    assert await sensor_sev(admin, s["id"]) == "minor"
    await run(rt, clock, s["id"], WARN)  # no warning override: the mapping
    assert await sensor_sev(admin, s["id"]) == "warning"


async def _event_sensor_with_rule(
    rt: Runtime, admin: httpx.AsyncClient, rule_sev: str | None, **sensor_kw: Any
) -> tuple[int, int]:
    dev = await make_device(admin, name="sw-ev", host="10.8.8.8")
    s = await make_sensor(admin, dev["id"], "syslog", **sensor_kw)
    rid = (
        await q(
            rt,
            "INSERT INTO event_rules (name, kind, enabled, priority, scope, match, action, "
            "clear_after_min, severity) VALUES ('link', 'syslog', true, 5, 'global', "
            "CAST('{\"text\": \"LINK\"}' AS jsonb), 'down', 60, :sev) RETURNING id",
            sev=rule_sev,
        )
    )[0]["id"]
    return s["id"], rid


async def _condition(rt: Runtime, sid: int, rid: int | None, key: str = "") -> None:
    now = rt.clock.now()
    async with rt.sessionmaker() as session:
        await rt.event_sensors._upsert_condition(  # noqa: SLF001
            session,
            sid,
            source="rule" if rid else "flap",
            rule_id=rid,
            key=key,
            status="down",
            message="link down",
            now=now,
            expires_at=None,
        )
        evs, upd = await rt.event_sensors._recompute_many(session, [sid])  # noqa: SLF001
        await session.commit()
    rt.event_sensors._publish(evs, upd)  # noqa: SLF001
    await rt.drain()


async def test_event_rule_severity_beats_mapping_and_override_beats_rule(
    rt: Runtime, admin: httpx.AsyncClient
) -> None:
    await feature(admin, True)
    sid, rid = await _event_sensor_with_rule(rt, admin, "major")
    await _condition(rt, sid, rid)
    assert await sensor_sev(admin, sid) == "major"  # rule, not the mapping's critical
    assert (await status_events(rt, sid))[-1] == ("down", "major")
    # the override wins over the rule (re-resolved: the problem is active)
    r = await admin.patch(f"/api/sensors/{sid}", json={"severity_down": "minor"})
    assert r.status_code == 200 and r.json()["severity"] == "minor"
    # history keeps the value the event was written with
    assert (await status_events(rt, sid))[-1] == ("down", "major")
    r = await admin.patch(f"/api/sensors/{sid}", json={"severity_down": None})
    assert r.json()["severity_down"] is None and r.json()["severity"] == "major"


async def test_event_rule_without_severity_uses_mapping(
    rt: Runtime, admin: httpx.AsyncClient
) -> None:
    await feature(admin, True)
    sid, rid = await _event_sensor_with_rule(rt, admin, None, impact="warning")
    await _condition(rt, sid, rid)
    assert await sensor_sev(admin, sid) == "major"  # down + impact warning
    # a flap condition (no rule) too
    sid2 = sid
    await _condition(rt, sid2, None, key="ge1")
    assert await sensor_sev(admin, sid2) in ("major",)


async def test_event_rule_severity_in_api(rt: Runtime, admin: httpx.AsyncClient) -> None:
    r = await admin.put("/api/settings", json={"feature_syslog_enabled": True})
    assert r.status_code == 200
    body = {
        "name": "Link",
        "kind": "syslog",
        "match": {"text": "LINK"},
        "action": "down",
        "clear_after_min": 10,
        "severity": "major",
    }
    r = await admin.post("/api/event-rules", json=body)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["severity"] == "major"
    r = await admin.patch(f"/api/event-rules/{rid}", json={"severity": None})
    assert r.status_code == 200 and r.json()["severity"] is None
    r = await admin.patch(f"/api/event-rules/{rid}", json={"severity": "huge"})
    assert r.status_code == 422
    r = await admin.post(
        "/api/event-rules", json={**body, "action": "ignore", "clear_after_min": None}
    )
    assert r.status_code == 422
    builtin = (await admin.get("/api/event-rules")).json()["items"]
    assert all("severity" in x for x in builtin)


# ---------------------------------------------------------------- mapping, history, re-resolution
async def test_mapping_edit_reresolves_active_keeps_history_and_audits(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, viewer: httpx.AsyncClient
) -> None:
    await feature(admin, True, audit=True)
    s = await ping(admin)
    other = await ping(admin, "zz")
    await run(rt, clock, s["id"], DOWN)
    await run(rt, clock, other["id"], UP)
    assert await sensor_sev(admin, s["id"]) == "critical"
    r = await viewer.get("/api/settings/severity-mapping")
    assert r.status_code == 200
    assert r.json()["mapping"]["down_critical"] == "critical"
    assert r.json()["defaults"] == r.json()["mapping"]
    assert r.json()["levels"] == ["critical", "major", "minor", "warning", "info"]
    r = await admin.put("/api/settings/severity-mapping", json={"down_critical": "major"})
    assert r.status_code == 200, r.text
    assert r.json()["mapping"]["down_critical"] == "major"
    assert r.json()["mapping"]["down_warning"] == "major"  # untouched rows kept
    # the active problem follows, its history does not
    assert await sensor_sev(admin, s["id"]) == "major"
    assert await status_events(rt, s["id"]) == [("down", "critical")]
    audit = await q(
        rt,
        "SELECT action, entity_id, reason, changes FROM audit_log "
        "WHERE action IN ('sensor.severity', 'settings.update') ORDER BY id",
    )
    assert audit[0]["action"] == "settings.update"
    assert audit[0]["changes"]["severity_mapping.down_critical"] == ["critical", "major"]
    assert audit[1] == {
        "action": "sensor.severity",
        "entity_id": s["id"],
        "reason": "mapping",
        "changes": {"severity": ["critical", "major"]},
    }
    assert len(audit) == 2  # the healthy sensor is not touched
    # new problems use the edited mapping
    await run(rt, clock, other["id"], DOWN)
    assert await status_events(rt, other["id"]) == [("up", "info"), ("down", "major")]
    # reset
    r = await admin.post("/api/settings/severity-mapping/reset")
    assert r.status_code == 200 and r.json()["mapping"] == r.json()["defaults"]
    assert await sensor_sev(admin, s["id"]) == "critical"
    assert await status_events(rt, other["id"]) == [("up", "info"), ("down", "major")]
    # invalid level / unknown key
    assert (
        await admin.put("/api/settings/severity-mapping", json={"info": "high"})
    ).status_code == 422
    assert (
        await admin.put("/api/settings/severity-mapping", json={"down_bogus": "info"})
    ).status_code == 422


async def test_override_change_audit_and_alert_state(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    await feature(admin, True, audit=True)
    ch = await channel(admin, "ops")
    await rule(admin, "all", ch)
    s = await ping(admin)
    await run(rt, clock, s["id"], DOWN)
    [st] = await q(rt, "SELECT severity FROM alert_state WHERE sensor_id = :s", s=s["id"])
    assert st["severity"] == "critical"
    r = await admin.patch(f"/api/sensors/{s['id']}", json={"severity_down": "info"})
    assert r.status_code == 200 and r.json()["severity"] == "info"
    [st] = await q(rt, "SELECT severity FROM alert_state WHERE sensor_id = :s", s=s["id"])
    assert st["severity"] == "info"
    rows = await q(
        rt,
        "SELECT action, reason, changes FROM audit_log WHERE entity_id = :s "
        "AND action LIKE 'sensor.%' ORDER BY id",
        s=s["id"],
    )
    assert rows[-2]["action"] == "sensor.update"
    assert rows[-2]["changes"] == {"severity_down": [None, "info"]}
    assert rows[-1] == {
        "action": "sensor.severity",
        "reason": "override",
        "changes": {"severity": ["critical", "info"]},
    }
    # impact change re-resolves too (input of the mapping)
    await admin.patch(f"/api/sensors/{s['id']}", json={"severity_down": None})
    r = await admin.patch(f"/api/sensors/{s['id']}", json={"impact": "none"})
    assert r.json()["severity"] == "minor"
    # the recovery payload names the severity of the problem that recovered
    outbox.items.clear()
    await run(rt, clock, s["id"], UP)
    [(_, rec)] = outbox.items
    assert rec["stage"] == "recovery" and rec["severity"] == "minor"


# ---------------------------------------------------------------- notifications
async def test_rule_severities_filter_and_payload(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    await feature(admin, True)
    a, b, c = await channel(admin, "a"), await channel(admin, "b"), await channel(admin, "c")
    ra = await rule(admin, "crit", a, severities=["critical", "major"])
    assert ra["severities"] == ["critical", "major"]
    rb = await rule(admin, "all", b)
    assert rb["severities"] == []
    await rule(admin, "minor", c, severities=["minor"])
    s = await ping(admin)
    await run(rt, clock, s["id"], DOWN)
    assert outbox.names() == ["a", "b"]  # empty = all; minor-only not matched
    payload = outbox.items[0][1]
    assert payload["severity"] == "critical"
    assert list(payload)[-1] == "severity"
    sent = await q(rt, "SELECT DISTINCT severity FROM events WHERE kind = 'notification_sent'")
    assert sent == [{"severity": "critical"}]
    outbox.items.clear()
    t = await ping(admin, "nn", impact="none")
    await run(rt, clock, t["id"], DOWN)
    assert outbox.names() == ["b", "c"]
    assert {p["severity"] for _, p in outbox.items} == {"minor"}
    # PATCH replaces the set, [] clears it, bad levels -> 422
    r = await admin.patch(f"/api/alert-rules/{ra['id']}", json={"severities": ["info", "critical"]})
    assert r.json()["severities"] == ["critical", "info"]
    r = await admin.patch(f"/api/alert-rules/{ra['id']}", json={"severities": []})
    assert r.json()["severities"] == []
    r = await admin.patch(f"/api/alert-rules/{ra['id']}", json={"severities": ["high"]})
    assert r.status_code == 422


# ---------------------------------------------------------------- filters / sort / counts
async def test_filters_sort_and_counts(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, viewer: httpx.AsyncClient
) -> None:
    await feature(admin, True)
    crit = await ping(admin, "a")
    minor = await ping(admin, "bb", impact="none")
    warn = await ping(admin, "ccc")
    await run(rt, clock, minor["id"], DOWN)
    await run(rt, clock, crit["id"], DOWN)
    await run(rt, clock, warn["id"], WARN)
    r = await viewer.get("/api/events", params={"severity": "critical,minor"})
    assert r.status_code == 200
    assert sorted(e["severity"] for e in r.json()["items"]) == ["critical", "minor"]
    r = await viewer.get("/api/events", params={"kind": "status_change", "sort": "severity"})
    assert [e["severity"] for e in r.json()["items"]] == ["critical", "minor", "warning"]
    assert (await viewer.get("/api/events", params={"severity": "high"})).status_code == 422
    assert (await viewer.get("/api/events", params={"sort": "bogus"})).status_code == 422
    r = await viewer.get(f"/api/sensors/{crit['id']}/events", params={"severity": "critical"})
    assert [e["severity"] for e in r.json()["items"]] == ["critical"]
    r = await viewer.get("/api/sensors", params={"status": "down,warning", "sort": "severity"})
    assert [x["id"] for x in r.json()["items"]] == [crit["id"], minor["id"], warn["id"]]
    r = await viewer.get("/api/sensors", params={"severity": "warning,minor"})
    assert sorted(x["id"] for x in r.json()["items"]) == sorted([minor["id"], warn["id"]])
    dash = (await viewer.get("/api/dashboard")).json()
    assert dash["severity_counts"] == {
        "critical": 1,
        "major": 0,
        "minor": 1,
        "warning": 1,
        "info": 0,
        "total": 3,
    }
    assert [x["id"] for x in dash["problem_sensors"]] == [crit["id"], minor["id"], warn["id"]]
    await admin.post(f"/api/sensors/{crit['id']}/acknowledge", json={})
    dash = (await viewer.get("/api/dashboard")).json()
    assert dash["severity_counts"]["critical"] == 1
    assert dash["severity_counts_unacknowledged"]["critical"] == 0
    assert dash["recent_events"][0]["severity"] is not None


async def test_kiosk_view_counts_and_rows(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    from netmon.kiosk.view import build_view

    screen = SimpleNamespace(
        name="wall",
        language="en",
        widgets=["summary", "problems", "recent_alerts"],
        refresh_s=30,
        scope_group_id=None,
        show_addresses=False,
    )
    s = await ping(admin)
    await run(rt, clock, s["id"], DOWN)
    async with rt.sessionmaker() as session:
        off = await build_view(rt, session, screen)
    assert off["summary"]["severity_counts"] is None
    assert off["problems"][0]["severity"] is None
    await feature(admin, True)
    minor = await ping(admin, "bb", impact="none")
    await run(rt, clock, minor["id"], DOWN)
    async with rt.sessionmaker() as session:
        on = await build_view(rt, session, screen)
    assert on["summary"]["severity_counts"]["critical"] == 1
    assert on["summary"]["severity_counts"]["minor"] == 1
    assert [p["severity"] for p in on["problems"]] == ["critical", "minor"]
    assert on["recent_alerts"][0]["severity"] == "minor"


# ---------------------------------------------------------------- bulk override, permissions
async def test_bulk_severity(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, viewer: httpx.AsyncClient
) -> None:
    await feature(admin, True, audit=True)
    a, b = await ping(admin, "a"), await ping(admin, "bb", severity_warning="major")
    await run(rt, clock, a["id"], DOWN)
    ids = [a["id"], b["id"]]
    r = await admin.post(
        "/api/sensors/bulk-severity", json={"sensor_ids": ids, "severity_down": "minor"}
    )
    assert r.status_code == 200 and r.json() == {"updated": 2}
    xa, xb = [(await admin.get(f"/api/sensors/{i}")).json() for i in ids]
    assert xa["severity_down"] == "minor" and xa["severity"] == "minor"  # re-resolved
    assert xb["severity_warning"] == "major"  # omitted key kept
    r = await admin.post(
        "/api/sensors/bulk-severity",
        json={"sensor_ids": ids, "severity_down": "minor", "severity_warning": None},
    )
    assert r.json() == {"updated": 1}  # only b changed (warning override cleared)
    assert (await admin.get(f"/api/sensors/{b['id']}")).json()["severity_warning"] is None
    rows = await q(rt, "SELECT count(*) AS n FROM audit_log WHERE action = 'sensor.update'")
    assert rows[0]["n"] == 3
    bad = await admin.post("/api/sensors/bulk-severity", json={"sensor_ids": [999999]})
    assert bad.status_code == 422
    bad = await admin.post(
        "/api/sensors/bulk-severity", json={"sensor_ids": [999999], "severity_down": "info"}
    )
    assert bad.status_code == 422
    bad = await admin.post(
        "/api/sensors/bulk-severity", json={"sensor_ids": ids, "severity_down": "high"}
    )
    assert bad.status_code == 422
    # viewer: reads severity, changes nothing
    v = await viewer.get(f"/api/sensors/{a['id']}")
    assert v.status_code == 200 and v.json()["severity"] == "minor"
    for method, path, body in (
        ("post", "/api/sensors/bulk-severity", {"sensor_ids": ids, "severity_down": "info"}),
        ("put", "/api/settings/severity-mapping", {"info": "minor"}),
        ("post", "/api/settings/severity-mapping/reset", None),
        ("patch", f"/api/sensors/{a['id']}", {"severity_down": "info"}),
    ):
        kw = {"json": body} if body is not None else {}
        assert (await getattr(viewer, method)(path, **kw)).status_code == 403
    assert (await viewer.get("/api/settings/severity-mapping")).status_code == 200


# ---------------------------------------------------------------- incidents, reports, tools
async def test_incident_severity_is_the_highest_member(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    from netmon.assistant.features.correlate import incident_counts, incident_out

    await feature(admin, True)
    a, b = await ping(admin, "a"), await ping(admin, "bb", impact="none")
    await run(rt, clock, a["id"], DOWN)
    await run(rt, clock, b["id"], DOWN)
    evs = await q(
        rt,
        "SELECT id, sensor_id, device_id FROM events WHERE kind = 'status_change' ORDER BY id",
    )
    async with rt.sessionmaker() as session:
        iid = (
            await session.execute(
                text(
                    "INSERT INTO assistant_incidents (state, opened_at, last_change_at, title, "
                    "title_fields, member_count) VALUES ('open', now(), now(), 't', '{}', 2) "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        empty = (
            await session.execute(
                text(
                    "INSERT INTO assistant_incidents (state, opened_at, last_change_at, title, "
                    "title_fields, member_count) VALUES ('open', now(), now(), 'e', '{}', 0) "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        for e in evs:
            await session.execute(
                text(
                    "INSERT INTO assistant_incident_members (incident_id, event_id, sensor_id, "
                    "device_id, status, time) VALUES (:i, :e, :s, :d, 'down', now())"
                ),
                {"i": iid, "e": e["id"], "s": e["sensor_id"], "d": e["device_id"]},
            )
        await session.commit()
        counts = await incident_counts(session, [iid, empty])
        rows = (
            await session.execute(
                text("SELECT * FROM assistant_incidents WHERE id = ANY(:i) ORDER BY id"),
                {"i": [iid, empty]},
            )
        ).all()
    assert counts[iid]["severity"] == "critical"
    out = [incident_out(r, {}, counts[r.id]) for r in rows]
    assert out[0]["severity"] == "critical" and out[1]["severity"] is None


async def test_report_top_alerts_severity_column(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock, outbox: Outbox
) -> None:
    from datetime import timedelta

    from netmon.reports import data, render

    await feature(admin, True)
    ch = await channel(admin, "ops")
    await rule(admin, "all", ch)
    s = await ping(admin)
    start = clock.now() - timedelta(minutes=1)
    await run(rt, clock, s["id"], DOWN)
    end = clock.now() + timedelta(minutes=1)
    did = s["device_id"]
    async with rt.sessionmaker() as session:
        off = await data._top_alerts(session, [did], start, end)  # noqa: SLF001
        on = await data._top_alerts(session, [did], start, end, with_severity=True)  # noqa: SLF001
    assert "severity" not in off[0]
    assert on[0]["severity"] == "critical"
    base = {"sections": ["top_alerts"], "notes": [], "period_start": "x", "period_end": "y"}
    html_on = render.html_report({**base, "top_alerts": on}, "r", "en")
    html_off = render.html_report({**base, "top_alerts": off}, "r", "en")
    assert ">Severity<" in html_on and "Critical" in html_on
    assert ">Severity<" not in html_off


async def test_assistant_tools_severity(
    rt: Runtime, admin: httpx.AsyncClient, clock: FakeClock
) -> None:
    from netmon.assistant import redact, tools

    s = await ping(admin)
    m = await ping(admin, "bb", impact="none")
    await feature(admin, True)
    await run(rt, clock, s["id"], DOWN)
    await run(rt, clock, m["id"], DOWN)

    async def call(name: str, args: dict[str, Any], on: bool) -> dict[str, Any]:
        g = tools.GateInfo(features={"assistant": True, "alert_severity": on})
        res = await tools.run_tool(
            rt,
            name=name,
            raw_args=args,
            offered=list(tools.ORDER),
            user_id=1,
            username="admin",
            role="admin",
            gate=g,
            policy=redact.Policy.make(),
            now=rt.clock.now(),
            timezone="UTC",
            kb_sources={},
        )
        return res.envelope["data"]

    on = await call("list_problems", {}, True)
    assert [i["severity"] for i in on["items"]] == ["critical", "minor"]
    on = await call("list_problems", {"severities": ["minor"]}, True)
    assert [i["sensor_id"] for i in on["items"]] == [m["id"]]
    off = await call("list_problems", {"severities": ["minor"]}, False)
    assert len(off["items"]) == 2 and all("severity" not in i for i in off["items"])
    ev = await call("list_events", {"kinds": ["status_change"], "severities": ["critical"]}, True)
    assert [i["severity"] for i in ev["items"]] == ["critical"]
    ev = await call("list_events", {"kinds": ["status_change"]}, False)
    assert all("severity" not in i for i in ev["items"])


# ---------------------------------------------------------------- migration 0015
@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_0015_up_down_up(mode: str, rt: Runtime) -> None:
    import secrets

    from alembic import command

    from netmon.db import timescale
    from netmon.db.engine import make_engine
    from netmon.db.migrate import _alembic_config, run_migrations
    from tests.conftest import TEST_DATABASE_URL, db_url_for

    assert TEST_DATABASE_URL
    name = f"netmon_test_m15_{secrets.token_hex(3)}"
    conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await conn.execute(f'CREATE DATABASE "{name}"')
    await conn.close()
    url = db_url_for(name)
    engine = make_engine(url.replace("postgresql://", "postgresql+asyncpg://"))
    cols = (
        ("sensors", "severity"),
        ("sensors", "severity_down"),
        ("sensors", "severity_warning"),
        ("events", "severity"),
        ("event_rules", "severity"),
        ("alert_state", "severity"),
        ("alert_rules", "severities"),
    )

    async def present(c: asyncpg.Connection) -> set[tuple[str, str]]:
        rows = await c.fetch(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE column_name IN ('severity', 'severity_down', 'severity_warning', "
            "'severities') AND table_schema = 'public'"
        )
        return {(r["table_name"], r["column_name"]) for r in rows}

    def migrate(action: str, rev: str) -> Any:
        def go(sync_conn: Any) -> None:
            cfg = _alembic_config()
            cfg.attributes["connection"] = sync_conn
            getattr(command, action)(cfg, rev)

        return go

    try:
        await run_migrations(engine)
        await timescale.bootstrap_status(engine, 90, disabled=mode == "fallback")
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == migration_head()
            assert set(cols) <= await present(c)
            # existing rows: null / {} ; checks reject unknown levels
            gid = await c.fetchval(
                "INSERT INTO groups (name, created_at, updated_at) VALUES ('g', now(), now()) "
                "RETURNING id"
            )
            did = await c.fetchval(
                "INSERT INTO devices (group_id, name, host, created_at, updated_at) "
                "VALUES ($1, 'd', 'h', now(), now()) RETURNING id",
                gid,
            )
            sid = await c.fetchval(
                "INSERT INTO sensors (device_id, name, type, config, created_at, updated_at) "
                "VALUES ($1, 's', 'ping', '{}', now(), now()) RETURNING id",
                did,
            )
            row = await c.fetchrow(
                "SELECT severity, severity_down, severity_warning FROM sensors WHERE id = $1", sid
            )
            assert tuple(row) == (None, None, None)
            with pytest.raises(asyncpg.CheckViolationError):
                await c.execute("UPDATE sensors SET severity_down = 'high' WHERE id = $1", sid)
            await c.execute("UPDATE sensors SET severity_down = 'major' WHERE id = $1", sid)
            rid = await c.fetchval(
                "INSERT INTO alert_rules (name, on_statuses) VALUES ('r', '{down}') RETURNING id"
            )
            assert await c.fetchval("SELECT severities FROM alert_rules WHERE id = $1", rid) == []
            with pytest.raises(asyncpg.CheckViolationError):
                await c.execute("UPDATE alert_rules SET severities = '{high}' WHERE id = $1", rid)
        finally:
            await c.close()
        async with engine.connect() as ac:
            await ac.run_sync(migrate("downgrade", "0014"))
            await ac.commit()
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == "0014"
            assert await present(c) & set(cols) == set()
            assert await c.fetchval("SELECT count(*) FROM sensors") == 1  # data kept
        finally:
            await c.close()
        await run_migrations(engine)  # and up again
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == migration_head()
            assert set(cols) <= await present(c)
            assert await c.fetchval("SELECT severity_down FROM sensors") is None
        finally:
            await c.close()
    finally:
        await engine.dispose()
        conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.close()


async def test_event_sensor_follows_a_new_worst_condition(
    rt: Runtime, admin: httpx.AsyncClient
) -> None:
    await feature(admin, True)
    sid, rid = await _event_sensor_with_rule(rt, admin, "minor")
    await _condition(rt, sid, rid)
    assert await sensor_sev(admin, sid) == "minor"
    rid2 = (
        await q(
            rt,
            "INSERT INTO event_rules (name, kind, enabled, priority, scope, match, action, "
            "clear_after_min, severity) VALUES ('psu', 'syslog', true, 6, 'global', "
            "CAST('{\"text\": \"PSU\"}' AS jsonb), 'down', 60, 'critical') RETURNING id",
        )
    )[0]["id"]
    import asyncio

    box: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    rt.hub.listeners.append(box)
    try:
        await _condition(rt, sid, rid2)  # same status (down), a newer worst condition
    finally:
        rt.hub.listeners.remove(box)
    assert await sensor_sev(admin, sid) == "critical"
    assert await status_events(rt, sid) == [("down", "minor")]  # no new event, history kept
    msgs = [box.get_nowait() for _ in range(box.qsize())]
    assert {"id": sid, "device_id": msgs[-1]["data"]["device_id"], "severity": "critical"} in [
        m["data"] for m in msgs if m["type"] == "sensor.severity"
    ]


# ---------------------------------------------------------------- [severity] subject option
async def test_subject_option_api_audit_and_engine(
    rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient, clock: FakeClock
) -> None:
    from netmon.alerts import templates

    r = await viewer.get("/api/settings/severity-mapping")
    assert r.json()["severity_in_subject"] is False  # default off
    await feature(admin, True, audit=True)
    r = await admin.put("/api/settings/severity-mapping", json={"severity_in_subject": True})
    assert r.status_code == 200, r.text
    assert r.json()["severity_in_subject"] is True
    assert r.json()["mapping"] == r.json()["defaults"]  # no mapping row changed
    audit = await q(
        rt, "SELECT changes FROM audit_log WHERE action = 'settings.update' ORDER BY id DESC"
    )
    assert audit[0]["changes"]["severity_in_subject"] == [False, True]
    assert "severity_in_subject" not in (await admin.get("/api/settings")).json()
    # reset puts the mapping back and keeps the option
    r = await admin.post("/api/settings/severity-mapping/reset")
    assert r.json()["severity_in_subject"] is True
    assert (
        await admin.put("/api/settings/severity-mapping", json={"severity_in_subject": "x"})
    ).status_code == 422
    assert (
        await viewer.put("/api/settings/severity-mapping", json={"severity_in_subject": False})
    ).status_code == 403

    # the engine renders the subject with the option while sending
    seen: list[str] = []

    async def sender(channel, payload, smtp, lang):  # type: ignore[no-untyped-def]
        seen.append(templates.subject(payload, "en"))

    rt.alerts.sender = sender
    chan = await channel(admin, "subj")
    await rule(admin, "all", chan)
    s = await ping(admin)
    await run(rt, clock, s["id"], DOWN)
    assert seen == [f"[NetworkMonitor] [Critical] DOWN: dev1 / {s['name']}"]
    # option off -> the subject as before
    seen.clear()
    await admin.put("/api/settings/severity-mapping", json={"severity_in_subject": False})
    await run(rt, clock, s["id"], UP)
    assert seen == [f"[NetworkMonitor] UP: dev1 / {s['name']}"]
    # option on but the feature off -> as before (no severity in the payload)
    seen.clear()
    await admin.put("/api/settings/severity-mapping", json={"severity_in_subject": True})
    await feature(admin, False)
    await run(rt, clock, s["id"], DOWN)
    assert seen == [f"[NetworkMonitor] DOWN: dev1 / {s['name']}"]
