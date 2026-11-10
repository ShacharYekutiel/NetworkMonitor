"""[2b-3] SIEM forwarding (architecture section 12.5): pure RFC 5424 rendering and one
asyncio task per target with a bounded queue (never blocks ingestion)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import ip_address, ip_network
from typing import Any

from netmon.ingest.limits import FORWARD_QUEUE

log = logging.getLogger("netmon.ingest.forward")

UDP_MAX = 8192
BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)
TRAP_FACILITY = 16  # local0
SEVERITY_BY_STATUS = {None: 5, "warning": 4, "down": 2}


def _ts(dt: datetime) -> str:
    dt = dt.astimezone(UTC)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        base += "." + f"{dt.microsecond:06d}".rstrip("0")
    return base + "Z"


def _field(value: str | None, limit: int) -> str:
    """An RFC 5424 header field: PRINTUSASCII (33-126), `-` when empty."""
    if not value:
        return "-"
    out = "".join(c if 33 <= ord(c) <= 126 else "_" for c in value)[:limit]
    return out or "-"


def _sd_value(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")


def origin_sd(source_ip: str, version: str) -> str:
    return (
        f'[origin ip="{_sd_value(source_ip)}" software="NetworkMonitor" '
        f'swVersion="{_sd_value(version)}"]'
    )


def trap_severity(raised_status: str | None) -> int:
    return SEVERITY_BY_STATUS.get(raised_status, 5)


def render_5424(rec: dict[str, Any], version: str) -> str:
    """One forwarded line (no framing). `rec`: the pipeline record dict (kind `syslog` or
    `trap`), see pipeline.Record."""
    sd_origin = origin_sd(str(rec["source_ip"]), version)
    if rec["kind"] == "syslog":
        pri = int(rec["facility"]) * 8 + int(rec["severity"])
        ts = rec.get("device_time") or rec["time"]
        host = rec.get("hostname") or str(rec["source_ip"])
        sd = (rec.get("structured_data") or "") + sd_origin
        return (
            f"<{pri}>1 {_ts(ts)} {_field(host, 255)} {_field(rec.get('program'), 48)} "
            f"{_field(rec.get('pid'), 128)} {_field(rec.get('msgid'), 32)} {sd} "
            f"{rec.get('message') or ''}"
        )
    pri = TRAP_FACILITY * 8 + trap_severity(rec.get("raised_status"))
    host = rec.get("device_host") or str(rec["source_ip"])
    return (
        f"<{pri}>1 {_ts(rec['time'])} {_field(host, 255)} snmptrap - "
        f"{_field(rec.get('trap_name'), 32)} {sd_origin} {rec.get('summary') or ''}"
    )


def record_severity(rec: dict[str, Any]) -> int:
    if rec["kind"] == "syslog":
        return int(rec["severity"])
    return trap_severity(rec.get("raised_status"))


def frame(line: str, protocol: str) -> bytes:
    data = line.encode("utf-8")
    if protocol == "udp":
        if len(data) > UDP_MAX:
            data = data[:UDP_MAX].decode("utf-8", errors="ignore").encode("utf-8")
        return data
    return str(len(data)).encode("ascii") + b" " + data


@dataclass
class Target:
    id: str
    name: str
    enabled: bool
    host: str
    port: int
    protocol: str
    tls_verify: bool
    kinds: list[str]
    min_severity: int
    sources: list[str]
    include_unknown: bool

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Target:
        return cls(
            id=str(d.get("id") or ""),
            name=str(d.get("name") or ""),
            enabled=bool(d.get("enabled", True)),
            host=str(d.get("host") or ""),
            port=int(d.get("port") or 514),
            protocol=str(d.get("protocol") or "udp"),
            tls_verify=bool(d.get("tls_verify", True)),
            kinds=list(d.get("kinds") or ["syslog", "trap"]),
            min_severity=int(d.get("min_severity", 7)),
            sources=list(d.get("sources") or []),
            include_unknown=bool(d.get("include_unknown", False)),
        )

    def accepts(self, rec: dict[str, Any]) -> bool:
        if rec["kind"] not in self.kinds:
            return False
        if rec.get("device_id") is None and not self.include_unknown:
            return False
        if record_severity(rec) > self.min_severity:
            return False
        if self.sources:
            try:
                addr = ip_address(str(rec["source_ip"]))
            except ValueError:
                return False
            ok = False
            for s in self.sources:
                try:
                    if addr in ip_network(s, strict=False):
                        ok = True
                        break
                except ValueError:
                    continue
            if not ok:
                return False
        return True


def new_target_id() -> str:
    return secrets.token_hex(3)


class Forwarder:
    """One SIEM target: bounded queue, own task, reconnect with backoff."""

    def __init__(self, target: Target, version: str) -> None:
        self.target = target
        self.version = version
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=FORWARD_QUEUE)
        self.sent = 0
        self.dropped = 0
        self.errors = 0
        self.state = "idle" if target.enabled else "disabled"
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._udp: asyncio.DatagramTransport | None = None

    def status(self) -> dict[str, Any]:
        return {
            "id": self.target.id,
            "name": self.target.name,
            "state": self.state,
            "sent": self.sent,
            "dropped": self.dropped,
            "errors": self.errors,
            "queue": self.queue.qsize(),
            "last_error": self.last_error,
        }

    def offer(self, rec: dict[str, Any]) -> bool:
        """Never blocks; returns True when the record was queued."""
        if not self.target.enabled or not self.target.accepts(rec):
            return False
        try:
            line = render_5424(rec, self.version)
        except Exception:  # noqa: BLE001 - a bad record is never fatal
            self.errors += 1
            return False
        try:
            self.queue.put_nowait(line)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            return False

    def start(self) -> None:
        if self.target.enabled and self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"netmon-forward-{self.target.id}")

    async def stop(self, grace_s: float = 2.0) -> None:
        if self._task is not None:
            # give the queue a moment to drain, then cancel
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._drained(), grace_s)
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None
        await self._close()
        self.state = "disabled"

    async def _drained(self) -> None:
        while not self.queue.empty() and self.state == "connected":  # noqa: ASYNC110
            await asyncio.sleep(0.05)

    async def _close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._writer.wait_closed(), 1)
            self._writer = None
        if self._udp is not None:
            self._udp.close()
            self._udp = None

    async def _connect(self) -> None:
        t = self.target
        self.state = "connecting"
        if t.protocol == "udp":
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol, remote_addr=(t.host, t.port)
            )
            self._udp = transport  # type: ignore[assignment]
        else:
            ctx: ssl.SSLContext | None = None
            if t.protocol == "tls":
                ctx = await asyncio.to_thread(client_ssl, t.tls_verify)  # reads trust stores
            _, self._writer = await asyncio.wait_for(
                asyncio.open_connection(
                    t.host, t.port, ssl=ctx, server_hostname=t.host if ctx else None
                ),
                10,
            )
        self.state = "connected"
        self.last_error = None

    async def _send(self, line: str) -> None:
        data = frame(line, self.target.protocol)
        if self._udp is not None:
            self._udp.sendto(data)
            return
        assert self._writer is not None
        self._writer.write(data)
        await asyncio.wait_for(self._writer.drain(), 10)

    async def _run(self) -> None:
        attempt = 0
        pending: str | None = None
        while True:
            try:
                if self._udp is None and self._writer is None:
                    await self._connect()
                    attempt = 0
                if pending is None:
                    pending = await self.queue.get()
                await self._send(pending)
                self.sent += 1
                pending = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self.state = "error"
                self.last_error = (str(exc) or type(exc).__name__)[:300]
                await self._close()
                delay = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
                attempt += 1
                log.debug("SIEM target %s: %s; retrying in %s s", self.target.name, exc, delay)
                await asyncio.sleep(delay)


def client_ssl(verify: bool) -> ssl.SSLContext:
    from netmon import tlsutil

    if verify:
        return tlsutil.client_ssl_context()  # shared, cached: never mutated here
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # tls_verify false (UI warning)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def send_test(target: Target, version: str, timeout_s: float = 10.0) -> None:
    """One test line (raises on connect / TLS / send errors)."""
    line = render_5424(
        {
            "kind": "syslog",
            "facility": 16,
            "severity": 5,
            "time": datetime.now(UTC),
            "device_time": None,
            "hostname": "netmon",
            "program": "netmon",
            "pid": "",
            "msgid": "TEST",
            "structured_data": None,
            "message": "NetworkMonitor SIEM forwarding test message",
            "source_ip": "127.0.0.1",
        },
        version,
    )
    data = frame(line, target.protocol)
    if target.protocol == "udp":
        loop = asyncio.get_running_loop()
        transport, _ = await asyncio.wait_for(
            loop.create_datagram_endpoint(
                asyncio.DatagramProtocol, remote_addr=(target.host, target.port)
            ),
            timeout_s,
        )
        try:
            transport.sendto(data)
            await asyncio.sleep(0)
        finally:
            transport.close()
        return
    ctx = (
        await asyncio.to_thread(client_ssl, target.tls_verify) if target.protocol == "tls" else None
    )
    _, writer = await asyncio.wait_for(
        asyncio.open_connection(
            target.host, target.port, ssl=ctx, server_hostname=target.host if ctx else None
        ),
        timeout_s,
    )
    try:
        writer.write(data)
        await asyncio.wait_for(writer.drain(), timeout_s)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), 2)
