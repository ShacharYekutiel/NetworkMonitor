"""Driver auto-selection fallback: the device's sysObjectID read live over SNMP.

`match_driver` uses the stored identity (suggested template / sys_object_id from discovery or
"Re-identify"). A device added by hand has no identity, so "Automatic" found no driver
(owner's office test). For such devices the sysObjectID is read over SNMP with the device's
effective credentials and cached in memory: 24 h when read, 15 min when the read failed."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.configbackup.sysoid")

SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
TTL_OK_S = 24 * 3600.0
TTL_FAIL_S = 15 * 60.0
PROBE_TIMEOUT_S = 6.0
PROBE_CONCURRENCY = 16
_cache: dict[int, tuple[str | None, float]] = {}


def cached(device_id: int) -> str | None:
    hit = _cache.get(device_id)
    if hit is None:
        return None
    value, until = hit
    if time.monotonic() > until:
        _cache.pop(device_id, None)
        return None
    return value


def forget(device_id: int | None = None) -> None:
    if device_id is None:
        _cache.clear()
    else:
        _cache.pop(device_id, None)


def _fresh(device_id: int) -> bool:
    hit = _cache.get(device_id)
    return hit is not None and time.monotonic() <= hit[1]


async def _probe_one(rt: Runtime, device_id: int) -> None:
    from netmon.models import Device
    from netmon.poller.snmp_client import SnmpError, snmp_get
    from netmon.services.sensors import effective_credentials

    async with rt.sessionmaker() as session:
        device = await session.get(Device, device_id)
        if device is None or not device.enabled:
            return
        creds: dict[str, Any] = await effective_credentials(session, device)
        host = device.host
    value: str | None = None
    try:
        res = await asyncio.wait_for(snmp_get(host, creds, [SYS_OBJECT_ID]), PROBE_TIMEOUT_S)
        raw = res.get(SYS_OBJECT_ID)
        if raw is not None:
            value = str(raw).strip().lstrip(".") or None
    except (SnmpError, TimeoutError, OSError) as exc:
        log.debug("sysObjectID of device %s not readable: %s", device_id, exc)
    _cache[device_id] = (value, time.monotonic() + (TTL_OK_S if value else TTL_FAIL_S))


def wanted(ctxs: list[Any]) -> list[int]:
    """Devices whose only missing piece is the driver: enabled, backup on, SSH credentials
    present, no override, nothing matched from the stored identity."""
    return [
        c.id
        for c in ctxs
        if c.eligible[1] == "no_driver" and not c.driver_override and not _fresh(c.id)
    ]


async def probe(rt: Runtime, device_ids: list[int]) -> None:
    """Read the sysObjectID of the given devices (skipping fresh cache entries)."""
    todo = [d for d in dict.fromkeys(device_ids) if not _fresh(d)]
    if not todo:
        return
    sem = asyncio.Semaphore(PROBE_CONCURRENCY)

    async def one(d: int) -> None:
        async with sem:
            try:
                await _probe_one(rt, d)
            except Exception as exc:  # noqa: BLE001 - a probe never breaks a backup pass
                log.debug("sysObjectID probe of device %s failed: %s", d, exc)
                _cache[d] = (None, time.monotonic() + TTL_FAIL_S)

    await asyncio.gather(*(one(d) for d in todo))
