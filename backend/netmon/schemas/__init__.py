"""Pydantic request models mirroring docs/api.md (responses are built in services/serialize.py
with exactly the api.md field names)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from netmon.auth.ldap_config import LdapSettingsIn
from netmon.schemas.ingest import IngestSettingsMixin, source_addresses

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

Role = Literal["admin", "viewer"]
Language = Literal["en", "he"]
StatusLit = Literal["up", "warning", "down", "paused", "unknown", "unreachable"]
InventoryMode = Literal["auto", "on", "off"]  # [2b-2]


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
def _tz(v: str | None) -> str | None:
    from netmon.services.maintenance import valid_timezone

    if v is None:
        return None
    if not valid_timezone(v):
        raise ValueError("must be a valid IANA time zone name (e.g. Asia/Jerusalem)")
    return v


class SetupIn(_In):
    username: str
    password: str = Field(min_length=8, max_length=128)
    email: str | None = None
    language: Language = "en"
    # [2b-1] the browser zone; an invalid value is ignored by the setup endpoint (logged)
    timezone: str | None = Field(default=None, max_length=128)

    _u = field_validator("username")(lambda cls, v: _username(v))
    _e = field_validator("email")(lambda cls, v: _email(v))


class LoginIn(_In):
    # [2b-3] AD names: `user`, `DOMAIN\\user`, `user@suffix` (1-256 chars)
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(max_length=1024)


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


# ---------------------------------------------------------------- [2b-1] dependencies
class DependencyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["inherit", "none", "device"]
    device_id: int | None = None
    sensor_id: int | None = None


class DependencyBulkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: int
    dependency: DependencyIn


class DependencyBulkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[DependencyBulkItem] = Field(min_length=1, max_length=1000)

    @field_validator("items")
    @classmethod
    def _unique(cls, v: list[DependencyBulkItem]) -> list[DependencyBulkItem]:
        ids = [i.device_id for i in v]
        if len(set(ids)) != len(ids):
            raise ValueError("device_id values must be unique")
        return v


# ---------------------------------------------------------------- groups / devices
class GroupCreate(_In):
    name: str = Field(min_length=1, max_length=128)
    parent_id: int | None = None
    description: str = Field(default="", max_length=10000)
    snmp_credentials: SnmpIn | None = None
    sort_order: int = 0
    dependency: DependencyIn | None = None


class GroupPatch(_In):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    parent_id: int | None = None
    description: str | None = Field(default=None, max_length=10000)
    snmp_credentials: SnmpIn | None = None
    sort_order: int | None = None
    dependency: DependencyIn | None = None


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
    dependency: DependencyIn | None = None
    port_inventory: InventoryMode = "auto"  # [2b-2]
    arp_collect: InventoryMode = "auto"  # [2b-2]
    source_addresses: list[str] = Field(default_factory=list)  # [2b-3]

    _h = field_validator("host")(lambda cls, v: _host(v))
    _sa = field_validator("source_addresses")(lambda cls, v: source_addresses(v))


class DevicePatch(_In):
    group_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=128)
    host: str | None = None
    description: str | None = Field(default=None, max_length=10000)
    enabled: bool | None = None
    snmp_credentials: SnmpIn | None = None
    sort_order: int | None = None
    dependency: DependencyIn | None = None
    port_inventory: InventoryMode | None = None  # [2b-2]
    arp_collect: InventoryMode | None = None  # [2b-2]
    source_addresses: list[str] | None = None  # [2b-3]

    _sa = field_validator("source_addresses")(lambda cls, v: source_addresses(v))

    @field_validator("port_inventory", "arp_collect")
    @classmethod
    def _mode_not_null(cls, v: str | None) -> str:
        # omitted = unchanged; an explicit null is invalid (only auto | on | off)
        if v is None:
            raise ValueError("must be one of 'auto', 'on', 'off'")
        return v

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
ChannelType = Literal["email", "webhook", "telegram", "teams"]


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


class TeamsConfig(BaseModel):
    """[2b-3] Teams Workflows webhook. `url` is a write-only secret (None = keep on PATCH)."""

    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(default=None, max_length=2048)
    # response-only keys a client may echo back; ignored
    url_set: bool | None = None
    url_host: str | None = None
    legacy: bool | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        v = validate_http_url(v)
        if not v.lower().startswith("https://"):
            raise ValueError("must start with https://")
        return v


class AlertRuleCreate(_In):
    name: str = Field(min_length=1, max_length=128)
    sensor_id: int | None = None
    on_statuses: list[Literal["down", "warning", "up"]] = Field(min_length=1)
    delay_s: int = Field(default=0, ge=0, le=86400)
    enabled: bool = True
    channel_ids: list[int] = Field(min_length=1)
    escalation_after_s: int | None = Field(default=None, ge=60, le=604800)  # [2b-1]
    escalation_channel_ids: list[int] = Field(default_factory=list, max_length=100)
    repeat_every_s: int | None = Field(default=None, ge=300, le=604800)


class AlertRulePatch(_In):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    sensor_id: int | None = None
    on_statuses: list[Literal["down", "warning", "up"]] | None = Field(default=None, min_length=1)
    delay_s: int | None = Field(default=None, ge=0, le=86400)
    enabled: bool | None = None
    channel_ids: list[int] | None = Field(default=None, min_length=1)
    escalation_after_s: int | None = Field(default=None, ge=60, le=604800)  # [2b-1]
    escalation_channel_ids: list[int] | None = Field(default=None, max_length=100)
    repeat_every_s: int | None = Field(default=None, ge=300, le=604800)


# ---------------------------------------------------------------- [2b-1] acknowledge
class AcknowledgeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str = Field(default="", max_length=1000)


# ---------------------------------------------------------------- [2b-1] maintenance
START_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class MaintenanceWindowIn(BaseModel):
    """Create (all fields) and PATCH (subset) of a maintenance window; the cross-field rules
    are checked in the API on the merged state."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    scope: Literal["global", "group", "device"] | None = None
    group_id: int | None = None
    device_id: int | None = None
    kind: Literal["once", "weekly"] | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    weekdays: list[int] | None = Field(default=None, max_length=7)
    start_time: str | None = None
    duration_min: int | None = Field(default=None, ge=1, le=10080)
    comment: str | None = Field(default=None, max_length=1000)

    @field_validator("start_time")
    @classmethod
    def _st(cls, v: str | None) -> str | None:
        if v is not None and not START_TIME_RE.match(v):
            raise ValueError("must be HH:MM (00:00-23:59)")
        return v

    @field_validator("weekdays")
    @classmethod
    def _wd(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return v
        if not v:
            raise ValueError("must not be empty")
        if any(not 1 <= d <= 7 for d in v) or len(set(v)) != len(v):
            raise ValueError("ISO weekdays 1-7, distinct")
        return sorted(v)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            from datetime import UTC

            v = v.replace(tzinfo=UTC)
        return v


# ---------------------------------------------------------------- [2b-1] topology
class TopologyLinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a_device_id: int | None = None
    a_if_index: int | None = Field(default=None, ge=1)
    a_port: str | None = Field(default=None, max_length=255)
    b_device_id: int | None = None
    b_neighbor_id: int | None = None
    b_if_index: int | None = Field(default=None, ge=1)
    b_port: str | None = Field(default=None, max_length=255)


class LayoutPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(min_length=1, max_length=32)
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)


class LayoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    positions: list[LayoutPosition] = Field(min_length=1, max_length=5000)


class ClockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offset_s: int = Field(ge=-31536000, le=31536000)


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


class SettingsPut(_In, IngestSettingsMixin):
    smtp: SmtpIn | None = None
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    event_retention_days: int | None = Field(default=None, ge=1, le=3650)
    default_interval_s: int | None = Field(default=None, ge=10, le=86400)
    default_language: Language | None = None
    public_base_url: str | None = Field(default=None, max_length=512)
    timezone: str | None = None  # [2b-1]
    topology_refresh_min: int | None = Field(default=None, ge=0, le=1440)  # [2b-1]
    # [2b-2]
    port_inventory_min: int | None = Field(default=None, ge=0, le=1440)
    uplink_mac_threshold: int | None = Field(default=None, ge=1, le=1000)
    port_mac_retention_days: int | None = Field(default=None, ge=1, le=365)
    reverse_dns: bool | None = None
    # [2b-3] Active Directory (merged field by field) and audit retention
    ldap: LdapSettingsIn | None = None
    audit_retention_days: int | None = Field(default=None, ge=30, le=3650)

    _tzv = field_validator("timezone")(lambda cls, v: _tz(v))

    @field_validator("topology_refresh_min", "port_inventory_min")
    @classmethod
    def _trm(cls, v: int | None) -> int | None:
        if v is not None and 0 < v < 5:
            raise ValueError("must be 0 (off) or 5-1440")
        return v

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
    fingerprint: bool | None = None  # [2b-1]
    fingerprint_timeout_ms: int | None = Field(default=None, ge=500, le=5000)  # [2b-1]

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
