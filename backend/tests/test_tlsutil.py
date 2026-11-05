"""Corporate CA support: every outbound TLS client trusts a private CA once it is configured.

A local HTTPS (and SMTP STARTTLS) server presents a leaf certificate signed by a throwaway
private CA, like a TLS-inspecting firewall does. Without the CA every client must fail
certificate verification; with NETMON_EXTRA_CA_FILE (or the CA in the system store,
emulated with SSL_CERT_FILE on Linux) every client must succeed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from netmon import tlsutil
from netmon.alerts.email import SmtpError, send_email
from netmon.alerts.telegram import TelegramError, send_telegram
from netmon.alerts.webhook import WebhookError, send_webhook
from netmon.poller.checks import http as http_check
from netmon.poller.sensor_types import validate_config
from netmon.poller.types import CheckContext

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")

TOKEN = "123456:TEST_TOKEN_tls_0123456789abcdef"


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Private root CA + a leaf for 127.0.0.1/localhost signed by it."""
    d = tmp_path_factory.mktemp("pki")
    _openssl(
        "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
        "-subj", "/CN=NetMon Test Corporate Root CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout", "ca.key", "-out", "ca.pem",
        cwd=d,
    )  # fmt: skip
    _openssl(
        "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
        "-keyout", "leaf.key", "-out", "leaf.csr",
        cwd=d,
    )  # fmt: skip
    (d / "leaf.ext").write_text(
        "basicConstraints=CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=IP:127.0.0.1,DNS:localhost\n"
    )
    _openssl(
        "x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
        "-CAcreateserial", "-days", "2", "-extfile", "leaf.ext", "-out", "leaf.pem",
        cwd=d,
    )  # fmt: skip
    return {"ca": d / "ca.pem", "cert": d / "leaf.pem", "key": d / "leaf.key"}


@pytest.fixture
def server_ctx(pki: dict[str, Path]) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(pki["cert"], pki["key"])
    return ctx


