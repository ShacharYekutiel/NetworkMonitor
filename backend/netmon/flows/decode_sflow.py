"""[3] sFlow v5 decoder (pure, architecture 13.3.3).

Flow samples (format 1) and expanded flow samples (format 3) with the raw packet header record
(format 1; header protocol 1 = Ethernet, also 11 = IPv4 and 12 = IPv6): Ethernet -> up to two
802.1Q / 802.1ad tags -> IPv4 / IPv6 -> TCP / UDP / SCTP ports. One record per sample: bytes =
frame_length, packets = 1, rate = the sample's sampling rate. Counter samples and all other
samples / records are skipped by length. Never raises: problems are `parse_error`."""

from __future__ import annotations

import socket
import struct

from netmon.flows.records import Decoded, FlowRecord

U32 = struct.Struct(">I")
FLOW_SAMPLE = struct.Struct(">IIIIIIII")  # seq, source id, rate, pool, drops, in, out, nrec
EXP_FLOW_SAMPLE = struct.Struct(">IIIIIIIIIII")  # seq, type, index, rate, pool, drops, in_fmt,
# in, out_fmt, out, nrec
RAW_HEADER = struct.Struct(">IIII")  # header protocol, frame length, stripped, header length
MAX_SAMPLES = 1024
MAX_RECORDS = 64

PORT_PROTOS = (6, 17, 132)
VLAN_TYPES = (0x8100, 0x88A8, 0x9100)


def agent_address(data: bytes) -> str | None:
    """The agent address of an sFlow v5 datagram (cheap pre-check before decoding)."""
    if len(data) < 12 or U32.unpack_from(data, 0)[0] != 5:
        return None
    kind = U32.unpack_from(data, 4)[0]
    if kind == 1 and len(data) >= 12:
        return socket.inet_ntoa(data[8:12])
    if kind == 2 and len(data) >= 24:
        raw = data[8:24]
        if raw[:12] == b"\x00" * 10 + b"\xff\xff":
            return socket.inet_ntoa(raw[12:])
        return socket.inet_ntop(socket.AF_INET6, raw)
    return None


def decode_sflow(data: bytes, source: str) -> Decoded:
    """`source` is the UDP source; the exporter is the agent address (the source when the agent
    address is unspecified)."""
    agent = agent_address(data)
    res = Decoded("sflow5", agent if agent and agent not in ("0.0.0.0", "::") else source)
    try:
        if agent is None:
            version = U32.unpack_from(data, 0)[0] if len(data) >= 4 else 5
            res.error("unsupported" if version != 5 else "parse_error")
            return res
        off = 8 + (4 if U32.unpack_from(data, 4)[0] == 1 else 16)
        n = len(data)
        if off + 16 > n:
            res.error("parse_error")
            return res
        nsamples = U32.unpack_from(data, off + 12)[0]  # after sub-agent, seq, uptime
        off += 16
        if nsamples > MAX_SAMPLES:
            res.error("parse_error")
            return res
        for _ in range(nsamples):
            if off + 8 > n:
                res.error("parse_error")
                return res
            fmt, length = struct.unpack_from(">II", data, off)
            body_off = off + 8
            off = body_off + length
            if off > n:
                res.error("parse_error")
                return res
            enterprise, kind = fmt >> 12, fmt & 0xFFF
            if enterprise != 0 or kind not in (1, 3):
                continue  # counter samples (2, 4) and anything else: skipped by length
            if not _flow_sample(data, body_off, off, kind == 3, res):
                res.error("parse_error")
    except Exception:  # noqa: BLE001
        res.error("parse_error")
    return res


