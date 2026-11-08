"""[3] Builders for NetFlow v5/v9, IPFIX and sFlow v5 datagrams used by the flow tests."""

from __future__ import annotations

import socket
import struct
from collections.abc import Iterable, Sequence
from typing import Any


def ip(a: str) -> bytes:
    if ":" in a:
        return socket.inet_pton(socket.AF_INET6, a)
    return socket.inet_aton(a)


# ------------------------------------------------------------------ v5
def v5(records: Sequence[dict[str, Any]], sampling: int = 0, count: int | None = None) -> bytes:
    hdr = struct.pack(
        ">HHIIIIBBH", 5, len(records) if count is None else count, 1000, 1, 0, 1, 0, 0, sampling
    )
    body = b""
    for r in records:
        body += struct.pack(
            ">4s4s4sHHIIIIHHBBBBHHBBH",
            ip(r["src"]),
            ip(r["dst"]),
            b"\x00" * 4,
            r.get("in_if", 0),
            r.get("out_if", 0),
            r.get("packets", 1),
            r["bytes"],
            0,
            0,
            r.get("sport", 0),
            r.get("dport", 0),
            0,
            0,
            r.get("proto", 6),
            0,
            0,
            0,
            0,
            0,
            0,
        )
    return hdr + body


# ------------------------------------------------------------------ v9 / IPFIX
def pack_value(value: Any, length: int) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return ip(value)
    return int(value).to_bytes(length, "big")


def v9(sets: Iterable[bytes], source_id: int = 0, count: int = 0) -> bytes:
    sets = list(sets)
    return struct.pack(">HHIIII", 9, count or len(sets), 1000, 1, 1, source_id) + b"".join(sets)


def ipfix(sets: Iterable[bytes], domain: int = 0, length: int | None = None) -> bytes:
    body = b"".join(sets)
    n = 16 + len(body) if length is None else length
    return struct.pack(">HHIII", 10, n, 1, 1, domain) + body


def set_(set_id: int, body: bytes, pad: int = 0) -> bytes:
    body = body + b"\x00" * pad
    return struct.pack(">HH", set_id, 4 + len(body)) + body


def v9_template(tid: int, fields: Sequence[tuple[int, int]]) -> bytes:
    return struct.pack(">HH", tid, len(fields)) + b"".join(
        struct.pack(">HH", f, n) for f, n in fields
    )


def v9_options_template(
    tid: int, scope: Sequence[tuple[int, int]], options: Sequence[tuple[int, int]]
) -> bytes:
    return (
        struct.pack(">HHH", tid, 4 * len(scope), 4 * len(options))
        + b"".join(struct.pack(">HH", f, n) for f, n in scope)
        + b"".join(struct.pack(">HH", f, n) for f, n in options)
    )


def ipfix_template(
    tid: int, fields: Sequence[tuple[int, int] | tuple[int, int, int]], scope: int | None = None
) -> bytes:
    out = struct.pack(">HH", tid, len(fields))
    if scope is not None:
        out += struct.pack(">H", scope)
    for f in fields:
        if len(f) == 3:
            ie, n, pen = f  # type: ignore[misc]
            out += struct.pack(">HHI", ie | 0x8000, n, pen)
        else:
            out += struct.pack(">HH", f[0], f[1])
    return out


def data_record(fields: Sequence[tuple[int, int]], values: dict[int, Any]) -> bytes:
    return b"".join(pack_value(values.get(f, 0), n) for f, n in fields)


V9_FIELDS = [(8, 4), (12, 4), (7, 2), (11, 2), (4, 1), (1, 4), (2, 4), (10, 2), (14, 2)]


def flow_values(
    src: str, dst: str, sport: int, dport: int, proto: int, nbytes: int, pkts: int, i: int, o: int
) -> dict[int, Any]:
    return {8: src, 12: dst, 7: sport, 11: dport, 4: proto, 1: nbytes, 2: pkts, 10: i, 14: o}


# ------------------------------------------------------------------ sFlow v5
def eth_ipv4_tcp(
    src: str, dst: str, sport: int, dport: int, vlan: int | None = None, proto: int = 6
) -> bytes:
    eth = b"\x00\x11\x22\x33\x44\x55" + b"\x66\x77\x88\x99\xaa\xbb"
    if vlan is not None:
        eth += struct.pack(">HH", 0x8100, vlan)
    eth += struct.pack(">H", 0x0800)
    iph = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 1500, 1, 0, 64, proto, 0, ip(src), ip(dst))
    l4 = struct.pack(">HHII", sport, dport, 0, 0)
    return eth + iph + l4


def eth_ipv6_udp(src: str, dst: str, sport: int, dport: int) -> bytes:
    eth = b"\x00\x11\x22\x33\x44\x55" + b"\x66\x77\x88\x99\xaa\xbb" + struct.pack(">H", 0x86DD)
    iph = struct.pack(">IHBB16s16s", 6 << 28, 8, 17, 64, ip(src), ip(dst))
    return eth + iph + struct.pack(">HHHH", sport, dport, 8, 0)


def sflow_raw_record(header: bytes, frame_length: int, proto: int = 1) -> bytes:
    pad = (-len(header)) % 4
    body = struct.pack(">IIII", proto, frame_length, 4, len(header)) + header + b"\x00" * pad
    return struct.pack(">II", 1, len(body)) + body


def sflow_flow_sample(
    rate: int, in_if: int, out_if: int, records: Sequence[bytes], expanded: bool = True
) -> bytes:
    if expanded:
        body = struct.pack(
            ">IIIIIIIIIII", 1, 0, in_if, rate, 1000, 0, 0, in_if, 0, out_if, len(records)
        )
        fmt = 3
    else:
        body = struct.pack(">IIIIIIII", 1, in_if, rate, 1000, 0, in_if, out_if, len(records))
        fmt = 1
    body += b"".join(records)
    return struct.pack(">II", fmt, len(body)) + body


def sflow_counter_sample() -> bytes:
    body = struct.pack(">IIII", 1, 5, 0, 0)  # seq, source, no records (+ junk word)
    return struct.pack(">II", 2, len(body)) + body


def sflow(agent: str, samples: Sequence[bytes], nsamples: int | None = None) -> bytes:
    raw = ip(agent)
    kind = 2 if len(raw) == 16 else 1
    hdr = (
        struct.pack(">II", 5, kind)
        + raw
        + struct.pack(">IIII", 0, 1, 1000, len(samples) if nsamples is None else nsamples)
    )
    return hdr + b"".join(samples)
