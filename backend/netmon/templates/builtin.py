"""Device templates: pure match helpers over the current template registry.

[2b-2] The template data lives in YAML files (`netmon/templates/library/<vendor>/<id>.yaml` plus
the user directory), loaded by `netmon.templates.registry`. `TEMPLATES` / `BY_ID` are views of
the current registry, updated in place on every (re)load. At import, the built-in library is
loaded (package files only) so pure code and unit tests work without a runtime."""

from __future__ import annotations

import copy
import re
from typing import Any

from netmon.templates import registry
from netmon.templates.library import IF_EXCLUDE, SOFTWARE_LOOPBACK
from netmon.templates.registry import BY_ID, TEMPLATES

__all__ = [
    "BY_ID",
    "IF_EXCLUDE",
    "SKIP_REASONS",
    "SOFTWARE_LOOPBACK",
    "TEMPLATES",
    "enterprise",
    "get_template",
    "list_templates",
    "rule_matches",
    "suggest",
    "template_matches",
]

SKIP_REASONS = ("exists", "probe_failed", "snmp_error", "max_reached", "no_rows", "not_applicable")

if not TEMPLATES:
    registry.load_builtin()


def list_templates() -> list[dict[str, Any]]:
    return copy.deepcopy(registry.current().templates)


def get_template(template_id: str) -> dict[str, Any] | None:
    t = registry.current().by_id.get(template_id)
    return copy.deepcopy(t) if t is not None else None


def enterprise(sys_object_id: str | None) -> int | None:
    oid = (sys_object_id or "").strip().lstrip(".")
    prefix = "1.3.6.1.4.1."
    if not oid.startswith(prefix):
        return None
    arc = oid[len(prefix) :].split(".", 1)[0]
    return int(arc) if arc.isdigit() else None


def oid_has_prefix(sys_object_id: str | None, prefix: str) -> bool:
    oid = (sys_object_id or "").strip().lstrip(".")
    p = prefix.strip().lstrip(".")
    return bool(oid) and (oid == p or oid.startswith(p + "."))


def rule_matches(
    rule: dict[str, Any],
    *,
    device_class: str,
    vendor: str | None,
    snmp_ok: bool,
    open_ports: list[int],
    sys_object_id: str | None = None,
    sys_descr: str | None = None,
) -> bool:
    if rule.get("requires_snmp") and not snmp_ok:
        return False
    if rule.get("device_classes") is not None and device_class not in rule["device_classes"]:
        return False
    if rule.get("vendors") is not None and vendor not in rule["vendors"]:
        return False
    ports = rule.get("any_open_ports")
    if ports is not None and not any(p in open_ports for p in ports):
        return False
    ents = rule.get("enterprises")
    if ents is not None and enterprise(sys_object_id) not in ents:
        return False
    prefixes = rule.get("sys_object_id_prefixes")
    if prefixes is not None and not any(oid_has_prefix(sys_object_id, p) for p in prefixes):
        return False
    rx = rule.get("sys_descr_regex")
    if rx is not None and re.search(rx, sys_descr or "", re.IGNORECASE) is None:
        return False
    return True


def template_matches(
    template: dict[str, Any],
    *,
    device_class: str,
    vendor: str | None,
    snmp_ok: bool,
    open_ports: list[int],
    sys_object_id: str | None = None,
    sys_descr: str | None = None,
) -> bool:
    return any(
        rule_matches(
            r,
            device_class=device_class,
            vendor=vendor,
            snmp_ok=snmp_ok,
            open_ports=open_ports,
            sys_object_id=sys_object_id,
            sys_descr=sys_descr,
        )
        for r in template["match"]
    )


def suggest(
    *,
    device_class: str,
    vendor: str | None,
    snmp_ok: bool,
    open_ports: list[int],
    sys_object_id: str | None = None,
    sys_descr: str | None = None,
) -> str:
    """The matching template with the highest priority (ties: lowest id; generic_ping always
    matches)."""
    for t in registry.current().templates:
        if template_matches(
            t,
            device_class=device_class,
            vendor=vendor,
            snmp_ok=snmp_ok,
            open_ports=open_ports,
            sys_object_id=sys_object_id,
            sys_descr=sys_descr,
        ):
            return str(t["id"])
    return "generic_ping"
