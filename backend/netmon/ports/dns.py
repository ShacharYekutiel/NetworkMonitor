"""[2b-2] Cached reverse DNS for ARP IPs (`dns_cache`). Filled only by the inventory job.

System resolver via `loop.getnameinfo(..., NI_NAMEREQD)` (works on Windows), 2 s per lookup,
8 concurrent, at most 256 lookups per pass. Test hook: NETMON_TEST_MODE=1 +
NETMON_TEST_PTR_FILE (JSON {"10.0.0.11": "pc.lan"}) replaces the resolver."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import socket
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.ports.dns")

LOOKUP_TIMEOUT_S = 2.0
CONCURRENCY = 8
MAX_PER_PASS = 256
PASS_BUDGET_S = 60.0
FOUND_TTL_H = 24
MISSING_TTL_H = 1


def _test_ptr(rt: Runtime) -> dict[str, str] | None:
    if not rt.settings.netmon_test_mode:
        return None
    path = os.environ.get("NETMON_TEST_PTR_FILE")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        log.warning("Cannot read NETMON_TEST_PTR_FILE %s: %s", path, exc)
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def clean_name(name: str | None) -> str | None:
    if not name:
        return None
    n = name.strip().rstrip(".").lower()
    return n[:255] or None


# PTR lookups use their own threads: the default executor also serves loop.getaddrinfo (pysnmp
# target resolution), so slow PTR queries there would stall polling.
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="ptr")
_sems: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _semaphore() -> asyncio.Semaphore:
    """One semaphore per event loop, shared by every device's DNS pass."""
    loop = asyncio.get_running_loop()
    sem = _sems.get(loop)
    if sem is None:
        for old in [lp for lp in _sems if lp.is_closed()]:
            del _sems[old]
        sem = _sems[loop] = asyncio.Semaphore(CONCURRENCY)
    return sem


def _getnameinfo(ip: str) -> str:
    return socket.getnameinfo((ip, 0), socket.NI_NAMEREQD)[0]


async def lookup(ip: str) -> str | None:
    loop = asyncio.get_running_loop()
    async with _semaphore():
        try:
            host = await asyncio.wait_for(
                loop.run_in_executor(_pool, _getnameinfo, ip), LOOKUP_TIMEOUT_S
            )
        except (TimeoutError, OSError, UnicodeError):
            return None
    return clean_name(host)


async def due_ips(session: Any, source_device_id: int, now: datetime) -> list[str]:
    rows = await session.execute(
        text(
            "SELECT DISTINCT a.ip FROM arp_entries a LEFT JOIN dns_cache c ON c.ip = a.ip "
            "WHERE a.source_device_id = :d AND a.present AND (c.ip IS NULL "
            "OR (c.name IS NOT NULL AND c.resolved_at < CAST(:now AS timestamptz) "
            "- make_interval(hours => :found)) "
            "OR (c.name IS NULL AND c.resolved_at < CAST(:now AS timestamptz) "
            "- make_interval(hours => :missing))) "
            "ORDER BY a.ip LIMIT :n"
        ),
        {
            "d": source_device_id,
            "now": now,
            "found": FOUND_TTL_H,
            "missing": MISSING_TTL_H,
            "n": MAX_PER_PASS,
        },
    )
    return [r[0] for r in rows]


async def refresh(rt: Runtime, source_device_id: int) -> int:
    """Resolve the due IPs of one ARP source; returns the number of lookups."""
    if not await _enabled(rt):
        return 0
    now = rt.clock.now()
    async with rt.sessionmaker() as session:
        ips = await due_ips(session, source_device_id, now)
    if not ips:
        return 0
    fake = _test_ptr(rt)
    results: dict[str, str | None] = {}

    async def one(ip: str) -> None:
        if fake is not None:
            results[ip] = clean_name(fake.get(ip))
            return
        results[ip] = await lookup(ip)

    try:
        await asyncio.wait_for(asyncio.gather(*(one(ip) for ip in ips)), PASS_BUDGET_S)
    except TimeoutError:
        log.info("Reverse DNS pass for device %s hit its budget", source_device_id)
    if not results:
        return 0
    async with rt.sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO dns_cache (ip, name, resolved_at) "
                "SELECT * FROM unnest(CAST(:ips AS varchar[]), CAST(:names AS varchar[]), "
                "CAST(:ts AS timestamptz[])) "
                "ON CONFLICT (ip) DO UPDATE SET name = EXCLUDED.name, "
                "resolved_at = EXCLUDED.resolved_at"
            ),
            {
                "ips": list(results),
                "names": [results[i] for i in results],
                "ts": [now] * len(results),
            },
        )
        await session.commit()
    return len(results)


async def _enabled(rt: Runtime) -> bool:
    from netmon.services import settings as settings_svc

    async with rt.sessionmaker() as session:
        return bool(await settings_svc.get(session, "reverse_dns"))
