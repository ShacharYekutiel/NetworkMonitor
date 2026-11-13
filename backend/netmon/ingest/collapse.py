"""[1.2.1] Repetitive traps: keep only the newest N per (source, trap OID), and bring stored
trap names up to date with the built-in OID name table.

Owner's switch: a Comware heartbeat (hh3cPeriodicalTrap) and syslog-as-trap
(syslogMsgNotification) filled the trap list. With `trap_collapse_enabled` (OFF by default)
traps whose OID is in `trap_collapse_oids` are pruned to the newest `trap_collapse_keep` per
source address and OID right after each write (event rules still see every trap)."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text

from netmon.ingest import oid_names

log = logging.getLogger("netmon.ingest.collapse")

DEFAULT_OIDS = [
    "1.3.6.1.4.1.25506.2.38.1.6.3.0.1",  # hh3cPeriodicalTrap (Comware heartbeat)
    "1.3.6.1.2.1.192.0.1",  # syslogMsgNotification (syslog sent as a trap)
]
NAMES_VERSION_KEY = "trap_names_version"

_PRUNE = text(
    "DELETE FROM traps t USING ("
    "  SELECT id, time FROM traps WHERE source_ip = CAST(:s AS inet) AND trap_oid = :o"
    "  ORDER BY time DESC, id DESC OFFSET :n"
    ") old WHERE t.id = old.id AND t.time = old.time"
)


def normalize_oid(oid: str) -> str:
    return oid.strip().lstrip(".")


def matches(oid: str, oids: Iterable[str]) -> bool:
    return normalize_oid(oid) in {normalize_oid(o) for o in oids}


async def prune(rt: Any, groups: Iterable[tuple[str, str]], keep: int) -> int:
    """Keep the newest `keep` traps of each (source_ip, trap_oid). Returns rows removed."""
    removed = 0
    async with rt.sessionmaker() as session:
        for source_ip, trap_oid in sorted(set(groups)):
            res = await session.execute(_PRUNE, {"s": source_ip, "o": trap_oid, "n": int(keep)})
            removed += int(res.rowcount or 0)
        await session.commit()
    return removed


async def prune_all(rt: Any, oids: list[str], keep: int) -> int:
    """Prune the rows already stored (when the setting is turned on or the list changes)."""
    if not oids:
        return 0
    async with rt.sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT host(source_ip) AS s, trap_oid AS o FROM traps "
                    "WHERE trap_oid = ANY(:oids)"
                ),
                {"oids": [normalize_oid(o) for o in oids]},
            )
        ).all()
    n = await prune(rt, [(r.s, r.o) for r in rows], keep)
    if n:
        log.info("Removed %d repetitive trap(s)", n)
    return n


def names_version() -> str:
    raw = json.dumps(oid_names.NAMES, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


async def backfill_names(rt: Any) -> int:
    """Once per OID-name-table version: set `trap_name` (and the summary's first word) of stored
    traps to the current name. Returns the number of OIDs renamed."""
    from netmon.services import settings as settings_svc

    version = names_version()
    async with rt.sessionmaker() as session:
        if await settings_svc.get(session, NAMES_VERSION_KEY) == version:
            return 0
        rows = (
            await session.execute(
                text("SELECT trap_oid, max(trap_name) AS trap_name FROM traps GROUP BY trap_oid")
            )
        ).all()
        renamed = 0
        for r in rows:
            new = oid_names.name(r.trap_oid)
            if not new or new == r.trap_name:
                continue
            # the summary starts with the old name (or the OID): swap that first word
            await session.execute(
                text(
                    "UPDATE traps SET trap_name = :n, summary = :n || substring(summary FROM "
                    "char_length(coalesce(trap_name, trap_oid)) + 1) "
                    "WHERE trap_oid = :o AND trap_name IS DISTINCT FROM :n "
                    "AND starts_with(summary, coalesce(trap_name, trap_oid))"
                ),
                {"n": new[:128], "o": r.trap_oid},
            )
            await session.execute(
                text(
                    "UPDATE traps SET trap_name = :n WHERE trap_oid = :o "
                    "AND trap_name IS DISTINCT FROM :n"
                ),
                {"n": new[:128], "o": r.trap_oid},
            )
            renamed += 1
        await settings_svc.put(session, NAMES_VERSION_KEY, version)
        await session.commit()
    if renamed:
        log.info("Updated the stored names of %d trap OID(s)", renamed)
    return renamed
