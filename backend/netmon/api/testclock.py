"""[2b-1] Test clock (QA only, NETMON_TEST_MODE=1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from netmon.api.deps import RT, Admin
from netmon.api.errors import not_found
from netmon.schemas import ClockIn
from netmon.services.serialize import iso

router = APIRouter()


def _test_mode(rt: RT) -> None:
    if not rt.settings.netmon_test_mode:
        raise not_found()


@router.put("/api/test/clock", dependencies=[Depends(_test_mode)])
async def set_clock(body: ClockIn, _: Admin, rt: RT) -> dict:
    rt.clock.set_offset(body.offset_s)
    if rt.maintenance is not None:
        await rt.maintenance.evaluate()
    if rt.alerts is not None:
        rt.alerts.notify()
    return {"now": iso(rt.clock.now()), "offset_s": int(rt.clock.offset_s)}
