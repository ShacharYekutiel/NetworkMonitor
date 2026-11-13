"""[3b] Test connection (architecture § 14.2.6) and the configure-first `test_id`s.

Steps (45 s budget): `config` → `credentials` → `endpoint` (TCP + TLS through the configured
path) → `converse` (one ConverseStream with a dummy `ping` tool) [→ `knowledge`: one Retrieve].
A success issues a `test_id` (128-bit, 30 min, in memory) bound to the admin's user id and the
SHA-256 of the connection settings; enabling (or changing a connection field while ON) needs
a matching one."""

from __future__ import annotations

import asyncio
import secrets
import ssl
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from netmon.assistant import config, profiles
from netmon.assistant import transport as tp
from netmon.assistant.errors import AssistantError, make, map_exception

if TYPE_CHECKING:
    from netmon.assistant.service import AssistantService

TEST_TTL = timedelta(minutes=30)
TEST_BUDGET_S = 45.0
RATE_LIMIT = 10
RATE_WINDOW_S = 600.0

PING_REQUEST: dict[str, Any] = {
    "messages": [
        {"role": "user", "content": [{"text": "Connection test. Call the ping tool once."}]}
    ],
    "toolConfig": {
        "tools": [
            {
                "toolSpec": {
                    "name": "ping",
                    "description": "Connection test. Call it once with no arguments.",
                    "inputSchema": {
                        "json": {"type": "object", "properties": {}, "additionalProperties": False}
                    },
                }
            }
        ],
        "toolChoice": {"tool": {"name": "ping"}},
    },
    "inferenceConfig": {"maxTokens": 50},
}


class TestRegistry:
    """test_ids (in memory) and the per-admin rate limit (10 tests per 10 min)."""

    def __init__(self) -> None:
        self._ids: dict[str, tuple[str, int, str, datetime]] = {}
        self._hits: dict[int, deque[float]] = {}

    def issue(
        self, kind: str, user_id: int, conn_hash: str, now: datetime | None = None
    ) -> tuple[str, datetime]:
        now = now or datetime.now(UTC)
        self._purge(now)
        tid = secrets.token_hex(16)
        exp = now + TEST_TTL
        self._ids[tid] = (kind, user_id, conn_hash, exp)
        return tid, exp

    def valid(
        self,
        kind: str,
        test_id: str | None,
        user_id: int,
        conn_hash: str,
        now: datetime | None = None,
    ) -> bool:
        if not test_id:
            return False
        now = now or datetime.now(UTC)
        entry = self._ids.get(test_id)
        if entry is None:
            return False
        k, uid, h, exp = entry
        return k == kind and uid == user_id and secrets.compare_digest(h, conn_hash) and now < exp

    def consume(self, test_id: str) -> None:
        self._ids.pop(test_id, None)

    def _purge(self, now: datetime) -> None:
        for k in [k for k, v in self._ids.items() if v[3] <= now]:
            del self._ids[k]

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        q = self._hits.setdefault(user_id, deque())
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return False
        q.append(now)
        return True

    def clear(self) -> None:
        self._ids.clear()
        self._hits.clear()


def _step(name: str, ok: bool, detail: str, t0: float, code: str | None = None) -> dict[str, Any]:
    return {
        "step": name,
        "ok": ok,
        "detail": detail[:500],
        "ms": int((time.monotonic() - t0) * 1000),
        "error_code": code,
    }


async def probe_endpoint(http: httpx.AsyncClient, url: str) -> tuple[str | None]:
    """TCP + TLS (+ proxy) to the endpoint: any HTTP answer proves the path. Returns the TLS
    issuer (CN / O) when it can be read."""
    resp = await http.get(f"{url}/ping")
    issuer = None
    stream = resp.extensions.get("network_stream")
    if stream is not None:
        try:
            sslobj = stream.get_extra_info("ssl_object")
            if isinstance(sslobj, ssl.SSLObject | ssl.SSLSocket):
                cert = sslobj.getpeercert() or {}
                parts = dict(x[0] for x in cert.get("issuer", ()) if x)
                issuer = parts.get("commonName") or parts.get("organizationName")
        except Exception:  # noqa: BLE001
            issuer = None
    return (issuer,)


