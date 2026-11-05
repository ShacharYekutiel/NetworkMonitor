"""Device classification from SNMP identity, subtree probes and open ports (pure, no I/O).

Implements the ordered rule table of docs/data-model.md "Classification"."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from netmon.templates.builtin import suggest

ENTERPRISE_PREFIX = "1.3.6.1.4.1."
NET_SNMP_PREFIX = "1.3.6.1.4.1.8072.3.2."
FREEBSD_OID = "1.3.6.1.4.1.8072.3.2.8"

# probe name -> subtree OID (one GETNEXT with all five varbinds after the identity GET)
SUBTREE_PROBES: dict[str, str] = {
    "unifi_vap": "1.3.6.1.4.1.41112.1.6",
    "printer_mib": "1.3.6.1.2.1.43",
    "synology": "1.3.6.1.4.1.6574",
    "qnap": "1.3.6.1.4.1.24681",
    "checkpoint": "1.3.6.1.4.1.2620.1.6",  # [2b-2] svn MIB (also behind the net-snmp Linux OID)
}

# enterprise -> (vendor slug | None, plain default class for rule 10 | None)
VENDORS: dict[int, tuple[str | None, str | None]] = {
    9: ("cisco", "router"),
    11: ("hp", "switch"),
    171: ("d-link", "switch"),
    232: ("hp", "server"),  # [2b-2] Compaq/HPE server management (iLO, Insight agents)
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
    2620: ("checkpoint", "router"),  # [2b-2] Check Point (Gaia)
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
    25506: ("h3c", "switch"),  # [2b-1] H3C / HPE Comware
    41112: ("ubiquiti", None),
    47196: ("aruba", "switch"),  # [2b-1] Aruba (HPE) AOS-CX
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
    r"switch|catalyst|procurve|nexus|jetstream|\bCRS\d|\bEX\d{4}|\bC(2|3|9)\d{3}|\b(GS|SG|XS)\d{3}"
    r"|officeconnect|comware|arubaos|aruba.*switch",
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


def classify_rule(
    sys_object_id: str | None,
    sys_descr: str | None,
    sys_name: str | None,
    probes: Collection[str],
    pf: bool,
    open_ports: Collection[int],
    snmp_ok: bool,
) -> tuple[str, str | None, int]:
    """Returns (device_class, vendor, rule number). Rules are evaluated in order; the first
    match wins."""
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
            return "router", "opnsense", 1
        # 2 pfSense
        if RE_PFSENSE.search(descr) or RE_PFSENSE.search(name) or ent == 12325:
            return "router", "pfsense", 2
        # 3 UniFi AP
        if "unifi_vap" in probes or RE_UNIFI_MODEL.search(descr) or RE_UNIFI_AP.search(descr):
            return "access_point", "ubiquiti", 3
        # 4 other Ubiquiti
        if ent == 41112 or RE_UBNT_SWITCH_ID.search(descr):
            if RE_UBNT_SWITCH.search(descr):
                return "switch", "ubiquiti", 4
            if RE_UBNT_ROUTER.search(descr):
                return "router", "ubiquiti", 4
            return "unknown", "ubiquiti", 4
        # 5 printers
        if (
            "printer_mib" in probes
            or ent in PRINTER_ENTERPRISES
            or oid.startswith("1.3.6.1.4.1.11.2.3.9.")
        ):
            return "printer", vendor_of(ent), 5
        # 6 Synology
        if "synology" in probes or ent == 6574 or RE_SYNOLOGY.search(descr):
            return "nas", "synology", 6
        # 7 QNAP
        if "qnap" in probes or ent == 24681:
            return "nas", "qnap", 7
        # 8 Microsoft
        if ent == 311:
            return ("server" if RE_SERVER.search(descr) else "workstation"), "microsoft", 8
        # 9 FreeBSD with pflog0
        if pf and is_freebsd(oid, descr):
            return "router", "freebsd", 9
        # [2b-2] 9a Check Point (enterprise or the svn subtree probe); reported as rule 9
        if ent == 2620 or "checkpoint" in probes:
            return "router", "checkpoint", 9
        # 10 vendor table with a plain default class
        if ent is not None and ent in VENDORS and VENDORS[ent][1] is not None:
            slug, default_class = VENDORS[ent]
            if RE_V_SWITCH.search(descr):
                return "switch", slug, 10
            if RE_V_AP.search(descr):
                return "access_point", slug, 10
            return str(default_class), slug, 10
        # 11 net-snmp
        if oid.startswith(NET_SNMP_PREFIX):
            sub = oid[len(NET_SNMP_PREFIX) :]
            cls, vendor = NET_SNMP_SUB.get(sub, ("server", None))
            return cls, vendor, 11
        # 12 any other SNMP answer
        vendor = vendor_of(ent)
        if RE_G_SWITCH.search(descr):
            return "switch", vendor, 12
        if RE_G_ROUTER.search(descr):
            return "router", vendor, 12
        if RE_G_PRINTER.search(descr):
            return "printer", vendor, 12
        if RE_G_SERVER.search(descr):
            return "server", vendor, 12
        if RE_G_WINDOWS.search(descr):
            return "workstation", vendor, 12
        return "unknown", vendor, 12

    # 13-18: port heuristics without SNMP
    if 9100 in ports or 631 in ports:
        return "printer", None, 13
    if 3389 in ports:
        return ("server" if 53 in ports else "workstation"), None, 14
    if 53 in ports and (80 in ports or 443 in ports):
        return "router", None, 15
    if 22 in ports:
        return "server", None, 16
    if 445 in ports:
        return "workstation", None, 17
    return "unknown", None, 18


def classify(
    sys_object_id: str | None,
    sys_descr: str | None,
    sys_name: str | None,
    probes: Collection[str],
    pf: bool,
    open_ports: Collection[int],
    snmp_ok: bool,
) -> tuple[str, str | None]:
    """Returns (device_class, vendor) (rules 1-18)."""
    cls, vendor, _ = classify_rule(
        sys_object_id, sys_descr, sys_name, probes, pf, open_ports, snmp_ok
    )
    return cls, vendor


def suggest_template(result: Any) -> str:
    """Template id for a classified result (an object or a dict with device_class, vendor,
    snmp_ok, open_ports and optionally sys_object_id and sys_descr)."""

    def get(key: str) -> Any:
        if isinstance(result, dict):
            return result.get(key)
        return getattr(result, key, None)

    return suggest(
        device_class=get("device_class"),
        vendor=get("vendor"),
        snmp_ok=bool(get("snmp_ok")),
        open_ports=list(get("open_ports") or []),
        sys_object_id=get("sys_object_id"),
        sys_descr=get("sys_descr"),
    )


# ------------------------------------------------------------------ [2b-1] fingerprint rules
@dataclass(frozen=True)
class FpMatch:
    device_class: str
    vendor: str | None
    confidence: str
    rule: str


@dataclass(frozen=True)
class Classification:
    device_class: str
    vendor: str | None
    model: str | None
    confidence: str
    classified_by: str


class _Fp:
    """Accessors over a fingerprint: T titles, S servers, R realms, M markers, B ssh banner,
    N telnet banner (docs/data-model.md "Fingerprint classification rules")."""

    def __init__(self, fp: dict[str, Any]) -> None:
        http = [h for h in (fp.get("http") or []) if isinstance(h, dict)]
        self.T = [str(h["title"]) for h in http if h.get("title")]
        self.S = [str(h["server"]) for h in http if h.get("server")]
        self.R = [str(h["realm"]) for h in http if h.get("realm")]
        self.M = sorted({str(m) for h in http for m in (h.get("markers") or [])})
        ssh = fp.get("ssh") or {}
        telnet = fp.get("telnet") or {}
        self.B = str(ssh.get("banner") or "") if isinstance(ssh, dict) else ""
        self.N = str(telnet.get("banner") or "") if isinstance(telnet, dict) else ""

    @staticmethod
    def _any(pattern: str, values: list[str]) -> bool:
        rx = re.compile(pattern, re.IGNORECASE)
        return any(rx.search(v) for v in values)

    def t(self, pattern: str) -> bool:
        return self._any(pattern, self.T)

    def m(self, pattern: str) -> bool:
        return self._any(pattern, self.M)

    def tm(self, pattern: str) -> bool:
        return self.t(pattern) or self.m(pattern)

    def s(self, pattern: str) -> bool:
        return self._any(pattern, self.S)

    def b(self, pattern: str) -> bool:
        return bool(self.B) and re.search(pattern, self.B, re.IGNORECASE) is not None


def classify_fingerprint(fp: dict[str, Any] | None) -> FpMatch | None:
    """Rules F1-F29, first match wins (pure)."""
    if not fp:
        return None
    f = _Fp(fp)
    if f.tm(r"opnsense"):
        return FpMatch("router", "opnsense", "high", "F1")
    if f.tm(r"pfsense"):
        return FpMatch("router", "pfsense", "high", "F2")
    if f.t(r"RouterOS|mikrotik|webfig") or f.B.startswith("SSH-2.0-ROSSSH"):
        return FpMatch("router", "mikrotik", "high", "F3")
    if f.t(r"UniFi OS"):
        return FpMatch("router", "ubiquiti", "medium", "F4")
    if f.t(r"UniFi Network"):
        return FpMatch("server", "ubiquiti", "medium", "F5")
    if f.tm(r"edgeos|edgerouter"):
        return FpMatch("router", "ubiquiti", "medium", "F6")
    if f.tm(r"edgeswitch"):
        return FpMatch("switch", "ubiquiti", "medium", "F6")
    if f.tm(r"airos"):
        return FpMatch("access_point", "ubiquiti", "medium", "F6")
    if f.m(r"unifi|ubiquiti"):
        return FpMatch("unknown", "ubiquiti", "low", "F6")
    if f.tm(r"TL-SG\d+\w*E\b|easy smart"):
        return FpMatch("switch", "tp-link", "medium", "F7")
    if f.t(r"\bTL-SG|\bT\d{4}|jetstream"):
        return FpMatch("switch", "tp-link", "medium", "F8")
    if f.tm(r"omada|\bEAP\d"):
        return FpMatch("access_point", "tp-link", "medium", "F8")
    if f.m(r"tp-link|tplink"):
        return FpMatch("unknown", "tp-link", "low", "F8")
    if f.b(r"Comware"):
        return FpMatch("switch", "h3c", "high", "F9")
    if f.tm(r"comware|h3c"):
        return FpMatch("switch", "h3c", "medium", "F10")
    if f.tm(r"officeconnect|procurve|arubaos|aruba.*switch|hpe.*switch"):
        return FpMatch("switch", "hp", "medium", "F10")
    if f.s(r"^eHTTP"):
        return FpMatch("switch", "hp", "medium", "F11")
    if f.tm(r"\bilo\b|integrated lights-out"):
        return FpMatch("server", "hp", "high", "F12")
    if f.tm(r"idrac"):
        return FpMatch("server", "dell", "high", "F13")
    if f.tm(r"supermicro|megarac") or f.t(r"\bIPMI\b"):
        vendor = "supermicro" if f.tm(r"supermicro") else None
        return FpMatch("server", vendor, "medium", "F14")
    if f.tm(r"synology|diskstation"):
        return FpMatch("nas", "synology", "high", "F15")
    if f.tm(r"qnap|\bQTS\b"):
        return FpMatch("nas", "qnap", "medium", "F16")
    if f.tm(r"laserjet|officejet|pagewide|designjet"):
        return FpMatch("printer", "hp", "high", "F17")
    if f.s(r"HP HTTP Server|HP-ChaiSOE"):
        return FpMatch("printer", "hp", "medium", "F17")
    if f.tm(r"brother"):
        return FpMatch("printer", "brother", "high", "F18")
    if f.s(r"^debut"):
        return FpMatch("printer", "brother", "medium", "F18")
    if f.tm(r"canon") or f.s(r"CANON HTTP Server|KS_HTTP"):
        return FpMatch("printer", "canon", "medium", "F19")
    if f.tm(r"kyocera|command center") or f.s(r"KM-MFP-http"):
        return FpMatch("printer", "kyocera", "medium", "F20")
    if f.tm(r"ricoh|web image monitor"):
        return FpMatch("printer", "ricoh", "medium", "F21")
    for word in ("epson", "xerox", "lexmark"):
        if f.tm(word):
            return FpMatch("printer", word, "medium", "F22")
    if f.tm(r"fortigate|fortinet") or f.s(r"^xxxxxxxx-xxxxx$"):
        return FpMatch("router", "fortinet", "medium", "F23")
    if f.tm(r"check point|gaia portal|\bgaia\b"):  # [2b-2]
        return FpMatch("router", "checkpoint", "medium", "F23a")
    if f.b(r"Cisco-\d") or f.s(r"cisco") or f.t(r"cisco"):
        cls = "switch" if f.t(r"\bSG\d{3}|\bCBS\d{3}|catalyst|switch") else "router"
        return FpMatch(cls, "cisco", "medium", "F24")
    if f.tm(r"netgear|prosafe"):
        if f.t(r"\b(GS|XS|MS)\d{3}"):
            return FpMatch("switch", "netgear", "medium", "F25")
        return FpMatch("unknown", "netgear", "low", "F25")
    if f.tm(r"d-link|dlink"):
        if f.t(r"\b(DGS|DES|DXS)-"):
            return FpMatch("switch", "d-link", "medium", "F26")
        return FpMatch("unknown", "d-link", "low", "F26")
    if f.tm(r"zyxel"):
        if f.t(r"\b(GS|XGS|XS)\d{3,4}"):
            return FpMatch("switch", "zyxel", "medium", "F27")
        return FpMatch("unknown", "zyxel", "low", "F27")
    if f.s(r"Microsoft-IIS"):
        return FpMatch("server", "microsoft", "low", "F28")
    if f.b(r"OpenSSH.*(Ubuntu|Debian|Raspbian)"):
        return FpMatch("server", "linux", "low", "F29")
    return None


MODEL_PATTERNS = [
    re.compile(r"\b(TL-[A-Z]{2,5}\d{2,5}[A-Z]{0,3})\b"),
    re.compile(r"\b(J[L]?\d{3,4}A)\b"),
    re.compile(r"OfficeConnect\s+(?:Switch\s+)?(\d{4}S?)", re.IGNORECASE),
    re.compile(r"\b((?:DS|RS)\d{3,4}\+?)(?!\w)"),
    re.compile(r"\b((?:GS|XS|MS)\d{3}[A-Z]{0,4})\b"),
    re.compile(r"\b((?:DGS|DES|DXS)-\d{4}[-\w]*)"),
    re.compile(r"\b(U[67]-[\w-]+|UAP-[\w-]+)"),
]


def extract_model(fp: dict[str, Any] | None) -> str | None:
    """First model pattern match over the HTTP titles, then the realms, then the telnet
    banner (<= 64 chars, else None)."""
    if not fp:
        return None
    f = _Fp(fp)
    for text in [*f.T, *f.R, *([f.N] if f.N else [])]:
        for rx in MODEL_PATTERNS:
            m = rx.search(text)
            if m:
                model = m.group(1)
                return model if len(model) <= 64 else None
    return None


def classify_host(
    sys_object_id: str | None,
    sys_descr: str | None,
    sys_name: str | None,
    probes: Collection[str],
    pf: bool,
    open_ports: Collection[int],
    snmp_ok: bool,
    fingerprint: dict[str, Any] | None = None,
) -> Classification:
    """SNMP rules 1-12, then fingerprint rules F1-F29 (when SNMP did not answer or ended in
    `unknown`; the SNMP vendor is kept when the fingerprint gives none), then port rules."""
    model = extract_model(fingerprint)
    cls, vendor, rule = classify_rule(
        sys_object_id, sys_descr, sys_name, probes, pf, open_ports, snmp_ok
    )
    if snmp_ok:
        conf = "high" if rule <= 10 else "medium"
        if cls != "unknown":
            return Classification(cls, vendor, model, conf, "snmp")
        fm = classify_fingerprint(fingerprint)
        if fm is not None:
            return Classification(
                fm.device_class, fm.vendor or vendor, model, fm.confidence, "fingerprint"
            )
        return Classification(cls, vendor, model, conf, "snmp")
    fm = classify_fingerprint(fingerprint)
    if fm is not None:
        return Classification(fm.device_class, fm.vendor, model, fm.confidence, "fingerprint")
    return Classification(cls, vendor, model, "low", "ports" if rule <= 17 else "none")
