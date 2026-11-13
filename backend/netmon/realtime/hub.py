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
    def __init__(
        self,
        ws: WebSocket,
        user_id: int | None = None,
        is_admin: bool = False,
        sid: str | None = None,
    ) -> None:
        self.ws = ws
        self.user_id = user_id
        self.sid = sid  # [1.4.1] session id of the cookie the socket was opened with
        self.is_admin = is_admin
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1000)
        self.task: asyncio.Task[None] | None = None
        self.topics: set[str] = set()  # [2b-3] live views (syslog, trap); per socket

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

    def register(
        self,
        ws: WebSocket,
        first: str | None = None,
        user_id: int | None = None,
        is_admin: bool = False,
        sid: str | None = None,
    ) -> _Client:
        client = _Client(ws, user_id, is_admin, sid)
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

    def broadcast(self, type_: str, data: dict[str, Any], admin_only: bool = False) -> None:
        """Send to every socket; `admin_only` messages (discovery.*) only to admin sockets
        (the role at connect time; a role change closes the socket)."""
        for q in self.listeners:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait({"type": type_, "data": data, "admin_only": admin_only})
        if not self._clients:
            return
        msg = make_message(type_, data)
        for client in list(self._clients):
            if admin_only and not client.is_admin:
                continue
            try:
                client.queue.put_nowait(msg)
            except asyncio.QueueFull:
                log.warning("Dropping slow WebSocket client")
                self._clients.discard(client)
                asyncio.ensure_future(_close(client.ws, 1008))

    def send_to_user(self, user_id: int, type_: str, data: dict[str, Any]) -> int:
        """[3b] Send only to the sockets of one user (`assistant.message`). Returns the number of
        recipients."""
        for q in self.listeners:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait({"type": type_, "data": data, "user_id": user_id})
        targets = [c for c in self._clients if c.user_id == user_id]
        if not targets:
            return 0
        msg = make_message(type_, data)
        for client in targets:
            try:
                client.queue.put_nowait(msg)
            except asyncio.QueueFull:
                log.warning("Dropping slow WebSocket client")
                self._clients.discard(client)
                asyncio.ensure_future(_close(client.ws, 1008))
        return len(targets)

    # [2b-3] topic subscriptions (`{"type":"subscribe","topics":[...]}`)
    def has_subscribers(self, topic: str) -> bool:
        return any(topic in c.topics for c in self._clients)

    def broadcast_topic(self, topic: str, type_: str, data: dict[str, Any]) -> int:
        """Send only to sockets subscribed to `topic`. Returns the number of recipients."""
        for q in self.listeners:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait({"type": type_, "data": data, "topic": topic})
        targets = [c for c in self._clients if topic in c.topics]
        if not targets:
            return 0
        msg = make_message(type_, data)
        for client in targets:
            try:
                client.queue.put_nowait(msg)
            except asyncio.QueueFull:
                log.warning("Dropping slow WebSocket client")
                self._clients.discard(client)
                asyncio.ensure_future(_close(client.ws, 1008))
        return len(targets)

    async def close_user(self, user_id: int, code: int = 4401) -> int:
        """Close every socket of a user (session revoked). Returns the number closed."""
        victims = [c for c in self._clients if c.user_id == user_id]
        for client in victims:
            self._clients.discard(client)
            await _close(client.ws, code)
        return len(victims)

    async def close_session(self, sid: str, code: int = 4401) -> int:
        """[1.4.1] Close the sockets of one login session (logout)."""
        victims = [c for c in self._clients if c.sid == sid]
        for client in victims:
            self._clients.discard(client)
            await _close(client.ws, code)
        return len(victims)

    async def close_all(self, code: int = 1001) -> None:
        for client in list(self._clients):
            await _close(client.ws, code)
            await self.unregister(client)


async def _close(ws: WebSocket, code: int) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code)