async def run_test(
    svc: AssistantService, cfg: dict[str, Any], *, user_id: int, purpose_user: int | None = None
) -> dict[str, Any]:
    """Run the steps for the effective (unsaved) settings `cfg`. Never raises."""
    endpoint = config.runtime_endpoint(cfg) if cfg.get("region") else ""
    result: dict[str, Any] = {
        "ok": False,
        "test_id": None,
        "expires_at": None,
        "steps": [],
        "via": tp.via(endpoint, cfg) if endpoint else "direct",
        "endpoint_host": tp.host_of(endpoint) if endpoint else "",
        "address_class": None,
        "model_id": cfg.get("model_id") or "",
        "time_to_first_event_ms": None,
        "usage": None,
        "warnings": [],
        "error": None,
    }
    try:
        await asyncio.wait_for(_steps(svc, cfg, result, user_id), TEST_BUDGET_S)
    except TimeoutError:
        err = make("assistant_timeout")
        result["error"] = {"code": err.code, "message": "The test took longer than 45 s."}
    if result["ok"]:
        tid, exp = svc.tests.issue("assistant", user_id, svc.connection_hash(cfg))
        from netmon.services.serialize import iso

        result["test_id"], result["expires_at"] = tid, iso(exp)
    if endpoint and tp.is_vpce(endpoint) and result["via"] != "direct":
        result["warnings"].append("vpce_via_proxy")
    result["warnings"] += profiles.warnings_for([cfg.get("model_id"), cfg.get("model_id_light")])
    if cfg.get("auth_mode") == "roles_anywhere" and config.arn_account_warning(cfg):
        result["warnings"].append("arn_accounts_differ")
    if config.temporary_credentials(cfg):
        # temporary credentials expire within hours: fine for a trial, not for the server
        result["warnings"].append("temporary_credentials")
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    return result


def _fail(result: dict[str, Any], step: dict[str, Any], err: AssistantError) -> None:
    step["ok"] = False
    step["error_code"] = err.code
    step["detail"] = err.message[:500]
    result["steps"].append(step)
    result["error"] = {"code": err.code, "message": err.message}


async def _steps(
    svc: AssistantService, cfg: dict[str, Any], result: dict[str, Any], user_id: int
) -> None:
    # 1. config
    t0 = time.monotonic()
    errs = config.field_errors(cfg, test_mode=svc.test_mode)
    has_cert, has_key = svc.has_cert_and_key(cfg)
    errs += config.required_errors(cfg, svc.secret_key, has_cert=has_cert, has_key=has_key)
    if errs:
        detail = "; ".join(f"{e['loc'][-1]}: {e['msg']}" for e in errs)
        step = _step("config", False, detail, t0, "validation_error")
        result["steps"].append(step)
        result["error"] = {"code": "validation_error", "message": f"Settings incomplete: {detail}"}
        return
    result["steps"].append(
        _step("config", True, f"auth {cfg['auth_mode']}, region {cfg['region']}", t0)
    )
    # 2. credentials
    t0 = time.monotonic()
    try:
        client = svc.build_client(cfg, report=False)
    except AssistantError as err:
        _fail(result, _step("credentials", False, "", t0), err)
        return
    try:
        try:
            creds = await client.credentials.get()
        except AssistantError as err:
            _fail(result, _step("credentials", False, "", t0), err)
            return
        parts = [f"source {creds.source}"]
        if creds.expiration:
            from netmon.services.serialize import iso

            parts.append(f"expires {iso(creds.expiration)}")
        if creds.detail.get("subject_arn"):
            parts.append(f"subject {creds.detail['subject_arn']}")
        if creds.detail.get("role"):
            parts.append(f"role {creds.detail['role']}")
        result["steps"].append(_step("credentials", True, ", ".join(parts), t0))
        # 3. endpoint
        t0 = time.monotonic()
        endpoint = client.endpoint
        result["address_class"] = await asyncio.to_thread(tp.address_class, tp.host_of(endpoint))
        try:
            (issuer,) = await asyncio.wait_for(probe_endpoint(client.http, endpoint), 15)
        except (httpx.HTTPError, TimeoutError) as exc:
            err = map_exception(exc, host=tp.host_of(endpoint), via=result["via"])
            if err.code == "assistant_timeout":
                err = make("aws_network", host=tp.host_of(endpoint), aws="connection timed out")
                err.details = [{"host": tp.host_of(endpoint), "via": result["via"]}]
            _fail(result, _step("endpoint", False, "", t0), err)
            return
        detail = f"{tp.host_of(endpoint)} via {result['via']}"
        if result["address_class"]:
            detail += f", address {result['address_class']}"
        if issuer:
            detail += f", TLS issuer {issuer}"
        result["steps"].append(_step("endpoint", True, detail, t0))
        # 4. converse
        t0 = time.monotonic()
        try:
            asm, end = await client.converse_stream_collect(PING_REQUEST)
        except AssistantError as err:
            _fail(result, _step("converse", False, "", t0), err)
            await _count(svc, user_id, None, err.code, getattr(client, "model_id", None))
            return
        usage = asm.usage
        await _count(svc, user_id, usage, None, getattr(client, "model_id", None))
        result["time_to_first_event_ms"] = end.get("time_to_first_event_ms")
        result["usage"] = {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
        }
        if end.get("buffered"):
            result["warnings"].append("stream_buffered")
        if client.caching_supported is False:
            result["warnings"].append("cache_unsupported")
        called = any(t.get("name") == "ping" for t in asm.tool_uses)
        detail = (
            f"model {end.get('model_id')}, first event {end.get('time_to_first_event_ms')} ms, "
            f"tool use {'ok' if called else 'not used'}, tokens {usage['input_tokens']}/"
            f"{usage['output_tokens']}"
        )
        result["steps"].append(_step("converse", True, detail, t0))
        result["ok"] = True
    finally:
        await client.aclose()


