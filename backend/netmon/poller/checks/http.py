"""HTTP(S) check: response time, status code, TLS certificate days left."""

from __future__ import annotations

import asyncio
import ipaddress
import ssl
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from netmon import __version__
from netmon.poller.types import CheckContext, CheckResult
from netmon.tlsutil import httpx_verify


def host_for_url(host: str) -> str:
    try:
        if ipaddress.ip_address(host).version == 6:
            return f"[{host}]"
    except ValueError:
        pass
    return host


# --- minimal DER walker for X.509 notAfter -----------------------------------------------------


def _tlv(data: bytes, pos: int) -> tuple[int, int, int]:
    """Returns (tag, content_start, content_end)."""
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(data[pos : pos + n], "big")
        pos += n
    return tag, pos, pos + length


def _children(data: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    out = []
    pos = start
    while pos < end:
        tag, cs, ce = _tlv(data, pos)
        out.append((tag, cs, ce))
        pos = ce
    return out


def _parse_time(tag: int, raw: str) -> datetime:
    raw = raw.rstrip("Z")
    if tag == 0x17:  # UTCTime YYMMDDHHMM[SS]
        yy = int(raw[:2])
        year = 2000 + yy if yy < 50 else 1900 + yy
        rest = raw[2:]
    else:  # GeneralizedTime
        year = int(raw[:4])
        rest = raw[4:]
    month, day, hour, minute = (int(rest[i : i + 2]) for i in (0, 2, 4, 6))
    second = int(rest[8:10]) if len(rest) >= 10 else 0
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def cert_not_after(der: bytes) -> datetime:
    _, cs, ce = _tlv(der, 0)  # Certificate
    tbs_tag, tcs, tce = _children(der, cs, ce)[0]
    items = _children(der, tcs, tce)
    if items and items[0][0] == 0xA0:  # explicit version
        items = items[1:]
    _, vcs, vce = items[3]  # serial, sigAlg, issuer, validity
    times = _children(der, vcs, vce)
    tag, s, e = times[1]
    return _parse_time(tag, der[s:e].decode("ascii"))


def days_left(der: bytes, now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    return round((cert_not_after(der) - now).total_seconds() / 86400.0, 3)


async def probe_cert_days(host: str, port: int, timeout: float) -> float | None:
    """Unverified TLS handshake to read the peer certificate."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    server_hostname = host
    try:
        ipaddress.ip_address(host)
        server_hostname = None  # type: ignore[assignment]
    except ValueError:
        pass
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=server_hostname),
            timeout,
        )
    except (OSError, TimeoutError, ssl.SSLError):
        return None
    try:
        sslobj = writer.get_extra_info("ssl_object")
        der = sslobj.getpeercert(binary_form=True) if sslobj else None
        return days_left(der) if der else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


def _stream_cert_days(resp: httpx.Response) -> float | None:
    try:
        stream = resp.extensions.get("network_stream")
        if stream is None:
            return None
        sslobj = stream.get_extra_info("ssl_object")
        if sslobj is None:
            return None
        der = sslobj.getpeercert(binary_form=True)
        return days_left(der) if der else None
    except Exception:  # noqa: BLE001
        return None


async def run(ctx: CheckContext) -> CheckResult:
    cfg = ctx.config
    url = str(cfg["url"]).replace("{host}", host_for_url(ctx.host))
    timeout = float(cfg.get("timeout_s", 10))
    headers = {"User-Agent": f"NetworkMonitor/{__version__}", **(cfg.get("headers") or {})}
    is_https = url.lower().startswith("https://")
    ssl_days: float | None = None
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            verify=httpx_verify(bool(cfg.get("verify_tls", True))),
            follow_redirects=bool(cfg.get("follow_redirects", True)),
            timeout=timeout,
            trust_env=False,
        ) as client:
            async with client.stream(cfg.get("method", "GET"), url, headers=headers) as resp:
                if is_https:
                    ssl_days = _stream_cert_days(resp)
                body = await resp.aread()
            elapsed_ms = (time.perf_counter() - start) * 1000.0
    except httpx.TimeoutException:
        return CheckResult(False, f"Request timed out after {timeout:g} s")
    except httpx.HTTPError as exc:
        msg = _error_text(exc)
        values: dict[str, float | None] = {}
        if is_https and _is_tls_error(exc):
            parts = urlsplit(url)
            days = await probe_cert_days(parts.hostname or ctx.host, parts.port or 443, timeout)
            if days is not None:
                values["ssl_days_left"] = days
        return CheckResult(False, msg, values)
    if is_https and ssl_days is None and resp.url.scheme == "https":
        ssl_days = await probe_cert_days(resp.url.host, resp.url.port or 443, timeout)
    values = {"response_time_ms": round(elapsed_ms, 3), "status_code": float(resp.status_code)}
    if ssl_days is not None:
        values["ssl_days_left"] = ssl_days
    expected = cfg.get("expected_status")
    if expected:
        if resp.status_code not in expected:
            return CheckResult(False, f"Unexpected status code {resp.status_code}", values)
    elif not (200 <= resp.status_code < 400):
        return CheckResult(False, f"Unexpected status code {resp.status_code}", values)
    keyword = cfg.get("keyword")
    if keyword and cfg.get("method", "GET") != "HEAD":
        text = body.decode(resp.encoding or "utf-8", errors="replace")
        found = keyword in text
        if cfg.get("keyword_must_exist", True) and not found:
            return CheckResult(False, f"Keyword not found: {keyword}", values)
        if not cfg.get("keyword_must_exist", True) and found:
            return CheckResult(False, f"Forbidden keyword found: {keyword}", values)
    return CheckResult(True, f"HTTP {resp.status_code}", values)


def _is_tls_error(exc: BaseException) -> bool:
    cur: BaseException | None = exc
    for _ in range(6):
        if cur is None:
            return False
        if isinstance(cur, ssl.SSLError) or "SSL" in str(cur) or "certificate" in str(cur):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _error_text(exc: BaseException) -> str:
    cur: BaseException | None = exc
    while cur is not None and cur.__cause__ is not None and not str(cur):
        cur = cur.__cause__
    text = str(cur or exc) or type(exc).__name__
    if isinstance(exc, httpx.ConnectError):
        return f"Connection failed: {text}"[:300]
    return text[:300]
