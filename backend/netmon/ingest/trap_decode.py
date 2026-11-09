"""[2b-3] Trap decoding: v1 / v2c messages with pysnmp's low-level `proto.api` (no engine),
varbind rendering, the RFC 3584 v1 -> v2 conversion, the summary text and the cleartext v3
header (architecture section 12.2.3).

Everything here is pure (no I/O); decoders raise `TrapDecodeError` with a drop-counter
reason, never anything else."""

from __future__ import annotations

import ipaddress
import string
from dataclasses import dataclass, field
from typing import Any

from netmon.ingest import oid_names
from netmon.ingest.limits import MAX_SUMMARY, MAX_VARBIND_VALUE, MAX_VARBINDS
from netmon.ingest.syslog_parse import REPLACEMENT

PRINTABLE = set(string.printable) - {"\x0b", "\x0c"}


class TrapDecodeError(Exception):
    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason  # parse_error | unsupported


@dataclass
class TrapRecord:
    version: str  # v1 | v2c | v3
    pdu: str  # trap | inform
    trap_oid: str
    uptime_ticks: int | None
    varbinds: list[dict[str, Any]]
    agent_addr: str | None = None
    security_name: str | None = None
    community: str | None = None  # never stored
    response: bytes | None = None  # v2c inform: the Response PDU to send back
    trap_name: str | None = None
    summary: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


TYPE_NAMES = {
    "Integer": "Integer",
    "Integer32": "Integer",
    "OctetString": "OctetString",
    "ObjectIdentifier": "ObjectIdentifier",
    "ObjectName": "ObjectIdentifier",
    "IpAddress": "IpAddress",
    "NetworkAddress": "IpAddress",
    "Counter": "Counter32",
    "Counter32": "Counter32",
    "Gauge": "Gauge32",
    "Gauge32": "Gauge32",
    "Unsigned32": "Gauge32",
    "TimeTicks": "TimeTicks",
    "Counter64": "Counter64",
    "Opaque": "Opaque",
    "Bits": "OctetString",
    "Null": "Null",
    "NoSuchObject": "noSuchObject",
    "NoSuchInstance": "noSuchInstance",
    "EndOfMibView": "endOfMibView",
}


def _no_nul(text: str) -> str:
    return text.replace("\x00", REPLACEMENT)


def octets_text(data: bytes) -> str:
    """OctetString: UTF-8 when it decodes and is printable, a 6-byte binary value as a MAC,
    else 0x + hex."""
    try:
        text = data.decode("utf-8")
        if all(c in PRINTABLE or (ord(c) > 0x7F and c.isprintable()) for c in text):
            return text
    except UnicodeDecodeError:
        pass
    if len(data) == 6:
        return ":".join(f"{b:02x}" for b in data)
    return "0x" + data.hex()


def render_value(value: Any) -> tuple[str, str]:
    """(SMI type name, raw value text) of a pyasn1 / pysnmp value."""
    cls = type(value).__name__
    # v1 NetworkAddress is a CHOICE holding an IpAddress
    if cls == "NetworkAddress":
        try:
            value = value.getComponent()
            cls = "IpAddress"
        except Exception:  # noqa: BLE001
            return "IpAddress", ""
    if cls == "ObjectSyntax" or cls in ("SimpleSyntax", "ApplicationSyntax"):
        try:
            return render_value(value.getComponent())
        except Exception:  # noqa: BLE001
            return "Null", ""
    type_name = TYPE_NAMES.get(cls, cls)
    try:
        if type_name in ("Null", "noSuchObject", "noSuchInstance", "endOfMibView"):
            return type_name, ""
        if type_name == "IpAddress":
            raw = bytes(value.asOctets())
            if len(raw) == 4:
                return type_name, str(ipaddress.IPv4Address(raw))
            return type_name, "0x" + raw.hex()
        if type_name == "ObjectIdentifier":
            return type_name, str(value)
        if type_name in ("OctetString", "Opaque"):
            return type_name, octets_text(bytes(value.asOctets()))
        if hasattr(value, "hasValue") and not value.hasValue():
            return type_name, ""
        return type_name, str(int(value))
    except Exception:  # noqa: BLE001 - odd encodings never break decoding
        try:
            return type_name, value.prettyPrint()
        except Exception:  # noqa: BLE001
            return type_name, ""