async def _count(
    svc: AssistantService,
    user_id: int,
    usage: dict[str, Any] | None,
    code: str | None,
    model_id: str | None = None,
) -> None:
    try:
        if code:
            await svc.usage.record_error(user_id, "test", code, model_id)
        else:
            await svc.usage.record(user_id, "test", usage or {}, model_calls=1, model_id=model_id)
    except Exception:  # noqa: BLE001 - usage bookkeeping never fails a test
        pass


async def run_kb_test(svc: AssistantService, kb: dict[str, Any], *, user_id: int) -> dict[str, Any]:
    """Knowledge Base test: one Retrieve with the query "test" through the assistant
    connection. Never raises."""
    t0 = time.monotonic()
    out: dict[str, Any] = {
        "ok": False,
        "test_id": None,
        "expires_at": None,
        "results": 0,
        "first_source": None,
        "ms": 0,
        "error": None,
    }
    errs = config.kb_field_errors(kb, test_mode=svc.test_mode)
    if not kb.get("knowledge_base_id"):
        errs.append({"loc": ["body", "knowledge_base_id"], "msg": "required"})
    if errs:
        out["error"] = {
            "code": "validation_error",
            "message": "; ".join(f"{e['loc'][-1]}: {e['msg']}" for e in errs),
        }
        return out
    try:
        client = svc.client
        passages = await asyncio.wait_for(client.retrieve("test", kb), TEST_BUDGET_S)
    except (AssistantError, TimeoutError) as exc:
        err = exc if isinstance(exc, AssistantError) else make("assistant_timeout")
        out["error"] = {"code": "kb_unavailable", "message": f"{err.message} ({err.code})"}
        out["ms"] = int((time.monotonic() - t0) * 1000)
        return out
    out["ok"] = True
    out["results"] = len(passages)
    if passages:
        out["first_source"] = {"title": passages[0]["title"], "url": passages[0]["source_url"]}
    out["ms"] = int((time.monotonic() - t0) * 1000)
    h = config.kb_connection_hash(kb, svc.connection_hash(svc.cfg))
    tid, exp = svc.tests.issue("kb", user_id, h)
    from netmon.services.serialize import iso

    out["test_id"], out["expires_at"] = tid, iso(exp)
    return out
