"""SnmpEngine is built in a worker thread: a slow build (MIB loading) must not freeze the event
loop (CI measured 0.27-0.33 s per engine inside the loop during a discovery scan)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from netmon.poller import snmp_client


class _SlowEngine:
    built = 0

    def __init__(self) -> None:
        time.sleep(0.3)  # blocking, like pysnmp loading its MIB modules
        type(self).built += 1

    def close_dispatcher(self) -> None:
        pass


async def test_engine_built_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import pysnmp.hlapi.v3arch.asyncio as h

    monkeypatch.setattr(h, "SnmpEngine", _SlowEngine)
    monkeypatch.setattr(snmp_client, "_engines", {})
    monkeypatch.setattr(snmp_client, "_engine_locks", {})
    _SlowEngine.built = 0
    v3: dict[str, Any] = {"version": "v3", "v3_username": "netmon", "v3_auth_key": "k" * 8}

    async def ticker() -> float:
        lag = 0.0
        for _ in range(12):
            t0 = time.perf_counter()
            await asyncio.sleep(0.05)
            lag = max(lag, time.perf_counter() - t0 - 0.05)
        return lag

    tick = asyncio.create_task(ticker())
    # concurrent first queries with the same credentials build ONE engine
    engines = await asyncio.gather(*(snmp_client._get_engine(v3) for _ in range(5)))
    v12 = await snmp_client._get_engine({"version": "v2c"})
    lag = await tick
    assert lag < 0.15, f"event loop blocked for {lag:.3f}s"
    assert len({id(e) for e in engines}) == 1 and v12 is not engines[0]
    assert _SlowEngine.built == 2
    assert await snmp_client._get_engine(v3) is engines[0]  # cached


class _FastEngine:
    closed = 0

    def close_dispatcher(self) -> None:
        type(self).closed += 1


async def test_v3_engine_replaced_for_a_device_that_was_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[1.4.2] pysnmp drops a device's SNMPv3 clock after 300 s without an answer and then
    sends boots = time = 0, which HPE Comware never answers: the device stayed down until a
    restart (credentials wrong for five minutes, then corrected). A device that answered
    before and was silent for V3_RESYNC_IDLE_S gets a fresh engine (new discovery)."""
    import pysnmp.hlapi.v3arch.asyncio as h

    monkeypatch.setattr(h, "SnmpEngine", _FastEngine)
    monkeypatch.setattr(snmp_client, "_engines", {})
    monkeypatch.setattr(snmp_client, "_engine_locks", {})
    monkeypatch.setattr(snmp_client, "_last_ok", {})
    monkeypatch.setattr(snmp_client, "_RETIRED_CLOSE_DELAY_S", 0.01)
    now = [1000.0]
    monkeypatch.setattr(snmp_client, "_now", lambda: now[0])
    _FastEngine.closed = 0
    v3: dict[str, Any] = {"version": "v3", "v3_username": "netmon", "v3_auth_key": "k" * 8}
    v2: dict[str, Any] = {"version": "v2c", "community": "public"}

    e1 = await snmp_client._get_engine(v3, "10.0.0.1")
    snmp_client.mark_answered(v3, "10.0.0.1")
    snmp_client.mark_answered(v3, "10.0.0.2")
    now[0] += 200  # polled regularly: the same engine
    assert await snmp_client._get_engine(v3, "10.0.0.1") is e1
    snmp_client.mark_answered(v3, "10.0.0.2")
    now[0] += 100  # .1 silent for 300 s, .2 answered 100 s ago
    assert await snmp_client._get_engine(v3, "10.0.0.2") is e1
    e2 = await snmp_client._get_engine(v3, "10.0.0.1")
    assert e2 is not e1
    await asyncio.sleep(0.05)
    assert _FastEngine.closed == 1  # the old engine is closed after the grace period
    # a device that never answers again does not replace the engine on every poll
    now[0] += 3600
    assert await snmp_client._get_engine(v3, "10.0.0.1") is e2
    assert await snmp_client._get_engine(v3, "10.0.0.9") is e2  # never seen: nothing to resync
    # v1 / v2c has no clock: the shared engine is never replaced
    v12 = await snmp_client._get_engine(v2, "10.0.0.1")
    snmp_client.mark_answered(v2, "10.0.0.1")
    now[0] += 3600
    assert await snmp_client._get_engine(v2, "10.0.0.1") is v12
