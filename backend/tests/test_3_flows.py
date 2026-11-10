"""[3] Flow collector integration: toggles, NetFlow v9 / v5 / IPFIX / sFlow end to end with exact
aggregates, exporter policy (auto-accept, pending, accept, ignore, delete), sampling override,
budget flood, DB errors, settings, storage modes (continuous aggregates vs fallback tables)."""

from __future__ import annotations

import asyncio
import struct
import time
from datetime import timedelta
from typing import Any

import asyncpg
import httpx
import pytest

from netmon.flows import query, storage
from netmon.runtime import Runtime
from tests import flow_packets as fp
from tests.conftest import _plain, make_device, make_settings, requires_db
from tests.ingest_helpers import send_udp, wait_for

pytestmark = requires_db

A = fp.flow_values("10.0.0.1", "10.0.0.9", 51000, 443, 6, 600, 6, 1, 2)
B = fp.flow_values("10.0.0.2", "10.0.0.9", 52000, 443, 6, 300, 3, 1, 2)
C = fp.flow_values("10.0.0.1", "8.8.8.8", 53000, 53, 17, 100, 1, 1, 3)


async def features(c: httpx.AsyncClient, **kw: bool) -> None:
    r = await c.put("/api/settings", json={f"feature_{k}_enabled": v for k, v in kw.items()})
    assert r.status_code == 200, r.text


def port(rt: Runtime, kind: str) -> int:
    return next(r.port for r in rt.flows.receivers if r.kind == kind)


def v9_packets() -> tuple[bytes, bytes]:
    tmpl = fp.v9([fp.set_(0, fp.v9_template(256, fp.V9_FIELDS))])
    data = fp.v9(
        [fp.set_(256, b"".join(fp.data_record(fp.V9_FIELDS, v) for v in (A, B, C)), pad=1)]
    )
    return tmpl, data


async def counters(c: httpx.AsyncClient) -> dict[str, Any]:
    r = await c.get("/api/flows/status")
    assert r.status_code == 200, r.text
    return r.json()["counters"]


async def received(rt: Runtime, n: int) -> None:
    async def check() -> bool:
        return rt.flows.received >= n

    await wait_for(check)


# ================================================================ toggles
async def test_flows_off_by_default_409_and_closed(rt: Runtime, admin: httpx.AsyncClient) -> None:
    for path in (
        "/api/flows/status",
        "/api/flows/exporters",
        "/api/flows/top/talkers",
        "/api/flows/top/applications",
        "/api/flows/top/conversations",
        "/api/flows/interfaces",
        "/api/flows/series",
    ):
        r = await admin.get(path)
        assert r.status_code == 409, path
        assert r.json()["error"]["code"] == "feature_disabled"
    h = (await admin.get("/api/health")).json()
    assert h["flows_rollup"] in ("cagg", "table")
    assert h["flows_rollup"] == ("cagg" if rt.timescale_enabled else "table")
    assert {h["ingest"][k] for k in ("netflow", "ipfix", "sflow")} == {"disabled"}
    assert all(not r.listening for r in rt.flows.receivers)

    await features(admin, flows=True)
    h = (await admin.get("/api/health")).json()
    assert {h["ingest"][k] for k in ("netflow", "ipfix", "sflow")} == {"listening"}
    st = (await admin.get("/api/flows/status")).json()
    assert {r["kind"] for r in st["receivers"]} == {"netflow", "ipfix", "sflow"}
    assert st["budget_per_s"] == 10000 and st["rollup"] == h["flows_rollup"]
    assert st["queue_capacity"] == 20050
    assert isinstance(st["storage_bytes"], int)
    assert rt.flows.worker is not None and rt.flows.worker.alive

    await features(admin, flows=False)
    assert all(not r.listening for r in rt.flows.receivers)
    assert rt.flows.worker is None
    assert (await admin.get("/api/flows/status")).status_code == 409


