"""[1.4.1] SSRF egress filter (pentest finding 3): webhook channel, Teams channel, HTTP sensor.

Loopback targets are allowed suite-wide (conftest); every test here switches the filter back to
its production default with `production_filter`.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import ssl
import subprocess
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import asyncpg
import httpx
import pytest

from netmon.alerts.teams import TeamsError, send_teams
from netmon.alerts.webhook import WebhookError, send_webhook
from netmon.poller.checks import http as http_check
from netmon.services import security as security_svc
from netmon.util import egress
from tests.conftest import _plain, make_device, requires_db
from tests.test_checks import ctx

METADATA = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"


@pytest.fixture
def production_filter() -> Iterator[None]:
    """The filter as a fresh install runs it: loopback blocked, nothing forced."""
    egress.override_loopback(None)
    egress.configure(allow_loopback=False)
    yield
    egress.configure(allow_loopback=False)
    egress.override_loopback(True)


@pytest.fixture
def loopback_on() -> Iterator[None]:
    """The setting `egress_allow_loopback` ON (metadata / link-local stay blocked)."""
    egress.override_loopback(None)
    egress.configure(allow_loopback=True)
    yield
    egress.configure(allow_loopback=False)
    egress.override_loopback(True)


def fake_dns(monkeypatch: pytest.MonkeyPatch, table: dict[str, list[str]]) -> list[str]:
    """Replace the resolver; returns the list of names that were looked up."""
    seen: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        seen.append(host)
        if host not in table:
            raise OSError(f"no such host {host}")
        return table[host]

    monkeypatch.setattr(egress, "_resolve", resolve)
    return seen


class _Target(BaseHTTPRequestHandler):
    redirect_to: str | None = None
    seen: list[dict[str, str]] = []

    def _answer(self) -> None:
        type(self).seen.append({"path": self.path, "host": self.headers.get("Host", "")})
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path.startswith("/redirect") and type(self).redirect_to:
            self.send_response(302)
            self.send_header("Location", type(self).redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer  # noqa: N815

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def target() -> Iterator[tuple[int, type[_Target]]]:
    handler = type("Handler", (_Target,), {"seen": [], "redirect_to": None})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], handler
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- the address rules
@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",  # cloud metadata
        "169.254.10.20",  # link-local
        "fe80::1",
        "[fe80::1]",
        "100.100.100.200",  # Alibaba metadata
        "metadata.google.internal",
        "metadata",
        "0.0.0.0",
        "::",
        "224.0.0.1",  # multicast
        "::ffff:169.254.169.254",  # IPv4-mapped
        "127.0.0.1",
        "127.8.9.10",
        "::1",
        "localhost",
        "LOCALHOST.",
    ],
)
async def test_blocked_hosts(production_filter: None, host: str) -> None:
    with pytest.raises(egress.EgressBlocked):
        await egress.checked_addresses(host, 80)


@pytest.mark.parametrize("host", ["10.1.2.3", "172.16.0.9", "192.168.1.1", "100.64.0.1", "fd00::5"])
async def test_private_ranges_are_never_blocked(production_filter: None, host: str) -> None:
    """Monitoring internal devices is the product's purpose (RFC 1918 / CGNAT / ULA)."""
    assert await egress.checked_addresses(host, 80) == [host]
    assert (
        await egress.url_block_reason(f"http://{host.join('[]') if ':' in host else host}/") is None
    )


async def test_loopback_setting_relaxes_only_loopback(loopback_on: None) -> None:
    assert await egress.checked_addresses("127.0.0.1", 80) == ["127.0.0.1"]
    assert await egress.checked_addresses("::1", 80) == ["::1"]
    for host in ("169.254.169.254", "fe80::1", "metadata.google.internal", "0.0.0.0"):
        with pytest.raises(egress.EgressBlocked):
            await egress.checked_addresses(host, 80)


