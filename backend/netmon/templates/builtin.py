"""Built-in device templates (docs/data-model.md "Device templates"). Pure data, no I/O."""

from __future__ import annotations

import copy
from typing import Any

IF_EXCLUDE = (
    r"^(lo\d*|pflog\d*|pfsync\d*|enc\d*|gif\d*|gre\d*|ovpn[cs]?\d*|wg\d+|tun\d*|tap\d*|ipfw\d*"
    r"|docker\d*|veth.*|virbr.*|br-.*|dummy\d*|sit\d*|ifb\d*|imq\d*|teql\d*|ip6tnl\d*|wifi\d+"
    r"|ath\d+|ra\d+|rai\d+)$"
)
SOFTWARE_LOOPBACK = 24

SKIP_REASONS = ("exists", "probe_failed", "snmp_error", "max_reached", "no_rows")


def _l(en: str, he: str) -> dict[str, str]:
    return {"en": en, "he": he}


def _rule(
    *,
    device_classes: list[str] | None = None,
    vendors: list[str] | None = None,
    requires_snmp: bool = False,
    any_open_ports: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "device_classes": device_classes,
        "vendors": vendors,
        "requires_snmp": requires_snmp,
        "any_open_ports": any_open_ports,
    }


def _sensor(
    type_: str,
    name: dict[str, str],
    config: dict[str, Any] | None = None,
    *,
    probe: bool = False,
    interval_s: int | None = None,
    channels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "kind": "sensor",
        "type": type_,
        "name": name,
        "config": config or {},
        "channels": channels or [],
        "interval_s": interval_s,
        "probe": probe,
    }


def _interfaces(
    max_: int,
    *,
    include: str | None = None,
    exclude: str | None = IF_EXCLUDE,
    oper_up_only: bool = True,
) -> dict[str, Any]:
    return {
        "kind": "snmp_interfaces",
        "oper_up_only": oper_up_only,
        "exclude_if_types": [SOFTWARE_LOOPBACK],
        "include_name_regex": include,
        "exclude_name_regex": exclude,
        "max": max_,
        "channels": [],
    }


def _disk(descr: str) -> dict[str, Any]:
    return _sensor(
        "snmp_disk", _l(f"Disk {descr}", f"דיסק {descr}"), {"storage_descr": descr}, probe=True
    )


PING = _sensor("ping", _l("Ping", "פינג"))
SYSTEM = _sensor("snmp_system", _l("System", "מערכת"))
CPU = _sensor("snmp_cpu", _l("CPU Load", "עומס מעבד"), probe=True)
MEMORY = _sensor("snmp_memory", _l("Memory", "זיכרון"), probe=True)

