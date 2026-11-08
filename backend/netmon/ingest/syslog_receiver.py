"""[2b-3] Syslog receivers: UDP endpoint and TCP server with RFC 6587 framing, connection
limits and an idle timeout. Frames go to the pipeline's raw queue; nothing is parsed here."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from netmon.ingest.attribution import normalize_ip
from netmon.ingest.limits import (
    TCP_IDLE_TIMEOUT_S,
    TCP_MAX_CONNECTIONS,
    TCP_MAX_PER_SOURCE,
)
from netmon.ingest.syslog_parse import FrameTooLarge, TcpFramer
from netmon.ingest.trap_receiver import UdpReceiver

if TYPE_CHECKING:
    from netmon.ingest.pipeline import Ingest

log = logging.getLogger("netmon.ingest.syslog")

__all__ = ["SyslogTcp", "UdpReceiver"]


class SyslogTcp:
    def __init__(self, ingest: Ingest, port: int) -> None:
        self.ingest = ingest
        self.port = port
        self.server: asyncio.base_events.Server | None = None
        self.state = "disabled"
        self.error: str | None = None
        self.since: Any = None
        self.wanted = False
        self.connections: dict[asyncio.Task[Any], tuple[str, asyncio.StreamWriter]] = {}
        self.status_kind = "syslog_tcp"

    @property
    def listening(self) -> bool:
        return self.server is not None

    @property
    def count(self) -> int:
        return len(self.connections)

    async def start(self, host: str) -> bool:
        self.wanted = True
        if self.server is not None:
            return True
        if self.port == 0:
            self._set("disabled", None)
            return True
        try:
            self.server = await asyncio.wait_for(
                asyncio.start_server(self._handle, host, self.port, limit=1 << 17), 5
            )
        except (OSError, TimeoutError) as exc:
            self._set("error", str(exc))
            return False
        self._set("listening", None)
        return True

    async def stop(self) -> None:
        self.wanted = False
        if self.server is not None:
            self.server.close()
            self.server = None
        for task, (_, writer) in list(self.connections.items()):
            writer.close()
            task.cancel()
        for task in list(self.connections):
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, 2)
        self.connections.clear()
        self._set("disabled", None)

    def _set(self, state: str, error: str | None) -> None:
        changed = state != self.state or error != self.error
        self.state, self.error = state, error
        if changed:
            self.since = self.ingest.rt.clock.now()
            self.ingest.receiver_changed(self)

    def status(self) -> dict[str, Any]:
        from netmon.services.serialize import iso

        return {
            "kind": "syslog_tcp",
            "port": self.port,
            "enabled": self.wanted,
            "state": self.state,
            "error": self.error,
            "since": iso(self.since),
        }

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        ip = normalize_ip(str(peer[0])) or str(peer[0])
        task = asyncio.current_task()
        assert task is not None
        per_source = sum(1 for src, _ in self.connections.values() if src == ip)
        if len(self.connections) >= TCP_MAX_CONNECTIONS or per_source >= TCP_MAX_PER_SOURCE:
            self.ingest.counters["syslog"].drop("rate_limited")
            writer.close()
            return
        self.connections[task] = (ip, writer)
        framer = TcpFramer()
        try:
            while True:
                data = await asyncio.wait_for(reader.read(65536), TCP_IDLE_TIMEOUT_S)
                if not data:
                    break
                try:
                    frames = framer.feed(data)
                except FrameTooLarge:
                    self.ingest.counters["syslog"].received += 1
                    self.ingest.counters["syslog"].drop("too_large")
                    break
                for frame in frames:
                    await self.ingest.accept_tcp(frame, ip, peer)
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.debug("Syslog TCP connection from %s failed", ip, exc_info=True)
        finally:
            self.connections.pop(task, None)
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 1)