def varbind_dict(oid: str, type_name: str, value: str) -> dict[str, Any]:
    value = _no_nul(value)[:MAX_VARBIND_VALUE]
    return {
        "oid": oid,
        "name": oid_names.name(oid),
        "type": type_name,
        "value": value,
        "display": oid_names.enum_display(oid, value),
    }


def build_varbinds(pairs: list[tuple[Any, Any]]) -> list[dict[str, Any]]:
    out = []
    for oid, value in pairs[:MAX_VARBINDS]:
        type_name, text = render_value(value)
        out.append(varbind_dict(str(oid), type_name, text))
    return out


def split_header(
    varbinds: list[dict[str, Any]],
) -> tuple[int | None, str | None, list[dict[str, Any]]]:
    """v2c/v3: take sysUpTime.0 and snmpTrapOID.0 out of the varbind list."""
    uptime: int | None = None
    trap_oid: str | None = None
    rest = []
    for vb in varbinds:
        if vb["oid"] == oid_names.SYS_UPTIME_0 and uptime is None:
            try:
                uptime = int(vb["value"])
            except ValueError:
                uptime = None
            continue
        if vb["oid"] == oid_names.SNMP_TRAP_OID_0 and trap_oid is None:
            trap_oid = vb["value"]
            continue
        rest.append(vb)
    return uptime, trap_oid, rest


def v1_trap_oid(enterprise: str, generic: int, specific: int) -> str:
    """RFC 3584 section 3.1."""
    if 0 <= generic <= 5:
        return f"1.3.6.1.6.3.1.1.5.{generic + 1}"
    return f"{enterprise}.0.{specific}"


def finish(rec: TrapRecord, port_name: str | None = None) -> TrapRecord:
    rec.trap_oid = rec.trap_oid[:255]
    name = oid_names.name(rec.trap_oid)
    rec.trap_name = name[:128] if name else None
    rec.summary = summary(rec.trap_name, rec.trap_oid, rec.varbinds, port_name)
    return rec


def varbind_label(vb: dict[str, Any]) -> str:
    """Summary label: the column / scalar name without its instance (`ifIndex`), the full
    name for vendor subtrees, else the OID."""
    name = vb.get("name")
    if not name:
        return str(vb["oid"])
    base = name.split(".", 1)[0]
    if base in oid_names.VENDOR_ROOTS:
        return str(name)
    return base


def summary(
    trap_name: str | None, trap_oid: str, varbinds: list[dict[str, Any]], port: str | None
) -> str:
    parts = [trap_name or trap_oid]
    for vb in varbinds[:8]:
        parts.append(f"{varbind_label(vb)}={vb['display']}")
    if port:
        parts.append(f"port={port}")
    return " ".join(parts)[:MAX_SUMMARY]


def if_index_of(varbinds: list[dict[str, Any]]) -> int | None:
    """Value of the first ifIndex varbind (`1.3.6.1.2.1.2.2.1.1[.x]`)."""
    for vb in varbinds:
        if oid_names.under(vb["oid"], oid_names.IF_INDEX_COLUMN):
            try:
                return int(vb["value"])
            except ValueError:
                return None
    return None


def varbind_value(varbinds: list[dict[str, Any]], oid: str) -> str | None:
    for vb in varbinds:
        if oid_names.under(vb["oid"], oid):
            return str(vb["value"])
    return None


# ------------------------------------------------------------------ v1 / v2c
def message_version(data: bytes) -> int:
    from pysnmp.proto import api

    try:
        return int(api.decodeMessageVersion(data))
    except Exception as exc:  # noqa: BLE001
        raise TrapDecodeError("parse_error", f"not an SNMP message: {exc}") from None