def _flow_sample(data: bytes, off: int, end: int, expanded: bool, res: Decoded) -> bool:
    if expanded:
        if off + EXP_FLOW_SAMPLE.size > end:
            return False
        (_, _, _, rate, _, _, in_fmt, in_if, out_fmt, out_if, nrec) = EXP_FLOW_SAMPLE.unpack_from(
            data, off
        )
        if in_fmt != 0:
            in_if = 0
        if out_fmt != 0:
            out_if = 0
        off += EXP_FLOW_SAMPLE.size
    else:
        if off + FLOW_SAMPLE.size > end:
            return False
        (_, _, rate, _, _, in_v, out_v, nrec) = FLOW_SAMPLE.unpack_from(data, off)
        in_if = in_v & 0x3FFFFFFF if in_v >> 30 == 0 else 0
        out_if = out_v & 0x3FFFFFFF if out_v >> 30 == 0 else 0
        off += FLOW_SAMPLE.size
    if nrec > MAX_RECORDS:
        return False
    for _ in range(nrec):
        if off + 8 > end:
            return False
        fmt, length = struct.unpack_from(">II", data, off)
        rec_off = off + 8
        off = rec_off + length
        if off > end:
            return False
        if fmt != 1:
            continue  # only enterprise 0 / format 1 (raw packet header)
        if rec_off + RAW_HEADER.size > off:
            return False
        proto, frame_len, _, hlen = RAW_HEADER.unpack_from(data, rec_off)
        hstart = rec_off + RAW_HEADER.size
        if hstart + hlen > off:
            return False
        rec = _packet(data[hstart : hstart + hlen], proto)
        if rec is None:
            continue
        src, dst, ip_proto, sport, dport = rec
        res.records.append(
            FlowRecord(src, dst, sport, dport, ip_proto, frame_len, 1, in_if, out_if, rate or None)
        )
        return True  # one packet header per sample
    return True


def _packet(h: bytes, proto: int) -> tuple[bytes, bytes, int, int, int] | None:
    """Raw header -> (src, dst, protocol, sport, dport), or None when not IP."""
    if proto == 1:  # Ethernet
        if len(h) < 14:
            return None
        etype = (h[12] << 8) | h[13]
        off = 14
        for _ in range(2):
            if etype in VLAN_TYPES:
                if len(h) < off + 4:
                    return None
                etype = (h[off + 2] << 8) | h[off + 3]
                off += 4
        if etype == 0x0800:
            return _ipv4(h, off)
        if etype == 0x86DD:
            return _ipv6(h, off)
        return None
    if proto == 11:
        return _ipv4(h, 0)
    if proto == 12:
        return _ipv6(h, 0)
    return None


def _ports(h: bytes, off: int, ip_proto: int) -> tuple[int, int]:
    if ip_proto in PORT_PROTOS and len(h) >= off + 4:
        return (h[off] << 8) | h[off + 1], (h[off + 2] << 8) | h[off + 3]
    return 0, 0


def _ipv4(h: bytes, off: int) -> tuple[bytes, bytes, int, int, int] | None:
    if len(h) < off + 20 or h[off] >> 4 != 4:
        return None
    ihl = (h[off] & 0x0F) * 4
    ip_proto = h[off + 9]
    frag_off = ((h[off + 6] & 0x1F) << 8) | h[off + 7]
    sport, dport = (0, 0) if frag_off else _ports(h, off + ihl, ip_proto)
    return h[off + 12 : off + 16], h[off + 16 : off + 20], ip_proto, sport, dport


EXT_HEADERS = (0, 43, 60)  # hop-by-hop, routing, destination options


def _ipv6(h: bytes, off: int) -> tuple[bytes, bytes, int, int, int] | None:
    if len(h) < off + 40 or h[off] >> 4 != 6:
        return None
    nxt = h[off + 6]
    src, dst = h[off + 8 : off + 24], h[off + 24 : off + 40]
    p = off + 40
    for _ in range(4):
        if nxt not in EXT_HEADERS or len(h) < p + 2:
            break
        nxt, p = h[p], p + (h[p + 1] + 1) * 8
    if nxt in EXT_HEADERS or nxt == 44:  # unparsed extension / fragment
        return src, dst, nxt, 0, 0
    sport, dport = _ports(h, p, nxt)
    return src, dst, nxt, sport, dport
