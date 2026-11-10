"""[3] Reports: runs and the scheduler (architecture § 13.6.1, 13.6.3).

`ReportService` (rt.reports) is a phase-3 feature component (`reports`):
- `start_run(...)` inserts a `report_runs` row (`running`), executes in the background (one
  run at a time, `asyncio.Lock`): gather (short read-only queries) → render (thread) →
  deliver → store; `report.run` WS on start and finish.
- the scheduler wakes at the earliest `next_run_at`, at least every 60 s and on clock changes;
  a run missed while the process was down runs at startup when < 24 h late, otherwise it is
  skipped with a `system` event.
- run retention (`report_retention_days`) runs hourly also while the feature is OFF.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from netmon.reports import compute, render
from netmon.reports import data as report_data
from netmon.reports import send as report_send
from netmon.services.serialize import iso

log = logging.getLogger("netmon.reports")

LOOP_MAX_S = 60.0
MISSED_MAX_S = 24 * 3600
RETENTION_EVERY_S = 3600.0
RETRY_MAX = 2  # retries after the first attempt: 3 attempts in all
RETRY_WINDOW_S = 6 * 3600.0
RETRY_DELAY_S = 1800.0
REPORT_COLUMNS = (
    "id, name, enabled, language, scope_group_ids, scope_device_ids, sections, schedule, "
    "weekday, month_day, send_time, email_to, channel_ids, next_run_at, last_run_id, "
    "created_at, updated_at, ai_summary"
)
RUN_COLUMNS = (
    "id, report_id, report_name, trigger, period_start, period_end, started_at, finished_at, "
    "status, language, deliveries, created_by, ai_summary_status"
)


def run_out(r: Any) -> dict[str, Any]:
    deliveries = r.deliveries
    if isinstance(deliveries, str):
        deliveries = json.loads(deliveries)
    return {
        "id": r.id,
        "report_id": r.report_id,
        "report_name": r.report_name,
        "trigger": r.trigger,
        "period_start": iso(r.period_start),
        "period_end": iso(r.period_end),
        "started_at": iso(r.started_at),
        "finished_at": iso(r.finished_at),
        "status": r.status,
        "language": r.language,
        "created_by": r.created_by,
        "deliveries": list(deliveries or []),
        "ai_summary_status": getattr(r, "ai_summary_status", None),  # [3b]
    }


def report_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "enabled": r.enabled,
        "language": r.language,
        "scope_group_ids": list(r.scope_group_ids or []),
        "scope_device_ids": list(r.scope_device_ids or []),
        "sections": list(r.sections or []),
        "schedule": r.schedule,
        "weekday": r.weekday,
        "month_day": r.month_day,
        "send_time": r.send_time,
        "email_to": list(r.email_to or []),
        "channel_ids": list(r.channel_ids or []),
        "next_run_at": r.next_run_at,
        "ai_summary": bool(getattr(r, "ai_summary", False)),  # [3b] § 14.8.10
    }


class ReportBusy(Exception):
    pass


class ReportService:
    features = ("reports",)

    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self._lock = asyncio.Lock()
        self._running: set[int] = set()
        self._task: asyncio.Task[None] | None = None
        self._retention_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._runs: set[asyncio.Task[Any]] = set()
        self.on = False
        self._retries: dict[int, tuple[int, float]] = {}
        self._retry_tasks: set[asyncio.Task[Any]] = set()
        self.retry_delay_s = RETRY_DELAY_S

    # ------------------------------------------------------------ component API
    async def start(self, snapshot: dict[str, bool]) -> None:
        if self._retention_task is None:
            self._retention_task = asyncio.create_task(
                self._retention_loop(), name="netmon-report-retention"
            )
        if snapshot.get("reports"):
            await self._start_scheduler(startup=True)

    async def feature_changed(self, name: str, on: bool) -> None:
        if name != "reports":
            return
        if on:
            await self._start_scheduler(startup=False)
        else:
            await self._stop_scheduler()

    async def stop(self) -> None:
        await self._stop_scheduler()
        for t in list(self._runs):
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, 5)
        if self._retention_task is not None:
            self._retention_task.cancel()
            with contextlib.suppress(BaseException):
                await self._retention_task
            self._retention_task = None

    async def _start_scheduler(self, startup: bool) -> None:  # noqa: ARG002
        self.on = True
        await self.recompute_all()
        await self._handle_missed()  # also after a long OFF period
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="netmon-report-scheduler")

    async def _stop_scheduler(self) -> None:
        self.on = False
        for t in list(self._retry_tasks):
            t.cancel()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def idle(self) -> None:
        """Tests: wait for running report tasks."""
        while pending := [t for t in self._runs if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------ schedule
    async def _tz(self, session: Any) -> str:
        from netmon.services import settings_p3c

        return await settings_p3c.timezone(session)

    async def recompute_all(self) -> None:
        """next_run_at of every enabled scheduled report (after toggles / tz changes); keeps
        an existing future value."""
        now = self.rt.clock.now()
        async with self.rt.sessionmaker() as session:
            tz = await self._tz(session)
            rows = (
                await session.execute(
                    text(
                        "SELECT id, enabled, schedule, weekday, month_day, send_time, next_run_at "
                        "FROM reports"
                    )
                )
            ).all()
            for r in rows:
                nxt = None
                if r.enabled and r.schedule != "none":
                    nxt = r.next_run_at
                    if nxt is None:
                        nxt = compute.next_run(
                            r.schedule, r.weekday, r.month_day, r.send_time, tz, now
                        )
                if nxt != r.next_run_at:
                    await session.execute(
                        text("UPDATE reports SET next_run_at = :n WHERE id = :i"),
                        {"n": nxt, "i": r.id},
                    )
            await session.commit()
        self.wake()

    async def _handle_missed(self) -> None:
        from netmon.services import events as events_svc

        now = self.rt.clock.now()
        async with self.rt.sessionmaker() as session:
            tz = await self._tz(session)
            rows = (
                await session.execute(
                    text(
                        f"SELECT {REPORT_COLUMNS} FROM reports WHERE enabled "
                        "AND schedule <> 'none' AND next_run_at < :now"
                    ),
                    {"now": now},
                )
            ).all()
        for r in rows:
            late = (now - r.next_run_at).total_seconds()
            if late < MISSED_MAX_S:
                continue  # the loop runs it right away
            nxt = compute.next_run(r.schedule, r.weekday, r.month_day, r.send_time, tz, now)
            async with self.rt.sessionmaker() as session:
                await session.execute(
                    text("UPDATE reports SET next_run_at = :n WHERE id = :i"),
                    {"n": nxt, "i": r.id},
                )
                await session.commit()
            await events_svc.write_system_event(
                self.rt,
                f'Report "{r.name}" was not sent: its run at {iso(r.next_run_at)} was missed '
                "by more than 24 h while NetworkMonitor was not running",
            )

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Report scheduler pass failed")
            timeout = LOOP_MAX_S
            try:
                async with self.rt.sessionmaker() as session:
                    nxt = (
                        await session.execute(
                            text(
                                "SELECT min(next_run_at) FROM reports WHERE enabled "
                                "AND schedule <> 'none'"
                            )
                        )
                    ).scalar()
                if nxt is not None:
                    delta = (nxt - self.rt.clock.now()).total_seconds()
                    timeout = min(timeout, max(0.05, delta + 0.05))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            changed = self.rt.clock.changed
            waiters = [
                asyncio.ensure_future(self._wake.wait()),
                asyncio.ensure_future(changed.wait()),
            ]
            try:
                await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()
            self._wake.clear()

    async def run_due(self) -> int:
        """Start every due scheduled report (one at a time; each run awaited)."""
        now = self.rt.clock.now()
        async with self.rt.sessionmaker() as session:
            tz = await self._tz(session)
            rows = (
                await session.execute(
                    text(
                        f"SELECT {REPORT_COLUMNS} FROM reports WHERE enabled "
                        "AND schedule <> 'none' AND next_run_at <= :now ORDER BY next_run_at, id"
                    ),
                    {"now": now},
                )
            ).all()
        n = 0
        for r in rows:
            rep = report_dict(r)
            start, end = compute.period_for(r.schedule, r.next_run_at, tz)
            nxt = compute.next_run(r.schedule, r.weekday, r.month_day, r.send_time, tz, now)
            async with self.rt.sessionmaker() as session:
                await session.execute(
                    text("UPDATE reports SET next_run_at = :n WHERE id = :i"),
                    {"n": nxt, "i": r.id},
                )
                await session.commit()
            try:
                run = await self.start_run(rep, start, end, "schedule", True, None)
            except ReportBusy:
                continue
            n += 1
            await self.wait_run(run["id"])
        return n

    async def wait_run(self, run_id: int) -> None:
        for t in list(self._runs):
            if t.get_name() == f"report-run-{run_id}":
                with contextlib.suppress(BaseException):
                    await t

    # ------------------------------------------------------------ runs
    async def start_run(
        self,
        report: dict[str, Any],
        start: datetime,
        end: datetime,
        trigger: str,
        send: bool,
        created_by: str | None,
        targets: list[str] | None = None,
    ) -> dict[str, Any]:
        rid = report.get("id")
        if rid is not None and rid in self._running:
            raise ReportBusy()
        now = self.rt.clock.now()
        async with self.rt.sessionmaker() as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO report_runs (report_id, report_name, trigger, period_start, "
                        "period_end, started_at, status, language, data, created_by) VALUES "
                        "(:rid, :name, :trigger, :ps, :pe, :now, 'running', :lang, '{}', :by) "
                        f"RETURNING {RUN_COLUMNS}"
                    ),
                    {
                        "rid": rid,
                        "name": report["name"][:128],
                        "trigger": trigger,
                        "ps": start,
                        "pe": end,
                        "now": now,
                        "lang": report.get("language") or "en",
                        "by": created_by,
                    },
                )
            ).one()
            if rid is not None:
                await session.execute(
                    text("UPDATE reports SET last_run_id = :r WHERE id = :i"),
                    {"r": row.id, "i": rid},
                )
            await session.commit()
        out = run_out(row)
        if rid is not None:
            self._running.add(rid)
        self.rt.hub.broadcast("report.run", {"run": out})
        task = asyncio.create_task(
            self._execute(out, report, send, targets), name=f"report-run-{out['id']}"
        )
        self._runs.add(task)
        task.add_done_callback(self._runs.discard)
        return out

    async def _execute(
        self,
        run: dict[str, Any],
        report: dict[str, Any],
        send: bool,
        targets: list[str] | None,
    ) -> None:
        from netmon.services import settings_p3c

        status = "failed"
        data: dict[str, Any] = {}
        html_doc: str | None = None
        deliveries: list[dict[str, Any]] = []
        try:
            async with self._lock:
                start = _parse(run["period_start"])
                end = _parse(run["period_end"])
                async with self.rt.sessionmaker() as session:
                    tz = await settings_p3c.timezone(session)
                    base = await settings_p3c.public_base_url(session)
                    feats = self.rt.features.snapshot()
                    data = await report_data.gather(
                        session, report, start, end, tz, feats, self.rt.clock.now(), self.rt
                    )
                ai = getattr(self.rt, "ai_features", None)  # [3b] § 14.8.10, never fails a report
                if ai is not None:
                    data["ai_summary"] = await ai.report_summary(report, run, data)
                link = f"{base}/reports/runs/{run['id']}" if base else None
                html_doc = await asyncio.to_thread(
                    render.html_report,
                    data,
                    report["name"],
                    report.get("language") or "en",
                    report.get("schedule") or "none",
                    link,
                )
                status = "ok"
                if send:
                    tg = targets if targets is not None else report_send.targets_of(report)
                    if tg:
                        deliveries = await report_send.deliver(
                            self.rt, tg, data, html_doc, run, report, link
                        )
                        if any(not d["ok"] for d in deliveries):
                            status = "partial" if any(d["ok"] for d in deliveries) else "failed"
        except Exception:  # noqa: BLE001
            log.exception("Report run %s failed", run["id"])
            status = "failed"
            if run.get("trigger") == "schedule":
                self._schedule_retry(report, run, send)
        finally:
            if report.get("id") is not None:
                self._running.discard(report["id"])
        await self._finish(run["id"], status, data, html_doc, deliveries)

    def _schedule_retry(self, report: dict[str, Any], run: dict[str, Any], send: bool) -> None:
        """§ 13.12 (e.g. DB down): a failed scheduled run is retried, at most 3 attempts
        within 6 h of the first."""
        rid = report.get("id")
        if rid is None or not self.on:
            return
        now = time.monotonic()
        n, first = self._retries.get(rid, (0, now))
        if n >= RETRY_MAX or now - first > RETRY_WINDOW_S:
            self._retries.pop(rid, None)
            return
        self._retries[rid] = (n + 1, first)

        async def later() -> None:
            await asyncio.sleep(self.retry_delay_s)
            if not self.on:
                return
            try:
                await self.start_run(
                    report,
                    _parse(run["period_start"]),
                    _parse(run["period_end"]),
                    "schedule",
                    send,
                    None,
                )
            except ReportBusy:
                pass

        task = asyncio.create_task(later(), name=f"report-retry-{rid}")
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)

    async def _finish(
        self,
        run_id: int,
        status: str,
        data: dict[str, Any],
        html_doc: str | None,
        deliveries: list[dict[str, Any]],
        merge: bool = False,
    ) -> None:
        try:
            async with self.rt.sessionmaker() as session:
                if merge:
                    row = (
                        await session.execute(
                            text("SELECT deliveries FROM report_runs WHERE id = :i"), {"i": run_id}
                        )
                    ).scalar_one()
                    old = row if isinstance(row, list) else json.loads(row or "[]")
                    new_targets = {d["target"] for d in deliveries}
                    deliveries = [d for d in old if d["target"] not in new_targets] + deliveries
                    oks = [d["ok"] for d in deliveries]
                    status = "ok" if all(oks) else "partial" if any(oks) else "failed"
                    row = (
                        await session.execute(
                            text(
                                "UPDATE report_runs SET deliveries = CAST(:d AS jsonb), "
                                f"status = :s WHERE id = :i RETURNING {RUN_COLUMNS}"
                            ),
                            {"d": json.dumps(deliveries), "s": status, "i": run_id},
                        )
                    ).one()
                else:
                    row = (
                        await session.execute(
                            text(
                                "UPDATE report_runs SET status = :s, finished_at = :f, "
                                "data = CAST(:data AS jsonb), html = :html, "
                                "deliveries = CAST(:d AS jsonb) "
                                f"WHERE id = :i RETURNING {RUN_COLUMNS}"
                            ),
                            {
                                "s": status,
                                "f": self.rt.clock.now(),
                                "data": json.dumps(data, ensure_ascii=False),
                                "html": html_doc,
                                "d": json.dumps(deliveries, ensure_ascii=False),
                                "i": run_id,
                            },
                        )
                    ).one()
                await session.commit()
        except Exception:  # noqa: BLE001
            log.exception("Storing report run %s failed", run_id)
            return
        self.rt.hub.broadcast("report.run", {"run": run_out(row)})

    async def resend(self, run_id: int, targets: list[str]) -> None:
        """Re-deliver a stored run to `targets` (background)."""

        async def go() -> None:
            async with self.rt.sessionmaker() as session:
                r = (
                    await session.execute(
                        text(f"SELECT {RUN_COLUMNS}, data, html FROM report_runs WHERE id = :i"),
                        {"i": run_id},
                    )
                ).one()
                rep_row = None
                if r.report_id is not None:
                    rep_row = (
                        await session.execute(
                            text(f"SELECT {REPORT_COLUMNS} FROM reports WHERE id = :i"),
                            {"i": r.report_id},
                        )
                    ).first()
                from netmon.services import settings_p3c

                base = await settings_p3c.public_base_url(session)
            report = report_dict(rep_row) if rep_row is not None else {"schedule": "none"}
            data = r.data if isinstance(r.data, dict) else json.loads(r.data or "{}")
            link = f"{base}/reports/runs/{run_id}" if base else None
            deliveries = await report_send.deliver(
                self.rt, targets, data, r.html or "", run_out(r), report, link
            )
            await self._finish(run_id, "", {}, None, deliveries, merge=True)

        task = asyncio.create_task(go(), name=f"report-resend-{run_id}")
        self._runs.add(task)
        task.add_done_callback(self._runs.discard)

    # ------------------------------------------------------------ retention
    async def purge(self) -> int:
        from netmon.services import settings_p3c

        async with self.rt.sessionmaker() as session:
            days = int(await settings_p3c.get(session, "report_retention_days") or 400)
            res = await session.execute(
                text("DELETE FROM report_runs WHERE started_at < :cut"),
                {"cut": self.rt.clock.now() - timedelta(days=days)},
            )
            await session.commit()
        return int(res.rowcount or 0)  # type: ignore[attr-defined]

    async def _retention_loop(self) -> None:
        while True:
            try:
                await self.purge()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.debug("Report retention failed", exc_info=True)
            await asyncio.sleep(RETENTION_EVERY_S)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
