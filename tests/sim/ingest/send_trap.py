#!/usr/bin/env python3
"""SNMP trap / inform sender for the 2b-3 ingest tests (docs/architecture.md §12.14). No root needed:
the sending socket is bound to a loopback address (Linux routes all of 127/8 to lo), so 127.0.0.81 can
"be" a switch.

    python tests/sim/ingest/send_trap.py --to 127.0.0.1:1162 --from 127.0.0.81 --version v2c \
        --oid 1.3.6.1.6.3.1.1.5.3 --varbind 1.3.6.1.2.1.2.2.1.1.5=i:5 --varbind 1.3.6.1.2.1.2.2.1.7.5=i:1 \
        --varbind 1.3.6.1.2.1.2.2.1.8.5=i:2
    ... --version v1 [--agent-addr 10.0.0.5] [--enterprise 1.3.6.1.4.1.11]    (v1 PDU with agent-addr)
    ... --version v2c --inform                (waits for the Response PDU; exit 3 when none arrives)
    ... --version v3 --user netmon --auth SHA --auth-key authpass1 --priv AES128 --priv-key privpass1 \
        --engine-id 0x8000000001020304       (the sender is the authoritative engine)

Varbind values use net-snmp type letters: i Integer32, u Gauge32, c Counter32, C Counter64, t TimeTicks,
a IpAddress, o OID, s OctetString, x hex OctetString, n Null.
v1 conversion: --oid 1.3.6.1.6.3.1.1.5.<g+1> (g 0..5) -> generic trap g with --enterprise (default
1.3.6.1.4.1.8072.3.2.10); any other --oid E.0.S -> enterprise E, enterpriseSpecific (6), specific S.
--repeat N --interval S sends N times. Prints one JSON line per sent PDU (request id, bytes).

Importable: `build_v1(...)`, `build_v2c(...)`, `send_udp(...)`, `send_v3(...)` (async).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import socket
import sys
import time

from pyasn1.codec.ber import decoder, encoder
from pysnmp.proto import api
from pysnmp.proto import rfc1902

AUTH = {
    "none": "usmNoAuthProtocol", "MD5": "usmHMACMD5AuthProtocol", "SHA": "usmHMACSHAAuthProtocol",
    "SHA224": "usmHMAC128SHA224AuthProtocol", "SHA256": "usmHMAC192SHA256AuthProtocol",
    "SHA384": "usmHMAC256SHA384AuthProtocol", "SHA512": "usmHMAC384SHA512AuthProtocol",
}
PRIV = {
    "none": "usmNoPrivProtocol", "DES": "usmDESPrivProtocol", "3DES": "usm3DESEDEPrivProtocol",
    "AES128": "usmAesCfb128Protocol", "AES": "usmAesCfb128Protocol", "AES192": "usmAesCfb192Protocol",
    "AES256": "usmAesCfb256Protocol",
}
GENERIC_PREFIX = "1.3.6.1.6.3.1.1.5."
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
TRAP_OID = "1.3.6.1.6.3.1.1.4.1.0"


def parse_value(spec: str):
    """`i:5` -> Integer32(5) etc."""
    if ":" not in spec:
        raise ValueError(f"varbind value needs TYPE:VALUE, got {spec!r}")
    t, v = spec.split(":", 1)
    if t == "i":
        return rfc1902.Integer32(int(v))
    if t == "u":
        return rfc1902.Gauge32(int(v))
    if t == "c":
        return rfc1902.Counter32(int(v))
    if t == "C":
        return rfc1902.Counter64(int(v))
    if t == "t":
        return rfc1902.TimeTicks(int(v))
    if t == "a":
        return rfc1902.IpAddress(v)
    if t == "o":
        return rfc1902.ObjectName(v)
    if t == "s":
        return rfc1902.OctetString(v.encode())
    if t == "x":
        return rfc1902.OctetString(hexValue=v.replace(":", "").replace(" ", "").removeprefix("0x"))
    if t == "n":
        from pyasn1.type.univ import Null

        return Null("")
    raise ValueError(f"unknown varbind type {t!r}")


def parse_varbinds(items: list[str]) -> list[tuple[str, object]]:
    out = []
    for it in items:
        oid, val = it.split("=", 1)
        out.append((oid.strip(), parse_value(val.strip())))
    return out


def v1_fields(trap_oid: str, enterprise: str | None) -> tuple[str, int, int]:
    """(enterprise, generic, specific) for a v2 trap OID (RFC 3584 § 3.2 reverse mapping)."""
    if trap_oid.startswith(GENERIC_PREFIX) and trap_oid[len(GENERIC_PREFIX):].isdigit():
        g = int(trap_oid[len(GENERIC_PREFIX):]) - 1
        if 0 <= g <= 5:
            return enterprise or "1.3.6.1.4.1.8072.3.2.10", g, 0
    parts = trap_oid.split(".")
    if len(parts) >= 3 and parts[-2] == "0":
        return ".".join(parts[:-2]), 6, int(parts[-1])
    return ".".join(parts[:-1]), 6, int(parts[-1])


def build_v1(community: str, trap_oid: str, varbinds: list[tuple[str, object]], agent_addr: str,
             uptime: int = 12345, enterprise: str | None = None) -> bytes:
    p = api.PROTOCOL_MODULES[api.SNMP_VERSION_1]
    pdu = p.TrapPDU()
    p.apiTrapPDU.set_defaults(pdu)
    ent, gen, spec = v1_fields(trap_oid, enterprise)
    p.apiTrapPDU.set_enterprise(pdu, ent)
    p.apiTrapPDU.set_agent_address(pdu, p.IpAddress(agent_addr))
    p.apiTrapPDU.set_generic_trap(pdu, gen)
    p.apiTrapPDU.set_specific_trap(pdu, spec)
    p.apiTrapPDU.set_timestamp(pdu, uptime)
    p.apiTrapPDU.set_varbinds(pdu, [(rfc1902.ObjectName(o), v) for o, v in varbinds])
    msg = p.Message()
    p.apiMessage.set_defaults(msg)
    p.apiMessage.set_community(msg, community)
    p.apiMessage.set_pdu(msg, pdu)
    return encoder.encode(msg)


def build_v2c(community: str, trap_oid: str, varbinds: list[tuple[str, object]], uptime: int = 12345,
              inform: bool = False, request_id: int | None = None) -> tuple[bytes, int]:
    p = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
    pdu = p.InformRequestPDU() if inform else p.SNMPv2TrapPDU()
    p.apiPDU.set_defaults(pdu)
    rid = request_id if request_id is not None else random.randint(1, 2**31 - 1)
    p.apiPDU.set_request_id(pdu, rid)
    vbs = [(rfc1902.ObjectName(SYS_UPTIME), rfc1902.TimeTicks(uptime)), (rfc1902.ObjectName(TRAP_OID), rfc1902.ObjectName(trap_oid))]
    vbs += [(rfc1902.ObjectName(o), v) for o, v in varbinds]
    p.apiPDU.set_varbinds(pdu, vbs)
    msg = p.Message()
    p.apiMessage.set_defaults(msg)
    p.apiMessage.set_community(msg, community)
    p.apiMessage.set_pdu(msg, pdu)
    return encoder.encode(msg), rid


def send_udp(data: bytes, to: tuple[str, int], src: str = "127.0.0.1", wait_response: float = 0.0) -> bytes | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((src, 0))
        s.sendto(data, to)
        if wait_response > 0:
            s.settimeout(wait_response)
            try:
                resp, _ = s.recvfrom(65535)
                return resp
            except TimeoutError:
                return None
        return None
    finally:
        s.close()


def decode_v2c_response(data: bytes) -> dict:
    p = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
    msg, _ = decoder.decode(data, asn1Spec=p.Message())
    pdu = p.apiMessage.get_pdu(msg)
    return {
        "pdu": pdu.getName() if hasattr(pdu, "getName") else type(pdu).__name__,
        "request_id": int(p.apiPDU.get_request_id(pdu)),
        "varbinds": [[str(o), v.prettyPrint()] for o, v in p.apiPDU.get_varbinds(pdu)],
    }


async def send_v3(to: tuple[str, int], src: str, user: str, trap_oid: str, varbinds: list[tuple[str, object]],
                  engine_id: str, auth: str = "none", auth_key: str | None = None, priv: str = "none",
                  priv_key: str | None = None, inform: bool = False, uptime: int = 12345) -> str | None:
    from pysnmp.hlapi.v3arch.asyncio import (
        ContextData,
        NotificationType,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        UsmUserData,
        send_notification,
    )
    from pysnmp.hlapi.v3arch.asyncio import auth as hauth
    from pysnmp.proto.rfc1902 import OctetString

    eng = SnmpEngine(snmpEngineID=OctetString(hexValue=engine_id.removeprefix("0x")))
    kwargs: dict = {}
    if auth != "none":
        kwargs["authKey"] = auth_key
        kwargs["authProtocol"] = getattr(hauth, AUTH[auth])
    if priv != "none":
        kwargs["privKey"] = priv_key
        kwargs["privProtocol"] = getattr(hauth, PRIV[priv])
    target = await UdpTransportTarget.create(to, timeout=2, retries=0)
    target.set_local_address((src, 0))
    nt = NotificationType(ObjectIdentity(trap_oid)).add_varbinds(
        ObjectType(ObjectIdentity(SYS_UPTIME), rfc1902.TimeTicks(uptime)),
        *[ObjectType(ObjectIdentity(o), v) for o, v in varbinds],
    )
    err, status, _idx, _vbs = await send_notification(
        eng, UsmUserData(user, **kwargs), target, ContextData(), "inform" if inform else "trap", nt
    )
    # let the dispatcher flush the datagram before closing
    await asyncio.sleep(0.05)
    eng.close_dispatcher()
    if err:
        return str(err)
    if status:
        return status.prettyPrint()
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True, help="host:port of the trap receiver")
    ap.add_argument("--from", dest="src", default="127.0.0.1", help="source address to bind (127.0.0.x)")
    ap.add_argument("--version", choices=["v1", "v2c", "v3"], default="v2c")
    ap.add_argument("--inform", action="store_true")
    ap.add_argument("--community", default="public")
    ap.add_argument("--oid", required=True, help="trap OID (snmpTrapOID.0)")
    ap.add_argument("--varbind", action="append", default=[], help="OID=TYPE:VALUE (repeatable)")
    ap.add_argument("--uptime", type=int, default=12345)
    ap.add_argument("--agent-addr", default=None, help="v1 agent-addr (default: --from)")
    ap.add_argument("--enterprise", default=None, help="v1 enterprise OID for generic traps")
    ap.add_argument("--user")
    ap.add_argument("--auth", default="none", choices=sorted(AUTH))
    ap.add_argument("--auth-key")
    ap.add_argument("--priv", default="none", choices=sorted(PRIV))
    ap.add_argument("--priv-key")
    ap.add_argument("--engine-id", default="0x8000000001020304")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--interval", type=float, default=0.0)
    ap.add_argument("--timeout", type=float, default=3.0, help="inform response wait (s)")
    a = ap.parse_args()
    host, port = a.to.rsplit(":", 1)
    to = (host, int(port))
    vbs = parse_varbinds(a.varbind)
    rc = 0
    for i in range(a.repeat):
        if i and a.interval:
            time.sleep(a.interval)
        out: dict = {"version": a.version, "from": a.src, "to": a.to, "oid": a.oid, "inform": a.inform}
        if a.version == "v1":
            data = build_v1(a.community, a.oid, vbs, a.agent_addr or a.src, a.uptime, a.enterprise)
            send_udp(data, to, a.src)
            out["bytes"] = len(data)
        elif a.version == "v2c":
            data, rid = build_v2c(a.community, a.oid, vbs, a.uptime, a.inform)
            resp = send_udp(data, to, a.src, a.timeout if a.inform else 0)
            out.update(bytes=len(data), request_id=rid)
            if a.inform:
                if resp is None:
                    out["response"] = None
                    rc = 3
                else:
                    out["response"] = decode_v2c_response(resp)
        else:
            if not a.user:
                ap.error("--user is required for v3")
            err = asyncio.run(send_v3(to, a.src, a.user, a.oid, vbs, a.engine_id, a.auth, a.auth_key, a.priv,
                                      a.priv_key, a.inform, a.uptime))
            out["error"] = err
            # a v3 trap gets no answer; errors here are local (e.g. key too short)
            if err and (a.inform or "timeout" not in err.lower()):
                rc = 4
        print(json.dumps(out), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
