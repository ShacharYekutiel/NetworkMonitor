#!/usr/bin/env python3
"""Microsoft Teams Workflows webhook mock (docs/architecture.md §12.10, §12.14), stdlib only.

    python tests/sim/teams_mock.py --port 18443 --certs DIR --control-port 18444 --out teams.jsonl

HTTPS on 127.0.0.1:--port with the make_certs.py server certificate (SAN localhost, 127.0.0.1; the backend
trusts DIR/ca.pem through NETMON_EXTRA_CA_FILE). Any POST is recorded ({"time", "path", "query", "headers",
"body"}) and answered with the configured status (default 202 Accepted, empty body, like Workflows).
Example channel URL: https://localhost:18443/workflows/qa/triggers/manual/paths/invoke?api-version=2016-06-01&sig=SECRET

Control (plain HTTP on 127.0.0.1:--control-port):
  GET /health · GET /records · DELETE /records · POST /status?code=400&body=Bad+Request (later POSTs get it;
  code=202 restores) · GET /state
Prints "READY https=<port> control=<port>".
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "ldap"))
from make_certs import make_certs  # noqa: E402

LOCK = threading.Lock()
STATE = {"code": 202, "body": ""}
RECORDS: list[dict] = []
OUT: Path | None = None


def _send(h: BaseHTTPRequestHandler, code: int, body: bytes, ctype: str = "application/json") -> None:
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


class Hook(BaseHTTPRequestHandler):
    server_version = "netmon-sim-teams/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            body: object = json.loads(raw)
        except ValueError:
            body = raw
        u = urlsplit(self.path)
        rec = {"time": time.time(), "path": u.path, "query": parse_qs(u.query),
               "headers": dict(self.headers.items()), "body": body}
        with LOCK:
            RECORDS.append(rec)
            if OUT:
                with OUT.open("a") as f:
                    f.write(json.dumps(rec) + "\n")
            code, text = STATE["code"], STATE["body"]
        _send(self, code, text.encode(), "text/plain")

    def do_GET(self) -> None:  # noqa: N802
        _send(self, 405, b'{"error":"POST only"}')


class Control(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def _json(self, code: int, obj: object) -> None:
        _send(self, code, json.dumps(obj).encode())

    def do_GET(self) -> None:  # noqa: N802
        p = urlsplit(self.path).path
        if p == "/health":
            self._json(200, {"ok": True})
        elif p == "/records":
            with LOCK:
                self._json(200, RECORDS)
        elif p == "/state":
            self._json(200, {**STATE, "records": len(RECORDS)})
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        with LOCK:
            RECORDS.clear()
            if OUT:
                OUT.write_text("")
        self._json(200, {"ok": True})

    def do_POST(self) -> None:  # noqa: N802
        u = urlsplit(self.path)
        if u.path == "/status":
            q = parse_qs(u.query)
            with LOCK:
                STATE["code"] = int(q.get("code", ["202"])[0])
                STATE["body"] = q.get("body", [""])[0]
            self._json(200, STATE)
        else:
            self._json(404, {"error": "not found"})


def main() -> None:
    global OUT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18443)
    ap.add_argument("--control-port", type=int, default=18444)
    ap.add_argument("--certs", required=True)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if a.out:
        OUT = Path(a.out)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text("")
    c = make_certs(a.certs)
    hook = ThreadingHTTPServer((a.host, a.port), Hook)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(c.server, c.server_key)
    hook.socket = ctx.wrap_socket(hook.socket, server_side=True)
    ctl = ThreadingHTTPServer(("127.0.0.1", a.control_port), Control)
    threading.Thread(target=ctl.serve_forever, daemon=True).start()
    print(f"READY https={hook.server_address[1]} control={ctl.server_address[1]}", flush=True)
    hook.serve_forever()


if __name__ == "__main__":
    main()
