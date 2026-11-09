"""[2b-3] Trap receiver: asyncio UDP endpoint (hands datagrams to the pipeline, never decodes
in the callback) and the SNMPv3 engine with lazily registered USM users (architecture
section 12.2.3)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import socket
from collections import OrderedDict, deque
from typing import TYPE_CHECKING, Any

from netmon.ingest.attribution import normalize_ip
from netmon.ingest.bind_error import bind_reason
from netmon.ingest.trap_decode import TrapDecodeError, TrapRecord, build_varbinds, from_v2_varbinds

if TYPE_CHECKING:
    from netmon.ingest.pipeline import Ingest

log = logging.getLogger("netmon.ingest.traps")

RCVBUF = 4 * 1024 * 1024
MAX_REGISTERED = 1024


def set_rcvbuf(transport: asyncio.BaseTransport) -> None:
    sock = transport.get_extra_info("socket")
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, ingest: Ingest, kind: str) -> None:
        self.ingest = ingest
        self.kind = kind  # trap | syslog
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        set_rcvbuf(transport)

    def datagram_received(self, data: bytes, addr: tuple[Any, ...]) -> None:
        ip = normalize_ip(str(addr[0])) or str(addr[0])
        self.ingest.accept(self.kind, data, ip, addr, "udp", self)

    def error_received(self, exc: Exception) -> None:  # e.g. ICMP port unreachable
        log.debug("UDP %s socket error: %s", self.kind, exc)

    def send(self, data: bytes, addr: tuple[Any, ...]) -> None:
        if self.transport is not None:
            with contextlib.suppress(Exception):
                self.transport.sendto(data, addr)


class UdpReceiver:
    """One UDP listener with its ReceiverStatus."""

    def __init__(self, ingest: Ingest, kind: str, status_kind: str, port: int) -> None:
        self.ingest = ingest
        self.kind = kind
        self.status_kind = status_kind
        self.port = port
        self.transport: asyncio.DatagramTransport | None = None
        self.protocol: _UdpProtocol | None = None
        self.state = "disabled"
        self.error: str | None = None
        self.since: Any = None
        self.wanted = False

    @property
    def listening(self) -> bool:
        return self.transport is not None

    async def start(self, host: str) -> bool:
        self.wanted = True
        if self.transport is not None:
            return True
        if self.port == 0:
            self._set("disabled", None)
            return True
        loop = asyncio.get_running_loop()
        try:
            transport, protocol = await asyncio.wait_for(
                loop.create_datagram_endpoint(
                    lambda: _UdpProtocol(self.ingest, self.kind), local_addr=(host, self.port)
                ),
                5,
            )
        except (OSError, TimeoutError) as exc:
            self._set("error", bind_reason(exc, host, self.port, udp=True))
            return False
        self.transport = transport  # type: ignore[assignment]
        self.protocol = protocol
        self._set("listening", None)
        return True

    async def stop(self) -> None:
        self.wanted = False
        if self.transport is not None:
            self.transport.close()
            self.transport = None
            self.protocol = None
        self._set("disabled", None)

    def _set(self, state: str, error: str | None) -> None:
        changed = state != self.state or error != self.error
        self.state = state
        self.error = error
        if changed:
            self.since = self.ingest.rt.clock.now()
            self.ingest.receiver_changed(self)

    def status(self) -> dict[str, Any]:
        from netmon.services.serialize import iso

        return {
            "kind": self.status_kind,
            "port": self.port,
            "enabled": self.wanted,
            "state": self.state,
            "error": self.error,
            "since": iso(self.since),
        }


# ------------------------------------------------------------------ SNMPv3
AUTH_PROTOCOLS = {
    "MD5": "USM_AUTH_HMAC96_MD5",
    "SHA": "USM_AUTH_HMAC96_SHA",
    "SHA224": "USM_AUTH_HMAC128_SHA224",
    "SHA256": "USM_AUTH_HMAC192_SHA256",
    "SHA384": "USM_AUTH_HMAC256_SHA384",
    "SHA512": "USM_AUTH_HMAC384_SHA512",
}
PRIV_PROTOCOLS = {
    "DES": "USM_PRIV_CBC56_DES",
    "AES128": "USM_PRIV_CFB128_AES",
    "AES192": "USM_PRIV_CFB192_AES",
    "AES256": "USM_PRIV_CFB256_AES",
}


def cred_fingerprint(c: dict[str, Any]) -> str:
    material = "\x1f".join(
        str(c.get(k) or "")
        for k in (
            "username",
            "security_level",
            "auth_protocol",
            "auth_key",
            "priv_protocol",
            "priv_key",
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


def snmp_creds_to_user(c: dict[str, Any]) -> dict[str, Any] | None:
    """A device/group snmp_credentials object (v3) -> a trap user dict."""
    if not isinstance(c, dict) or c.get("version") != "v3" or not c.get("v3_username"):
        return None
    return {
        "username": c.get("v3_username"),
        "security_level": c.get("v3_security_level") or "noAuthNoPriv",
        "auth_protocol": c.get("v3_auth_protocol") or "SHA",
        "auth_key": c.get("v3_auth_key"),
        "priv_protocol": c.get("v3_priv_protocol") or "AES128",
        "priv_key": c.get("v3_priv_key"),
    }


class V3Engine:
    """A receiver-only SnmpEngine (no transport): datagrams are fed to its message
    dispatcher; (engine id, user) pairs are registered on first use (LRU)."""

    def __init__(self) -> None:
        self.engine: Any = None
        self._registered: OrderedDict[tuple[bytes, str], str] = OrderedDict()
        self._result: tuple[str, bool, list[Any]] | None = None
        self._failure: str | None = None
        self._lock = asyncio.Lock()
        # per username, in precedence order: settings users first, then device/group creds
        self.settings_users: dict[str, dict[str, Any]] = {}
        self.cred_users: dict[str, dict[str, Any]] = {}
        self.device_users: dict[int, dict[str, Any]] = {}
        self.failures: deque[dict[str, Any]] = deque(maxlen=20)

    def configure(
        self,
        settings_users: list[dict[str, Any]],
        cred_users: list[dict[str, Any]],
        device_users: dict[int, dict[str, Any]],
    ) -> bool:
        """Returns True when the candidate keys changed (the engine is then rebuilt)."""
        su = {}
        for u in settings_users:
            if u.get("username") and u["username"] not in su:
                su[u["username"]] = u
        cu = {}
        for u in cred_users:
            if u.get("username") and u["username"] not in cu:
                cu[u["username"]] = u
        old = (self.settings_users, self.cred_users, self.device_users)
        changed = (
            {k: cred_fingerprint(v) for k, v in old[0].items()}
            != {k: cred_fingerprint(v) for k, v in su.items()}
            or {k: cred_fingerprint(v) for k, v in old[1].items()}
            != {k: cred_fingerprint(v) for k, v in cu.items()}
            or {k: cred_fingerprint(v) for k, v in old[2].items()}
            != {k: cred_fingerprint(v) for k, v in device_users.items()}
        )
        self.settings_users, self.cred_users, self.device_users = su, cu, device_users
        if changed:
            self.drop()
        return changed

    def drop(self) -> None:
        self.engine = None
        self._registered.clear()

    async def ensure(self) -> Any:
        if self.engine is None:
            async with self._lock:
                if self.engine is None:
                    self.engine = await asyncio.to_thread(self._build)
        return self.engine

    def _build(self) -> Any:
        from pysnmp.entity.engine import SnmpEngine
        from pysnmp.proto.api import v2c

        engine = SnmpEngine()
        engine.message_dispatcher.register_context_engine_id(
            b"", (v2c.SNMPv2TrapPDU.tagSet, v2c.InformRequestPDU.tagSet), self._process_pdu
        )
        engine.observer.register_observer(
            self._sm_failure, "rfc3412.prepareDataElements:sm-failure"
        )
        return engine

    def _process_pdu(
        self,
        snmpEngine: Any,  # noqa: N803 - pysnmp callback signature
        messageProcessingModel: Any,  # noqa: N803
        securityModel: Any,  # noqa: N803
        securityName: Any,  # noqa: N803
        securityLevel: Any,  # noqa: N803
        contextEngineId: Any,  # noqa: N803
        contextName: Any,  # noqa: N803
        pduVersion: Any,  # noqa: N803
        PDU: Any,  # noqa: N803
        maxSizeResponseScopedPDU: Any,  # noqa: N803
        stateReference: Any,  # noqa: N803
    ) -> None:
        from pysnmp.proto import rfc3411
        from pysnmp.proto.api import v2c

        confirmed = PDU.tagSet in rfc3411.CONFIRMED_CLASS_PDUS
        self._result = (str(securityName), confirmed, list(v2c.apiPDU.get_varbinds(PDU)))

    def _sm_failure(self, snmpEngine: Any, execpoint: Any, variables: Any, cbCtx: Any) -> None:  # noqa: N803
        info = variables.get("statusInformation") if isinstance(variables, dict) else None
        ind = None
        with contextlib.suppress(Exception):
            ind = info.get("errorIndication") if info is not None else None
        self._failure = str(ind) if ind is not None else "authentication failed"

    def candidate(self, username: str, device_id: int | None) -> dict[str, Any] | None:
        if device_id is not None:
            u = self.device_users.get(device_id)
            if u is not None and u.get("username") == username:
                return u
        return self.settings_users.get(username) or self.cred_users.get(username)

    def _register(self, engine_id: bytes, user: dict[str, Any]) -> None:
        from pysnmp.entity import config
        from pysnmp.proto import rfc1902

        key = (engine_id, str(user["username"]))
        fp = cred_fingerprint(user)
        if self._registered.get(key) == fp:
            self._registered.move_to_end(key)
            return
        level = user.get("security_level") or "noAuthNoPriv"
        auth = config.USM_AUTH_NONE
        priv = config.USM_PRIV_NONE
        auth_key = priv_key = None
        if level in ("authNoPriv", "authPriv"):
            auth = getattr(
                config,
                AUTH_PROTOCOLS.get(user.get("auth_protocol") or "SHA", "USM_AUTH_HMAC96_SHA"),
            )
            auth_key = user.get("auth_key") or ""
        if level == "authPriv":
            priv = getattr(
                config,
                PRIV_PROTOCOLS.get(user.get("priv_protocol") or "AES128", "USM_PRIV_CFB128_AES"),
            )
            priv_key = user.get("priv_key") or ""
        eid = rfc1902.OctetString(engine_id)
        if key in self._registered:
            with contextlib.suppress(Exception):
                config.delete_v3_user(self.engine, key[1], securityEngineId=eid)
        config.add_v3_user(
            self.engine,
            key[1],
            auth,
            auth_key,
            priv,
            priv_key,
            securityEngineId=eid,
        )
        self._registered[key] = fp
        self._registered.move_to_end(key)
        while len(self._registered) > MAX_REGISTERED:
            (old_eid, old_user), _ = self._registered.popitem(last=False)
            with contextlib.suppress(Exception):
                config.delete_v3_user(
                    self.engine, old_user, securityEngineId=rfc1902.OctetString(old_eid)
                )

    def fail(self, now: Any, ip: str, engine_id: bytes, username: str, reason: str) -> None:
        from netmon.services.serialize import iso

        self.failures.append(
            {
                "time": iso(now),
                "source_ip": ip,
                "engine_id": engine_id.hex(),
                "username": username,
                "reason": reason[:200],
            }
        )

    def decode(
        self,
        data: bytes,
        addr: tuple[Any, ...],
        engine_id: bytes,
        username: str,
        device_id: int | None,
    ) -> TrapRecord:
        """Decrypt/authenticate one v3 message. Raises TrapDecodeError(v3_auth|unsupported)."""
        from pysnmp.carrier.asyncio.dgram import udp

        user = self.candidate(username, device_id)
        if user is None:
            raise TrapDecodeError("v3_auth", "unknown user")
        try:
            self._register(engine_id, user)
        except Exception as exc:  # noqa: BLE001 - e.g. bad key length
            raise TrapDecodeError("v3_auth", f"cannot register user: {exc}") from None
        self._result = None
        self._failure = None
        try:
            self.engine.message_dispatcher.receive_message(
                self.engine, udp.DOMAIN_NAME, (str(addr[0]), int(addr[1])), data
            )
        except Exception as exc:  # noqa: BLE001 - a report/response attempt without transport
            if self._result is None:
                raise TrapDecodeError("v3_auth", self._failure or str(exc) or "error") from None
        if self._result is None:
            raise TrapDecodeError("v3_auth", self._failure or "authentication failed")
        sec_name, confirmed, pairs = self._result
        if confirmed:
            raise TrapDecodeError("unsupported", "SNMPv3 inform")
        return from_v2_varbinds(build_varbinds(pairs), "v3", "trap", security_name=sec_name[:64])
