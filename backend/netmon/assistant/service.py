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
first use or Test; turning `assistant` OFF drops the credentials and closes the clients.

[4] PR 4-0a: the credentials come from the shared AWS connection the assistant uses
(`cfg["connection_id"]`, null = the default) and its access (`direct` / `assume_role` /
`ra_role`) through `rt.aws.chain("assistant", view, access)`; `self.view` is the resolved
connection view (None = no connection → `unconfigured`)."""

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

from netmon.assistant import config, profiles
from netmon.assistant.bedrock import BedrockClient
from netmon.assistant.usage import UsageService
from netmon.aws import certs
from netmon.aws import connections as conns
from netmon.aws import transport as tp
from netmon.aws.credentials import CachingProvider, RaMaterial
from netmon.aws.errors import AssistantError, make
from netmon.services.testids import TestRegistry

log = logging.getLogger("netmon.assistant")

THROTTLE_DEGRADED_S = 300.0
STATE_EVENT_EVERY_S = 3600.0
FLAGS_EVERY_S = 300.0
OLD_CLIENT_GRACE_S = 300.0


class AssistantService:
    features = ("assistant", "ai_knowledge")

    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.usage = UsageService(rt)
        aws = getattr(rt, "aws", None)
        # [4] one registry for every configure-first test (shared rate limit)
        self.tests = aws.tests if aws is not None else TestRegistry()
        self.cfg: dict[str, Any] = copy.deepcopy(config.DEFAULT_ASSISTANT)
        self.view: dict[str, Any] | None = None  # [4] the resolved AWS connection view
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
            self.view = await self.resolve_view(session, self.cfg)

    async def resolve_view(self, session: Any, cfg: dict[str, Any]) -> dict[str, Any] | None:
        """[4] The connection view `cfg` uses (None: no connection / table not there yet)."""
        try:
            row = await conns.resolve(session, cfg.get("connection_id"))
        except Exception:  # noqa: BLE001 - e.g. an older schema during start-up
            log.debug("AWS connection lookup failed", exc_info=True)
            return None
        return conns.view(row) if row is not None else None

    async def reload(self) -> None:
        """Settings, the AWS connection (or its certificate) changed: re-read them and start a
        new client generation (the old client is closed after a grace period)."""
        before = self._conn_view(self.cfg, self.view)
        await self.load()
        if self._conn_view(self.cfg, self.view) != before:
            await self._drop_client()
            self.state_error = None
        await self.refresh_flags()
        await self._notify_state()

    @staticmethod
    def _conn_view(cfg: dict[str, Any], view: dict[str, Any] | None) -> dict[str, Any]:
        out: dict[str, Any] = {k: cfg.get(k) for k in config.BEDROCK_FIELDS}
        out["access"] = conns.normalize_access(cfg.get("access"))
        out["view"] = {k: v for k, v in (view or {}).items() if k != "_last_test"}
        return out

    async def _drop_client(self) -> None:
        client, creds = self._client, self._creds
        self._client, self._creds = None, None
        self.generation += 1
        # [4] the credential chain belongs to rt.aws (shared, keyed by the connection hash):
        # it is not cleared here
        del creds
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
    def _legacy_view(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """A 1.4.x `assistant` object read as a connection view (same AADs: scope
        `assistant`; host files apply as they did to the assistant)."""
        return {**cfg, "_aad_scope": conns.LEGACY_SCOPE, "_is_default": True}

    def ra_material(self, cfg: dict[str, Any]) -> RaMaterial:
        """1.4.x shape (kept for the rollback tests): the material of a 1.4.x object."""
        return conns.ra_material(self._legacy_view(cfg), self.secret_key)

    def load_material(self, cfg: dict[str, Any]) -> certs.Material | None:
        return conns.load_material(self._legacy_view(cfg), self.secret_key)

    def cert_status_view(self, v: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """(CertInfo | None, warnings) of a connection view."""
        warnings: list[str] = []
        files = conns.uses_host_files(v)
        try:
            mat = conns.load_material(v, self.secret_key)
        except (OSError, certs.CertError, ValueError) as exc:
            log.warning("Roles Anywhere host files unusable: %s", type(exc).__name__)
            return None, warnings
        if mat is None:
            return None, warnings
        info = certs.cert_info(
            mat.cert,
            mat.chain,
            source="files" if files else "uploaded",
            uploaded_at=None if files else v.get("ra_cert_uploaded_at"),
            key_unreadable=mat.key is None,
        )
        if certs.expiring(mat.cert, datetime.now(UTC)):
            warnings.append("cert_expiring")
        if files and os.name == "posix":
            hf = conns.host_files() or {}
            try:
                mode = os.stat(hf["key"]).st_mode
                if mode & (stat.S_IRWXG | stat.S_IRWXO):
                    warnings.append("key_file_mode")
            except (OSError, KeyError):
                pass
        return info, warnings

    def connection_hash(
        self, cfg: dict[str, Any], view: dict[str, Any] | None | bool = False
    ) -> str:
        """[4] The assistant's test binding (`config.assistant_hash`) for `cfg` and the
        connection view it uses (default: the loaded one)."""
        v = self.view if view is False else view
        if v is None or v is True:
            return config.assistant_hash(cfg, None, None)
        assert isinstance(v, dict)
        return config.assistant_hash(cfg, v.get("_id"), conns.connection_hash(v, self.secret_key))

    # ------------------------------------------------------------ clients
    def make_provider(self, cfg: dict[str, Any], http: Any) -> Any:
        """1.4.x shape (the "1.4.x reader" of the rollback tests): the provider a 1.4.x image
        builds from the legacy keys of the `assistant` row."""
        return conns.make_provider(
            self._legacy_view(cfg), http, self.secret_key, unconfigured="assistant_unconfigured"
        )

    def bedrock_cfg(self, cfg: dict[str, Any], view: dict[str, Any] | None) -> dict[str, Any]:
        """The Bedrock settings + the proxy of the connection (architecture § 15.1.3)."""
        v = view or {}
        return {**cfg, "proxy_mode": v.get("proxy_mode") or "env", "proxy_url": v.get("proxy_url")}

    def build_client(
        self,
        cfg: dict[str, Any],
        *,
        report: bool = True,
        view: dict[str, Any] | None | bool = False,
        credentials: CachingProvider | None = None,
    ) -> BedrockClient:
        """A client for `cfg` and its connection (`view`, default: the loaded one). The
        credentials are the assistant's chain from `rt.aws` unless given (Test)."""
        v = self.view if view is False else view
        if not cfg.get("region") or not cfg.get("model_id"):
            raise make("assistant_unconfigured")
        if not isinstance(v, dict):
            raise make("assistant_unconfigured", aws="No AWS connection (Settings → AWS)")
        if credentials is None:
            if not conns.configured(v):
                raise make("assistant_unconfigured")
            try:
                credentials = self.rt.aws.chain("assistant", v, cfg.get("access") or {})
            except AssistantError as err:
                if err.code == "aws_connection_unconfigured":
                    raise make("assistant_unconfigured") from None
                raise
        bcfg = self.bedrock_cfg(cfg, v)
        http = tp.make_client(bcfg)
        return BedrockClient(
            bcfg,
            credentials,
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
        return conns.configured(self.view)

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
        """Certificate expiry (of every `roles_anywhere` connection) and the background stop
        threshold (degraded state)."""
        expiring: list[tuple[str, str]] = []
        own = False
        if self.rt.sessionmaker is not None:
            try:
                async with self.rt.sessionmaker() as session:
                    rows = await conns.list_rows(session)
            except Exception:  # noqa: BLE001
                rows = []
            for row in rows:
                v = conns.view(row)
                if v.get("auth_mode") != "roles_anywhere":
                    continue
                info, warns = self.cert_status_view(v)
                if info and "cert_expiring" in warns:
                    expiring.append((str(row["name"]), str(info["not_after"])))
                    if self.view is not None and self.view.get("_id") == row["id"]:
                        own = True
        self._flags["cert_expiring"] = own
        today = datetime.now(UTC).date()
        if expiring and self.enabled and self._cert_event_day != today:
            self._cert_event_day = today
            for name, not_after in expiring:
                self._spawn(
                    self._system_event(
                        f"AWS connection {name}: the Roles Anywhere certificate expires on "
                        f"{not_after}"
                    )
                )
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
    def warnings(self, cfg: dict[str, Any], view: dict[str, Any] | None = None) -> list[str]:
        """The Bedrock warnings (certificate / key warnings belong to the connection)."""
        out: list[str] = []
        v = view if view is not None else self.view
        try:
            endpoint = config.runtime_endpoint(cfg)
            if tp.is_vpce(endpoint) and tp.via(endpoint, self.bedrock_cfg(cfg, v)) != "direct":
                out.append("vpce_via_proxy")
        except (KeyError, TypeError):
            pass
        client = self._client
        if client is not None:
            if client.last_stream_buffered:
                out.append("stream_buffered")
            if client.caching_supported is False:
                out.append("cache_unsupported")
        out += profiles.warnings_for([cfg.get("model_id"), cfg.get("model_id_light")])
        return list(dict.fromkeys(out))

    def connection_out(self) -> dict[str, Any] | None:
        """`connection` of the settings (read-only): `{id, name, auth_mode, is_default,
        account, arn, state}`."""
        v = self.view
        if v is None:
            return None
        last = v.get("_last_test") or {}
        st = (
            self.rt.aws.cache.consumer_status("assistant")
            if getattr(self.rt, "aws", None)
            else None
        )
        hops = (st or {}).get("hops") or []
        account = arn = None
        if hops:
            account, arn = hops[-1].get("account"), hops[-1].get("arn")
        state = (st or {}).get("state") or "idle"
        return {
            "id": v.get("_id"),
            "name": v.get("_name"),
            "auth_mode": v.get("auth_mode"),
            "is_default": bool(v.get("_is_default")),
            "account": account or last.get("account"),
            "arn": arn or last.get("arn"),
            "state": state,
        }

    def aws_status(self) -> dict[str, Any]:
        """`GET /api/assistant/status` → `aws` (architecture § 15.1.5)."""
        v = self.view or {}
        st = (
            self.rt.aws.cache.consumer_status("assistant")
            if getattr(self.rt, "aws", None)
            else None
        )
        access = conns.normalize_access(self.cfg.get("access"))
        last_error = None
        if st and st.get("state") == "error":
            last_error = {"code": st.get("error_code"), "message": None, "at": None}
            if self.state_error and self.state_error.get("code") == st.get("error_code"):
                last_error = dict(self.state_error)
        elif self.state_error:
            last_error = dict(self.state_error)
        return {
            "connection_id": v.get("_id"),
            "connection_name": v.get("_name"),
            "access_mode": access["mode"],
            "hops": (st or {}).get("hops") or [],
            "last_error": last_error,
        }

    def settings_out(self, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg = cfg if cfg is not None else self.cfg
        features = getattr(self.rt, "features", None)
        prices = {**config.DEFAULT_ASSISTANT["prices"], **(cfg.get("prices") or {})}
        return {
            "enabled": bool(features is not None and features.enabled("assistant")),
            "state": self.state(),
            "state_error": self.state_error,
            "region": cfg.get("region"),
            "model_id": cfg.get("model_id") or "",
            "model_id_light": cfg.get("model_id_light"),
            "endpoint_url": cfg.get("endpoint_url"),
            "control_endpoint_url": cfg.get("control_endpoint_url"),
            "prompt_caching": bool(cfg.get("prompt_caching", True)),
            # [4] the shared AWS connection and the access (architecture § 15.1.6)
            "connection_id": cfg.get("connection_id"),
            "access": conns.normalize_access(cfg.get("access")),
            "connection": self.connection_out(),
            "send_config_excerpts": bool(cfg.get("send_config_excerpts")),
            "redact_internal_ips": bool(cfg.get("redact_internal_ips")),
            "internal_cidrs": list(cfg.get("internal_cidrs") or []),
            "chat_redact_ips": bool(cfg.get("chat_redact_ips", True)),
            "prices": prices,
            "chat_models": [
                {"id": m.get("id"), "label": m.get("label")}
                for m in (cfg.get("chat_models") or [])
                if isinstance(m, dict)
            ],
            "chat_models_effective": config.chat_models_effective(cfg),
            "last_test": cfg.get("last_test"),
            "warnings": self.warnings(cfg),
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
