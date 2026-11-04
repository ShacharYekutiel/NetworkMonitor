"""Pydantic request models mirroring docs/api.md (responses are built in services/serialize.py
with exactly the api.md field names)."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

Role = Literal["admin", "viewer"]
Language = Literal["en", "he"]
StatusLit = Literal["up", "warning", "down", "paused", "unknown"]


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _email(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if v == "":
        return None
    if len(v) > 255 or not EMAIL_RE.match(v):
        raise ValueError("invalid email address")
    return v


def _username(v: str) -> str:
    if not USERNAME_RE.match(v):
        raise ValueError("must be 3-64 characters: letters, digits, '.', '_' or '-'")
    return v


# ---------------------------------------------------------------- setup / auth / users
class SetupIn(_In):
    username: str
    password: str = Field(min_length=8, max_length=128)
    email: str | None = None
    language: Language = "en"

    _u = field_validator("username")(lambda cls, v: _username(v))
    _e = field_validator("email")(lambda cls, v: _email(v))


class LoginIn(_In):
    username: str = Field(max_length=64)
    password: str = Field(max_length=128)


class MePatch(_In):
    email: str | None = None
    language: Language | None = None

    _e = field_validator("email")(lambda cls, v: _email(v))


class ChangePasswordIn(_In):
    current_password: str = Field(max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class UserCreate(_In):
    username: str
    password: str = Field(min_length=8, max_length=128)
    email: str | None = None
    role: Role = "viewer"
    language: Language = "en"

    _u = field_validator("username")(lambda cls, v: _username(v))
    _e = field_validator("email")(lambda cls, v: _email(v))


class UserPatch(_In):
    email: str | None = None
    role: Role | None = None
    language: Language | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)

    _e = field_validator("email")(lambda cls, v: _email(v))


# ---------------------------------------------------------------- SNMP credentials
class SnmpIn(_In):
    version: Literal["v1", "v2c", "v3"] = "v2c"
    port: int = Field(default=161, ge=1, le=65535)
    timeout_s: int = Field(default=2, ge=1, le=30)
    retries: int = Field(default=1, ge=0, le=5)
    community: str | None = Field(default=None, max_length=255)  # write-only secret
    v3_username: str | None = Field(default=None, max_length=64)
    v3_security_level: Literal["noAuthNoPriv", "authNoPriv", "authPriv"] | None = None
    v3_auth_protocol: Literal["MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512"] | None = None
    v3_auth_key: str | None = None
    v3_priv_protocol: Literal["DES", "AES128", "AES192", "AES256"] | None = None
    v3_priv_key: str | None = None

    @field_validator("v3_auth_key", "v3_priv_key")
    @classmethod
    def _secret(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        if not 8 <= len(v) <= 64:
            raise ValueError("must be 8-64 characters")
        return v


# ---------------------------------------------------------------- groups / devices
class GroupCreate(_In):
    name: str = Field(min_length=1, max_length=128)
    parent_id: int | None = None
    description: str = Field(default="", max_length=10000)
    snmp_credentials: SnmpIn | None = None
    sort_order: int = 0


class GroupPatch(_In):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    parent_id: int | None = None
    description: str | None = Field(default=None, max_length=10000)
    snmp_credentials: SnmpIn | None = None
    sort_order: int | None = None


def validate_http_url(v: str) -> str:
    """An absolute http(s) URL with a host and a valid port, parseable by httpx."""
    from urllib.parse import urlsplit

    import httpx

    v = v.strip()
    if not re.match(r"^https?://\S+$", v, re.IGNORECASE):
        raise ValueError("must start with http:// or https://")
    try:
        parts = urlsplit(v)
        _ = parts.port  # raises on a bad port
        httpx.URL(v)
    except (ValueError, httpx.InvalidURL) as exc:
        raise ValueError(f"invalid URL: {exc}") from exc
    if not parts.hostname:
        raise ValueError("the URL has no host")
    return v


HOST_RE = re.compile(r"^[A-Za-z0-9._:%\-\[\]]+$")


def _host(v: str) -> str:
    v = v.strip()
    if not v or len(v) > 255 or not HOST_RE.match(v) or v.startswith("-"):
        raise ValueError("must be a hostname, IPv4 or IPv6 address")
    return v.strip("[]")


class DeviceCreate(_In):
    group_id: int
    name: str = Field(min_length=1, max_length=128)
    host: str
    description: str = Field(default="", max_length=10000)
    enabled: bool = True
    snmp_credentials: SnmpIn | None = None
    sort_order: int = 0
    auto_sensors: list[str] = Field(default_factory=list)

    _h = field_validator("host")(lambda cls, v: _host(v))


class DevicePatch(_In):
    group_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=128)
    host: str | None = None
    description: str | None = Field(default=None, max_length=10000)
    enabled: bool | None = None
    snmp_credentials: SnmpIn | None = None
    sort_order: int | None = None

    @field_validator("host")
    @classmethod
    def _hv(cls, v: str | None) -> str | None:
        return None if v is None else _host(v)


# ---------------------------------------------------------------- sensors
class SensorCreate(_In):
    device_id: int
    name: str | None = Field(default=None, max_length=128)
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    interval_s: int | None = Field(default=None, ge=10, le=86400)
    enabled: bool = True
    sort_order: int = 0
    channels: list[dict[str, Any]] | None = None


class SensorPatch(_In):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    type: str | None = None
    config: dict[str, Any] | None = None
    interval_s: int | None = Field(default=None, ge=10, le=86400)
    enabled: bool | None = None
    sort_order: int | None = None
    channels: list[dict[str, Any]] | None = None


class SensorTestIn(_In):
    device_id: int | None = None
    host: str | None = None
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    snmp_credentials: SnmpIn | None = None
    channels: list[dict[str, Any]] | None = None

    @field_validator("host")
    @classmethod
    def _hv(cls, v: str | None) -> str | None:
        return None if v is None else _host(v)


# ---------------------------------------------------------------- notifications / alerts
class NotificationChannelCreate(_In):
    name: str = Field(min_length=1, max_length=128)
    type: Literal["email", "webhook"]
    enabled: bool = True
    config: dict[str, Any]


class NotificationChannelPatch(_In):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    type: Literal["email", "webhook"] | None = None
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class EmailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: list[str] = Field(min_length=1, max_length=20)

    @field_validator("to")
    @classmethod
    def _to(cls, v: list[str]) -> list[str]:
        out = []
        for a in v:
            e = _email(a)
            if e is None:
                raise ValueError("invalid email address")
            out.append(e)
        return out


class WebhookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=2048)
    headers: dict[str, str] = Field(default_factory=dict)
    verify_tls: bool = True
    timeout_s: int = Field(default=10, ge=1, le=60)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return validate_http_url(v)


class AlertRuleCreate(_In):
    name: str = Field(min_length=1, max_length=128)
    sensor_id: int | None = None
    on_statuses: list[Literal["down", "warning", "up"]] = Field(min_length=1)
    delay_s: int = Field(default=0, ge=0, le=86400)
    enabled: bool = True
    channel_ids: list[int] = Field(min_length=1)


class AlertRulePatch(_In):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    sensor_id: int | None = None
    on_statuses: list[Literal["down", "warning", "up"]] | None = Field(default=None, min_length=1)
    delay_s: int | None = Field(default=None, ge=0, le=86400)
    enabled: bool | None = None
    channel_ids: list[int] | None = Field(default=None, min_length=1)


# ---------------------------------------------------------------- settings
class SmtpIn(_In):
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    security: Literal["none", "starttls", "ssl"] | None = None
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=255)
    from_address: str | None = Field(default=None, max_length=255)
    from_name: str | None = Field(default=None, max_length=128)

    @field_validator("from_address")
    @classmethod
    def _from(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        return _email(v)


class SettingsPut(_In):
    smtp: SmtpIn | None = None
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    event_retention_days: int | None = Field(default=None, ge=1, le=3650)
    default_interval_s: int | None = Field(default=None, ge=10, le=86400)
    default_language: Language | None = None
    public_base_url: str | None = Field(default=None, max_length=512)

    @field_validator("public_base_url")
    @classmethod
    def _pbu(cls, v: str | None) -> str | None:
        if v is None or v.strip() == "":
            return None
        if not re.match(r"^https?://\S+$", v.strip(), re.IGNORECASE):
            raise ValueError("must start with http:// or https://")
        return v.strip().rstrip("/")


class SmtpTestIn(_In):
    to: str

    @field_validator("to")
    @classmethod
    def _to(cls, v: str) -> str:
        e = _email(v)
        if e is None:
            raise ValueError("invalid email address")
        return e
