"""[3] `port_flap_episodes` (data-model § Port flapping episodes, for the reports'
`flapping_ports` section). Fed by the 2b-3 flapping tracker while `flapping` is ON: insert at
the start, update on transitions (at most once per 10 s) and at the end."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

log = logging.getLogger("netmon.reports.episodes")

WRITE_EVERY_S = 10.0
Port = tuple[int, int]


@dataclass
class _Open:
    id: int | None
    transitions: int
    written_at: float


class EpisodeRecorder:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.open: dict[Port, _Open] = {}
        self._lock = asyncio.Lock()  # start/update/end of one port stay in order

    async def change(self, kind: str, port: Port, count: int, at: float, name: str = "") -> None:
        async with self._lock:
            await self._change(kind, port, count, at, name)

    async def _change(self, kind: str, port: Port, count: int, at: float, name: str) -> None:
        try:
            if kind == "start":
                await self._start(port, count, at, name)
            elif kind == "update":
                ep = self.open.get(port)
                if ep is None:
                    await self._start(port, count, at, name)
                    return
                ep.transitions += 1
                if time.monotonic() - ep.written_at >= WRITE_EVERY_S:
                    await self._write(ep, None)
        except Exception:  # noqa: BLE001 - statistics never break the flapping path
            log.debug("Writing a flapping episode failed", exc_info=True)

    async def _start(self, port: Port, count: int, at: float, name: str) -> None:
        async with self.rt.sessionmaker() as session:
            eid = (
                await session.execute(
                    text(
                        "INSERT INTO port_flap_episodes (device_id, if_index, port_name, "
                        "started_at, transitions) VALUES (:d, :i, :n, :t, :c) RETURNING id"
                    ),
                    {
                        "d": port[0],
                        "i": port[1],
                        "n": (name or "")[:255],
                        "t": datetime.fromtimestamp(at, UTC),
                        "c": count,
                    },
                )
            ).scalar_one()
            await session.commit()
        self.open[port] = _Open(int(eid), count, time.monotonic())

    async def _write(self, ep: _Open, ended: datetime | None) -> None:
        async with self.rt.sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE port_flap_episodes SET transitions = :c, "
                    "ended_at = coalesce(:e, ended_at) WHERE id = :i"
                ),
                {"c": ep.transitions, "e": ended, "i": ep.id},
            )
            await session.commit()
        ep.written_at = time.monotonic()

    async def end(self, ports: list[Port] | None = None) -> None:
        """Ports stopped flapping (quiet period over, cleared, feature OFF: None = all)."""
        async with self._lock:
            await self._end(ports)

    async def _end(self, ports: list[Port] | None) -> None:
        now = self.rt.clock.now()
        for port in list(self.open) if ports is None else ports:
            ep = self.open.pop(port, None)
            if ep is None:
                continue
            try:
                await self._write(ep, now)
            except Exception:  # noqa: BLE001
                log.debug("Closing a flapping episode failed", exc_info=True)