# ================================================================ NetFlow v9 end to end
async def test_v9_end_to_end_exact_aggregates(
    rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient
) -> None:
    dev = await make_device(admin, name="core", host="127.0.0.1")
    await features(admin, flows=True)
    tmpl, data = v9_packets()
    p = port(rt, "netflow")
    send_udp(p, data, "127.0.0.1")  # data before template -> no_template
    await received(rt, 1)
    await rt.flows.sync()
    assert (await counters(admin))["dropped"]["no_template"] == 1
    send_udp(p, tmpl, "127.0.0.1")
    send_udp(p, data, "127.0.0.1")
    await received(rt, 3)
    await rt.flows.sync()
    cnt = await counters(admin)
    assert cnt["records"] == 3 and cnt["stored_rows"] > 0
    assert cnt["received_datagrams"] == 3

    ex = (await admin.get("/api/flows/exporters")).json()["items"]
    assert len(ex) == 1
    e = ex[0]
    assert (e["address"], e["state"], e["device_id"], e["device_name"]) == (
        "127.0.0.1",
        "accepted",
        dev["id"],
        "core",
    )
    assert e["protocols"] == ["netflow9"] and e["sampling_seen"] == 1
    assert e["both_directions_warning"] is False and e["unsampled_warning"] is False
    eid = e["id"]

    q = {"exporter_id": eid}
    r = (await viewer.get("/api/flows/top/talkers", params={**q, "side": "src"})).json()
    assert r["source"] == "minute" and r["total_bytes"] == 1000
    assert [(i["ip"], i["bytes"]) for i in r["items"]] == [("10.0.0.1", 700), ("10.0.0.2", 300)]
    assert r["items"][0]["share"] == 0.7 and r["other_bytes"] == 0
    r = (await viewer.get("/api/flows/top/talkers", params={**q, "side": "dst"})).json()
    assert [(i["ip"], i["bytes"]) for i in r["items"]] == [("10.0.0.9", 900), ("8.8.8.8", 100)]
    r = (await viewer.get("/api/flows/top/applications", params=q)).json()
    assert [(i["key"], i["label"], i["bytes"]) for i in r["items"]] == [
        ("tcp/443", "https (tcp/443)", 900),
        ("udp/53", "dns (udp/53)", 100),
    ]
    r = (await viewer.get("/api/flows/top/conversations", params={**q, "limit": 1})).json()
    assert r["total_bytes"] == 1000 and r["other_bytes"] == 400
    (conv,) = r["items"]
    assert (conv["src_ip"], conv["dst_ip"], conv["src_port"], conv["dst_port"], conv["bytes"]) == (
        "10.0.0.1",
        "10.0.0.9",
        0,
        443,
        600,
    )
    r = (await viewer.get("/api/flows/interfaces", params=q)).json()
    assert [(i["if_index"], i["in_bytes"], i["out_bytes"], i["name"]) for i in r["items"]] == [
        (1, 1000, 0, "ifIndex 1"),
        (2, 0, 900, "ifIndex 2"),
        (3, 0, 100, "ifIndex 3"),
    ]
    # all accepted exporters (no exporter_id) and by device
    r = (await viewer.get("/api/flows/top/talkers", params={"device_id": dev["id"]})).json()
    assert r["total_bytes"] == 2000  # side both: each flow counted as source and destination
    # interface-level application query (conversation table)
    r = (
        await viewer.get(
            "/api/flows/top/applications", params={**q, "if_index": 3, "direction": "out"}
        )
    ).json()
    assert [(i["key"], i["bytes"]) for i in r["items"]] == [("udp/53", 100)]
    assert (
        await viewer.get("/api/flows/top/applications", params={"if_index": 3})
    ).status_code == 422
    # series: interface in/out in bits/s, split by application
    r = (await viewer.get("/api/flows/series", params={**q, "if_index": 1})).json()
    assert r["bucket_s"] == 60 and r["interface"]["if_index"] == 1
    ins = next(s for s in r["series"] if s["direction"] == "in")
    assert sum(v for _, v in ins["points"]) == pytest.approx(1000 * 8 / 60, rel=1e-3)
    r = (await viewer.get("/api/flows/series", params={**q, "split": "app"})).json()
    assert {s["key"] for s in r["series"]} == {"tcp/443", "udp/53"}
    r = (await viewer.get("/api/flows/series", params={**q, "split": "host"})).json()
    assert {s["key"] for s in r["series"]} == {"10.0.0.1", "10.0.0.2"}
    # range limits
    now = rt.clock.now()
    long = {"since": (now - timedelta(days=2)).isoformat(), "until": now.isoformat()}
    assert (
        await viewer.get("/api/flows/top/conversations", params={**q, **long})
    ).status_code == 422
    r = (
        await viewer.get(
            "/api/flows/top/talkers",
            params={**q, "since": (now - timedelta(days=3)).isoformat()},
        )
    ).json()
    assert r["source"] == "hour" and r["total_bytes"] == 2000
    too_long = {"since": (now - timedelta(days=32)).isoformat()}
    assert (await viewer.get("/api/flows/top/talkers", params=too_long)).status_code == 422

    # sampling override (admin) multiplies bytes and packets; viewers cannot change it
    assert (
        await viewer.patch(f"/api/flows/exporters/{eid}", json={"sampling_override": 10})
    ).status_code == 403
    r = await admin.patch(f"/api/flows/exporters/{eid}", json={"sampling_override": 10})
    assert r.status_code == 200 and r.json()["sampling_override"] == 10
    send_udp(p, data, "127.0.0.1")
    await received(rt, 4)
    await rt.flows.sync()
    r = (await viewer.get("/api/flows/top/talkers", params={**q, "side": "src"})).json()
    assert r["total_bytes"] == 1000 + 10000
    ex = (await admin.get("/api/flows/exporters")).json()["items"][0]
    assert ex["sampling_seen"] == 10


