"""[2b-3] Senders for the ingestion tests: syslog over UDP/TCP and SNMP v1/v2c/v3 traps from a
chosen loopback source address (Linux routes all of 127/8 locally)."""

from __future__ import annotations

import asyncio
import socket
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from pyasn1.codec.ber import encoder
from pysnmp.proto import api

LOOPBACK_ALIASES = sys.platform.startswith("linux")


def src(addr: str) -> str:
    """127.0.0.x source addresses only work on Linux; elsewhere everything is 127.0.0.1."""
    return addr if LOOPBACK_ALIASES else "127.0.0.1"


def send_udp(port: int, data: bytes, source: str = "127.0.0.81") -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((src(source), 0))
        s.sendto(data, ("127.0.0.1", port))
    finally:
        s.close()


def send_tcp(port: int, data: bytes, source: str = "127.0.0.81") -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((src(source), 0))
    s.connect(("127.0.0.1", port))
    s.sendall(data)
    return s


async def wait_for(check: Callable[[], Awaitable[Any]], timeout: float = 8.0) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await check()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.1)


def _value(p: Any, typ: str, v: Any) -> Any:
    return {
        "i": lambda: p.Integer(int(v)),
        "s": lambda: p.OctetString(v if isinstance(v, bytes) else str(v).encode()),
        "o": lambda: p.ObjectIdentifier(str(v)),
        "a": lambda: p.IpAddress(str(v)),
        "t": lambda: p.TimeTicks(int(v)),
    }[typ]()


def v2c_trap(
    trap_oid: str,
    varbinds: list[tuple[str, str, Any]] = (),  # type: ignore[assignment]
    community: str = "public",
    inform: bool = False,
    uptime: int = 100,
) -> bytes:
    p = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
    pdu = p.InformRequestPDU() if inform else p.SNMPv2TrapPDU()
    p.apiPDU.set_defaults(pdu)
    vbs = [
        (p.ObjectIdentifier("1.3.6.1.2.1.1.3.0"), p.TimeTicks(uptime)),
        (p.ObjectIdentifier("1.3.6.1.6.3.1.1.4.1.0"), p.ObjectIdentifier(trap_oid)),
    ]
    vbs += [(p.ObjectIdentifier(o), _value(p, t, v)) for o, t, v in varbinds]
    p.apiPDU.set_varbinds(pdu, vbs)
    msg = p.Message()
    p.apiMessage.set_defaults(msg)
    p.apiMessage.set_community(msg, community)
    p.apiMessage.set_pdu(msg, pdu)
    return bytes(encoder.encode(msg))


def v1_trap(
    enterprise: str,
    generic: int,
    specific: int,
    agent: str,
    varbinds: list[tuple[str, str, Any]] = (),  # type: ignore[assignment]
    community: str = "public",
) -> bytes:
    p = api.PROTOCOL_MODULES[api.SNMP_VERSION_1]
    pdu = p.TrapPDU()
    p.apiTrapPDU.set_defaults(pdu)
    p.apiTrapPDU.set_enterprise(pdu, tuple(int(x) for x in enterprise.split(".")))
    p.apiTrapPDU.set_agent_address(pdu, agent)
    p.apiTrapPDU.set_generic_trap(pdu, generic)
    p.apiTrapPDU.set_specific_trap(pdu, specific)
    p.apiTrapPDU.set_timestamp(pdu, 55)
    p.apiTrapPDU.set_varbinds(
        pdu, [(p.ObjectIdentifier(o), _value(p, t, v)) for o, t, v in varbinds]
    )
    msg = p.Message()
    p.apiMessage.set_defaults(msg)
    p.apiMessage.set_community(msg, community)
    p.apiMessage.set_pdu(msg, pdu)
    return bytes(encoder.encode(msg))


async def v3_trap(
    engine_id_hex: str,
    user: str,
    trap_oid: str,
    *,
    auth: str | None = "SHA",
    auth_key: str | None = None,
    priv: str | None = "AES128",
    priv_key: str | None = None,
    varbinds: list[tuple[str, Any]] = (),  # type: ignore[assignment]
) -> bytes:
    """The bytes of one SNMPv3 trap (sent by pysnmp's hlapi to a local socket)."""
    from pysnmp.hlapi.v3arch import asyncio as h
    from pysnmp.proto import rfc1902

    auth_map = {
        "SHA": h.usmHMACSHAAuthProtocol,
        "SHA256": h.usmHMAC192SHA256AuthProtocol,
        "MD5": h.usmHMACMD5AuthProtocol,
    }
    priv_map = {
        "AES128": h.usmAesCfb128Protocol,
        "AES256": h.usmAesCfb256Protocol,
        "DES": h.usmDESPrivProtocol,
    }
    if auth_key is None:
        data = h.UsmUserData(user)
    elif priv_key is None:
        data = h.UsmUserData(user, authKey=auth_key, authProtocol=auth_map[auth or "SHA"])
    else:
        data = h.UsmUserData(
            user,
            authKey=auth_key,
            privKey=priv_key,
            authProtocol=auth_map[auth or "SHA"],
            privProtocol=priv_map[priv or "AES128"],
        )
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    sink.setblocking(False)
    engine = h.SnmpEngine(snmpEngineID=rfc1902.OctetString(hexValue=engine_id_hex))
    try:
        nt = h.NotificationType(h.ObjectIdentity(trap_oid))
        if varbinds:
            nt = nt.add_varbinds(*varbinds)
        await h.send_notification(
            engine,
            data,
            await h.UdpTransportTarget.create(sink.getsockname()),
            h.ContextData(),
            "trap",
            nt,
        )
        loop = asyncio.get_running_loop()
        return bytes(await asyncio.wait_for(loop.sock_recv(sink, 65535), 5))
    finally:
        engine.close_dispatcher()
        sink.close()
