"""SNMP get/walk helpers on top of pysnmp (v3arch asyncio) and credential resolution."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
from typing import Any

DEFAULT_SNMP: dict[str, Any] = {
    "version": "v2c",
    "port": 161,
    "timeout_s": 2,
    "retries": 1,
    "community": "public",
    "v3_username": None,
    "v3_security_level": None,
    "v3_auth_protocol": None,
    "v3_auth_key": None,
    "v3_priv_protocol": None,
    "v3_priv_key": None,
}


class SnmpError(Exception):
    pass


# One SNMP engine for v1/v2c, and one per distinct SNMPv3 credential set.
#
# pysnmp keeps SNMPv3 users in the engine keyed by user name only. With a single shared
# engine, two credential sets that use the same user name (e.g. "netmon" on a device and
# on its group, with different keys or protocols) overwrite each other: concurrent polls
# then go out with the other set's keys and fail ("Wrong SNMP PDU digest" or a timeout).
# A separate engine per credential fingerprint keeps them isolated.
_MAX_V3_ENGINES = 64
_engines: dict[str, Any] = {}
_engine_loop: asyncio.AbstractEventLoop | None = None


def _v3_fingerprint(creds: dict[str, Any]) -> str:
    material = "\x1f".join(
        str(creds.get(k) or "")
        for k in (
            "v3_username",
            "v3_security_level",
            "v3_auth_protocol",
            "v3_auth_key",
            "v3_priv_protocol",
            "v3_priv_key",
        )
    )
    return "v3:" + hashlib.sha256(material.encode()).hexdigest()


def _close_engine(engine: Any) -> None:
    try:
        engine.close_dispatcher()
    except Exception:  # noqa: BLE001 - best effort on eviction
        pass


_engine_locks: dict[str, asyncio.Lock] = {}


async def _get_engine(creds: dict[str, Any] | None = None) -> Any:
    """The engine for these credentials. A new SnmpEngine loads pysnmp's MIB modules (~60 ms,
    several times that on a slow host), so it is built in a worker thread: building it in the
    event loop froze the whole server on the first query and on every new v3 credential set."""
    global _engine_loop
    from pysnmp.hlapi.v3arch.asyncio import SnmpEngine

    loop = asyncio.get_running_loop()
    if _engine_loop is not loop:
        # New event loop (tests, restart): engines are bound to the loop they ran on.
        _engines.clear()
        _engine_locks.clear()
        _engine_loop = loop
    key = "v12" if not creds or creds.get("version") != "v3" else _v3_fingerprint(creds)
    engine = _engines.pop(key, None)
    if engine is None:
        lock = _engine_locks.setdefault(key, asyncio.Lock())
        async with lock:  # concurrent first queries with one credential set build one engine
            engine = _engines.pop(key, None)
            if engine is None:
                engine = await asyncio.to_thread(SnmpEngine)
                if _engine_loop is not loop:  # the loop changed while building
                    _engines.clear()
                    _engine_loop = loop
                if len(_engines) >= _MAX_V3_ENGINES + 1:
                    oldest = next((k for k in _engines if k != "v12"), None)
                    if oldest is not None:
                        _close_engine(_engines.pop(oldest))
                        _engine_locks.pop(oldest, None)
    _engines[key] = engine  # re-insert: most recently used last
    return engine


def _cred_bytes(value: Any, default: bytes = b"") -> Any:
    """SNMP credentials (community, v3 user name and keys) are byte strings, not text. pysnmp
    wraps a Python str in a us-ascii OctetString and raises PyAsn1UnicodeEncodeError on any
    non-ASCII character (e.g. a password typed with a Hebrew letter or a smart quote), which
    aborted every SNMP read of the device. Pass UTF-8 bytes so encoding always succeeds."""
    if value is None or value == "":
        return default
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _auth_data(creds: dict[str, Any]) -> Any:
    from pysnmp.hlapi.v3arch import asyncio as h

    version = creds.get("version", "v2c")
    if version in ("v1", "v2c"):
        community = _cred_bytes(creds.get("community"), b"public")
        return h.CommunityData(community, mpModel=0 if version == "v1" else 1)
    level = creds.get("v3_security_level") or "noAuthNoPriv"
    auth_map = {
        "MD5": h.usmHMACMD5AuthProtocol,
        "SHA": h.usmHMACSHAAuthProtocol,
        "SHA224": h.usmHMAC128SHA224AuthProtocol,
        "SHA256": h.usmHMAC192SHA256AuthProtocol,
        "SHA384": h.usmHMAC256SHA384AuthProtocol,
        "SHA512": h.usmHMAC384SHA512AuthProtocol,
    }
    priv_map = {
        "DES": h.usmDESPrivProtocol,
        "AES128": h.usmAesCfb128Protocol,
        "AES192": h.usmAesCfb192Protocol,
        "AES256": h.usmAesCfb256Protocol,
    }
    user = _cred_bytes(creds.get("v3_username"))
    if level == "noAuthNoPriv":
        return h.UsmUserData(user)
    auth_proto = auth_map.get(creds.get("v3_auth_protocol") or "SHA", h.usmHMACSHAAuthProtocol)
    if level == "authNoPriv":
        return h.UsmUserData(
            user, authKey=_cred_bytes(creds.get("v3_auth_key")), authProtocol=auth_proto
        )
    priv_proto = priv_map.get(creds.get("v3_priv_protocol") or "AES128", h.usmAesCfb128Protocol)
    return h.UsmUserData(
        user,
        authKey=_cred_bytes(creds.get("v3_auth_key")),
        privKey=_cred_bytes(creds.get("v3_priv_key")),
        authProtocol=auth_proto,
        privProtocol=priv_proto,
    )


async def _target(host: str, creds: dict[str, Any]) -> Any:
    from pysnmp.hlapi.v3arch import asyncio as h

    port = int(creds.get("port", 161))
    timeout = float(creds.get("timeout_s", 2))
    retries = int(creds.get("retries", 1))
    is_v6 = False
    try:
        is_v6 = ipaddress.ip_address(host).version == 6
    except ValueError:
        pass
    cls = h.Udp6TransportTarget if is_v6 else h.UdpTransportTarget
    try:
        return await cls.create((host, port), timeout=timeout, retries=retries)
    except Exception as exc:  # noqa: BLE001
        raise SnmpError(f"Cannot resolve {host}: {exc}") from exc


def convert(value: Any) -> Any:
    """pysnmp value -> python (int | bytes | str | None)."""
    from pysnmp.proto import rfc1902, rfc1905

    if isinstance(value, (rfc1905.NoSuchObject, rfc1905.NoSuchInstance, rfc1905.EndOfMibView)):
        return None
    if isinstance(value, rfc1902.ObjectIdentifier):
        return str(value)
    if isinstance(value, rfc1902.IpAddress):
        return ".".join(str(b) for b in value.asOctets())
    if isinstance(value, rfc1902.OctetString):
        return bytes(value.asOctets())
    if value.__class__.__name__ == "Null":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def _check(err_ind: Any, err_status: Any, err_index: Any, var_binds: Any) -> None:
    if err_ind:
        raise SnmpError(str(err_ind))
    if err_status and int(err_status) != 0:
        raise SnmpError(f"SNMP error: {err_status.prettyPrint()}")


async def snmp_get(host: str, creds: dict[str, Any], oids: list[str]) -> dict[str, Any]:
    """GET several OIDs at once. Returns {oid: value_or_None}. noSuchName (v1) -> SnmpError."""
    from pysnmp.hlapi.v3arch import asyncio as h

    engine = await _get_engine(creds)
    target = await _target(host, creds)
    try:
        err_ind, err_status, err_index, var_binds = await h.get_cmd(
            engine,
            _auth_data(creds),
            target,
            h.ContextData(),
            *[h.ObjectType(h.ObjectIdentity(o)) for o in oids],
            lookupMib=False,
        )
    except SnmpError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SnmpError(str(exc) or type(exc).__name__) from exc
    _check(err_ind, err_status, err_index, var_binds)
    out: dict[str, Any] = {}
    for oid, val in var_binds:
        out[str(oid)] = convert(val)
    for o in oids:
        out.setdefault(o, None)
    return out


async def snmp_walk(
    host: str, creds: dict[str, Any], base_oid: str, max_rows: int = 10000
) -> list[tuple[str, Any]]:
    """Walk a subtree. Returns [(oid, value)]."""
    from pysnmp.hlapi.v3arch import asyncio as h

    engine = await _get_engine(creds)
    target = await _target(host, creds)
    auth = _auth_data(creds)
    obj = h.ObjectType(h.ObjectIdentity(base_oid))
    if creds.get("version") == "v1":
        gen = h.walk_cmd(
            engine, auth, target, h.ContextData(), obj, lexicographicMode=False, lookupMib=False
        )
    else:
        gen = h.bulk_walk_cmd(
            engine,
            auth,
            target,
            h.ContextData(),
            0,
            25,
            obj,
            lexicographicMode=False,
            lookupMib=False,
        )
    rows: list[tuple[str, Any]] = []
    prefix = base_oid.rstrip(".") + "."
    try:
        async for err_ind, err_status, _err_index, var_binds in gen:
            if err_ind:
                raise SnmpError(str(err_ind))
            if err_status and int(err_status) != 0:
                # noSuchName at end of a v1 walk is normal termination
                if err_status.prettyPrint() == "noSuchName":
                    break
                raise SnmpError(f"SNMP error: {err_status.prettyPrint()}")
            for oid, val in var_binds:
                soid = str(oid)
                if not soid.startswith(prefix):
                    return rows
                cv = convert(val)
                if cv is None and val.__class__.__name__ == "EndOfMibView":
                    return rows
                rows.append((soid, cv))
                if len(rows) >= max_rows:
                    return rows
    except SnmpError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SnmpError(str(exc) or type(exc).__name__) from exc
    return rows


def walk_to_index(rows: list[tuple[str, Any]], base_oid: str) -> dict[str, Any]:
    """Map the suffix after base_oid to value."""
    prefix = base_oid.rstrip(".") + "."
    return {oid[len(prefix) :]: v for oid, v in rows if oid.startswith(prefix)}


def as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace").strip("\x00").strip()
    return str(v)


def resolve_credentials(
    device_creds: dict[str, Any] | None,
    group_chain: list[tuple[int, dict[str, Any] | None]],
    device_id: int | None = None,
) -> tuple[dict[str, Any], str, int | None]:
    """group_chain: [(group_id, creds)] from the device's group up to the root. The stored
    objects may be encrypted ([1.4.2] netmon.services.secretbox: `device_id` is part of the
    device credentials' AAD); the result is always plaintext.

    Returns (credentials, source, group_id)."""
    from netmon.services import secretbox

    if device_creds is not None:
        return (
            {**DEFAULT_SNMP, **secretbox.open_snmp(device_creds, "device", device_id)},
            "device",
            None,
        )
    for gid, creds in group_chain:
        if creds is not None:
            return {**DEFAULT_SNMP, **secretbox.open_snmp(creds, "group", gid)}, "group", gid
    return dict(DEFAULT_SNMP), "default", None


MISSING_TYPES = ("NoSuchObject", "NoSuchInstance", "EndOfMibView", "noSuchName")


async def snmp_get_typed(
    host: str, creds: dict[str, Any], oids: list[str]
) -> dict[str, tuple[Any, str]]:
    """GET several OIDs in one request. Returns {oid: (value, asn1_type_name)}.

    The type name is the pysnmp class name (`Counter32`, `Counter64`, `Gauge32`, `Integer32`,
    `Integer`, `OctetString`, `TimeTicks`, ...). A missing OID has value None and type
    `NoSuchObject` / `NoSuchInstance` / `EndOfMibView`, or `noSuchName` (SNMP v1)."""
    from pysnmp.hlapi.v3arch import asyncio as h

    engine = await _get_engine(creds)
    target = await _target(host, creds)
    try:
        err_ind, err_status, err_index, var_binds = await h.get_cmd(
            engine,
            _auth_data(creds),
            target,
            h.ContextData(),
            *[h.ObjectType(h.ObjectIdentity(o)) for o in oids],
            lookupMib=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise SnmpError(str(exc) or type(exc).__name__) from exc
    if err_ind:
        raise SnmpError(str(err_ind))
    if err_status and int(err_status) != 0:
        if err_status.prettyPrint() == "noSuchName":
            return {o: (None, "noSuchName") for o in oids}
        raise SnmpError(f"SNMP error: {err_status.prettyPrint()}")
    out: dict[str, tuple[Any, str]] = {}
    for oid, val in var_binds:
        out[str(oid)] = (convert(val), val.__class__.__name__)
    for o in oids:
        out.setdefault(o, (None, "NoSuchObject"))
    return out


async def snmp_getnext(
    host: str, creds: dict[str, Any], oids: list[str]
) -> list[tuple[str, Any, str]]:
    """One GETNEXT with several varbinds. Returns [(next_oid, value, type_name)] in request
    order. v1 noSuchName (end of MIB) yields an empty list."""
    from pysnmp.hlapi.v3arch import asyncio as h

    engine = await _get_engine(creds)
    target = await _target(host, creds)
    try:
        err_ind, err_status, _err_index, var_binds = await h.next_cmd(
            engine,
            _auth_data(creds),
            target,
            h.ContextData(),
            *[h.ObjectType(h.ObjectIdentity(o)) for o in oids],
            lookupMib=False,
            lexicographicMode=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise SnmpError(str(exc) or type(exc).__name__) from exc
    if err_ind:
        raise SnmpError(str(err_ind))
    if err_status and int(err_status) != 0:
        if err_status.prettyPrint() == "noSuchName":
            return []
        raise SnmpError(f"SNMP error: {err_status.prettyPrint()}")
    return [(str(oid), convert(val), val.__class__.__name__) for oid, val in var_binds]


def in_subtree(oid: str, base: str) -> bool:
    base = base.strip(".")
    oid = oid.strip(".")
    return oid == base or oid.startswith(base + ".")
