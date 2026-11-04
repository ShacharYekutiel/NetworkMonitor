"""In-memory WebSocket fan-out."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from starlette.websockets import WebSocket

from netmon.services.serialize import iso, now_utc

log = logging.getLogger("netmon.realtime")


def make_message(type_: str, data: dict[str, Any]) -> str:
    return json.dumps({"type": type_, "ts": iso(now_utc()), "data": data}, ensure_ascii=False)


class _Client:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1000)
        self.task: asyncio.Task[None] | None = None

    async def sender(self) -> None:
        while True:
            msg = await self.queue.get()
            if msg is None:
                return
            try:
                await self.ws.send_text(msg)
            except Exception:  # noqa: BLE001
                return


class Hub:
    def __init__(self) -> None:
        self._clients: set[_Client] = set()
        self.listeners: list[asyncio.Queue[dict[str, Any]]] = []  # for tests / internal use

    @property
    def count(self) -> int:
        return len(self._clients)

    def register(self, ws: WebSocket, first: str | None = None) -> _Client:
        client = _Client(ws)
        if first is not None:
            client.queue.put_nowait(first)
        client.task = asyncio.create_task(client.sender())
        self._clients.add(client)
        return client

    async def unregister(self, client: _Client) -> None:
        self._clients.discard(client)
        with contextlib.suppress(asyncio.QueueFull):
            client.queue.put_nowait(None)
        if client.task:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.task, 2)

    def broadcast(self, type_: str, data: dict[str, Any]) -> None:
        for q in self.listeners:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait({"type": type_, "data": data})
        if not self._clients:
            return
        msg = make_message(type_, data)
        for client in list(self._clients):
            try:
                client.queue.put_nowait(msg)
            except asyncio.QueueFull:
                log.warning("Dropping slow WebSocket client")
                self._clients.discard(client)
                asyncio.ensure_future(_close(client.ws, 1008))

    async def close_all(self, code: int = 1001) -> None:
        for client in list(self._clients):
            await _close(client.ws, code)
            await self.unregister(client)


async def _close(ws: WebSocket, code: int) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code)
