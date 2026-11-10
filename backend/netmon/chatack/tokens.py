"""[3] Signed acknowledge tokens (architecture § 13.8.1, data-model § Ack token vectors).

    token = "a1." + base64url(sensor_id u32 ‖ event_id u64 ‖ exp u32 (unix minutes)
                              ‖ HMAC-SHA256(key, b"a1" ‖ those 16 bytes)[:12])

3 + 38 = 41 characters — fits Telegram's 64-byte `callback_data`. The token names *which
problem* (sensor + the problem's opening `status_change` event); it authenticates nobody."""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import hmac
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PREFIX = "a1."
TOKEN_LEN = 41
TAG_LEN = 12
INFO = b"netmon/chat-ack/v1"


class AckTokenError(ValueError):
    def __init__(self, reason: str) -> None:  # "invalid" | "expired"
        super().__init__(reason)
        self.reason = reason


@functools.lru_cache(maxsize=4)
def derive_key(secret_key: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=INFO).derive(
        secret_key.encode("utf-8")
    )


def _tag(key: bytes, body: bytes) -> bytes:
    return hmac.new(key, b"a1" + body, hashlib.sha256).digest()[:TAG_LEN]


def encode(key: bytes, sensor_id: int, event_id: int, exp_min: int) -> str:
    body = struct.pack(">IQI", sensor_id, event_id, exp_min)
    raw = body + _tag(key, body)
    return PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(key: bytes, token: str, now_min: int | None = None) -> tuple[int, int, int]:
    """(sensor_id, event_id, exp_min); raises AckTokenError("invalid" | "expired")."""
    if not isinstance(token, str) or len(token) != TOKEN_LEN or not token.startswith(PREFIX):
        raise AckTokenError("invalid")
    b64 = token[len(PREFIX) :]
    try:
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    except (binascii.Error, ValueError):
        raise AckTokenError("invalid") from None
    if len(raw) != 16 + TAG_LEN:
        raise AckTokenError("invalid")
    # canonical form only (the last character carries 4 unused bits)
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != b64:
        raise AckTokenError("invalid")
    body, tag = raw[:16], raw[16:]
    if not hmac.compare_digest(tag, _tag(key, body)):
        raise AckTokenError("invalid")
    sensor_id, event_id, exp_min = struct.unpack(">IQI", body)
    if now_min is not None and exp_min < now_min:
        raise AckTokenError("expired")
    return sensor_id, event_id, exp_min
