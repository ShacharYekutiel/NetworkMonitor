#!/usr/bin/env python3
"""NOC kiosk client for the phase-3 tests (architecture.md §13.7, api.md [3] NOC kiosk). Simulates a wall screen
from a chosen loopback source address (the IP allow-list is checked against the peer address).

    backend/.venv/bin/python tests/sim/kiosk_client.py --base http://127.0.0.1:8080 --token nmk_... view
    ... --source 127.0.0.77 view                       # from another "screen" address (allow-list deny -> 404)
    ... ws --seconds 20                                # kiosk_auth, then print every server message (JSON lines)
    ... ws --no-auth                                   # expect close 4401 after 5 s
    ... ws --auth-delay 6                              # auth too late -> 4401
    ... probe --path /api/devices                      # the kiosk header on a normal route (must not authorise)
    ... hammer --count 31                              # 31 wrong-token requests -> the last ones 429

Output: one JSON object per line ({"kind": "http", "status": 200, "body": {...}} / {"kind": "ws", "event":
"message"|"close", "code": 4401, "data": {...}, "t": seconds since connect}). Exit 0 always (the caller
asserts); importable: view(), ws_session().
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import time

import httpx
import websockets


def out(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def client(source: str | None) -> httpx.Client:
    transport = httpx.HTTPTransport(local_address=source) if source else httpx.HTTPTransport()
    return httpx.Client(transport=transport, timeout=15, trust_env=False)


def view(base: str, token: str | None, source: str | None = None, path: str = "/api/kiosk/view",
         extra_headers: dict | None = None) -> dict:
    h = {"X-Netmon-Kiosk": token} if token else {}
    h.update(extra_headers or {})
    with client(source) as c:
        r = c.get(base + path, headers=h)
    try:
        body = r.json()
    except ValueError:
        body = r.text[:500]
    return {"kind": "http", "path": path, "status": r.status_code, "cache_control": r.headers.get("cache-control"),
            "set_cookie": r.headers.get("set-cookie"), "body": body}


async def ws_session(base: str, token: str | None, seconds: float, source: str | None = None,
                     auth_delay: float = 0.0, bad_token: bool = False) -> list[dict]:
    url = base.replace("http://", "ws://").replace("https://", "wss://") + "/ws/kiosk"
    events: list[dict] = []
    sock = None
    if source:
        host, port = url.split("//", 1)[1].split("/", 1)[0].rsplit(":", 1)
        sock = socket.create_connection((host, int(port)), source_address=(source, 0))
    t0 = time.monotonic()
    try:
        async with websockets.connect(url, sock=sock, open_timeout=10, max_size=2**22) as ws:
            if token is not None or bad_token:
                if auth_delay:
                    await asyncio.sleep(auth_delay)
                await ws.send(json.dumps({"type": "kiosk_auth", "token": "nmk_" + "x" * 43 if bad_token else token}))
            end = t0 + seconds
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    break
                try:
                    msg = await asyncio.wait_for(ws.recv(), left)
                except TimeoutError:
                    break
                try:
                    data = json.loads(msg)
                except ValueError:
                    data = msg
                events.append({"kind": "ws", "event": "message", "data": data, "t": round(time.monotonic() - t0, 2)})
    except websockets.ConnectionClosed as e:
        events.append({"kind": "ws", "event": "close", "code": e.rcvd.code if e.rcvd else None,
                       "reason": e.rcvd.reason if e.rcvd else None, "t": round(time.monotonic() - t0, 2)})
    except (OSError, websockets.InvalidStatus, websockets.InvalidHandshake) as e:
        events.append({"kind": "ws", "event": "error", "error": f"{type(e).__name__}: {e}",
                       "t": round(time.monotonic() - t0, 2)})
    return events


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True)
    ap.add_argument("--token", default=None)
    ap.add_argument("--source", default=None, help="local address to connect from (e.g. 127.0.0.77)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("view")
    w = sub.add_parser("ws")
    w.add_argument("--seconds", type=float, default=10)
    w.add_argument("--no-auth", action="store_true")
    w.add_argument("--bad-token", action="store_true")
    w.add_argument("--auth-delay", type=float, default=0)
    p = sub.add_parser("probe")
    p.add_argument("--path", default="/api/devices")
    h = sub.add_parser("hammer")
    h.add_argument("--count", type=int, default=31)
    a = ap.parse_args()
    if a.cmd == "view":
        out(view(a.base, a.token, a.source))
    elif a.cmd == "probe":
        out(view(a.base, a.token, a.source, a.path))
    elif a.cmd == "hammer":
        codes = [view(a.base, "nmk_" + "y" * 43, a.source)["status"] for _ in range(a.count)]
        out({"kind": "hammer", "statuses": codes})
    else:
        for e in asyncio.run(ws_session(a.base, None if a.no_auth else a.token, a.seconds, a.source, a.auth_delay,
                                        a.bad_token)):
            out(e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
