"""POST /devices/{id}/apply-template removing sensors vs. the poller's result writer.

`poller.processor.persist` locks a sensor row (`SELECT ... FOR UPDATE`) and then updates its
`sensor_channels` rows. The apply transaction must take the locks in the same order: deleting
an ORM-loaded `Sensor` flushes `DELETE FROM sensor_channels` (its loaded channels, delete
cascade) before `DELETE FROM sensors`, so without locking the sensor rows first the two
transactions could deadlock (seen in E2E spec 80-apply-ports: `deadlock detected` between
`UPDATE sensor_channels` and `DELETE FROM sensors`)."""

from __future__ import annotations

import asyncio
import time

import asyncpg
import httpx
from sqlalchemy import text

from netmon.runtime import Runtime
from tests.conftest import _plain, requires_db
from tests.test_13 import _lab
from tests.test_13_apply import SW_IP, _apply, _creds, _ifaces, agents  # noqa: F401

pytestmark = requires_db


async def _blocked_backends(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch(
        "SELECT query FROM pg_stat_activity WHERE datname = current_database() "
        "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
    )
    return [r["query"] for r in rows]


async def test_apply_remove_locks_sensor_before_channels(
    rt: Runtime,
    admin: httpx.AsyncClient,
    agents: dict[str, int],  # noqa: F811
) -> None:
    a, _ = await _lab(rt, admin, host=SW_IP)
    await admin.patch(f"/api/devices/{a}", json={"snmp_credentials": _creds(agents[SW_IP])})
    res = await _apply(admin, a, interfaces=[1, 8])
    assert _ifaces(res) == ["Gi1/0/1", "Gi1/0/8"]
    async with rt.sessionmaker() as s:
        sid = (
            await s.execute(
                text(
                    "SELECT id FROM sensors WHERE device_id = :d AND type = 'snmp_interface' "
                    "ORDER BY id LIMIT 1"
                ),
                {"d": a},
            )
        ).scalar_one()
    url = rt.settings.database_url
    poller = await asyncpg.connect(_plain(url))
    watcher = await asyncpg.connect(_plain(url))
    try:
        # the result writer's transaction for sensor `sid`, step 1: the sensor row lock
        tr = poller.transaction()
        await tr.start()
        await poller.execute("SELECT id FROM sensors WHERE id = $1 FOR UPDATE", sid)
        apply = asyncio.create_task(
            admin.post(
                f"/api/devices/{a}/apply-template",
                json={
                    "template_id": "hpe_comware",
                    "interfaces": "none",
                    "remove_unselected_interfaces": True,
                },
            )
        )
        deadline = time.monotonic() + 30
        while not await _blocked_backends(watcher):
            assert not apply.done(), (await apply).text
            assert time.monotonic() < deadline, "apply never waited for the sensor lock"
            await asyncio.sleep(0.05)
        # the apply transaction now waits for `sid`, before touching its channel rows
        # step 2 of the result writer: its channels (a deadlock here before the fix)
        await poller.execute(
            "UPDATE sensor_channels SET last_value = 1, status = 'up' WHERE sensor_id = $1", sid
        )
        await tr.commit()
        r = await asyncio.wait_for(apply, 60)
    finally:
        await poller.close()
        await watcher.close()
    assert r.status_code == 200, r.text
    assert sorted(x["name"] for x in r.json()["removed"]) == ["Gi1/0/1", "Gi1/0/8"]
    async with rt.sessionmaker() as s:
        left = (
            await s.execute(
                text("SELECT count(*) FROM sensor_channels WHERE sensor_id = :s"), {"s": sid}
            )
        ).scalar_one()
    assert left == 0
