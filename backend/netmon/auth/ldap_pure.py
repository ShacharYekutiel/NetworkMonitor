"""[2b-3] Pure Active Directory helpers (no I/O): login-name parsing, RFC 4515 escaping,
filters, userAccountControl decisions, AD bind-error mapping, server parsing.

Exhaustively unit-tested (tests/test_2b3_ldap_pure.py) with the data-model vectors."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ldap3.utils.conv import escape_filter_chars

# userAccountControl / msDS-User-Account-Control-Computed bits
UAC_ACCOUNTDISABLE = 0x2
UAC_LOCKOUT = 0x10
UAC_PASSWORD_EXPIRED = 0x800000

NEVER_EXPIRES = (0, 9223372036854775807)
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)

# OID of LDAP_MATCHING_RULE_IN_CHAIN (transitive membership)
IN_CHAIN = "1.2.840.113556.1.4.1941"
# rootDSE supportedCapabilities value of Active Directory (LDAP_CAP_ACTIVE_DIRECTORY_OID)
AD_CAPABILITY = "1.2.840.113556.1.4.800"

USER_ATTRS = [
    "sAMAccountName",
    "userPrincipalName",
    "displayName",
    "mail",
    "objectGUID",
    "userAccountControl",
    "msDS-User-Account-Control-Computed",
    "accountExpires",
    "distinguishedName",
]

# AD "data XXX" sub-codes of LDAP result 49 (invalidCredentials)
AD_BIND_ERRORS = {
    "525": "unknown_user",
    "52e": "bad_password",
    "530": "not_permitted",
    "531": "not_permitted",
    "532": "password_expired",
    "533": "account_disabled",
    "701": "account_expired",
    "773": "password_must_change",
    "775": "account_locked",
}

_SAM_FORBIDDEN = set('"/\\[]:;|=,+*?<>@')
_DATA_RE = re.compile(r"\bdata\s+([0-9a-fA-F]{3,8})\b")


def escape(value: str) -> str:
    """RFC 4515 escaping (\\ * ( ) NUL) of a value placed in a search filter."""
    return str(escape_filter_chars(value))


@dataclass(frozen=True)
class LoginName:
    sam: str | None  # sAMAccountName to search for
    upn: str | None  # userPrincipalName to search for (searched first)


def valid_login_text(username: str) -> bool:
    return 1 <= len(username) <= 256 and not any(ord(c) < 32 or ord(c) == 127 for c in username)


def parse_login(username: str, domain: str = "", netbios_domain: str = "") -> LoginName | None:
    """`user` | `DOMAIN\\user` | `user@suffix`. Returns None when the name cannot be a
    directory name (bad characters, a foreign NetBIOS domain, empty parts).

    For `user@suffix` the UPN is searched first; when the suffix is the configured domain the
    local part is also a sAMAccountName candidate."""
    if not valid_login_text(username):
        return None
    name = username.strip()
    if not name:
        return None
    if "\\" in name:
        dom, _, user = name.partition("\\")
        allowed = {d.lower() for d in (netbios_domain, domain.split(".")[0] if domain else "") if d}
        if not dom or dom.lower() not in allowed or not valid_sam(user):
            return None
        return LoginName(sam=user, upn=None)
    if "@" in name:
        local, _, suffix = name.rpartition("@")
        if not local or not suffix:
            return None
        fallback = None
        if domain and suffix.lower() == domain.lower() and valid_sam(local):
            fallback = local
        return LoginName(sam=fallback, upn=name)
    if not valid_sam(name):
        return None
    return LoginName(sam=name, upn=None)


def valid_sam(sam: str) -> bool:
    """sAMAccountName as stored for LDAP users: 1–64 printable chars, none of "/\\[]:;|=,+*?<>@."""
    if not 1 <= len(sam) <= 64:
        return False
    return all(c.isprintable() and c not in _SAM_FORBIDDEN for c in sam)


def user_filter_sam(sam: str) -> str:
    return f"(&(objectCategory=person)(objectClass=user)(sAMAccountName={escape(sam)}))"


def user_filter_upn(upn: str) -> str:
    return f"(&(objectCategory=person)(objectClass=user)(userPrincipalName={escape(upn)}))"


def group_filter(group_dn: str) -> str:
    return f"(memberOf:{IN_CHAIN}:={escape(group_dn)})"


def base_dn_from_domain(domain: str) -> str:
    labels = [p for p in (domain or "").strip().strip(".").split(".") if p]
    return ",".join(f"DC={p}" for p in labels)


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def account_expires_at(value: Any) -> datetime | None:
    """AD `accountExpires` (FILETIME, 100 ns since 1601) → aware datetime; None = never."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        # ldap3 formats "never" as 9999-12-31 / 1601-01-01
        if value.year >= 9999 or value.year <= 1601:
            return None
        return value
    n = _as_int(value)
    if n is None or n in NEVER_EXPIRES or n < 0:
        return None
    try:
        return _FILETIME_EPOCH + timedelta(microseconds=n // 10)
    except OverflowError:
        return None


def account_state(uac: Any, computed: Any, account_expires: Any, now: datetime) -> str | None:
    """Reason code when the account may not log in, else None."""
    u = _as_int(uac) or 0
    c = _as_int(computed) or 0
    if u & UAC_ACCOUNTDISABLE:
        return "account_disabled"
    if c & UAC_LOCKOUT or u & UAC_LOCKOUT:
        return "account_locked"
    if c & UAC_PASSWORD_EXPIRED or u & UAC_PASSWORD_EXPIRED:
        return "password_expired"
    exp = account_expires_at(account_expires)
    if exp is not None and exp <= now:
        return "account_expired"
    return None


def map_bind_error(message: str | None) -> str:
    """AD bind failure message (`…AcceptSecurityContext error, data 52e, v4563`) → reason."""
    m = _DATA_RE.search(message or "")
    if m:
        return AD_BIND_ERRORS.get(m.group(1).lower(), "bad_password")
    return "bad_password"


def guid_hex(value: Any) -> str | None:
    """objectGUID → 32 lower-case hex chars of the raw 16 bytes."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, bytes | bytearray) and len(value) == 16:
        return bytes(value).hex()
    if isinstance(value, str):
        s = value.strip().strip("{}").replace("-", "").lower()
        if re.fullmatch(r"[0-9a-f]{32}", s):
            return s
    return None


def parse_server(server: str, security: str) -> tuple[str, int]:
    """`host` | `host:port` | `[v6]:port` | `[v6]` → (host, port); default port by security."""
    default = 636 if security == "ldaps" else 389
    s = server.strip()
    if s.startswith("["):
        end = s.find("]")
        if end < 0:
            raise ValueError(f"invalid server {server!r}")
        host = s[1:end]
        rest = s[end + 1 :]
        if rest.startswith(":"):
            return host, _port(rest[1:], server)
        if rest:
            raise ValueError(f"invalid server {server!r}")
        return host, default
    if s.count(":") == 1:
        host, _, port = s.partition(":")
        return host, _port(port, server)
    if not s or s.count(":") > 1:
        if s.count(":") > 1:  # bare IPv6 without brackets
            return s, default
        raise ValueError(f"invalid server {server!r}")
    return s, default


def _port(text: str, server: str) -> int:
    try:
        p = int(text)
    except ValueError:
        raise ValueError(f"invalid port in {server!r}") from None
    if not 1 <= p <= 65535:
        raise ValueError(f"invalid port in {server!r}")
    return p


def server_label(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def text_attr(value: Any) -> str | None:
    v = first(value)
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    s = str(v)
    return s if s != "" else None
