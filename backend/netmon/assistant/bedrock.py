"""[3b] Amazon Bedrock client (architecture § 14.2): Converse, ConverseStream, Retrieve,
List models. httpx + our own SigV4 (no boto3/botocore at runtime).

Interface for the agent loop and the AI features (`rt.assistant.client`):

    resp = await client.converse(request, light=True)        # non-streamed, ≤ 90 s
    resp["output"]["message"], resp["stopReason"], resp["usage"]   # Bedrock shapes
    resp["_meta"] == {"model_id", "retries": [codes], "duration_ms", "caching": bool}

    async for ev in client.converse_stream(request):          # ev = {"type", "data"}
        # Bedrock events: messageStart, contentBlockStart, contentBlockDelta,
        # contentBlockStop, messageStop, metadata (data = the event payload) plus synthetic
        #   {"type": "retrying", "data": {"code", "attempt", "delay_s"}}  (before any text)
        #   {"type": "stream_end", "data": {"time_to_first_event_ms", "duration_ms",
        #                                   "buffered", "model_id", "retries"}}
    asm = StreamAssembler(); asm.feed(ev) ... asm.message / asm.stop_reason / asm.usage

    passages = await client.retrieve(query, kb_cfg)           # Knowledge Base
    models = await client.list_models()                       # control plane (optional)

`request` is a Converse body without `modelId` (`system`, `messages`, `toolConfig`,
`inferenceConfig`); `cachePoint` blocks are removed automatically when prompt caching is off
or the model rejected them (then remembered for this settings generation). Errors raise
`AssistantError` (pure mapping: `map_error`, re-exported here). Breaking out of the stream
iterator (or cancelling the task) closes the HTTP stream to Bedrock at once."""

from __future__ import annotations

import asyncio
import copy
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from netmon.assistant import config, sigv4
from netmon.assistant import transport as tp
from netmon.assistant.credentials import CachingProvider
from netmon.assistant.errors import (
    AssistantError,
    make,
    map_error,
    map_exception,
    map_stream_exception,
)
from netmon.assistant.eventstream import Decoder, EventStreamError

__all__ = ["BedrockClient", "StreamAssembler", "map_error", "usage_of", "strip_cache_points"]

SERVICE = "bedrock"
THROTTLE_DELAYS = (1.0, 4.0)
UNAVAILABLE_DELAY = 2.0
BUFFERED_AFTER_S = 3.0

Hook = Callable[..., Awaitable[None] | None]


def usage_of(usage: dict[str, Any] | None) -> dict[str, int]:
    """Bedrock `usage` → our counter names."""
    u = usage or {}
    return {
        "input_tokens": int(u.get("inputTokens") or 0),
        "output_tokens": int(u.get("outputTokens") or 0),
        "cache_read_tokens": int(
            u.get("cacheReadInputTokens") or u.get("cacheReadInputTokenCount") or 0
        ),
        "cache_write_tokens": int(
            u.get("cacheWriteInputTokens") or u.get("cacheWriteInputTokenCount") or 0
        ),
    }


def _has_cache_point(blocks: Any) -> bool:
    return isinstance(blocks, list) and any(
        isinstance(b, dict) and "cachePoint" in b for b in blocks
    )


def has_cache_points(request: dict[str, Any]) -> bool:
    if _has_cache_point(request.get("system")):
        return True
    if _has_cache_point((request.get("toolConfig") or {}).get("tools")):
        return True
    return any(_has_cache_point(m.get("content")) for m in request.get("messages") or [])


