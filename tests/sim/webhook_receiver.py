#!/usr/bin/env python3
"""Webhook receiver for E2E tests (stdlib only). Records every POST as one JSON line.

    python tests/sim/webhook_receiver.py --port 18082 --out /tmp/webhooks.jsonl

Any POST path is recorded as {"time", "path", "headers", "body"} (body parsed as JSON when possible)
and answered with 200 {"ok": true}.
    GET    /records   -> JSON list of all recorded requests
    DELETE /records   -> clear the records (file truncated)
    GET    /health    -> 200
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOCK = threading.Lock()
OUT: Path


def _read_all() -> list[object]:
    if not OUT.exists():
        return []
    return [json.loads(line) for line in OUT.read_text().splitlines() if line.strip()]


class Handler(BaseHTTPRequestHandler):
    server_version = "netmon-sim-webhook/1.0"

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
        elif self.path.startswith("/records"):
            with LOCK:
                self._json(200, _read_all())
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        if self.path.startswith("/records"):
            with LOCK:
                OUT.write_text("")
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            body: object = json.loads(raw)
        except ValueError:
            body = raw
        rec = {
            "time": time.time(),
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "body": body,
        }
        with LOCK, OUT.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        self._json(200, {"ok": True})


def main() -> None:
    global OUT
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18082)
    p.add_argument("--out", default="webhooks.jsonl")
    a = p.parse_args()
    OUT = Path(a.out)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"webhook_receiver listening on http://{a.host}:{a.port} -> {OUT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
