#!/usr/bin/env python3
"""SIEM sink for the 2b-3 forwarding tests (docs/architecture.md §12.5, §12.14): UDP, TCP and TLS listeners
that record every received syslog line, plus a plain-HTTP control API.

    python tests/sim/ingest/siem_sink.py --udp-port 15514 --tcp-port 15515 --tls-port 16514 \
        --certs DIR --control-port 18514 --out sink.jsonl

TCP/TLS frames are parsed per RFC 6587: a frame starting with a digit is octet-counted (`LEN SP MSG`),
otherwise LF-terminated (a trailing CR is stripped). Every record: {"time", "transport": "udp"|"tcp"|"tls",
"peer", "framing": "datagram"|"octet"|"lf", "line"} (one JSON line in --out).

Control (127.0.0.1:--control-port): GET /health, GET /records (JSON list; ?transport=tcp filters),
DELETE /records, POST /down (close all listeners and connections: "sink down"), POST /up (re-open on the same
ports), GET /state. Prints "READY udp=<p> tcp=<p> tls=<p> control=<p>" once listening.
--certs DIR = make_certs.py output (tests/sim/ldap/make_certs.py; the backend trusts ca.pem).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "ldap"))


class Sink:
    def __init__(self, out: Path | None, certs_dir: str | None, host: str = "127.0.0.1"):
        self.out = out
        self.records: list[dict] = []
        self.host = host
        self.certs_dir = certs_dir
        self.ports = {"udp": 0, "tcp": 0, "tls": 0}
        self._servers: list = []
        self._udp: asyncio.DatagramTransport | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self.down = False

    def add(self, transport: str, peer: str, framing: str, line: bytes) -> None:
        rec = {"time": time.time(), "transport": transport, "peer": peer, "framing": framing,
               "line": line.decode("utf-8", "replace")}
        self.records.append(rec)
        if self.out:
            with self.out.open("a") as f:
                f.write(json.dumps(rec) + "\n")

    async def start(self, udp: int | None, tcp: int | None, tls: int | None) -> None:
        loop = asyncio.get_running_loop()
        sink = self

        class Udp(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple) -> None:
                sink.add("udp", f"{addr[0]}:{addr[1]}", "datagram", data)

        if udp is not None:
            tr, _ = await loop.create_datagram_endpoint(Udp, local_addr=(self.host, udp))
            self._udp = tr
            self.ports["udp"] = tr.get_extra_info("sockname")[1]
        if tcp is not None:
            srv = await asyncio.start_server(lambda r, w: self._stream(r, w, "tcp"), self.host, tcp)
            self.ports["tcp"] = srv.sockets[0].getsockname()[1]
            self._servers.append(srv)
        if tls is not None and self.certs_dir:
            from make_certs import make_certs

            c = make_certs(self.certs_dir)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(c.server, c.server_key)
            srv = await asyncio.start_server(lambda r, w: self._stream(r, w, "tls"), self.host, tls, ssl=ctx)
            self.ports["tls"] = srv.sockets[0].getsockname()[1]
            self._servers.append(srv)
        self.down = False

    async def stop(self) -> None:
        if self._udp:
            self._udp.close()
            self._udp = None
        for s in self._servers:
            s.close()
        for w in list(self._writers):
            with contextlib.suppress(Exception):
                w.close()
        self._servers = []
        self.down = True

    async def _stream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, transport: str) -> None:
        self._writers.add(writer)
        peer = writer.get_extra_info("peername")
        peer_s = f"{peer[0]}:{peer[1]}" if peer else "?"
        buf = b""
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buf += chunk
                while buf:
                    if buf[:1].isdigit():
                        sp = buf.find(b" ")
                        if sp < 0:
                            break
                        n = int(buf[:sp])
                        if len(buf) < sp + 1 + n:
                            break
                        self.add(transport, peer_s, "octet", buf[sp + 1 : sp + 1 + n])
                        buf = buf[sp + 1 + n :]
                    else:
                        nl = buf.find(b"\n")
                        if nl < 0:
                            break
                        self.add(transport, peer_s, "lf", buf[:nl].rstrip(b"\r"))
                        buf = buf[nl + 1 :]
        except (ConnectionError, ssl.SSLError, OSError):
            pass
        finally:
            self._writers.discard(writer)
            with contextlib.suppress(Exception):
                writer.close()


async def control(sink: Sink, wanted: dict, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        line = (await reader.readline()).decode("latin-1").strip()
        while (await reader.readline()).strip():
            pass
        method, target = (line.split(" ") + ["", ""])[:2]
        url = urlsplit(target)
        q = parse_qs(url.query)
        code, body = 200, {"ok": True}
        if url.path == "/health":
            pass
        elif url.path == "/records" and method == "GET":
            recs = sink.records
            if "transport" in q:
                recs = [r for r in recs if r["transport"] in q["transport"]]
            body = recs
        elif url.path == "/records" and method == "DELETE":
            sink.records.clear()
            if sink.out:
                sink.out.write_text("")
        elif url.path == "/down" and method == "POST":
            await sink.stop()
        elif url.path == "/up" and method == "POST":
            if sink.down:
                await sink.start(*(sink.ports[k] if wanted[k] is not None else None for k in ("udp", "tcp", "tls")))
        elif url.path == "/state":
            body = {"down": sink.down, "ports": sink.ports, "records": len(sink.records)}
        else:
            code, body = 404, {"error": "not found"}
        data = json.dumps(body).encode()
        writer.write(f"HTTP/1.1 {code} X\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                     f"Connection: close\r\n\r\n".encode() + data)
        await writer.drain()
    finally:
        writer.close()


async def amain(a: argparse.Namespace) -> None:
    out = Path(a.out) if a.out else None
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("")
    sink = Sink(out, a.certs or None, a.host)
    wanted = {"udp": a.udp_port if a.udp_port >= 0 else None, "tcp": a.tcp_port if a.tcp_port >= 0 else None,
              "tls": a.tls_port if a.tls_port >= 0 and a.certs else None}
    await sink.start(wanted["udp"], wanted["tcp"], wanted["tls"])
    if a.control_port:
        await asyncio.start_server(lambda r, w: control(sink, wanted, r, w), "127.0.0.1", a.control_port)
    p = sink.ports
    print(f"READY udp={p['udp']} tcp={p['tcp']} tls={p['tls']} control={a.control_port}", flush=True)
    await asyncio.Event().wait()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--udp-port", type=int, default=0, help="0 = free port, -1 = off")
    ap.add_argument("--tcp-port", type=int, default=0)
    ap.add_argument("--tls-port", type=int, default=0)
    ap.add_argument("--certs", default="")
    ap.add_argument("--control-port", type=int, default=0)
    ap.add_argument("--out", default="")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    main()
