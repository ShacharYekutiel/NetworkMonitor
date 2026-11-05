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
    enterprises: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "device_classes": device_classes,
        "vendors": vendors,
        "requires_snmp": requires_snmp,
        "any_open_ports": any_open_ports,
        "enterprises": enterprises,
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
    exclude_if_types: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "kind": "snmp_interfaces",
        "oper_up_only": oper_up_only,
        "exclude_if_types": exclude_if_types or [SOFTWARE_LOOPBACK],
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
    "skip_zero": False,
    "filters": [],
    "interval_s": None,
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
    "skip_zero": False,
    "filters": [],
    "interval_s": None,
}


def _table(
    walk_oid: str,
    name: dict[str, str],
    config: dict[str, Any],
    *,
    label_oid: str | None = None,
    percent_of_column: str | None = None,
    channels: list[dict[str, Any]] | None = None,
    max_: int = 8,
    skip_zero: bool = False,
    filters: list[dict[str, Any]] | None = None,
    interval_s: int | None = None,
) -> dict[str, Any]:
    return {
        "kind": "snmp_table",
        "walk_oid": walk_oid,
        "label_oid": label_oid,
        "label2_oid": None,
        "percent_of_column": percent_of_column,
        "name": name,
        "config": config,
        "channels": channels or [],
        "max": max_,
        "skip_zero": skip_zero,
        "filters": filters or [],
        "interval_s": interval_s,
    }


def _filter(oid: str, in_: list[int] | None = None, not_in: list[int] | None = None) -> dict:
    return {"oid": oid, "in": in_, "not_in": not_in}


# ------------------------------------------------------------------ [2b-1] HPE switches
HPE_DESCRIPTION = _l(
    "Ping, system, CPU, memory, hardware health, PoE and active ports",
    "פינג, מערכת, מעבד, זיכרון, תקינות חומרה, PoE ופורטים פעילים",
)
HPE_IF_TYPES = [1, 24, 53, 131, 136]  # other, loopback, propVirtual/VLAN, tunnel, l3ipvlan
HPICF_SENSOR_MAP = {
    "1": {"status": "warning", "text": "unknown"},
    "2": {"status": "down", "text": "bad"},
    "3": {"status": "warning", "text": "warning"},
    "4": {"status": "up", "text": "good"},
}
HH3C_ERROR_MAP = {"2": {"status": "up", "text": "normal"}}
ENT_PHYSICAL_NAME = "1.3.6.1.2.1.47.1.1.1.1.7"
ENT_PHYSICAL_CLASS = "1.3.6.1.2.1.47.1.1.1.1.5"
HH3C_ENTITY = "1.3.6.1.4.1.25506.2.6.1.1.1.1"
HPICF_SENSOR = "1.3.6.1.4.1.11.2.14.11.1.2.6.1"
HP_LOCAL_MEM = "1.3.6.1.4.1.11.2.14.11.5.1.1.2.1.1.1"
PETH_PSE = "1.3.6.1.2.1.105.1.3.1.1"
PCT_GAUGE = {"value_type": "gauge", "unit": "%"}


def _poe_entries() -> list[dict[str, Any]]:
    present = [_filter(f"{PETH_PSE}.2", not_in=[0])]
    return [
        _table(
            f"{PETH_PSE}.4",
            _l("PoE {index} usage", "PoE {index} ניצול"),
            PCT_GAUGE,
            percent_of_column=f"{PETH_PSE}.2",
            channels=[{"key": "value", "warning_max": 80, "down_max": 95}],
            filters=present,
        ),
        _table(
            f"{PETH_PSE}.4",
            _l("PoE {index} power", "PoE {index} הספק"),
            {"value_type": "gauge", "unit": "W"},
            filters=present,
        ),
        _table(
            f"{PETH_PSE}.2",
            _l("PoE {index} budget", "PoE {index} תקציב"),
            {"value_type": "gauge", "unit": "W"},
            skip_zero=True,
            interval_s=3600,
        ),
    ]