PRINTER_SUPPLIES = {
    "kind": "snmp_table",
    "walk_oid": "1.3.6.1.2.1.43.11.1.1.9",
    "label_oid": "1.3.6.1.2.1.43.11.1.1.6",
    "label2_oid": None,
    "percent_of_column": "1.3.6.1.2.1.43.11.1.1.8",
    "name": _l("Supply {label}", "חומר מתכלה {label}"),
    "config": {"value_type": "gauge", "unit": "%"},
    "channels": [{"key": "value", "warning_min": 15, "down_min": 5}],
    "max": 8,
}
PAGE_COUNTER = _sensor(
    "snmp_custom",
    _l("Page counter", "מונה דפים"),
    {
        "oid": "1.3.6.1.2.1.43.10.2.1.4.1.1",
        "value_type": "gauge",
        "unit": "pages",
        "label": "Pages",
    },
    probe=True,
    interval_s=3600,
)
UNIFI_CLIENTS = {
    "kind": "snmp_table",
    "walk_oid": "1.3.6.1.4.1.41112.1.6.1.2.1.8",
    "label_oid": "1.3.6.1.4.1.41112.1.6.1.2.1.6",
    "label2_oid": "1.3.6.1.4.1.41112.1.6.1.2.1.9",
    "percent_of_column": None,
    "name": _l("Clients {label}", "לקוחות {label}"),
    "config": {"value_type": "gauge", "unit": "clients"},
    "channels": [],
    "max": 16,
}

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "opnsense",
        "name": _l("OPNsense / pfSense firewall", "חומת אש OPNsense / pfSense"),
        "description": _l(
            "Ping, system, CPU, memory, root disk, active interfaces and the web GUI",
            "פינג, מערכת, מעבד, זיכרון, דיסק שורש, ממשקים פעילים וממשק הניהול",
        ),
        "requires_snmp": True,
        "priority": 90,
        "match": [
            _rule(vendors=["opnsense", "pfsense"], requires_snmp=True),
            _rule(device_classes=["router"], vendors=["freebsd"], requires_snmp=True),
        ],
        "entries": [
            PING,
            SYSTEM,
            CPU,
            MEMORY,
            _disk("/"),
            _interfaces(16),
            _sensor(
                "http",
                _l("Web GUI", "ממשק ניהול"),
                {"url": "https://{host}/", "verify_tls": False},
                probe=True,
            ),
        ],
    },
    {
        "id": "unifi_ap",
        "name": _l("UniFi access point", "נקודת גישה UniFi"),
        "description": _l(
            "Ping, system, CPU, memory, uplink port and clients per SSID/radio",
            "פינג, מערכת, מעבד, זיכרון, פורט Uplink ולקוחות לכל SSID/רדיו",
        ),
        "requires_snmp": True,
        "priority": 80,
        "match": [_rule(device_classes=["access_point"], vendors=["ubiquiti"], requires_snmp=True)],
        "entries": [
            PING,
            SYSTEM,
            CPU,
            MEMORY,
            _interfaces(1, include="^eth0$", exclude=None),
            UNIFI_CLIENTS,
        ],
    },
    {
        "id": "printer",
        "name": _l("Printer", "מדפסת"),
        "description": _l(
            "Ping, system, toner/supply levels, page counter and the web page",
            "פינג, מערכת, רמות טונר וחומרים מתכלים, מונה דפים ודף האינטרנט",
        ),
        "requires_snmp": True,
        "priority": 60,
        "match": [_rule(device_classes=["printer"], requires_snmp=True)],
        "entries": [
            PING,
            SYSTEM,
            PRINTER_SUPPLIES,
            PAGE_COUNTER,
            _sensor("http", _l("HTTP", "HTTP"), {"url": "http://{host}/"}, probe=True),
        ],
    },
    {
        "id": "nas_server",
        "name": _l("Server / NAS", "שרת / NAS"),
        "description": _l(
            "Ping, system, CPU, memory, disks (/, /volume1, C:\\) and active interfaces",
            "פינג, מערכת, מעבד, זיכרון, דיסקים (/, /volume1, C:\\) וממשקים פעילים",
        ),
        "requires_snmp": True,
        "priority": 40,
        "match": [_rule(device_classes=["server", "nas", "workstation"], requires_snmp=True)],
        "entries": [
            PING,
            SYSTEM,
            CPU,
            MEMORY,
            _disk("/"),
            _disk("/volume1"),
            _disk("C:\\"),
            _interfaces(8),
        ],
    },
    {
        "id": "generic_snmp",
        "name": _l("Generic SNMP device", "התקן SNMP כללי"),
        "description": _l(
            "Ping, system, CPU, memory and active interfaces",
            "פינג, מערכת, מעבד, זיכרון וממשקים פעילים",
        ),
        "requires_snmp": True,
        "priority": 20,
        "match": [_rule(requires_snmp=True)],
        "entries": [PING, SYSTEM, CPU, MEMORY, _interfaces(32)],
    },
    {
        "id": "http_server",
        "name": _l("Web server", "שרת אינטרנט"),
        "description": _l(
            "Ping and HTTP/HTTPS checks on ports 80, 443, 8080 and 8443",
            "פינג ובדיקות HTTP/HTTPS בפורטים 80, 443, 8080 ו-8443",
        ),
        "requires_snmp": False,
        "priority": 10,
        "match": [_rule(any_open_ports=[80, 443, 8080, 8443])],
        "entries": [
            PING,
            _sensor("http", _l("HTTP", "HTTP"), {"url": "http://{host}/"}, probe=True),
            _sensor(
                "http",
                _l("HTTPS", "HTTPS"),
                {"url": "https://{host}/", "verify_tls": False},
                probe=True,
            ),
            _sensor(
                "http", _l("HTTP 8080", "HTTP 8080"), {"url": "http://{host}:8080/"}, probe=True
            ),
            _sensor(
                "http",
                _l("HTTPS 8443", "HTTPS 8443"),
                {"url": "https://{host}:8443/", "verify_tls": False},
                probe=True,
            ),
        ],
    },
    {
        "id": "generic_ping",
        "name": _l("Ping only", "פינג בלבד"),
        "description": _l("A single ping sensor", "חיישן פינג יחיד"),
        "requires_snmp": False,
        "priority": 0,
        "match": [_rule()],
        "entries": [PING],
    },
]

TEMPLATES.sort(key=lambda t: -t["priority"])
BY_ID: dict[str, dict[str, Any]] = {t["id"]: t for t in TEMPLATES}
TEMPLATE_IDS = frozenset(BY_ID)


def list_templates() -> list[dict[str, Any]]:
    return copy.deepcopy(TEMPLATES)


def get_template(template_id: str) -> dict[str, Any] | None:
    t = BY_ID.get(template_id)
    return copy.deepcopy(t) if t is not None else None


def rule_matches(
    rule: dict[str, Any],
    *,
    device_class: str,
    vendor: str | None,
    snmp_ok: bool,
    open_ports: list[int],
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
    return True


def template_matches(
    template: dict[str, Any],
    *,
    device_class: str,
    vendor: str | None,
    snmp_ok: bool,
    open_ports: list[int],
) -> bool:
    return any(
        rule_matches(
            r, device_class=device_class, vendor=vendor, snmp_ok=snmp_ok, open_ports=open_ports
        )
        for r in template["match"]
    )


def suggest(*, device_class: str, vendor: str | None, snmp_ok: bool, open_ports: list[int]) -> str:
    """The matching template with the highest priority (generic_ping always matches)."""
    for t in TEMPLATES:  # sorted by priority desc
        if template_matches(
            t, device_class=device_class, vendor=vendor, snmp_ok=snmp_ok, open_ports=open_ports
        ):
            return str(t["id"])
    return "generic_ping"
