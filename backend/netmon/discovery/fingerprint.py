"""[2b-1] Device fingerprinting without SNMP: HTTP(S) titles/headers, SSH and telnet banners.

Connects only to the host being scanned/identified, follows at most 2 same-host redirects,
reads at most 64 KiB per response, sends no credentials, and sends nothing on SSH/telnet.
TLS verification is off for fingerprinting only."""

from __future__ import annotations

import asyncio
import contextlib
import html
import ipaddress
import re
import ssl
from typing import Any
from urllib.parse import urljoin, urlsplit

from netmon import __version__
from netmon.discovery.probe import _tcp_semaphore

HTTP_PORTS = (80, 443, 8080, 8443)
HTTPS_PORTS = (443, 8443)
FINGERPRINT_PORTS = (*HTTP_PORTS, 22, 23)
BODY_LIMIT = 64 * 1024
MAX_REDIRECTS = 2
FIELD_MAX = 200
BANNER_MAX = 255
TELNET_READ = 512
MARKERS = (
    "opnsense, pfsense, unifi, ubiquiti, airos, edgeos, mikrotik, routeros, tp-link, tplink, "
    "easy smart, omada, procurve, aruba, arubaos, officeconnect, comware, h3c, hpe, cisco, "
    "netgear, prosafe, d-link, dlink, zyxel, fortinet, fortigate, synology, diskstation, qnap, "
    "laserjet, officejet, brother, canon, kyocera, ricoh, epson, xerox, lexmark, ilo, idrac, "
    "supermicro, megarac"
).split(", ")
_MARKER_RE = {m: re.compile(rf"\b{re.escape(m)}\b", re.IGNORECASE) for m in MARKERS}
_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_REALM_RE = re.compile(r'realm\s*=\s*"([^"]*)"', re.IGNORECASE)
_WS = re.compile(r"\s+")


# ------------------------------------------------------------------ pure helpers
def extract_title(body: bytes) -> str | None:
    m = _TITLE_RE.search(body)
    if m is None:
        return None
    raw = m.group(1).decode("utf-8", errors="replace")
    title = _WS.sub(" ", html.unescape(raw)).strip()
    return title[:FIELD_MAX] or None


def find_markers(*texts: str | None) -> list[str]:
    blob = "\n".join(t for t in texts if t)
    return sorted(m for m, rx in _MARKER_RE.items() if rx.search(blob))


def extract_realm(header: str | None) -> str | None:
    if not header:
        return None
    m = _REALM_RE.search(header)
    return m.group(1)[:FIELD_MAX] if m else None


