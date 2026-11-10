"""[3] NOC kiosk WebSocket hub (feature component `noc`).

Kiosk sockets (`/ws/kiosk`) are kept apart from the session hub. While `noc` is ON, the hub
listens to the main hub's broadcasts and sends `{"type": "kiosk.refresh"}` (debounced 5 s) on
any status / acknowledgement / maintenance change; the client then refetches
`/api/kiosk/view`. Disabling a screen, regenerating its token or turning the feature OFF closes
its sockets with 4403."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from starlette.websockets import WebSocket

log = logging.getLogger("netmon.kiosk")

DEBOUNCE_S = 5.0
CLOSE_REVOKED = 4403
REFRESH_TYPES = (
    "device.status",
    "group.status",
    "sensor.ack",
    "event.created",
    "maintenance.changed",
    "config.changed",
)
EVENT_KINDS = ("status_change", "acknowledge", "maintenance")
CONFIG_ENTITIES = ("device", "group", "sensor", "maintenance", "kiosk_screen")


class KioskHub:
    features = ("noc",)

    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.conns: dict[int, set[WebSocket]] = {}
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._pending: asyncio.TimerHandle | None = None
        self.debounce_s = DEBOUNCE_S

    # ------------------------------------------------------------ component API
    async def start(self, snapshot: dict[str, bool]) -> None:
        if snapshot.get("noc"):
            self._listen()

    async def feature_changed(self, name: str, on: bool) -> None:
        if on:
            self._listen()
        else:
            await self._unlisten()
            await self.close_all()

    async def stop(self) -> None:
        await self._unlisten()
        await self.close_all(1001)

    def _listen(self) -> None:
        if self._task is not None and not self._task.done():
            if self._queue is not None and self._queue not in self.rt.hub.listeners:
                self.rt.hub.listeners.append(self._queue)
            return
        self._queue = asyncio.Queue(maxsize=10000)
        self.rt.hub.listeners.append(self._queue)
        self._task = asyncio.create_task(self._drain(), name="netmon-kiosk-hub")

    async def _unlisten(self) -> None:
        if self._queue is not None:
            with contextlib.suppress(ValueError):
                self.rt.hub.listeners.remove(self._queue)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        self._task = self._queue = None
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None

    async def _drain(self) -> None:
        assert self._queue is not None
        while True:
            msg = await self._queue.get()
            kind = msg.get("type")
            data = msg.get("data") or {}
            if kind not in REFRESH_TYPES:
                continue
            if kind == "config.changed" and data.get("entity") not in CONFIG_ENTITIES:
                continue
            if kind == "event.created" and data.get("kind") not in EVENT_KINDS:
                continue
            self.schedule_refresh()

    # ------------------------------------------------------------ refresh
    def schedule_refresh(self) -> None:
        if self._pending is not None or not self.conns:
            return
        loop = asyncio.get_running_loop()
        self._pending = loop.call_later(self.debounce_s, self._fire)

    def _fire(self) -> None:
        self._pending = None
        asyncio.ensure_future(self.refresh_all())

    async def refresh_all(self) -> None:
        for sid in list(self.conns):
            await self.refresh(sid)

    async def refresh(self, screen_id: int) -> None:
        msg = json.dumps({"type": "kiosk.refresh"})
        for ws in list(self.conns.get(screen_id, ())):
            try:
                await ws.send_text(msg)
            except Exception:  # noqa: BLE001
                self.remove(screen_id, ws)

    # ------------------------------------------------------------ connections
    def add(self, screen_id: int, ws: WebSocket) -> None:
        self.conns.setdefault(screen_id, set()).add(ws)

    def remove(self, screen_id: int, ws: WebSocket) -> None:
        s = self.conns.get(screen_id)
        if s is not None:
            s.discard(ws)
            if not s:
                self.conns.pop(screen_id, None)

    async def close_screen(self, screen_id: int, code: int = CLOSE_REVOKED) -> None:
        for ws in list(self.conns.pop(screen_id, set())):
            with contextlib.suppress(Exception):
                await ws.close(code=code)

    async def close_all(self, code: int = CLOSE_REVOKED) -> None:
        for sid in list(self.conns):
            await self.close_screen(sid, code)
