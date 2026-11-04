"""Scan options: defaults, merge, secret-free views and the credential list (pure)."""

from __future__ import annotations

import copy
from typing import Any

from netmon.poller.snmp_client import DEFAULT_SNMP

DEFAULT_TCP_PORTS = [22, 23, 53, 80, 443, 445, 631, 3389, 8080, 8443, 9100]
DEFAULT_OPTIONS: dict[str, Any] = {
    "ping": True,
    "ping_timeout_ms": 1000,
    "snmp": True,
    "snmp_port": None,
    "snmp_timeout_s": 1,
    "extra_communities": [],
    "extra_v3": [],
    "tcp": True,
    "tcp_ports": DEFAULT_TCP_PORTS,
    "tcp_timeout_ms": 1000,
    "dns": True,
}
V3_FIELDS = (
    "v3_username",
    "v3_security_level",
    "v3_auth_protocol",
    "v3_auth_key",
    "v3_priv_protocol",
    "v3_priv_key",
)
SECRET_LISTS = ("extra_communities", "extra_v3")


def defaults() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_OPTIONS)


def merge_options(
    incoming: dict[str, Any] | None, base: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Field-by-field merge of the given keys over `base` (or the defaults). For the two
    secret lists, a missing key or null keeps the stored list."""
    out = defaults()
    if base:
        out.update({k: copy.deepcopy(v) for k, v in base.items() if k in DEFAULT_OPTIONS})
    for key, value in (incoming or {}).items():
        if key not in DEFAULT_OPTIONS:
            continue
        if key in SECRET_LISTS and value is None:
            continue
        out[key] = copy.deepcopy(value)
    return out


def _v3_out(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "v3_username": entry.get("v3_username"),
        "v3_security_level": entry.get("v3_security_level"),
        "v3_auth_protocol": entry.get("v3_auth_protocol"),
        "v3_auth_key": None,
        "v3_auth_key_set": bool(entry.get("v3_auth_key") or entry.get("v3_auth_key_set")),
        "v3_priv_protocol": entry.get("v3_priv_protocol"),
        "v3_priv_key": None,
        "v3_priv_key_set": bool(entry.get("v3_priv_key") or entry.get("v3_priv_key_set")),
    }


def public_options(opts: dict[str, Any]) -> dict[str, Any]:
    """The API `ScanOptions` shape: secrets replaced by counts and *_set flags."""
    full = merge_options(None, opts)
    comm_count = opts.get("extra_communities_count")
    if comm_count is None:
        comm_count = len(full["extra_communities"] or [])
    v3 = [_v3_out(e) for e in (full["extra_v3"] or [])]
    return {
        "ping": bool(full["ping"]),
        "ping_timeout_ms": full["ping_timeout_ms"],
        "snmp": bool(full["snmp"]),
        "snmp_port": full["snmp_port"],
        "snmp_timeout_s": full["snmp_timeout_s"],
        "extra_communities": None,
        "extra_communities_count": int(comm_count),
        "extra_v3": v3,
        "extra_v3_count": len(v3),
        "tcp": bool(full["tcp"]),
        "tcp_ports": list(full["tcp_ports"] or []),
        "tcp_timeout_ms": full["tcp_timeout_ms"],
        "dns": bool(full["dns"]),
    }


def scan_snapshot(opts: dict[str, Any]) -> dict[str, Any]:
    """What `discovery_scans.options` stores: secrets removed, plus the counts."""
    full = merge_options(None, opts)
    v3 = []
    for e in full["extra_v3"] or []:
        v3.append(
            {
                "v3_username": e.get("v3_username"),
                "v3_security_level": e.get("v3_security_level"),
                "v3_auth_protocol": e.get("v3_auth_protocol"),
                "v3_auth_key": None,
                "v3_auth_key_set": bool(e.get("v3_auth_key")),
                "v3_priv_protocol": e.get("v3_priv_protocol"),
                "v3_priv_key": None,
                "v3_priv_key_set": bool(e.get("v3_priv_key")),
            }
        )
    full["extra_communities_count"] = len(full["extra_communities"] or [])
    full["extra_v3_count"] = len(v3)
    full["extra_communities"] = []
    full["extra_v3"] = v3
    return full


def build_credentials(group_creds: dict[str, Any], opts: dict[str, Any]) -> list[dict[str, Any]]:
    """Ordered credential list: group effective credentials, extra communities (v2c), extra v3.
    All use snmp_port when set, timeout_s = snmp_timeout_s and retries = 0. Max 9."""
    full = merge_options(None, opts)
    port = full["snmp_port"] or int(group_creds.get("port") or 161)
    common = {"port": int(port), "timeout_s": int(full["snmp_timeout_s"]), "retries": 0}
    out = [{**DEFAULT_SNMP, **group_creds, **common}]
    for community in full["extra_communities"] or []:
        out.append({**DEFAULT_SNMP, "version": "v2c", "community": community, **common})
    for e in full["extra_v3"] or []:
        entry = {**DEFAULT_SNMP, "version": "v3", "community": None, **common}
        for k in V3_FIELDS:
            entry[k] = e.get(k)
        out.append(entry)
    return out[:9]
