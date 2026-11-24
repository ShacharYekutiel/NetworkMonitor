"""[syslog-protection] Syslog flood protection: filter rules (drop / keep newest N) with hit
counters, deduplication (repeat count), the per-source storage rate limit, the counters, top
talkers, the offered cleanup of stored rows, permissions and audit, and migration 0018 up /
down / up in both database modes."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy import text

from netmon.db.migrate import migration_head
from netmon.ingest import syslog_cleanup
from netmon.ingest.syslog_protect import FilterRule, SyslogProtection, rule_sql
from netmon.runtime import Runtime
from tests.conftest import _plain, make_device, requires_db
from tests.ingest_helpers import send_udp, src, wait_for
from tests.test_2b3_events import _status
from tests.test_2b3_ingest import count, features, ports

pytestmark = requires_db

SNMP = "<14>Nov 24 10:00:00 noisy SNMP: seqNO={i} PART=1 get 1.3.6.1.2.1.1.5.0 from 10.0.0.9"


def line(msg: str, i: int = 0) -> bytes:
    return msg.replace("{i}", str(i)).encode()


async def status(admin: httpx.AsyncClient) -> dict[str, Any]:
    return (await admin.get("/api/ingest/status")).json()


async def rows(rt: Runtime, where: str = "TRUE", **p: Any) -> list[Any]:
    async with rt.sessionmaker() as s:
        return list(
            (
                await s.execute(
                    text(f"SELECT * FROM syslog_messages WHERE {where} ORDER BY time, id"),  # noqa: S608
                    p,
                )
            ).mappings()
        )


async def audit_actions(rt: Runtime) -> list[str]:
    async with rt.sessionmaker() as s:
        return [
            r[0]
            for r in await s.execute(
                text("SELECT action FROM audit_log WHERE action LIKE 'syslog_filter.%' ORDER BY id")
            )
        ]


# ------------------------------------------------------------------ settings / defaults
async def test_defaults_on_and_validation(rt: Runtime, admin: httpx.AsyncClient) -> None:
    s = (await admin.get("/api/settings")).json()
    assert s["syslog_dedup_enabled"] is True and s["syslog_dedup_window_s"] == 60
    assert s["syslog_rate_limit_enabled"] is True and s["syslog_rate_limit_per_min"] == 3000
    for body in (
        {"syslog_dedup_window_s": 4},
        {"syslog_dedup_window_s": 3601},
        {"syslog_rate_limit_per_min": 9},
        {"syslog_rate_limit_per_min": 100001},
    ):
        assert (await admin.put("/api/settings", json=body)).status_code == 422, body
    await features(admin, syslog=True)
    st = (await status(admin))["syslog_protection"]
    assert st["dedup_enabled"] and st["rate_limit_enabled"] and st["filter_rules"] == 0
    assert (await admin.get("/api/syslog-filters")).json() == {"items": [], "cleanup": None}


# ------------------------------------------------------------------ dedup
async def test_dedup_folds_identical_messages(rt: Runtime, admin: httpx.AsyncClient) -> None:
    _, sp = ports(rt)
    await make_device(admin, name="noisy", host=src("127.0.0.81"))
    await features(admin, syslog=True)
    for _ in range(5):
        send_udp(sp, b"<12>Nov 24 10:00:00 sw fan: fan 2 failed")
    # the reported device: lines differ by a sequence number -> not identical, not folded
    for i in range(3):
        send_udp(sp, line(SNMP, i))

    async def folded() -> bool:
        r = await rows(rt, "message LIKE 'fan 2%'")
        return len(r) == 1 and r[0]["repeat_count"] == 5

    await wait_for(folded)
    one = (await admin.get("/api/syslog", params={"q": "fan 2"})).json()["items"]
    assert len(one) == 1 and one[0]["repeat_count"] == 5 and one[0]["last_seen"]
    assert await count(admin, "/api/syslog", q="seqNO") == 3
    st = (await admin.get("/api/syslog/stats", params={"q": "fan 2"})).json()
    assert st["total"] == 5 and st["rows"] == 1 and st["top_sources"][0]["count"] == 5
    c = (await status(admin))["counters"]["syslog"]
    assert c["deduplicated"] == 4 and c["stored"] == 4
    top = (await status(admin))["syslog_protection"]["top_sources"]
    assert top[0]["source_ip"] == src("127.0.0.81") and top[0]["deduplicated"] == 4
    # off: every message is a row again (the old behaviour)
    await admin.put("/api/settings", json={"syslog_dedup_enabled": False})
    for _ in range(3):
        send_udp(sp, b"<12>Nov 24 10:00:00 sw fan: fan 3 failed")
    await wait_for(lambda: has_rows(rt, "message LIKE 'fan 3%'", 3))
    assert all(r["repeat_count"] == 1 for r in await rows(rt, "message LIKE 'fan 3%'"))


async def has_rows(rt: Runtime, where: str, n: int) -> bool:
    return len(await rows(rt, where)) == n


async def test_dedup_window(rt: Runtime, admin: httpx.AsyncClient) -> None:
    """A new row once the window (measured from the first message) is over."""
    p = SyslogProtection()
    p.configure(dedup_on=True, window_s=60, rate_on=False, per_min=10)
    t0 = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)

    def rec(sec: int) -> dict[str, Any]:
        return {"source_ip": "10.0.0.1", "program": "a", "msgid": "", "severity": 6,
                "message": "same", "time": t0 + timedelta(seconds=sec),
                "device_id": None}  # fmt: skip

    first = rec(0)
    assert p.check(first) is None
    p.stored(first, 7)
    assert p.check(rec(30)) == "deduplicated"
    assert p.check(rec(59)) == "deduplicated"
    assert [(u.id, u.count) for u in p.repeat_updates()] == [(7, 3)]
    assert p.repeat_updates() == []  # clean until the next repeat
    assert p.check(rec(61)) is None  # window over: a new row
    assert p.state_size()["dedup_entries"] == 1


# ------------------------------------------------------------------ rate limit
async def test_rate_limit_drops_excess_and_counts(rt: Runtime, admin: httpx.AsyncClient) -> None:
    _, sp = ports(rt)
    await features(admin, syslog=True)
    await admin.put("/api/settings", json={"syslog_rate_limit_per_min": 10})
    # stay inside one minute of the limiter's clock
    if time.monotonic() % 60 > 50:
        await asyncio.sleep(61 - time.monotonic() % 60)
    for i in range(25):
        send_udp(sp, line("<14>Nov 24 10:00:00 sw app: storm {i}", i), "127.0.0.84")

    async def dropped() -> bool:
        c = (await status(admin))["counters"]["syslog"]
        return c["dropped"]["source_rate_limit"] == 15 and c["stored"] == 10

    await wait_for(dropped)
    assert await count(admin, "/api/syslog", q="storm") == 10
    top = (await status(admin))["syslog_protection"]["top_sources"]
    assert top[0] == {"source_ip": src("127.0.0.84"), "filter_rule": 0, "deduplicated": 0,
                      "source_rate_limit": 15, "pruned": 0, "total": 15}  # fmt: skip
    await rt.drain()
    async with rt.sessionmaker() as s:
        ev = (
            await s.execute(
                text("SELECT message FROM events WHERE message LIKE '%rows per minute%'")
            )
        ).all()
    assert len(ev) == 1 and "10 rows per minute" in ev[0][0]
    # off: nothing is limited by it
    await admin.put("/api/settings", json={"syslog_rate_limit_enabled": False})
    for i in range(15):
        send_udp(sp, line("<14>Nov 24 10:00:00 sw app: calm {i}", i), "127.0.0.84")
    await wait_for(lambda: has_api(admin, "calm", 15))


async def has_api(admin: httpx.AsyncClient, q: str, n: int) -> bool:
    return await count(admin, "/api/syslog", q=q) == n


# ------------------------------------------------------------------ filter rules
async def test_filter_rules_crud_order_permissions_audit(
    rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient
) -> None:
    await features(admin, syslog=True, audit=True)
    d = await make_device(admin, name="noisy", host="10.1.1.1")
    r = await admin.post("/api/syslog-filters", json={"name": "nothing"})
    assert r.status_code == 422  # at least one condition
    r = await admin.post("/api/syslog-filters", json={"name": "x", "sources": ["bad"]})
    assert r.status_code == 422
    r = await admin.post("/api/syslog-filters", json={"name": "x", "device_ids": [9999]})
    assert r.status_code == 422
    r = await admin.post("/api/syslog-filters", json={"program": "SNMP"})
    assert r.status_code == 422  # name required
    a = await admin.post(
        "/api/syslog-filters",
        json={"name": "SNMP GET log", "device_ids": [d["id"]], "program": "SNMP",
              "severity_min": 6},
    )  # fmt: skip
    assert a.status_code == 201, a.text
    a = a.json()
    assert a["action"] == "drop" and a["enabled"] and a["position"] == 1 and a["hits"] == 0
    assert a["devices"] == [{"id": d["id"], "name": "noisy"}]
    b = (
        await admin.post(
            "/api/syslog-filters",
            json={"name": "keep", "sources": ["10.2.0.0/16"], "action": "keep", "keep_n": 5,
                  "program": "hb", "program_match": "prefix"},
        )
    ).json()  # fmt: skip
    assert b["position"] == 2 and b["sources"] == ["10.2.0.0/16"]
    r = await admin.put("/api/syslog-filters/order", json={"ids": [b["id"], a["id"]]})
    assert [x["id"] for x in r.json()["items"]] == [b["id"], a["id"]]
    assert (
        await admin.put("/api/syslog-filters/order", json={"ids": [a["id"]]})
    ).status_code == 422
    r = await admin.patch(f"/api/syslog-filters/{a['id']}", json={"enabled": False})
    assert r.json()["enabled"] is False
    r = await admin.patch(f"/api/syslog-filters/{a['id']}", json={"severity_min": None})
    assert r.json()["severity_min"] is None
    assert (await admin.patch("/api/syslog-filters/999", json={"name": "y"})).status_code == 404
    # viewers: 403 everywhere (admin-only like the trap settings); top talkers is read-only
    for method, path in (
        ("GET", "/api/syslog-filters"),
        ("POST", "/api/syslog-filters"),
        ("PATCH", f"/api/syslog-filters/{a['id']}"),
        ("DELETE", f"/api/syslog-filters/{a['id']}"),
        ("POST", f"/api/syslog-filters/{a['id']}/cleanup-preview"),
        ("GET", "/api/syslog-filters/cleanup"),
    ):
        r = await viewer.request(method, path, json={} if method in ("POST", "PATCH") else None)
        assert r.status_code == 403, (method, path)
    assert (await viewer.get("/api/syslog/top-talkers")).status_code == 200
    assert (await admin.delete(f"/api/syslog-filters/{b['id']}")).status_code == 204
    assert [x["id"] for x in (await admin.get("/api/syslog-filters")).json()["items"]] == [a["id"]]
    assert await audit_actions(rt) == [
        "syslog_filter.create",
        "syslog_filter.create",
        "syslog_filter.reorder",
        "syslog_filter.update",
        "syslog_filter.update",
        "syslog_filter.delete",
    ]
    # feature off: 409 like the other syslog endpoints
    await features(admin, syslog=False)
    assert (await admin.get("/api/syslog-filters")).status_code == 409


async def test_drop_rule_counts_and_event_rules_still_see(
    rt: Runtime, admin: httpx.AsyncClient
) -> None:
    _, sp = ports(rt)
    d = await make_device(admin, name="noisy", host=src("127.0.0.81"))
    await features(admin, syslog=True, event_alerts=True)
    ev = await admin.post(
        "/api/event-rules",
        json={"name": "snmp auth", "kind": "syslog", "scope": "global", "action": "down",
              "match": {"text": "authFail"}, "clear_after_min": 60},
    )  # fmt: skip
    assert ev.status_code == 201, ev.text
    rule = (
        await admin.post(
            "/api/syslog-filters",
            json={"name": "SNMP info", "device_ids": [d["id"]], "program": "snmp",
                  "severity_min": 6},
        )
    ).json()  # fmt: skip
    for i in range(6):
        send_udp(sp, line(SNMP, i))
    send_udp(sp, b"<14>Nov 24 10:00:00 noisy SNMP: authFail from 10.9.9.9")  # dropped, but...
    send_udp(sp, b"<11>Nov 24 10:00:00 noisy SNMP: agent restarted")  # err: not matched
    send_udp(sp, b"<14>Nov 24 10:00:00 noisy sshd: login ok")  # other program: kept

    async def counted() -> bool:
        c = (await status(admin))["counters"]["syslog"]
        return c["dropped"]["filter_rule"] == 7 and c["stored"] == 2

    await wait_for(counted)
    assert {i["message"] for i in (await admin.get("/api/syslog")).json()["items"]} == {
        "agent restarted",
        "login ok",
    }
    st = (await status(admin))["syslog_protection"]
    assert st["rule_hits"] == {str(rule["id"]): 7} and st["filter_rules"] == 1
    # ...the event rule saw the dropped message: the syslog event sensor is down
    s = await wait_for(lambda: _status(admin, d["id"], "syslog", "down"), timeout=15)
    assert "authFail" in s["status_message"]
    # hit counter persisted (flushed every 10 s, or at the pipeline stop)
    await rt.ingest.flush_protection(hits=True)
    got = (await admin.get("/api/syslog-filters")).json()["items"][0]
    assert got["hits"] == 7 and got["last_hit_at"]
    # changing the match resets the counter
    got = (await admin.patch(f"/api/syslog-filters/{rule['id']}", json={"severity_min": 7})).json()
    assert got["hits"] == 0
    # disabled: stored again
    await admin.patch(f"/api/syslog-filters/{rule['id']}", json={"enabled": False})
    send_udp(sp, line(SNMP, 99))
    await wait_for(lambda: has_api(admin, "seqNO=99", 1))


async def test_keep_newest_n(rt: Runtime, admin: httpx.AsyncClient) -> None:
    _, sp = ports(rt)
    await features(admin, syslog=True)
    rule = (
        await admin.post(
            "/api/syslog-filters",
            json={"name": "keep 3", "program": "SNMP", "action": "keep", "keep_n": 3},
        )
    ).json()
    for i in range(8):
        send_udp(sp, line(SNMP, i), "127.0.0.81")
    send_udp(sp, line(SNMP, 100), "127.0.0.82")  # its own window

    async def kept() -> bool:
        c = (await status(admin))["counters"]["syslog"]
        return c["stored"] == 9 and await count(admin, "/api/syslog", q="seqNO") == 4

    await wait_for(kept)
    r = await rows(rt, "source_ip = CAST(:s AS inet)", s=src("127.0.0.81"))
    assert [x["message"].split()[0] for x in r] == ["seqNO=5", "seqNO=6", "seqNO=7"]
    assert {x["filter_rule_id"] for x in r} == {rule["id"]}
    c = (await status(admin))["counters"]["syslog"]
    assert c["pruned"] == 5
    top = (await status(admin))["syslog_protection"]["top_sources"]
    assert top[0]["pruned"] == 5


# ------------------------------------------------------------------ top talkers
async def test_top_talkers(rt: Runtime, admin: httpx.AsyncClient) -> None:
    _, sp = ports(rt)
    d = await make_device(admin, name="noisy", host=src("127.0.0.81"))
    await features(admin, syslog=True)
    for i in range(12):
        send_udp(sp, line(SNMP, i))
    send_udp(sp, b"<11>Nov 24 10:00:00 sw sshd: one", "127.0.0.83")
    await wait_for(lambda: total_rows(admin, 13))
    t = (await admin.get("/api/syslog/top-talkers")).json()
    assert t["total_rows"] == 13 and t["hours"] == 24
    assert t["items"][0] == {
        "source_ip": src("127.0.0.81"), "device_id": d["id"], "device_name": "noisy",
        "program": "SNMP", "severity": 6, "rows": 12, "messages": 12,
        "last_seen": t["items"][0]["last_seen"],
    }  # fmt: skip
    assert t["items"][1]["source_ip"] == src("127.0.0.83") and t["items"][1]["device_id"] is None
    assert (await admin.get("/api/syslog/top-talkers", params={"hours": 0})).status_code == 422


async def total_rows(admin: httpx.AsyncClient, n: int) -> bool:
    return (await admin.get("/api/syslog/top-talkers")).json()["total_rows"] == n


# ------------------------------------------------------------------ cleanup of stored rows
async def _seed(rt: Runtime, n_snmp: int, n_other: int, sources: list[str]) -> None:
    now = datetime.now(UTC)
    async with rt.sessionmaker() as s:
        for ip in sources:
            await s.execute(
                text(
                    "INSERT INTO syslog_messages (time, source_ip, transport, format, facility, "
                    "severity, program, message) SELECT now() - make_interval(mins => g), "
                    "CAST(:ip AS inet), 'udp', 'rfc3164', 1, 6, 'SNMP', 'seqNO=' || g "
                    "FROM generate_series(1, :n) g"
                ),
                {"ip": ip, "n": n_snmp},
            )
            await s.execute(
                text(
                    "INSERT INTO syslog_messages (time, source_ip, transport, format, facility, "
                    "severity, program, message) "
                    "SELECT CAST(:t AS timestamptz) - make_interval(mins => g), "
                    "CAST(:ip AS inet), 'udp', 'rfc3164', 1, 3, 'SNMP', 'error ' || g "
                    "FROM generate_series(1, :n) g"
                ),
                {"ip": ip, "n": n_other, "t": now},
            )
        await s.commit()


async def _wait_job(admin: httpx.AsyncClient) -> dict[str, Any]:
    async def done() -> Any:
        j = (await admin.get("/api/syslog-filters/cleanup")).json()["cleanup"]
        return j if j and j["state"] != "running" else None

    return await wait_for(done, timeout=30)


async def test_cleanup_drop_rule(rt: Runtime, admin: httpx.AsyncClient) -> None:
    await features(admin, syslog=True, audit=True)
    # rows spread over ~2 days: many time slices, several batches
    await _seed(rt, 3000, 20, ["10.1.1.1", "10.1.1.2"])
    syslog_cleanup.BATCH, old_batch = 700, syslog_cleanup.BATCH
    try:
        rule = (
            await admin.post(
                "/api/syslog-filters",
                json={"name": "SNMP", "sources": ["10.1.1.1/32"], "program": "snmp",
                      "severity_min": 6},
            )
        ).json()  # fmt: skip
        pv = await admin.post(f"/api/syslog-filters/{rule['id']}/cleanup-preview")
        assert pv.status_code == 200, pv.text
        pv = pv.json()
        assert pv["count"] == 3000 and pv["action"] == "drop" and not pv["running"]
        r = await admin.post(
            f"/api/syslog-filters/{rule['id']}/cleanup",
            json={"until": pv["until"], "expected": pv["count"]},
        )
        assert r.status_code == 202, r.text
        job = await _wait_job(admin)
    finally:
        syslog_cleanup.BATCH = old_batch
    assert job["state"] == "done" and job["deleted"] == 3000 and job["expected"] == 3000
    left = await rows(rt)
    assert len(left) == 3000 + 40  # the other source and the errors stay
    assert not [x for x in left if str(x["source_ip"]) == "10.1.1.1" and x["severity"] == 6]
    assert (await audit_actions(rt))[-1] == "syslog_filter.cleanup"
    bad = await admin.post(
        f"/api/syslog-filters/{rule['id']}/cleanup",
        json={"until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(), "expected": 0},
    )
    assert bad.status_code == 422


async def test_cleanup_keep_rule_prunes_per_source_then_ingest_keeps_n(
    rt: Runtime, admin: httpx.AsyncClient
) -> None:
    _, sp = ports(rt)
    await features(admin, syslog=True)
    a, b = src("127.0.0.81"), src("127.0.0.82")
    await _seed(rt, 40, 0, [a, b])
    rule = (
        await admin.post(
            "/api/syslog-filters",
            json={"name": "keep", "program": "SNMP", "action": "keep", "keep_n": 10},
        )
    ).json()
    pv = (await admin.post(f"/api/syslog-filters/{rule['id']}/cleanup-preview")).json()
    assert pv["count"] == 60 and pv["keep_n"] == 10
    await admin.post(
        f"/api/syslog-filters/{rule['id']}/cleanup", json={"until": pv["until"], "expected": 60}
    )
    job = await _wait_job(admin)
    assert job["deleted"] == 60, (job["state"], job["error"])
    for ip in (a, b):
        r = await rows(rt, "source_ip = CAST(:s AS inet)", s=ip)
        # the newest 10 (seqNO=1..10: minutes ago) stay, tagged with the rule
        assert sorted(int(x["message"][6:]) for x in r) == list(range(1, 11))
        assert {x["filter_rule_id"] for x in r} == {rule["id"]}
    # new arrivals keep the window at 10 per source
    for i in range(3):
        send_udp(sp, line(SNMP, 500 + i), "127.0.0.81")

    async def window() -> bool:
        r = await rows(rt, "source_ip = CAST(:s AS inet)", s=a)
        new = [x for x in r if x["message"].startswith("seqNO=50")]
        return len(r) == 10 and len(new) == 3

    await wait_for(window)


# ------------------------------------------------------------------ units / performance
def test_rule_matching_and_sql_agree() -> None:
    row = {"id": 1, "name": "r", "enabled": True, "sources": ["10.0.0.0/8"], "device_ids": [7],
           "program": "SNMP", "program_match": "prefix", "msgid": "", "severity_min": 6,
           "message_contains": "PART=", "action": "drop", "keep_n": 100}  # fmt: skip
    r = FilterRule.from_row(row)

    def rec(**kw: Any) -> dict[str, Any]:
        base = {"source_ip": "10.1.2.3", "device_id": None, "program": "snmpd", "msgid": "",
                "severity": 6, "message": "seqNO=1 part=2"}  # fmt: skip
        return {**base, **kw}

    assert r.matches(rec(), [None])
    assert r.matches(rec(source_ip="192.168.1.1", device_id=7), [None])  # device OR source
    assert not r.matches(rec(source_ip="192.168.1.1"), [None])
    assert not r.matches(rec(severity=5), [None])  # "info and less important" only
    assert not r.matches(rec(program="xsnmp"), [None])
    assert not r.matches(rec(message="seqNO=1"), [None])
    where, p = rule_sql(row)
    assert "<<= ANY" in where and "device_id = ANY" in where and " OR " in where
    assert p["f_prog"] == "snmp%" and p["f_msg"] == "%PART=%" and p["f_sev"] == 6
    exact = FilterRule.from_row({**row, "program_match": "exact", "sources": [], "device_ids": []})
    assert not exact.matches(rec(), [None]) and exact.matches(rec(program="SNMP"), [None])


def test_burst_of_50k_messages_is_cheap_and_bounded() -> None:
    """Micro-benchmark of the hot path: 50,000 messages (10 filter rules that do not match,
    dedup, rate limit) from 5,000 sources; memory stays within the caps."""
    p = SyslogProtection(dedup_max=20000, tracked=4096)
    p.configure(dedup_on=True, window_s=60, rate_on=True, per_min=3000)
    p.set_rules(
        [
            {"id": i, "name": f"r{i}", "enabled": True, "sources": [f"192.168.{i}.0/24"],
             "device_ids": [], "program": f"prog{i}", "program_match": "exact", "msgid": "",
             "severity_min": None, "message_contains": "", "action": "drop", "keep_n": 100}
            for i in range(10)
        ]
    )  # fmt: skip
    t0 = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    recs = [
        {"source_ip": f"10.{(i % 5000) // 250}.{i % 250}.1", "device_id": None,
         "program": "SNMP", "msgid": "", "severity": 6,
         "message": f"seqNO={i} PART=1 get 1.3.6.1.2.1.1.5.0" if i % 3 else "same line",
         "time": t0 + timedelta(milliseconds=i)}
        for i in range(50000)
    ]  # fmt: skip
    start = time.perf_counter()
    out: dict[str | None, int] = {}
    for i, r in enumerate(recs):
        v = p.check(r, mono=1000.0)
        out[v] = out.get(v, 0) + 1
        if v is None:
            p.stored(r, i)
    elapsed = time.perf_counter() - start
    assert elapsed < 3.0, f"{elapsed:.2f} s for 50k messages"  # ~0.4 s on the dev box
    # "same line" per source, while its entry is among the newest 20,000 (FIFO cap)
    assert out.get("deduplicated", 0) > 6000
    assert p.state_size()["dedup_entries"] <= 20000
    assert p.state_size()["rate_sources"] <= 4096 and len(p.sources) <= 4096
    print(f"\n50k syslog messages through the protection: {elapsed * 1000:.0f} ms")  # noqa: T201


def test_rate_limit_per_minute_window() -> None:
    p = SyslogProtection()
    p.configure(dedup_on=False, window_s=60, rate_on=True, per_min=3)
    t = datetime(2026, 11, 24, tzinfo=UTC)
    r = {"source_ip": "10.0.0.1", "device_id": None, "program": "", "msgid": "", "severity": 6,
         "message": "m", "time": t}  # fmt: skip
    assert [p.check(dict(r), mono=60.0) for _ in range(4)] == [
        None,
        None,
        None,
        "source_rate_limit",
    ]
    assert p.check(dict(r, source_ip="10.0.0.2"), mono=60.5) is None  # per source
    assert p.check(dict(r), mono=120.0) is None  # next minute


# ------------------------------------------------------------------ migration 0018
async def _migration_db(mode: str) -> tuple[str, str]:
    import secrets

    from tests.conftest import TEST_DATABASE_URL, db_url_for

    assert TEST_DATABASE_URL
    name = f"netmon_test_m18_{secrets.token_hex(3)}"
    conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await conn.execute(f'CREATE DATABASE "{name}"')
    await conn.close()
    return name, db_url_for(name)


@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_0018_up_down_up(mode: str, rt: Runtime) -> None:
    from alembic import command

    from netmon.db import timescale
    from netmon.db.engine import make_engine
    from netmon.db.migrate import _alembic_config, run_migrations
    from tests.conftest import TEST_DATABASE_URL

    name, url = await _migration_db(mode)
    engine = make_engine(url.replace("postgresql://", "postgresql+asyncpg://"))
    cols = {"repeat_count", "last_seen", "filter_rule_id"}

    async def present(c: asyncpg.Connection) -> set[str]:
        got = await c.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'syslog_messages'"
        )
        return {r["column_name"] for r in got} & cols

    def migrate(action: str, rev: str) -> Any:
        def go(sync_conn: Any) -> None:
            cfg = _alembic_config()
            cfg.attributes["connection"] = sync_conn
            getattr(command, action)(cfg, rev)

        return go

    try:
        # an existing install at 0016 with stored syslog, then the upgrade
        async with engine.connect() as ac:
            await ac.run_sync(migrate("upgrade", "0016"))
            await ac.commit()
        enabled = await timescale.bootstrap(engine, 90, disabled=mode == "fallback")
        await timescale.bootstrap_ingest(engine, enabled, 30, 30)
        c = await asyncpg.connect(url)
        try:
            hyper = await c.fetchval(
                "SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'"
            ) and await c.fetchval(
                "SELECT count(*) FROM timescaledb_information.hypertables "
                "WHERE hypertable_name = 'syslog_messages'"
            )
            assert bool(hyper) == (mode == "timescale")
            await c.execute(
                "INSERT INTO syslog_messages (time, source_ip, transport, format, facility, "
                "severity, message) VALUES (now() - interval '3 days', '10.0.0.1', 'udp', "
                "'raw', 1, 6, 'old')"
            )
        finally:
            await c.close()
        await run_migrations(engine)
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == migration_head()
            assert await present(c) == cols
            row = await c.fetchrow("SELECT repeat_count, last_seen, filter_rule_id, id, time "
                                   "FROM syslog_messages")  # fmt: skip
            assert tuple(row)[:3] == (1, None, None)
            await c.execute(
                "UPDATE syslog_messages SET repeat_count = 37, last_seen = now() "
                "WHERE id = $1 AND time = $2",
                row["id"],
                row["time"],
            )
            assert await c.fetchval("SELECT repeat_count FROM syslog_messages") == 37
            fid = await c.fetchval(
                "INSERT INTO syslog_filters (name, program) VALUES ('f', 'SNMP') RETURNING id"
            )
            assert (
                await c.fetchval(
                    "SELECT action || keep_n || hits FROM syslog_filters WHERE id = $1", fid
                )
                == "drop1000"
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await c.execute("UPDATE syslog_filters SET action = 'mute'")
            assert (
                await c.fetchval(
                    "SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_syslog_filter_rule'"
                )
                == 1
            )
        finally:
            await c.close()
        async with engine.connect() as ac:
            await ac.run_sync(migrate("downgrade", "0016"))
            await ac.commit()
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == "0016"
            assert await present(c) == set()
            assert await c.fetchval("SELECT to_regclass('syslog_filters')") is None
            assert await c.fetchval("SELECT count(*) FROM syslog_messages") == 1  # data kept
        finally:
            await c.close()
        await run_migrations(engine)  # and up again
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == migration_head()
            assert await present(c) == cols
            assert await c.fetchval("SELECT repeat_count FROM syslog_messages") == 1
        finally:
            await c.close()
    finally:
        await engine.dispose()
        conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.close()


async def test_compressed_syslog_chunk_takes_the_columns_and_updates(rt: Runtime) -> None:
    """The app never compresses `syslog_messages` (netmon.db.timescale), but an operator might:
    ADD COLUMN with a constant default, the dedup UPDATE and the cleanup DELETE must still work
    on a compressed chunk (TimescaleDB >= 2.11 DML on compressed chunks)."""
    from alembic import command

    from netmon.db import timescale
    from netmon.db.engine import make_engine
    from netmon.db.migrate import _alembic_config, run_migrations
    from tests.conftest import TEST_DATABASE_URL

    name, url = await _migration_db("timescale")
    engine = make_engine(url.replace("postgresql://", "postgresql+asyncpg://"))

    def migrate(action: str, rev: str) -> Any:
        def go(sync_conn: Any) -> None:
            cfg = _alembic_config()
            cfg.attributes["connection"] = sync_conn
            getattr(command, action)(cfg, rev)

        return go

    try:
        async with engine.connect() as ac:
            await ac.run_sync(migrate("upgrade", "0016"))
            await ac.commit()
        if not await timescale.bootstrap(engine, 90):
            pytest.skip("TimescaleDB not available")
        await timescale.bootstrap_ingest(engine, True, 30, 30)
        c = await asyncpg.connect(url)
        try:
            await c.execute(
                "INSERT INTO syslog_messages (time, source_ip, transport, format, facility, "
                "severity, program, message) SELECT now() - interval '5 days' + "
                "make_interval(secs => g), '10.0.0.1', 'udp', 'raw', 1, 6, 'SNMP', 'm' || g "
                "FROM generate_series(1, 50) g"
            )
            await c.execute(
                "ALTER TABLE syslog_messages SET (timescaledb.compress, "
                "timescaledb.compress_segmentby = 'source_ip')"
            )
            await c.execute(
                "SELECT compress_chunk(ch) FROM show_chunks('syslog_messages', "
                "older_than => interval '1 day') ch"
            )
        finally:
            await c.close()
        await run_migrations(engine)  # ADD COLUMN ... DEFAULT on a compressed hypertable
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT sum(repeat_count) FROM syslog_messages") == 50
            row = await c.fetchrow("SELECT id, time FROM syslog_messages ORDER BY id LIMIT 1")
            await c.execute(
                "UPDATE syslog_messages SET repeat_count = 9 WHERE id = $1 AND time = $2",
                row["id"],
                row["time"],
            )
            assert await c.fetchval("SELECT sum(repeat_count) FROM syslog_messages") == 58
            await c.execute("DELETE FROM syslog_messages WHERE message LIKE 'm1%'")
            assert await c.fetchval("SELECT count(*) FROM syslog_messages") == 50 - 11
        finally:
            await c.close()
    finally:
        await engine.dispose()
        conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.close()