def strip_cache_points(request: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(request)

    def strip(blocks: Any) -> Any:
        if not isinstance(blocks, list):
            return blocks
        return [b for b in blocks if not (isinstance(b, dict) and "cachePoint" in b)]

    if "system" in out:
        out["system"] = strip(out["system"])
    if isinstance(out.get("toolConfig"), dict) and "tools" in out["toolConfig"]:
        out["toolConfig"]["tools"] = strip(out["toolConfig"]["tools"])
    for m in out.get("messages") or []:
        if isinstance(m, dict) and "content" in m:
            m["content"] = strip(m["content"])
    return out


def _is_cache_rejection(err: AssistantError) -> bool:
    return err.code == "bedrock_validation" and "cache" in err.message.lower()


def _is_sampling_rejection(err: AssistantError, request: dict[str, Any]) -> bool:
    """Newer Claude models reject sampling parameters (`temperature`, `topP`)."""
    ic = request.get("inferenceConfig") or {}
    if "temperature" not in ic and "topP" not in ic:
        return False
    msg = err.message.lower()
    return err.code == "bedrock_validation" and any(
        w in msg for w in ("temperature", "top_p", "topp", "sampling")
    )


def _is_forced_tool_rejection(err: AssistantError, request: dict[str, Any]) -> bool:
    """Newer Claude models reject a forced `toolChoice` (`tool` / `any`)."""
    choice = (request.get("toolConfig") or {}).get("toolChoice") or {}
    if "tool" not in choice and "any" not in choice:
        return False
    msg = err.message.lower().replace("_", "")
    return err.code == "bedrock_validation" and "toolchoice" in msg


def _jitter(delay: float) -> float:
    return delay * random.uniform(0.8, 1.2)  # noqa: S311 - retry jitter


class StreamAssembler:
    """Builds the assistant message of a ConverseStream: text blocks, toolUse blocks (input JSON
    assembled from the deltas), stop reason, usage, metrics."""

    def __init__(self) -> None:
        self.blocks: dict[int, dict[str, Any]] = {}
        self._tool_json: dict[int, list[str]] = {}
        self.stop_reason: str | None = None
        self.raw_usage: dict[str, Any] = {}
        self.metrics: dict[str, Any] = {}
        self.text_parts: list[str] = []

    def feed(self, event: dict[str, Any]) -> str | None:
        """Consume one event; returns the text delta (if any)."""
        t, d = event.get("type"), event.get("data") or {}
        if t == "contentBlockStart":
            idx = int(d.get("contentBlockIndex", 0))
            start = d.get("start") or {}
            if "toolUse" in start:
                tu = start["toolUse"]
                self.blocks[idx] = {
                    "toolUse": {"toolUseId": tu.get("toolUseId"), "name": tu.get("name"),
                                "input": {}}
                }  # fmt: skip
                self._tool_json[idx] = []
        elif t == "contentBlockDelta":
            idx = int(d.get("contentBlockIndex", 0))
            delta = d.get("delta") or {}
            if "text" in delta:
                block = self.blocks.setdefault(idx, {"text": ""})
                if "text" not in block:
                    block["text"] = ""
                block["text"] += delta["text"]
                self.text_parts.append(delta["text"])
                return str(delta["text"])
            if "toolUse" in delta:
                self._tool_json.setdefault(idx, []).append(str(delta["toolUse"].get("input", "")))
                self.blocks.setdefault(idx, {"toolUse": {"toolUseId": None, "name": None,
                                                         "input": {}}})  # fmt: skip
        elif t == "contentBlockStop":
            idx = int(d.get("contentBlockIndex", 0))
            self._finish_tool(idx)
        elif t == "messageStop":
            self.stop_reason = d.get("stopReason")
        elif t == "metadata":
            self.raw_usage = d.get("usage") or {}
            self.metrics = d.get("metrics") or {}
        return None

    def _finish_tool(self, idx: int) -> None:
        if idx not in self._tool_json or "toolUse" not in self.blocks.get(idx, {}):
            return
        raw = "".join(self._tool_json.pop(idx))
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            parsed = {"_invalid_json": raw[:2000]}
        self.blocks[idx]["toolUse"]["input"] = parsed

    @property
    def message(self) -> dict[str, Any]:
        for idx in list(self._tool_json):
            self._finish_tool(idx)
        content = [self.blocks[i] for i in sorted(self.blocks)]
        content = [b for b in content if not ("text" in b and b["text"] == "")]
        return {"role": "assistant", "content": content}

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b["toolUse"] for b in self.message["content"] if "toolUse" in b]

    @property
    def usage(self) -> dict[str, int]:
        return usage_of(self.raw_usage)


