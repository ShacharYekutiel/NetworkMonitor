"""[2b-3] Request models of the ingestion / event-rule / SIEM API (api.md "Phase 2b-3"),
`extra="forbid"`."""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_HOST_RE = re.compile(r"^[A-Za-z0-9._:\-\[\]]{1,255}$")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


def ip_or_cidr(v: str) -> str:
    try:
        return str(ipaddress.ip_network(str(v).strip(), strict=False))
    except ValueError:
        raise ValueError(f"{v!r} is not an IP address or CIDR") from None


class TrapV3UserIn(_In):
    username: str = Field(min_length=1, max_length=32)
    security_level: Literal["noAuthNoPriv", "authNoPriv", "authPriv"] = "authPriv"
    auth_protocol: Literal["MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512"] | None = "SHA"
    auth_key: str | None = Field(default=None, max_length=128)
    priv_protocol: Literal["DES", "AES128", "AES192", "AES256"] | None = "AES128"
    priv_key: str | None = Field(default=None, max_length=128)


class SiemTargetIn(_In):
    id: str | None = Field(default=None, pattern=r"^[0-9a-f]{6}$")
    name: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    protocol: Literal["udp", "tcp", "tls"] = "udp"
    tls_verify: bool = True
    kinds: list[Literal["syslog", "trap"]] = Field(default_factory=lambda: ["syslog", "trap"])
    min_severity: int = Field(default=7, ge=0, le=7)
    sources: list[str] = Field(default_factory=list, max_length=64)
    include_unknown: bool = False

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        if not _HOST_RE.match(v):
            raise ValueError("must be a host name or IP address")
        return v

    @field_validator("sources")
    @classmethod
    def _sources(cls, v: list[str]) -> list[str]:
        return [ip_or_cidr(x) for x in v]


class IngestSettingsMixin(BaseModel):
    """[2b-3] keys of `PUT /api/settings` (feature toggles and ingestion)."""

    feature_traps_enabled: bool | None = None
    feature_syslog_enabled: bool | None = None
    feature_event_alerts_enabled: bool | None = None
    feature_lag_enabled: bool | None = None
    feature_ldap_enabled: bool | None = None
    feature_audit_enabled: bool | None = None
    feature_timeline_enabled: bool | None = None
    feature_flapping_enabled: bool | None = None
    feature_siem_forwarding_enabled: bool | None = None
    trap_retention_days: int | None = Field(default=None, ge=1, le=3650)
    syslog_retention_days: int | None = Field(default=None, ge=1, le=3650)
    trap_community_check: Literal["any", "known"] | None = None
    trap_communities: list[str] | None = Field(default=None, max_length=32)
    trap_v3_users: list[TrapV3UserIn] | None = Field(default=None, max_length=32)
    ingest_unknown_sources: Literal["store", "drop"] | None = None
    ingest_ignored_sources: list[str] | None = Field(default=None, max_length=256)
    trap_collapse_enabled: bool | None = None
    trap_collapse_oids: list[str] | None = Field(default=None, max_length=64)
    trap_collapse_keep: int | None = Field(default=None, ge=1, le=1000)
    flap_threshold: int | None = Field(default=None, ge=3, le=50)
    flap_window_min: int | None = Field(default=None, ge=1, le=120)
    flap_quiet_min: int | None = Field(default=None, ge=1, le=1440)
    event_linkdown_scope: Literal["important", "all"] | None = None
    trunk_min_tagged_vlans: int | None = Field(default=None, ge=1, le=100)  # [1.3]
    event_linkdown_include_trunks: bool | None = None  # [1.3]
    siem_targets: list[SiemTargetIn] | None = Field(default=None, max_length=4)

    @field_validator("trap_communities")
    @classmethod
    def _communities(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        for c in v:
            if not 1 <= len(c) <= 64:
                raise ValueError("each community must be 1-64 characters")
        return list(dict.fromkeys(v))

    @field_validator("trap_collapse_oids")
    @classmethod
    def _collapse_oids(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out = []
        for raw in v:
            o = raw.strip().lstrip(".")
            if not re.fullmatch(r"[0-9]+(\.[0-9]+){1,127}", o) or len(o) > 255:
                raise ValueError(f"not a numeric OID: {raw!r}")
            out.append(o)
        return list(dict.fromkeys(out))

    @field_validator("ingest_ignored_sources")
    @classmethod
    def _ignored(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return list(dict.fromkeys(ip_or_cidr(x) for x in v))

    @field_validator("trap_v3_users")
    @classmethod
    def _users(cls, v: list[TrapV3UserIn] | None) -> list[TrapV3UserIn] | None:
        if v is None:
            return v
        names = [u.username for u in v]
        if len(set(names)) != len(names):
            raise ValueError("user names must be unique")
        return v


class TrapMatchIn(_In):
    trap_oids: list[str] = Field(default_factory=list)
    varbinds: list[dict[str, Any]] = Field(default_factory=list)
    ports: Literal["any", "important"] = "any"


class SyslogMatchIn(_In):
    severity_max: int | None = None
    facilities: list[int] = Field(default_factory=list)
    programs: list[str] = Field(default_factory=list)
    text: str | None = None
    text_regex: bool = False


class EventRuleIn(_In):
    """Writable fields; `match` / `clear_match` are validated per kind by
    netmon.ingest.rules.validate_rule (TrapMatch / SyslogMatch)."""

    name: str | None = Field(default=None, max_length=128)
    kind: Literal["trap", "syslog"] | None = None
    enabled: bool | None = None
    priority: int | None = None
    scope: Literal["global", "group", "device"] | None = None
    group_id: int | None = None
    device_id: int | None = None
    match: dict[str, Any] | None = None
    action: Literal["warning", "down", "ignore"] | None = None
    key_varbind_oid: str | None = Field(default=None, max_length=255)
    key_regex: str | None = Field(default=None, max_length=200)
    clear_match: dict[str, Any] | None = None
    clear_after_min: int | None = None


class EventRuleTestIn(_In):
    rule: EventRuleIn
    hours: int = Field(default=24, ge=1, le=168)


class AssignSourceIn(_In):
    source_ip: str
    device_id: int

    @field_validator("source_ip")
    @classmethod
    def _ip(cls, v: str) -> str:
        try:
            addr = ipaddress.ip_address(v.strip())
        except ValueError:
            raise ValueError("must be an IP address") from None
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(addr)


class ForwardTestIn(_In):
    target_id: str | None = None
    target: SiemTargetIn | None = None


def source_addresses(v: list[str] | None) -> list[str] | None:
    """[2b-3] Device.source_addresses: <= 16 unique IPv4/IPv6 addresses (no prefixes)."""
    if v is None:
        return None
    if len(v) > 16:
        raise ValueError("at most 16 source addresses")
    out = []
    for x in v:
        try:
            addr = ipaddress.ip_address(str(x).strip())
        except ValueError:
            raise ValueError(f"{x!r} is not an IP address") from None
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        out.append(str(addr))
    return list(dict.fromkeys(out))
