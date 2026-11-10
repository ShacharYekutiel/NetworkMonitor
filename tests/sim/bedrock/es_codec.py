"""`application/vnd.amazon.eventstream` encoder (and a small reference decoder) for the Bedrock mock.

Independent of `netmon.assistant.eventstream` on purpose: the mock builds frames with its own encoder so the
product's decoder is tested against a second implementation (docs/architecture.md §14.11).

Frame layout (all integers big-endian):
    total_length u32 | headers_length u32 | prelude_crc u32 | headers ... | payload ... | message_crc u32
Header: name_len u8 | name | value_type u8 | value. Only type 7 (string: u16 length + utf-8) is produced here,
which is all Bedrock sends; the decoder also understands bool/byte/short/int/long/bytes/timestamp/uuid.
"""

from __future__ import annotations

import binascii
import json
import struct
from typing import Any

MAX_FRAME = 1024 * 1024


def _crc(b: bytes) -> int:
    return binascii.crc32(b) & 0xFFFFFFFF


def encode_headers(headers: list[tuple[str, str]]) -> bytes:
    out = bytearray()
    for name, value in headers:
        n = name.encode()
        v = value.encode()
        out += struct.pack(">B", len(n)) + n + b"\x07" + struct.pack(">H", len(v)) + v
    return bytes(out)


def encode_frame(headers: list[tuple[str, str]], payload: bytes) -> bytes:
    h = encode_headers(headers)
    total = 12 + len(h) + len(payload) + 4
    prelude = struct.pack(">II", total, len(h))
    prelude += struct.pack(">I", _crc(prelude))
    body = prelude + h + payload
    return body + struct.pack(">I", _crc(body))


def event(event_type: str, payload: dict[str, Any], *, pad: str | None = None) -> bytes:
    """One `:message-type event` frame. Real Bedrock adds a random padding field `p`; so do we."""
    p = dict(payload)
    if pad is not None:
        p["p"] = pad
    return encode_frame(
        [(":event-type", event_type), (":content-type", "application/json"), (":message-type", "event")],
        json.dumps(p, separators=(",", ":"), ensure_ascii=False).encode(),
    )


def exception(exception_type: str, message: str) -> bytes:
    """An in-stream exception frame, e.g. `throttlingException`, `modelStreamErrorException`."""
    return encode_frame(
        [(":exception-type", exception_type), (":content-type", "application/json"), (":message-type", "exception")],
        json.dumps({"message": message}).encode(),
    )


# ------------------------------------------------------------------------------------------------ decoder
class DecodeError(ValueError):
    pass


def _read_headers(b: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {}
    i = 0
    while i < len(b):
        n = b[i]
        name = b[i + 1 : i + 1 + n].decode()
        i += 1 + n
        t = b[i]
        i += 1
        if t in (0, 1):
            v: Any = t == 0
        elif t == 2:
            v = struct.unpack(">b", b[i : i + 1])[0]; i += 1
        elif t == 3:
            v = struct.unpack(">h", b[i : i + 2])[0]; i += 2
        elif t == 4:
            v = struct.unpack(">i", b[i : i + 4])[0]; i += 4
        elif t in (5, 8):
            v = struct.unpack(">q", b[i : i + 8])[0]; i += 8
        elif t in (6, 7):
            ln = struct.unpack(">H", b[i : i + 2])[0]
            raw = b[i + 2 : i + 2 + ln]
            v = raw.decode() if t == 7 else raw
            i += 2 + ln
        elif t == 9:
            v = b[i : i + 16]; i += 16
        else:
            raise DecodeError(f"bad header type {t}")
        out[name] = v
    return out


def decode(buf: bytes) -> tuple[list[dict[str, Any]], bytes]:
    """Reference decoder: returns ([{"headers", "payload"}], rest). Raises DecodeError on CRC/size errors."""
    msgs = []
    while len(buf) >= 12:
        total, hlen = struct.unpack(">II", buf[:8])
        if struct.unpack(">I", buf[8:12])[0] != _crc(buf[:8]):
            raise DecodeError("bad_prelude_crc")
        if total > MAX_FRAME:
            raise DecodeError("frame_too_large")
        if len(buf) < total:
            break
        frame, buf = buf[:total], buf[total:]
        if struct.unpack(">I", frame[-4:])[0] != _crc(frame[:-4]):
            raise DecodeError("bad_message_crc")
        headers = _read_headers(frame[12 : 12 + hlen])
        payload = frame[12 + hlen : -4]
        msgs.append({"headers": headers, "payload": payload})
    return msgs, buf


def decode_events(buf: bytes) -> list[dict[str, Any]]:
    """Convenience for tests: every frame as {"type", "message_type", "body"} (JSON payload parsed)."""
    msgs, rest = decode(buf)
    if rest:
        raise DecodeError(f"{len(rest)} trailing bytes")
    out = []
    for m in msgs:
        h = m["headers"]
        body = json.loads(m["payload"]) if m["payload"] else None
        out.append({"type": h.get(":event-type") or h.get(":exception-type"),
                    "message_type": h.get(":message-type"), "body": body})
    return out


# The contract vector (docs/data-model.md § [3b] Event-stream vector); asserted by the self-test.
VECTOR_HEX = (
    "0000009f00000057c37babff0b3a6576656e742d74797065070011636f6e74656e74426c6f636b44656c74610d3a636f6e74656e742d74"
    "7970650700106170706c69636174696f6e2f6a736f6e0d3a6d6573736167652d747970650700056576656e747b22636f6e74656e74"
    "426c6f636b496e646578223a302c2264656c7461223a7b2274657874223a224869227d2c2270223a2261626364227df2ace5ff"
)


def vector_frame() -> bytes:
    return encode_frame(
        [(":event-type", "contentBlockDelta"), (":content-type", "application/json"), (":message-type", "event")],
        b'{"contentBlockIndex":0,"delta":{"text":"Hi"},"p":"abcd"}',
    )