# ================================================================ sFlow / exporter policy
def sflow_packet(agent: str) -> bytes:
    hdr = fp.eth_ipv4_tcp("10.0.0.1", "10.0.0.9", 51000, 443, vlan=10)
    sample = fp.sflow_flow_sample(4096, 7, 9, [fp.sflow_raw_record(hdr, 1514)])
    return fp.sflow(agent, [fp.sflow_counter_sample(), sample])


async def test_sflow_unknown_exporter_accept_ignore_delete(
    rt: Runtime, admin: httpx.AsyncClient
) -> None:
    await features(admin, flows=True)
    p = port(rt, "sflow")
    pkt = sflow_packet("10.0.0.77")  # agent address, not the UDP source
    send_udp(p, pkt, "127.0.0.1")
    await received(rt, 1)
    await rt.flows.sync()
    cnt = await counters(admin)
    assert cnt["dropped"]["unknown_exporter"] == 1 and cnt["records"] == 0
    items = (await admin.get("/api/flows/exporters", params={"state": "pending"})).json()["items"]
    assert [(i["address"], i["device_id"]) for i in items] == [("10.0.0.77", None)]
    eid = items[0]["id"]
    async with rt.sessionmaker() as s:
        from sqlalchemy import text

        n = (await s.execute(text("SELECT count(*) FROM audit_log"))).scalar()
    r = await admin.patch(f"/api/flows/exporters/{eid}", json={"state": "accepted"})
    assert r.status_code == 200 and r.json()["state"] == "accepted"
    send_udp(p, pkt, "127.0.0.1")
    await received(rt, 2)
    await rt.flows.sync()
    r = await admin.get("/api/flows/top/talkers", params={"exporter_id": eid, "side": "src"})
    item = r.json()["items"][0]
    assert (item["ip"], item["bytes"], item["packets"]) == ("10.0.0.1", 6201344, 4096)
    r = (await admin.get("/api/flows/interfaces", params={"exporter_id": eid})).json()
    assert {(i["if_index"], i["in_bytes"], i["out_bytes"]) for i in r["items"]} == {
        (7, 6201344, 0),
        (9, 0, 6201344),
    }
    ex = (await admin.get("/api/flows/exporters")).json()["items"][0]
    assert ex["protocols"] == ["sflow5"] and ex["sampling_seen"] == 4096

    r = await admin.patch(f"/api/flows/exporters/{eid}", json={"state": "ignored"})
    assert r.json()["state"] == "ignored"
    send_udp(p, pkt, "127.0.0.1")
    await received(rt, 3)
    assert (await counters(admin))["dropped"]["ignored_exporter"] == 1
    assert (
        await admin.patch(f"/api/flows/exporters/{eid}", json={"state": "pending"})
    ).status_code == 422
    assert (
        await admin.patch(f"/api/flows/exporters/{eid}", json={"device_id": 999999})
    ).status_code == 422

    r = await admin.delete(f"/api/flows/exporters/{eid}")
    assert r.status_code == 204
    assert (await admin.delete(f"/api/flows/exporters/{eid}")).status_code == 404
    await rt.drain()
    async with rt.sessionmaker() as s:
        left = (
            await s.execute(
                text("SELECT count(*) FROM flow_host_1m WHERE exporter_id = :e"), {"e": eid}
            )
        ).scalar()
        audits = (await s.execute(text("SELECT count(*) FROM audit_log"))).scalar()
    assert left == 0
    if rt.features.enabled("audit"):
        assert audits > n