def strip_telnet(data: bytes) -> bytes:
    """Remove IAC sequences: IAC + command + option (WILL/WONT/DO/DONT), IAC SB … IAC SE,
    IAC + 1-byte commands; IAC IAC is a literal 0xFF (dropped as non-printable later)."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b != 0xFF:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            break
        cmd = data[i + 1]
        if cmd == 0xFA:  # SB ... IAC SE
            j = data.find(b"\xff\xf0", i + 2)
            i = n if j < 0 else j + 2
        elif cmd in (0xFB, 0xFC, 0xFD, 0xFE):
            i += 3
        elif cmd == 0xFF:
            i += 2
        else:
            i += 2
    return bytes(out)


def first_line(data: bytes, limit: int = BANNER_MAX) -> str | None:
    text = "".join(ch for ch in data.decode("latin-1") if ch in "\r\n" or 32 <= ord(ch) < 127)
    for line in re.split(r"[\r\n]+", text):
        line = line.strip()
        if line:
            return line[:limit]
    return None


def same_host(location_host: str | None, ip: str, host_header: str | None) -> bool:
    if not location_host:
        return False
    h = location_host.strip("[]").lower()
    try:
        if ipaddress.ip_address(h) == ipaddress.ip_address(ip):
            return True
    except ValueError:
        pass
    return bool(host_header) and h == str(host_header).strip("[]").lower()


def redirect_target(
    current: str, location: str, ip: str, host_header: str | None
) -> tuple[str, int, str] | None:
    """(scheme, port, path?query) for a same-host redirect on an allowed port, else None."""
    url = urljoin(current, location.strip())
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    if port not in HTTP_PORTS or not same_host(parts.hostname, ip, host_header):
        return None
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, port, path


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return False
    return True


def _host_for_url(ip: str) -> str:
    return f"[{ip}]" if ":" in ip else ip


# ------------------------------------------------------------------ HTTP
async def _http_once(
    ip: str, port: int, scheme: str, path: str, host_header: str, timeout: float
) -> tuple[int | None, dict[str, list[str]], bytes]:
    """One GET; returns (status, headers (lower-case name -> values), body <= 64 KiB)."""
    ctx = None
    if scheme == "https":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    sni: str | None = None
    if ctx is not None:
        sni = "" if _is_ip(host_header) else host_header
    async with _tcp_semaphore():
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port, ssl=ctx, server_hostname=sni), timeout
        )
        try:
            hh = host_header if port in (80, 443) else f"{host_header}:{port}"
            if ":" in host_header and not host_header.startswith("["):
                hh = f"[{host_header}]" + ("" if port in (80, 443) else f":{port}")
            req = (
                f"GET {path} HTTP/1.1\r\nHost: {hh}\r\n"
                f"User-Agent: NetworkMonitor/{__version__} (discovery)\r\n"
                "Accept: text/html\r\nConnection: close\r\n\r\n"
            )
            writer.write(req.encode("latin-1", errors="replace"))
            await asyncio.wait_for(writer.drain(), timeout)
            head = await asyncio.wait_for(_read_head(reader), timeout)
            status, headers = _parse_head(head)
            body = await asyncio.wait_for(_read_body(reader, headers), timeout)
            return status, headers, body
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 1.0)


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data and b"\n\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > 65536:
            break
    return data


def _parse_head(data: bytes) -> tuple[int | None, dict[str, list[str]]]:
    sep = data.find(b"\r\n\r\n")
    if sep < 0:
        sep = data.find(b"\n\n")
    head = data if sep < 0 else data[:sep]
    lines = head.decode("latin-1").splitlines()
    status = None
    if lines:
        parts = lines[0].split()
        if len(parts) >= 2 and parts[0].upper().startswith("HTTP/") and parts[1].isdigit():
            status = int(parts[1])
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name:
            headers.setdefault(name.strip().lower(), []).append(value.strip())
    rest = b"" if sep < 0 else data[sep + (4 if data[sep : sep + 4] == b"\r\n\r\n" else 2) :]
    headers["__rest__"] = [rest.decode("latin-1")]
    return status, headers


async def _read_body(reader: asyncio.StreamReader, headers: dict[str, list[str]]) -> bytes:
    body = headers.pop("__rest__", [""])[0].encode("latin-1")
    chunked = "chunked" in ",".join(headers.get("transfer-encoding", [])).lower()
    while len(body) < BODY_LIMIT + 16:
        chunk = await reader.read(8192)
        if not chunk:
            break
        body += chunk
    if chunked:
        body = _dechunk(body)
    return body[:BODY_LIMIT]


def _dechunk(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        j = data.find(b"\r\n", i)
        if j < 0:
            break
        try:
            size = int(data[i:j].split(b";")[0].strip() or b"0", 16)
        except ValueError:
            return data
        if size == 0:
            break
        out += data[j + 2 : j + 2 + size]
        i = j + 2 + size + 2
    return bytes(out)


async def http_fingerprint(
    ip: str, port: int, host_header: str | None, timeout_ms: int
) -> dict[str, Any]:
    scheme = "https" if port in HTTPS_PORTS else "http"
    entry: dict[str, Any] = {
        "port": port,
        "scheme": scheme,
        "status": None,
        "title": None,
        "server": None,
        "realm": None,
        "markers": [],
        "redirects": 0,
        "error": None,
    }
    timeout = max(0.1, timeout_ms / 1000.0)
    host = host_header or ip
    cur_scheme, cur_port, path = scheme, port, "/"
    redirects = 0
    try:
        while True:
            status, headers, body = await _http_once(ip, cur_port, cur_scheme, path, host, timeout)
            location = (headers.get("location") or [None])[0]
            if status in (301, 302, 303, 307, 308) and location and redirects < MAX_REDIRECTS:
                current = f"{cur_scheme}://{_host_for_url(host)}:{cur_port}{path}"
                nxt = redirect_target(current, location, ip, host)
                if nxt is not None:
                    cur_scheme, cur_port, path = nxt
                    redirects += 1
                    continue
            break
    except TimeoutError:
        entry["error"] = "timeout"
        entry["redirects"] = redirects
        return entry
    except ssl.SSLError:
        entry["error"] = "tls error"
        entry["redirects"] = redirects
        return entry
    except ConnectionRefusedError:
        entry["error"] = "connection refused"
        entry["redirects"] = redirects
        return entry
    except (OSError, asyncio.IncompleteReadError, UnicodeError, ValueError) as exc:
        entry["error"] = (str(exc) or type(exc).__name__)[:100] or "error"
        entry["redirects"] = redirects
        return entry
    title = extract_title(body)
    server = (headers.get("server") or [None])[0]
    entry.update(
        status=status,
        title=title,
        server=server[:FIELD_MAX] if server else None,
        realm=extract_realm((headers.get("www-authenticate") or [None])[0]),
        markers=find_markers(title, body.decode("utf-8", errors="replace")),
        redirects=redirects,
    )
    return entry


# ------------------------------------------------------------------ SSH / telnet
async def ssh_banner(ip: str, timeout_ms: int, port: int = 22) -> dict[str, Any]:
    out: dict[str, Any] = {"port": port, "banner": None}
    timeout = max(0.1, timeout_ms / 1000.0)
    try:
        async with _tcp_semaphore():
            reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
            try:
                line = await asyncio.wait_for(reader.readline(), timeout)
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), 1.0)
    except (TimeoutError, OSError, ValueError, asyncio.LimitOverrunError):
        return out
    out["banner"] = first_line(line[:4096])
    return out


async def telnet_banner(ip: str, timeout_ms: int, port: int = 23) -> dict[str, Any]:
    out: dict[str, Any] = {"port": port, "banner": None}
    timeout = max(0.1, timeout_ms / 1000.0)
    data = b""
    try:
        async with _tcp_semaphore():
            reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
            try:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout
                while len(data) < TELNET_READ:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        chunk = await asyncio.wait_for(
                            reader.read(TELNET_READ - len(data)), remaining
                        )
                    except TimeoutError:
                        break
                    if not chunk:
                        break
                    data += chunk
                    cleaned = strip_telnet(data)
                    if b"\n" in cleaned and first_line(cleaned.rsplit(b"\n", 1)[0]):
                        break
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), 1.0)
    except (TimeoutError, OSError):
        return out
    out["banner"] = first_line(strip_telnet(data[:TELNET_READ]))
    return out


async def fingerprint_host(
    ip: str, host_header: str | None, open_ports: list[int], timeout_ms: int
) -> dict[str, Any] | None:
    """Fingerprint the open ports of {80, 443, 8080, 8443, 22, 23}, all concurrently. None
    when there is nothing to fingerprint."""
    ports = set(open_ports)
    if not ports & set(FINGERPRINT_PORTS):
        return None
    http_ports = [p for p in HTTP_PORTS if p in ports]
    jobs: list[Any] = [http_fingerprint(ip, p, host_header, timeout_ms) for p in http_ports]
    if 22 in ports:
        jobs.append(ssh_banner(ip, timeout_ms))
    if 23 in ports:
        jobs.append(telnet_banner(ip, timeout_ms))
    results = await asyncio.gather(*jobs)
    fp: dict[str, Any] = {
        "http": list(results[: len(http_ports)]),
        "ssh": None,
        "telnet": None,
    }
    rest = list(results[len(http_ports) :])
    if 22 in ports:
        fp["ssh"] = rest.pop(0)
    if 23 in ports:
        fp["telnet"] = rest.pop(0)
    return fp
