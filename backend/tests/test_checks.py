"""Network check plugins against local targets (ping, http(s), tcp, dns)."""

from __future__ import annotations

import asyncio
import os
import shutil
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from netmon.poller.checks import dns as dns_check
from netmon.poller.checks import http as http_check
from netmon.poller.checks import ping as ping_check
from netmon.poller.checks import tcp_port
from netmon.poller.sensor_types import validate_config
from netmon.poller.types import CheckContext


def ctx(type_: str, config: dict, host: str = "127.0.0.1") -> CheckContext:
    return CheckContext(1, type_, validate_config(type_, config), host, {}, {}, timeout_s=30)


# ------------------------------------------------------------------ ping
async def test_ping_localhost() -> None:
    r = await ping_check.run(ctx("ping", {"count": 2, "timeout_ms": 500}))
    assert r.ok, r.message
    assert r.values["packet_loss_pct"] == 0.0 and r.values["latency_ms"] >= 0


async def test_ping_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    import icmplib

    async def no_reply(address, count=4, **kw):  # type: ignore[no-untyped-def]
        return icmplib.Host(address, count, [])

    monkeypatch.setattr(icmplib, "async_ping", no_reply)
    monkeypatch.setattr(ping_check, "_mode", None)
    r = await ping_check.run(ctx("ping", {"count": 2, "timeout_ms": 200}, host="192.0.2.1"))
    assert not r.ok and r.message == "Host unreachable"
    monkeypatch.setattr(ping_check, "_mode", None)


async def test_ping_unresolvable() -> None:
    r = await ping_check.run(
        ctx("ping", {"count": 1, "timeout_ms": 200}, host="no-such-host.invalid")
    )
    assert not r.ok


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell fake")
async def test_ping_binary_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import icmplib

    if not shutil.which("ping"):  # provide a fake system ping printing Linux-style output
        fake = tmp_path / "ping"
        fake.write_text(
            "#!/bin/sh\n"
            "echo '64 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=0.050 ms'\n"
            "echo '64 bytes from 127.0.0.1: icmp_seq=2 ttl=64 time=0.070 ms'\n"
        )
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    async def denied(*a, **kw):  # type: ignore[no-untyped-def]
        raise icmplib.SocketPermissionError(False)

    monkeypatch.setattr(icmplib, "async_ping", denied)
    monkeypatch.setattr(ping_check, "_mode", None)
    r = await ping_check.run(ctx("ping", {"count": 2, "timeout_ms": 1000}))
    assert ping_check._mode == "binary"  # noqa: SLF001
    assert r.ok, r.message
    monkeypatch.setattr(ping_check, "_mode", None)


# ------------------------------------------------------------------ http
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path == "/missing":
            self.send_response(404)
            self.end_headers()
            return
        if self.path == "/slow":
            import time

            time.sleep(2)
        body = b"<html>Welcome to NetworkMonitor test</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()


def _serve(server: ThreadingHTTPServer) -> Iterator[int]:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def http_port() -> Iterator[int]:
    yield from _serve(ThreadingHTTPServer(("127.0.0.1", 0), _Handler))


@pytest.fixture(scope="module")
def https_port(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    d: Path = tmp_path_factory.mktemp("tls")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "30",
            "-subj",
            "/CN=localhost",
            "-keyout",
            str(d / "k.pem"),
            "-out",
            str(d / "c.pem"),
        ],
        check=True,
        capture_output=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(d / "c.pem", d / "k.pem")
    server.socket = sctx.wrap_socket(server.socket, server_side=True)
    yield from _serve(server)


async def test_http_ok_keyword_and_status(http_port: int) -> None:
    base = f"http://{{host}}:{http_port}"
    r = await http_check.run(ctx("http", {"url": base + "/", "keyword": "Welcome"}))
    assert r.ok, r.message
    assert r.values["status_code"] == 200 and r.values["response_time_ms"] > 0
    assert "ssl_days_left" not in r.values
    r = await http_check.run(ctx("http", {"url": base + "/", "keyword": "Missing"}))
    assert not r.ok and "Keyword not found" in r.message
    r = await http_check.run(
        ctx("http", {"url": base + "/", "keyword": "Welcome", "keyword_must_exist": False})
    )
    assert not r.ok
    r = await http_check.run(ctx("http", {"url": base + "/missing"}))
    assert not r.ok and "404" in r.message
    r = await http_check.run(ctx("http", {"url": base + "/missing", "expected_status": [404]}))
    assert r.ok
    r = await http_check.run(ctx("http", {"url": base + "/redirect", "follow_redirects": False}))
    assert r.ok and r.values["status_code"] == 302
    r = await http_check.run(ctx("http", {"url": base + "/redirect", "keyword": "Welcome"}))
    assert r.ok and r.values["status_code"] == 200
    r = await http_check.run(ctx("http", {"url": base + "/", "method": "HEAD"}))
    assert r.ok
    r = await http_check.run(ctx("http", {"url": base + "/slow", "timeout_s": 1}))
    assert not r.ok and "timed out" in r.message


