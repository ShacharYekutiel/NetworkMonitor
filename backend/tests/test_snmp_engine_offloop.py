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
