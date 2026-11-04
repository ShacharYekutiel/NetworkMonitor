#!/usr/bin/env python3
"""Telegram Bot API mock for E2E tests (stdlib only, architecture.md §9.6).

    python3 tests/sim/telegram_mock.py --port 18081
    # backend: NETMON_TELEGRAM_API_BASE=http://127.0.0.1:18081

POST /bot<token>/sendMessage  (JSON body; form-encoded is accepted too)
    token  123456:TEST_TOKEN_valid_0123456789abcdef  is valid; any other -> 401 Unauthorized
    chat_id "-1001" | "@netmon_test"  -> 200 {"ok":true,"result":{"message_id":N,...}}
            "404"                     -> 400 Bad Request: chat not found   (also any other chat id)
            "429"                     -> 429 Too Many Requests: retry after 5
            "slow"                    -> sleeps 15 s, then 200 (client timeout path)
    Like the real API it also rejects: an empty text (400), a text > 4096 characters (400), and with
    parse_mode "HTML" a text Telegram could not parse (unsupported tag, unbalanced tags, a bare "<" or
    "&") -> 400 "Bad Request: can't parse entities: ...". This catches missing HTML escaping.
GET|POST /bot<token>/getMe     200 for the valid token, else 401
GET    /_messages              every request received so far: [{"time","method","path","token","method_name",
                               "body","status","response"}, ...]
DELETE /_messages              clears them
GET    /health                 200 "ok"
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
SLOW_S = 15.0
MESSAGES: list[dict] = []
LOCK = threading.Lock()
COUNTER = {"message_id": 0}

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
    outside_tags = re.sub(r"<[^>]*>", lambda m: " " * len(m.group(0)), text)
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
        elif path.startswith("/bot"):
            self._bot({})
        else:
            self._send(404, {"ok": False, "error_code": 404, "description": "Not Found"})

    def do_DELETE(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] == "/_messages":
            with LOCK:
                MESSAGES.clear()
            self._send(200, {"ok": True})
        else:
            self._send(404, {"ok": False, "error_code": 404, "description": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith("/bot"):
            self._bot(self._body())
        else:
            self._send(404, {"ok": False, "error_code": 404, "description": "Not Found"})

    def _bot(self, body: dict) -> None:
        path = self.path.split("?", 1)[0]
        m = re.match(r"^/bot([^/]*)/([A-Za-z]+)$", path)
        token, method = (m.group(1), m.group(2)) if m else ("", "")
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
        if method != "sendMessage":
            return tg_error(404, "Not Found: method not found")
        chat = body.get("chat_id")
        chat = "" if chat is None else str(chat)
        text = body.get("text")
        if chat == "":
            return tg_error(400, "Bad Request: chat_id is empty")
        if chat == "429":
            return tg_error(429, "Too Many Requests: retry after 5", parameters={"retry_after": 5})
        if chat == "slow":
            time.sleep(SLOW_S)
        elif chat not in OK_CHATS:
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
            return tg_error(400, f"Bad Request: unsupported parse_mode")
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
            },
        }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18081)
    a = p.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"telegram_mock listening on http://{a.host}:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