async def test_hostname_resolving_to_a_blocked_address(
    production_filter: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_dns(
        monkeypatch,
        {
            "imds.evil.test": ["169.254.169.254"],
            "home.evil.test": ["127.0.0.1"],
            "mixed.evil.test": ["93.184.216.34", "169.254.169.254"],  # any blocked address
            "fine.example.test": ["10.20.30.40"],
        },
    )
    for name in ("imds.evil.test", "home.evil.test", "mixed.evil.test"):
        with pytest.raises(egress.EgressBlocked) as exc:
            await egress.checked_addresses(name, 443)
        assert name in str(exc.value)
        assert await egress.url_block_reason(f"https://{name}/x") is not None
    assert await egress.checked_addresses("fine.example.test", 443) == ["10.20.30.40"]
    # a name that does not resolve: a connection error at request time, no error at save time
    with pytest.raises(httpx.ConnectError):
        await egress.checked_addresses("nowhere.test", 443)
    assert await egress.url_block_reason("https://nowhere.test/") is None
    assert await egress.url_block_reason("http://{host}/status") is None  # sensor placeholder
    # the loopback message tells the admin which setting to use; metadata has no such hint
    with pytest.raises(egress.EgressBlocked, match="targets on this server"):
        await egress.checked_addresses("127.0.0.1", 80)
    with pytest.raises(egress.EgressBlocked) as exc:
        await egress.checked_addresses("169.254.169.254", 80)
    assert "targets on this server" not in str(exc.value)


# ---------------------------------------------------------------- connection-time pinning
async def test_connects_to_the_checked_address(
    loopback_on: None, monkeypatch: pytest.MonkeyPatch, target: tuple[int, type[_Target]]
) -> None:
    """The name is resolved once by the filter and the connection goes to that address (the
    name does not exist in real DNS, so a second lookup would fail): no TOCTOU window."""
    port, handler = target
    seen = fake_dns(monkeypatch, {"pinned.test": ["127.0.0.1"]})
    async with egress.guarded_client(trust_env=False, timeout=5) as client:
        r = await client.get(f"http://pinned.test:{port}/x")
    assert r.status_code == 200 and r.text == "hello"
    assert str(r.url) == f"http://pinned.test:{port}/x"  # the caller still sees the name
    assert handler.seen == [{"path": "/x", "host": f"pinned.test:{port}"}]  # Host header kept
    assert seen == ["pinned.test"]


async def test_dns_rebinding_cannot_reach_a_blocked_address(
    production_filter: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First answer public, second answer metadata: every request is checked against the
    address it will connect to, so the rebound answer is refused."""
    answers = iter([["10.255.255.1"], ["169.254.169.254"]])

    async def resolve(host: str, port: int) -> list[str]:
        return next(answers)

    monkeypatch.setattr(egress, "_resolve", resolve)
    assert await egress.checked_addresses("rebind.test", 80) == ["10.255.255.1"]
    async with egress.guarded_client(trust_env=False, timeout=2) as client:
        with pytest.raises(egress.EgressBlocked):
            await client.get("http://rebind.test/latest/meta-data/")


async def test_slow_dns_is_bounded_by_the_connect_timeout(
    production_filter: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        await asyncio.sleep(30)
        return ["10.0.0.1"]

    monkeypatch.setattr(egress, "_resolve", resolve)
    async with egress.guarded_client(trust_env=False, timeout=0.3) as client:
        with pytest.raises(httpx.ConnectTimeout):
            await client.get("http://slow-dns.test/")
    r = await http_check.run(ctx("http", {"url": "http://slow-dns.test/", "timeout_s": 1}))
    assert not r.ok and "timed out" in r.message


async def test_idn_host_is_resolved_and_sent_as_punycode(
    loopback_on: None, monkeypatch: pytest.MonkeyPatch, target: tuple[int, type[_Target]]
) -> None:
    port, handler = target
    seen = fake_dns(monkeypatch, {"xn--bcher-kva.test": ["127.0.0.1"]})
    async with egress.guarded_client(trust_env=False, timeout=5) as client:
        r = await client.get(f"http://bücher.test:{port}/")
    assert r.status_code == 200 and seen == ["xn--bcher-kva.test"]
    assert handler.seen[0]["host"] == f"xn--bcher-kva.test:{port}"


async def test_falls_back_to_the_next_checked_address(
    loopback_on: None, monkeypatch: pytest.MonkeyPatch, target: tuple[int, type[_Target]]
) -> None:
    port, _ = target
    fake_dns(monkeypatch, {"two.test": ["::1", "127.0.0.1"]})  # nothing listens on ::1
    async with egress.guarded_client(trust_env=False, timeout=5) as client:
        r = await client.get(f"http://two.test:{port}/")
    assert r.status_code == 200


@pytest.fixture(scope="module")
def tls_target(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[int, Path, list[str]]]:
    """HTTPS server on 127.0.0.1 with a certificate for `pinned.test` (records the SNI)."""
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    d = tmp_path_factory.mktemp("tls141")
    subprocess.run(
        [
            *("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30"),
            *("-subj", "/CN=pinned.test", "-addext", "subjectAltName=DNS:pinned.test"),
            *("-keyout", str(d / "k.pem"), "-out", str(d / "c.pem")),
        ],
        check=True,
        capture_output=True,
    )
    sni: list[str] = []
    handler = type("Handler", (_Target,), {"seen": [], "redirect_to": None})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(d / "c.pem", d / "k.pem")
    sctx.sni_callback = lambda sock, name, c: sni.append(name or "")
    server.socket = sctx.wrap_socket(server.socket, server_side=True)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], d / "c.pem", sni
    finally:
        server.shutdown()
        server.server_close()


async def test_tls_verification_and_sni_use_the_host_name(
    loopback_on: None, monkeypatch: pytest.MonkeyPatch, tls_target: tuple[int, Path, list[str]]
) -> None:
    """Pinning the address must not weaken TLS: SNI and the certificate name check use the
    original host name, not the IP address the connection goes to."""
    port, cert, sni = tls_target
    fake_dns(monkeypatch, {"pinned.test": ["127.0.0.1"], "other.test": ["127.0.0.1"]})
    verify = ssl.create_default_context(cafile=str(cert))
    async with egress.guarded_client(verify=verify, trust_env=False, timeout=5) as client:
        r = await client.get(f"https://pinned.test:{port}/")
        assert r.status_code == 200
        assert sni[-1] == "pinned.test"
        # the same server under a name its certificate does not cover: verification fails
        with pytest.raises(httpx.ConnectError, match="(?i)certificate"):
            await client.get(f"https://other.test:{port}/")
    # the HTTP sensor reads the certificate through the pinned connection as well
    r2 = await http_check.run(
        ctx("http", {"url": f"https://pinned.test:{port}/", "verify_tls": False})
    )
    assert r2.ok, r2.message
    assert 29 < r2.values["ssl_days_left"] <= 30


def test_every_transport_of_a_client_is_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails loudly if httpx moves the attributes `guard_client` relies on. A proxy from the
    environment (Teams / webhook clients honour it) is checked, not pinned."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:3128")
    monkeypatch.setenv("NO_PROXY", "internal.example.test")
    client = egress.guarded_client(trust_env=True)
    assert isinstance(client._transport, egress.GuardedTransport)  # noqa: SLF001
    assert client._transport.mode == egress.PIN  # noqa: SLF001
    mounts = [t for t in client._mounts.values() if t is not None]  # noqa: SLF001
    assert mounts and all(
        isinstance(t, egress.GuardedTransport) and t.mode == egress.CHECK for t in mounts
    )
    # every request is routed through a guarded transport
    for url in (
        "https://hooks.example.test/x",
        "http://internal.example.test/",
        "http://10.0.0.1/",
    ):
        assert isinstance(
            client._transport_for_url(httpx.URL(url)),  # noqa: SLF001
            egress.GuardedTransport,
        )


async def test_proxied_requests_are_still_checked(
    production_filter: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class Inner(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200)

    fake_dns(monkeypatch, {"imds.evil.test": ["169.254.169.254"]})
    t = egress.GuardedTransport(Inner(), mode=egress.CHECK)
    for url in (METADATA, "http://imds.evil.test/", "http://localhost:8080/"):
        with pytest.raises(egress.EgressBlocked):
            await t.handle_async_request(httpx.Request("GET", url))
    # a name only the proxy can resolve goes through unchanged (the proxy connects)
    r = await t.handle_async_request(httpx.Request("GET", "https://hooks.partner.test/x"))
    assert r.status_code == 200 and calls == ["https://hooks.partner.test/x"]


# ---------------------------------------------------------------- the three senders
async def test_webhook_and_teams_refuse_blocked_targets(production_filter: None) -> None:
    for url in (METADATA, "http://127.0.0.1:9777/pwned", "http://localhost:9777/pwned"):
        with pytest.raises(WebhookError, match="Blocked target"):
            await send_webhook({"url": url, "timeout_s": 2}, {"event": "test"})
    with pytest.raises(TeamsError, match="Blocked target"):
        await send_teams({"url": "https://169.254.169.254/workflows/x?sig=SECRET"}, {})


async def test_http_sensor_refuses_blocked_targets(production_filter: None) -> None:
    for url in (METADATA, "http://127.0.0.1:9777/", "http://{host}/"):
        r = await http_check.run(ctx("http", {"url": url, "timeout_s": 2}, host="127.0.0.1"))
        assert not r.ok and r.message.startswith("Blocked target"), r.message
        assert r.values == {}
    assert await http_check.probe_cert_days("169.254.169.254", 443, 1) is None


async def test_redirect_to_metadata_is_refused(
    loopback_on: None, monkeypatch: pytest.MonkeyPatch, target: tuple[int, type[_Target]]
) -> None:
    """An allowed target that redirects to the metadata service (by address or by a name that
    resolves to it): the hop is checked like the first request and the check fails cleanly."""
    port, handler = target
    fake_dns(monkeypatch, {"imds.evil.test": ["169.254.169.254"]})
    for location in (METADATA, "http://imds.evil.test/latest/", "http://[fe80::1]/"):
        handler.redirect_to = location
        handler.seen.clear()
        r = await http_check.run(
            ctx("http", {"url": f"http://127.0.0.1:{port}/redirect", "timeout_s": 3})
        )
        assert not r.ok and r.message.startswith("Blocked target"), r.message
        assert [s["path"] for s in handler.seen] == ["/redirect"]
    # the same sensor with a harmless redirect keeps working
    handler.redirect_to = f"http://127.0.0.1:{port}/final"
    r = await http_check.run(ctx("http", {"url": f"http://127.0.0.1:{port}/redirect"}))
    assert r.ok, r.message
    # the webhook client does not follow redirects at all: the 302 is the answer
    handler.redirect_to = METADATA
    with pytest.raises(WebhookError, match="HTTP 302"):
        await send_webhook({"url": f"http://127.0.0.1:{port}/redirect"}, {"event": "test"})


async def test_redirect_to_loopback_is_refused_by_default(
    production_filter: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:2019/config/"})

    client = egress.guard_client(
        httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    )
    async with client:
        with pytest.raises(egress.EgressBlocked):
            await client.get("http://10.1.1.1/start")
    assert hops == ["http://10.1.1.1/start"]


# ---------------------------------------------------------------- API (the report's steps)
@requires_db
async def test_pentest_reproduction_channels(
    production_filter: None, rt: Any, admin: httpx.AsyncClient
) -> None:
    """Finding 3, steps 2-3: both channels were created (201) and the test send reached the
    listener. Now: field error on save; an existing row fails with a clear message."""
    for url in ("http://127.0.0.1:9777/pwned", METADATA):
        r = await admin.post(
            "/api/notification-channels",
            json={"name": f"ssrf {url[:20]}", "type": "webhook", "config": {"url": url}},
        )
        assert r.status_code == 422, r.text
        detail = r.json()["error"]["details"][0]
        assert detail["loc"] == ["body", "config", "url"] and "Blocked target" in detail["msg"]
    r = await admin.post(
        "/api/notification-channels",
        json={"name": "t", "type": "teams", "config": {"url": "https://169.254.169.254/w?sig=1"}},
    )
    assert r.status_code == 422
    # a private (RFC 1918) target is what the product is for
    r = await admin.post(
        "/api/notification-channels",
        json={"name": "internal", "type": "webhook", "config": {"url": "http://10.9.8.7/hook"}},
    )
    assert r.status_code == 201, r.text
    internal = r.json()["id"]
    # PATCH to a blocked URL: refused; other fields of the channel stay editable
    r = await admin.patch(
        f"/api/notification-channels/{internal}", json={"config": {"url": METADATA}}
    )
    assert r.status_code == 422
    # a channel saved before 1.4.1 (written straight into the table) fails cleanly on send
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        old = await conn.fetchval(
            "INSERT INTO notification_channels (name, type, config, enabled, created_at, "
            "updated_at) VALUES ('old loopback', 'webhook', $1::jsonb, true, now(), now()) "
            "RETURNING id",
            '{"url": "http://127.0.0.1:9777/pwned", "verify_tls": true, "timeout_s": 5, '
            '"headers": {}}',
        )
    finally:
        await conn.close()
    r = await admin.post(f"/api/notification-channels/{old}/test")
    assert r.status_code == 502 and r.json()["error"]["code"] == "webhook_error"
    assert "Blocked target" in r.json()["error"]["message"]
    r = await admin.patch(f"/api/notification-channels/{old}", json={"enabled": False})
    assert r.status_code == 200


@requires_db
async def test_loopback_setting_and_sensor_api(
    production_filter: None,
    rt: Any,
    admin: httpx.AsyncClient,
    target: tuple[int, type[_Target]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    port, _ = target
    r = await admin.get("/api/settings")
    assert r.json()["egress_allow_loopback"] is False  # secure default
    dev = await make_device(admin, host="10.0.0.5")
    local = await make_device(admin, name="self", host="127.0.0.1")

    def sensor(device: dict[str, Any], url: str) -> dict[str, Any]:
        return {"device_id": device["id"], "type": "http", "name": "web", "config": {"url": url}}

    r = await admin.post("/api/sensors", json=sensor(dev, f"http://127.0.0.1:{port}/"))
    assert r.status_code == 422 and "Blocked target" in r.text
    r = await admin.post("/api/sensors", json=sensor(local, "http://{host}/"))  # device host
    assert r.status_code == 422 and "Blocked target" in r.text
    r = await admin.post("/api/sensors", json=sensor(dev, METADATA))
    assert r.status_code == 422
    r = await admin.post("/api/sensors", json=sensor(dev, "http://{host}/"))
    assert r.status_code == 201, r.text
    # the admin turns the setting on: loopback is allowed, metadata is not
    r = await admin.put("/api/settings", json={"egress_allow_loopback": True})
    assert r.status_code == 200 and r.json()["egress_allow_loopback"] is True
    assert rt.security.allow_loopback is True and egress.loopback_allowed()
    r = await admin.post("/api/sensors", json=sensor(local, f"http://{{host}}:{port}/"))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    r = await admin.post("/api/sensors", json=sensor(dev, METADATA))
    assert r.status_code == 422
    r = await admin.post(
        "/api/notification-channels",
        json={
            "name": "local hook",
            "type": "webhook",
            "config": {"url": f"http://127.0.0.1:{port}/hook"},
        },
    )
    assert r.status_code == 201, r.text
    ch = r.json()["id"]
    assert (await admin.post(f"/api/notification-channels/{ch}/test")).status_code == 200
    r = await admin.post(f"/api/sensors/{sid}/run")
    assert r.status_code == 200, r.text
    assert await security_svc.warn_blocked_targets(rt) == []
    # ...and off again: the existing sensor and channel stay, fail clearly, and are listed in
    # the startup warning
    r = await admin.put("/api/settings", json={"egress_allow_loopback": False})
    assert r.status_code == 200 and not egress.loopback_allowed()
    r = await admin.post(f"/api/notification-channels/{ch}/test")
    assert r.status_code == 502 and "Blocked target" in r.json()["error"]["message"]
    r = await http_check.run(ctx("http", {"url": f"http://127.0.0.1:{port}/"}))
    assert not r.ok and "targets on this server" in r.message
    with caplog.at_level(logging.WARNING, logger="netmon.security"):
        found = await security_svc.warn_blocked_targets(rt)
    assert found == [
        "HTTP sensor #2 'web' on device 'self'",
        "webhook channel #1 'local hook'",
    ]
    assert "blocked by the egress filter" in caplog.text and "local hook" in caplog.text
    await asyncio.sleep(0)