async def test_exporter_attributed_after_source_address_added(
    rt: Runtime, admin: httpx.AsyncClient
) -> None:
    await features(admin, flows=True)
    p = port(rt, "sflow")
    send_udp(p, sflow_packet("10.0.0.78"), "127.0.0.1")
    await received(rt, 1)
    await rt.drain()
    dev = await make_device(admin, name="sw", host="10.99.0.1", source_addresses=["10.0.0.78"])
    await rt.flows.rebuild_sources()

    async def accepted() -> bool:
        items = (await admin.get("/api/flows/exporters")).json()["items"]
        return bool(items) and items[0]["state"] == "accepted"

    await wait_for(accepted)
    items = (await admin.get("/api/flows/exporters")).json()["items"]
    assert items[0]["device_id"] == dev["id"]


# ================================================================ v5 / IPFIX on the IPFIX port
async def test_v5_and_ipfix_on_ipfix_port(rt: Runtime, admin: httpx.AsyncClient) -> None:
    await make_device(admin, name="fw", host="127.0.0.1")
    await features(admin, flows=True)
    p = port(rt, "ipfix")
    v5 = fp.v5(
        [
            {
                "src": "10.1.0.1",
                "dst": "10.1.0.2",
                "sport": 50000,
                "dport": 22,
                "bytes": 10,
                "packets": 1,
                "in_if": 4,
                "out_if": 5,
            }
        ],
        sampling=0x4064,
    )
    fields = [(8, 4), (12, 4), (7, 2), (11, 2), (4, 1), (82, 65535), (1, 8), (2, 8), (10, 4)]
    tmpl = fp.set_(2, fp.ipfix_template(300, fields))
    rec = (
        fp.pack_value("10.2.0.1", 4)
        + fp.pack_value("10.2.0.2", 4)
        + struct.pack(">HHB", 40000, 3389, 6)
        + b"\x04eth0"
        + struct.pack(">QQI", 77, 1, 4)
    )
    ipfix = fp.ipfix([tmpl, fp.set_(300, rec)])
    send_udp(p, v5, "127.0.0.1")
    send_udp(p, ipfix, "127.0.0.1")
    await received(rt, 2)
    await rt.flows.sync()
    r = (await admin.get("/api/flows/top/applications")).json()
    assert {(i["key"], i["bytes"]) for i in r["items"]} == {("tcp/22", 1000), ("tcp/3389", 77)}
    ex = (await admin.get("/api/flows/exporters")).json()["items"][0]
    assert ex["protocols"] == ["ipfix", "netflow5"]


# ================================================================ limits
async def test_budget_flood_over_budget_health_fast(rt: Runtime, admin: httpx.AsyncClient) -> None:
    await make_device(admin, name="core", host="127.0.0.1")
    r = await admin.put("/api/settings", json={"flow_max_records_per_s": 1000})
    assert r.status_code == 200 and r.json()["flow_max_records_per_s"] == 1000
    await features(admin, flows=True)
    p = port(rt, "netflow")
    recs = [
        {"src": f"10.3.0.{i}", "dst": "10.3.1.1", "bytes": 100, "dport": 80, "sport": 40000}
        for i in range(30)
    ]
    pkt = fp.v5(recs)
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    worst = 0.0
    try:
        for i in range(300):  # 9 000 records in well under a second
            s.sendto(pkt, ("127.0.0.1", p))
            if i % 100 == 0:
                await asyncio.sleep(0)
                t0 = time.perf_counter()
                assert (await admin.get("/api/health")).status_code == 200
                worst = max(worst, time.perf_counter() - t0)
    finally:
        s.close()
    await asyncio.sleep(0.3)
    await rt.flows.sync()
    cnt = await counters(admin)
    assert cnt["dropped"]["over_budget"] > 0
    assert cnt["records"] <= 2000 + 1000 * 2  # burst 2x plus refill during the test
    assert worst < 0.2


