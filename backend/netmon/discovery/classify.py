"""Device classification from SNMP identity, subtree probes and open ports (pure, no I/O).

Implements the ordered rule table of docs/data-model.md "Classification"."""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Any

from netmon.templates.builtin import suggest

ENTERPRISE_PREFIX = "1.3.6.1.4.1."
NET_SNMP_PREFIX = "1.3.6.1.4.1.8072.3.2."
FREEBSD_OID = "1.3.6.1.4.1.8072.3.2.8"

# probe name -> subtree OID (one GETNEXT with all four varbinds after the identity GET)
SUBTREE_PROBES: dict[str, str] = {
    "unifi_vap": "1.3.6.1.4.1.41112.1.6",
    "printer_mib": "1.3.6.1.2.1.43",
    "synology": "1.3.6.1.4.1.6574",
    "qnap": "1.3.6.1.4.1.24681",
}

# enterprise -> (vendor slug | None, plain default class for rule 10 | None)
VENDORS: dict[int, tuple[str | None, str | None]] = {
    9: ("cisco", "router"),
    11: ("hp", "switch"),
    171: ("d-link", "switch"),
    253: ("xerox", None),
    311: ("microsoft", None),
    318: ("apc", "unknown"),
    367: ("ricoh", None),
    641: ("lexmark", None),
    674: ("dell", "server"),
    890: ("zyxel", "switch"),
    1248: ("epson", None),
    1347: ("kyocera", None),
    1602: ("canon", None),
    2011: ("huawei", "router"),
    2435: ("brother", None),
    2636: ("juniper", "router"),
    4413: ("broadcom", "switch"),
    4526: ("netgear", "switch"),
    6574: ("synology", None),
    8072: (None, None),
    11863: ("tp-link", "switch"),
    12325: ("pfsense", None),
    12356: ("fortinet", "router"),
    14823: ("aruba", "access_point"),
    14988: ("mikrotik", "router"),
    24681: ("qnap", None),
    25461: ("paloalto", "router"),
    41112: ("ubiquiti", None),
}
PRINTER_ENTERPRISES = {2435, 1602, 1248, 253, 367, 1347, 641}
NET_SNMP_SUB = {
    "10": ("server", "linux"),
    "8": ("server", "freebsd"),
    "13": ("workstation", "microsoft"),
    "16": ("workstation", "apple"),
}

_I = re.IGNORECASE
RE_OPNSENSE = re.compile(r"opnsense", _I)
RE_OPN_STABLE = re.compile(r"stable/\d{2}\.\d", _I)
RE_PFSENSE = re.compile(r"pfsense", _I)
RE_UNIFI_MODEL = re.compile(r"\b(UAP|U6|U7|UAL|UAE)[-\w]*")  # case-sensitive by contract
RE_UNIFI_AP = re.compile(r"unifi.*(ap\b|access point)", _I)
RE_UBNT_SWITCH_ID = re.compile(r"\bUSW|\bUS-\d|unifi.*switch", _I)
RE_UBNT_SWITCH = re.compile(r"\bUSW|\bUS-\d|switch", _I)
RE_UBNT_ROUTER = re.compile(r"EdgeOS|EdgeRouter|UDM|UXG|USG|UniFiOS", _I)
RE_SYNOLOGY = re.compile(r"synology|diskstation", _I)
RE_SERVER = re.compile(r"server", _I)
RE_V_SWITCH = re.compile(
    r"switch|catalyst|procurve|nexus|jetstream|\bCRS\d|\bEX\d{4}|\bC(2|3|9)\d{3}|\b(GS|SG|XS)\d{3}",
    _I,
)
RE_V_AP = re.compile(r"\b(access point|aironet)\b|\bAP\d", _I)
RE_G_SWITCH = re.compile(r"switch", _I)
RE_G_ROUTER = re.compile(r"router|routeros|gateway", _I)
RE_G_PRINTER = re.compile(r"printer|jetdirect", _I)
RE_G_SERVER = re.compile(r"linux|unix|bsd", _I)
RE_G_WINDOWS = re.compile(r"windows", _I)


def enterprise_of(sys_object_id: str | None) -> int | None:
    oid = (sys_object_id or "").strip().lstrip(".")
    if not oid.startswith(ENTERPRISE_PREFIX):
        return None
    arc = oid[len(ENTERPRISE_PREFIX) :].split(".", 1)[0]
    return int(arc) if arc.isdigit() else None


