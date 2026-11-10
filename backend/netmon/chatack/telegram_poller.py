"""[3] Telegram update poller (feature component `chat_ack`, architecture § 13.8.2).

One long-poll task per distinct bot token among enabled telegram channels with
`ack_buttons`: `getUpdates(offset, timeout, allowed_updates=["callback_query","message"])`
through the proxy (httpx `trust_env=True`, read timeout timeout + 15 s), errors back off
5 → 60 s. The offset is persisted per bot (`telegram_poll_state`, key SHA-256 of the token),
so a press is processed once across restarts. `409 Conflict` (webhook set / another consumer)
→ state `error`; the app never deletes a webhook on its own (`take_over` is the explicit admin
action). The bot token never appears in logs, errors or the database."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections import defaultdict, deque
from typing import Any

import httpx
from sqlalchemy import text

from netmon import __version__
from netmon.alerts import telegram as tg
from netmon.chatack import linking, service, tokens

log = logging.getLogger("netmon.chatack.telegram")

BACKOFF_MIN_S = 5.0
BACKOFF_MAX_S = 60.0
RESYNC_S = 30.0
PRESS_LIMIT = 10
PRESS_WINDOW_S = 60.0
CONFLICT_TEXT = (
    "This bot has a webhook or another consumer — use a dedicated bot for NetworkMonitor"
)


def bot_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TelegramApiError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class _Bot:
    def __init__(self, token: str, channel_ids: list[int]) -> None:
        self.token = token
        self.key = bot_key(token)
        self.channel_ids = channel_ids
        self.username: str | None = None
        self.task: asyncio.Task[None] | None = None


class TelegramPollers:
    features = ("chat_ack",)

    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.bots: dict[str, _Bot] = {}
        self.transport: httpx.AsyncBaseTransport | None = None  # tests
        self._sync_task: asyncio.Task[None] | None = None
        self._presses: dict[str, deque[float]] = defaultdict(deque)
        self._wake = asyncio.Event()
        self.on = False

    # ------------------------------------------------------------ component API
    async def start(self, snapshot: dict[str, bool]) -> None:
        if snapshot.get("chat_ack"):
            await self._turn_on()

    async def feature_changed(self, name: str, on: bool) -> None:
        if on:
            await self._turn_on()
        else:
            await self._turn_off()

    async def stop(self) -> None:
        await self._turn_off()

    async def _turn_on(self) -> None:
        self.on = True
        await self.sync()
        if self._sync_task is None:
            self._sync_task = asyncio.create_task(self._sync_loop(), name="netmon-tg-sync")

    async def _turn_off(self) -> None:
        self.on = False
        if self._sync_task is not None:
            self._sync_task.cancel()
            with contextlib.suppress(BaseException):
                await self._sync_task
            self._sync_task = None
        for key in list(self.bots):
            await self._stop_bot(key)

    def channels_changed(self) -> None:
        self._wake.set()

    async def _sync_loop(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), RESYNC_S)
            self._wake.clear()
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Telegram poller sync failed")

    async def sync(self) -> None:
        """One task per distinct bot token of enabled `ack_buttons` telegram channels."""
        if not self.on:
            return
        wanted: dict[str, tuple[str, list[int]]] = {}
        async with self.rt.sessionmaker() as session:
            for ch in await session.execute(
                text(
                    "SELECT id, config FROM notification_channels "
                    "WHERE type = 'telegram' AND enabled ORDER BY id"
                )
            ):
                cfg = ch.config or {}
                token = str(cfg.get("bot_token") or "")
                if not token or not cfg.get("ack_buttons"):
                    continue
                k = bot_key(token)
                wanted.setdefault(k, (token, []))[1].append(ch.id)
        for k in list(self.bots):
            if k not in wanted:
                await self._stop_bot(k)
        for k, (token, ids) in wanted.items():
            bot = self.bots.get(k)
            if bot is None:
                bot = self.bots[k] = _Bot(token, ids)
                bot.task = asyncio.create_task(self._poll(bot), name="netmon-tg-poll")
            else:
                bot.channel_ids = ids

    async def _stop_bot(self, key: str) -> None:
        bot = self.bots.pop(key, None)
        if bot is None or bot.task is None:
            return
        bot.task.cancel()
        with contextlib.suppress(BaseException):
            await bot.task
        await self._set_state(bot, "idle", None)

    # ------------------------------------------------------------ HTTP
    def _client(self, read_timeout: float) -> httpx.AsyncClient:
        from netmon.tlsutil import client_ssl_context

        kw: dict[str, Any] = {
            "timeout": httpx.Timeout(10.0, read=read_timeout),
            "headers": {"User-Agent": f"NetworkMonitor/{__version__}"},
        }
        if self.transport is not None:
            kw["transport"] = self.transport
        else:
            kw["verify"] = client_ssl_context()
            kw["trust_env"] = True
        return httpx.AsyncClient(**kw)

    async def call(
        self, token: str, method: str, body: dict[str, Any], read_timeout: float = 15.0
    ) -> Any:
        url = f"{tg.api_base()}/bot{token}/{method}"
        try:
            async with self._client(read_timeout) as c:
                resp = await c.post(url, json=body)
        except httpx.TimeoutException:
            raise TelegramApiError("Telegram request timed out") from None
        except httpx.HTTPError as exc:
            raise TelegramApiError(tg.scrub(f"{type(exc).__name__}: {exc}", token)) from None
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("ok") is True and resp.status_code == 200:
            return data.get("result")
        desc = (
            str(data.get("description"))
            if isinstance(data, dict) and data.get("description")
            else f"Telegram returned HTTP {resp.status_code}"
        )
        raise TelegramApiError(tg.scrub(desc, token)[:500], resp.status_code)

    # ------------------------------------------------------------ state
    async def _set_state(self, bot: _Bot, state: str, error: str | None) -> None:
        try:
            async with self.rt.sessionmaker() as session:
                await session.execute(
                    text(
                        "INSERT INTO telegram_poll_state (bot_key, bot_username, state, "
                        "last_error, updated_at) VALUES (:k, :u, :s, :e, now()) "
                        "ON CONFLICT (bot_key) DO UPDATE SET bot_username = "
                        "coalesce(EXCLUDED.bot_username, telegram_poll_state.bot_username), "
                        "state = EXCLUDED.state, last_error = EXCLUDED.last_error, "
                        "updated_at = now()"
                    ),
                    {"k": bot.key, "u": bot.username, "s": state, "e": error and error[:500]},
                )
                await session.commit()
        except Exception:  # noqa: BLE001
            log.debug("Cannot store the Telegram poller state", exc_info=True)
        self.rt.hub.broadcast(
            "chat_ack.poller",
            {"bot_username": bot.username, "state": state, "last_error": error},
            admin_only=True,
        )

    async def _offset(self, bot: _Bot) -> int:
        async with self.rt.sessionmaker() as session:
            v = (
                await session.execute(
                    text("SELECT next_offset FROM telegram_poll_state WHERE bot_key = :k"),
                    {"k": bot.key},
                )
            ).scalar()
        return int(v or 0)

    async def _save_offset(self, bot: _Bot, offset: int) -> None:
        async with self.rt.sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO telegram_poll_state (bot_key, bot_username, next_offset, state, "
                    "updated_at) VALUES (:k, :u, :o, 'polling', now()) ON CONFLICT (bot_key) "
                    "DO UPDATE SET next_offset = GREATEST(telegram_poll_state.next_offset, "
                    "EXCLUDED.next_offset), updated_at = now()"
                ),
                {"k": bot.key, "u": bot.username, "o": offset},
            )
            await session.commit()

    # ------------------------------------------------------------ poll loop
    async def _poll_timeout(self) -> int:
        from netmon.services import settings_p3c

        async with self.rt.sessionmaker() as session:
            v = await settings_p3c.get(session, "telegram_poll_timeout_s")
        try:
            return max(1, min(50, int(v)))
        except (TypeError, ValueError):
            return 50

    async def _poll(self, bot: _Bot) -> None:
        backoff = BACKOFF_MIN_S
        state = ""
        while True:
            try:
                if bot.username is None:
                    me = await self.call(bot.token, "getMe", {})
                    bot.username = str((me or {}).get("username") or "") or None
                timeout = await self._poll_timeout()
                offset = await self._offset(bot)
                if state != "polling":
                    state = "polling"
                    await self._set_state(bot, "polling", None)
                updates = await self.call(
                    bot.token,
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": timeout,
                        "allowed_updates": ["callback_query", "message"],
                    },
                    read_timeout=timeout + 15.0,
                )
                backoff = BACKOFF_MIN_S
                for upd in updates or []:
                    uid = int(upd.get("update_id", 0))
                    try:
                        await self.handle_update(bot, upd)
                    except Exception:  # noqa: BLE001 - one bad update never blocks the bot
                        log.exception("Handling a Telegram update failed")
                    await self._save_offset(bot, uid + 1)
            except asyncio.CancelledError:
                raise
            except TelegramApiError as exc:
                msg = CONFLICT_TEXT if exc.status == 409 else str(exc)
                if state != "error" or exc.status == 409:
                    state = "error"
                    await self._set_state(bot, "error", msg)
                wait = BACKOFF_MAX_S if exc.status in (401, 404, 409) else backoff
                backoff = min(BACKOFF_MAX_S, backoff * 2)
                await asyncio.sleep(wait)
            except Exception as exc:  # noqa: BLE001
                log.warning("Telegram poller error: %s", tg.scrub(str(exc), bot.token))
                state = "error"
                await self._set_state(bot, "error", tg.scrub(str(exc), bot.token)[:300])
                await asyncio.sleep(backoff)
                backoff = min(BACKOFF_MAX_S, backoff * 2)

    # ------------------------------------------------------------ updates
    async def handle_update(self, bot: _Bot, upd: dict[str, Any]) -> None:
        if isinstance(upd.get("callback_query"), dict):
            await self.handle_press(bot, upd["callback_query"])
        elif isinstance(upd.get("message"), dict):
            await self.handle_message(bot, upd["message"])

    async def _send(self, bot: _Bot, method: str, body: dict[str, Any]) -> None:
        try:
            await self.call(bot.token, method, body)
        except TelegramApiError as exc:
            log.info("Telegram %s failed: %s", method, exc)

    async def handle_message(self, bot: _Bot, msg: dict[str, Any]) -> None:
        cmd = linking.parse_command(str(msg.get("text") or ""), bot.username)
        sender = msg.get("from") or {}
        chat = msg.get("chat") or {}
        if cmd is None or not sender.get("id") or chat.get("id") is None:
            return  # other messages are ignored (no reply)
        ext = str(sender["id"])
        now = self.rt.clock.now()
        from netmon.services import audit
        from netmon.services import settings as settings_svc

        async with self.rt.sessionmaker() as session:
            lang = str(await settings_svc.get(session, "default_language") or "en")
            if cmd[0] == "link":
                user = await linking.consume(
                    session, cmd[1] or "", ext, linking.display_of(sender), now
                )
                if user is None:
                    await session.rollback()
                    reply = service.text_for(lang, "link_failed")
                else:
                    ctx = audit.AuditCtx(
                        actor_type="user",
                        actor_user_id=user.id,
                        actor_name=user.username,
                        enabled=audit.audit_enabled(self.rt),
                    )
                    await audit.record(
                        session,
                        ctx,
                        "chatack.link",
                        audit.entity("user", user.id, user.username),
                        {"telegram": [None, linking.display_of(sender)]},
                    )
                    await session.commit()
                    reply = service.text_for(user.language, "linked", user=user.username)
            else:
                row = await linking.unlink_external(session, ext)
                if row is not None:
                    ctx = audit.AuditCtx(
                        actor_type="user",
                        actor_user_id=row.user_id,
                        actor_name=linking.display_of(sender),
                        enabled=audit.audit_enabled(self.rt),
                    )
                    await audit.record(
                        session,
                        ctx,
                        "chatack.unlink",
                        audit.entity("chat_identity", row.id, linking.display_of(sender)),
                    )
                await session.commit()
                reply = service.text_for(lang, "unlinked" if row else "not_linked_unlink")
        await self._send(bot, "sendMessage", {"chat_id": chat["id"], "text": reply})

    def _rate_limited(self, ext: str) -> bool:
        q = self._presses[ext]
        now = time.monotonic()
        while q and q[0] < now - PRESS_WINDOW_S:
            q.popleft()
        if len(q) >= PRESS_LIMIT:
            return True
        q.append(now)
        return False

    async def handle_press(self, bot: _Bot, cq: dict[str, Any]) -> None:
        cq_id = cq.get("id")
        data = str(cq.get("data") or "")
        sender = cq.get("from") or {}
        ext = str(sender.get("id") or "")
        if data == "noop":
            await self._send(bot, "answerCallbackQuery", {"callback_query_id": cq_id})
            return
        reason, text_, user_name = await self._press(cq, data, ext, sender)
        refusal = reason != "ok"
        await self._send(
            bot,
            "answerCallbackQuery",
            {"callback_query_id": cq_id, "text": text_, "show_alert": refusal},
        )
        msg = cq.get("message") or {}
        if reason in ("ok", "already") and msg.get("message_id") and user_name:
            when = await self._local_hhmm()
            await self._send(
                bot,
                "editMessageReplyMarkup",
                {
                    "chat_id": (msg.get("chat") or {}).get("id"),
                    "message_id": msg["message_id"],
                    "reply_markup": {
                        "inline_keyboard": [
                            [{"text": f"✓ {user_name} {when}", "callback_data": "noop"}]
                        ]
                    },
                },
            )

    async def _local_hhmm(self) -> str:
        from zoneinfo import ZoneInfo

        from netmon.services import settings_p3c

        async with self.rt.sessionmaker() as session:
            tz = await settings_p3c.timezone(session)
        try:
            return self.rt.clock.now().astimezone(ZoneInfo(tz)).strftime("%H:%M")
        except Exception:  # noqa: BLE001
            return self.rt.clock.now().strftime("%H:%M")

    async def _press(
        self, cq: dict[str, Any], data: str, ext: str, sender: dict[str, Any]
    ) -> tuple[str, str, str | None]:
        """(reason, answer text, username for the edited button)."""
        from netmon.services import audit
        from netmon.services import settings as settings_svc

        enabled = audit.audit_enabled(self.rt)
        display = linking.display_of(sender)
        async with self.rt.sessionmaker() as session:
            lang = str(await settings_svc.get(session, "default_language") or "en")
            ident = None
            if ext:
                ident = (
                    await session.execute(
                        text(
                            "SELECT ci.id, ci.user_id, u.username, u.role, u.is_active, "
                            "u.language FROM chat_identities ci JOIN users u ON u.id = ci.user_id "
                            "WHERE ci.provider = 'telegram' AND ci.external_id = :x"
                        ),
                        {"x": ext},
                    )
                ).first()
            if ident is not None:
                lang = ident.language or lang
                ctx = audit.AuditCtx(
                    actor_type="user",
                    actor_user_id=ident.user_id,
                    actor_name=ident.username,
                    enabled=enabled,
                )
            else:
                ctx = audit.AuditCtx(
                    actor_type="anonymous", actor_name=display[:256], enabled=enabled
                )

            async def done(reason: str, key: str, ent: Any = None, **kw: Any) -> tuple:
                await audit.record_now(
                    self.rt,
                    ctx,
                    "chatack.press",
                    ent,
                    {"telegram": [None, display]},
                    reason=reason,
                )
                return reason, service.text_for(lang, key, **kw), kw.get("_user")

            if not ext or self._rate_limited(ext):
                return await done("rate_limited", "rate_limited")
            try:
                sensor_id, event_id, _ = tokens.decode(
                    service.key_of(self.rt), data, service.now_min(self.rt)
                )
            except tokens.AckTokenError as exc:
                return await done(exc.reason, exc.reason)
            ent = audit.entity("sensor", sensor_id)
            if ident is None:
                return await done("not_linked", "not_linked", ent)
            if not ident.is_active or ident.role != "admin":
                return await done("not_admin", "not_admin", ent)
            problem = await service.problem_of(session, sensor_id, event_id, lock=True)
            if problem is None or not problem.active:
                await session.rollback()
                return await done("stale", "stale", ent)
            ent = audit.entity(
                "sensor", sensor_id, problem.sensor.name, device_id=problem.device.id
            )
            if problem.sensor.ack_at is not None:
                by = problem.sensor.ack_by or "?"
                await session.rollback()
                return await done("already", "already", ent, by=by, _user=by)
            await service.acknowledge(
                self.rt,
                session,
                problem,
                ident.user_id,
                ident.username,
                "Acknowledged from Telegram",
                ctx,
                "telegram",
            )
            await session.execute(
                text("UPDATE chat_identities SET last_used_at = :t WHERE id = :i"),
                {"t": self.rt.clock.now(), "i": ident.id},
            )
            await session.commit()
            return await done("ok", "acknowledged", ent, _user=ident.username)

    # ------------------------------------------------------------ admin actions
    async def take_over(self, token: str) -> None:
        """Explicit admin action: delete the bot's webhook so long polling works."""
        await self.call(token, "deleteWebhook", {"drop_pending_updates": False})
        bot = self.bots.get(bot_key(token))
        if bot is not None and bot.task is not None:
            bot.task.cancel()
            with contextlib.suppress(BaseException):
                await bot.task
            bot.task = asyncio.create_task(self._poll(bot), name="netmon-tg-poll")

    async def status(self) -> list[dict[str, Any]]:
        async with self.rt.sessionmaker() as session:
            rows = {
                r.bot_key: r
                for r in await session.execute(
                    text(
                        "SELECT bot_key, bot_username, state, last_error, updated_at "
                        "FROM telegram_poll_state"
                    )
                )
            }
        from netmon.services.serialize import iso

        out = []
        for k, bot in self.bots.items():
            r = rows.get(k)
            out.append(
                {
                    "bot_username": bot.username or (r.bot_username if r else None),
                    "channel_ids": list(bot.channel_ids),
                    "state": r.state if r else "idle",
                    "last_error": r.last_error if r else None,
                    "updated_at": iso(r.updated_at) if r else None,
                }
            )
        return out
