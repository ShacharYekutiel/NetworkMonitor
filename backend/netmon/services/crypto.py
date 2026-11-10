"""[2b-3] Settings encryption: AES-256-GCM with a key derived from `secret.key`.

key   = HKDF-SHA256(secret.key bytes, salt none, info b"netmon/settings/v1", 32 bytes)
value = "v1:" + base64url(nonce12 || AESGCM(plaintext) incl. tag), AAD = the setting name
        (e.g. b"ldap.bind_password").

A value that cannot be decrypted (secret.key replaced, corrupted row) raises `DecryptError`;
callers treat it as "unset" (data-model § LDAP settings)."""

from __future__ import annotations

import base64
import functools
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PREFIX = "v1:"
INFO = b"netmon/settings/v1"
AAD_LDAP_BIND_PASSWORD = b"ldap.bind_password"


class DecryptError(ValueError):
    pass


@functools.lru_cache(maxsize=8)
def derive_key(secret_key: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=INFO).derive(
        secret_key.encode("utf-8")
    )


def encrypt(secret_key: str, plaintext: str, aad: bytes) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(derive_key(secret_key)).encrypt(nonce, plaintext.encode("utf-8"), aad)
    return PREFIX + base64.urlsafe_b64encode(nonce + ct).decode("ascii").rstrip("=")


def decrypt(secret_key: str, value: str, aad: bytes) -> str:
    if not isinstance(value, str) or not value.startswith(PREFIX):
        raise DecryptError("unknown format")
    raw = value[len(PREFIX) :]
    try:
        blob = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError) as exc:
        raise DecryptError("bad encoding") from exc
    if len(blob) < 12 + 16:
        raise DecryptError("too short")
    try:
        pt = AESGCM(derive_key(secret_key)).decrypt(blob[:12], blob[12:], aad)
    except InvalidTag as exc:
        raise DecryptError("cannot decrypt (secret.key changed?)") from exc
    return pt.decode("utf-8")
