#!/usr/bin/env python3
"""Telegram Bot API mock for E2E tests (stdlib only, architecture.md §9.6).

    python3 tests/sim/telegram_mock.py --port 18081
    # backend: NETMON_TELEGRAM_API_BASE=http://127.0.0.1:18081

POST /bot<token>/sendMessage  (JSON body; form-encoded is accepted too)
    token  123456:TEST_TOKEN_valid_0123456789abcdef  is valid; any other -> 401 Unauthorized
    chat_id "-1001" | "@netmon_test"  -> 200 {"ok":true,"result":{"message_id":N,...}}
            "404"                     -> 400 Bad Request: chat not found   (also any other chat id)
            "429"                     -> 429 Too Many Requests: retry after 5
            "slow" | "-1009"          -> sleeps 15 s, then 200 (client timeout path; "-1009" because the
                                         API's chat_id validation does not accept "slow")
    Like the real API it also rejects: an empty text (400), a text > 4096 characters (400), and with
    parse_mode "HTML" a text Telegram could not parse (unsupported tag, unbalanced tags, a bare "<" or
    "&") -> 400 "Bad Request: can't parse entities: ...". This catches missing HTML escaping.
GET|POST /bot<token>/getMe     200 for the valid token, else 401
GET    /_messages              every request received so far: [{"time","method","path","token","method_name",
                               "body","status","response"}, ...] (getUpdates polls are NOT recorded here)
DELETE /_messages              clears them
GET    /health                 200 "ok"

[3] Chat acknowledge (architecture.md §13.8.2, §13.11):
POST /bot<token>/sendMessage  also validates `reply_markup` like the API: inline_keyboard must be an array of
                              arrays of buttons with `text` and exactly one of `callback_data` (1-64 bytes) or
                              `url` (http/https) -> else 400 "Bad Request: ... reply markup ...".
GET|POST /bot<token>/getUpdates   long poll (offset, timeout <= 50, allowed_updates); updates fed by /_inject;
                              an offset confirms (drops) every update with a lower id, like the real API.
                              409 when a webhook is set (--conflict / POST /_control {"webhook": true}) or
                              {"poll_conflict": true} ("terminated by other getUpdates request").
POST /bot<token>/answerCallbackQuery, editMessageReplyMarkup, deleteWebhook (clears the webhook), getWebhookInfo
POST   /_inject               add an update. Raw: {"callback_query": {...}} / {"message": {...}}. Shortcuts:
                              {"type": "press", "data": "a1.…", "from_id": 777, "username": "jdoe_tg",
                               "message_id": <default: last sent message with a callback button>, "chat_id": -1001}
                              {"type": "link", "text": "/link NM-ABCD2345", "from_id": 777, "username": "jdoe_tg",
                               "chat_type": "private"}
                              -> {"update_id": N, ...}
DELETE /_updates, /_polls     clear them
GET    /_updates              every injected update and whether it was delivered (and the confirming offset)
GET    /_polls                getUpdates requests (time, token, params, status, returned update ids)
POST   /_control              {"webhook": bool, "poll_conflict": bool, "fail_methods": {"answerCallbackQuery": 400}}
GET    /_control              current control state
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl

VALID_TOKEN = "123456:TEST_TOKEN_valid_0123456789abcdef"
OK_CHATS = {"-1001", "@netmon_test"}
SLOW_CHATS = {"slow", "-1009"}
SLOW_S = 15.0
MESSAGES: list[dict] = []
LOCK = threading.Lock()
COUNTER = {"message_id": 0}
COND = threading.Condition(LOCK)
UPDATES: list[dict] = []          # injected updates {"update": {...}, "delivered": bool, "confirmed": bool}
POLLS: list[dict] = []
CONTROL: dict = {"webhook": False, "poll_conflict": False, "fail_methods": {}}
UPDATE_SEQ = {"id": 500000}
PRIVATE_CHATS: set[str] = set()   # [3] private chats of injected users (replies to /link are deliverable)
POLL_MAX_S = 50.0

# Telegram's supported HTML tags (https://core.telegram.org/bots/api#html-style).
ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "span",
    "tg-spoiler", "tg-emoji", "blockquote",
}
ENTITY_RE = re.compile(r"&(lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);")
BARE_LT_RE = re.compile(r"<(?!/?[A-Za-z])")


class _TagChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.error: str | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self.error:
            return
        if tag not in ALLOWED_TAGS:
            self.error = f'Unsupported start tag "{tag}"'
            return
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.error:
            return
        if not self.stack or self.stack[-1] != tag:
            self.error = f'Unexpected end tag "{tag}"'
            return
        self.stack.pop()


def html_error(text: str) -> str | None:
    m = BARE_LT_RE.search(text)
    if m:
        return f"Unsupported start tag at byte offset {m.start()}"
    outside_tags = re.sub(r"<[^<>]*>", lambda m: " " * len(m.group(0)), text)
    for m in re.finditer(r"&", outside_tags):
        if not ENTITY_RE.match(outside_tags, m.start()):
            return f'Character entity expected at byte offset {m.start()}'
    p = _TagChecker()
    p.feed(text)
    p.close()
    if p.error:
        return p.error
    if p.stack:
        return f'Can\'t find end tag corresponding to start tag "{p.stack[-1]}"'
    return None


def tg_error(code: int, description: str, **extra: object) -> tuple[int, dict]:
    body: dict = {"ok": False, "error_code": code, "description": description}
    body.update(extra)
    return code, body


class Handler(BaseHTTPRequestHandler):
    server_version = "netmon-sim-telegram/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # quiet
        pass

    def _send(self, code: int, obj: object) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        ctype = (self.headers.get("Content-Type") or "").lower()
        if not raw:
            return {}
        if "application/json" in ctype:
            try:
                v = json.loads(raw)
                return v if isinstance(v, dict) else {"_raw": v}
            except ValueError:
                return {"_invalid_json": raw.decode(errors="replace")}
        return dict(parse_qsl(raw.decode(errors="replace")))

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, "ok")
        elif path == "/_messages":
            with LOCK:
                self._send(200, list(MESSAGES))
        elif path == "/_updates":
            with LOCK:
                self._send(200, list(UPDATES))
        elif path == "/_polls":
            with LOCK:
                self._send(200, list(POLLS))
        elif path == "/_control":
            with LOCK:
                self._send(200, dict(CONTROL))
        elif path.startswith("/bot"):
            self._bot({})
        else:
            self._send(404, {"ok": False, "error_code": 404, "description": "Not Found"})

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/_messages", "/_updates", "/_polls"):
            with LOCK:
                {"/_messages": MESSAGES, "/_updates": UPDATES, "/_polls": POLLS}[path].clear()
            self._send(200, {"ok": True})
        else:
            self._send(404, {"ok": False, "error_code": 404, "description": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/_inject":
            self._send(200, inject(self._body()))
        elif path == "/_control":
            b = self._body()
            with LOCK:
                for k in ("webhook", "poll_conflict", "fail_methods"):
                    if k in b:
                        CONTROL[k] = b[k]
                self._send(200, dict(CONTROL))
        elif path.startswith("/bot"):
            self._bot(self._body())
        else:
            self._send(404, {"ok": False, "error_code": 404, "description": "Not Found"})

    def _bot(self, body: dict) -> None:
        path = self.path.split("?", 1)[0]
        m = re.match(r"^/bot([^/]*)/([A-Za-z]+)$", path)
        token, method = (m.group(1), m.group(2)) if m else ("", "")
        if method == "getUpdates":
            q = dict(parse_qsl(self.path.split("?", 1)[1])) if "?" in self.path else {}
            q.update(body)
            status, resp = get_updates(token, q)
            try:
                self._send(status, resp)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        status, resp = self._handle(token, method, body)
        with LOCK:
            MESSAGES.append(
                {
                    "time": time.time(),
                    "method": self.command,
                    "path": path,
                    "token": token,
                    "method_name": method,
                    "content_type": self.headers.get("Content-Type"),
                    "body": body,
                    "status": status,
                    "response": resp,
                }
            )
        try:
            self._send(status, resp)
        except (BrokenPipeError, ConnectionResetError):  # client timed out ("slow")
            pass

    def _handle(self, token: str, method: str, body: dict) -> tuple[int, dict]:
        if not method:
            return tg_error(404, "Not Found")
        if token != VALID_TOKEN:
            return tg_error(401, "Unauthorized")
        if method == "getMe":
            return 200, {"ok": True, "result": {"id": 123456, "is_bot": True, "first_name": "NetMon QA",
                                                "username": "netmon_qa_bot"}}
        fail = (CONTROL.get("fail_methods") or {}).get(method)
        if fail:
            return tg_error(int(fail), f"Bad Request: mock failure for {method}")
        if method == "answerCallbackQuery":
            if not body.get("callback_query_id"):
                return tg_error(400, "Bad Request: query is too old and response timeout expired or "
                                     "query ID is invalid")
            if len(str(body.get("text", ""))) > 200:
                return tg_error(400, "Bad Request: MESSAGE_TOO_LONG")
            return 200, {"ok": True, "result": True}
        if method == "editMessageReplyMarkup":
            if not body.get("chat_id") or not body.get("message_id"):
                return tg_error(400, "Bad Request: message identifier is not specified")
            err = markup_error(body.get("reply_markup"))
            if err:
                return tg_error(400, f"Bad Request: {err}")
            return 200, {"ok": True, "result": {"message_id": int(body["message_id"]), "chat": {"id": body["chat_id"]},
                                                "reply_markup": body.get("reply_markup")}}
        if method == "deleteWebhook":
            with LOCK:
                CONTROL["webhook"] = False
            return 200, {"ok": True, "result": True, "description": "Webhook was deleted"}
        if method == "getWebhookInfo":
            url = "https://example.invalid/hook" if CONTROL["webhook"] else ""
            return 200, {"ok": True, "result": {"url": url, "has_custom_certificate": False, "pending_update_count": 0}}
        if method != "sendMessage":
            return tg_error(404, "Not Found: method not found")
        chat = body.get("chat_id")
        chat = "" if chat is None else str(chat)
        text = body.get("text")
        if chat == "":
            return tg_error(400, "Bad Request: chat_id is empty")
        if chat == "429":
            return tg_error(429, "Too Many Requests: retry after 5", parameters={"retry_after": 5})
        if chat in SLOW_CHATS:
            time.sleep(SLOW_S)
        elif chat not in OK_CHATS and chat not in PRIVATE_CHATS:
            return tg_error(400, "Bad Request: chat not found")
        if not isinstance(text, str) or not text.strip():
            return tg_error(400, "Bad Request: message text is empty")
        if len(text) > 4096:
            return tg_error(400, "Bad Request: message is too long")
        if body.get("parse_mode") == "HTML":
            err = html_error(text)
            if err:
                return tg_error(400, f"Bad Request: can't parse entities: {err}")
        elif body.get("parse_mode") not in (None, "", "MarkdownV2", "Markdown"):
            return tg_error(400, "Bad Request: unsupported parse_mode")
        if body.get("reply_markup") is not None:
            err = markup_error(body.get("reply_markup"))
            if err:
                return tg_error(400, f"Bad Request: {err}")
        with LOCK:
            COUNTER["message_id"] += 1
            mid = COUNTER["message_id"]
        return 200, {
            "ok": True,
            "result": {
                "message_id": mid,
                "date": int(time.time()),
                "chat": {"id": int(chat) if chat.lstrip("-").isdigit() else -1009999, "type": "supergroup"},
                "text": text,
                **({"reply_markup": _markup(body.get("reply_markup"))} if body.get("reply_markup") else {}),
            },
        }


def _markup(m: object) -> object:
    if isinstance(m, str):
        try:
            return json.loads(m)
        except ValueError:
            return m
    return m


def markup_error(m: object) -> str | None:
    m = _markup(m)
    if not isinstance(m, dict):
        return "can't parse reply keyboard markup JSON object"
    kb = m.get("inline_keyboard")
    if not isinstance(kb, list) or not all(isinstance(r, list) for r in kb):
        return "field \"inline_keyboard\" of the InlineKeyboardMarkup must be an Array of Arrays"
    for row in kb:
        for b in row:
            if not isinstance(b, dict) or not isinstance(b.get("text"), str) or not b["text"]:
                return "inline keyboard button text must be non-empty"
            kinds = [k for k in ("callback_data", "url", "login_url", "switch_inline_query") if k in b]
            if len(kinds) != 1:
                return "can't parse inline keyboard button: exactly one of the optional fields must be used"
            if "callback_data" in b:
                n = len(str(b["callback_data"]).encode())
                if n < 1 or n > 64:
                    return "BUTTON_DATA_INVALID"
            if "url" in b and not re.match(r"^https?://[^\s/]+", str(b["url"])):
                return f"inline keyboard button URL '{b['url']}' is invalid: Wrong HTTP URL"
    return None


def _last_button_message() -> dict | None:
    for m in reversed(MESSAGES):
        if m["method_name"] == "sendMessage" and m["status"] == 200:
            mk = _markup(m["body"].get("reply_markup"))
            if isinstance(mk, dict) and any("callback_data" in b for r in mk.get("inline_keyboard", []) for b in r):
                return m
    return None


def inject(b: dict) -> dict:
    with COND:
        UPDATE_SEQ["id"] += 1
        uid = UPDATE_SEQ["id"]
        if "callback_query" in b or "message" in b:
            upd = {"update_id": uid, **{k: v for k, v in b.items() if k in ("callback_query", "message")}}
        elif b.get("type") == "press":
            src = _last_button_message()
            mid = b.get("message_id") or (src["response"]["result"]["message_id"] if src else 1)
            chat = b.get("chat_id") or (src["body"].get("chat_id") if src else "-1001")
            text = (src["body"].get("text") if src else "") or ""
            frm = {"id": int(b.get("from_id", 777)), "is_bot": False, "first_name": b.get("first_name", "QA"),
                   "username": b.get("username", "qa_tg"), "language_code": "en"}
            upd = {"update_id": uid, "callback_query": {
                "id": str(b.get("query_id") or f"cbq{uid}"), "from": frm, "chat_instance": "-42",
                "data": b.get("data") if "data" in b else _first_callback(src),
                "message": {"message_id": int(mid), "date": int(time.time()), "text": text,
                            "chat": {"id": int(chat) if str(chat).lstrip("-").isdigit() else chat,
                                     "type": "supergroup"}}}}
        elif b.get("type") == "link":
            frm = {"id": int(b.get("from_id", 777)), "is_bot": False, "first_name": b.get("first_name", "QA"),
                   "username": b.get("username", "qa_tg"), "language_code": "en"}
            ctype = b.get("chat_type", "private")
            chat = {"id": frm["id"], "type": "private", "first_name": frm["first_name"]} if ctype == "private" else \
                {"id": int(b.get("chat_id", -1001)), "type": ctype, "title": "NOC"}
            PRIVATE_CHATS.add(str(chat["id"]))
            upd = {"update_id": uid, "message": {"message_id": uid, "date": int(time.time()), "from": frm,
                                                 "chat": chat, "text": b.get("text", "/link"),
                                                 "entities": [{"type": "bot_command", "offset": 0,
                                                               "length": len(b.get("text", "/link").split()[0])}]}}
        else:
            return {"ok": False, "error": "unknown inject body"}
        UPDATES.append({"update": upd, "delivered": False, "confirmed": False, "time": time.time()})
        COND.notify_all()
        return upd


def _first_callback(src: dict | None) -> str | None:
    if not src:
        return None
    mk = _markup(src["body"].get("reply_markup"))
    for r in mk.get("inline_keyboard", []):
        for b in r:
            if "callback_data" in b:
                return b["callback_data"]
    return None


def get_updates(token: str, q: dict) -> tuple[int, dict]:
    rec = {"time": time.time(), "token": token, "params": q, "status": 200, "returned": []}
    if token != VALID_TOKEN:
        rec["status"] = 401
        with LOCK:
            POLLS.append(rec)
        return tg_error(401, "Unauthorized")
    with COND:
        if CONTROL["webhook"]:
            rec["status"] = 409
            POLLS.append(rec)
            return tg_error(409, "Conflict: can't use getUpdates method while webhook is active; use deleteWebhook "
                                 "to delete the webhook first")
        if CONTROL["poll_conflict"]:
            rec["status"] = 409
            POLLS.append(rec)
            return tg_error(409, "Conflict: terminated by other getUpdates request; make sure that only one bot "
                                 "instance is running")
        try:
            offset = int(q.get("offset") or 0)
            timeout = min(float(q.get("timeout") or 0), POLL_MAX_S)
        except ValueError:
            return tg_error(400, "Bad Request: wrong parameter")
        allowed = q.get("allowed_updates")
        if isinstance(allowed, str):
            try:
                allowed = json.loads(allowed)
            except ValueError:
                allowed = None
        if offset:
            for u in UPDATES:
                if u["update"]["update_id"] < offset:
                    u["confirmed"] = True
        end = time.time() + timeout

        def pending() -> list[dict]:
            out = []
            for u in UPDATES:
                if u["confirmed"]:
                    continue
                if allowed and not any(k in u["update"] for k in allowed):
                    continue
                out.append(u)
            return out[: int(q.get("limit") or 100)]

        items = pending()
        POLLS.append(rec)  # recorded when the poll starts (a long poll may take 50 s)
        rec["open"] = True
        while not items and time.time() < end:
            COND.wait(max(0.0, end - time.time()))
            if CONTROL["webhook"] or CONTROL["poll_conflict"]:
                break
            items = pending()
        for u in items:
            u["delivered"] = True
        rec["returned"] = [u["update"]["update_id"] for u in items]
        rec["end"] = time.time()
        rec["open"] = False
        return 200, {"ok": True, "result": [u["update"] for u in items]}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18081)
    p.add_argument("--conflict", action="store_true", help="a webhook is set: getUpdates answers 409")
    a = p.parse_args()
    CONTROL["webhook"] = a.conflict
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"telegram_mock listening on http://{a.host}:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
