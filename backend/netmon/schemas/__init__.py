"""Pydantic request models mirroring docs/api.md (responses are built in services/serialize.py
with exactly the api.md field names)."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
ChannelType = Literal["email", "webhook", "telegram"]


class NotificationChannelCreate(_In):
    name: str = Field(min_length=1, max_length=128)
    type: ChannelType
    enabled: bool = True
    config: dict[str, Any]


class NotificationChannelPatch(_In):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    type: ChannelType | None = None
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


TELEGRAM_TOKEN_RE = re.compile(r"^\d{3,20}:[A-Za-z0-9_-]{20,100}$")
TELEGRAM_CHAT_RE = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31})$")


class TelegramConfig(BaseModel):
    """Telegram channel config. `bot_token` is a write-only secret (None = keep on PATCH)."""

    model_config = ConfigDict(extra="forbid")
    bot_token: str | None = None
    bot_token_set: bool | None = None  # ignored on input
    chat_id: str = Field(max_length=64)
    parse_mode: Literal["HTML"] | None = "HTML"
    disable_notification: bool = False

    @field_validator("bot_token")
    @classmethod
    def _token(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        if not TELEGRAM_TOKEN_RE.match(v):
            raise ValueError("invalid bot token format")
        return v

    @field_validator("chat_id")
    @classmethod
    def _chat(cls, v: str) -> str:
        v = v.strip()
        if not TELEGRAM_CHAT_RE.match(v):
            raise ValueError("must be a numeric chat id or @channelname")
        return v


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


# ---------------------------------------------------------------- [2a] discovery
V3Level = Literal["noAuthNoPriv", "authNoPriv", "authPriv"]
V3Auth = Literal["MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512"]
V3Priv = Literal["DES", "AES128", "AES192", "AES256"]


class ExtraV3In(BaseModel):
    """One extra SNMPv3 credential (same rules as SNMP credentials)."""

    model_config = ConfigDict(extra="forbid")
    v3_username: str = Field(min_length=1, max_length=64)
    v3_security_level: V3Level = "noAuthNoPriv"
    v3_auth_protocol: V3Auth | None = None
    v3_auth_key: str | None = None
    v3_auth_key_set: bool | None = None  # ignored on input
    v3_priv_protocol: V3Priv | None = None
    v3_priv_key: str | None = None
    v3_priv_key_set: bool | None = None  # ignored on input

    @field_validator("v3_auth_key", "v3_priv_key")
    @classmethod
    def _secret(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not 8 <= len(v) <= 64:
            raise ValueError("must be 8-64 characters")
        return v

    @model_validator(mode="after")
    def _keys_for_level(self) -> ExtraV3In:
        level = self.v3_security_level
        if level in ("authNoPriv", "authPriv") and not self.v3_auth_key:
            raise ValueError("v3_auth_key is required for this security level")
        if level == "authPriv" and not self.v3_priv_key:
            raise ValueError("v3_priv_key is required for this security level")
        return self

    def stored(self) -> dict[str, Any]:
        level = self.v3_security_level
        auth = level in ("authNoPriv", "authPriv")
        priv = level == "authPriv"
        return {
            "v3_username": self.v3_username,
            "v3_security_level": level,
            "v3_auth_protocol": (self.v3_auth_protocol or "SHA") if auth else None,
            "v3_auth_key": self.v3_auth_key if auth else None,
            "v3_priv_protocol": (self.v3_priv_protocol or "AES128") if priv else None,
            "v3_priv_key": self.v3_priv_key if priv else None,
        }


class ScanOptionsIn(BaseModel):
    """Scan options; every key is optional (omitted keys keep the defaults / stored value)."""

    model_config = ConfigDict(extra="forbid")
    ping: bool | None = None
    ping_timeout_ms: int | None = Field(default=None, ge=100, le=5000)
    snmp: bool | None = None
    snmp_port: int | None = Field(default=None, ge=1, le=65535)
    snmp_timeout_s: int | None = Field(default=None, ge=1, le=10)
    extra_communities: list[str] | None = Field(default=None, max_length=5)
    extra_communities_count: int | None = None  # ignored on input
    extra_v3: list[ExtraV3In] | None = Field(default=None, max_length=3)
    extra_v3_count: int | None = None  # ignored on input
    tcp: bool | None = None
    tcp_ports: list[int] | None = Field(default=None, min_length=1, max_length=32)
    tcp_timeout_ms: int | None = Field(default=None, ge=100, le=5000)
    dns: bool | None = None

    @field_validator("extra_communities")
    @classmethod
    def _comms(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        for c in v:
            if not 1 <= len(c) <= 64:
                raise ValueError("each community must be 1-64 characters")
        return v

    @field_validator("tcp_ports")
    @classmethod
    def _ports(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return v
        for p in v:
            if not 1 <= p <= 65535:
                raise ValueError("ports must be 1-65535")
        if len(set(v)) != len(v):
            raise ValueError("ports must be distinct")
        return v

    def given(self) -> dict[str, Any]:
        """Only the keys present in the request (explicit nulls included), with the v3
        entries normalized to their stored form."""
        out: dict[str, Any] = {}
        for key in self.model_fields_set:
            if key in ("extra_communities_count", "extra_v3_count"):
                continue
            value = getattr(self, key)
            if key == "extra_v3" and value is not None:
                value = [e.stored() for e in value]
            elif value is None and key not in ("snmp_port", "extra_communities", "extra_v3"):
                continue  # null for a non-nullable option = keep
            out[key] = value
        return out


class DiscoveryProfileCreate(_In):
    name: str = Field(min_length=1, max_length=128)
    targets: list[str] = Field(min_length=1, max_length=64)
    group_id: int
    options: ScanOptionsIn | None = None
    schedule_enabled: bool = False
    schedule_every_hours: int = Field(default=24, ge=1, le=720)
    notify_new_hosts: bool = False
    notify_channel_ids: list[int] = Field(default_factory=list)


class DiscoveryProfilePatch(_In):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    targets: list[str] | None = Field(default=None, min_length=1, max_length=64)
    group_id: int | None = None
    options: ScanOptionsIn | None = None
    schedule_enabled: bool | None = None
    schedule_every_hours: int | None = Field(default=None, ge=1, le=720)
    notify_new_hosts: bool | None = None
    notify_channel_ids: list[int] | None = None


class DiscoveryScanCreate(_In):
    targets: list[str] = Field(min_length=1, max_length=64)
    group_id: int
    options: ScanOptionsIn | None = None


class ImportItem(_In):
    result_id: int
    template_id: str
    name: str | None = Field(default=None, min_length=1, max_length=128)


class ImportRequest(_In):
    group_id: int | None = None
    items: list[ImportItem] = Field(min_length=1, max_length=256)

    @field_validator("items")
    @classmethod
    def _unique(cls, v: list[ImportItem]) -> list[ImportItem]:
        ids = [i.result_id for i in v]
        if len(set(ids)) != len(ids):
            raise ValueError("result_id values must be unique")
        return v


class ApplyTemplateRequest(_In):
    template_id: str
