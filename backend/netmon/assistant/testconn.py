"""[3b] Test connection (architecture § 14.2.6) and the configure-first `test_id`s.

Steps (45 s budget): [4] the chain steps of the assistant's AWS connection and access
(`netmon.aws.testconn`: `config` → `credentials` → `identity` → [`hop` → `hop_identity`]) →
`endpoint` (TCP + TLS through the connection's proxy path) → `converse` (one ConverseStream
with a dummy `ping` tool) [→ `knowledge`: one Retrieve]. Every step carries `hop` and `hints`.
A success issues a `test_id` (128-bit, 30 min, in memory) bound to the admin's user id and the
SHA-256 of the connection settings (`config.assistant_hash`); enabling (or changing a
connection field while ON) needs a matching one. The registry moved to
`netmon.services.testids` (re-exported here)."""

from __future__ import annotations

import asyncio
import ssl
import time
from typing import TYPE_CHECKING, Any

import httpx

from netmon.assistant import config, profiles
from netmon.aws import testconn as awstest
from netmon.aws import transport as tp
from netmon.aws.credentials import CachingProvider
from netmon.aws.errors import AssistantError, hints_for, make, map_exception
from netmon.services.testids import RATE_LIMIT, RATE_WINDOW_S, TEST_TTL, TestRegistry

__all__ = ["RATE_LIMIT", "RATE_WINDOW_S", "TEST_TTL", "TestRegistry", "run_kb_test", "run_test"]

if TYPE_CHECKING:
    from netmon.assistant.service import AssistantService

TEST_BUDGET_S = 45.0

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


def _step(name: str, ok: bool, detail: str, t0: float, code: str | None = None) -> dict[str, Any]:
    return awstest.step(name, None, ok, detail, t0, code)


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
    svc: AssistantService,
    cfg: dict[str, Any],
    *,
    user_id: int,
    view: dict[str, Any] | None | bool = False,
    purpose_user: int | None = None,
) -> dict[str, Any]:
    """Run the steps for the effective (unsaved) settings `cfg` and the connection view it uses
    (default: the loaded one). Never raises."""
    v = svc.view if view is False else view
    v = v if isinstance(v, dict) else None
    bcfg = svc.bedrock_cfg(cfg, v)
    endpoint = config.runtime_endpoint(cfg) if cfg.get("region") else ""
    result: dict[str, Any] = {
        "ok": False,
        "test_id": None,
        "expires_at": None,
        "steps": [],
        "via": tp.via(endpoint, bcfg) if endpoint else "direct",
        "endpoint_host": tp.host_of(endpoint) if endpoint else "",
        "address_class": None,
        "model_id": cfg.get("model_id") or "",
        "time_to_first_event_ms": None,
        "usage": None,
        "warnings": [],
        "error": None,
    }
    try:
        await asyncio.wait_for(_steps(svc, cfg, v, result, user_id), TEST_BUDGET_S)
    except TimeoutError:
        err = make("assistant_timeout")
        result["error"] = {"code": err.code, "message": "The test took longer than 45 s."}
        result["ok"] = False
    if result["ok"]:
        tid, exp = svc.tests.issue("assistant", user_id, svc.connection_hash(cfg, v))
        from netmon.services.serialize import iso

        result["test_id"], result["expires_at"] = tid, iso(exp)
    if endpoint and tp.is_vpce(endpoint) and result["via"] != "direct":
        result["warnings"].append("vpce_via_proxy")
    result["warnings"] += profiles.warnings_for([cfg.get("model_id"), cfg.get("model_id_light")])
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    return result


def _fail(result: dict[str, Any], step: dict[str, Any], err: AssistantError) -> None:
    step["ok"] = False
    step["error_code"] = err.code
    step["detail"] = err.message[:500]
    result["steps"].append(step)
    result["error"] = {"code": err.code, "message": err.message}


async def _steps(
    svc: AssistantService,
    cfg: dict[str, Any],
    v: dict[str, Any] | None,
    result: dict[str, Any],
    user_id: int,
) -> None:
    # 1. config (Bedrock fields + the connection's fields and access)
    t0 = time.monotonic()
    errs = config.settings_field_errors(cfg, test_mode=svc.test_mode)
    errs += config.settings_required_errors(cfg)
    if v is None:
        errs.append({"loc": ["body", "connection_id"], "msg": "no AWS connection (Settings → AWS)"})
    if errs:
        detail = "; ".join(f"{e['loc'][-1]}: {e['msg']}" for e in errs)
        result["steps"].append(_step("config", False, detail, t0, "validation_error"))
        result["error"] = {"code": "validation_error", "message": f"Settings incomplete: {detail}"}
        return
    assert v is not None
    # 1-5. the chain of the connection (+ the hop of the access)
    run = await awstest.run_chain(
        v, [cfg.get("access") or {}], secret_key=svc.secret_key, test_mode=svc.test_mode,
        consumer="assistant",
    )  # fmt: skip
    result["steps"] += run.steps
    result["warnings"] += run.warnings
    if run.error is not None or run.creds is None:
        result["error"] = run.error
        return
    ident = run.hop_identity or run.identity or {}
    creds = CachingProvider(awstest.FixedProvider(run.creds))
    try:
        client = svc.build_client(cfg, report=False, view=v, credentials=creds)
    except AssistantError as err:
        _fail(result, _step("endpoint", False, "", time.monotonic()), err)
        return
    try:
        # 6. endpoint
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
        # 7. converse
        t0 = time.monotonic()
        try:
            asm, end = await client.converse_stream_collect(PING_REQUEST)
        except AssistantError as err:
            st = _step("converse", False, "", t0)
            st["hints"] = hints_for(
                "converse",
                err,
                {"bedrock_region": cfg.get("region"), "account": ident.get("account")},  # fmt: skip
            )
            _fail(result, st, err)
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
