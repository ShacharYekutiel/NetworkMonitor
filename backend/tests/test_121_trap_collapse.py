"""[1.2.1] Repetitive traps (owner's Comware switch): keep the newest N per source and OID when
`trap_collapse_enabled` is on; stored trap names follow the OID name table."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import text

from netmon.ingest import collapse
from netmon.runtime import Runtime
from tests.conftest import make_device, requires_db
from tests.ingest_helpers import send_udp, v2c_trap, wait_for
from tests.test_2b3_ingest import count, features, ports

pytestmark = requires_db

HEARTBEAT = "1.3.6.1.4.1.25506.2.38.1.6.3.0.1"
LINK_DOWN = "1.3.6.1.6.3.1.1.5.3"


async def _n(admin: httpx.AsyncClient, oid: str) -> int:
    return await count(admin, "/api/traps", trap_oid=oid)


async def test_collapse_off_by_default_then_keep_newest(
    rt: Runtime, admin: httpx.AsyncClient
) -> None:
    s = (await admin.get("/api/settings")).json()
    assert s["trap_collapse_enabled"] is False and s["trap_collapse_keep"] == 10
    assert HEARTBEAT in s["trap_collapse_oids"]
    tp, _ = ports(rt)
    await make_device(admin, name="sw1", host="127.0.0.81")
    await features(admin, traps=True)
    for i in range(15):
        send_udp(tp, v2c_trap(HEARTBEAT, uptime=100 + i))
    send_udp(tp, v2c_trap(LINK_DOWN))

    async def all_in() -> bool:
        return await _n(admin, HEARTBEAT) == 15 and await _n(admin, LINK_DOWN) == 1

    await wait_for(all_in)
    # OFF: everything kept. Turning it on prunes what is stored already.
    r = await admin.put(
        "/api/settings", json={"trap_collapse_enabled": True, "trap_collapse_keep": 5}
    )
    assert r.status_code == 200, r.text

    async def pruned(n: int) -> bool:
        return await _n(admin, HEARTBEAT) == n

    await wait_for(lambda: pruned(5))
    items = (await admin.get("/api/traps", params={"trap_oid": HEARTBEAT})).json()["items"]
    assert sorted(int(i["uptime_ticks"]) for i in items) == [110, 111, 112, 113, 114]
    assert items[0]["trap_name"] == "hh3cPeriodicalTrap"
    # new arrivals keep the window; other OIDs are untouched
    for i in range(3):
        send_udp(tp, v2c_trap(HEARTBEAT, uptime=200 + i))

    async def newest() -> bool:
        got = (await admin.get("/api/traps", params={"trap_oid": HEARTBEAT})).json()["items"]
        return len(got) == 5 and max(int(x["uptime_ticks"]) for x in got) == 202

    await wait_for(newest)
    assert await _n(admin, LINK_DOWN) == 1
    # per source: a second source has its own window
    send_udp(tp, v2c_trap(HEARTBEAT), source="127.0.0.82")
    await wait_for(lambda: pruned(6))


async def test_collapse_settings_validation(rt: Runtime, admin: httpx.AsyncClient) -> None:
    for body in (
        {"trap_collapse_oids": ["not.an.oid"]},
        {"trap_collapse_oids": ["1"]},
        {"trap_collapse_keep": 0},
        {"trap_collapse_keep": 1001},
    ):
        r = await admin.put("/api/settings", json=body)
        assert r.status_code == 422, (body, r.text)
    r = await admin.put("/api/settings", json={"trap_collapse_oids": [".1.3.6.1.2.1.192.0.1"]})
    assert r.json()["trap_collapse_oids"] == ["1.3.6.1.2.1.192.0.1"]


async def test_backfill_names(rt: Runtime, admin: httpx.AsyncClient) -> None:
    async with rt.sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO traps (time, source_ip, version, pdu, trap_oid, trap_name, summary) "
                "VALUES (now(), '10.0.0.1', 'v2c', 'trap', :o, 'h3c.2.38.1.6.3.0.1', "
                "'h3c.2.38.1.6.3.0.1 sysUpTime.0=1'), "
                "(now(), '10.0.0.1', 'v2c', 'trap', '1.3.6.1.2.1.192.0.1', NULL, "
                "'1.3.6.1.2.1.192.0.1 x=1'), "
                "(now(), '10.0.0.1', 'v2c', 'trap', '1.3.6.1.4.1.999999.1', NULL, "
                "'1.3.6.1.4.1.999999.1 y=2')"
            ),
            {"o": HEARTBEAT},
        )
        await session.execute(text("DELETE FROM settings WHERE key = 'trap_names_version'"))
        await session.commit()
    assert await collapse.backfill_names(rt) == 2
    assert await collapse.backfill_names(rt) == 0  # same table version: nothing to do
    async with rt.sessionmaker() as session:
        rows: dict[str, Any] = {
            r.trap_oid: (r.trap_name, r.summary)
            for r in await session.execute(text("SELECT trap_oid, trap_name, summary FROM traps"))
        }
    assert rows[HEARTBEAT] == ("hh3cPeriodicalTrap", "hh3cPeriodicalTrap sysUpTime.0=1")
    assert rows["1.3.6.1.2.1.192.0.1"] == ("syslogMsgNotification", "syslogMsgNotification x=1")
    assert rows["1.3.6.1.4.1.999999.1"] == (None, "1.3.6.1.4.1.999999.1 y=2")
