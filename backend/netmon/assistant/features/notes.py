"""[3b] AI note in alert notifications (`ai_alert_notes`, architecture § 14.8.3).

The dispatcher sends exactly as before; after a successful **problem** send of a rule with
`ai_note` it calls `NoteWorker.after_sends(...)` (non-blocking): the delivery targets are
recorded (`assistant_note_targets`) and one job per problem event is queued (bounded queue 50,
overflow → `queue_full`; 2 workers). The note (light model, ≤ 200 output tokens, ≤ 300 chars)
must be ready before `sent_at + alert_note_timeout_s`, else it is skipped (`timeout`). Delivery:
Telegram edit (fallback reply), Teams follow-up card, email threaded reply; webhooks are not
supported (`unsupported_channel`). Any error → skip silently (counted, status line)."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import text

from netmon.assistant.features import common

log = logging.getLogger("netmon.assistant.notes")

QUEUE_MAX = 50
WORKERS = 2
NOTE_MAX_CHARS = 300
NOTE_TYPES = ("telegram", "teams", "email")
TELEGRAM_MAX = 4096

NOTE_BLOCK = (
    "Task: write a 1–2 line note (at most 300 characters, plain text, no Markdown, no links) "
    "that is appended to an alert notification that was already sent. Say what most likely "
    "matters for this alert based only on the data given (recent related events, dependencies, "
    "maintenance, preceding changes); mark a guess as a guess. If the data shows nothing "
    "useful, say so briefly."
)


# ------------------------------------------------------------------ pure builders
def clean_note(text_: str) -> str:
    s = " ".join((text_ or "").replace("\r", " ").split())
    s = s.replace("**", "").replace("`", "")
    if len(s) > NOTE_MAX_CHARS:
        s = s[: NOTE_MAX_CHARS - 1].rstrip() + "…"
    return s


def telegram_edit_text(original: str, note: str, parse_mode: str | None, lang: str = "en") -> str:
    """The edited message: original + a separated `AI: <note>` line (HTML-escaped)."""
    if parse_mode == "HTML":
        add = f"\n\n<i>AI:</i> {html.escape(note, quote=False)}"
    else:
        add = f"\n\nAI: {note}"
    if len(original) + len(add) > TELEGRAM_MAX:
        return ""  # too long to edit → reply instead
    return original + add


def telegram_reply_text(note: str, parse_mode: str | None, lang: str = "en") -> str:
    label = "הערת AI:" if lang == "he" else "AI note:"
    if parse_mode == "HTML":
        return f"<i>{label}</i> {html.escape(note, quote=False)}"
    return f"{label} {note}"


def teams_card(payload_title: str, note: str, url: str | None, lang: str = "en") -> dict[str, Any]:
    head = ("הערת AI: " if lang == "he" else "AI note: ") + payload_title
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5" if lang == "he" else "1.4",
        "msteams": {"width": "Full"},
        "body": [
            {"type": "TextBlock", "text": head[:300], "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": note, "wrap": True},
            {
                "type": "TextBlock",
                "text": common.AI_LABEL.get(lang, common.AI_LABEL["en"]),
                "isSubtle": True,
                "size": "Small",
                "wrap": True,
            },
        ],
    }
    if lang == "he":
        card["rtl"] = True
    if url:
        card["actions"] = [
            {
                "type": "Action.OpenUrl",
                "title": "פתח ב-NetworkMonitor" if lang == "he" else "Open in NetworkMonitor",
                "url": url,
            }
        ]
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }


def email_reply(
    ref: dict[str, Any], note: str, lang: str = "en"
) -> tuple[str, str, str, dict[str, str]]:
    """(subject, text, html, headers) of the threaded reply."""
    subject = str(ref.get("subject") or "NetworkMonitor")
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    label = common.AI_LABEL.get(lang, common.AI_LABEL["en"])
    text_ = f"{note}\n\n— {label}. It may be wrong."
    html_ = (
        f'<div dir="{"rtl" if lang == "he" else "ltr"}"><p>{html.escape(note)}</p>'
        f'<p style="color:#666;font-size:12px">{html.escape(label)}</p></div>'
    )
    mid = str(ref.get("message_id") or "")
    headers = {"In-Reply-To": mid, "References": mid} if mid else {}
    return subject, text_, html_, headers


# ------------------------------------------------------------------ worker
@dataclass
class NoteJob:
    event_id: int
    rule_id: int | None
    sensor_id: int | None
    device_id: int | None
    sent_monotonic: float
    sent_at: datetime
    title: str
    url: str | None
    target_ids: list[int] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)


class NoteWorker:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.on = False
        self._queue: asyncio.Queue[NoteJob] = asyncio.Queue(QUEUE_MAX)
        self._workers: list[asyncio.Task[None]] = []
        self._pending: set[asyncio.Task[Any]] = set()
        self.last_status: str | None = None
        self.counts: dict[str, int] = {}
        self.transport: httpx.AsyncBaseTransport | None = None  # tests
        self.email_sender: Any = None  # tests: async (smtp, to, subject, text, html, headers)
        self.teams_sender: Any = None  # tests: async (config, body)

    async def start(self) -> None:
        if self.on:
            return
        self.on = True
        self._workers = [
            asyncio.create_task(self._run(), name=f"netmon-ai-notes-{i}") for i in range(WORKERS)
        ]

    async def stop(self) -> None:
        self.on = False
        for t in [*self._workers, *self._pending]:
            t.cancel()
        for t in [*self._workers, *list(self._pending)]:
            with contextlib.suppress(BaseException):
                await t
        self._workers = []
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()

    async def idle(self) -> None:
        while True:
            pend = [t for t in self._pending if not t.done()]
            if pend:
                await asyncio.gather(*pend, return_exceptions=True)
                continue
            await self._queue.join()
            if not [t for t in self._pending if not t.done()]:
                return

    def _count(self, status: str) -> None:
        self.counts[status] = self.counts.get(status, 0) + 1
        self.last_status = status

    # ------------------------------------------------------------ dispatcher hook
    def after_sends(
        self, plans: list[Any], rows: list[Any], channels: dict[int, Any], receipts: dict[Any, Any]
    ) -> None:
        """Called by the alert engine after a send pass (sync, never blocks the alert)."""
        if not self.on:
            return
        items = []
        for p, r in zip(plans, rows, strict=True):
            if r.event_id is None:
                continue
            for s in p.sends:
                if s.stage.split(":", 1)[0] != "problem" or not s.ok:
                    continue
                targets = []
                for cid in s.ok:
                    key = s.keys.get(cid)
                    if key not in receipts:
                        continue
                    targets.append((cid, channels.get(cid) or {}, receipts.get(key)))
                if targets:
                    items.append((p, r, s.payload, targets))
        if not items:
            return
        task = asyncio.ensure_future(self._enqueue(items))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _enqueue(self, items: list[Any]) -> None:
        now_m = time.monotonic()
        now = self.rt.clock.now()
        rule_ids = sorted({p.rule_id for p, *_ in items})
        async with self.rt.sessionmaker() as session:
            if not await common.active(self.rt, "ai_alert_notes", session):
                return
            flags = {
                r.id: bool(r.ai_note)
                for r in await session.execute(
                    text("SELECT id, ai_note FROM alert_rules WHERE id = ANY(:ids)"),
                    {"ids": rule_ids},
                )
            }
            jobs: dict[int, NoteJob] = {}
            for p, r, payload, targets in items:
                if not flags.get(p.rule_id):
                    continue
                job = jobs.get(r.event_id)
                if job is None:
                    sensor = payload.get("sensor") or {}
                    device = payload.get("device") or {}
                    status = str(payload.get("new_status") or "").upper()
                    job = jobs[r.event_id] = NoteJob(
                        event_id=r.event_id,
                        rule_id=p.rule_id,
                        sensor_id=r.sensor_id,
                        device_id=r.device_id,
                        sent_monotonic=now_m,
                        sent_at=now,
                        title=f"{status} {device.get('name', '?')} / {sensor.get('name', '?')}",
                        url=payload.get("url"),
                    )
                for cid, ch, receipt in targets:
                    ctype = ch.get("type")
                    if ctype not in NOTE_TYPES:
                        job.skipped.append({"channel_id": cid, "reason": "unsupported_channel"})
                        continue
                    if ctype == "email" and not (ch.get("config") or {}).get("ai_note_email", True):
                        continue
                    tid = (
                        await session.execute(
                            text(
                                "INSERT INTO assistant_note_targets (event_id, rule_id, "
                                "channel_id, "
                                "channel_type, ref, sent_at) VALUES (:e, :r, :c, :t, CAST(:ref AS "
                                "jsonb), :at) RETURNING id"
                            ),
                            {
                                "e": r.event_id,
                                "r": p.rule_id,
                                "c": cid,
                                "t": ctype,
                                "ref": json.dumps(receipt or {}, ensure_ascii=False),
                                "at": now,
                            },
                        )
                    ).scalar_one()
                    job.target_ids.append(tid)
            await session.commit()
        for job in jobs.values():
            if not job.target_ids:
                await self._audit(job, "skipped", "unsupported_channel")
                continue
            try:
                self._queue.put_nowait(job)
            except asyncio.QueueFull:
                self._count("queue_full")
                await self._finish(job, "skipped", "queue_full")

    # ------------------------------------------------------------ processing
    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self.process(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("AI note for event %s failed", job.event_id)
            finally:
                self._queue.task_done()

    async def process(self, job: NoteJob) -> str:
        rt = self.rt
        async with rt.sessionmaker() as session:
            timeout_s = await common.int_setting(session, "alert_note_timeout_s")
            timeout_s = max(10, min(120, timeout_s))
            remaining = timeout_s - (time.monotonic() - job.sent_monotonic)
            if remaining <= 1:
                return await self._finish(job, "skipped", "timeout")
            if not await common.active(rt, "ai_alert_notes", session):
                return await self._finish(job, "skipped", "feature_off")
            # incident attach: only the root's alert gets a (summary) note
            if await common.setting(session, "correlation_attach_to_alert") and common.on(
                rt, "ai_correlation"
            ):
                inc = (
                    await session.execute(
                        text(
                            "SELECT i.id, i.root_device_id FROM assistant_incident_members m JOIN "
                            "assistant_incidents i ON i.id = m.incident_id WHERE m.event_id = :e "
                            "AND i.state = 'open' LIMIT 1"
                        ),
                        {"e": job.event_id},
                    )
                ).first()
                if inc is not None and inc.root_device_id != job.device_id:
                    return await self._finish(job, "skipped", "incident_member")
            existing = await common.get_artifact(session, "alert_note", f"note:{job.event_id}")
            note = existing.content if existing is not None and existing.status == "ok" else None
            if note is None:
                cap = await common.int_setting(session, "alert_notes_daily_cap")
                if await common.purpose_count_today(rt, session, "alert_note") >= cap:
                    return await self._finish(job, "skipped", "cap")
                from netmon.assistant.features import explain_gather

                ev = (
                    await explain_gather.problem_event(session, job.sensor_id, job.event_id)
                    if job.sensor_id
                    else None
                )
                facts = (
                    await explain_gather.collect(rt, session, job.sensor_id, ev, reduced=True)
                    if job.sensor_id
                    else {}
                )
                a = await common.assistant_settings(session)
                policy = common.redact_policy(a)
                lang = await common.language_default(session)
                system = await common.system_prompt(
                    rt,
                    "alert_note",
                    NOTE_BLOCK + f" Write in {'Hebrew' if lang == 'he' else 'English'}.",
                    redaction=bool(a.get("redact_internal_ips")),
                    session=session,
                )
            else:
                lang = await common.language_default(session)
        comp = None
        if note is None:
            try:
                comp = await asyncio.wait_for(
                    common.complete(
                        rt,
                        purpose="alert_note",
                        system=system,
                        messages=[
                            common.json_message(
                                "Alert data (JSON):", common.redact_value(facts, policy)
                            )
                        ],
                        light=True,
                        max_tokens=200,
                        timeout_s=max(1.0, remaining),
                    ),
                    max(1.0, remaining),
                )
            except TimeoutError:
                return await self._finish(job, "skipped", "timeout")
            except Exception as exc:  # noqa: BLE001
                return await self._finish(job, "skipped", common.to_ai_error(exc).code)
            if time.monotonic() - job.sent_monotonic > timeout_s:
                return await self._finish(job, "skipped", "timeout", comp)
            note = clean_note(str(common.detokenize(comp.text, policy)))
            if not note:
                return await self._finish(job, "skipped", "empty", comp)
            async with rt.sessionmaker() as session:
                await common.put_artifact(
                    session,
                    "alert_note",
                    f"note:{job.event_id}",
                    "ok",
                    content=note,
                    data={"rule_id": job.rule_id},
                    comp=comp,
                )
                await session.commit()
        results = await self.deliver(job.target_ids, note, job.title, job.url, lang)
        status = (
            "sent"
            if any(v in ("edited", "replied", "sent") for v in results.values())
            else "failed"
        )
        return await self._finish(
            job, status, None if status == "sent" else "delivery_failed", comp, results
        )

    async def _finish(
        self,
        job: NoteJob,
        status: str,
        reason: str | None,
        comp: Any = None,
        results: dict[int, str] | None = None,
    ) -> str:
        self._count(reason or status)
        if status == "skipped":
            with contextlib.suppress(Exception):
                async with self.rt.sessionmaker() as session:
                    if job.target_ids:
                        await session.execute(
                            text(
                                "UPDATE assistant_note_targets SET note_status = :s WHERE id = "
                                "ANY(:ids) "
                                "AND note_status IS NULL"
                            ),
                            {"s": f"skipped:{reason}"[:64], "ids": job.target_ids},
                        )
                    old = await common.get_artifact(session, "alert_note", f"note:{job.event_id}")
                    if old is None or old.status != "ok":
                        await common.put_artifact(
                            session,
                            "alert_note",
                            f"note:{job.event_id}",
                            "skipped",
                            skip_reason=reason,
                            data={"rule_id": job.rule_id},
                            comp=comp,
                        )
                    await session.commit()
        await self._audit(job, status, reason, comp, results)
        return reason or status

    async def _audit(
        self,
        job: NoteJob,
        status: str,
        reason: str | None,
        comp: Any = None,
        results: dict[int, str] | None = None,
    ) -> None:
        changes: dict[str, Any] = {
            "event_id": job.event_id,
            "rule_id": job.rule_id,
            "status": status,
            "channels": sorted({*(results or {}).keys()}) or job.target_ids,
            **common.usage_changes(comp),
        }
        if reason:
            changes["skip_reason"] = reason
        if job.skipped:
            changes["unsupported"] = [s["channel_id"] for s in job.skipped]
        await common.audit_system(self.rt, "assistant.alert_note", "event", job.event_id, changes)

    # ------------------------------------------------------------ delivery
    async def deliver(
        self, target_ids: list[int], note: str, title: str, url: str | None, lang: str
    ) -> dict[int, str]:
        """Deliver to every recorded target that has no note yet. Returns {channel_id: status}."""
        out: dict[int, str] = {}
        async with self.rt.sessionmaker() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT t.id, t.channel_id, t.channel_type, t.ref, t.note_status, c.config "
                        "FROM assistant_note_targets t JOIN notification_channels c ON c.id = "
                        "t.channel_id WHERE t.id = ANY(:ids)"
                    ),
                    {"ids": target_ids},
                )
            ).all()
            from netmon.services import settings as settings_svc

            smtp = await settings_svc.get(session, "smtp")
        for r in rows:
            ref = common.jsonb(r.ref) or {}
            cfg = common.jsonb(r.config) or {}
            try:
                if r.channel_type == "telegram":
                    st = await self._telegram(cfg, ref, note, lang)
                elif r.channel_type == "teams":
                    await self._teams(cfg, teams_card(title, note, url, lang))
                    st = "sent"
                else:
                    await self._email(smtp, ref, note, lang)
                    st = "replied"
            except Exception as exc:  # noqa: BLE001
                log.info(
                    "AI note delivery via channel %s failed: %s", r.channel_id, type(exc).__name__
                )
                st = "failed"
            out[r.channel_id] = st
            with contextlib.suppress(Exception):
                async with self.rt.sessionmaker() as session:
                    await session.execute(
                        text("UPDATE assistant_note_targets SET note_status = :s WHERE id = :i"),
                        {"s": st if st != "failed" else "skipped:delivery_failed", "i": r.id},
                    )
                    await session.commit()
        return out

    async def _tg_call(self, token: str, method: str, body: dict[str, Any]) -> Any:
        from netmon.alerts import telegram as tg

        kw: dict[str, Any] = {"timeout": 10.0}
        if self.transport is not None:
            kw["transport"] = self.transport
        else:
            from netmon.tlsutil import client_ssl_context

            kw["verify"] = client_ssl_context()
            kw["trust_env"] = True
        async with httpx.AsyncClient(**kw) as c:
            resp = await c.post(f"{tg.api_base()}/bot{token}/{method}", json=body)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("ok") is True:
            return data.get("result")
        raise RuntimeError(f"telegram {method} failed ({resp.status_code})")

    async def _telegram(
        self, cfg: dict[str, Any], ref: dict[str, Any], note: str, lang: str
    ) -> str:
        token = str(cfg.get("bot_token") or "")
        chat_id = ref.get("chat_id") or cfg.get("chat_id")
        mid = ref.get("message_id")
        if not token or chat_id is None or mid is None:
            raise RuntimeError("no telegram target")
        pm = ref.get("parse_mode") or cfg.get("parse_mode", "HTML")
        edited = (
            telegram_edit_text(str(ref.get("text") or ""), note, pm, lang)
            if ref.get("text")
            else ""
        )
        if edited:
            body: dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": mid,
                "text": edited,
                "disable_web_page_preview": True,
            }
            if pm:
                body["parse_mode"] = pm
            if ref.get("reply_markup"):
                body["reply_markup"] = ref["reply_markup"]
            try:
                await self._tg_call(token, "editMessageText", body)
                return "edited"
            except Exception:  # noqa: BLE001 - too old / edited by others → reply
                pass
        body = {
            "chat_id": chat_id,
            "text": telegram_reply_text(note, pm, lang),
            "reply_to_message_id": mid,
            "disable_web_page_preview": True,
        }
        if pm:
            body["parse_mode"] = pm
        await self._tg_call(token, "sendMessage", body)
        return "replied"

    async def _teams(self, cfg: dict[str, Any], body: dict[str, Any]) -> None:
        if self.teams_sender is not None:
            await self.teams_sender(cfg, body)
            return
        from netmon.alerts import teams

        await teams.send_teams(cfg, body)

    async def _email(self, smtp: dict[str, Any], ref: dict[str, Any], note: str, lang: str) -> None:
        to = list(ref.get("to") or [])
        if not to:
            raise RuntimeError("no recipients")
        subject, text_, html_, headers = email_reply(ref, note, lang)
        if self.email_sender is not None:
            await self.email_sender(smtp, to, subject, text_, html_, headers)
            return
        from netmon.alerts.email import send_email

        await send_email(smtp, to, subject, text_, html_, headers=headers)

    # ------------------------------------------------------------ incidents (attach)
    async def incident_note(self, incident_id: int) -> str:
        """`correlation_attach_to_alert`: the incident summary replaces the root alert's note
        (one follow-up per incident)."""
        rt = self.rt
        async with rt.sessionmaker() as session:
            if not await common.active(rt, "ai_alert_notes", session):
                return "feature_off"
            inc = (
                await session.execute(
                    text(
                        "UPDATE assistant_incidents SET note_sent = true WHERE id = :i AND NOT "
                        "note_sent AND ai_summary IS NOT NULL RETURNING id, root_device_id, "
                        "ai_title, title, ai_summary"
                    ),
                    {"i": incident_id},
                )
            ).first()
            if inc is None:
                return "done"
            targets = (
                (
                    await session.execute(
                        text(
                            "SELECT t.id FROM assistant_note_targets t JOIN "
                            "assistant_incident_members m "
                            "ON m.event_id = t.event_id WHERE m.incident_id = :i AND m.device_id "
                            "= :d"
                        ),
                        {"i": incident_id, "d": inc.root_device_id},
                    )
                )
                .scalars()
                .all()
            )
            lang = await common.language_default(session)
            base = await common.public_base(session)
            await session.commit()
        if not targets:
            return "no_target"
        url = f"{base.rstrip('/')}/incidents/{incident_id}" if base else None
        # a fresh delivery: reset the targets so they get the summary
        async with rt.sessionmaker() as session:
            await session.execute(
                text("UPDATE assistant_note_targets SET note_status = NULL WHERE id = ANY(:ids)"),
                {"ids": list(targets)},
            )
            await session.commit()
        note = clean_note(inc.ai_summary)
        await self.deliver(list(targets), note, inc.ai_title or inc.title, url, lang)
        return "sent"

    async def purge(self) -> int:
        async with self.rt.sessionmaker() as session:
            n = (
                await session.execute(
                    text("DELETE FROM assistant_note_targets WHERE sent_at < :c"),
                    {"c": self.rt.clock.now() - __import__("datetime").timedelta(days=2)},
                )
            ).rowcount
            await session.commit()
        return int(n or 0)
