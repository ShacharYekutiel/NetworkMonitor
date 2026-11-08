"""Runtime settings stored in the `settings` table, with code defaults."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.flows.settings import DEFAULTS as FLOW_DEFAULTS
from netmon.models import Setting
from netmon.services.features import FEATURES
from netmon.services.settings_p3c import DEFAULTS as P3C_DEFAULTS

DEFAULTS: dict[str, Any] = {
    "smtp": {
        "host": "",
        "port": 587,
        "security": "starttls",
        "username": None,
        "password": None,
        "from_address": "",
        "from_name": "NetworkMonitor",
    },
    "retention_days": 90,
    "event_retention_days": 365,
    "default_interval_s": 60,
    "default_language": "en",
    "public_base_url": None,
    # [2b-1]; the timezone default is computed (TZ when valid, else UTC), see defaults()
    "timezone": None,
    "topology_refresh_min": 30,
    # [2b-2] switch ports view
    "port_inventory_min": 10,
    "uplink_mac_threshold": 5,
    "port_mac_retention_days": 30,
    "reverse_dns": True,
    # [2b-3] Active Directory (data-model § LDAP settings) and audit retention
    "ldap": {
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
    },
    "audit_retention_days": 365,
    # [2b-3] feature toggles (all OFF by default, also after an upgrade) and ingestion
    **{f"feature_{n}_enabled": False for n in FEATURES},
    "trap_retention_days": 30,
    "syslog_retention_days": 30,
    "trap_community_check": "any",
    "trap_communities": [],
    "trap_v3_users": [],
    "ingest_unknown_sources": "store",
    "ingest_ignored_sources": [],
    "flap_threshold": 5,
    "flap_window_min": 10,
    "flap_quiet_min": 15,
    "event_linkdown_scope": "important",
    "siem_targets": [],
    # [3] config backup (data-model § [3] Settings keys)
    "config_backup_time": "02:47",
    "config_backup_window_min": 60,
    "config_backup_concurrency": 4,
    "config_backup_timeout_s": 120,
    "config_backup_mask_secrets": True,
    "config_backup_max_versions": 100,
    "config_backup_min_versions": 10,
    "config_backup_retention_days": 365,
    "config_backup_fail_threshold": 2,
    "config_change_alert": False,
    "config_change_clear_min": 60,
    # [3] new-device alerts (newdev_learning_until: set by the first ON, read-only in the API)
    "newdev_learning_days": 7,
    "newdev_learning_until": None,
    "newdev_scope_group_ids": [],
    "newdev_ignore_random_macs": True,
    "newdev_condition_clear_min": 1440,
    "newdev_forget_after_days": 0,
    # [3] flow collector (netmon.flows.settings)
    **FLOW_DEFAULTS,
    # [3] SSO objects, reports, chat acknowledge (netmon.services.settings_p3c)
    **P3C_DEFAULTS,
}

# [3] plain keys written by PUT /api/settings as they come (validated by the schema mixins)
CONFIG_BACKUP_KEYS = (
    "config_backup_time",
    "config_backup_window_min",
    "config_backup_concurrency",
    "config_backup_timeout_s",
    "config_backup_mask_secrets",
    "config_backup_max_versions",
    "config_backup_min_versions",
    "config_backup_retention_days",
    "config_backup_fail_threshold",
    "config_change_alert",
    "config_change_clear_min",
)
NEWDEV_KEYS = (
    "newdev_learning_days",
    "newdev_scope_group_ids",
    "newdev_ignore_random_macs",
    "newdev_condition_clear_min",
    "newdev_forget_after_days",
)

MERGED_OBJECTS = ("smtp", "ldap", "kerberos", "oidc")  # stored objects merged over their defaults


def defaults() -> dict[str, Any]:
    from netmon.services.maintenance import default_timezone

    out = copy.deepcopy(DEFAULTS)
    out["timezone"] = default_timezone()
    return out


async def get_all(session: AsyncSession) -> dict[str, Any]:
    out = defaults()
    for row in (await session.execute(select(Setting))).scalars():
        if row.key in MERGED_OBJECTS and isinstance(row.value, dict):
            out[row.key] = {**out[row.key], **row.value}
        elif row.key in out:
            out[row.key] = row.value
    return out


async def get(session: AsyncSession, key: str) -> Any:
    row = await session.get(Setting, key)
    if row is None:
        return defaults()[key]
    if key in MERGED_OBJECTS and isinstance(row.value, dict):
        return {**copy.deepcopy(DEFAULTS[key]), **row.value}
    return row.value


async def put(session: AsyncSession, key: str, value: Any) -> None:
    stmt = insert(Setting).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(index_elements=[Setting.key], set_={"value": value})
    await session.execute(stmt)


async def ensure_defaults(session: AsyncSession) -> None:
    existing = set((await session.execute(select(Setting.key))).scalars())
    for key, value in defaults().items():
        if key not in existing:
            await put(session, key, copy.deepcopy(value))


def settings_out(values: dict[str, Any], secret_key: str | None = None) -> dict[str, Any]:
    """API shape. `secret_key` lets the [2b-3] `ldap` object report an unreadable stored bind
    password (`bind_password_unreadable`)."""
    from netmon.auth.ldap_config import ldap_out

    smtp = dict(values["smtp"])
    smtp["password_set"] = bool(smtp.get("password"))
    smtp["password"] = None
    return {
        "smtp": {
            k: smtp.get(k)
            for k in (
                "host",
                "port",
                "security",
                "username",
                "password",
                "password_set",
                "from_address",
                "from_name",
            )
        },
        "retention_days": values["retention_days"],
        "event_retention_days": values["event_retention_days"],
        "default_interval_s": values["default_interval_s"],
        "default_language": values["default_language"],
        "public_base_url": values["public_base_url"],
        "timezone": values["timezone"],
        "topology_refresh_min": values["topology_refresh_min"],
        "port_inventory_min": values["port_inventory_min"],
        "uplink_mac_threshold": values["uplink_mac_threshold"],
        "port_mac_retention_days": values["port_mac_retention_days"],
        "reverse_dns": values["reverse_dns"],
        # [2b-3]
        "ldap": ldap_out(values["ldap"], secret_key),
        "audit_retention_days": values["audit_retention_days"],
        **ingest_settings_out(values),
        # [3]
        **{k: values[k] for k in (*CONFIG_BACKUP_KEYS, *NEWDEV_KEYS)},
        **{k: values[k] for k in FLOW_DEFAULTS},  # [3] flows
        "newdev_learning_until": values.get("newdev_learning_until"),
        # [3] reports / chat ack (kerberos / oidc: GET /api/sso/*, never here)
        **{
            k: values[k]
            for k in ("report_retention_days", "chat_ack_ttl_h", "telegram_poll_timeout_s")
        },
    }


def ingest_settings_out(values: dict[str, Any]) -> dict[str, Any]:
    """[2b-3] feature toggles and ingestion keys (secrets write-only)."""
    out: dict[str, Any] = {
        f"feature_{n}_enabled": bool(values[f"feature_{n}_enabled"]) for n in FEATURES
    }
    users = [
        {
            "username": u.get("username"),
            "security_level": u.get("security_level"),
            "auth_protocol": u.get("auth_protocol"),
            "auth_key": None,
            "auth_key_set": bool(u.get("auth_key")),
            "priv_protocol": u.get("priv_protocol"),
            "priv_key": None,
            "priv_key_set": bool(u.get("priv_key")),
        }
        for u in values.get("trap_v3_users") or []
    ]
    out.update(
        {
            "trap_retention_days": values["trap_retention_days"],
            "syslog_retention_days": values["syslog_retention_days"],
            "trap_community_check": values["trap_community_check"],
            "trap_communities": None,
            "trap_communities_count": len(values.get("trap_communities") or []),
            "trap_v3_users": users,
            "ingest_unknown_sources": values["ingest_unknown_sources"],
            "ingest_ignored_sources": list(values.get("ingest_ignored_sources") or []),
            "flap_threshold": values["flap_threshold"],
            "flap_window_min": values["flap_window_min"],
            "flap_quiet_min": values["flap_quiet_min"],
            "event_linkdown_scope": values["event_linkdown_scope"],
            "siem_targets": list(values.get("siem_targets") or []),
        }
    )
    return out
