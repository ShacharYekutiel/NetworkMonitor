"""ICMP ping: icmplib unprivileged -> privileged -> system `ping` binary."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sys

from netmon.poller.types import CheckContext, CheckResult

log = logging.getLogger("netmon.poller.ping")

_mode: str | None = None  # remembered working mode: "unprivileged" | "privileged" | "binary"


async def run(ctx: CheckContext) -> CheckResult:
    count = int(ctx.config.get("count", 4))
    timeout_ms = int(ctx.config.get("timeout_ms", 1000))
    size = int(ctx.config.get("packet_size", 56))
    try:
        rtts, sent = await _ping(ctx.host, count, timeout_ms, size)
    except _ResolveError as exc:
        return CheckResult(False, f"Cannot resolve host: {exc}")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(False, f"Ping failed: {exc}")
    return result_from_rtts(rtts, sent)


def result_from_rtts(rtts: list[float], sent: int) -> CheckResult:
    if sent <= 0 or not rtts:
        return CheckResult(False, "Host unreachable")
    loss = (sent - len(rtts)) / sent * 100.0
    avg = sum(rtts) / len(rtts)
    return CheckResult(True, "", {"latency_ms": round(avg, 3), "packet_loss_pct": round(loss, 2)})


class _ResolveError(Exception):
    pass


async def _ping(host: str, count: int, timeout_ms: int, size: int) -> tuple[list[float], int]:
    global _mode
    import icmplib

    timeout = timeout_ms / 1000.0
    modes = [_mode] if _mode else ["unprivileged", "privileged", "binary"]
    last_exc: Exception | None = None
    for mode in modes:
        try:
            if mode == "binary":
                result = await _ping_binary(host, count, timeout_ms, size)
            else:
                h = await icmplib.async_ping(
                    host,
                    count=count,
                    interval=0.2,
                    timeout=timeout,
                    payload_size=size,
                    privileged=(mode == "privileged"),
                )
                result = (list(h.rtts), h.packets_sent)
            if _mode != mode:
                log.info("Ping uses %s mode", mode)
                _mode = mode
            return result
        except icmplib.NameLookupError as exc:
            raise _ResolveError(str(exc)) from exc
        except (icmplib.ICMPSocketError, PermissionError, OSError) as exc:
            last_exc = exc
            if _mode == mode:
                _mode = None  # re-probe next time
                return await _ping(host, count, timeout_ms, size)
            continue
    raise RuntimeError(str(last_exc) if last_exc else "no ping method available")


_RTT_RE = re.compile(r"(?:time|Zeit|temps|tiempo)[=<]\s*([\d.,]+)\s*ms", re.IGNORECASE)


def parse_ping_output(output: str) -> list[float]:
    """Parse RTTs from Linux / Windows ping output."""
    rtts = []
    for m in _RTT_RE.finditer(output):
        try:
            rtts.append(float(m.group(1).replace(",", ".")))
        except ValueError:
            continue
    return rtts


async def _ping_binary(
    host: str, count: int, timeout_ms: int, size: int
) -> tuple[list[float], int]:
    exe = shutil.which("ping")
    if not exe:
        raise OSError("system ping binary not found")
    if host.startswith("-"):
        raise _ResolveError("invalid host")
    if sys.platform.startswith("win"):
        args = [exe, "-n", str(count), "-w", str(timeout_ms), "-l", str(size), host]
    else:
        wait_s = max(1, round(timeout_ms / 1000))
        args = [exe, "-n", "-c", str(count), "-W", str(wait_s), "-i", "0.2", "-s", str(size), host]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(), timeout=count * (timeout_ms / 1000 + 0.3) + 5
        )
    except TimeoutError:
        proc.kill()
        raise
    text = out.decode(errors="replace")
    rtts = parse_ping_output(text)
    if not rtts and ("unknown host" in text.lower() or "could not find host" in text.lower()):
        raise _ResolveError(host)
    return rtts, count
