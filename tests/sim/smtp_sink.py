#!/usr/bin/env python3
"""SMTP sink for the phase-3 report tests (architecture.md §13.6.3, §13.11): accepts every message (no TLS, no
auth; AUTH PLAIN/LOGIN accepted with any credentials when the client insists) and exposes them over HTTP.

    backend/.venv/bin/python tests/sim/smtp_sink.py --smtp-port 18025 --http-port 18026 [--out mails.jsonl]
    # backend settings: smtp = {"host": "127.0.0.1", "port": 18025, "security": "none", "from": "netmon@mnidom.test"}

HTTP (127.0.0.1:--http-port):
  GET    /health
  GET    /messages   [{"time", "mail_from", "rcpt_tos", "subject" (decoded), "from", "to", "headers",
                       "text" (text/plain part), "html" (text/html part), "size", "attachments": [{filename, type}],
                       "remote_resources": [...http(s) src/href found in the HTML that would load remotely]}]
  DELETE /messages
Prints "READY smtp=<port> http=<port>".
"""

from __future__ import annotations

import argparse
import email
import email.policy
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

LOCK = threading.Lock()
MESSAGES: list[dict] = []
OUT: Path | None = None
REMOTE = re.compile(r"""(?:src|background)\s*=\s*["']?(https?://[^"' >]+)|url\(\s*["']?(https?://[^)"']+)""", re.I)


def parse(envelope) -> dict:  # noqa: ANN001
    raw = envelope.original_content or envelope.content
    msg = email.message_from_bytes(raw if isinstance(raw, bytes) else raw.encode(), policy=email.policy.default)
    text = html = None
    atts = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        fn = part.get_filename()
        if fn:
            atts.append({"filename": fn, "type": ctype})
            continue
        if ctype == "text/plain" and text is None:
            text = part.get_content()
        elif ctype == "text/html" and html is None:
            html = part.get_content()
    remote = [a or b for a, b in REMOTE.findall(html or "")]
    return {"time": time.time(), "mail_from": envelope.mail_from, "rcpt_tos": list(envelope.rcpt_tos),
            "subject": str(msg.get("Subject", "")), "from": str(msg.get("From", "")), "to": str(msg.get("To", "")),
            "headers": {k: str(v) for k, v in msg.items()}, "text": text, "html": html, "size": len(raw),
            "attachments": atts, "remote_resources": remote}


class Handler:
    async def handle_DATA(self, server, session, envelope) -> str:  # noqa: ANN001, N802
        rec = parse(envelope)
        with LOCK:
            MESSAGES.append(rec)
            if OUT:
                with OUT.open("a") as f:
                    f.write(json.dumps(rec) + "\n")
        return "250 Message accepted for delivery"


def any_auth(server, session, envelope, mechanism, auth_data) -> AuthResult:  # noqa: ANN001
    return AuthResult(success=True)


class Http(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def _json(self, code: int, obj: object) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/health"):
            self._json(200, {"ok": True})
        elif self.path.startswith("/messages"):
            with LOCK:
                self._json(200, list(MESSAGES))
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        with LOCK:
            MESSAGES.clear()
        self._json(200, {"ok": True})


def main() -> None:
    global OUT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--smtp-port", type=int, default=18025)
    ap.add_argument("--http-port", type=int, default=18026)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if a.out:
        OUT = Path(a.out)
        OUT.write_text("")
    ctl = Controller(Handler(), hostname=a.host, port=a.smtp_port, authenticator=any_auth,
                     auth_require_tls=False, data_size_limit=10 * 1024 * 1024)
    ctl.start()
    http = ThreadingHTTPServer((a.host, a.http_port), Http)
    print(f"READY smtp={a.smtp_port} http={http.server_address[1]}", flush=True)
    try:
        http.serve_forever()
    finally:
        ctl.stop()


if __name__ == "__main__":
    main()
