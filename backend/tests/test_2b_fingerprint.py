"""[2b-1] Fingerprinting against local servers on 127.0.6.41-.49, and a discovery scan with
fingerprinting + import (identity). The probed ports are patched to high ports."""

from __future__ import annotations

import asyncio
import datetime as dt
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from netmon.discovery import fingerprint as fpm
from netmon.discovery import runner as runner_mod
from netmon.runtime import Runtime
from tests.conftest import requires_db

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def _serve(ip: str, handler: Handler, ssl_ctx: ssl.SSLContext | None = None) -> Any:
    return await asyncio.start_server(handler, ip, 0, ssl=ssl_ctx)


def _port(server: Any) -> int:
    return int(server.sockets[0].getsockname()[1])


async def _read_request(reader: asyncio.StreamReader) -> dict[str, str]:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    hdrs = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
    hdrs["__line__"] = lines[0]
    return hdrs


def _http(status: str, body: bytes, headers: dict[str, str] | None = None) -> bytes:
    h = {"Content-Length": str(len(body)), "Connection": "close", **(headers or {})}
    return (
        f"HTTP/1.1 {status}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
    ).encode() + body


def _self_signed(tmp: Path) -> ssl.SSLContext:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "opnsense.localdomain")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    (tmp / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp / "k.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(tmp / "c.pem", tmp / "k.pem")
    return ctx


class Sims:
    def __init__(self) -> None:
        self.ports: dict[str, int] = {}
        self.servers: list[Any] = []
        self.requests: list[dict[str, str]] = []


@pytest.fixture
async def sims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Sims]:
    s = Sims()

    async def tplink(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        s.requests.append(await _read_request(r))
        w.write(_http("200 OK", b"<html><title>TL-SG108E</title><body>TP-LINK Easy</body>"))
        await w.drain()
        w.close()

    async def opn(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        req = await _read_request(r)
        s.requests.append(req)
        if req["__line__"].startswith("GET / "):
            w.write(_http("302 Found", b"", {"Location": "/ui/"}))
        else:
            w.write(_http("200 OK", b"<title>Login | OPNsense</title>", {"Server": "OPNsense"}))
        await w.drain()
        w.close()

    async def foreign(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await _read_request(r)
        w.write(_http("302 Found", b"", {"Location": "http://example.com/"}))
        await w.drain()
        w.close()

    async def ssh(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.write(b"SSH-2.0-Comware-7.1.064\r\n")
        await w.drain()
        await asyncio.sleep(0.5)
        w.close()

    async def telnet(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.write(b"\xff\xfb\x01\xff\xfb\x03\xff\xfd\x18\xff\xfa\x18\x01\xff\xf0")
        await w.drain()
        await asyncio.sleep(0.05)
        w.write(b"\r\nUser Access Verification\r\n\r\nUsername: ")
        await w.drain()
        await asyncio.sleep(0.5)
        w.close()

    async def big(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await _read_request(r)
        body = b"x" * (70 * 1024) + b" LaserJet "  # marker beyond 64 KiB: never read
        body += b"y" * (30 * 1024)
        w.write(_http("401 Unauthorized", body, {"WWW-Authenticate": 'Basic realm="DGS-1210-28"'}))
        try:
            await w.drain()
        except ConnectionError:
            pass
        w.close()

    async def printer(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await _read_request(r)
        w.write(_http("200 OK", b"<html>status</html>", {"Server": "HP HTTP Server; HP LaserJet"}))
        await w.drain()
        w.close()

    async def silent(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await asyncio.sleep(10)
        w.close()

    tls = _self_signed(tmp_path)
    for name, ip, handler, ctx in [
        ("tplink", "127.0.6.41", tplink, None),
        ("opn", "127.0.6.42", opn, tls),
        ("foreign", "127.0.6.43", foreign, None),
        ("ssh", "127.0.6.44", ssh, None),
        ("telnet", "127.0.6.46", telnet, None),
        ("printer", "127.0.6.47", printer, None),
        ("big", "127.0.6.48", big, None),
        ("silent", "127.0.6.49", silent, None),
    ]:
        srv = await _serve(ip, handler, ctx)
        s.servers.append(srv)
        s.ports[name] = _port(srv)
    http_ports = tuple(s.ports[k] for k in ("tplink", "opn", "foreign", "printer", "big", "silent"))
    monkeypatch.setattr(fpm, "HTTP_PORTS", http_ports)
    monkeypatch.setattr(fpm, "HTTPS_PORTS", (s.ports["opn"],))
    monkeypatch.setattr(fpm, "FINGERPRINT_PORTS", http_ports)
    monkeypatch.setattr(runner_mod, "FINGERPRINT_PORTS", http_ports)
    yield s
    for srv in s.servers:
        srv.close()


async def test_http_fingerprints(sims: Sims) -> None:
    p = sims.ports
    got = await fpm.http_fingerprint("127.0.6.41", p["tplink"], None, 2000)
    assert got == {
        "port": p["tplink"],
        "scheme": "http",
        "status": 200,
        "title": "TL-SG108E",
        "server": None,
        "realm": None,
        "markers": ["tp-link"],
        "redirects": 0,
        "error": None,
    }
    req = sims.requests[-1]
    assert req["user-agent"].startswith("NetworkMonitor/") and req["user-agent"].endswith(
        "(discovery)"
    )
    assert req["host"] == f"127.0.6.41:{p['tplink']}" and "authorization" not in req
    assert req["accept"] == "text/html"
    got = await fpm.http_fingerprint("127.0.6.42", p["opn"], None, 2000)
    assert (got["scheme"], got["status"], got["redirects"]) == ("https", 200, 1)
    assert got["title"] == "Login | OPNsense" and got["markers"] == ["opnsense"]
    assert got["server"] == "OPNsense"
    got = await fpm.http_fingerprint("127.0.6.43", p["foreign"], None, 2000)
    assert (got["status"], got["redirects"], got["error"]) == (302, 0, None)
    got = await fpm.http_fingerprint("127.0.6.48", p["big"], None, 2000)
    assert got["realm"] == "DGS-1210-28" and got["title"] is None and got["status"] == 401
    assert "laserjet" not in got["markers"]  # beyond the 64 KiB cap
    t0 = time.monotonic()
    got = await fpm.http_fingerprint("127.0.6.49", p["silent"], None, 600)
    assert got["error"] == "timeout" and time.monotonic() - t0 < 1.5
    got = await fpm.http_fingerprint("127.0.6.45", 1, None, 500)  # nothing listens
    assert got["error"] == "connection refused"


async def test_banners_and_host(sims: Sims) -> None:
    ssh = await fpm.ssh_banner("127.0.6.44", 2000, sims.ports["ssh"])
    assert ssh == {"port": sims.ports["ssh"], "banner": "SSH-2.0-Comware-7.1.064"}
    tel = await fpm.telnet_banner("127.0.6.46", 2000, sims.ports["telnet"])
    assert tel["banner"] == "User Access Verification"
    none = await fpm.ssh_banner("127.0.6.49", 300, sims.ports["silent"])
    assert none["banner"] is None
    fp = await fpm.fingerprint_host("127.0.6.47", None, [sims.ports["printer"], 9100], 2000)
    assert fp is not None and fp["ssh"] is None and fp["telnet"] is None
    from netmon.discovery.classify import classify_host

    c = classify_host(None, None, None, [], False, [sims.ports["printer"], 9100], False, fp)
    assert (c.device_class, c.vendor, c.classified_by) == ("printer", "hp", "fingerprint")
    assert await fpm.fingerprint_host("127.0.6.47", None, [9100], 2000) is None


@requires_db
async def test_scan_with_fingerprint_and_import(
    rt: Runtime, admin: httpx.AsyncClient, sims: Sims
) -> None:
    gid = (await admin.get("/api/groups", params={"parent_id": "root"})).json()["items"][0]["id"]
    port = sims.ports["tplink"]
    r = await admin.post(
        "/api/discovery/scans",
        json={
            "targets": ["127.0.6.41"],
            "group_id": gid,
            "options": {
                "ping": False,
                "snmp": False,
                "tcp": True,
                "tcp_ports": [port],
                "dns": False,
                "fingerprint_timeout_ms": 1500,
            },
        },
    )
    assert r.status_code == 202, r.text
    assert r.json()["options"]["fingerprint"] is True
    assert r.json()["options"]["fingerprint_timeout_ms"] == 1500
    await rt.discovery.wait()
    res = (await admin.get(f"/api/discovery/scans/{r.json()['id']}/results")).json()["items"]
    [row] = res
    assert (row["device_class"], row["vendor"], row["model"]) == ("switch", "tp-link", "TL-SG108E")
    assert (row["classified_by"], row["confidence"]) == ("fingerprint", "medium")
    assert row["fingerprint"]["http"][0]["title"] == "TL-SG108E"
    assert row["fingerprint"]["ssh"] is None
    r = await admin.post(
        f"/api/discovery/scans/{r.json()['id']}/import",
        json={"items": [{"result_id": row["id"], "template_id": "generic_ping"}]},
    )
    assert r.status_code == 200, r.text
    did = r.json()["created"][0]["device_id"]
    ident = (await admin.get(f"/api/devices/{did}")).json()["identity"]
    assert ident["device_class"] == "switch" and ident["model"] == "TL-SG108E"
    assert ident["classified_by"] == "fingerprint" and ident["open_ports"] == [port]
    # fingerprinting off
    r = await admin.post(
        "/api/discovery/scans",
        json={
            "targets": ["127.0.6.41"],
            "group_id": gid,
            "options": {
                "ping": False,
                "snmp": False,
                "tcp_ports": [port],
                "dns": False,
                "fingerprint": False,
            },
        },
    )
    await rt.discovery.wait()
    [row] = (await admin.get(f"/api/discovery/scans/{r.json()['id']}/results")).json()["items"]
    assert row["fingerprint"] is None and row["classified_by"] == "none"
    bad = await admin.post(
        "/api/discovery/scans",
        json={
            "targets": ["127.0.6.41"],
            "group_id": gid,
            "options": {"fingerprint_timeout_ms": 100},
        },
    )
    assert bad.status_code == 422
