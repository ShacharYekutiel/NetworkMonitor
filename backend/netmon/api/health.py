"""Liveness / readiness (`GET /api/health`, public) and runtime details
(`GET /api/health/details`, any signed-in user or API token).

[security] The public answer says only whether the service is up: `{"status": "ok"}` (200)
or `{"status": "fail"}` (503, database unreachable). Version, uptime, scheduler, receiver,
Kerberos and assistant states and test mode are only returned to an authenticated caller."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from netmon.api.deps import RT, Viewer
from netmon.runtime import Runtime

router = APIRouter()


async def _db_ok(rt: Runtime) -> bool:
    if rt.engine is None:
        return False
    try:
        async with rt.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return False
    return True


@router.get("/api/health")
async def health(rt: RT) -> JSONResponse:
    """Public: exactly one key. 503 makes `curl -f`, Docker healthchecks and the install /
    upgrade waits treat a service without its database as not ready."""
    if await _db_ok(rt):
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "fail"}, status_code=503)


@router.get("/api/health/details")
async def health_details(rt: RT, _user: Viewer) -> dict:
    db_ok = await _db_ok(rt)
    sched = rt.scheduler
    return {
        "status": "ok" if db_ok else "degraded",
        "version": rt.version,
        "database": "ok" if db_ok else "error",
        "timescale": rt.timescale_enabled,
        "poller": "running" if sched is not None and sched.running else "stopped",
        "sensors_scheduled": sched.scheduled_count if sched is not None else 0,
        "uptime_s": rt.uptime_s(),
        "test_mode": bool(rt.settings.netmon_test_mode),
        # [2b-3]
        "trgm": bool(getattr(rt, "trgm_enabled", False)),
        "ingest": {
            **(
                rt.ingest.receiver_states()
                if getattr(rt, "ingest", None) is not None
                else {"trap": "disabled", "syslog_udp": "disabled", "syslog_tcp": "disabled"}
            ),
            # [3] flow receivers
            **(
                rt.flows.receiver_states()
                if getattr(rt, "flows", None) is not None
                else {"netflow": "disabled", "ipfix": "disabled", "sflow": "disabled"}
            ),
        },
        "flows_rollup": getattr(rt, "flows_rollup", None),  # [3]
        "kerberos": (  # [3]
            rt.kerberos.health_state() if getattr(rt, "kerberos", None) is not None else "disabled"
        ),
        "assistant": (  # [3b]
            rt.assistant.health_state()
            if getattr(rt, "assistant", None) is not None
            else "disabled"
        ),
    }