class BedrockClient:
    """One instance per settings generation (built by `AssistantService`)."""

    def __init__(
        self,
        cfg: dict[str, Any],
        credentials: CachingProvider,
        http: httpx.AsyncClient,
        *,
        on_success: Hook | None = None,
        on_error: Hook | None = None,
        on_retry: Hook | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        now: Callable[[], Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.credentials = credentials
        self.http = http
        self.region: str = cfg["region"]
        self.endpoint = config.runtime_endpoint(cfg)
        self.model_id: str = cfg.get("model_id") or ""
        self.model_id_light: str | None = cfg.get("model_id_light") or None
        self.caching_enabled = bool(cfg.get("prompt_caching", True))
        self.caching_supported: bool | None = None  # False after a cachePoint rejection
        # compatibility fallbacks, remembered for this settings generation (models that reject
        # sampling parameters or a forced toolChoice)
        self.sampling_supported: bool | None = None
        self.forced_tool_supported: bool | None = None
        self.on_success = on_success
        self.on_error = on_error
        self.on_retry = on_retry
        self.sleep = sleep
        self._now = now
        self.last_stream_buffered = False

    # ---------------------------------------------------------------- helpers
    def model_for(self, model_id: str | None = None, light: bool = False) -> str:
        if model_id:
            return model_id
        if light and self.model_id_light:
            return self.model_id_light
        if not self.model_id:
            raise make("assistant_unconfigured")
        return self.model_id

    @property
    def caching_active(self) -> bool:
        return self.caching_enabled and self.caching_supported is not False

    def _body(self, request: dict[str, Any]) -> tuple[bytes, bool]:
        req = {k: v for k, v in request.items() if k != "modelId"}
        cached = has_cache_points(req)
        if cached and not self.caching_active:
            req = strip_cache_points(req)
            cached = False
        if self.sampling_supported is False and isinstance(req.get("inferenceConfig"), dict):
            req["inferenceConfig"] = {
                k: v for k, v in req["inferenceConfig"].items() if k not in ("temperature", "topP")
            }
        choice = (req.get("toolConfig") or {}).get("toolChoice") or {}
        if self.forced_tool_supported is False and ("tool" in choice or "any" in choice):
            req["toolConfig"] = {**req["toolConfig"], "toolChoice": {"auto": {}}}
        return json.dumps(req, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), cached

    def _utcnow(self) -> Any:
        if self._now is not None:
            return self._now()
        from datetime import UTC, datetime

        return datetime.now(UTC)

    async def _signed_headers(
        self,
        method: str,
        url: str,
        body: bytes,
        region: str,
        force_refresh: bool,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        creds = await self.credentials.get(force=force_refresh)
        headers = {"Content-Type": "application/json", **(extra or {})}
        if method == "GET":
            headers.pop("Content-Type", None)
        return sigv4.sign_v4(
            method,
            url,
            headers,
            body,
            access_key=creds.access_key_id,
            secret_key=creds.secret_access_key,
            session_token=creds.session_token,
            region=region,
            service=SERVICE,
            amzdate=sigv4.amz_date(self._utcnow()),
        ).headers

    async def _report_ok(self) -> None:
        if self.on_success is not None:
            res = self.on_success()
            if asyncio.iscoroutine(res):
                await res

    async def _report_err(self, err: AssistantError) -> None:
        if self.on_error is not None:
            try:
                res = self.on_error(err)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # noqa: BLE001 - state bookkeeping never hides the error
                pass

    async def _report_retry(self, err: AssistantError) -> None:
        if self.on_retry is not None:
            try:
                res = self.on_retry(err)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # noqa: BLE001
                pass

    def _compat_retry(
        self, err: AssistantError, request: dict[str, Any], cached: bool
    ) -> str | None:
        """Turn off a request feature the model rejected; returns the marker of the retry."""
        if cached and _is_cache_rejection(err) and self.caching_supported is not False:
            self.caching_supported = False
            return "cache_unsupported"
        if self.sampling_supported is not False and _is_sampling_rejection(err, request):
            self.sampling_supported = False
            return "sampling_unsupported"
        if self.forced_tool_supported is not False and _is_forced_tool_rejection(err, request):
            self.forced_tool_supported = False
            return "forced_tool_unsupported"
        return None

    def _retry_delay(self, err: AssistantError, attempts: dict[str, int]) -> float | None:
        """Delay before the next attempt, or None when no retry is allowed."""
        policy = err.retry_policy
        n = attempts.get(err.code, 0)
        if policy == "throttle" and n < len(THROTTLE_DELAYS):
            return _jitter(THROTTLE_DELAYS[n])
        if policy == "unavailable" and n < 1:
            return UNAVAILABLE_DELAY
        if policy in ("auth", "network") and n < 1:
            return 0.0
        return None

    # ---------------------------------------------------------------- Converse
    async def converse(
        self,
        request: dict[str, Any],
        *,
        model_id: str | None = None,
        light: bool = False,
        timeout: float = tp.NONSTREAM_TIMEOUT_S,
    ) -> dict[str, Any]:
        mid = self.model_for(model_id, light)
        try:
            return await asyncio.wait_for(self._converse(request, mid), timeout)
        except TimeoutError:
            err = make("assistant_timeout")
            await self._report_err(err)
            raise err from None

    async def _converse(self, request: dict[str, Any], mid: str) -> dict[str, Any]:
        url = f"{self.endpoint}{sigv4.model_path(mid, 'converse')}"
        attempts: dict[str, int] = {}
        retries: list[str] = []
        force = False
        t0 = time.monotonic()
        while True:
            body, cached = self._body(request)
            try:
                headers = await self._signed_headers("POST", url, body, self.region, force)
                force = False
                resp = await self.http.post(url, content=body, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    data["_meta"] = {
                        "model_id": mid,
                        "retries": retries,
                        "duration_ms": int((time.monotonic() - t0) * 1000),
                        "caching": cached,
                    }
                    await self._report_ok()
                    return data
                err = map_error(
                    resp.status_code, dict(resp.headers), resp.content, region=self.region
                )
            except AssistantError as exc:
                err = exc
            except httpx.HTTPError as exc:
                err = map_exception(exc, host=tp.host_of(url), via=tp.via(url, self.cfg))
            except ValueError:
                err = make("bedrock_unavailable")
            marker = self._compat_retry(err, request, cached)
            if marker:
                retries.append(marker)
                continue
            delay = self._retry_delay(err, attempts)
            if delay is None:
                await self._report_err(err)
                raise err
            attempts[err.code] = attempts.get(err.code, 0) + 1
            retries.append(err.code)
            await self._report_retry(err)
            if err.retry_policy == "auth":
                force = True
            if delay:
                await self.sleep(delay)

    # ---------------------------------------------------------------- ConverseStream
    async def converse_stream(
        self,
        request: dict[str, Any],
        *,
        model_id: str | None = None,
        light: bool = False,
        first_event_timeout: float = tp.FIRST_EVENT_TIMEOUT_S,
        idle_timeout: float = tp.IDLE_TIMEOUT_S,
    ) -> AsyncIterator[dict[str, Any]]:
        mid = self.model_for(model_id, light)
        url = f"{self.endpoint}{sigv4.model_path(mid, 'converse-stream')}"
        attempts: dict[str, int] = {}
        retries: list[str] = []
        force = False
        t0 = time.monotonic()
        while True:
            body, cached = self._body(request)
            text_started = False
            ttfe: int | None = None
            reads = 0
            try:
                headers = await self._signed_headers(
                    "POST", url, body, self.region, force,
                    {"Accept": "application/vnd.amazon.eventstream"},
                )  # fmt: skip
                force = False
                async with self.http.stream("POST", url, content=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        raw = await resp.aread()
                        raise map_error(
                            resp.status_code, dict(resp.headers), raw, region=self.region
                        )
                    decoder = Decoder()
                    chunks = resp.aiter_bytes().__aiter__()
                    t_req = time.monotonic()
                    while True:
                        wait = first_event_timeout if ttfe is None else idle_timeout
                        try:
                            chunk = await asyncio.wait_for(chunks.__anext__(), wait)
                        except StopAsyncIteration:
                            break
                        except TimeoutError:
                            raise make("assistant_timeout") from None
                        reads += 1
                        for msg in decoder.feed(chunk):
                            if ttfe is None:
                                ttfe = int((time.monotonic() - t_req) * 1000)
                            if msg.message_type != "event":
                                raise map_stream_exception(
                                    msg.exception_type, msg.json(), region=self.region
                                )
                            ev = {"type": msg.event_type, "data": msg.json()}
                            if ev["type"] == "contentBlockDelta":
                                text_started = True
                            yield ev
                    total = time.monotonic() - t_req
                    self.last_stream_buffered = reads <= 1 and total > BUFFERED_AFTER_S
                await self._report_ok()
                yield {
                    "type": "stream_end",
                    "data": {
                        "time_to_first_event_ms": ttfe,
                        "duration_ms": int((time.monotonic() - t0) * 1000),
                        "buffered": self.last_stream_buffered,
                        "model_id": mid,
                        "retries": retries,
                        "caching": cached,
                    },
                }
                return
            except AssistantError as exc:
                err = exc
            except EventStreamError:
                err = make("bedrock_unavailable")
            except httpx.HTTPError as exc:
                err = map_exception(exc, host=tp.host_of(url), via=tp.via(url, self.cfg))
            if text_started:
                await self._report_err(err)
                raise err
            marker = self._compat_retry(err, request, cached)
            if marker:
                retries.append(marker)
                continue
            delay = self._retry_delay(err, attempts)
            if delay is None:
                await self._report_err(err)
                raise err
            attempts[err.code] = attempts.get(err.code, 0) + 1
            retries.append(err.code)
            await self._report_retry(err)
            if err.retry_policy == "auth":
                force = True
            yield {
                "type": "retrying",
                "data": {"code": err.code, "attempt": attempts[err.code], "delay_s": delay},
            }
            if delay:
                await self.sleep(delay)

    async def converse_stream_collect(
        self, request: dict[str, Any], **kw: Any
    ) -> tuple[StreamAssembler, dict[str, Any]]:
        """Convenience: run a stream to the end → (assembler, stream_end data)."""
        asm = StreamAssembler()
        end: dict[str, Any] = {}
        async for ev in self.converse_stream(request, **kw):
            if ev["type"] == "stream_end":
                end = ev["data"]
            else:
                asm.feed(ev)
        return asm, end

    # ---------------------------------------------------------------- Knowledge Base
    async def retrieve(
        self,
        query: str,
        kb: dict[str, Any],
        *,
        results: int | None = None,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Bedrock Agent Runtime `Retrieve` → `[{"text", "score", "title", "source_url",
        "location_type"}]` filtered by `min_score`, at most `results` items."""
        region = config.kb_region(kb, self.cfg)
        endpoint = config.kb_endpoint(kb, self.cfg)
        kb_id = kb.get("knowledge_base_id") or ""
        n = int(results or kb.get("results") or 5)
        url = f"{endpoint}/knowledgebases/{sigv4.uri_encode(kb_id, safe='')}/retrieve"
        body = json.dumps(
            {
                "retrievalQuery": {"text": query},
                "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": n}},
            },
            separators=(",", ":"),
        ).encode()
        attempts: dict[str, int] = {}
        force = False
        while True:
            try:
                headers = await self._signed_headers("POST", url, body, region, force)
                force = False
                resp = await asyncio.wait_for(
                    self.http.post(url, content=body, headers=headers), timeout
                )
                if resp.status_code == 200:
                    return parse_retrieve(resp.json(), float(kb.get("min_score") or 0.0), n)
                err = map_error(resp.status_code, dict(resp.headers), resp.content, region=region)
            except AssistantError as exc:
                err = exc
            except TimeoutError:
                err = make("assistant_timeout")
            except httpx.HTTPError as exc:
                err = map_exception(exc, host=tp.host_of(url), via=tp.via(url, self.cfg))
            except ValueError:
                err = make("bedrock_unavailable")
            delay = self._retry_delay(err, attempts)
            if delay is None:
                raise err
            attempts[err.code] = attempts.get(err.code, 0) + 1
            if err.retry_policy == "auth":
                force = True
            if delay:
                await self.sleep(delay)

    # ---------------------------------------------------------------- control plane
    async def list_models(self) -> list[dict[str, Any]]:
        """Anthropic inference profiles and foundation models that support streaming."""
        base = config.control_endpoint(self.cfg)
        items: list[dict[str, Any]] = []
        profiles = await self._get_json(f"{base}/inference-profiles?maxResults=1000")
        for p in profiles.get("inferenceProfileSummaries") or []:
            pid = str(p.get("inferenceProfileId") or "")
            if "anthropic" not in pid.lower():
                continue
            items.append(
                {
                    "id": pid,
                    "name": p.get("inferenceProfileName") or pid,
                    "kind": "inference_profile",
                    "streaming": True,
                    "status": p.get("status") or "ACTIVE",
                }
            )
        models = await self._get_json(f"{base}/foundation-models?byProvider=Anthropic")
        for m in models.get("modelSummaries") or []:
            if str(m.get("providerName") or "").lower() != "anthropic":
                continue
            if m.get("responseStreamingSupported") is False:
                continue
            items.append(
                {
                    "id": m.get("modelId"),
                    "name": m.get("modelName") or m.get("modelId"),
                    "kind": "foundation_model",
                    "streaming": bool(m.get("responseStreamingSupported", True)),
                    "status": (m.get("modelLifecycle") or {}).get("status") or "ACTIVE",
                }
            )
        return items

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            headers = await self._signed_headers("GET", url, b"", self.region, False)
            resp = await self.http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise map_exception(exc, host=tp.host_of(url), via=tp.via(url, self.cfg)) from None
        if resp.status_code != 200:
            raise map_error(resp.status_code, dict(resp.headers), resp.content, region=self.region)
        try:
            return dict(resp.json())
        except ValueError:
            raise make("bedrock_unavailable") from None

    async def aclose(self) -> None:
        await self.http.aclose()


def parse_retrieve(data: dict[str, Any], min_score: float, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in data.get("retrievalResults") or []:
        score = r.get("score")
        if score is not None and float(score) < min_score:
            continue
        loc = r.get("location") or {}
        url = None
        for key in ("webLocation", "confluenceLocation", "sharePointLocation",
                    "salesforceLocation", "customDocumentLocation"):  # fmt: skip
            v = loc.get(key) or {}
            url = v.get("url") or v.get("id")
            if url:
                break
        if not url:
            url = (loc.get("s3Location") or {}).get("uri")
        meta = r.get("metadata") or {}
        src = url or meta.get("x-amz-bedrock-kb-source-uri") or ""
        title = meta.get("title") or (str(src).rstrip("/").rsplit("/", 1)[-1] if src else "")
        out.append(
            {
                "text": str((r.get("content") or {}).get("text") or ""),
                "score": float(score) if score is not None else None,
                "title": str(title)[:200],
                "source_url": str(src) if src else None,
                "location_type": loc.get("type"),
            }
        )
        if len(out) >= limit:
            break
    return out
