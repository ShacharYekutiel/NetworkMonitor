"""[3b] `/ask` from Telegram (`ai_chat_ask`, architecture § 14.8.9).

The phase-3 long-poll (`chat_ack`) hands `/ask <question>` (also `/ask@BotName …`) to
`ChatAsk.handle(poller, bot, msg)` for bots of Telegram channels with `ai_ask: true`. Private
chats only; the sender must be linked (`chat_identities`) to an active admin; 1–1000 chars;
`chat_ask_per_user_hour` + the usual caps; one running question per user. The reply "Working
on it…" is edited with the answer (Telegram HTML, ≤ 3500 chars, "Full answer" link). Internal IP
placeholders stay placeholders in Telegram while `chat_redact_ips` is on."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections import defaultdict, deque
from typing import Any

from sqlalchemy import text

from netmon.assistant.features import common

log = logging.getLogger("netmon.assistant.chatask")

_ASK_RE = re.compile(r"^/ask(?:@(\w+))?(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
QUESTION_MAX = 1000
ANSWER_MAX = 3500
HOUR_S = 3600.0

TEXTS = {
    "en": {
        "working": "Working on it…",
        "private_only": "Use a private chat with the bot",
        "off": "Asking is turned off",
        "not_linked": (
            "Your Telegram account is not linked to NetworkMonitor (My profile → Chat accounts)"
        ),
        "not_admin": "Only administrators can ask the assistant",
        "empty": "Write a question after /ask (up to 1000 characters)",
        "too_long": "The question is longer than 1000 characters",
        "cap": "You reached the question limit{reset}",
        "busy": "Still working on your previous question",
        "error": "The assistant could not answer: {msg}",
        "full": "Full answer",
        "ai": "AI-generated, may be wrong",
    },
    "he": {
        "working": "עובד על זה…",
        "private_only": "השתמש בצ'אט פרטי עם הבוט",
        "off": "השאלות כבויות",
        "not_linked": "חשבון הטלגרם שלך אינו מקושר ל-NetworkMonitor (הפרופיל שלי ← חשבונות צ'אט)",
        "not_admin": "רק מנהלים יכולים לשאול את העוזר",
        "empty": "כתוב שאלה אחרי ‎/ask (עד 1000 תווים)",
        "too_long": "השאלה ארוכה מ-1000 תווים",
        "cap": "הגעת למגבלת השאלות{reset}",
        "busy": "עדיין עובד על השאלה הקודמת שלך",
        "error": "העוזר לא הצליח לענות: {msg}",
        "full": "התשובה המלאה",
        "ai": "נוצר ע״י AI, ייתכנו טעויות",
    },
}


def t(lang: str, key: str, **kw: Any) -> str:
    return TEXTS.get(lang, TEXTS["en"])[key].format(**kw)


def parse_ask(text_: str, bot_username: str | None) -> str | None:
    """`/ask q` / `/ask@Bot q` → the question ("" when empty); None when not an /ask for us."""
    m = _ASK_RE.match((text_ or "").strip())
    if not m:
        return None
    if m.group(1) and bot_username and m.group(1).lower() != bot_username.lower():
        return None
    return (m.group(2) or "").strip()


# ------------------------------------------------------------------ Markdown → Telegram HTML
_LINK = re.compile(r"\[([^\]\n]{1,300})\]\((nm:[^)\s]+|[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(
    r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])"
)
_CODE = re.compile(r"`([^`\n]+)`")


def _inline(line: str, base_url: str | None, kb: dict[str, str] | None) -> str:
    from netmon.assistant import links

    placeholders: list[str] = []

    def keep(s: str) -> str:
        placeholders.append(s)
        return f"\x00{len(placeholders) - 1}\x00"

    def code(m: re.Match[str]) -> str:
        return keep(f"<code>{html.escape(m.group(1), quote=False)}</code>")

    def link(m: re.Match[str]) -> str:
        label, target = m.group(1), m.group(2)
        url = links.absolute_url(target[3:], base_url, kb) if target.startswith("nm:") else None
        if url:
            return keep(f'<a href="{html.escape(url)}">{html.escape(label, quote=False)}</a>')
        return keep(html.escape(label, quote=False))

    s = _CODE.sub(code, line)
    s = _LINK.sub(link, s)
    s = html.escape(s, quote=False)
    s = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", s)
    s = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], s)


def to_telegram_html(
    md: str, base_url: str | None, kb: dict[str, str] | None = None, limit: int = ANSWER_MAX
) -> str:
    """Markdown answer → Telegram HTML (b/i/code/pre/a only, escaped), cut at whole lines to
    `limit` characters."""
    out: list[str] = []
    size = 0
    lines = (md or "").replace("\r", "").split("\n")
    i = 0
    truncated = False
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            piece = f"<pre>{html.escape(chr(10).join(body), quote=False)}</pre>"
        else:
            s = ln.rstrip()
            m = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", s)
            if m:
                piece = f"<b>{_inline(m.group(1), base_url, kb)}</b>"
            else:
                m = re.match(r"^(\s*)[-*+]\s+(.*)$", s)
                if m:
                    piece = f"{m.group(1)}• {_inline(m.group(2), base_url, kb)}"
                elif re.match(r"^\s*\|?\s*:?-{3,}", s):
                    piece = ""  # table separator rows
                else:
                    piece = _inline(s, base_url, kb)
        i += 1
        if size + len(piece) + 1 > limit:
            truncated = True
            break
        out.append(piece)
        size += len(piece) + 1
    text_ = "\n".join(out).strip()
    if truncated:
        text_ += "\n…"
    return text_


# ------------------------------------------------------------------ handler
class ChatAsk:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self._hour: dict[int, deque[float]] = defaultdict(deque)
        self._tasks: set[asyncio.Task[Any]] = set()

    async def stop(self) -> None:
        for tk in list(self._tasks):
            tk.cancel()
        for tk in list(self._tasks):
            try:
                await tk
            except BaseException:  # noqa: BLE001,S110
                pass

    async def idle(self) -> None:
        while pend := [tk for tk in self._tasks if not tk.done()]:
            await asyncio.gather(*pend, return_exceptions=True)

    def _limited(self, user_id: int, per_hour: int) -> bool:
        q = self._hour[user_id]
        now = time.monotonic()
        while q and q[0] < now - HOUR_S:
            q.popleft()
        if len(q) >= per_hour:
            return True
        q.append(now)
        return False

    async def _ask_enabled_for(self, bot: Any) -> bool:
        ids = list(getattr(bot, "channel_ids", []) or [])
        if not ids:
            return False
        async with self.rt.sessionmaker() as session:
            for r in await session.execute(
                text("SELECT config FROM notification_channels WHERE id = ANY(:i) AND enabled"),
                {"i": ids},
            ):
                if (common.jsonb(r.config) or {}).get("ai_ask"):
                    return True
        return False

    async def handle(self, poller: Any, bot: Any, msg: dict[str, Any]) -> bool:
        """Returns True when the message was an `/ask` for this bot (answered or refused)."""
        question = parse_ask(str(msg.get("text") or ""), getattr(bot, "username", None))
        if question is None:
            return False
        if not await self._ask_enabled_for(bot):
            return False
        sender = msg.get("from") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if not sender.get("id") or chat_id is None:
            return True
        ext = str(sender["id"])
        async with self.rt.sessionmaker() as session:
            lang = await common.language_default(session)
            user = (
                await session.execute(
                    text(
                        "SELECT u.id, u.username, u.role, u.is_active, u.language FROM "
                        "chat_identities c "
                        "JOIN users u ON u.id = c.user_id WHERE c.provider = 'telegram' AND "
                        "c.external_id = :x"
                    ),
                    {"x": ext},
                )
            ).first()
            per_hour = await common.int_setting(session, "chat_ask_per_user_hour")
            a = await common.assistant_settings(session)
            base = await common.public_base(session)
        if user is not None and user.language in TEXTS:
            lang = user.language

        async def reply(txt: str) -> None:
            await poller._send(bot, "sendMessage", {"chat_id": chat_id, "text": txt})  # noqa: SLF001

        if not await common.active(self.rt, "ai_chat_ask"):
            await reply(t(lang, "off"))
            return True
        if chat.get("type") != "private":
            await reply(t(lang, "private_only"))
            return True
        if user is None or not user.is_active:
            await reply(t(lang, "not_linked"))
            return True
        if user.role != "admin":
            await reply(t(lang, "not_admin"))
            return True
        if not question:
            await reply(t(lang, "empty"))
            return True
        if len(question) > QUESTION_MAX:
            await reply(t(lang, "too_long"))
            return True
        from netmon.assistant import agent

        runs = agent.runs_of(self.rt)
        if runs.running_for(user.id) is not None:
            await reply(t(lang, "busy"))
            return True
        if self._limited(user.id, max(1, per_hour)):
            await reply(t(lang, "cap", reset=""))
            return True
        try:
            working = await poller.call(
                bot.token, "sendMessage", {"chat_id": chat_id, "text": t(lang, "working")}
            )
            work_id = (working or {}).get("message_id")
        except Exception as exc:  # noqa: BLE001
            log.info("Telegram /ask reply failed: %s", type(exc).__name__)
            return True
        redact_ips = bool(a.get("chat_redact_ips", True))
        from netmon.services import audit

        ctx = audit.AuditCtx(
            actor_type="user",
            actor_user_id=user.id,
            actor_name=user.username,
            enabled=audit.audit_enabled(self.rt),
        )
        opts = agent.AgentOptions(
            purpose="chat_ask",
            channel="telegram",
            # owner decision: internal-IP redaction ON for Telegram unless `chat_redact_ips` is off
            redact_ips=True if redact_ips else None,
            audit_ctx=ctx,
        )
        auser = agent.AgentUser(id=user.id, username=user.username, role=user.role, language=lang)
        try:
            run = await agent.start(self.rt, auser, question, options=opts)
        except Exception as exc:  # noqa: BLE001 - caps, busy, unconfigured
            code = getattr(exc, "code", "")
            if code == "assistant_busy":
                txt = t(lang, "busy")
            elif code == "assistant_cap_reached":
                det = (getattr(exc, "details", None) or [{}])[0] or {}
                reset = f" ({det.get('resets_at')})" if det.get("resets_at") else ""
                txt = t(lang, "cap", reset=reset)
            else:
                txt = t(lang, "error", msg=getattr(exc, "message", None) or str(exc))
            await self._edit(poller, bot, chat_id, work_id, html.escape(txt, quote=False))
            return True
        task = asyncio.create_task(
            self._finish(poller, bot, chat_id, work_id, run, lang, base, redact_ips)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _finish(
        self,
        poller: Any,
        bot: Any,
        chat_id: Any,
        work_id: Any,
        run: Any,
        lang: str,
        base: str | None,
        redact_ips: bool,
    ) -> None:
        await run.done.wait()
        res = run.result
        if res is None or res.status == "error":
            msg = (res.error or {}).get("message") if res is not None and res.error else "error"
            await self._edit(
                poller, bot, chat_id, work_id, html.escape(t(lang, "error", msg=msg), quote=False)
            )
            return
        answer = res.text_redacted if redact_ips else res.text
        kb = {
            s["ref"]: s.get("url")
            for s in res.sources or []
            if str(s.get("ref", "")).startswith("kb/") and s.get("url")
        }
        body = to_telegram_html(answer, base, kb)
        tail = f"\n\n<i>{html.escape(t(lang, 'ai'), quote=False)}</i>"
        if base:
            url = f"{base.rstrip('/')}/assistant/c/{res.conversation_id}"
            tail += (
                f'\n<a href="{html.escape(url)}">{html.escape(t(lang, "full"), quote=False)}</a>'
            )
        await self._edit(poller, bot, chat_id, work_id, (body or "…") + tail)

    async def _edit(
        self, poller: Any, bot: Any, chat_id: Any, message_id: Any, html_text: str
    ) -> None:
        if message_id is None:
            await poller._send(
                bot, "sendMessage", {"chat_id": chat_id, "text": html_text, "parse_mode": "HTML"}
            )  # noqa: SLF001
            return
        body = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": html_text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            await poller.call(bot.token, "editMessageText", body)
        except Exception:  # noqa: BLE001 - e.g. invalid HTML → plain text
            plain = re.sub(r"<[^>]+>", "", html_text)
            await poller._send(  # noqa: SLF001
                bot,
                "editMessageText",
                {"chat_id": chat_id, "message_id": message_id, "text": html.unescape(plain)[:4096]},
            )