def decode_v1v2c(data: bytes) -> TrapRecord:
    """A v1 Trap, v2c Trap or v2c InformRequest (with the Response to send back)."""
    from pyasn1.codec.ber import decoder, encoder
    from pysnmp.proto import api

    version = message_version(data)
    if version not in (api.SNMP_VERSION_1, api.SNMP_VERSION_2C):
        raise TrapDecodeError("unsupported", f"SNMP message version {version}")
    p_mod = api.PROTOCOL_MODULES[version]
    try:
        msg, _ = decoder.decode(data, asn1Spec=p_mod.Message())
        community = octets_text(bytes(p_mod.apiMessage.get_community(msg).asOctets()))
        pdu = p_mod.apiMessage.get_pdu(msg)
    except Exception as exc:  # noqa: BLE001
        raise TrapDecodeError("parse_error", f"malformed SNMP message: {exc}") from None
    try:
        if version == api.SNMP_VERSION_1:
            if not pdu.isSameTypeWith(p_mod.TrapPDU()):
                raise TrapDecodeError("unsupported", "not a trap PDU")
            t = p_mod.apiTrapPDU
            enterprise = str(t.get_enterprise(pdu))
            _, agent = render_value(t.get_agent_address(pdu))
            generic = int(t.get_generic_trap(pdu))
            specific = int(t.get_specific_trap(pdu))
            uptime = int(t.get_timestamp(pdu))
            varbinds = build_varbinds(list(t.get_varbinds(pdu)))
            if len(varbinds) < MAX_VARBINDS:
                varbinds.append(
                    varbind_dict(oid_names.SNMP_TRAP_ENTERPRISE_0, "ObjectIdentifier", enterprise)
                )
            return TrapRecord(
                "v1",
                "trap",
                v1_trap_oid(enterprise, generic, specific),
                uptime,
                varbinds,
                agent_addr=agent or None,
                community=community,
            )
        if pdu.isSameTypeWith(p_mod.SNMPv2TrapPDU()):
            kind = "trap"
        elif pdu.isSameTypeWith(p_mod.InformRequestPDU()):
            kind = "inform"
        else:
            raise TrapDecodeError("unsupported", "not a notification PDU")
        pairs = list(p_mod.apiPDU.get_varbinds(pdu))
        response = None
        if kind == "inform":
            rsp_msg = p_mod.apiMessage.get_response(msg)
            rsp_pdu = p_mod.apiMessage.get_pdu(rsp_msg)
            p_mod.apiPDU.set_varbinds(rsp_pdu, pairs)
            p_mod.apiPDU.set_error_status(rsp_pdu, 0)
            p_mod.apiPDU.set_error_index(rsp_pdu, 0)
            response = encoder.encode(rsp_msg)
        rec = from_v2_varbinds(build_varbinds(pairs), "v2c", kind, community=community)
        rec.response = response
        return rec
    except TrapDecodeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TrapDecodeError("parse_error", f"malformed trap PDU: {exc}") from None


def from_v2_varbinds(
    varbinds: list[dict[str, Any]],
    version: str,
    pdu: str,
    *,
    community: str | None = None,
    security_name: str | None = None,
) -> TrapRecord:
    uptime, trap_oid, rest = split_header(varbinds)
    if not trap_oid:
        raise TrapDecodeError("parse_error", "snmpTrapOID.0 missing")
    return TrapRecord(
        version,
        pdu,
        trap_oid,
        uptime,
        rest,
        community=community,
        security_name=security_name,
    )


# ------------------------------------------------------------------ v3
def parse_v3_header(data: bytes) -> tuple[bytes, str, int]:
    """(msgAuthoritativeEngineID, msgUserName, msgSecurityModel) from the cleartext header."""
    from pyasn1.codec.ber import decoder
    from pysnmp.proto.mpmod.rfc3412 import SNMPv3Message
    from pysnmp.proto.secmod.rfc3414.service import UsmSecurityParameters

    try:
        msg, _ = decoder.decode(data, asn1Spec=SNMPv3Message())
        model = int(msg["msgGlobalData"]["msgSecurityModel"])
        sp, _ = decoder.decode(
            bytes(msg["msgSecurityParameters"]), asn1Spec=UsmSecurityParameters()
        )
        engine_id = bytes(sp["msgAuthoritativeEngineId"].asOctets())
        user = bytes(sp["msgUserName"].asOctets()).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        raise TrapDecodeError("parse_error", f"malformed SNMPv3 message: {exc}") from None
    return engine_id, _no_nul(user), model
