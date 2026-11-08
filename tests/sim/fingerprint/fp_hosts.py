#!/usr/bin/env python3
"""Phase 2b-1 fingerprint targets (docs/architecture.md §10.4), one asyncio process for all hosts.

    backend/.venv/bin/python tests/sim/fingerprint/fp_hosts.py            # prints READY when all listen
    backend/.venv/bin/python tests/sim/fingerprint/fp_hosts.py --list     # print the inventory

Hosts of the contract table (127.0.0.41-.49):
  .41 tcp/80   <title>TL-SG108E</title>, body contains TP-LINK       -> switch / tp-link / TL-SG108E / medium (F7)
  .42 tcp/443  self-signed TLS; GET / -> 302 /ui/ (same host); /ui/ -> <title>Login | OPNsense</title>
                                                                      -> router / opnsense / high, redirects 1
  .43 tcp/80   302 -> http://example.com/                             -> not followed: redirects 0, status 302
  .44 tcp/22   banner SSH-2.0-Comware-7.1.064                         -> switch / h3c / high (F9)
  .45 tcp/22   banner SSH-2.0-ROSSSH                                  -> router / mikrotik / high (F3)
  .46 tcp/23   IAC negotiation, then "\\r\\nUser Access Verification\\r\\n" -> banner without IAC bytes
  .47 tcp/80   Server: HP HTTP Server; HP LaserJet ..., no title      -> printer / hp (F17, medium)
  .48 tcp/8080 401, WWW-Authenticate: Basic realm="DGS-1210-28", 100 KiB body without a title; the word
               "zyxel" sits after the first 64 KiB (must NOT be a marker: body cap)
  .49 tcp/80   accepts and never answers                              -> error "timeout"

QA extras (127.0.0.61-.67, outside the contract table, cover the remaining vendor rules):
  .61 tcp/80   <title>Aruba 2930F-24G-4SFP+ Switch (JL259A)</title>   -> switch / hp / JL259A / medium (F10)
  .62 tcp/22   banner SSH-2.0-Cisco-1.25                              -> router / cisco / medium (F24)
  .63 tcp/443  <title>FortiGate</title>, Server: xxxxxxxx-xxxxx       -> router / fortinet / medium (F23)
  .64 tcp/80   <title>Synology DiskStation - DS920+</title>           -> nas / synology / high (F15), model DS920(+)
  .65 tcp/443  <title>iLO 5 - ProLiant DL380 Gen10</title>             -> server / hp / high (F12)
  .66 tcp/443  <title>iDRAC9 - Login</title>, body "Integrated Dell Remote Access Controller"
                                                                      -> server / dell / high (F13)
  .67 tcp/80   <title>Welcome to nginx!</title>, Server: nginx        -> no fingerprint rule (port rules)

Every request is logged as one JSON line on stdout ({"ip","port","path","host","ua","auth"}) so specs can
assert that no credentials were sent. Nothing is ever written on the SSH/telnet sockets except the banner.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import ssl
import sys
import tempfile
from pathlib import Path

PAD_64K = 64 * 1024


def html(title: str | None, body: str) -> bytes:
    head = f"<head><title>{title}</title></head>" if title is not None else "<head></head>"
    return f"<!DOCTYPE html><html>{head}<body>{body}</body></html>".encode()


def big_body() -> bytes:
    # 100 KiB, no <title>; a marker word only after 64 KiB (proves the read cap).
    start = b"<html><body><p>Authorization required</p>"
    filler = b"<!-- " + b"x" * 1000 + b" -->\n"
    out = start
    while len(out) < PAD_64K + 4096:
        out += filler
    out += b"<p>zyxel</p>"
    while len(out) < 100 * 1024:
        out += filler
    return out + b"</body></html>"


# (ip, port, scheme, handler spec). Handler spec: dict path -> (status, headers, body) or a special kind.
def http_hosts() -> list[tuple[str, int, str, dict]]:
    return [
        ("127.0.0.41", 80, "http", {"/": (200, {"Server": "Embedded HTTP Server"},
                                          html("TL-SG108E", "<p>TP-LINK Easy Smart Configuration Utility</p>"))}),
        ("127.0.0.42", 443, "https", {
            "/": (302, {"Location": "/ui/", "Server": "OPNsense"}, b""),
            "/ui/": (200, {"Server": "OPNsense"}, html("Login | OPNsense", "<form>OPNsense login</form>")),
        }),
        ("127.0.0.43", 80, "http", {"/": (302, {"Location": "http://example.com/"}, b"moved")}),
        ("127.0.0.47", 80, "http", {"/": (200, {"Server": "HP HTTP Server; HP LaserJet Pro M404dn - W1A53A; Serial Number: PHBQA00001"},
                                          b"<html><body><p>Device status</p></body></html>")}),
        ("127.0.0.48", 8080, "http", {"/": (401, {"WWW-Authenticate": 'Basic realm="DGS-1210-28"', "Server": "Embedded"},
                                            big_body())}),
        ("127.0.0.61", 80, "http", {"/": (200, {"Server": "eWeb"},
                                          html("Aruba 2930F-24G-4SFP+ Switch (JL259A)", "<p>Aruba web management</p>"))}),
        ("127.0.0.63", 443, "https", {"/": (200, {"Server": "xxxxxxxx-xxxxx"}, html("FortiGate", "<p>Please login</p>"))}),
        ("127.0.0.64", 80, "http", {"/": (200, {"Server": "nginx"}, html("Synology DiskStation - DS920+", "<p>DSM</p>"))}),
        ("127.0.0.65", 443, "https", {"/": (200, {"Server": "HPE-iLO-Server/1.0"},
                                            html("iLO 5 - ProLiant DL380 Gen10", "<p>HPE Integrated Lights-Out</p>"))}),
        ("127.0.0.66", 443, "https", {"/": (200, {"Server": "Apache"},
                                            html("iDRAC9 - Login", "<p>Integrated Dell Remote Access Controller</p>"))}),
        ("127.0.0.67", 80, "http", {"/": (200, {"Server": "nginx/1.24.0"},
                                          html("Welcome to nginx!", "<p>If you see this page, the nginx web server is working.</p>"))}),
    ]


BANNERS = [
    ("127.0.0.44", 22, b"SSH-2.0-Comware-7.1.064\r\n"),
    ("127.0.0.45", 22, b"SSH-2.0-ROSSSH\r\n"),
    ("127.0.0.62", 22, b"SSH-2.0-Cisco-1.25\r\n"),
    # IAC WILL ECHO, IAC WILL SUPPRESS-GO-AHEAD, IAC DO TERMINAL-TYPE, IAC SB NAWS 0 80 0 24 IAC SE
    ("127.0.0.46", 23, bytes([255, 251, 1, 255, 251, 3, 255, 253, 24, 255, 250, 31, 0, 80, 0, 24, 255, 240])
     + b"\r\nUser Access Verification\r\n\r\nUsername: "),
]

HANG = [("127.0.0.49", 80)]

REASONS = {200: "OK", 302: "Found", 401: "Unauthorized", 404: "Not Found"}


def log(**kw: object) -> None:
    print(json.dumps(kw), flush=True)


def make_tls_context(workdir: Path) -> ssl.SSLContext:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "netmon-fp-sim")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    cf, kf = workdir / "cert.pem", workdir / "key.pem"
    cf.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kf.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cf, kf)
    return ctx


def http_handler(ip: str, port: int, routes: dict):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError, ssl.SSLError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, path, _ = lines[0].split(" ", 2)
        except ValueError:
            writer.close()
            return
        hdrs = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        log(ip=ip, port=port, method=method, path=path, host=hdrs.get("host"), ua=hdrs.get("user-agent"),
            auth=hdrs.get("authorization"), cookie=hdrs.get("cookie"))
        status, extra, body = routes.get(path.split("?", 1)[0], (404, {}, b"not found"))
        out = [f"HTTP/1.1 {status} {REASONS.get(status, 'X')}", "Content-Type: text/html; charset=utf-8",
               f"Content-Length: {len(body)}", "Connection: close"]
        out += [f"{k}: {v}" for k, v in extra.items()]
        try:
            writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
            if method != "HEAD":
                writer.write(body)
            await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    return handle


def banner_handler(ip: str, port: int, banner: bytes):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        log(ip=ip, port=port, kind="banner")
        try:
            writer.write(banner)
            await writer.drain()
            # Keep the connection open (like a real daemon waiting for the client) and record whatever
            # the client sends; the contract says the prober sends nothing.
            data = await asyncio.wait_for(reader.read(1024), 10)
            if data:
                log(ip=ip, port=port, kind="client_sent", bytes=data.hex())
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    return handle


def hang_handler(ip: str, port: int):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        log(ip=ip, port=port, kind="hang")
        try:
            await asyncio.wait_for(reader.read(65536), 120)  # never answer
            await asyncio.sleep(120)
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    return handle


def inventory() -> list[str]:
    out = [f"{ip}:{port} {scheme}" for ip, port, scheme, _ in http_hosts()]
    out += [f"{ip}:{port} banner" for ip, port, _ in BANNERS]
    out += [f"{ip}:{port} hang" for ip, port in HANG]
    return sorted(out, key=lambda s: tuple(int(x) for x in s.split(":")[0].split(".")))


async def main_async() -> None:
    work = Path(tempfile.mkdtemp(prefix="netmon-fp-"))
    tls = make_tls_context(work)
    servers = []
    for ip, port, scheme, routes in http_hosts():
        servers.append(await asyncio.start_server(http_handler(ip, port, routes), ip, port,
                                                  ssl=tls if scheme == "https" else None, reuse_address=True))
    for ip, port, banner in BANNERS:
        servers.append(await asyncio.start_server(banner_handler(ip, port, banner), ip, port, reuse_address=True))
    for ip, port in HANG:
        servers.append(await asyncio.start_server(hang_handler(ip, port), ip, port, reuse_address=True))
    print(f"READY {len(servers)} listeners", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--list", action="store_true")
    a = p.parse_args()
    if a.list:
        print("\n".join(inventory()))
        return 0
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
