"""[2b-3] Built-in event rules (data-model "Built-in rules"; seeded disabled by migration 0006)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

LINK_DOWN = "1.3.6.1.6.3.1.1.5.3"
LINK_UP = "1.3.6.1.6.3.1.1.5.4"

BUILTIN_RULES: dict[str, dict[str, Any]] = {
    "trap_link_down": {
        "names": {"en": "Link down", "he": "קישור נפל"},
        "kind": "trap",
        "priority": 1000,
        "match": {
            "trap_oids": [LINK_DOWN],
            "varbinds": [{"oid": "1.3.6.1.2.1.2.2.1.7", "op": "ne", "value": "2"}],
            "ports": "important",
        },
        "action": "warning",
        "key_varbind_oid": "1.3.6.1.2.1.2.2.1.1",
        "key_regex": None,
        "clear_match": {"trap_oids": [LINK_UP], "varbinds": [], "ports": "any"},
        "clear_after_min": 1440,
    },
    "trap_auth_failure": {
        "names": {"en": "SNMP authentication failure", "he": "כשל אימות SNMP"},
        "kind": "trap",
        "priority": 1010,
        "match": {"trap_oids": ["1.3.6.1.6.3.1.1.5.5"], "varbinds": [], "ports": "any"},
        "action": "warning",
        "key_varbind_oid": None,
        "key_regex": None,
        "clear_match": None,
        "clear_after_min": 60,
    },
    "trap_restart": {
        "names": {"en": "Device restarted", "he": "ההתקן הופעל מחדש"},
        "kind": "trap",
        "priority": 1020,
        "match": {
            "trap_oids": ["1.3.6.1.6.3.1.1.5.1", "1.3.6.1.6.3.1.1.5.2"],
            "varbinds": [],
            "ports": "any",
        },
        "action": "warning",
        "key_varbind_oid": None,
        "key_regex": None,
        "clear_match": None,
        "clear_after_min": 30,
    },
    "syslog_error": {
        "names": {"en": "Syslog error or worse", "he": "שגיאת Syslog ומעלה"},
        "kind": "syslog",
        "priority": 1030,
        "match": {
            "severity_max": 3,
            "facilities": [],
            "programs": [],
            "text": None,
            "text_regex": False,
        },
        "action": "warning",
        "key_varbind_oid": None,
        "key_regex": None,
        "clear_match": None,
        "clear_after_min": 30,
    },
    # [3] config-change triggers (data-model "Built-in config-change rules"; seeded by 0007)
    "syslog_comware_cfg_changed": {
        "names": {"en": "Comware configuration changed", "he": "שינוי תצורה Comware"},
        "kind": "syslog",
        "priority": 2000,
        "match": {
            "severity_max": None,
            "facilities": [],
            "programs": ["CFGMAN"],
            "text": None,
            "text_regex": False,
        },
        "action": "config_backup",
        "key_varbind_oid": None,
        "key_regex": None,
        "clear_match": None,
        "clear_after_min": None,
    },
    "syslog_arubaos_cfg_changed": {
        "names": {"en": "ArubaOS-Switch configuration changed", "he": "שינוי תצורה ArubaOS"},
        "kind": "syslog",
        "priority": 2010,
        "match": {
            "severity_max": None,
            "facilities": [],
            "programs": ["mgr"],
            "text": "(running|startup) config(uration)? (file )?changed",
            "text_regex": True,
        },
        "action": "config_backup",
        "key_varbind_oid": None,
        "key_regex": None,
        "clear_match": None,
        "clear_after_min": None,
    },
    "syslog_cisco_config_i": {
        "names": {"en": "Cisco IOS configured", "he": "שינוי תצורה Cisco"},
        "kind": "syslog",
        "priority": 2020,
        "match": {
            "severity_max": None,
            "facilities": [],
            "programs": [],
            "text": "%SYS-5-CONFIG_I",
            "text_regex": False,
        },
        "action": "config_backup",
        "key_varbind_oid": None,
        "key_regex": None,
        "clear_match": None,
        "clear_after_min": None,
    },
}
BACKUP_BUILTIN_KEYS = (
    "syslog_comware_cfg_changed",
    "syslog_arubaos_cfg_changed",
    "syslog_cisco_config_i",
)


def builtin_values(key: str, language: str = "en") -> dict[str, Any]:
    d = BUILTIN_RULES[key]
    return {
        "name": d["names"].get(language) or d["names"]["en"],
        "kind": d["kind"],
        "priority": d["priority"],
        "scope": "global",
        "group_id": None,
        "device_id": None,
        "match": d["match"],
        "action": d["action"],
        "key_varbind_oid": d["key_varbind_oid"],
        "key_regex": d["key_regex"],
        "clear_match": d["clear_match"],
        "clear_after_min": d["clear_after_min"],
    }


async def ensure_builtin_rules(session: Any, language: str = "en") -> int:
    """Insert missing built-in rules (disabled). Returns the number inserted."""
    n = 0
    for key in BUILTIN_RULES:
        v = builtin_values(key, language)
        res = await session.execute(
            text(
                "INSERT INTO event_rules (name, kind, enabled, builtin_key, priority, scope, "
                "match, "
                "action, key_varbind_oid, key_regex, clear_match, clear_after_min) VALUES (:name, "
                ":kind, false, :bk, :priority, 'global', CAST(:match AS jsonb), :action, :kvo, "
                ":kr, CAST(:cm AS jsonb), :cam) ON CONFLICT (builtin_key) DO NOTHING"
            ),
            {
                "name": v["name"],
                "kind": v["kind"],
                "bk": key,
                "priority": v["priority"],
                "match": json.dumps(v["match"]),
                "action": v["action"],
                "kvo": v["key_varbind_oid"],
                "kr": v["key_regex"],
                "cm": json.dumps(v["clear_match"]) if v["clear_match"] is not None else None,
                "cam": v["clear_after_min"],
            },
        )
        n += res.rowcount or 0
    return n