HPE_ARUBAOS_ENTRIES: list[dict[str, Any]] = [
    PING,
    _sensor("snmp_system", _l("System", "מערכת")),
    _sensor(
        "snmp_custom",
        _l("CPU", "מעבד"),
        {
            "oid": "1.3.6.1.4.1.11.2.14.11.5.1.9.6.1.0",
            "value_type": "gauge",
            "unit": "%",
            "label": "CPU",
        },
        probe=True,
        channels=[{"key": "value", "warning_max": 80, "down_max": 95}],
    ),
    _table(
        f"{HP_LOCAL_MEM}.7",
        _l("Memory {index}", "זיכרון {index}"),
        PCT_GAUGE,
        percent_of_column=f"{HP_LOCAL_MEM}.5",
        channels=[{"key": "value", "warning_max": 85, "down_max": 95}],
    ),
    _table(
        f"{HPICF_SENSOR}.4",
        _l("{label}", "{label}"),
        {"value_type": "enum", "value_map": HPICF_SENSOR_MAP, "value_map_default": "warning"},
        label_oid=f"{HPICF_SENSOR}.7",
        filters=[_filter(f"{HPICF_SENSOR}.4", not_in=[5])],
        max_=16,
        interval_s=300,
    ),
    *_poe_entries(),
    _interfaces(52, exclude_if_types=HPE_IF_TYPES),
]

HPE_COMWARE_ENTRIES: list[dict[str, Any]] = [
    PING,
    _sensor("snmp_system", _l("System", "מערכת")),
    _table(
        f"{HH3C_ENTITY}.6",
        _l("CPU {label}", "מעבד {label}"),
        PCT_GAUGE,
        label_oid=ENT_PHYSICAL_NAME,
        channels=[{"key": "value", "warning_max": 80, "down_max": 95}],
        skip_zero=True,
    ),
    _table(
        f"{HH3C_ENTITY}.8",
        _l("Memory {label}", "זיכרון {label}"),
        PCT_GAUGE,
        label_oid=ENT_PHYSICAL_NAME,
        channels=[{"key": "value", "warning_max": 85, "down_max": 95}],
        skip_zero=True,
    ),
    _table(
        f"{HH3C_ENTITY}.12",
        _l("Temperature {label}", "טמפרטורה {label}"),
        {"value_type": "gauge", "unit": "°C"},
        label_oid=ENT_PHYSICAL_NAME,
        channels=[{"key": "value", "warning_max": 65, "down_max": 80}],
        skip_zero=True,
        max_=16,
    ),
    _table(
        f"{HH3C_ENTITY}.19",
        _l("{label}", "{label}"),
        {"value_type": "enum", "value_map": HH3C_ERROR_MAP, "value_map_default": "down"},
        label_oid=ENT_PHYSICAL_NAME,
        filters=[
            _filter(ENT_PHYSICAL_CLASS, in_=[6, 7]),
            _filter(f"{HH3C_ENTITY}.19", not_in=[1, 4]),
        ],
        max_=16,
        interval_s=300,
    ),
    *_poe_entries(),
    _interfaces(52, exclude_if_types=HPE_IF_TYPES),
]

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "hpe_arubaos_switch",
        "name": _l("HPE ArubaOS-Switch / ProCurve", "מתג HPE ArubaOS"),
        "description": HPE_DESCRIPTION,
        "requires_snmp": True,
        "priority": 50,
        "match": [_rule(device_classes=["switch"], requires_snmp=True, enterprises=[11])],
        "entries": HPE_ARUBAOS_ENTRIES,
    },
    {
        "id": "hpe_comware",
        "name": _l("HPE Comware / H3C switch", "מתג HPE Comware"),
        "description": HPE_DESCRIPTION,
        "requires_snmp": True,
        "priority": 50,
        "match": [_rule(device_classes=["switch"], requires_snmp=True, enterprises=[25506])],
        "entries": HPE_COMWARE_ENTRIES,
    },
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


def enterprise(sys_object_id: str | None) -> int | None:
    oid = (sys_object_id or "").strip().lstrip(".")
    prefix = "1.3.6.1.4.1."
    if not oid.startswith(prefix):
        return None
    arc = oid[len(prefix) :].split(".", 1)[0]
    return int(arc) if arc.isdigit() else None


def rule_matches(
    rule: dict[str, Any],
    *,
    device_class: str,
    vendor: str | None,
    snmp_ok: bool,
    open_ports: list[int],
    sys_object_id: str | None = None,
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
    return True


def template_matches(
    template: dict[str, Any],
    *,
    device_class: str,
    vendor: str | None,
    snmp_ok: bool,
    open_ports: list[int],
    sys_object_id: str | None = None,
) -> bool:
    return any(
        rule_matches(
            r,
            device_class=device_class,
            vendor=vendor,
            snmp_ok=snmp_ok,
            open_ports=open_ports,
            sys_object_id=sys_object_id,
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
) -> str:
    """The matching template with the highest priority (generic_ping always matches)."""
    for t in TEMPLATES:  # sorted by priority desc (stable: list order breaks ties)
        if template_matches(
            t,
            device_class=device_class,
            vendor=vendor,
            snmp_ok=snmp_ok,
            open_ports=open_ports,
            sys_object_id=sys_object_id,
        ):
            return str(t["id"])
    return "generic_ping"
