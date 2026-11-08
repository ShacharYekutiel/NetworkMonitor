#!/usr/bin/env python3
"""Syslog sender for the 2b-3 ingest tests (docs/architecture.md §12.14). No root needed: the socket is
bound to a loopback source address (127.0.0.x simulates a device).

    python tests/sim/ingest/send_syslog.py --to 127.0.0.1:1514 --from 127.0.0.81 --udp "<187>Nov  8 12:00:00 sw1 app: x"
    python tests/sim/ingest/send_syslog.py --to 127.0.0.1:1514 --from 127.0.0.81 --tcp --framing octet --file vectors.txt
    python tests/sim/ingest/send_syslog.py --to 127.0.0.1:1514 --udp --count 3000 --rate 0 "<14>flood {i}"
    python tests/sim/ingest/send_syslog.py --to 127.0.0.1:1514 --tcp --raw-hex 3237203c31333e...   (exact bytes)

Messages: positional arguments and/or --file (one message per line, `ID|message`, `#` comments; `--ids S1,S5`
selects lines). Escapes `\\xHH`, `\\n`, `\\r`, `\\t`, `\\0`, `\\\\` are decoded; `{x*9000}` repeats a character;
`{i}` is replaced by the message sequence number (with --count).
TCP: one connection for everything; --framing octet (`LEN SP MSG`, RFC 6587 3.4.1) or lf (MSG + LF).
--rate N = messages per second (0 = as fast as possible). Prints a JSON summary line.
Importable: `load_vectors(path)`, `send(messages, to, src, transport, framing, rate)`.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPEAT = re.compile(r"\{(.)\*(\d+)\}")


def decode_escapes(s: str) -> bytes:
    out = bytearray()
    i = 0
    raw = s.encode("utf-8")
    while i < len(raw):
        c = raw[i : i + 1]
        if c == b"\\" and i + 1 < len(raw):
            n = raw[i + 1 : i + 2]
            if n == b"x" and i + 3 < len(raw) + 1:
                out += bytes([int(raw[i + 2 : i + 4], 16)])
                i += 4
                continue
            out += {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"0": b"\0", b"\\": b"\\"}.get(n, b"\\" + n)
            i += 2
            continue
        out += c
        i += 1
    return bytes(out)


def expand(s: str, seq: int = 0) -> bytes:
    s = REPEAT.sub(lambda m: m.group(1) * int(m.group(2)), s).replace("{i}", str(seq))
    return decode_escapes(s)


def load_vectors(path: str | Path = HERE / "vectors.txt") -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        vid, msg = line.split("|", 1)
        out[vid.strip()] = expand(msg)
    return out


def frame(msg: bytes, framing: str) -> bytes:
    if framing == "octet":
        return str(len(msg)).encode() + b" " + msg
    return msg + b"\n"


def send(messages: list[bytes], to: tuple[str, int], src: str = "127.0.0.1", transport: str = "udp",
         framing: str = "octet", rate: float = 0.0, raw: bytes | None = None) -> dict:
    gap = 1.0 / rate if rate > 0 else 0.0
    t0 = time.monotonic()
    sent = 0
    if transport == "udp":
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((src, 0))
        try:
            for i, m in enumerate(messages):
                if gap:
                    delay = t0 + i * gap - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                s.sendto(m, to)
                sent += 1
            if raw is not None:
                s.sendto(raw, to)
                sent += 1
        finally:
            s.close()
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((src, 0))
        s.settimeout(10)
        s.connect(to)
        closed_by_peer = False
        try:
            if raw is not None:
                s.sendall(raw)
                sent += 1
            for i, m in enumerate(messages):
                if gap:
                    delay = t0 + i * gap - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                s.sendall(frame(m, framing))
                sent += 1
            # detect a server-side close (e.g. oversized frame) within 1 s
            s.settimeout(1.0)
            try:
                closed_by_peer = s.recv(1) == b""
            except TimeoutError:
                closed_by_peer = False
            except ConnectionError:
                closed_by_peer = True
        except (BrokenPipeError, ConnectionResetError):
            closed_by_peer = True
        finally:
            s.close()
        return {"sent": sent, "seconds": round(time.monotonic() - t0, 3), "closed_by_peer": closed_by_peer}
    return {"sent": sent, "seconds": round(time.monotonic() - t0, 3)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True)
    ap.add_argument("--from", dest="src", default="127.0.0.1")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--udp", action="store_true", default=True)
    g.add_argument("--tcp", action="store_true")
    ap.add_argument("--framing", choices=["octet", "lf"], default="octet")
    ap.add_argument("--rate", type=float, default=0.0)
    ap.add_argument("--count", type=int, default=1, help="send every message N times ({i} = sequence)")
    ap.add_argument("--file", help="vectors file (ID|message per line)")
    ap.add_argument("--ids", help="comma-separated vector ids from --file (default: all)")
    ap.add_argument("--raw-hex", help="exact bytes to send first (UDP: one datagram; TCP: written as-is)")
    ap.add_argument("messages", nargs="*")
    a = ap.parse_args()
    host, port = a.to.rsplit(":", 1)
    templates: list[str | bytes] = list(a.messages)
    if a.file:
        vec = load_vectors(a.file)
        ids = [x.strip() for x in a.ids.split(",")] if a.ids else list(vec)
        templates += [vec[i] for i in ids]
    msgs: list[bytes] = []
    for n in range(a.count):
        for t in templates:
            msgs.append(expand(t, n) if isinstance(t, str) else t)
    raw = bytes.fromhex(a.raw_hex) if a.raw_hex else None
    res = send(msgs, (host, int(port)), a.src, "tcp" if a.tcp else "udp", a.framing, a.rate, raw)
    res.update(transport="tcp" if a.tcp else "udp", src=a.src, to=a.to)
    print(json.dumps(res), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