def vendor_of(enterprise: int | None) -> str | None:
    if enterprise is None:
        return None
    return VENDORS.get(enterprise, (None, None))[0]


def is_freebsd(sys_object_id: str | None, sys_descr: str | None) -> bool:
    oid = (sys_object_id or "").strip().lstrip(".")
    return oid == FREEBSD_OID or (sys_descr or "").startswith("FreeBSD")


def classify(
    sys_object_id: str | None,
    sys_descr: str | None,
    sys_name: str | None,
    probes: Collection[str],
    pf: bool,
    open_ports: Collection[int],
    snmp_ok: bool,
) -> tuple[str, str | None]:
    """Returns (device_class, vendor). Rules are evaluated in order; the first match wins."""
    descr = sys_descr or ""
    name = sys_name or ""
    oid = (sys_object_id or "").strip().lstrip(".")
    ent = enterprise_of(oid)
    ports = set(open_ports)

    if snmp_ok:
        # 1 OPNsense
        if (
            RE_OPNSENSE.search(descr)
            or RE_OPNSENSE.search(name)
            or (descr.startswith("FreeBSD") and RE_OPN_STABLE.search(descr))
        ):
            return "router", "opnsense"
        # 2 pfSense
        if RE_PFSENSE.search(descr) or RE_PFSENSE.search(name) or ent == 12325:
            return "router", "pfsense"
        # 3 UniFi AP
        if "unifi_vap" in probes or RE_UNIFI_MODEL.search(descr) or RE_UNIFI_AP.search(descr):
            return "access_point", "ubiquiti"
        # 4 other Ubiquiti
        if ent == 41112 or RE_UBNT_SWITCH_ID.search(descr):
            if RE_UBNT_SWITCH.search(descr):
                return "switch", "ubiquiti"
            if RE_UBNT_ROUTER.search(descr):
                return "router", "ubiquiti"
            return "unknown", "ubiquiti"
        # 5 printers
        if (
            "printer_mib" in probes
            or ent in PRINTER_ENTERPRISES
            or oid.startswith("1.3.6.1.4.1.11.2.3.9.")
        ):
            return "printer", vendor_of(ent)
        # 6 Synology
        if "synology" in probes or ent == 6574 or RE_SYNOLOGY.search(descr):
            return "nas", "synology"
        # 7 QNAP
        if "qnap" in probes or ent == 24681:
            return "nas", "qnap"
        # 8 Microsoft
        if ent == 311:
            return ("server" if RE_SERVER.search(descr) else "workstation"), "microsoft"
        # 9 FreeBSD with pflog0
        if pf and is_freebsd(oid, descr):
            return "router", "freebsd"
        # 10 vendor table with a plain default class
        if ent is not None and ent in VENDORS and VENDORS[ent][1] is not None:
            slug, default_class = VENDORS[ent]
            if RE_V_SWITCH.search(descr):
                return "switch", slug
            if RE_V_AP.search(descr):
                return "access_point", slug
            return str(default_class), slug
        # 11 net-snmp
        if oid.startswith(NET_SNMP_PREFIX):
            sub = oid[len(NET_SNMP_PREFIX) :]
            return NET_SNMP_SUB.get(sub, ("server", None))
        # 12 any other SNMP answer
        vendor = vendor_of(ent)
        if RE_G_SWITCH.search(descr):
            return "switch", vendor
        if RE_G_ROUTER.search(descr):
            return "router", vendor
        if RE_G_PRINTER.search(descr):
            return "printer", vendor
        if RE_G_SERVER.search(descr):
            return "server", vendor
        if RE_G_WINDOWS.search(descr):
            return "workstation", vendor
        return "unknown", vendor

    # 13-18: port heuristics without SNMP
    if 9100 in ports or 631 in ports:
        return "printer", None
    if 3389 in ports:
        return ("server" if 53 in ports else "workstation"), None
    if 53 in ports and (80 in ports or 443 in ports):
        return "router", None
    if 22 in ports:
        return "server", None
    if 445 in ports:
        return "workstation", None
    return "unknown", None


def suggest_template(result: Any) -> str:
    """Template id for a classified result (an object or a dict with device_class, vendor,
    snmp_ok, open_ports)."""

    def get(key: str) -> Any:
        return result[key] if isinstance(result, dict) else getattr(result, key)

    return suggest(
        device_class=get("device_class"),
        vendor=get("vendor"),
        snmp_ok=bool(get("snmp_ok")),
        open_ports=list(get("open_ports") or []),
    )
