"""[3b] Pure `application/vnd.amazon.eventstream` codec (ConverseStream responses).

Frame: total length (u32) · headers length (u32) · prelude CRC32 (u32) · headers · payload ·
message CRC32 (u32, over everything before it). Header: name length (u8) · name · type (u8) ·
value (0 true, 1 false, 2 i8, 3 i16, 4 i32, 5 i64, 6 bytes u16-len, 7 string u16-len,
8 timestamp i64 ms, 9 uuid 16 bytes). A frame larger than 1 MiB is refused.

`Decoder.feed(chunk)` returns the complete messages so far and keeps the rest; a corrupt
stream raises `EventStreamError` (code `bad_prelude_crc`, `bad_message_crc`,
`frame_too_large`, `bad_header`), which the Bedrock client maps to `bedrock_unavailable`."""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

MAX_FRAME = 1024 * 1024
PRELUDE = 12
MIN_FRAME = PRELUDE + 4


class EventStreamError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass
class Message:
    headers: dict[str, Any] = field(default_factory=dict)
    payload: bytes = b""

    @property
    def message_type(self) -> str:
        return str(self.headers.get(":message-type") or "event")

    @property
    def event_type(self) -> str | None:
        v = self.headers.get(":event-type")
        return str(v) if v is not None else None

    @property
    def exception_type(self) -> str | None:
        v = self.headers.get(":exception-type") or self.headers.get(":error-code")
        return str(v) if v is not None else None

    def json(self) -> Any:
        if not self.payload:
            return {}
        try:
            return json.loads(self.payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {"raw": self.payload.decode("utf-8", "replace")[:500]}


def _crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def parse_headers(raw: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {}
    i, n = 0, len(raw)
    try:
        while i < n:
            nlen = raw[i]
            i += 1
            name = raw[i : i + nlen].decode("utf-8")
            i += nlen
            typ = raw[i]
            i += 1
            value: Any
            if typ == 0:
                value = True
            elif typ == 1:
                value = False
            elif typ == 2:
                value = struct.unpack_from(">b", raw, i)[0]
                i += 1
            elif typ == 3:
                value = struct.unpack_from(">h", raw, i)[0]
                i += 2
            elif typ == 4:
                value = struct.unpack_from(">i", raw, i)[0]
                i += 4
            elif typ in (5, 8):
                value = struct.unpack_from(">q", raw, i)[0]
                i += 8
            elif typ in (6, 7):
                vlen = struct.unpack_from(">H", raw, i)[0]
                i += 2
                data = raw[i : i + vlen]
                if len(data) != vlen:
                    raise EventStreamError("bad_header", "truncated value")
                i += vlen
                value = data.decode("utf-8") if typ == 7 else bytes(data)
            elif typ == 9:
                value = raw[i : i + 16].hex()
                i += 16
            else:
                raise EventStreamError("bad_header", f"unknown header type {typ}")
            if i > n:
                raise EventStreamError("bad_header", "truncated header")
            out[name] = value
    except (IndexError, struct.error, UnicodeDecodeError) as exc:
        raise EventStreamError("bad_header", str(exc)) from None
    return out


def decode_frames(buf: bytes) -> tuple[list[Message], bytes]:
    """Every complete message at the start of `buf` and the remaining bytes."""
    out: list[Message] = []
    view = memoryview(buf)
    pos = 0
    while len(buf) - pos >= PRELUDE:
        total, hlen, pcrc = struct.unpack_from(">III", view, pos)
        if _crc(bytes(view[pos : pos + 8])) != pcrc:
            raise EventStreamError("bad_prelude_crc")
        if total > MAX_FRAME:
            raise EventStreamError("frame_too_large", str(total))
        if total < MIN_FRAME or hlen > total - MIN_FRAME:
            raise EventStreamError("bad_header", "inconsistent lengths")
        if len(buf) - pos < total:
            break
        frame = bytes(view[pos : pos + total])
        (mcrc,) = struct.unpack_from(">I", frame, total - 4)
        if _crc(frame[:-4]) != mcrc:
            raise EventStreamError("bad_message_crc")
        headers = parse_headers(frame[PRELUDE : PRELUDE + hlen])
        out.append(Message(headers, frame[PRELUDE + hlen : total - 4]))
        pos += total
    return out, bytes(view[pos:])


class Decoder:
    """Incremental decoder: `feed(chunk)` → complete messages (any chunk boundaries)."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, chunk: bytes) -> list[Message]:
        self._buf += chunk
        msgs, self._buf = decode_frames(self._buf)
        return msgs

    @property
    def pending(self) -> int:
        return len(self._buf)


# ---------------------------------------------------------------- encoder (tests / mocks)
def encode_headers(headers: dict[str, Any]) -> bytes:
    out = bytearray()
    for name, value in headers.items():
        nb = name.encode("utf-8")
        out += bytes([len(nb)]) + nb
        if isinstance(value, bool):
            out += bytes([0 if value else 1])
        elif isinstance(value, int):
            out += bytes([4]) + struct.pack(">i", value)
        elif isinstance(value, bytes):
            out += bytes([6]) + struct.pack(">H", len(value)) + value
        else:
            vb = str(value).encode("utf-8")
            out += bytes([7]) + struct.pack(">H", len(vb)) + vb
    return bytes(out)


def encode_message(headers: dict[str, Any], payload: bytes) -> bytes:
    hb = encode_headers(headers)
    total = PRELUDE + len(hb) + len(payload) + 4
    prelude = struct.pack(">II", total, len(hb))
    prelude += struct.pack(">I", _crc(prelude))
    body = prelude + hb + payload
    return body + struct.pack(">I", _crc(body))


def encode_event(event_type: str, data: Any) -> bytes:
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return encode_message(
        {
            ":event-type": event_type,
            ":content-type": "application/json",
            ":message-type": "event",
        },
        payload,
    )


def encode_exception(exception_type: str, message: str) -> bytes:
    payload = json.dumps({"message": message}).encode("utf-8")
    return encode_message(
        {
            ":exception-type": exception_type,
            ":content-type": "application/json",
            ":message-type": "exception",
        },
        payload,
    )
