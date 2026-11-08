"""[3] Flow receivers: asyncio UDP endpoints (NetFlow v5/v9 + IPFIX on 2055 and 4739, sFlow
on 6343). The callback only hands the datagram to the collector (never decodes, never
blocks); bind errors give state `error` and are retried every 60 s by the collector."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from typing import TYPE_CHECKING, Any

from netmon.flows.limits import RCVBUF
from netmon.ingest.attribution import normalize_ip

if TYPE_CHECKING:
    from netmon.flows.service import FlowCollector

log = logging.getLogger("netmon.flows.receiver")


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, collector: FlowCollector, sflow: bool) -> None:
        self.collector = collector
        self.sflow = sflow

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        sock = transport.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)

    def datagram_received(self, data: bytes, addr: tuple[Any, ...]) -> None:
        ip = normalize_ip(str(addr[0])) or str(addr[0])
        self.collector.accept(data, ip, self.sflow)

    def error_received(self, exc: Exception) -> None:
        log.debug("Flow UDP socket error: %s", exc)


class FlowReceiver:
    """One UDP listener with its ReceiverStatus (kinds "netflow", "ipfix", "sflow")."""

    def __init__(self, collector: FlowCollector, kind: str, port: int) -> None:
        self.collector = collector
        self.kind = kind
        self.status_kind = kind
        self.sflow = kind == "sflow"
        self.port = port
        self.transport: asyncio.DatagramTransport | None = None
        self.state = "disabled"
        self.error: str | None = None
        self.since: Any = None
        self.wanted = False

    @property
    def listening(self) -> bool:
        return self.transport is not None

    @property
    def bound_port(self) -> int | None:
        if self.transport is None:
            return None
        sock = self.transport.get_extra_info("sockname")
        return int(sock[1]) if sock else None

    async def start(self, host: str) -> bool:
        self.wanted = True
        if self.transport is not None:
            return True
        if self.port == 0:
            self._set("disabled", None)
            return True
        loop = asyncio.get_running_loop()
        try:
            transport, _ = await asyncio.wait_for(
                loop.create_datagram_endpoint(
                    lambda: _Protocol(self.collector, self.sflow), local_addr=(host, self.port)
                ),
                5,
            )
        except (OSError, TimeoutError) as exc:
            self._set("error", str(exc) or type(exc).__name__)
            return False
        self.transport = transport  # type: ignore[assignment]
        self._set("listening", None)
        return True

    async def stop(self) -> None:
        self.wanted = False
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        self._set("disabled", None)

    def _set(self, state: str, error: str | None) -> None:
        changed = state != self.state or error != self.error
        self.state, self.error = state, error
        if changed:
            self.since = self.collector.rt.clock.now()
            self.collector.receiver_changed(self)

    def status(self) -> dict[str, Any]:
        from netmon.services.serialize import iso

        return {
            "kind": self.kind,
            "port": self.port,
            "enabled": self.wanted,
            "state": self.state,
            "error": self.error,
            "since": iso(self.since),
        }
