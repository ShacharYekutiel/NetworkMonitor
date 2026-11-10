"""[2b-3] LDAP settings (key `ldap`, data-model § LDAP settings): defaults, API shape, merge with
the write-only bind password (AES-GCM encrypted at rest), validation."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from netmon.auth import ldap_pure
from netmon.services import crypto

DEFAULT_LDAP: dict[str, Any] = {
    "servers": [],
    "security": "ldaps",
    "tls_verify": True,
    "domain": "",
    "netbios_domain": "",
    "base_dn": "",
    "bind_dn": "",
    "bind_password_enc": None,
    "admin_group_dn": "",
    "viewer_group_dn": "",
    "timeout_s": 5,
}

PUBLIC_FIELDS = (
    "servers",
    "security",
    "tls_verify",
    "domain",
    "netbios_domain",
    "base_dn",
    "bind_dn",
    "admin_group_dn",
    "viewer_group_dn",
    "timeout_s",
)


def _dn_ok(v: str) -> bool:
    from ldap3.core.exceptions import LDAPInvalidDnError
    from ldap3.utils.dn import parse_dn

    try:
        parse_dn(v)
    except (LDAPInvalidDnError, ValueError, TypeError, IndexError):
        return False
    return True


class LdapSettingsIn(BaseModel):
    """Partial `ldap` settings (PUT /api/settings, POST /api/ldap/test*)."""

    model_config = ConfigDict(extra="forbid")
    servers: list[str] | None = Field(default=None, max_length=8)
    security: Literal["ldaps", "starttls"] | None = None
    tls_verify: bool | None = None
    domain: str | None = Field(default=None, max_length=255)
    netbios_domain: str | None = Field(default=None, max_length=15)
    base_dn: str | None = Field(default=None, max_length=1024)
    bind_dn: str | None = Field(default=None, max_length=1024)
    bind_password: str | None = Field(default=None, max_length=1024)
    # response-only keys a client may echo back; ignored
    bind_password_set: bool | None = None
    bind_password_unreadable: bool | None = None
    admin_group_dn: str | None = Field(default=None, max_length=1024)
    viewer_group_dn: str | None = Field(default=None, max_length=1024)
    timeout_s: int | None = Field(default=None, ge=1, le=30)

    @field_validator("servers")
    @classmethod
    def _servers(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out = []
        for s in v:
            s = s.strip()
            if not s or len(s) > 255 or any(c.isspace() for c in s):
                raise ValueError("each server must be host, host:port or [v6]:port")
            try:
                ldap_pure.parse_server(s, "ldaps")
            except ValueError as exc:
                raise ValueError(str(exc)) from None
            out.append(s)
        return out

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().strip(".")
        if v and not all(p and len(p) <= 63 for p in v.split(".")):
            raise ValueError("invalid domain name")
        return v

    @field_validator("netbios_domain", "bind_dn")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v

    @field_validator("base_dn", "admin_group_dn", "viewer_group_dn")
    @classmethod
    def _dn(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if v and not _dn_ok(v):
            raise ValueError(
                "must be a distinguished name (e.g. CN=NetMon-Admins,DC=mnidom,DC=net)"
            )
        return v


def normalize(stored: Any) -> dict[str, Any]:
    out = copy.deepcopy(DEFAULT_LDAP)
    if isinstance(stored, dict):
        for k in DEFAULT_LDAP:
            if k in stored:
                out[k] = stored[k]
    return out


def decrypt_password(stored: dict[str, Any], secret_key: str) -> tuple[str | None, bool]:
    """(password, unreadable)."""
    enc = stored.get("bind_password_enc")
    if not enc:
        return None, False
    try:
        return crypto.decrypt(secret_key, enc, crypto.AAD_LDAP_BIND_PASSWORD), False
    except crypto.DecryptError:
        return None, True


def ldap_out(stored: Any, secret_key: str | None) -> dict[str, Any]:
    s = normalize(stored)
    out = {k: copy.deepcopy(s[k]) for k in PUBLIC_FIELDS}
    unreadable = False
    if s.get("bind_password_enc") and secret_key:
        _, unreadable = decrypt_password(s, secret_key)
    out["bind_password"] = None
    out["bind_password_set"] = bool(s.get("bind_password_enc")) and not unreadable
    out["bind_password_unreadable"] = unreadable
    # api.md field order
    order = [
        "servers",
        "security",
        "tls_verify",
        "domain",
        "netbios_domain",
        "base_dn",
        "bind_dn",
        "bind_password",
        "bind_password_set",
        "bind_password_unreadable",
        "admin_group_dn",
        "viewer_group_dn",
        "timeout_s",
    ]
    return {k: out[k] for k in order}


def merge(stored: Any, incoming: LdapSettingsIn | None, secret_key: str) -> dict[str, Any]:
    """Field-by-field merge. `bind_password`: omitted/null keep, "" clear, string replace."""
    out = normalize(stored)
    if incoming is None:
        return out
    for key in incoming.model_fields_set:
        if key in ("bind_password_set", "bind_password_unreadable"):
            continue
        value = getattr(incoming, key)
        if key == "bind_password":
            if value is None:
                continue
            out["bind_password_enc"] = (
                None
                if value == ""
                else crypto.encrypt(secret_key, value, crypto.AAD_LDAP_BIND_PASSWORD)
            )
            continue
        if value is None:
            continue
        out[key] = value
    return out


def validation_errors(stored: Any, secret_key: str) -> list[dict[str, Any]]:
    """What is missing for AD logins (422 when turning `ldap` on / saving it while on)."""
    s = normalize(stored)
    loc = ["body", "ldap"]
    errors: list[dict[str, Any]] = []
    if not s["bind_dn"]:
        errors.append({"loc": [*loc, "bind_dn"], "msg": "a service account is required"})
    pw, unreadable = decrypt_password(s, secret_key)
    if not pw:
        msg = (
            "the stored bind password cannot be decrypted; enter it again"
            if unreadable
            else "the service account password is required"
        )
        errors.append({"loc": [*loc, "bind_password"], "msg": msg})
    if not s["servers"] and not s["domain"]:
        errors.append(
            {"loc": [*loc, "servers"], "msg": "servers or a domain (DNS SRV) is required"}
        )
    base = s["base_dn"] or ldap_pure.base_dn_from_domain(s["domain"])
    if not base:
        errors.append({"loc": [*loc, "base_dn"], "msg": "a base DN (or a domain) is required"})
    elif not _dn_ok(base):
        errors.append({"loc": [*loc, "base_dn"], "msg": "invalid base DN"})
    if not s["admin_group_dn"] and not s["viewer_group_dn"]:
        errors.append({"loc": [*loc, "admin_group_dn"], "msg": "at least one group DN is required"})
    for k in ("admin_group_dn", "viewer_group_dn"):
        if s[k] and not _dn_ok(s[k]):
            errors.append({"loc": [*loc, k], "msg": "invalid DN"})
    return errors


@dataclass
class LdapConfig:
    servers: list[str] = field(default_factory=list)
    security: str = "ldaps"
    tls_verify: bool = True
    domain: str = ""
    netbios_domain: str = ""
    base_dn: str = ""
    bind_dn: str = ""
    bind_password: str | None = None
    bind_password_unreadable: bool = False
    admin_group_dn: str = ""
    viewer_group_dn: str = ""
    timeout_s: int = 5

    @property
    def effective_base_dn(self) -> str:
        return self.base_dn or ldap_pure.base_dn_from_domain(self.domain)

    @property
    def budget_s(self) -> float:
        return float(min(3 * self.timeout_s + 2, 30))


def to_config(stored: Any, secret_key: str) -> LdapConfig:
    s = normalize(stored)
    pw, unreadable = decrypt_password(s, secret_key)
    return LdapConfig(
        servers=list(s["servers"] or []),
        security=s["security"] if s["security"] in ("ldaps", "starttls") else "ldaps",
        tls_verify=bool(s["tls_verify"]),
        domain=s["domain"] or "",
        netbios_domain=s["netbios_domain"] or "",
        base_dn=s["base_dn"] or "",
        bind_dn=s["bind_dn"] or "",
        bind_password=pw,
        bind_password_unreadable=unreadable,
        admin_group_dn=s["admin_group_dn"] or "",
        viewer_group_dn=s["viewer_group_dn"] or "",
        timeout_s=int(s["timeout_s"] or 5),
    )


def diffable(stored: Any) -> dict[str, Any]:
    """The stored config as audited: the password only as a secret marker."""
    s = normalize(stored)
    out = {k: s[k] for k in PUBLIC_FIELDS}
    out["bind_password"] = s.get("bind_password_enc")  # redacted by the audit (secret name)
    return out
