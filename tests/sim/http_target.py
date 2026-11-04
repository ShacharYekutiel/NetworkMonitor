#!/usr/bin/env python3
"""Toggleable HTTP target for E2E tests (stdlib only).

    python tests/sim/http_target.py --port 18081

Endpoints:
    GET  /          200 "NetworkMonitor sim OK" while healthy, 503 "sim failing" while failing
    GET  /health    always 200 (readiness probe for the test harness)
    GET  /state     {"healthy": bool, "hits": int}
    POST|GET /toggle   flip the state, returns the new state
    POST|GET /fail     force failing
    POST|GET /ok       force healthy
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"healthy": True, "hits": 0}
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "netmon-sim-http/1.0"

    def log_message(self, fmt: str, *args: object) -> None:  # quiet
        pass

    def _send(self, code: int, body: str, ctype: str = "text/plain; charset=utf-8") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, obj: object) -> None:
        self._send(200, json.dumps(obj), "application/json")

    def _control(self) -> bool:
        path = self.path.split("?", 1)[0]
        with LOCK:
            if path == "/toggle":
                STATE["healthy"] = not STATE["healthy"]
            elif path == "/fail":
                STATE["healthy"] = False
            elif path == "/ok":
                STATE["healthy"] = True
            elif path == "/state":
                pass
            else:
                return False
            snapshot = dict(STATE)
        self._json(snapshot)
        return True

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, "ok")
            return
        if self._control():
            return
        with LOCK:
            STATE["hits"] += 1
            healthy = STATE["healthy"]
        if healthy:
            self._send(200, "NetworkMonitor sim OK")
        else:
            self._send(503, "sim failing")

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        if not self._control():
            self._send(404, "not found")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18081)
    a = p.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"http_target listening on http://{a.host}:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
