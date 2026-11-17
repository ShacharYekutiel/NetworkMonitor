"""[premium] What is connected to a switch port: `connected_kind` / `connected_kind_source`
(docs/data-model.md "[premium] Connected device kind"). Pure; no SNMP, no I/O.

Rules, first match wins:
1. a manual override (`port_kind_overrides`)                         -> source `manual`
2. the primary neighbour (LLDP / CDP / manual topology link):
   a managed device -> its effective class; an unmanaged neighbour -> its LLDP / CDP system
   capabilities, then its CDP platform / LLDP sysDescr text            -> source = link source
3. a neighbour that is a managed device of unknown class on a LAG / LAG member / trunk
   -> `switch`                                                         -> source = link source
4. the MAC OUI of the single learned MAC -> vendor -> kind (VENDOR_KINDS, PREFIX_KINDS)
5. one learned MAC without a match -> `computer`; several learned MACs (also an uplink by MAC
   count) and no neighbour -> `phone` / `server` when one of them is a phone / hypervisor MAC,
   else `unknown`                                                       -> source `oui`
A port whose link is not up, or up with nothing learned behind it, is null / null (an override
is still returned: the user's label stays visible on a port that is down).

The OUI -> kind tables are HEURISTIC: a hand-curated list of vendors whose registered blocks are
dominated by one kind of product (printers, cameras, IP phones, access points,
hypervisors / servers). Vendor names are matched against the bundled IEEE MA-L listing
(`netmon/data/oui.tsv.gz`, see `netmon.ports.oui`); a few blocks registered under a mixed
vendor name (Aruba blocks now listed as Hewlett Packard Enterprise, Hyper-V, VirtualBox) are
listed by prefix. Mixed vendors (Cisco, HP / HPE, Dell, Lenovo, Apple, Samsung, ...) are left
out on purpose: their MACs fall through to `computer`. Nothing is downloaded at runtime."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

KINDS = ("computer", "phone", "ap", "printer", "camera", "server", "switch", "router", "unknown")
SOURCES = ("lldp", "cdp", "oui", "manual")

# effective device class of a managed neighbour -> kind (None: not decisive, go on)
CLASS_KINDS: dict[str, str] = {
    "switch": "switch",
    "router": "router",
    "access_point": "ap",
    "printer": "printer",
    "server": "server",
    "nas": "server",
    "workstation": "computer",
}

# LLDP / CDP capability names (netmon.topology.collect) by priority: a phone also announces
# `bridge` (its PC port), an AP `bridge` (and sometimes `router`), an L3 switch `bridge` +
# `router`.
CAP_KINDS: tuple[tuple[str, str], ...] = (
    ("telephone", "phone"),
    ("wlan_ap", "ap"),
    ("bridge", "switch"),
    ("router", "router"),
    ("docsis", "router"),
    ("station", "computer"),
)

# CDP platform / LLDP sysDescr text of an unmanaged neighbour without capabilities
_TEXT_KINDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bIP Phone\b|\bphone\b|\bSEP[0-9A-F]{12}\b|\bCP-\d{4}", re.I), "phone"),
    (re.compile(r"\bAIR-|\baccess point\b|\bwireless ap\b|\bUAP\b|\bU6-", re.I), "ap"),
    (re.compile(r"\bprinter\b|\bLaserJet\b|\bOfficejet\b", re.I), "printer"),
    (re.compile(r"\bcamera\b|\bIPC\b|\bNVR\b", re.I), "camera"),
    (re.compile(r"\bVMware ESXi?\b|\bHyper-V\b|\bProxmox\b", re.I), "server"),
)

# Vendor name (IEEE MA-L organisation, case-insensitive, anchored at the start) -> kind.
VENDOR_KINDS: tuple[tuple[str, str], ...] = (
    # printers
    (r"brother industries", "printer"),
    (r"canon inc", "printer"),
    (r"seiko epson", "printer"),
    (r"lexmark", "printer"),
    (r"xerox corp", "printer"),
    (r"ricoh company", "printer"),
    (r"kyocera corporation", "printer"),
    (r"konica minolta", "printer"),
    (r"zebra technologies", "printer"),
    (r"toshiba tec", "printer"),
    (r"fujifilm business innovation", "printer"),
    (r"zhuhai pantum", "printer"),
    # cameras
    (r"axis communications", "camera"),
    (r"hangzhou hikvision", "camera"),
    (r"prama hikvision", "camera"),
    (r"zhejiang dahua", "camera"),
    (r"zhejiang uniview", "camera"),
    (r"hanwha vision", "camera"),
    (r"vivotek", "camera"),
    (r"mobotix", "camera"),
    (r"avigilon", "camera"),
    (r"bosch security systems", "camera"),
    (r"reolink", "camera"),
    (r"xiamen milesight", "camera"),
    # IP phones
    (r"yealink", "phone"),
    (r"xiamen yealink", "phone"),
    (r"polycom", "phone"),
    (r"grandstream", "phone"),
    (r"snom technology", "phone"),
    (r"avaya", "phone"),
    (r"mitel", "phone"),
    (r"fanvil", "phone"),
    (r"gigaset", "phone"),
    (r"spectralink", "phone"),
    # access points
    (r"ubiquiti", "ap"),
    (r"ruckus wireless", "ap"),
    (r"cambium networks", "ap"),
    (r"cisco meraki", "ap"),
    (r"mist systems", "ap"),
    (r"engenius", "ap"),
    # servers / hypervisors (virtual machine MACs)
    (r"vmware", "server"),
    (r"super micro computer", "server"),
    (r"nutanix", "server"),
    (r"xensource", "server"),
    (r"proxmox server solutions", "server"),
    (r"parallels", "server"),
    (r"pcs systemtechnik", "server"),  # VirtualBox 08:00:27
)
_VENDOR_RES = tuple((re.compile("^" + p, re.I), k) for p, k in VENDOR_KINDS)

# Blocks registered under a mixed vendor name, by prefix (upper-case hex, 6 digits).
PREFIX_KINDS: dict[str, str] = {
    "00155D": "server",  # Microsoft Hyper-V virtual machines
    # Aruba Networks blocks (listed as Hewlett Packard Enterprise in the IEEE MA-L listing)
    "000B86": "ap",
    "001A1E": "ap",
    "00246C": "ap",
    "04BD88": "ap",
    "186472": "ap",
    "204C03": "ap",
    "24DEC6": "ap",
    "40E3D6": "ap",
    "6CF37F": "ap",
    "703A0E": "ap",
    "84D47E": "ap",
    "94B40F": "ap",
    "9C1C12": "ap",
    "ACA31E": "ap",
    "B45D50": "ap",
    "D8C7C8": "ap",
    "F05C19": "ap",
}

# several MACs on a port: these kinds win over `unknown` (a phone with a PC behind it; a
# hypervisor host with its virtual machines)
_MULTI_WINNERS = ("phone", "server")


def _prefix(mac: str | None) -> str:
    return "".join(ch for ch in (mac or "") if ch.isalnum()).upper()[:6]


def vendor_kind(mac: str | None, vendor: str | None) -> str | None:
    """Kind of a MAC from its OUI (prefix table first, then the vendor name), else None."""
    hit = PREFIX_KINDS.get(_prefix(mac))
    if hit:
        return hit
    if vendor:
        for rx, kind in _VENDOR_RES:
            if rx.search(vendor.strip()):
                return kind
    return None


def capability_kind(caps: Iterable[str] | None) -> str | None:
    have = set(caps or ())
    for cap, kind in CAP_KINDS:
        if cap in have:
            return kind
    return None


def text_kind(text: str | None) -> str | None:
    if not text:
        return None
    for rx, kind in _TEXT_KINDS:
        if rx.search(text):
            return kind
    return None


def neighbor_kind(
    primary: dict[str, Any] | None,
    nb_info: dict[str, Any] | None,
    lag_or_trunk: bool,
) -> str | None:
    """Rules 2 and 3 for the primary PortNeighbor (`nb_info`: the topology_neighbors row of an
    unmanaged neighbour: capabilities, sys_descr)."""
    if primary is None:
        return None
    if primary.get("kind") == "device":
        kind = CLASS_KINDS.get(str(primary.get("device_class") or "unknown"))
        if kind is not None:
            return kind
        return "switch" if lag_or_trunk else None
    info = nb_info or {}
    return capability_kind(info.get("capabilities")) or text_kind(info.get("sys_descr"))


def source_of(primary: dict[str, Any]) -> str:
    src = str(primary.get("source") or "")
    return src if src in ("lldp", "cdp") else "manual"


def derive(
    *,
    state: str,
    override: str | None,
    primary: dict[str, Any] | None,
    nb_info: dict[str, Any] | None,
    lag_or_trunk: bool,
    mac_count: int,
    macs: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """(connected_kind, connected_kind_source) of one port. `macs`: the present PortMac dicts
    of the port (with `mac` and `vendor`; empty for an uplink, whose MACs are not loaded);
    `mac_count`: the number of learned MACs (also for an uplink)."""
    if override is not None:
        return override, "manual"
    if state != "up":
        return None, None
    kind = neighbor_kind(primary, nb_info, lag_or_trunk)
    if kind is not None and primary is not None:
        return kind, source_of(primary)
    count = max(int(mac_count), len(macs))
    if count == 0:
        return None, None
    if count == 1 and len(macs) == 1:
        return vendor_kind(macs[0].get("mac"), macs[0].get("vendor")) or "computer", "oui"
    kinds = {vendor_kind(m.get("mac"), m.get("vendor")) for m in macs}
    for k in _MULTI_WINNERS:
        if k in kinds:
            return k, "oui"
    return "unknown", "oui"
