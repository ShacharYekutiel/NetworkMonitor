#!/usr/bin/env python3
"""Raw TCP listener for the discovery lab: accepts connections and closes them (stdlib only).

    python3 tests/sim/tcp_listener.py --host 127.0.0.23 --port 9100     # "JetDirect" port of the printer

Prints "READY <host>:<port>" once listening.
"""

from __future__ import annotations

import argparse
import socket


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    a = p.parse_args()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((a.host, a.port))
    s.listen(64)
    print(f"READY {a.host}:{a.port}", flush=True)
    while True:
        c, _ = s.accept()
        c.close()


if __name__ == "__main__":
    main()