async def test_db_error_counted_and_recovery(
    rt: Runtime, admin: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await make_device(admin, name="core", host="127.0.0.1")
    await features(admin, flows=True)
    tmpl, data = v9_packets()
    p = port(rt, "netflow")
    calls = {"n": 0}
    real = storage.write_batch

    async def broken(*a: Any, **kw: Any) -> int:
        calls["n"] += 1
        raise RuntimeError("db down")

    monkeypatch.setattr(storage, "write_batch", broken)
    monkeypatch.setattr("netmon.flows.service.DB_RETRY_S", 0.01)
    send_udp(p, tmpl, "127.0.0.1")
    send_udp(p, data, "127.0.0.1")
    await received(rt, 2)
    await rt.flows.sync()
    assert calls["n"] == 2  # one retry
    assert (await counters(admin))["dropped"]["db_error"] == 1
    monkeypatch.setattr(storage, "write_batch", real)
    send_udp(p, data, "127.0.0.1")
    await received(rt, 3)
    await rt.flows.sync()
    r = (await admin.get("/api/flows/top/applications")).json()
    assert r["total_bytes"] == 1000


async def test_toggle_off_discards_current_minute(rt: Runtime, admin: httpx.AsyncClient) -> None:
    await make_device(admin, name="core", host="127.0.0.1")
    await features(admin, flows=True)
    tmpl, data = v9_packets()
    p = port(rt, "netflow")
    send_udp(p, tmpl, "127.0.0.1")
    send_udp(p, data, "127.0.0.1")
    await received(rt, 2)
    await asyncio.sleep(0.3)  # decoded but the minute is still open
    await features(admin, flows=False)
    await rt.drain()
    async with rt.sessionmaker() as s:
        from sqlalchemy import text

        n = (await s.execute(text("SELECT count(*) FROM flow_app_1m"))).scalar()
    assert n == 0
    send_udp(p, data, "127.0.0.1")  # socket closed: nothing received
    await asyncio.sleep(0.2)
    assert rt.flows.received == 2
    # ON again without a restart; exporters and data are kept
    await features(admin, flows=True)
    assert (await admin.get("/api/flows/exporters")).json()["items"][0]["state"] == "accepted"


# ================================================================ settings
async def test_flow_settings_round_trip(rt: Runtime, admin: httpx.AsyncClient) -> None:
    s = (await admin.get("/api/settings")).json()
    assert s["flow_max_records_per_s"] == 10000 and s["flow_top_conversations"] == 500
    assert s["flow_minute_retention_hours"] == 48 and s["flow_app_overrides"] == []
    r = await admin.put(
        "/api/settings",
        json={
            "flow_top_apps": 20,
            "flow_app_overrides": [{"protocol": 6, "port": 9999, "name": "billing"}],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["flow_top_apps"] == 20
    assert r.json()["flow_app_overrides"] == [{"protocol": 6, "port": 9999, "name": "billing"}]
    assert rt.flows.services().label(6, 9999) == "billing (tcp/9999)"
    for bad in (
        {"flow_minute_retention_hours": 10},
        {"flow_max_records_per_s": 60000},
        {"flow_app_overrides": [{"protocol": 6, "port": 70000, "name": "x"}]},
    ):
        assert (await admin.put("/api/settings", json=bad)).status_code == 422
    r = await admin.put("/api/settings", json={"flow_raw_retention_hours": 12})
    assert r.status_code == 200
    if rt.flows_rollup == "cagg":
        assert rt.flows_policies_ok is True


# ================================================================ storage modes
async def _seed_three_days(rt: Runtime, eid: int) -> dict[str, int]:
    """Minute rows over the last ~40 h (inside the minute retention) of a 3-day range."""
    now = rt.clock.now().replace(second=0, microsecond=0)
    rows = []
    expected: dict[str, int] = {}
    for h in range(40):
        t = now - timedelta(hours=h, minutes=30)
        for ip, b in (("10.5.0.1", 1000 + h), ("10.5.0.2", 500)):
            rows.append((t, eid, ip, "src", False, b, 1, 1))
            expected[ip] = expected.get(ip, 0) + b
        rows.append((t, eid, "0.0.0.0", "src", True, 7, 1, 1))
        expected["other"] = expected.get("other", 0) + 7
    async with rt.sessionmaker() as s:
        from sqlalchemy import text

        await s.execute(
            text(
                "INSERT INTO flow_host_1m (time, exporter_id, ip, direction, other, bytes, "
                "packets, flows) VALUES (:t, :e, CAST(:ip AS inet), :d, :o, :b, :p, :f)"
            ),
            [dict(zip(("t", "e", "ip", "d", "o", "b", "p", "f"), r, strict=True)) for r in rows],
        )
        await s.commit()
    return expected


async def _three_day_answer(rt: Runtime, eid: int) -> dict[str, Any]:
    if rt.flows_rollup == "table":
        await storage.rollup(rt, hours=48)
    else:
        conn = await asyncpg.connect(_plain(rt.settings.database_url))
        try:
            await conn.execute(
                "CALL refresh_continuous_aggregate('flow_host_1h', now() - interval '4 days', "
                "date_trunc('hour', now()) - interval '1 hour')"
            )
        finally:
            await conn.close()
    now = rt.clock.now()
    rng = query.make_range(now, (now - timedelta(days=3)).isoformat(), now.isoformat())
    assert rng.source == "hour"
    async with rt.sessionmaker() as s:
        out = await query.top_talkers(
            s, rng, [eid], mode=rt.flows_rollup, now=now, side="src", limit=10
        )
    return {
        "total": out["total_bytes"],
        "items": [(i["ip"], i["bytes"]) for i in out["items"]],
        "other": out["other_bytes"],
    }


async def _make_exporter(rt: Runtime) -> int:
    async with rt.sessionmaker() as s:
        from sqlalchemy import text

        eid = (
            await s.execute(
                text(
                    "INSERT INTO flow_exporters (address, state, first_seen, last_seen) VALUES "
                    "('10.5.5.5', 'accepted', now(), now()) RETURNING id"
                )
            )
        ).scalar()
        await s.commit()
    return int(eid)


async def test_three_day_range_same_in_both_modes(
    rt: Runtime, admin: httpx.AsyncClient, databases: dict[str, str], tmp_path: Any
) -> None:
    eid = await _make_exporter(rt)
    expected = await _seed_three_days(rt, eid)
    main = await _three_day_answer(rt, eid)
    want_items = sorted(
        ((k, v) for k, v in expected.items() if k != "other"), key=lambda kv: -kv[1]
    )
    assert main["items"] == want_items
    assert main["total"] == sum(expected.values()) and main["other"] == expected["other"]

    settings = make_settings(databases["fallback"], tmp_path, NETMON_DISABLE_TIMESCALE="1")
    fb = Runtime(settings)
    await fb.start(run_poller=False, maintenance=False)
    try:
        assert fb.flows_rollup == "table"
        conn = await asyncpg.connect(_plain(settings.database_url))
        try:
            await conn.execute("TRUNCATE flow_exporters, flow_host_1m, flow_host_1h")
        finally:
            await conn.close()
        fb_eid = await _make_exporter(fb)
        await _seed_three_days(fb, fb_eid)
        assert await _three_day_answer(fb, fb_eid) == main
        # retention: rows beyond the minute retention go, hourly rows stay
        out = await storage.run_retention(fb, {"flow_minute_retention_hours": 24})
        assert out["flow_rows"] > 0
        assert await _three_day_answer(fb, fb_eid) == main  # served from the hourly table
    finally:
        await fb.stop()
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        if rt.flows_rollup == "table":
            await conn.execute("TRUNCATE flow_host_1h, flow_if_1h, flow_app_1h")
        else:  # drop the materialised hours again (exporter ids restart in later tests)
            await conn.execute("DELETE FROM flow_host_1m WHERE exporter_id = $1", eid)
            await conn.execute(
                "CALL refresh_continuous_aggregate('flow_host_1h', now() - interval '4 days', "
                "date_trunc('hour', now()) - interval '1 hour')"
            )
    finally:
        await conn.close()


async def test_bind_error_state_event_and_garbage(rt: Runtime, admin: httpx.AsyncClient) -> None:
    import socket

    from sqlalchemy import text

    busy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    busy.bind(("127.0.0.1", port(rt, "ipfix")))
    try:
        await features(admin, flows=True)
        st = {r["kind"]: r for r in (await admin.get("/api/flows/status")).json()["receivers"]}
        assert st["ipfix"]["state"] == "error" and st["ipfix"]["error"]
        assert st["netflow"]["state"] == "listening"
        assert (await admin.get("/api/health")).json()["ingest"]["ipfix"] == "error"
        async with rt.sessionmaker() as s:
            msgs = [
                r[0]
                for r in await s.execute(text("SELECT message FROM events WHERE kind = 'system'"))
            ]
        assert any("IPFIX receiver cannot listen on UDP" in m for m in msgs)
    finally:
        busy.close()
    # hostile datagrams are counted, never fatal
    send_udp(port(rt, "sflow"), b"\x00\x00\x00\x04" + b"\x00" * 40, "127.0.0.1")
    send_udp(port(rt, "sflow"), b"\x00\x00\x00\x05\x00\x00\x00\x01\x0a", "127.0.0.1")
    send_udp(port(rt, "netflow"), b"\x00\x07" + b"\x00" * 30, "127.0.0.1")
    await received(rt, 3)
    await rt.flows.sync()
    d = (await counters(admin))["dropped"]
    assert d["unsupported"] >= 1 and d["parse_error"] >= 1