@pytest.fixture(autouse=True)
def fresh_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test starts with no extra CA and rebuilds the shared context."""
    monkeypatch.delenv(tlsutil.EXTRA_CA_ENV, raising=False)
    tlsutil.client_ssl_context.cache_clear()
    yield
    tlsutil.client_ssl_context.cache_clear()


def trust_extra_ca(monkeypatch: pytest.MonkeyPatch, ca: Path) -> None:
    monkeypatch.setenv(tlsutil.EXTRA_CA_ENV, str(ca))
    tlsutil.client_ssl_context.cache_clear()


class HttpsServer:
    """Tiny HTTPS server answering every request with a Telegram-style JSON 200."""

    def __init__(self, ctx: ssl.SSLContext) -> None:
        self.ctx = ctx
        self.port = 0
        self.hits = 0
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=self.ctx)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.decode("latin-1").split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1])
            if length:
                await reader.readexactly(length)
            self.hits += 1
            body = json.dumps({"ok": True, "result": {}}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass  # client aborted the handshake (expected in the "untrusted" tests)
        finally:
            writer.close()


@pytest.fixture
async def https(server_ctx: ssl.SSLContext) -> AsyncIterator[HttpsServer]:
    srv = HttpsServer(server_ctx)
    await srv.start()
    yield srv
    await srv.stop()


# ------------------------------------------------------------------ the helper itself


def test_context_verifies_by_default() -> None:
    ctx = tlsutil.client_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert tlsutil.client_ssl_context() is ctx  # built once, shared
    assert tlsutil.httpx_verify(False) is False
    assert tlsutil.httpx_verify(True) is ctx


def test_broken_extra_ca_file_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pki: dict[str, Path]
) -> None:
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate")
    missing = tmp_path / "missing.pem"
    monkeypatch.setenv(tlsutil.EXTRA_CA_ENV, f"{junk}{os.pathsep}{missing}")
    tlsutil.client_ssl_context.cache_clear()
    assert tlsutil.client_ssl_context().verify_mode == ssl.CERT_REQUIRED


async def test_stdlib_client_with_extra_ca(
    monkeypatch: pytest.MonkeyPatch, https: HttpsServer, pki: dict[str, Path]
) -> None:
    async def handshake() -> None:
        _, w = await asyncio.open_connection(
            "127.0.0.1", https.port, ssl=tlsutil.client_ssl_context(), server_hostname="localhost"
        )
        w.close()

    with pytest.raises(ssl.SSLCertVerificationError):
        await handshake()
    trust_extra_ca(monkeypatch, pki["ca"])
    await handshake()


# ------------------------------------------------------------------ Telegram


async def test_telegram_private_ca(
    monkeypatch: pytest.MonkeyPatch, https: HttpsServer, pki: dict[str, Path]
) -> None:
    from netmon.alerts import telegram

    monkeypatch.setattr(telegram, "_api_base", https.url)
    cfg = {"bot_token": TOKEN, "chat_id": "1"}
    with pytest.raises(TelegramError) as ei:
        await send_telegram(cfg, "hi")
    assert "certificate" in str(ei.value).lower()
    assert TOKEN not in str(ei.value)
    assert https.hits == 0

    trust_extra_ca(monkeypatch, pki["ca"])
    await send_telegram(cfg, "hi")
    assert https.hits == 1


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="OpenSSL system store")
async def test_telegram_private_ca_in_system_store(
    monkeypatch: pytest.MonkeyPatch, https: HttpsServer, pki: dict[str, Path], tmp_path: Path
) -> None:
    """The CA in the OS trust store (what deploy/certs + update-ca-certificates produce).

    On Linux the system store is OpenSSL's default verify paths, which SSL_CERT_FILE
    overrides; a bundle holding only the test CA emulates "CA installed system-wide".
    """
    from netmon.alerts import telegram

    monkeypatch.setattr(telegram, "_api_base", https.url)
    cfg = {"bot_token": TOKEN, "chat_id": "1"}
    with pytest.raises(TelegramError):
        await send_telegram(cfg, "hi")

    bundle = tmp_path / "system-bundle.pem"
    bundle.write_bytes(pki["ca"].read_bytes())
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    tlsutil.client_ssl_context.cache_clear()
    await send_telegram(cfg, "hi")
    assert https.hits == 1


# ------------------------------------------------------------------ webhook


async def test_webhook_private_ca(
    monkeypatch: pytest.MonkeyPatch, https: HttpsServer, pki: dict[str, Path]
) -> None:
    with pytest.raises(WebhookError) as ei:
        await send_webhook({"url": https.url + "/hook"}, {"event": "test"})
    assert "certificate" in str(ei.value).lower()
    # verify_tls=False keeps working without any CA (unchanged behaviour)
    await send_webhook({"url": https.url + "/hook", "verify_tls": False}, {"event": "test"})
    assert https.hits == 1

    trust_extra_ca(monkeypatch, pki["ca"])
    await send_webhook({"url": https.url + "/hook"}, {"event": "test"})
    assert https.hits == 2


# ------------------------------------------------------------------ HTTPS sensor


def _http_ctx(url: str, verify_tls: bool) -> CheckContext:
    cfg = validate_config("http", {"url": url, "verify_tls": verify_tls})
    return CheckContext(1, "http", cfg, "127.0.0.1", {}, {}, timeout_s=30)


async def test_http_sensor_private_ca(
    monkeypatch: pytest.MonkeyPatch, https: HttpsServer, pki: dict[str, Path]
) -> None:
    url = f"https://localhost:{https.port}/"
    r = await http_check.run(_http_ctx(url, True))
    assert not r.ok and "certificate" in r.message.lower(), r.message
    r = await http_check.run(_http_ctx(url, False))
    assert r.ok, r.message

    trust_extra_ca(monkeypatch, pki["ca"])
    r = await http_check.run(_http_ctx(url, True))
    assert r.ok, r.message
    assert r.values["ssl_days_left"] is not None and r.values["ssl_days_left"] > 0


# ------------------------------------------------------------------ SMTP STARTTLS


@pytest.fixture
def starttls_server(server_ctx: ssl.SSLContext) -> Iterator[tuple[int, list[Any]]]:
    from aiosmtpd.controller import Controller

    messages: list[Any] = []

    class Handler:
        async def handle_DATA(self, server, session, envelope):  # type: ignore[no-untyped-def] # noqa: N802
            messages.append(envelope)
            return "250 OK"

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    ctl = Controller(
        Handler(),
        hostname="127.0.0.1",
        port=port,
        server_kwargs={"tls_context": server_ctx, "require_starttls": True},
    )
    ctl.start()
    yield port, messages
    ctl.stop()


async def test_smtp_starttls_private_ca(
    monkeypatch: pytest.MonkeyPatch, starttls_server: tuple[int, list[Any]], pki: dict[str, Path]
) -> None:
    port, messages = starttls_server
    smtp = {
        "host": "localhost",
        "port": port,
        "security": "starttls",
        "from_address": "netmon@example.com",
    }
    with pytest.raises(SmtpError) as ei:
        await send_email(smtp, ["ops@example.com"], "s", "t")
    assert "certificate" in str(ei.value).lower()
    assert messages == []

    trust_extra_ca(monkeypatch, pki["ca"])
    await send_email(smtp, ["ops@example.com"], "s", "t")
    assert len(messages) == 1
