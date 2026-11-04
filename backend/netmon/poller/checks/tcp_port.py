from __future__ import annotations

import asyncio
import time

from netmon.poller.types import CheckContext, CheckResult


async def run(ctx: CheckContext) -> CheckResult:
    port = int(ctx.config["port"])
    timeout = float(ctx.config.get("timeout_s", 5))
    start = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ctx.host, port), timeout)
    except TimeoutError:
        return CheckResult(False, f"Connection to port {port} timed out")
    except ConnectionRefusedError:
        return CheckResult(False, f"Connection to port {port} refused")
    except OSError as exc:
        return CheckResult(False, f"Connection to port {port} failed: {exc.strerror or exc}")
    elapsed = (time.perf_counter() - start) * 1000.0
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 2)
    except (OSError, TimeoutError):
        pass
    return CheckResult(True, "", {"connect_time_ms": round(elapsed, 3)})
