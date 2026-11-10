from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from netmon.api.deps import RT

router = APIRouter()


@router.get("/api/health")
async def health(rt: RT) -> dict:
    db_ok = False
    if rt.engine is not None:
        try:
            async with rt.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            db_ok = True
        except Exception:  # noqa: BLE001
            db_ok = False
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
