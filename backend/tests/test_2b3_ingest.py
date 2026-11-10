"""[2b-3] Feature toggles, settings, trap / syslog receivers end to end, unknown sources, flood
protection, SIEM forwarding, live WS batches, health."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from typing import Any

import httpx
import pytest

from netmon.runtime import Runtime
from netmon.services.features import FEATURES
from tests.conftest import make_device, requires_db
from tests.ingest_helpers import (
    LOOPBACK_ALIASES,
    send_tcp,
    send_udp,
    src,
    v1_trap,
    v2c_trap,
    v3_trap,
    wait_for,
)

pytestmark = requires_db

LINK_DOWN = "1.3.6.1.6.3.1.1.5.3"
IFINDEX = "1.3.6.1.2.1.2.2.1.1"


async def features(c: httpx.AsyncClient, **kw: bool) -> dict[str, Any]:
    r = await c.put("/api/settings", json={f"feature_{k}_enabled": v for k, v in kw.items()})
    assert r.status_code == 200, r.text
    return r.json()


def ports(rt: Runtime) -> tuple[int, int]:
    return rt.ingest.trap_rx.port, rt.ingest.syslog_udp.port


async def count(c: httpx.AsyncClient, path: str, **params: Any) -> int:
    r = await c.get(path, params={"limit": 500, **params})
    assert r.status_code == 200, r.text
    return len(r.json()["items"])


# ------------------------------------------------------------------ toggles
async def test_features_off_by_default(
    rt: Runtime, admin: httpx.AsyncClient, viewer: httpx.AsyncClient
) -> None:
    pub = (await admin.get("/api/settings/public")).json()
    assert pub["features"] == dict.fromkeys(FEATURES, False)
    s = (await admin.get("/api/settings")).json()
    assert all(s[f"feature_{n}_enabled"] is False for n in FEATURES)
    for path in (
        "/api/traps",
        "/api/traps/stats",
        "/api/syslog",
        "/api/syslog/1",
        "/api/ingest/status",
        "/api/ingest/unknown-sources",
        "/api/event-rules",
    ):
        r = await admin.get(path)
        assert r.status_code == 409, path
        assert r.json()["error"]["code"] == "feature_disabled"
    r = await admin.get("/api/traps")
    assert r.json()["error"]["details"] == [{"feature": "traps"}]
    r = await admin.post("/api/ingest/forwarders/test", json={"target_id": "abcdef"})
    assert r.status_code == 409
    # role before feature state: a viewer gets 403, not 409 (E2E 49)
    r = await viewer.post("/api/ingest/forwarders/test", json={"target_id": "abcdef"})
    assert r.status_code == 403
    h = (await admin.get("/api/health")).json()
    # [3] `ingest` also carries the flow receivers (netflow / ipfix / sflow)
    assert {k: h["ingest"][k] for k in ("trap", "syslog_udp", "syslog_tcp")} == {
        "trap": "disabled",
        "syslog_udp": "disabled",
        "syslog_tcp": "disabled",
    }
    assert isinstance(h["trgm"], bool)
    d = await make_device(admin)
    r = await admin.get(f"/api/devices/{d['id']}/timeline")
    assert r.status_code == 409 and r.json()["error"]["details"] == [{"feature": "timeline"}]


async def test_toggle_runtime_and_ws_broadcast(rt: Runtime, admin: httpx.AsyncClient) -> None:
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    rt.hub.listeners.append(q)
    tp, sp = ports(rt)
    await features(admin, syslog=True)
    msg = await asyncio.wait_for(q.get(), 5)
    while msg["type"] != "features.changed":
        msg = await asyncio.wait_for(q.get(), 5)
    assert msg["data"]["features"]["syslog"] is True
    assert rt.ingest.receiver_states() == {
        "trap": "disabled",
        "syslog_udp": "listening",
        "syslog_tcp": "listening",
    }
    assert (await admin.get("/api/settings/public")).json()["features"]["syslog"] is True
    assert (await admin.get("/api/ingest/status")).status_code == 200
    await features(admin, syslog=False)
    assert rt.ingest.receiver_states()["syslog_tcp"] == "disabled"
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", sp), timeout=1).close()
    await features(admin, syslog=True)  # ON again without restart
    socket.create_connection(("127.0.0.1", sp), timeout=1).close()
    await features(admin, syslog=False)


async def test_settings_keys(rt: Runtime, admin: httpx.AsyncClient) -> None:
    s = (await admin.get("/api/settings")).json()
    assert s["trap_retention_days"] == 30 and s["syslog_retention_days"] == 30
    assert s["trap_community_check"] == "any" and s["trap_communities"] is None
    assert s["trap_communities_count"] == 0 and s["ingest_unknown_sources"] == "store"
    assert (s["flap_threshold"], s["flap_window_min"], s["flap_quiet_min"]) == (5, 10, 15)
    assert s["event_linkdown_scope"] == "important" and s["siem_targets"] == []
    for bad in (
        {"event_linkdown_scope": "some"},
        {"ingest_ignored_sources": ["not-an-ip"]},
        {"flap_threshold": 2},
        {"trap_communities": [""]},
        {"siem_targets": [{"name": "x", "host": "h", "port": 0}]},
    ):
        r = await admin.put("/api/settings", json=bad)
        assert r.status_code == 422, bad
    r = await admin.put(
        "/api/settings",
        json={
            "event_linkdown_scope": "all",
            "trap_communities": ["c1", "c2"],
            "ingest_ignored_sources": ["10.9.0.0/16", "10.1.1.1"],
            "trap_v3_users": [
                {"username": "u1", "security_level": "authPriv", "auth_protocol": "SHA",
                 "auth_key": "authpass123", "priv_protocol": "AES128", "priv_key": "privpass123"}
            ],
            "siem_targets": [{"name": "QRadar", "host": "siem.example", "port": 6514,
                              "protocol": "tls"}],
        },
    )  # fmt: skip
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["event_linkdown_scope"] == "all"
    assert s["trap_communities"] is None and s["trap_communities_count"] == 2
    assert s["ingest_ignored_sources"] == ["10.9.0.0/16", "10.1.1.1/32"]
    u = s["trap_v3_users"][0]
    assert u["auth_key"] is None and u["auth_key_set"] and u["priv_key_set"]
    tid = s["siem_targets"][0]["id"]
    assert len(tid) == 6
    # keys kept when omitted for the same user name; "" clears
    r = await admin.put(
        "/api/settings",
        json={
            "trap_v3_users": [{"username": "u1", "security_level": "authPriv", "priv_key": ""}],
            "siem_targets": [{**s["siem_targets"][0], "port": 6515}],
        },
    )
    u = r.json()["trap_v3_users"][0]
    assert u["auth_key_set"] is True and u["priv_key_set"] is False
    assert r.json()["siem_targets"][0]["id"] == tid
    assert (await admin.put("/api/settings", json={"trap_communities": None})).json()[
        "trap_communities_count"
    ] == 2


# ------------------------------------------------------------------ syslog
async def test_syslog_end_to_end(rt: Runtime, admin: httpx.AsyncClient) -> None:
    _, sp = ports(rt)
    d = await make_device(admin, name="sw81", host=src("127.0.0.81"))
    await features(admin, syslog=True)
    send_udp(sp, b"<30>Nov  8 09:05:01 gw-hq sshd[2211]: Accepted password for admin")
    send_udp(sp, b"<187>Nov  8 09:05:02 gw-hq app: disk failure")
    s = send_tcp(sp, b"27 <13>1 - h a - - - octet one<13>lf one\n<13>lf two\r\n")
    try:
        await wait_for(lambda: has(admin, "/api/syslog", 5))
    finally:
        s.close()

    async def total() -> int:
        return await count(admin, "/api/syslog")

    assert await total() == 5
    items = (await admin.get("/api/syslog")).json()["items"]
    assert all(i["device_id"] == d["id"] and i["device_name"] == "sw81" for i in items)
    assert {i["transport"] for i in items} == {"udp", "tcp"}
    assert items[0]["time"] >= items[-1]["time"]
    assert await count(admin, "/api/syslog", severity_max=3) == 1
    assert await count(admin, "/api/syslog", program="SSHD") == 1
    assert await count(admin, "/api/syslog", q="FAIL") == 1
    assert await count(admin, "/api/syslog", q="100%_") == 0
    assert await count(admin, "/api/syslog", unknown=True) == 0
    assert await count(admin, "/api/syslog", device_id=d["id"]) == 5
    assert await count(admin, "/api/syslog", facility=[1]) == 3
    page = (await admin.get("/api/syslog", params={"limit": 2})).json()
    assert len(page["items"]) == 2 and page["next_cursor"] and page["limit"] == 2
    page2 = (
        await admin.get("/api/syslog", params={"limit": 2, "cursor": page["next_cursor"]})
    ).json()
    assert {i["id"] for i in page2["items"]}.isdisjoint({i["id"] for i in page["items"]})
    r = await admin.get("/api/syslog", params={"cursor": "zzz"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_cursor"
    r = await admin.get("/api/syslog", params={"since": "2026-01-01T00:00:00Z"})
    assert r.status_code == 422  # > 31 days
    one = (await admin.get(f"/api/syslog/{items[0]['id']}")).json()
    assert one["id"] == items[0]["id"]
    assert (await admin.get("/api/syslog/999999")).status_code == 404
    st = (await admin.get("/api/syslog/stats")).json()
    assert st["total"] == 5 and sum(st["by_severity"]) == 5 and st["by_severity"][3] == 1
    assert st["bucket_s"] == 900 and len(st["buckets"]) >= 90
    assert st["top_sources"][0]["device_id"] == d["id"]
    status = (await admin.get("/api/ingest/status")).json()
    assert status["counters"]["syslog"]["stored"] == 5
    assert status["counters"]["syslog"]["received"] >= 5
    # TCP frame > 64 KiB closes the connection (too_large)
    s = send_tcp(sp, b"x" * 70000)
    with contextlib.suppress(OSError):
        s.settimeout(3)
        assert s.recv(10) == b""
    s.close()
    await wait_for(lambda: _dropped(admin, "syslog", "too_large"))
    await features(admin, syslog=False)
    assert (await admin.get("/api/syslog")).status_code == 409
    await features(admin, syslog=True)
    assert await total() == 5  # data kept


async def has(c: httpx.AsyncClient, path: str, n: int, **params: Any) -> bool:
    return await count(c, path, **params) == n


async def dropped_at_least(c: httpx.AsyncClient, kind: str, reason: str, n: int) -> bool:
    return await _dropped(c, kind, reason) >= n


async def _dropped(c: httpx.AsyncClient, kind: str, reason: str) -> int:
    r = await c.get("/api/ingest/status")
    return int(r.json()["counters"][kind]["dropped"][reason]) if r.status_code == 200 else 0


async def test_unknown_sources_assign_and_policies(rt: Runtime, admin: httpx.AsyncClient) -> None:
    if not LOOPBACK_ALIASES:
        pytest.skip("needs 127.0.0.x source addresses")
    _, sp = ports(rt)
    d = await make_device(admin, name="target", host="10.77.0.1")
    await features(admin, syslog=True)
    send_udp(sp, b"<13>Nov  8 12:00:00 strange app: one", "127.0.0.99")
    await wait_for(lambda: count(admin, "/api/syslog", unknown=True))
    us = (await admin.get("/api/ingest/unknown-sources")).json()["items"]
    assert us[0]["source_ip"] == "127.0.0.99" and us[0]["syslog_count"] == 1
    assert us[0]["last_hostname"] == "strange" and us[0]["ignored"] is False
    r = await admin.post(
        "/api/ingest/unknown-sources/assign", json={"source_ip": "127.0.0.99", "device_id": d["id"]}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "device_id": d["id"],
        "source_addresses": ["127.0.0.99"],
        "reassigning": True,
    }
    r = await admin.post(
        "/api/ingest/unknown-sources/assign", json={"source_ip": "127.0.0.99", "device_id": d["id"]}
    )
    assert r.status_code == 422
    r = await admin.post(
        "/api/ingest/unknown-sources/assign", json={"source_ip": "127.0.0.98", "device_id": 9999}
    )
    assert r.status_code == 404
    await rt.drain()
    assert await count(admin, "/api/syslog", device_id=d["id"]) == 1  # re-attributed
    send_udp(sp, b"<13>Nov  8 12:00:00 strange app: two", "127.0.0.99")
    await wait_for(lambda: has(admin, "/api/syslog", 2, device_id=d["id"]))
    dev = (await admin.get(f"/api/devices/{d['id']}")).json()
    assert dev["source_addresses"] == ["127.0.0.99"]
    # policy drop + ignore list
    await admin.put(
        "/api/settings",
        json={"ingest_unknown_sources": "drop", "ingest_ignored_sources": ["127.0.0.97"]},
    )
    send_udp(sp, b"<13>x", "127.0.0.96")
    send_udp(sp, b"<13>y", "127.0.0.97")
    await wait_for(lambda: _dropped(admin, "syslog", "unknown_source"))
    await wait_for(lambda: _dropped(admin, "syslog", "ignored_source"))
    # device delete -> its rows become an unknown source
    await admin.delete(f"/api/devices/{d['id']}")
    await rt.drain()
    await admin.put("/api/settings", json={"ingest_unknown_sources": "store"})
    assert await count(admin, "/api/syslog", unknown=True) == 2


async def test_flood_rate_limited(rt: Runtime, admin: httpx.AsyncClient) -> None:
    _, sp = ports(rt)
    await features(admin, syslog=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((src("127.0.0.85"), 0))
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    t0 = time.monotonic()
    for i in range(3000):
        sock.sendto(b"<13>flood %d" % i, ("127.0.0.1", sp))
        if i % 500 == 0:
            await asyncio.sleep(0)
    sock.close()
    assert time.monotonic() - t0 < 5
    h0 = time.monotonic()
    assert (await admin.get("/api/health")).status_code == 200
    assert time.monotonic() - h0 < 1.0  # responsive during the flood
    await wait_for(lambda: dropped_at_least(admin, "syslog", "rate_limited", 1))

    # wait until the counters stop moving, then check the token-bucket rule itself:
    # stored <= burst (1000) + rate (200/s) * elapsed. Fixed counts were timing-dependent on
    # slow CI runners (more refill while the flood is still being received).
    async def settled() -> dict:
        prev = None
        for _ in range(100):
            st = (await admin.get("/api/ingest/status")).json()["counters"]["syslog"]
            total = st["stored"] + st["dropped"]["rate_limited"] + st["dropped"]["queue_full"]
            if total == prev:
                return st
            prev = total
            await asyncio.sleep(0.5)
        raise AssertionError("counters kept moving")

    st = await settled()
    elapsed = time.monotonic() - t0
    assert st["stored"] <= 1000 + 200 * elapsed + 50
    assert st["dropped"]["rate_limited"] >= 1
    assert st["dropped"]["rate_limited"] + st["stored"] + st["dropped"]["queue_full"] <= 3000


# ------------------------------------------------------------------ traps
async def test_traps_v1_v2c_inform_community(rt: Runtime, admin: httpx.AsyncClient) -> None:
    tp, _ = ports(rt)
    d = await make_device(admin, name="sw7", host="10.0.0.7")
    await features(admin, traps=True)
    # v1 from an unknown source, attributed by agent-addr
    send_udp(tp, v1_trap("1.3.6.1.4.1.25506.2", 2, 0, "10.0.0.7", [(f"{IFINDEX}.5", "i", 5)]))
    await wait_for(lambda: count(admin, "/api/traps"))
    t = (await admin.get("/api/traps")).json()["items"][0]
    assert t["version"] == "v1" and t["trap_oid"] == LINK_DOWN and t["trap_name"] == "linkDown"
    assert t["device_id"] == d["id"] and t["agent_addr"] == "10.0.0.7"
    assert t["varbinds"][0] == {
        "oid": f"{IFINDEX}.5",
        "name": "ifIndex.5",
        "type": "Integer",
        "value": "5",
        "display": "5",
    }
    # v2c inform: answered with a Response echoing request-id
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((src("127.0.0.82"), 0))
    s.setblocking(False)
    s.sendto(v2c_trap("1.3.6.1.6.3.1.1.5.1", inform=True), ("127.0.0.1", tp))
    data = await asyncio.wait_for(asyncio.get_running_loop().sock_recv(s, 65535), 5)
    s.close()
    from pysnmp.proto import api

    assert int(api.decodeMessageVersion(data)) == 1
    await wait_for(lambda: count(admin, "/api/traps", trap_oid="1.3.6.1.6.3.1.1.5.1"))
    inf = (await admin.get("/api/traps", params={"trap_oid": "1.3.6.1.6.3.1.1.5.1"})).json()
    assert inf["items"][0]["pdu"] == "inform" and inf["items"][0]["device_id"] is None
    assert await count(admin, "/api/traps", trap_oid="1.3.6.1.6.3.1.1.5") == 2  # prefix
    assert await count(admin, "/api/traps", q="IFINDEX=5") == 1
    st = (await admin.get("/api/traps/stats")).json()
    assert st["total"] == 2 and {x["trap_name"] for x in st["top_traps"]} == {
        "linkDown",
        "coldStart",
    }
    assert (await admin.get(f"/api/traps/{t['id']}")).json()["summary"] == t["summary"]
    # community check `known`: device/group communities + trap_communities + public
    await admin.put("/api/settings", json={"trap_community_check": "known"})
    send_udp(tp, v2c_trap(LINK_DOWN, community="wrong"))
    await wait_for(lambda: _dropped(admin, "trap", "bad_community"))
    send_udp(tp, v2c_trap(LINK_DOWN, community="public"))
    await wait_for(lambda: has(admin, "/api/traps", 2, trap_oid=LINK_DOWN))
    send_udp(tp, b"\x30\x03garbage")
    await wait_for(lambda: _dropped(admin, "trap", "parse_error"))


async def test_traps_v3(rt: Runtime, admin: httpx.AsyncClient) -> None:
    if not LOOPBACK_ALIASES:
        pytest.skip("needs 127.0.0.x source addresses")
    tp, _ = ports(rt)
    creds = {"version": "v3", "v3_username": "netmon", "v3_security_level": "authPriv",
             "v3_auth_protocol": "SHA", "v3_auth_key": "authpass123",
             "v3_priv_protocol": "AES128", "v3_priv_key": "privpass123"}  # fmt: skip
    await make_device(admin, name="v3a", host="127.0.0.83", snmp_credentials=creds)
    await make_device(admin, name="v3b", host="127.0.0.84", snmp_credentials=creds)
    await admin.put(
        "/api/settings",
        json={"trap_v3_users": [{"username": "trapper", "security_level": "authPriv",
                                 "auth_protocol": "SHA256", "auth_key": "authpass456",
                                 "priv_protocol": "AES256", "priv_key": "privpass456"}]},
    )  # fmt: skip
    await features(admin, traps=True)
    keys = {"auth_key": "authpass123", "priv_key": "privpass123"}
    # two engine IDs, one user
    send_udp(tp, await v3_trap("8000000001020304", "netmon", LINK_DOWN, **keys), "127.0.0.83")
    send_udp(tp, await v3_trap("8000000001020305", "netmon", LINK_DOWN, **keys), "127.0.0.84")
    send_udp(
        tp,
        await v3_trap("80000000010203aa", "trapper", "1.3.6.1.6.3.1.1.5.5", auth="SHA256",
                      priv="AES256", auth_key="authpass456", priv_key="privpass456"),
        "127.0.0.86",
    )  # fmt: skip
    await wait_for(lambda: has(admin, "/api/traps", 3))
    items = (await admin.get("/api/traps")).json()["items"]
    assert {i["version"] for i in items} == {"v3"}
    assert {i["security_name"] for i in items} == {"netmon", "trapper"}
    assert {i["device_name"] for i in items} == {"v3a", "v3b", None}
    # wrong key, unknown user -> v3_auth + failures (admins only)
    bad = await v3_trap("8000000001020306", "netmon", LINK_DOWN, auth_key="wrongpass99",
                        priv_key="privpass123")  # fmt: skip
    send_udp(tp, bad, "127.0.0.83")
    send_udp(tp, await v3_trap("8000000001020307", "nobody", LINK_DOWN, **keys), "127.0.0.83")
    await wait_for(lambda: dropped_at_least(admin, "trap", "v3_auth", 2))
    st = (await admin.get("/api/ingest/status")).json()
    users = {f["username"] for f in st["v3_failures"]}
    assert users == {"netmon", "nobody"}
    assert any(f["engine_id"] == "8000000001020306" for f in st["v3_failures"])


# ------------------------------------------------------------------ SIEM forwarding
class _Sink:
    def __init__(self) -> None:
        self.lines: list[bytes] = []

    async def udp(self) -> tuple[Any, int]:
        loop = asyncio.get_running_loop()
        sink = self

        class P(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: Any) -> None:
                sink.lines.append(data)

        t, _ = await loop.create_datagram_endpoint(P, local_addr=("127.0.0.1", 0))
        return t, t.get_extra_info("sockname")[1]

    async def tcp(self) -> tuple[Any, int]:
        async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            while data := await r.read(65536):
                self.lines.append(data)
            w.close()

        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        return srv, srv.sockets[0].getsockname()[1]


async def test_siem_forwarding(rt: Runtime, admin: httpx.AsyncClient) -> None:
    _, sp = ports(rt)
    await make_device(admin, name="fw-src", host=src("127.0.0.81"))
    usink, tsink = _Sink(), _Sink()
    ut, uport = await usink.udp()
    srv, tport = await tsink.tcp()
    try:
        r = await admin.put(
            "/api/settings",
            json={"siem_targets": [
                {"name": "udp", "host": "127.0.0.1", "port": uport, "protocol": "udp"},
                {"name": "tcp", "host": "127.0.0.1", "port": tport, "protocol": "tcp",
                 "min_severity": 3},
            ]},
        )  # fmt: skip
        tid = r.json()["siem_targets"][0]["id"]
        await features(admin, syslog=True, siem_forwarding=True)
        send_udp(sp, b"<30>Nov  8 09:05:01 gw-hq sshd[2211]: Accepted")
        send_udp(sp, b"<187>Nov  8 09:05:02 gw-hq app: disk failure")
        await wait_for(lambda: _ready(len(usink.lines) >= 2))
        await wait_for(lambda: _ready(len(tsink.lines) >= 1))
        line = usink.lines[0].decode()
        assert line.startswith("<30>1 ") and " gw-hq sshd 2211 - [origin ip=" in line
        assert b" <187>1 " in b" " + tsink.lines[0].split(b" ", 1)[1] or tsink.lines[0].split(
            b" ", 1
        )[1].startswith(b"<187>1")
        st = (await admin.get("/api/ingest/status")).json()
        fw = {f["name"]: f for f in st["forwarders"]}
        assert fw["udp"]["sent"] >= 2 and fw["tcp"]["sent"] == 1
        assert st["counters"]["syslog"]["forwarded"] == 2
        r = await admin.post("/api/ingest/forwarders/test", json={"target_id": tid})
        assert r.status_code == 200 and r.json() == {"ok": True, "message": "sent"}
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        cport = closed.getsockname()[1]
        closed.close()
        r = await admin.post(
            "/api/ingest/forwarders/test",
            json={"target": {"name": "x", "host": "127.0.0.1", "port": cport, "protocol": "tcp"}},
        )
        assert r.status_code == 502 and r.json()["error"]["code"] == "forward_error"
    finally:
        ut.close()
        srv.close()
        await features(admin, siem_forwarding=False)


async def _ready(v: bool) -> bool:
    return v


# ------------------------------------------------------------------ WS live batches
async def test_ws_live_batches(rt: Runtime, admin: httpx.AsyncClient) -> None:
    from netmon.realtime.hub import _Client

    class _WS:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_text(self, m: str) -> None:
            self.sent.append(m)

    ws = _WS()
    client = rt.hub.register(ws, user_id=1)  # type: ignore[arg-type]
    try:
        assert isinstance(client, _Client)
        client.topics.add("syslog")
        _, sp = ports(rt)
        await features(admin, syslog=True)
        send_udp(sp, b"<13>live one")
        await wait_for(lambda: _ready(any('"syslog.batch"' in m for m in ws.sent)))
        msg = next(m for m in ws.sent if '"syslog.batch"' in m)
        assert '"live one"' in msg and '"skipped": 0' in msg
    finally:
        await rt.hub.unregister(client)