async def test_http_connection_refused() -> None:
    srv = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    srv.close()
    await srv.wait_closed()
    r = await http_check.run(ctx("http", {"url": f"http://127.0.0.1:{port}/"}))
    assert not r.ok and "Connection failed" in r.message


async def test_https_cert_days(https_port: int) -> None:
    url = f"https://localhost:{https_port}/"
    r = await http_check.run(ctx("http", {"url": url, "verify_tls": False}))
    assert r.ok, r.message
    assert 29 < r.values["ssl_days_left"] <= 30
    # self-signed + verify -> failure, but ssl_days_left is still reported
    r = await http_check.run(ctx("http", {"url": url}))
    assert not r.ok
    assert 29 < r.values["ssl_days_left"] <= 30


# ------------------------------------------------------------------ tcp
async def test_tcp_port_open_and_refused() -> None:
    srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    r = await tcp_port.run(ctx("tcp_port", {"port": port}))
    assert r.ok and r.values["connect_time_ms"] > 0
    srv.close()
    await srv.wait_closed()
    r = await tcp_port.run(ctx("tcp_port", {"port": port}))
    assert not r.ok and "refused" in r.message
    r = await tcp_port.run(ctx("tcp_port", {"port": 80, "timeout_s": 1}, host="192.0.2.1"))
    assert not r.ok


# ------------------------------------------------------------------ dns
@pytest.fixture(scope="module")
def dns_port() -> Iterator[int]:
    from dnslib import QTYPE, RR, A
    from dnslib.server import BaseResolver, DNSServer

    zone = RR.fromZone(
        "example.test. 60 A 10.1.2.3\n"
        "example.test. 60 MX 10 mail.example.test.\n"
        'example.test. 60 TXT "v=spf1 -all"\n'
        "alias.example.test. 60 CNAME example.test.\n"
    )

    class Resolver(BaseResolver):
        def resolve(self, request, handler):  # type: ignore[no-untyped-def]
            reply = request.reply()
            qname, qtype = request.q.qname, QTYPE[request.q.qtype]
            found = False
            for rr in zone:
                if rr.rname == qname:
                    found = True
                    if QTYPE[rr.rtype] == qtype:
                        reply.add_answer(rr)
            if not found:
                reply.header.rcode = 3  # NXDOMAIN
            _ = A
            return reply

    server = DNSServer(Resolver(), port=0, address="127.0.0.1")
    server.start_thread()
    port = server.server.server_address[1]
    yield port
    server.stop()


async def test_dns(dns_port: int) -> None:
    r = await dns_check.run(
        ctx("dns", {"query_name": "example.test", "port": dns_port, "expected": "10.1.2.3"})
    )
    assert r.ok, r.message
    assert r.values["resolve_time_ms"] > 0
    r = await dns_check.run(
        ctx(
            "dns",
            {
                "query_name": "example.test",
                "port": dns_port,
                "record_type": "MX",
                "expected": "10 MAIL.example.test.",
            },
        )
    )
    assert r.ok, r.message
    r = await dns_check.run(
        ctx(
            "dns",
            {
                "query_name": "example.test",
                "port": dns_port,
                "record_type": "TXT",
                "expected": "v=spf1 -all",
            },
        )
    )
    assert r.ok, r.message
    r = await dns_check.run(
        ctx("dns", {"query_name": "example.test", "port": dns_port, "expected": "10.9.9.9"})
    )
    assert not r.ok and "Expected" in r.message
    r = await dns_check.run(ctx("dns", {"query_name": "nope.example.test", "port": dns_port}))
    assert not r.ok and r.message == "NXDOMAIN"
    r = await dns_check.run(
        ctx("dns", {"query_name": "example.test", "port": dns_port, "record_type": "AAAA"})
    )
    assert not r.ok and r.message == "No answer"
    r = await dns_check.run(
        ctx(
            "dns",
            {"query_name": "example.test", "port": dns_port, "server": "127.0.0.1"},
            host="10.255.255.1",
        )
    )
    assert r.ok
    # timeout: a UDP port where nobody answers
    r = await dns_check.run(ctx("dns", {"query_name": "example.test", "port": 9, "timeout_s": 1}))
    assert not r.ok
