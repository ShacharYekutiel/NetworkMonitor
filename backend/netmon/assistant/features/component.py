"""[3b] `rt.ai_features`: the runtime component of the AI features (architecture § 14.1
startup: correlation listener → alert-note worker → digest scheduler; the Telegram `/ask`
handler is called by the phase-3 poller).

Hooks used by phase-1..3 code (each is a no-op while its feature is OFF or idle):

    rt.ai_features.on_events(events)                          # services.events.published
    rt.ai_features.after_alert_sends(plans, rows, chans, rc)  # alerts.engine (after sending)
    await rt.ai_features.report_summary(report, run, data)    # reports.scheduler (before render)
    await rt.ai_features.chat_ask_handle(poller, bot, msg)    # chatack.telegram_poller
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import text

from netmon.assistant.features import common
from netmon.assistant.features.chatask import ChatAsk
from netmon.assistant.features.correlate import CorrelationService
from netmon.assistant.features.digest import DigestService
from netmon.assistant.features.notes import NoteWorker
from netmon.assistant.features.templates import TemplateDrafts

log = logging.getLogger("netmon.assistant.features")

RETENTION_EVERY_S = 3600.0
DIGEST_KEEP_DAYS = 400


class AiFeatures:
    features = (
        "assistant",
        *common.AI_FEATURES,
        "syslog",
        "chat_ack",
        "config_backup",
        "reports",
    )

    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.correlation = CorrelationService(rt)
        self.notes = NoteWorker(rt)
        self.digest = DigestService(rt)
        self.templates = TemplateDrafts(rt)
        self.chatask = ChatAsk(rt)
        self._retention: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.started = False
        svc = getattr(rt, "assistant", None)
        providers = getattr(svc, "status_providers", None)
        if isinstance(providers, list):
            providers.append(self.status)

    # ------------------------------------------------------------ component API
    async def start(self, snapshot: dict[str, bool]) -> None:
        self.started = True
        await self._apply(snapshot)
        if self._retention is None:
            self._retention = asyncio.create_task(
                self._retention_loop(), name="netmon-ai-retention"
            )

    async def feature_changed(self, name: str, on: bool) -> None:
        await self._apply(self.rt.features.snapshot())
        if name in ("ai_chat_ask", "assistant"):
            pollers = getattr(self.rt, "telegram_pollers", None)
            if pollers is not None:
                with contextlib.suppress(Exception):
                    pollers.channels_changed()

    async def stop(self) -> None:
        self.started = False
        if self._retention is not None:
            self._retention.cancel()
            with contextlib.suppress(BaseException):
                await self._retention
            self._retention = None
        await self.chatask.stop()
        await self.digest.stop()
        await self.notes.stop()
        await self.correlation.stop(mark_stale=False)
        await self.templates.stop()

    async def _apply(self, snap: dict[str, bool]) -> None:
        async with self._lock:
            base = bool(snap.get("assistant"))
            want = {
                "correlation": base and bool(snap.get("ai_correlation")),
                "notes": base and bool(snap.get("ai_alert_notes")),
                "digest": base and bool(snap.get("ai_syslog_digest")) and bool(snap.get("syslog")),
                "templates": base and bool(snap.get("ai_template_drafts")),
            }
            for key, on in want.items():
                comp = getattr(self, key)
                try:
                    if on and not comp.on:
                        await comp.start()
                    elif not on and comp.on:
                        await comp.stop()
                except Exception:  # noqa: BLE001
                    log.exception("Applying AI feature %s failed", key)

    async def idle(self) -> None:
        """Tests: wait for background AI work."""
        await self.correlation.idle()
        await self.notes.idle()
        await self.digest.idle()
        await self.templates.idle()
        await self.chatask.idle()

    # ------------------------------------------------------------ hooks
    def on_events(self, events: list[dict[str, Any]]) -> None:
        if self.correlation.on:
            self.correlation.on_events(events)

    def after_alert_sends(self, plans: Any, rows: Any, channels: Any, receipts: Any) -> None:
        if self.notes.on:
            self.notes.after_sends(plans, rows, channels, receipts)

    async def report_summary(
        self, report: dict[str, Any], run: dict[str, Any], data: dict[str, Any]
    ) -> str | None:
        from netmon.assistant.features import reportsummary

        try:
            summary, status = await reportsummary.summarize(self.rt, report, run, data)
        except Exception:  # noqa: BLE001 - never fails a report
            log.exception("Report summary failed")
            return None
        if run.get("id") is not None:
            await reportsummary.set_status(self.rt, int(run["id"]), status)
        return summary

    async def chat_ask_handle(self, poller: Any, bot: Any, msg: dict[str, Any]) -> bool:
        try:
            return await self.chatask.handle(poller, bot, msg)
        except Exception:  # noqa: BLE001
            log.exception("Telegram /ask failed")
            return False

    # ------------------------------------------------------------ status lines
    async def status(self, user_id: int | None = None) -> dict[str, Any]:
        lines: dict[str, str] = {}
        with contextlib.suppress(Exception):
            async with self.rt.sessionmaker() as session:
                n = (
                    await session.execute(
                        text("SELECT count(*) FROM assistant_incidents WHERE state = 'open'")
                    )
                ).scalar()
                lines["ai_correlation"] = f"{int(n or 0)} open incidents"
                last = (
                    await session.execute(
                        text(
                            "SELECT key, status, skip_reason FROM assistant_artifacts WHERE kind = "
                            "'digest' ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                ).first()
                if last is not None:
                    lines["ai_syslog_digest"] = (
                        f"Last digest {last.key.split(':', 1)[-1]}: {last.status}"
                        + (f" ({last.skip_reason})" if last.skip_reason else "")
                    )
        if self.notes.last_status:
            st = self.notes.last_status
            lines["ai_alert_notes"] = (
                "Last note sent" if st == "sent" else f"Last note skipped: {st}"
            )
        return {"status_lines": lines}

    # ------------------------------------------------------------ retention
    async def _retention_loop(self) -> None:
        while True:
            await asyncio.sleep(RETENTION_EVERY_S)
            try:
                await self.purge()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("AI feature retention failed")

    async def purge(self) -> dict[str, int]:
        """Artifacts (`assistant_artifact_retention_days`; digests 400 days), resolved/stale
        incidents, note targets (2 days), SNMP walks (7 days)."""
        out: dict[str, int] = {}
        now = self.rt.clock.now()
        async with self.rt.sessionmaker() as session:
            days = await common.int_setting(session, "assistant_artifact_retention_days")
            cut = now - timedelta(days=max(7, days))
            out["artifacts"] = (
                await session.execute(
                    text(
                        "DELETE FROM assistant_artifacts WHERE (kind <> 'digest' AND created_at "
                        "< :c) "
                        "OR (kind = 'digest' AND created_at < :d)"
                    ),
                    {"c": cut, "d": now - timedelta(days=DIGEST_KEEP_DAYS)},
                )
            ).rowcount or 0
            out["incidents"] = (
                await session.execute(
                    text(
                        "DELETE FROM assistant_incidents WHERE state <> 'open' AND "
                        "last_change_at < :c"
                    ),
                    {"c": cut},
                )
            ).rowcount or 0
            await session.commit()
        out["note_targets"] = await self.notes.purge()
        out["walks"] = await self.templates.purge()
        return out
