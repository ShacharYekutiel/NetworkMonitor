"""[4] `rt.aws`: the credential chains of every AWS consumer (architecture § 15.1.5).

`chain(consumer, view, access) -> CachingProvider`:
- `direct`: the connection's base `CachingProvider`;
- `assume_role`: `ChainCachingProvider(AssumeRoleProvider(source=base, …))`;
- `ra_role`: `CachingProvider(RolesAnywhereProvider(view, …, role_arn=…))`.
The base cache of a connection is shared by all chains of that connection (one `CreateSession`
serves every consumer). Key `(connection id, connection hash, mode, role_arn,
sha256(external_id))`; LRU 32 chains; a connection save drops its chains (in-flight calls
finish on the old ones; their HTTP clients close after a grace period)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from netmon.aws import connections as conns
from netmon.aws import endpoints
from netmon.aws import transport as tp
from netmon.aws.credentials import (
    AssumeRoleProvider,
    CachingProvider,
    ChainCachingProvider,
    Credentials,
)
from netmon.aws.errors import AssistantError
from netmon.aws.sts import StsClient, account_of, session_name
from netmon.services.testids import TestRegistry

log = logging.getLogger("netmon.aws")

MAX_CHAINS = 32
OLD_CLIENT_GRACE_S = 300.0


def chain_key(
    conn_id: int | None, conn_hash: str, access: dict[str, Any]
) -> tuple[int | None, str, str, str | None, str | None]:
    """Pure (data-model vector): `(connection id, connection hash, mode, role_arn,
    sha256(external_id))`."""
    a = conns.normalize_access(access)
    ext = a.get("external_id")
    return (
        conn_id,
        conn_hash,
        a["mode"],
        a.get("role_arn"),
        hashlib.sha256(ext.encode()).hexdigest() if ext else None,
    )


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    from netmon.services.serialize import iso

    return iso(dt)


class _Recording:
    """Wraps a provider: remembers the last refresh, the state and the error of one hop."""

    def __init__(self, inner: Any, hop: dict[str, Any], on_state: Any) -> None:
        self.inner = inner
        self.hop = hop
        self.on_state = on_state
        self.source = getattr(inner, "source", None)

    async def fetch(self) -> Credentials:
        try:
            creds = await self.inner.fetch()
        except AssistantError as err:
            self.hop["error_code"] = err.code
            self.on_state("error", err.code)
            raise
        d = creds.detail or {}
        arn = d.get("assumed_role_arn") or d.get("role_arn") or d.get("arn") or self.hop.get("arn")
        self.hop.update(
            arn=arn,
            account=account_of(arn) or self.hop.get("account"),
            expires_at=_iso(creds.expiration),
            last_refresh=_iso(datetime.now(UTC)),
            error_code=None,
        )
        if d.get("source_identity"):
            self.hop["source_identity"] = d["source_identity"]
        self.on_state("ok", None)
        return creds

    async def aclose(self) -> None:
        close = getattr(self.inner, "aclose", None)
        if close is not None:
            await close()


@dataclass
class _Base:
    conn_id: int | None
    conn_hash: str
    view: dict[str, Any]
    http: Any
    provider: CachingProvider
    hop: dict[str, Any]


@dataclass
class _Chain:
    consumer: str
    key: tuple[Any, ...]
    base: _Base
    provider: CachingProvider
    mode: str
    hop: dict[str, Any] | None = None
    state: str = "idle"
    error_code: str | None = None
    sts: StsClient | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def hops(self) -> list[dict[str, Any]]:
        out = [{k: self.base.hop.get(k) for k in ("hop", "kind", "arn", "account", "expires_at",
                                                    "last_refresh")}]  # fmt: skip
        if self.hop is not None:
            out.append(
                {
                    k: self.hop.get(k)
                    for k in ("hop", "kind", "arn", "account", "expires_at", "last_refresh")
                }  # fmt: skip
            )
        return out


class CredentialCache:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.bases: dict[tuple[int | None, str], _Base] = {}
        self.chains: OrderedDict[tuple[Any, ...], _Chain] = OrderedDict()
        self.by_consumer: dict[str, _Chain] = {}
        self._bg: set[asyncio.Task[Any]] = set()

    @property
    def secret_key(self) -> str:
        return str(getattr(self.rt, "secret_key", "") or "")

    # ------------------------------------------------------------ chains
    def _base(self, v: dict[str, Any], conn_hash: str) -> _Base:
        key = (v.get("_id"), conn_hash)
        b = self.bases.get(key)
        if b is not None:
            return b
        http = tp.make_client(v)
        hop0: dict[str, Any] = {
            "hop": 0,
            "kind": v.get("auth_mode"),
            "arn": None,
            "account": None,
            "expires_at": None,
            "last_refresh": None,
            "error_code": None,
        }
        last = v.get("_last_test") or {}
        if last.get("arn"):
            hop0["arn"], hop0["account"] = last.get("arn"), last.get("account")
        try:
            provider = conns.make_provider(v, http, self.secret_key)
        except Exception:
            self._spawn(http.aclose())
            raise
        holder: dict[str, Any] = {}
        rec = _Recording(provider, hop0, lambda s, c: self._base_state(holder, s, c))
        b = _Base(v.get("_id"), conn_hash, v, http, CachingProvider(rec), hop0)
        holder["base"] = b
        self.bases[key] = b
        return b

    def _base_state(self, holder: dict[str, Any], state: str, code: str | None) -> None:
        base = holder.get("base")
        for ch in self.chains.values():
            if ch.base is base and ch.mode == "direct":
                self._set_state(ch, state, code)

    def chain(self, consumer: str, v: dict[str, Any], access: dict[str, Any]) -> CachingProvider:
        """The credential chain of `consumer` on the connection view `v` with `access`."""
        access = conns.normalize_access(access)
        conn_hash = conns.connection_hash(v, self.secret_key)
        key = chain_key(v.get("_id"), conn_hash, access)
        ch = self.chains.get(key)
        if ch is not None:
            self.chains.move_to_end(key)
            ch.consumer = consumer
            self.by_consumer[consumer] = ch
            return ch.provider
        base = self._base(v, conn_hash)
        mode = access["mode"]
        if mode == "direct":
            ch = _Chain(consumer, key, base, base.provider, mode)
        else:
            hop1: dict[str, Any] = {
                "hop": 1,
                "kind": mode,
                "arn": None,
                "account": account_of(access.get("role_arn")),
                "expires_at": None,
                "last_refresh": None,
                "error_code": None,
            }
            holder: dict[str, Any] = {}

            def on_state(s: str, c: str | None, holder: dict[str, Any] = holder) -> None:
                if holder.get("chain") is not None:
                    self._set_state(holder["chain"], s, c)

            if mode == "assume_role":
                sts = StsClient(
                    base.http,
                    str(v["region"]),
                    endpoints.sts_endpoint(v),
                    via=tp.via(endpoints.sts_endpoint(v), v),
                )
                inner: Any = AssumeRoleProvider(
                    base.provider,
                    str(access["role_arn"]),
                    access.get("external_id"),
                    session_name(consumer),
                    sts,
                )
                provider: CachingProvider = ChainCachingProvider(
                    _Recording(inner, hop1, on_state), base.provider
                )
            else:  # ra_role: a second CreateSession with the same certificate
                inner = conns.make_provider(
                    v, base.http, self.secret_key, role_arn=access["role_arn"]
                )
                provider = CachingProvider(_Recording(inner, hop1, on_state))
            ch = _Chain(consumer, key, base, provider, mode, hop=hop1)
            holder["chain"] = ch
        self.chains[key] = ch
        self.by_consumer[consumer] = ch
        while len(self.chains) > MAX_CHAINS:
            _, old = self.chains.popitem(last=False)
            self._drop_base_if_unused(old.base)
        return ch.provider

    def _set_state(self, ch: _Chain, state: str, code: str | None) -> None:
        old = ch.state
        ch.state, ch.error_code = state, code
        if old != state and state in ("ok", "error") and not (old == "idle" and state == "ok"):
            hub = getattr(self.rt, "hub", None)
            if hub is not None:
                with contextlib.suppress(Exception):
                    hub.broadcast(
                        "aws.connection",
                        {"connection_id": ch.base.conn_id, "consumer": ch.consumer,
                         "state": state, "error_code": code},
                        admin_only=True,
                    )  # fmt: skip

    def _drop_base_if_unused(self, base: _Base) -> None:
        if any(c.base is base for c in self.chains.values()):
            return
        self.bases.pop((base.conn_id, base.conn_hash), None)
        self._spawn(self._close_later(base.http))

    def invalidate(self, conn_id: int | None = None) -> None:
        """Drop the chains of a connection (all when None): the next use builds new ones."""
        for key in [
            k for k, c in self.chains.items() if conn_id is None or c.base.conn_id == conn_id
        ]:
            ch = self.chains.pop(key)
            if self.by_consumer.get(ch.consumer) is ch:
                self.by_consumer.pop(ch.consumer, None)
        for key in [k for k, b in self.bases.items() if conn_id is None or b.conn_id == conn_id]:
            b = self.bases.pop(key)
            self._spawn(self._close_later(b.http))

    # ------------------------------------------------------------ status
    def status_for(self, conn_id: int | None) -> list[dict[str, Any]]:
        out = []
        for consumer, ch in self.by_consumer.items():
            if ch.base.conn_id != conn_id:
                continue
            out.append(
                {
                    "consumer": consumer,
                    "hops": ch.hops(),
                    "state": ch.state,
                    "error_code": ch.error_code,
                }  # fmt: skip
            )
        return out

    def consumer_status(self, consumer: str) -> dict[str, Any] | None:
        ch = self.by_consumer.get(consumer)
        if ch is None:
            return None
        return {"hops": ch.hops(), "state": ch.state, "error_code": ch.error_code,
                "conn_id": ch.base.conn_id}  # fmt: skip

    # ------------------------------------------------------------ lifecycle
    def _spawn(self, coro: Any) -> None:
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _close_later(self, http: Any) -> None:
        await asyncio.sleep(OLD_CLIENT_GRACE_S)
        with contextlib.suppress(Exception):
            await http.aclose()

    async def aclose(self) -> None:
        for t in list(self._bg):
            t.cancel()
        self._bg.clear()
        for b in list(self.bases.values()):
            with contextlib.suppress(Exception):
                await b.http.aclose()
        self.bases.clear()
        self.chains.clear()
        self.by_consumer.clear()


class AwsRuntime:
    """`rt.aws`: the shared credential cache and the shared `test_id` registry."""

    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.tests = TestRegistry()
        self.cache = CredentialCache(rt)

    def chain(self, consumer: str, v: dict[str, Any], access: dict[str, Any]) -> CachingProvider:
        return self.cache.chain(consumer, v, access)

    def invalidate(self, conn_id: int | None = None) -> None:
        self.cache.invalidate(conn_id)

    async def aclose(self) -> None:
        await self.cache.aclose()
