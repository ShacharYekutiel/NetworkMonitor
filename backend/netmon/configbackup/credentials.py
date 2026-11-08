"""[3] SSH credentials (data-model § [3] SSH credentials): stored as jsonb on groups / devices
(NULL = inherit), secret fields AES-256-GCM encrypted with `netmon.services.crypto`
(AAD `b"ssh.<field>"`). API secret rules as `SnmpCredentials`: responses carry null + `*_set`;
in requests omitted/null keep, `""` clears, a string replaces."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from netmon.api.errors import ApiError
from netmon.services import crypto

SECRET_FIELDS = ("password", "private_key", "passphrase", "enable_password")
MAX_PRIVATE_KEY = 16 * 1024
USERNAME_RE = re.compile(r"^[\x21-\x7e]{1,64}$")


_SECRET_KEY: str | None = None  # set by the runtime (responses report unreadable secrets)


def configure(secret_key: str) -> None:
    global _SECRET_KEY
    _SECRET_KEY = secret_key


def aad(field: str) -> bytes:
    return f"ssh.{field}".encode()


class SshCredentialsIn(BaseModel):
    """Create / PATCH body of `ssh_credentials` (also the partial override of the SSH test)."""

    model_config = ConfigDict(extra="forbid")
    username: str | None = Field(default=None, max_length=64)
    port: int | None = Field(default=None, ge=1, le=65535)
    password: str | None = Field(default=None, max_length=1024)
    private_key: str | None = Field(default=None, max_length=MAX_PRIVATE_KEY)
    passphrase: str | None = Field(default=None, max_length=1024)
    enable_password: str | None = Field(default=None, max_length=1024)
    legacy_algorithms: bool | None = None

    @field_validator("username")
    @classmethod
    def _user(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not USERNAME_RE.match(v):
            raise ValueError("1-64 printable characters without spaces")
        return v


@dataclass(frozen=True)
class SshCreds:
    """Decrypted credentials, used only inside a session task (never logged)."""

    username: str
    port: int = 22
    password: str | None = None
    private_key: str | None = None
    passphrase: str | None = None
    enable_password: str | None = None
    legacy_algorithms: bool = False

    def __repr__(self) -> str:  # never print secrets
        return f"SshCreds(username={self.username!r}, port={self.port})"


class CredentialsUnreadable(ValueError):
    pass


def check_private_key(key: str, passphrase: str | None) -> None:
    """422 `invalid_private_key` when asyncssh cannot import the key (no key material in the
    message)."""
    import asyncssh

    try:
        asyncssh.import_private_key(key, passphrase or None)
    except Exception:  # noqa: BLE001 - any parse/decrypt problem
        raise ApiError(
            422,
            "invalid_private_key",
            "The SSH private key cannot be read (wrong format or passphrase)",
            [{"loc": ["body", "ssh_credentials", "private_key"], "msg": "invalid private key"}],
        ) from None


def _dec(secret_key: str, stored: dict[str, Any], name: str) -> str | None:
    value = stored.get(f"{name}_enc")
    if not value:
        return None
    return crypto.decrypt(secret_key, value, aad(name))


def merge(
    incoming: SshCredentialsIn,
    stored: dict[str, Any] | None,
    secret_key: str,
    *,
    loc: tuple[str, ...] = ("body", "ssh_credentials"),
) -> dict[str, Any]:
    """Incoming API object over the stored one -> the stored jsonb (secrets encrypted).
    Raises 422 (validation_error / invalid_private_key)."""
    stored = dict(stored or {})
    fields = incoming.model_fields_set
    out: dict[str, Any] = {
        "username": incoming.username
        if "username" in fields and incoming.username
        else stored.get("username"),
        "port": incoming.port
        if "port" in fields and incoming.port is not None
        else int(stored.get("port") or 22),
        "legacy_algorithms": bool(incoming.legacy_algorithms)
        if "legacy_algorithms" in fields and incoming.legacy_algorithms is not None
        else bool(stored.get("legacy_algorithms", False)),
    }
    errors: list[dict[str, Any]] = []
    if not out["username"]:
        errors.append({"loc": [*loc, "username"], "msg": "required"})
    changed_key = False
    for name in SECRET_FIELDS:
        value = getattr(incoming, name) if name in fields else None
        if value is None:
            out[f"{name}_enc"] = stored.get(f"{name}_enc")
        elif value == "":
            out[f"{name}_enc"] = None
            changed_key = changed_key or name in ("private_key", "passphrase")
        else:
            out[f"{name}_enc"] = crypto.encrypt(secret_key, value, aad(name))
            changed_key = changed_key or name in ("private_key", "passphrase")
    if not out.get("password_enc") and not out.get("private_key_enc"):
        errors.append({"loc": [*loc, "password"], "msg": "a password or a private key is required"})
    if errors:
        raise ApiError(422, "validation_error", "Invalid SSH credentials", errors)
    if changed_key and out.get("private_key_enc"):
        try:
            key = _dec(secret_key, out, "private_key")
            phrase = _dec(secret_key, out, "passphrase")
        except crypto.DecryptError:
            raise ApiError(
                422,
                "validation_error",
                "The stored SSH key cannot be read; enter the key again",
                [{"loc": [*loc, "private_key"], "msg": "unreadable"}],
            ) from None
        check_private_key(key or "", phrase)
    return out


def unreadable(stored: dict[str, Any] | None, secret_key: str) -> bool:
    if not stored:
        return False
    for name in SECRET_FIELDS:
        try:
            _dec(secret_key, stored, name)
        except crypto.DecryptError:
            return True
    return False


def ssh_out(stored: dict[str, Any] | None, secret_key: str | None = None) -> dict[str, Any] | None:
    """api.md `SshCredentials` (secrets never returned)."""
    if stored is None:
        return None
    out: dict[str, Any] = {
        "username": stored.get("username") or "",
        "port": int(stored.get("port") or 22),
    }
    for name in SECRET_FIELDS:
        out[name] = None
        out[f"{name}_set"] = bool(stored.get(f"{name}_enc"))
    out["legacy_algorithms"] = bool(stored.get("legacy_algorithms", False))
    key = secret_key or _SECRET_KEY
    out["credentials_unreadable"] = bool(key) and unreadable(stored, key or "")
    return out


def decrypt(stored: dict[str, Any], secret_key: str) -> SshCreds:
    """Raises CredentialsUnreadable when a secret cannot be decrypted (`secret.key`
    replaced) or nothing usable is left."""
    try:
        values = {name: _dec(secret_key, stored, name) for name in SECRET_FIELDS}
    except crypto.DecryptError as exc:
        raise CredentialsUnreadable(str(exc)) from None
    if not stored.get("username") or not (values["password"] or values["private_key"]):
        raise CredentialsUnreadable("incomplete credentials")
    return SshCreds(
        username=str(stored["username"]),
        port=int(stored.get("port") or 22),
        password=values["password"],
        private_key=values["private_key"],
        passphrase=values["passphrase"],
        enable_password=values["enable_password"],
        legacy_algorithms=bool(stored.get("legacy_algorithms", False)),
    )


def resolve(
    device_creds: dict[str, Any] | None, chain: list[tuple[int, dict[str, Any] | None]]
) -> tuple[dict[str, Any] | None, str, int | None]:
    """Effective stored credentials: device -> its group -> parent groups -> none.
    `chain`: [(group id, ssh_credentials)] from the device's group upwards.
    Returns (stored, source "device" | "group" | "none", group id)."""
    if device_creds is not None:
        return device_creds, "device", None
    for gid, creds in chain:
        if creds is not None:
            return creds, "group", gid
    return None, "none", None


def audit_view(stored: dict[str, Any] | None) -> dict[str, Any] | None:
    """Audited form (secrets as *** by the audit redaction: `*_enc` -> the secret names)."""
    if stored is None:
        return None
    out: dict[str, Any] = {
        "username": stored.get("username"),
        "port": stored.get("port"),
        "legacy_algorithms": bool(stored.get("legacy_algorithms", False)),
    }
    for name in SECRET_FIELDS:
        out[name] = "set" if stored.get(f"{name}_enc") else None
    return out
