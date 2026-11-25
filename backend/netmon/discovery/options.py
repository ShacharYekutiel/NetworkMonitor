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
    # [2b-1]
    "fingerprint": True,
    "fingerprint_timeout_ms": 2000,
    # [profiles] SNMP connection profiles tried after the group's credentials
    "snmp_profile_ids": [],
    # [profiles] opt-in SSH login check (owner decision Q3): SSH connection profiles and typed
    # SSH credentials (the [3] stored form, secrets encrypted); none = no SSH at all
    "ssh_profile_ids": [],
    "ssh_credentials": None,
}
MAX_SNMP_CREDENTIALS = 17  # 1 group + 8 profiles + 5 communities + 3 v3
MAX_SSH_CANDIDATES = 4  # 3 profiles + the typed credentials
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


def ssh_enabled(opts: dict[str, Any]) -> bool:
    """[profiles] SSH is attempted only with SSH profiles or typed SSH credentials."""
    return bool(opts.get("ssh_profile_ids")) or bool(opts.get("ssh_credentials"))


def public_options(opts: dict[str, Any]) -> dict[str, Any]:
    """The API `ScanOptions` shape: secrets replaced by counts and *_set flags."""
    full = merge_options(None, opts)
    ssh = full.get("ssh_credentials") if isinstance(full.get("ssh_credentials"), dict) else None
    ssh_set = opts.get("ssh_credentials_set")
    ssh_user = opts.get("ssh_username")
    if ssh_set is None:
        ssh_set = ssh is not None
        ssh_user = (ssh or {}).get("username")
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
        "fingerprint": bool(full["fingerprint"]),
        "fingerprint_timeout_ms": full["fingerprint_timeout_ms"],
        "snmp_profile_ids": [int(i) for i in full.get("snmp_profile_ids") or []],
        "ssh_profile_ids": [int(i) for i in full.get("ssh_profile_ids") or []],
        "ssh_credentials": None,  # write-only
        "ssh_credentials_set": bool(ssh_set),
        "ssh_username": ssh_user if ssh_set else None,
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
    ssh = full.get("ssh_credentials") if isinstance(full.get("ssh_credentials"), dict) else None
    full["ssh_credentials"] = (
        None  # [profiles] the candidates live in discovery_scans.ssh_candidates
    )
    full["ssh_credentials_set"] = ssh is not None
    full["ssh_username"] = (ssh or {}).get("username")
    return full


def build_credentials(
    group_creds: dict[str, Any],
    opts: dict[str, Any],
    group_profile_id: int | None = None,
    profiles: list[tuple[int, dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Ordered credential list (data-model "[profiles] Discovery"): 0) the group's effective
    credentials, then each SNMP connection profile of `snmp_profile_ids` in the given order
    (`profiles`: [(id, plaintext data)]; skipped when it is the profile that already supplied
    entry 0), then extra communities (v2c), then extra v3. All use snmp_port when set (else
    the credential's own port; extras the group's), timeout_s = snmp_timeout_s and
    retries = 0. Max 17. Each entry carries `_profile_id` and `_origin`
    (`group` | `profile` | `extra`)."""
    full = merge_options(None, opts)
    port = full["snmp_port"] or int(group_creds.get("port") or 161)
    timing = {"timeout_s": int(full["snmp_timeout_s"]), "retries": 0}
    common = {"port": int(port), **timing}
    out = [
        {
            **DEFAULT_SNMP,
            **group_creds,
            **common,
            "_profile_id": group_profile_id,
            "_origin": "group",
        }
    ]
    for pid, data in profiles or []:
        if pid == group_profile_id:
            continue
        own_port = full["snmp_port"] or int(data.get("port") or 161)
        out.append(
            {
                **DEFAULT_SNMP,
                **data,
                **timing,
                "port": int(own_port),
                "_profile_id": pid,
                "_origin": "profile",
            }
        )
    extra = {"_profile_id": None, "_origin": "extra"}
    for community in full["extra_communities"] or []:
        out.append({**DEFAULT_SNMP, "version": "v2c", "community": community, **common, **extra})
    for e in full["extra_v3"] or []:
        entry = {**DEFAULT_SNMP, "version": "v3", "community": None, **common, **extra}
        for k in V3_FIELDS:
            entry[k] = e.get(k)
        out.append(entry)
    return out[:MAX_SNMP_CREDENTIALS]


def probe_view(cred: dict[str, Any]) -> dict[str, Any]:
    """A credential entry without the snapshot keys (`_profile_id`, `_origin`)."""
    return {k: v for k, v in cred.items() if not k.startswith("_")}


def build_ssh_candidates(
    profiles: list[tuple[int, dict[str, Any]]], typed: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """[profiles] SSH login candidates in order: the SSH profiles of `ssh_profile_ids`, then
    the typed credentials. Each is the [3] stored form plus `_profile_id` and `_origin`
    (`profile` | `typed`). Max 4."""
    out = [{**data, "_profile_id": pid, "_origin": "profile"} for pid, data in profiles]
    if typed:
        out.append({**typed, "_profile_id": None, "_origin": "typed"})
    return out[:MAX_SSH_CANDIDATES]
