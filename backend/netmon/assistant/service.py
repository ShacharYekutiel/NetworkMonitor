"""[3b] `rt.assistant`: the assistant's runtime component (architecture § 14.1 startup,
§ 14.2.5 health).

Interface for the agent loop (developer B) and the AI features (developer C):

    svc = rt.assistant
    svc.client                  # BedrockClient of the current settings generation
                                #   (converse / converse_stream / retrieve: .bedrock)
    svc.usage                   # UsageService (check_and_reserve / record / record_error / …)
    svc.cfg, svc.kb             # current `assistant` / `assistant_kb` objects (secrets encrypted)
    svc.scalars()               # 3b scalar settings (async)
    svc.state()                 # disabled | unconfigured | ready | degraded | error
    svc.runs                    # dict for the running question tasks (owned by the agent loop)
    svc.disable_hooks.append(f) # async f() called when `assistant` goes OFF (cancel runs …)
    svc.status_providers.append(f)  # async f() -> {"tools": [...]} / {"features": [...]}
                                #   merged into GET /api/assistant/status

Lifecycle: registered with `rt.features` (component); credentials are fetched lazily on the
first use or Test; turning `assistant` OFF drops the credentials and closes the clients."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import os
import stat
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any

from netmon.assistant import certs, config, profiles
from netmon.assistant import transport as tp
from netmon.assistant.bedrock import BedrockClient
from netmon.assistant.credentials import (
    CachingProvider,
    DefaultChainProvider,
    RaMaterial,
    RolesAnywhereProvider,
    StaticProvider,
)
from netmon.assistant.errors import AssistantError, make
from netmon.assistant.testconn import TestRegistry
from netmon.assistant.usage import UsageService

log = logging.getLogger("netmon.assistant")

THROTTLE_DEGRADED_S = 300.0
STATE_EVENT_EVERY_S = 3600.0
FLAGS_EVERY_S = 300.0
OLD_CLIENT_GRACE_S = 300.0
ACCESS_KEY_OLD_DAYS = 90


class AssistantService:
    features = ("assistant", "ai_knowledge")

    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.usage = UsageService(rt)
        self.tests = TestRegistry()
        self.cfg: dict[str, Any] = copy.deepcopy(config.DEFAULT_ASSISTANT)
        self.kb: dict[str, Any] = copy.deepcopy(config.DEFAULT_KB)
        self.enabled = False
        self.generation = 0
        self._client: BedrockClient | None = None
        self._creds: CachingProvider | None = None
        self.state_error: dict[str, Any] | None = None
        self._last_throttle: float | None = None
        self._flags = {"cert_expiring": False, "background_stopped": False}
        self._event_marks: dict[str, float] = {}
        self._cert_event_day: date | None = None
        self._last_state = "disabled"
        self._task: asyncio.Task[Any] | None = None
        self._bg: set[asyncio.Task[Any]] = set()
        self.runs: dict[Any, Any] = {}
        self.disable_hooks: list[Callable[[], Awaitable[None] | None]] = []
        self.status_providers: list[Callable[[], Awaitable[dict[str, Any]]]] = []

    # ------------------------------------------------------------ lifecycle (component)
    async def start(self, snapshot: dict[str, bool]) -> None:
        await self.load()
        if snapshot.get("assistant"):
            await self._activate()

    async def feature_changed(self, name: str, on: bool) -> None:
        if name == "assistant":
            if on:
                await self.load()
                await self._activate()
            else:
                await self._deactivate()
        elif name == "ai_knowledge":
            await self.load()
        await self._notify_state()

    async def stop(self) -> None:
        await self._deactivate()
        for t in list(self._bg):
            t.cancel()
        self._bg.clear()

    async def _activate(self) -> None:
        self.enabled = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._flags_loop(), name="assistant-flags")

    async def _deactivate(self) -> None:
        was = self.enabled
        self.enabled = False
        for hook in list(self.disable_hooks):
            try:
                res = hook()
                if asyncio.iscoroutine(res):
                    await asyncio.wait_for(res, 5)
            except Exception:  # noqa: BLE001
                log.exception("Assistant disable hook failed")
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        await self._drop_client()
        self.state_error = None
        self._last_throttle = None
        if was:
            await self._notify_state()

    async def load(self) -> None:
        if self.rt.sessionmaker is None:
            return
        async with self.rt.sessionmaker() as session:
            self.cfg = await config.load(session)
            self.kb = await config.load_kb(session)

    async def reload(self) -> None:
        """Settings (or the certificate) changed: re-read them and start a new client
        generation (the old client is closed after a grace period)."""
        before = self._conn_view(self.cfg)
        await self.load()
        if self._conn_view(self.cfg) != before:
            await self._drop_client()
            self.state_error = None
        await self.refresh_flags()
        await self._notify_state()

    @staticmethod
    def _conn_view(cfg: dict[str, Any]) -> dict[str, Any]:
        keys = (*config.CONNECTION_FIELDS, "secret_access_key_enc", "ra_cert_pem", "ra_chain_pem",
                "ra_key_enc")  # fmt: skip
        return {k: cfg.get(k) for k in keys}

    async def _drop_client(self) -> None:
        client, creds = self._client, self._creds
        self._client, self._creds = None, None
        self.generation += 1
        if creds is not None:
            creds.clear()
        if client is not None:
            if self.enabled:  # in-flight calls of the old generation may still run
                self._spawn(self._close_later(client))
            else:
                with contextlib.suppress(Exception):
                    await client.aclose()

    async def _close_later(self, client: BedrockClient) -> None:
        await asyncio.sleep(OLD_CLIENT_GRACE_S)
        with contextlib.suppress(Exception):
            await client.aclose()

    def _spawn(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ------------------------------------------------------------ settings helpers
    async def scalars(self) -> dict[str, Any]:
        async with self.rt.sessionmaker() as session:
            return await config.scalars(session)

    @property
    def secret_key(self) -> str:
        return str(getattr(self.rt, "secret_key", "") or "")

    @property
    def test_mode(self) -> bool:
        return bool(getattr(self.rt.settings, "netmon_test_mode", False))

    # ------------------------------------------------------------ certificate material
    def ra_material(self, cfg: dict[str, Any]) -> RaMaterial:
        mat = self.load_material(cfg)
        if mat is None or mat.key is None:
            raise make(
                "rolesanywhere_failed", aws="No Roles Anywhere certificate and key configured"
            )
        return RaMaterial(
            certs.der(mat.cert), mat.cert.serial_number, [certs.der(c) for c in mat.chain],
            mat.key,
        )  # fmt: skip

    def load_material(self, cfg: dict[str, Any]) -> certs.Material | None:
        """The certificate (+ chain, + key when readable) from host files or the upload.
        Raises `certs.CertError` / OSError for broken host files."""
        files = config.host_files()
        if files is not None:
            with open(files["cert"], "rb") as fh:
                cert_data = fh.read()
            with open(files["key"], "rb") as fh:
                key_data = fh.read()
            chain_data = b""
            if files.get("chain"):
                with open(files["chain"], "rb") as fh:
                    chain_data = fh.read()
            found = certs.load_pem_certs(cert_data + b"\n" + chain_data)
            if not found:
                raise certs.CertError("invalid_certificate", "host certificate file unreadable")
            leaf, chain = certs._split_leaf(found)  # noqa: SLF001
            key = certs.load_key(key_data, None)
            return certs.Material(leaf, chain, key)
        if not cfg.get("ra_cert_pem"):
            return None
        found = certs.load_pem_certs(
            (cfg["ra_cert_pem"] + "\n" + (cfg.get("ra_chain_pem") or "")).encode()
        )
        if not found:
            return None
        leaf = found[0]
        chain = certs.order_chain(leaf, found[1:])
        key = None
        try:
            pem = config.decrypt_opt(self.secret_key, cfg.get("ra_key_enc"), config.AAD_RA_KEY)
            key = certs.key_from_pkcs8_pem(pem) if pem else None
        except (config.Unreadable, ValueError):
            key = None
        return certs.Material(leaf, chain, key)

    def material_pems(self, cfg: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
        try:
            mat = self.load_material(cfg)
        except (OSError, certs.CertError, ValueError):
            return None, None, None
        if mat is None:
            return None, None, None
        return mat.cert_pem, mat.chain_pem, mat.key_pem

    def cert_status(self, cfg: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """(CertInfo | None, warnings)."""
        warnings: list[str] = []
        files = config.host_files()
        try:
            mat = self.load_material(cfg)
        except (OSError, certs.CertError, ValueError) as exc:
            log.warning("Roles Anywhere host files unusable: %s", type(exc).__name__)
            return None, warnings
        if mat is None:
            return None, warnings
        key_unreadable = mat.key is None
        info = certs.cert_info(
            mat.cert,
            mat.chain,
            source="files" if files else "uploaded",
            uploaded_at=None if files else cfg.get("ra_cert_uploaded_at"),
            key_unreadable=key_unreadable,
        )
        if certs.expiring(mat.cert, datetime.now(UTC)):
            warnings.append("cert_expiring")
        if files and os.name == "posix":
            try:
                mode = os.stat(files["key"]).st_mode
                if mode & (stat.S_IRWXG | stat.S_IRWXO):
                    warnings.append("key_file_mode")
            except OSError:
                pass
        return info, warnings

    def connection_hash(self, cfg: dict[str, Any]) -> str:
        try:
            secret = config.secret_access_key(cfg, self.secret_key)
        except config.Unreadable:
            secret = None
        cert_pem, chain_pem, key_pem = self.material_pems(cfg)
        return config.connection_hash(
            cfg, secret_plain=secret, cert_pem=cert_pem, chain_pem=chain_pem, key_pem=key_pem
        )

    def has_cert_and_key(self, cfg: dict[str, Any]) -> tuple[bool, bool]:
        cert_pem, _, key_pem = self.material_pems(cfg)
        return cert_pem is not None, key_pem is not None

    # ------------------------------------------------------------ clients
    def make_provider(self, cfg: dict[str, Any], http: Any) -> Any:
        mode = cfg.get("auth_mode")
        if mode == "access_key":
            try:
                secret = config.secret_access_key(cfg, self.secret_key)
            except config.Unreadable:
                raise AssistantError(
                    "aws_auth_failed",
                    "The stored secret access key cannot be decrypted (secret.key changed?); "
                    "enter it again.",
                ) from None
            if not cfg.get("access_key_id") or not secret:
                raise make("assistant_unconfigured")
            return StaticProvider(cfg["access_key_id"], secret)
        if mode == "roles_anywhere":
            return RolesAnywhereProvider(cfg, lambda: self.ra_material(cfg), http)
        if mode == "default_chain":
            return DefaultChainProvider()
        raise make("assistant_unconfigured")

    def build_client(self, cfg: dict[str, Any], *, report: bool = True) -> BedrockClient:
        """A client for `cfg` (Test connection uses its own with `report=False`)."""
        if not cfg.get("region") or not cfg.get("model_id"):
            raise make("assistant_unconfigured")
        http = tp.make_client(cfg)
        try:
            provider = self.make_provider(cfg, http)
        except Exception:
            self._spawn(http.aclose())
            raise
        creds = CachingProvider(provider)
        return BedrockClient(
            cfg,
            creds,
            http,
            on_success=self._on_success if report else None,
            on_error=self._on_error if report else None,
            on_retry=self._on_retry if report else None,
        )

    @property
    def client(self) -> BedrockClient:
        """The current generation's client (built on first use)."""
        if not self.enabled:
            raise make("assistant_unconfigured", aws="The assistant is turned off")
        if self._client is None:
            try:
                self._client = self.build_client(self.cfg)
            except AssistantError as err:
                self._record_error(err)
                raise
            self._creds = self._client.credentials
        return self._client

    # ------------------------------------------------------------ state
    def configured(self, cfg: dict[str, Any] | None = None) -> bool:
        cfg = cfg if cfg is not None else self.cfg
        if not cfg.get("region") or not cfg.get("model_id"):
            return False
        mode = cfg.get("auth_mode")
        if mode == "access_key":
            return bool(cfg.get("access_key_id") and cfg.get("secret_access_key_enc"))
        if mode == "roles_anywhere":
            has_cert = bool(cfg.get("ra_cert_pem") and cfg.get("ra_key_enc")) or bool(
                config.host_files()
            )
            return has_cert and all(
                cfg.get(f) for f in ("ra_trust_anchor_arn", "ra_profile_arn", "ra_role_arn")
            )
        return mode == "default_chain"

    def state(self) -> str:
        features = getattr(self.rt, "features", None)
        if features is None or not features.enabled("assistant"):
            return "disabled"
        if not self.configured():
            return "unconfigured"
        if self.state_error is not None:
            return "error"
        throttled = (
            self._last_throttle is not None
            and time.monotonic() - self._last_throttle < THROTTLE_DEGRADED_S
        )
        if throttled or self._flags["cert_expiring"] or self._flags["background_stopped"]:
            return "degraded"
        return "ready"

    def health_state(self) -> str:
        return self.state()

    def _record_error(self, err: AssistantError) -> None:
        if err.code == "bedrock_throttled":
            self._last_throttle = time.monotonic()
        if err.is_error_state:
            from netmon.services.serialize import iso

            self.state_error = {"code": err.code, "message": err.message,
                                "at": iso(datetime.now(UTC))}  # fmt: skip

    async def _on_error(self, err: AssistantError) -> None:
        self._record_error(err)
        await self._notify_state(err.code)

    async def _on_retry(self, err: AssistantError) -> None:
        """A retried error (throttling marks the state `degraded` for 5 min)."""
        if err.code == "bedrock_throttled":
            self._last_throttle = time.monotonic()
            await self._notify_state()

    async def _on_success(self) -> None:
        if self.state_error is not None:
            self.state_error = None
            await self._notify_state()

    async def _notify_state(self, code: str | None = None) -> None:
        new = self.state()
        old, self._last_state = self._last_state, new
        if new == old:
            return
        hub = getattr(self.rt, "hub", None)
        if hub is not None:
            hub.broadcast(
                "assistant.state",
                {"state": new, "error_code": (self.state_error or {}).get("code")},
                admin_only=True,
            )
        if {old, new} == {"ready", "error"} or (new == "error" and old != "error"):
            key = (self.state_error or {}).get("code") or code or new
            now = time.monotonic()
            last = self._event_marks.get(key)
            if last is None or now - last >= STATE_EVENT_EVERY_S:
                self._event_marks[key] = now
                msg = (
                    f"Assistant: {self.state_error['message']} ({key})"
                    if new == "error" and self.state_error
                    else "Assistant: connection to Amazon Bedrock works again"
                )
                self._spawn(self._system_event(msg))

    async def _system_event(self, message: str) -> None:
        from netmon.services import events as events_svc

        with contextlib.suppress(Exception):
            await events_svc.write_system_event(self.rt, message[:500])

    async def refresh_flags(self) -> None:
        """Certificate expiry and the background stop threshold (degraded state)."""
        if self.cfg.get("auth_mode") == "roles_anywhere":
            info, warns = self.cert_status(self.cfg)
            self._flags["cert_expiring"] = "cert_expiring" in warns
            today = datetime.now(UTC).date()
            if info and "cert_expiring" in warns and self.enabled and self._cert_event_day != today:
                self._cert_event_day = today
                self._spawn(
                    self._system_event(
                        f"Assistant: the Roles Anywhere certificate expires on {info['not_after']}"
                    )
                )
        else:
            self._flags["cert_expiring"] = False
        if self.enabled and self.rt.sessionmaker is not None:
            try:
                summary = await self.usage.month_summary()
                self._flags["background_stopped"] = bool(summary["background_stopped"])
            except Exception:  # noqa: BLE001
                log.debug("usage summary failed", exc_info=True)

    async def _flags_loop(self) -> None:
        while True:
            try:
                await self.refresh_flags()
                await self._notify_state()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Assistant state refresh failed")
            await asyncio.sleep(FLAGS_EVERY_S)

    # ------------------------------------------------------------ API shapes
    def warnings(self, cfg: dict[str, Any], cert_warnings: list[str]) -> list[str]:
        out = list(cert_warnings)
        try:
            endpoint = config.runtime_endpoint(cfg)
            if tp.is_vpce(endpoint) and tp.via(endpoint, cfg) != "direct":
                out.append("vpce_via_proxy")
        except (KeyError, TypeError):
            pass
        client = self._client
        if client is not None:
            if client.last_stream_buffered:
                out.append("stream_buffered")
            if client.caching_supported is False:
                out.append("cache_unsupported")
        created = cfg.get("access_key_created_at")
        if cfg.get("auth_mode") == "access_key" and created:
            try:
                age = (datetime.now(UTC).date() - date.fromisoformat(str(created)[:10])).days
                if age >= ACCESS_KEY_OLD_DAYS:
                    out.append("access_key_old")
            except ValueError:
                pass
        out += profiles.warnings_for([cfg.get("model_id"), cfg.get("model_id_light")])
        if cfg.get("auth_mode") == "roles_anywhere" and config.arn_account_warning(cfg):
            out.append("arn_accounts_differ")
        return list(dict.fromkeys(out))

    def settings_out(self, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg = cfg if cfg is not None else self.cfg
        features = getattr(self.rt, "features", None)
        info, cert_warns = self.cert_status(cfg)
        secret_set = bool(cfg.get("secret_access_key_enc"))
        prices = {**config.DEFAULT_ASSISTANT["prices"], **(cfg.get("prices") or {})}
        csr = None
        if cfg.get("ra_csr_pem"):
            csr = {
                "subject": cfg.get("ra_csr_subject"),
                "created_at": cfg.get("ra_csr_created_at"),
                "pem": cfg.get("ra_csr_pem"),
                "key_unreadable": config.secret_unreadable(
                    cfg, self.secret_key, "ra_csr_key_enc", config.AAD_RA_CSR_KEY
                ),
            }
        return {
            "enabled": bool(features is not None and features.enabled("assistant")),
            "state": self.state(),
            "state_error": self.state_error,
            "auth_mode": cfg.get("auth_mode"),
            "region": cfg.get("region"),
            "model_id": cfg.get("model_id") or "",
            "model_id_light": cfg.get("model_id_light"),
            "endpoint_url": cfg.get("endpoint_url"),
            "control_endpoint_url": cfg.get("control_endpoint_url"),
            "proxy_mode": cfg.get("proxy_mode"),
            "proxy_url": cfg.get("proxy_url"),
            "prompt_caching": bool(cfg.get("prompt_caching", True)),
            "access_key_id": cfg.get("access_key_id"),
            "secret_access_key": None,
            "secret_access_key_set": secret_set,
            "secret_access_key_unreadable": secret_set
            and config.secret_unreadable(
                cfg, self.secret_key, "secret_access_key_enc", config.AAD_SECRET_ACCESS_KEY
            ),
            "access_key_created_at": cfg.get("access_key_created_at"),
            "ra_region": cfg.get("ra_region"),
            "ra_endpoint_url": cfg.get("ra_endpoint_url"),
            "ra_trust_anchor_arn": cfg.get("ra_trust_anchor_arn"),
            "ra_profile_arn": cfg.get("ra_profile_arn"),
            "ra_role_arn": cfg.get("ra_role_arn"),
            "ra_session_seconds": cfg.get("ra_session_seconds"),
            "certificate": info,
            "certificate_host_files": config.host_files() is not None,
            "csr": csr,
            "send_config_excerpts": bool(cfg.get("send_config_excerpts")),
            "redact_internal_ips": bool(cfg.get("redact_internal_ips")),
            "internal_cidrs": list(cfg.get("internal_cidrs") or []),
            "chat_redact_ips": bool(cfg.get("chat_redact_ips", True)),
            "prices": prices,
            "last_test": cfg.get("last_test"),
            "warnings": self.warnings(cfg, cert_warns),
            # owner decision 3b: data-location explanation per inference-profile prefix
            "profile": profiles.classify(cfg.get("model_id")),
            "profile_light": profiles.classify(cfg.get("model_id_light")),
            "profile_scopes": profiles.SCOPES,
            "recommended": {
                "region": profiles.RECOMMENDED_REGION,
                "profile_prefix": profiles.RECOMMENDED_PREFIX,
            },
        }

    def kb_out(self, kb: dict[str, Any] | None = None) -> dict[str, Any]:
        kb = kb if kb is not None else self.kb
        features = getattr(self.rt, "features", None)
        return {
            "enabled": bool(features is not None and features.enabled("ai_knowledge")),
            "knowledge_base_id": kb.get("knowledge_base_id") or "",
            "region": kb.get("region"),
            "endpoint_url": kb.get("endpoint_url"),
            "results": kb.get("results"),
            "min_score": kb.get("min_score"),
            "description": kb.get("description") or "",
            "last_test": kb.get("last_test"),
        }
