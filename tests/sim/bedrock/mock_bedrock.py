#!/usr/bin/env python3
"""Amazon Bedrock mock for NetworkMonitor phase 3b (docs/architecture.md §14.11). No AWS, one process.

    python tests/sim/bedrock/mock_bedrock.py --port 0 [--https-port 0 --pki DIR] [--log FILE]
    -> prints `READY {"port": 43123, "url": "http://127.0.0.1:43123", "https_port": ..., ...}`

One server answers everything the backend talks to (point every endpoint URL at it):
    POST /model/{modelId}/converse | /converse-stream     Bedrock Runtime (event-stream framing by es_codec.py)
    POST /knowledgebases/{kbId}/retrieve                  Bedrock Agent Runtime (Knowledge Base Retrieve)
    POST /sessions                                        IAM Roles Anywhere CreateSession (X.509 signature)
    GET  /inference-profiles, /foundation-models          Bedrock control plane (List models)
    PUT  /latest/api/token, GET /latest/meta-data/iam/security-credentials/[role]    EC2 IMDSv2
    GET  /container-credentials, /v2/credentials/{id}     ECS/EKS container credentials
Every AWS request is verified with botocore's SigV4Auth (signing name `bedrock`, any region; the region and
service from the credential scope are logged) or, for /sessions, with the leaf certificate's public key and the
trust anchor of the test PKI (pki.py).

Control API (JSON; see README.md):
    GET  /_health                        {"ok": true}
    GET  /_requests[?op=converse_stream&purpose=ask]   request log; DELETE /_requests clears it
    POST /_mode {"mode": "throttle:2", "target": "converse"|"kb"|"sessions"|"imds"|"control"|"all", "times": n}
    GET  /_mode, DELETE /_mode           current modes / clear all modes
    POST /_scenarios {"scenario": {...}} | {"scenarios": [...]}   add runtime scenarios (win over files)
    GET  /_scenarios, DELETE /_scenarios
    POST /_force_scenario {"name": "stream_long" | null}
    GET|POST /_kb {"knowledge_base_ids": [...], "passages": [...]}
    GET  /_credentials, POST /_credentials {access_key_id, secret_access_key, session_token?, expires_in?}
    POST /_credentials/expire {"access_key_id": "..."} | {"sessions": true}
    GET|POST /_config  {strict_models, verify_signatures, anchor, session_seconds, clock_offset_s, crl_serials,
                        container_token, imds_role, expected_ra_arns, models_by_region}
    GET  /_usage                         sum of the usage numbers the mock returned (by purpose/model)
    POST /_reset                         modes, log, runtime scenarios, KB, config and issued sessions back to start
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import es_codec  # noqa: E402
import scripted_model as sm  # noqa: E402
import sigv4_check  # noqa: E402
from cryptography import x509  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

# ------------------------------------------------------------------------------------------------ constants
STATIC_KEYS = {
    # access key id -> secret (static IAM user keys, auth mode access_key)
    "AKIAMOCKSTATIC000001": "mockStaticSecretKey/000000000000000000000",
    "AKIAMOCKSTATIC000002": "mockStaticSecretKey/000000000000000000002",
}
ACCOUNT = "111122223333"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/netmon-bedrock"
IMDS_ROLE = "netmon-bedrock-role"
CONTAINER_TOKEN = "mock-container-auth-token"
DEFAULT_KB_IDS = ["QAKBTEST01", "QAKBTEST02"]
ANTHROPIC_MODELS = [  # (base id, name, on-demand allowed)
    ("anthropic.claude-sonnet-4-5-20250929-v1:0", "Claude Sonnet 4.5", False),
    ("anthropic.claude-haiku-4-5-20251001-v1:0", "Claude Haiku 4.5", False),
    ("anthropic.claude-opus-4-1-20250805-v1:0", "Claude Opus 4.1", False),
    ("anthropic.claude-3-haiku-20240307-v1:0", "Claude 3 Haiku", True),
]
# geographic inference-profile prefixes and the regions that serve them (owner: us-east-1 + `us.`; il-central-1 +
# `global.`)
PROFILE_REGIONS = {
    "us": re.compile(r"^us-(east|west)-\d$"),
    "eu": re.compile(r"^eu-[a-z]+-\d$"),
    "apac": re.compile(r"^ap-[a-z]+-\d$"),
    "jp": re.compile(r"^ap-northeast-[13]$"),
    "au": re.compile(r"^ap-southeast-[24]$"),
    "ca": re.compile(r"^ca-[a-z]+-\d$"),
    "us-gov": re.compile(r"^us-gov-[a-z]+-\d$"),
    "global": re.compile(r"^(us-(east|west)-\d|eu-[a-z]+-\d|ap-[a-z]+-\d|ca-central-1|sa-east-1|il-central-1|me-central-1)$"),
}
PROFILE_RE = re.compile(r"^(us|eu|apac|jp|au|ca|us-gov|global)\.(anthropic\.claude-[A-Za-z0-9.\-]+(?::\d+)?)$")
ARN_MODEL_RE = re.compile(
    r"^arn:aws[a-z-]*:bedrock:(?P<region>[a-z0-9-]+):(?P<acct>\d{12}|):"
    r"(?P<kind>inference-profile|application-inference-profile|provisioned-model|foundation-model)/(?P<id>.+)$")

ERRORS = {  # mode -> (status, x-amzn-ErrorType, message)
    "throttle": (429, "ThrottlingException", "Too many requests, please wait before trying again."),
    "access_denied": (403, "AccessDeniedException",
                      "User: arn:aws:sts::111122223333:assumed-role/netmon-bedrock/4660 is not authorized to perform: "
                      "bedrock:InvokeModelWithResponseStream on resource: arn:aws:bedrock:{region}:111122223333:"
                      "inference-profile/{model} because no identity-based policy allows the "
                      "bedrock:InvokeModelWithResponseStream action"),
    "model_access": (403, "AccessDeniedException", "You don't have access to the model with the specified model ID."),
    "model_invalid": (400, "ValidationException", "The provided model identifier is invalid."),
    "on_demand_unsupported": (400, "ValidationException",
                              "Invocation of model ID {model} with on-demand throughput isn’t supported. Retry your "
                              "request with the ID or ARN of an inference profile that contains this model."),
    "not_found": (404, "ResourceNotFoundException", "Model not found: {model}"),
    "quota": (400, "ServiceQuotaExceededException",
              "Your request exceeds the service quota for your account. You can view your quotas at "
              "https://docs.aws.amazon.com/servicequotas/latest/userguide/gs-request-quota.html."),
    "validation": (400, "ValidationException",
                   "Malformed input request: #: extraneous key [mockField] is not permitted, please reformat your "
                   "input and try again."),
    "unavailable": (503, "ServiceUnavailableException", "Bedrock is unable to process your request."),
    "model_timeout": (408, "ModelTimeoutException", "Model has timed out in processing the request. Try your request again."),
    "model_not_ready": (429, "ModelNotReadyException",
                        "Model is not ready to serve inference requests. The AWS SDK will automatically retry the "
                        "operation up to 5 times."),
    "model_error": (424, "ModelErrorException", "The model encountered an error processing the request."),
    "internal": (500, "InternalServerException", "The server encountered an internal error while processing the request."),
    "expired_token": (403, "ExpiredTokenException", "The security token included in the request is expired"),
    "invalid_signature": (403, "InvalidSignatureException",
                          "The request signature we calculated does not match the signature you provided. Check your "
                          "AWS Secret Access Key and signing method. Consult the service documentation for details."),
    "unrecognized_client": (403, "UnrecognizedClientException", "The security token included in the request is invalid."),
    "cache_unsupported": (400, "ValidationException",
                          "The model {model} does not support prompt caching. Remove the cachePoint blocks from the "
                          "request and try again."),
    "sampling_unsupported": (400, "ValidationException",
                             "The model returned the following errors: `temperature` and `top_p` are not supported "
                             "for this model. Remove them and try again."),
    "forced_tool_unsupported": (400, "ValidationException",
                                "The model returned the following errors: tool_choice: forcing tool use with "
                                "`tool` or `any` is not supported for this model."),
    # Roles Anywhere
    "untrusted_cert": (403, "AccessDeniedException", "Untrusted certificate. Insufficient certificate"),
    "unable_to_assume": (403, "AccessDeniedException", f"Unable to assume role for arn: {ROLE_ARN}"),
    "profile_disabled": (403, "AccessDeniedException", "Profile is disabled"),
    # Knowledge Base
    "kb_error": (403, "AccessDeniedException",
                 "User: arn:aws:sts::111122223333:assumed-role/netmon-bedrock/4660 is not authorized to perform: "
                 "bedrock:Retrieve on resource: arn:aws:bedrock:{region}:111122223333:knowledge-base/{kb}"),
    "kb_validation": (400, "ValidationException", "1 validation error detected: Value at 'retrievalQuery.text' failed to satisfy constraint"),
}
STREAM_EXC = {"throttle_stream": ("throttlingException", ERRORS["throttle"][2]),
              "stream_error": ("modelStreamErrorException", "The model stream terminated unexpectedly."),
              "stream_internal": ("internalServerException", ERRORS["internal"][2]),
              "stream_unavailable": ("serviceUnavailableException", ERRORS["unavailable"][2]),
              "stream_validation": ("validationException", "Input is too long for requested model.")}
TARGET_OF = {"untrusted_cert": "sessions", "unable_to_assume": "sessions", "profile_disabled": "sessions",
             "ra_short": "sessions", "kb_empty": "kb", "kb_error": "kb", "kb_validation": "kb",
             "imds_down": "imds", "imds_no_role": "imds", "container_down": "imds"}
DEFAULT_PASSAGES = [
    {"text": "Runbook: core switch replacement. Before replacing core-sw, move the uplinks of sw-floor1 and sw-floor2 "
             "to the standby core and verify LACP on Bridge-Aggregation1.",
     "location": {"type": "S3", "s3Location": {"uri": "s3://netmon-kb-docs/runbooks/core-switch.pdf"}},
     "score": 0.82, "metadata": {"x-amz-bedrock-kb-source-uri": "s3://netmon-kb-docs/runbooks/core-switch.pdf"}},
    {"text": "Network diagram: the branch routers connect over MPLS to the HQ firewall cluster gw-hq.",
     "location": {"type": "CONFLUENCE", "confluenceLocation": {"url": "https://wiki.mnidom.test/display/NET/Diagram"}},
     "score": 0.74, "metadata": {"title": "Network diagram", "x-amz-bedrock-kb-source-uri": "https://wiki.mnidom.test/display/NET/Diagram"}},
    {"text": "On-call procedure for printers: call the vendor after two failed toner alerts.",
     "location": {"type": "SHAREPOINT", "sharePointLocation": {"url": "https://mnidom.sharepoint.test/sites/it/Printers.docx"}},
     "score": 0.41, "metadata": {"x-amz-bedrock-kb-source-uri": "https://mnidom.sharepoint.test/sites/it/Printers.docx"}},
    {"text": "VLAN plan: VLAN 20 servers 10.20.0.0/24, VLAN 30 printers 10.30.0.0/24.",
     "location": {"type": "WEB", "webLocation": {"url": "https://intranet.mnidom.test/vlans.html"}},
     "score": 0.33, "metadata": {"title": "VLAN plan"}},
]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rand_upper(n: int) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    return "".join(secrets.choice(alphabet) for _ in range(n))


# ------------------------------------------------------------------------------------------------ state
class MockState:
    def __init__(self, *, pki_dir: str | None = None, log_file: str | None = None,
                 scenario_dirs: list[str] | None = None, anchor: str = "issuing"):
        self.pki_dir = pki_dir
        self.log_file = log_file
        self.scenario_dirs = [Path(d) for d in (scenario_dirs or [])]
        self.default_anchor = anchor
        self.lock = threading.RLock()
        self.seq = 0
        self.model = sm.Model(sm.load_scenarios([sm.SCENARIO_DIR, *self.scenario_dirs]))
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.log: list[dict[str, Any]] = []
            self.modes: dict[str, dict[str, Any]] = {}
            self.model.runtime = []
            self.model.forced_name = None
            self.kb_ids = list(DEFAULT_KB_IDS)
            self.passages = [dict(p) for p in DEFAULT_PASSAGES]
            self.sessions: dict[str, dict[str, Any]] = {}
            self.imds_tokens: dict[str, float] = {}
            self.cache_unsupported = False
            self.sampling_unsupported = False
            self.forced_tool_unsupported = False
            self.config: dict[str, Any] = {
                "strict_models": True, "verify_signatures": True, "anchor": self.default_anchor,
                "session_seconds": None, "clock_offset_s": 0, "crl_serials": [], "container_token": CONTAINER_TOKEN,
                "imds_role": IMDS_ROLE, "expected_ra_arns": None, "profile_max_seconds": 43200,
                "chunk_chars": 16, "delay_ms": 10, "first_delay_ms": 0,
            }
            self.static = dict(STATIC_KEYS)

    # -------------------------------------------------------------- clock / credentials
    def now(self) -> dt.datetime:
        return utcnow() + dt.timedelta(seconds=int(self.config.get("clock_offset_s") or 0))

    def lookup_secret(self, akid: str):
        with self.lock:
            if akid in self.static:
                return self.static[akid], None, False
            s = self.sessions.get(akid)
            if s:
                return s["secret"], s["token"], s["expires_at"] <= time.time()
        return None

    def issue_session(self, source: str, seconds: int) -> dict[str, Any]:
        override = self.config.get("session_seconds")
        if override:
            seconds = int(override)
        akid = "ASIA" + _rand_upper(16)
        cred = {"access_key_id": akid, "secret": secrets.token_urlsafe(30)[:40],
                "token": "IQoJb3JpZ2luX2VjE" + base64.b64encode(secrets.token_bytes(150)).decode(),
                "expires_at": time.time() + seconds, "source": source, "issued_at": time.time()}
        with self.lock:
            self.sessions[akid] = cred
        return cred

    def anchors(self) -> list[x509.Certificate]:
        if not self.pki_dir:
            return []
        names = {"issuing": ["issuing.pem"], "root": ["root.pem"], "both": ["issuing.pem", "root.pem"]}
        out = []
        for n in names.get(self.config.get("anchor", "issuing"), ["issuing.pem"]):
            p = Path(self.pki_dir) / n
            if p.exists():
                out.append(x509.load_pem_x509_certificate(p.read_bytes()))
        return out

    # -------------------------------------------------------------- modes
    def set_mode(self, mode: str, target: str | None, times: int | None) -> dict[str, Any]:
        name, *args = mode.split(":")
        if name == "normal":
            with self.lock:
                if target:
                    self.modes.pop(target, None)
                else:
                    self.modes.clear()
            return {"modes": self.modes}
        if name == "clock_skew":
            self.config["clock_offset_s"] = int(args[0]) if args else 900
            return {"modes": self.modes, "clock_offset_s": self.config["clock_offset_s"]}
        if name in ("cache_unsupported", "sampling_unsupported", "forced_tool_unsupported"):
            setattr(self, name, True)  # sticky model capability flags (until DELETE /_mode or /_reset)
            return {"modes": self.modes, name: True}
        tgt = target or TARGET_OF.get(name, "converse")
        remaining = times
        if name in ("throttle", "throttle_stream") and times is None:
            remaining = int(args[0]) if args else 1
        spec = {"name": name, "args": args, "remaining": remaining, "mode": mode}
        with self.lock:
            for t in (["converse", "kb", "control", "sessions"] if tgt == "all" else [tgt]):
                self.modes[t] = dict(spec)
        return {"modes": self.modes}

    def take_mode(self, target: str) -> dict[str, Any] | None:
        with self.lock:
            m = self.modes.get(target)
            if not m:
                return None
            if m["remaining"] is not None:
                m["remaining"] -= 1
                if m["remaining"] <= 0:
                    self.modes.pop(target, None)
            return dict(m)

    # -------------------------------------------------------------- log
    def record(self, entry: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.seq += 1
            entry["seq"] = self.seq
            entry["time"] = time.time()
            self.log.append(entry)
        return entry

    def flush(self, entry: dict[str, Any]) -> None:
        if self.log_file:
            with self.lock, open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


# ------------------------------------------------------------------------------------------------ helpers
def aws_error(status: int, etype: str, message: str, *, headers: dict[str, str] | None = None) -> Response:
    h = {"x-amzn-ErrorType": f"{etype}:http://internal.amazon.com/coral/com.amazon.bedrock/",
         "x-amzn-RequestId": str(uuid.uuid4())}
    h.update(headers or {})
    return JSONResponse({"message": message}, status_code=status, headers=h)


def err_from(mode: str, **fmt: str) -> tuple[int, str, str]:
    st, et, msg = ERRORS[mode]
    try:
        msg = msg.format(**fmt)
    except (KeyError, IndexError):
        pass
    return st, et, msg


async def wait_disconnect(request: Request, max_s: float) -> bool:
    end = time.monotonic() + max_s
    while time.monotonic() < end:
        if await request.is_disconnected():
            return True
        await asyncio.sleep(0.1)
    return False


def raw_path_query(request: Request) -> tuple[str, str]:
    raw = request.scope.get("raw_path") or request.url.path.encode()
    raw_s = raw.decode("latin-1")
    path, _, q = raw_s.partition("?")
    return path, request.scope.get("query_string", b"").decode("latin-1") or q


def check_model(state: MockState, model_id: str, region: str) -> tuple[str, str] | None:
    """Return (mode, model) for an invalid model id in this region, else None."""
    if not state.config.get("strict_models", True):
        return None
    m = PROFILE_RE.match(model_id)
    if m:
        base = m.group(2)
        if base not in {b for b, _, _ in ANTHROPIC_MODELS} and not base.startswith("anthropic.claude-test"):
            return "model_invalid", model_id
        if not PROFILE_REGIONS[m.group(1)].match(region or ""):
            return "model_invalid", model_id
        return None
    if model_id.startswith("anthropic.claude-"):
        known = {b: od for b, _, od in ANTHROPIC_MODELS}
        if model_id not in known and not model_id.startswith("anthropic.claude-test"):
            return "model_invalid", model_id
        if not known.get(model_id, False):
            return "on_demand_unsupported", model_id
        return None
    a = ARN_MODEL_RE.match(model_id)
    if a:
        if a.group("kind") == "foundation-model":
            return check_model(state, a.group("id"), region)
        return None if a.group("region") == region else ("model_invalid", model_id)
    return "model_invalid", model_id


# ------------------------------------------------------------------------------------------------ app
def create_app(state: MockState) -> Starlette:
    async def verify(request: Request, body: bytes, service: str = "bedrock"):
        path, query = raw_path_query(request)
        headers = dict(request.headers)
        if not state.config.get("verify_signatures", True):
            a = sigv4_check.parse_auth(headers.get("authorization")) or {}
            return True, None, "not verified", {"region": a.get("region"), "service": a.get("service"),
                                                "access_key_id": a.get("key")}
        return sigv4_check.verify_sigv4(request.method, headers.get("host", ""), path, query, headers, body,
                                        lookup_secret=state.lookup_secret, now=state.now(), expected_service=service)

    def base_entry(request: Request, op: str, body: bytes) -> dict[str, Any]:
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = {"_unparseable": body[:2000].decode("utf-8", "replace")}
        h = {k: v for k, v in request.headers.items()}
        return {"op": op, "method": request.method, "path": request.url.path,
                "raw_path": raw_path_query(request)[0], "query": raw_path_query(request)[1],
                "headers": h, "body": parsed, "body_sha256": hashlib.sha256(body).hexdigest(),
                "client": request.client.host if request.client else None,
                "scheme": request.url.scheme}

    # ---------------------------------------------------------------- Bedrock runtime
    async def model_route(request: Request) -> Response:
        path, _ = raw_path_query(request)
        m = re.match(r"^/model/(?P<id>[^/]+)/(?P<op>converse|converse-stream)$", path)
        if not m:
            return aws_error(404, "UnknownOperationException", f"Unknown operation {path}")
        model_id = unquote(m.group("id"))
        streaming = m.group("op") == "converse-stream"
        body = await request.body()
        entry = state.record(base_entry(request, "converse_stream" if streaming else "converse", body))
        entry.update({"model_id": model_id, "model_id_raw": m.group("id"),
                      "model_id_encoded": ":" not in m.group("id")})
        ok, etype, msg, info = await verify(request, body)
        entry["sig"] = {"ok": ok, "error": etype, "message": msg, **{k: v for k, v in info.items() if k != "canonical_request"}}
        entry["region"] = info.get("region")
        if not ok:
            if "canonical_request" in info:
                entry["sig"]["canonical_request"] = info["canonical_request"]
            return finish(entry, aws_error(403 if etype != "IncompleteSignatureException" else 400, etype, msg))
        if not isinstance(entry["body"], dict) or "_unparseable" in (entry["body"] or {}):
            return finish(entry, aws_error(400, "ValidationException", "Malformed input request, please reformat your input and try again."))
        bad = check_model(state, model_id, info.get("region") or "")
        if bad:
            st, et, ms = err_from(bad[0], model=model_id, region=info.get("region") or "")
            return finish(entry, aws_error(st, et, ms))
        mode = state.take_mode("converse")
        entry["mode"] = mode["mode"] if mode else None
        v = sm.view(entry["body"])
        entry.update({"purpose": v.purpose, "offered_tools": v.offered, "forced_tool": v.forced_tool,
                      "tool_choice": v.tool_choice, "has_cache_point": v.has_cache_point, "question": v.question,
                      "turn_index": v.turn_index, "final_call": v.is_final_call, "max_tokens": v.max_tokens,
                      "lang": v.lang})
        if mode:
            name, args = mode["name"], mode["args"]
            if name in ERRORS:
                st, et, ms = err_from(name, model=model_id, region=entry["region"] or "")
                return finish(entry, aws_error(st, et, ms))
            if name == "hang":
                closed = await wait_disconnect(request, float(args[0]) if args else 600)
                entry["closed_by_client"] = closed
                entry["closed_at"] = time.time()
                return finish(entry, aws_error(504, "ModelTimeoutException", "mock hang ended"))
            if name == "slow" and not streaming:
                await asyncio.sleep(float(args[0]) if args else 5)
        err = sm.validate(entry["body"])
        if err:
            return finish(entry, aws_error(400, "ValidationException", err))
        if state.cache_unsupported and v.has_cache_point:
            st, et, ms = err_from("cache_unsupported", model=model_id)
            return finish(entry, aws_error(st, et, ms))
        ic = entry["body"].get("inferenceConfig") or {}
        if state.sampling_unsupported and ("temperature" in ic or "topP" in ic):
            return finish(entry, aws_error(*err_from("sampling_unsupported")))
        ch = (entry["body"].get("toolConfig") or {}).get("toolChoice") or {}
        if state.forced_tool_unsupported and ("tool" in ch or "any" in ch):
            return finish(entry, aws_error(*err_from("forced_tool_unsupported")))
        reply = state.model.play(v, entry["seq"])
        entry.update({"scenario": reply.scenario, "stop_reason": reply.stop_reason, "usage": reply.usage,
                      "template_errors": reply.template_errors,
                      "response_blocks": reply.blocks})
        if not streaming:
            out = {"output": {"message": {"role": "assistant", "content": reply.blocks}},
                   "stopReason": reply.stop_reason, "usage": reply.usage, "metrics": {"latencyMs": 42}}
            return finish(entry, JSONResponse(out, headers={"x-amzn-RequestId": str(uuid.uuid4())}))
        return stream_reply(request, entry, reply, mode)

    def finish(entry: dict[str, Any], resp: Response) -> Response:
        entry["status"] = resp.status_code
        if resp.status_code >= 400:
            try:
                entry["error_type"] = resp.headers.get("x-amzn-errortype", "").split(":")[0]
                entry["error_message"] = json.loads(resp.body).get("message")
            except Exception:  # noqa: BLE001
                pass
        entry["finished"] = True
        entry["duration_ms"] = int((time.time() - entry["time"]) * 1000)
        state.flush(entry)
        return resp

    def stream_reply(request: Request, entry: dict[str, Any], reply: sm.Reply, mode: dict | None) -> Response:
        cfg = state.config
        chunk = int(reply.stream.get("chunk_chars", cfg["chunk_chars"]))
        delay = float(reply.stream.get("delay_ms", cfg["delay_ms"])) / 1000
        first_delay = float(reply.stream.get("first_delay_ms", cfg["first_delay_ms"])) / 1000
        name = mode["name"] if mode else None
        args = mode["args"] if mode else []
        if name == "slow":
            first_delay = max(first_delay, float(args[0]) if args else 5)
        exc_after = None
        if name in STREAM_EXC:
            exc_after = int(args[1] if name == "throttle_stream" and len(args) > 1 else (args[0] if args and name != "throttle_stream" else 0))
        stall_after = int(args[0]) if name == "stall" and args else (0 if name == "stall" else None)
        buffered = float(args[0]) if name == "buffered" and args else (4.0 if name == "buffered" else None)
        entry.update({"events_sent": 0, "deltas_sent": 0, "closed_by_client": False, "status": 200})

        def frames():
            pad = lambda: "abcdefghijklmnopqrstuvwxyz0123456789"[: 1 + secrets.randbelow(30)]  # noqa: E731
            yield "event", es_codec.event("messageStart", {"role": "assistant"}, pad=pad())
            for i, b in enumerate(reply.blocks):
                if "text" in b:
                    t = b["text"]
                    for k in range(0, max(1, len(t)), max(1, chunk)):
                        yield "delta", es_codec.event("contentBlockDelta", {"contentBlockIndex": i, "delta": {"text": t[k:k + chunk]}}, pad=pad())
                    yield "event", es_codec.event("contentBlockStop", {"contentBlockIndex": i}, pad=pad())
                elif "toolUse" in b:
                    tu = b["toolUse"]
                    yield "event", es_codec.event("contentBlockStart", {"contentBlockIndex": i, "start": {
                        "toolUse": {"toolUseId": tu["toolUseId"], "name": tu["name"]}}}, pad=pad())
                    s = json.dumps(tu.get("input") or {}, ensure_ascii=False)
                    for k in range(0, len(s), max(1, chunk)):
                        yield "delta", es_codec.event("contentBlockDelta", {"contentBlockIndex": i, "delta": {"toolUse": {"input": s[k:k + chunk]}}}, pad=pad())
                    yield "event", es_codec.event("contentBlockStop", {"contentBlockIndex": i}, pad=pad())
            yield "event", es_codec.event("messageStop", {"stopReason": reply.stop_reason}, pad=pad())
            yield "event", es_codec.event("metadata", {"usage": reply.usage, "metrics": {"latencyMs": 42}}, pad=pad())

        async def gen():
            try:
                if buffered is not None:
                    data = b"".join(f for _, f in frames())
                    await asyncio.sleep(buffered)
                    entry["events_sent"] = -1
                    entry["first_event_at"] = time.time()
                    yield data
                    return
                if first_delay:
                    await asyncio.sleep(first_delay)
                deltas = 0
                for kind, f in frames():
                    if kind == "delta":
                        if exc_after is not None and deltas >= exc_after:
                            et, em = STREAM_EXC[name]
                            entry["stream_exception"] = et
                            yield es_codec.exception(et, em)
                            return
                        if stall_after is not None and deltas >= stall_after:
                            entry["stalled_at"] = time.time()
                            await asyncio.sleep(600)
                            return
                        deltas += 1
                        entry["deltas_sent"] = deltas
                        if delay:
                            await asyncio.sleep(delay)
                    entry["events_sent"] += 1
                    entry.setdefault("first_event_at", time.time())
                    yield f
                entry["complete"] = True
            except (asyncio.CancelledError, GeneratorExit):
                entry["closed_by_client"] = True
                entry["closed_at"] = time.time()
                raise
            finally:
                entry["finished"] = True
                entry["duration_ms"] = int((time.time() - entry["time"]) * 1000)
                state.flush(entry)

        return StreamingResponse(gen(), media_type="application/vnd.amazon.eventstream",
                                 headers={"x-amzn-RequestId": str(uuid.uuid4()),
                                          "x-amzn-bedrock-content-type": "application/json"})

    # ---------------------------------------------------------------- Knowledge Base
    async def retrieve(request: Request) -> Response:
        kb = request.path_params["kb"]
        body = await request.body()
        entry = state.record(base_entry(request, "retrieve", body))
        entry["knowledge_base_id"] = kb
        ok, etype, msg, info = await verify(request, body)
        entry["sig"] = {"ok": ok, "error": etype, "message": msg}
        entry["region"] = info.get("region")
        if not ok:
            return finish(entry, aws_error(403, etype, msg))
        if not re.fullmatch(r"[0-9A-Z]{10}", kb) or kb not in state.kb_ids:
            return finish(entry, aws_error(404, "ResourceNotFoundException", f"Knowledge base with id {kb} does not exist"))
        b = entry["body"] or {}
        q = ((b.get("retrievalQuery") or {}).get("text") or "")
        if not q.strip() or len(q) > 1000:
            return finish(entry, aws_error(400, "ValidationException", "retrievalQuery.text must be 1-1000 characters"))
        n = (((b.get("retrievalConfiguration") or {}).get("vectorSearchConfiguration") or {}).get("numberOfResults") or 5)
        mode = state.take_mode("kb")
        entry["mode"] = mode["mode"] if mode else None
        if mode:
            if mode["name"] == "kb_empty":
                return finish(entry, JSONResponse({"retrievalResults": []}))
            if mode["name"] in ERRORS:
                st, et, ms = err_from(mode["name"], kb=kb, region=entry["region"] or "")
                return finish(entry, aws_error(st, et, ms))
            if mode["name"] == "slow":
                await asyncio.sleep(float(mode["args"][0]) if mode["args"] else 5)
        res = sorted(state.passages, key=lambda p: -float(p.get("score", 0)))[: int(n)]
        entry["results"] = len(res)
        return finish(entry, JSONResponse({"retrievalResults": [
            {"content": {"text": p["text"], "type": "TEXT"}, "location": p.get("location"), "score": p.get("score"),
             "metadata": p.get("metadata") or {}} for p in res]}))

    # ---------------------------------------------------------------- Roles Anywhere
    async def sessions(request: Request) -> Response:
        body = await request.body()
        entry = state.record(base_entry(request, "create_session", body))
        path, query = raw_path_query(request)
        mode = state.take_mode("sessions")
        entry["mode"] = mode["mode"] if mode else None
        ok, etype, msg, info = sigv4_check.verify_x509_request(
            request.method, request.headers.get("host", ""), path, query, dict(request.headers), body,
            anchors=state.anchors(), now=state.now(), crl_serials={int(s) for s in state.config.get("crl_serials") or []})
        entry["sig"] = {"ok": ok, "error": etype, "message": msg,
                        **{k: v for k, v in info.items() if k not in ("canonical_request",)}}
        entry["region"] = info.get("region")
        if not ok:
            if "canonical_request" in info:
                entry["sig"]["canonical_request"] = info["canonical_request"]
            return finish(entry, aws_error(403, etype or "AccessDeniedException", msg))
        b = entry["body"] or {}
        for k in ("profileArn", "roleArn", "trustAnchorArn"):
            if not isinstance(b.get(k), str):
                return finish(entry, aws_error(400, "ValidationException", f"{k} is required"))
        if not re.match(r"^arn:aws[a-z-]*:rolesanywhere:[a-z0-9-]+:\d{12}:profile/[\w-]+$", b["profileArn"]) or \
           not re.match(r"^arn:aws[a-z-]*:rolesanywhere:[a-z0-9-]+:\d{12}:trust-anchor/[\w-]+$", b["trustAnchorArn"]) or \
           not re.match(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]+$", b["roleArn"]):
            return finish(entry, aws_error(400, "ValidationException", "Invalid ARN"))
        exp = state.config.get("expected_ra_arns")
        if exp:
            for k, v in exp.items():
                if b.get(k) != v:
                    return finish(entry, aws_error(403, "AccessDeniedException", f"Unable to assume role for arn: {b['roleArn']}"
                                                   if k == "roleArn" else f"Resource {b.get(k)} not found"))
        dur = b.get("durationSeconds", 3600)
        if not isinstance(dur, int) or not (900 <= dur <= 43200):
            return finish(entry, aws_error(400, "ValidationException", "durationSeconds must be between 900 and 43200"))
        if dur > int(state.config.get("profile_max_seconds") or 43200):
            return finish(entry, aws_error(400, "ValidationException", "Requested DurationSeconds exceeds the profile's session limit"))
        if mode:
            if mode["name"] in ERRORS:
                st, et, ms = err_from(mode["name"])
                return finish(entry, aws_error(st, et, ms))
            if mode["name"] == "ra_short":
                dur = int(mode["args"][0]) if mode["args"] else 60
        cred = state.issue_session("roles_anywhere", dur)
        entry["issued_access_key_id"] = cred["access_key_id"]
        entry["duration_seconds"] = b.get("durationSeconds")
        subject_id = str(uuid.uuid4())
        region = info.get("region") or "us-east-1"
        return finish(entry, JSONResponse({
            "credentialSet": [{
                "assumedRoleUser": {"arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/netmon-bedrock/{info.get('leaf_serial')}",
                                    "assumedRoleId": f"AROAMOCK{_rand_upper(12)}:{info.get('leaf_serial')}"},
                "credentials": {"accessKeyId": cred["access_key_id"], "secretAccessKey": cred["secret"],
                                "sessionToken": cred["token"],
                                "expiration": iso(dt.datetime.fromtimestamp(cred["expires_at"], dt.timezone.utc))},
                "packedPolicySize": 0, "roleArn": b["roleArn"], "sourceIdentity": info.get("subject")}],
            "subjectArn": f"arn:aws:rolesanywhere:{region}:{ACCOUNT}:subject/{subject_id}",
            "enrollmentArn": b["trustAnchorArn"]}, status_code=201))

    # ---------------------------------------------------------------- control plane
    async def control_plane(request: Request) -> Response:
        op = "list_profiles" if request.url.path.endswith("inference-profiles") else "list_models"
        entry = state.record(base_entry(request, op, b""))
        ok, etype, msg, info = await verify(request, b"")
        entry["sig"] = {"ok": ok, "error": etype, "message": msg}
        region = info.get("region") or "us-east-1"
        entry["region"] = region
        if not ok:
            return finish(entry, aws_error(403, etype, msg))
        mode = state.take_mode("control")
        if mode and mode["name"] in ERRORS:
            st, et, ms = err_from(mode["name"], model="*", region=region)
            if mode["name"] == "access_denied":
                ms = (f"User: arn:aws:sts::{ACCOUNT}:assumed-role/netmon-bedrock/4660 is not authorized to perform: "
                      f"bedrock:{'ListInferenceProfiles' if op == 'list_profiles' else 'ListFoundationModels'}")
            return finish(entry, aws_error(st, et, ms))
        if op == "list_profiles":
            items = []
            for prefix, rx in PROFILE_REGIONS.items():
                if not rx.match(region) or prefix in ("jp", "au", "ca", "us-gov"):
                    continue
                for base, name, _ in ANTHROPIC_MODELS[:3]:
                    pid = f"{prefix}.{base}"
                    items.append({"inferenceProfileId": pid, "inferenceProfileName": f"{prefix.upper()} {name}",
                                  "inferenceProfileArn": f"arn:aws:bedrock:{region}:{ACCOUNT}:inference-profile/{pid}",
                                  "status": "ACTIVE", "type": "SYSTEM_DEFINED", "description": f"Routes requests to {name}",
                                  "models": [{"modelArn": f"arn:aws:bedrock:{region}::foundation-model/{base}"}]})
            items.append({"inferenceProfileId": f"us.amazon.nova-pro-v1:0", "inferenceProfileName": "US Nova Pro",
                          "status": "ACTIVE", "type": "SYSTEM_DEFINED",
                          "inferenceProfileArn": f"arn:aws:bedrock:{region}:{ACCOUNT}:inference-profile/us.amazon.nova-pro-v1:0",
                          "models": []})
            return finish(entry, JSONResponse({"inferenceProfileSummaries": items}))
        models = [{"modelId": b, "modelArn": f"arn:aws:bedrock:{region}::foundation-model/{b}", "modelName": n,
                   "providerName": "Anthropic", "inputModalities": ["TEXT", "IMAGE"], "outputModalities": ["TEXT"],
                   "responseStreamingSupported": True,
                   "inferenceTypesSupported": ["ON_DEMAND"] if od else ["INFERENCE_PROFILE"],
                   "modelLifecycle": {"status": "ACTIVE"}} for b, n, od in ANTHROPIC_MODELS]
        models += [{"modelId": "amazon.titan-embed-text-v2:0", "modelName": "Titan Text Embeddings V2", "providerName": "Amazon",
                    "inputModalities": ["TEXT"], "outputModalities": ["EMBEDDING"], "responseStreamingSupported": False,
                    "inferenceTypesSupported": ["ON_DEMAND"], "modelLifecycle": {"status": "ACTIVE"}},
                   {"modelId": "anthropic.claude-v2:1", "modelName": "Claude 2.1 (legacy)", "providerName": "Anthropic",
                    "inputModalities": ["TEXT"], "outputModalities": ["TEXT"], "responseStreamingSupported": True,
                    "inferenceTypesSupported": ["ON_DEMAND"], "modelLifecycle": {"status": "LEGACY"}}]
        return finish(entry, JSONResponse({"modelSummaries": models}))

    # ---------------------------------------------------------------- IMDSv2 + container credentials
    def proxied(request: Request) -> bool:
        return any(h in request.headers for h in ("x-forwarded-for", "via", "forwarded"))

    async def imds_token(request: Request) -> Response:
        entry = state.record(base_entry(request, "imds_token", b""))
        mode = state.take_mode("imds")
        if mode and mode["name"] == "imds_down":
            return finish(entry, PlainTextResponse("", status_code=503))
        if proxied(request):  # IMDSv2 refuses token requests that went through a proxy
            entry["proxied"] = True
            return finish(entry, PlainTextResponse("Forbidden", status_code=403))
        ttl = request.headers.get("x-aws-ec2-metadata-token-ttl-seconds")
        if not ttl or not ttl.isdigit() or not (1 <= int(ttl) <= 21600):
            return finish(entry, PlainTextResponse("Bad Request", status_code=400))
        tok = "AQAEA" + secrets.token_urlsafe(40)
        state.imds_tokens[tok] = time.time() + int(ttl)
        entry["ttl"] = int(ttl)
        return finish(entry, PlainTextResponse(tok, headers={"x-aws-ec2-metadata-token-ttl-seconds": ttl}))

    async def imds_creds(request: Request) -> Response:
        role = request.path_params.get("role")
        entry = state.record(base_entry(request, "imds_creds" if role else "imds_role", b""))
        tok = request.headers.get("x-aws-ec2-metadata-token")
        if not tok or state.imds_tokens.get(tok, 0) < time.time():
            return finish(entry, PlainTextResponse("Unauthorized", status_code=401))
        mode = state.take_mode("imds")
        if mode and mode["name"] == "imds_down":
            return finish(entry, PlainTextResponse("", status_code=503))
        if mode and mode["name"] == "imds_no_role":
            return finish(entry, PlainTextResponse("Not Found", status_code=404))
        want = state.config.get("imds_role") or IMDS_ROLE
        if not role:
            return finish(entry, PlainTextResponse(want))
        if role != want:
            return finish(entry, PlainTextResponse("Not Found", status_code=404))
        cred = state.issue_session("imds", 6 * 3600)
        entry["issued_access_key_id"] = cred["access_key_id"]
        return finish(entry, JSONResponse({
            "Code": "Success", "LastUpdated": iso(utcnow()), "Type": "AWS-HMAC", "AccessKeyId": cred["access_key_id"],
            "SecretAccessKey": cred["secret"], "Token": cred["token"],
            "Expiration": iso(dt.datetime.fromtimestamp(cred["expires_at"], dt.timezone.utc))}))

    async def container_creds(request: Request) -> Response:
        entry = state.record(base_entry(request, "container_creds", b""))
        mode = state.take_mode("imds")
        if mode and mode["name"] == "container_down":
            return finish(entry, PlainTextResponse("", status_code=503))
        want = state.config.get("container_token")
        if want and request.headers.get("authorization") != want:
            return finish(entry, JSONResponse({"code": "AccessDenied", "message": "bad token"}, status_code=403))
        cred = state.issue_session("container", 6 * 3600)
        entry["issued_access_key_id"] = cred["access_key_id"]
        return finish(entry, JSONResponse({
            "AccessKeyId": cred["access_key_id"], "SecretAccessKey": cred["secret"], "Token": cred["token"],
            "Expiration": iso(dt.datetime.fromtimestamp(cred["expires_at"], dt.timezone.utc)),
            "RoleArn": ROLE_ARN}))

    # ---------------------------------------------------------------- control API
    async def _json(request: Request) -> dict[str, Any]:
        b = await request.body()
        return json.loads(b) if b else {}

    async def c_health(_: Request) -> Response:
        return JSONResponse({"ok": True})

    async def c_requests(request: Request) -> Response:
        if request.method == "DELETE":
            with state.lock:
                state.log.clear()
            return JSONResponse({"ok": True})
        q = request.query_params
        with state.lock:
            items = list(state.log)
        for k in ("op", "purpose", "scenario", "model_id"):
            if q.get(k):
                vals = q.get(k).split(",")
                items = [e for e in items if str(e.get(k)) in vals]
        if q.get("since"):
            items = [e for e in items if e["seq"] > int(q.get("since"))]
        return JSONResponse(json.loads(json.dumps(items, default=str)))

    async def c_mode(request: Request) -> Response:
        if request.method == "GET":
            return JSONResponse({"modes": state.modes, "cache_unsupported": state.cache_unsupported,
                                 "sampling_unsupported": state.sampling_unsupported,
                                 "forced_tool_unsupported": state.forced_tool_unsupported,
                                 "clock_offset_s": state.config["clock_offset_s"]})
        if request.method == "DELETE":
            with state.lock:
                state.modes.clear()
                state.cache_unsupported = False
                state.sampling_unsupported = False
                state.forced_tool_unsupported = False
                state.config["clock_offset_s"] = 0
            return JSONResponse({"modes": {}})
        b = await _json(request)
        return JSONResponse(state.set_mode(str(b.get("mode", "normal")), b.get("target"), b.get("times")))

    async def c_scenarios(request: Request) -> Response:
        if request.method == "GET":
            return JSONResponse([{"name": s.name, "purposes": s.purposes, "source": s.source, "priority": s.priority,
                                  "match": s.match.pattern if s.match else None} for s in state.model.all()])
        if request.method == "DELETE":
            state.model.runtime = []
            return JSONResponse({"ok": True})
        b = await _json(request)
        items = b.get("scenarios") or ([b["scenario"]] if "scenario" in b else [])
        added = []
        for d in items:
            d = dict(d)
            d.setdefault("priority", 100)
            s = sm.Scenario.from_dict(d, source="runtime", order=-len(state.model.runtime) - 1)
            state.model.runtime = [x for x in state.model.runtime if x.name != s.name]
            state.model.runtime.insert(0, s)
            added.append(s.name)
        return JSONResponse({"added": added})

    async def c_force(request: Request) -> Response:
        b = await _json(request)
        state.model.forced_name = b.get("name")
        return JSONResponse({"forced": state.model.forced_name})

    async def c_kb(request: Request) -> Response:
        if request.method == "POST":
            b = await _json(request)
            if "knowledge_base_ids" in b:
                state.kb_ids = list(b["knowledge_base_ids"])
            if "passages" in b:
                state.passages = list(b["passages"])
            if b.get("add_passages"):
                state.passages += list(b["add_passages"])
        return JSONResponse({"knowledge_base_ids": state.kb_ids, "passages": state.passages})

    async def c_creds(request: Request) -> Response:
        if request.method == "POST":
            b = await _json(request)
            if b.get("session_token"):
                state.sessions[b["access_key_id"]] = {"access_key_id": b["access_key_id"], "secret": b["secret_access_key"],
                                                      "token": b["session_token"], "source": "control",
                                                      "expires_at": time.time() + int(b.get("expires_in", 3600)),
                                                      "issued_at": time.time()}
            else:
                state.static[b["access_key_id"]] = b["secret_access_key"]
        now = time.time()
        return JSONResponse({"static": sorted(state.static),
                             "sessions": [{"access_key_id": k, "source": v["source"], "expires_in": int(v["expires_at"] - now),
                                           "issued_at": v["issued_at"]} for k, v in state.sessions.items()]})

    async def c_expire(request: Request) -> Response:
        b = await _json(request)
        with state.lock:
            for k, v in state.sessions.items():
                if b.get("sessions") or b.get("access_key_id") == k:
                    v["expires_at"] = time.time() - 1
        return JSONResponse({"ok": True})

    async def c_config(request: Request) -> Response:
        if request.method == "POST":
            b = await _json(request)
            state.config.update(b)
        return JSONResponse(state.config)

    async def c_usage(_: Request) -> Response:
        tot: dict[str, Any] = {"calls": 0, "inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0,
                               "cacheWriteInputTokens": 0, "by_purpose": {}}
        with state.lock:
            items = [e for e in state.log if e.get("usage") and e.get("op", "").startswith("converse")]
        for e in items:
            u = e["usage"]
            p = tot["by_purpose"].setdefault(e.get("purpose") or "?", {"calls": 0, "inputTokens": 0, "outputTokens": 0,
                                                                      "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0})
            for d in (tot, p):
                d["calls"] += 1
                for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens"):
                    d[k] += int(u.get(k, 0))
        return JSONResponse(tot)

    async def c_reset(_: Request) -> Response:
        state.reset()
        return JSONResponse({"ok": True})

    routes = [
        Route("/model/{rest:path}", model_route, methods=["POST"]),
        Route("/knowledgebases/{kb}/retrieve", retrieve, methods=["POST"]),
        Route("/sessions", sessions, methods=["POST"]),
        Route("/inference-profiles", control_plane, methods=["GET"]),
        Route("/foundation-models", control_plane, methods=["GET"]),
        Route("/latest/api/token", imds_token, methods=["PUT"]),
        Route("/latest/meta-data/iam/security-credentials/", imds_creds, methods=["GET"]),
        Route("/latest/meta-data/iam/security-credentials", imds_creds, methods=["GET"]),
        Route("/latest/meta-data/iam/security-credentials/{role}", imds_creds, methods=["GET"]),
        Route("/container-credentials", container_creds, methods=["GET"]),
        Route("/v2/credentials/{cid}", container_creds, methods=["GET"]),
        Route("/_health", c_health), Route("/health", c_health),
        Route("/_requests", c_requests, methods=["GET", "DELETE"]),
        Route("/_mode", c_mode, methods=["GET", "POST", "DELETE"]),
        Route("/_scenarios", c_scenarios, methods=["GET", "POST", "DELETE"]),
        Route("/_force_scenario", c_force, methods=["POST"]),
        Route("/_kb", c_kb, methods=["GET", "POST"]),
        Route("/_credentials", c_creds, methods=["GET", "POST"]),
        Route("/_credentials/expire", c_expire, methods=["POST"]),
        Route("/_config", c_config, methods=["GET", "POST"]),
        Route("/_usage", c_usage, methods=["GET"]),
        Route("/_reset", c_reset, methods=["POST"]),
    ]
    app = Starlette(routes=routes)
    app.state.mock = state
    return app


# ------------------------------------------------------------------------------------------------ serving
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class MockBedrock:
    """In-process server for pytest: `with MockBedrock() as m: m.url, m.mode("throttle:1"), m.requests()`.

    Runs uvicorn in a daemon thread (own event loop), so it works from sync and async tests alike.
    `https=True` needs `pki_dir` (pki.make_pki) and serves the TLS certificate of the test PKI.
    """

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0, https: bool = False, pki_dir: str | None = None,
                 log_file: str | None = None, anchor: str = "issuing", scenario_dirs: list[str] | None = None):
        import pki as _pki  # noqa: PLC0415
        if pki_dir is None:
            import tempfile  # noqa: PLC0415
            pki_dir = tempfile.mkdtemp(prefix="bedrock-pki-")
        self.pki = _pki.make_pki(pki_dir)
        self.state = MockState(pki_dir=pki_dir, log_file=log_file, scenario_dirs=scenario_dirs, anchor=anchor)
        self.app = create_app(self.state)
        self.host = host
        self.port = port or _free_port()
        self.https = https
        self._server = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"{'https' if self.https else 'http'}://{self.host}:{self.port}"

    def start(self) -> "MockBedrock":
        import uvicorn  # noqa: PLC0415
        kw: dict[str, Any] = {}
        if self.https:
            kw = {"ssl_certfile": self.pki["files"]["server-bundle.pem"], "ssl_keyfile": self.pki["files"]["server.key"]}
        cfg = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning", lifespan="off", **kw)
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True, name="mock-bedrock")
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                return self
            time.sleep(0.02)
        raise RuntimeError("mock bedrock did not start")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> "MockBedrock":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # control shortcuts (direct state access; thread-safe enough for tests)
    def reset(self) -> None:
        self.state.reset()

    def mode(self, mode: str, target: str | None = None, times: int | None = None) -> None:
        self.state.set_mode(mode, target, times)

    def requests(self, op: str | None = None) -> list[dict[str, Any]]:
        with self.state.lock:
            items = list(self.state.log)
        return [e for e in items if op is None or e.get("op") in op.split(",")]

    def add_scenario(self, d: dict[str, Any]) -> None:
        d = dict(d)
        d.setdefault("priority", 100)
        s = sm.Scenario.from_dict(d, source="runtime")
        self.state.model.runtime = [s] + [x for x in self.state.model.runtime if x.name != s.name]

    def config(self, **kw: Any) -> None:
        self.state.config.update(kw)

    static_keys = STATIC_KEYS
    ra_arns = {"trust_anchor": f"arn:aws:rolesanywhere:us-east-1:{ACCOUNT}:trust-anchor/7a1b2c3d-0000-4000-8000-000000000001",
               "profile": f"arn:aws:rolesanywhere:us-east-1:{ACCOUNT}:profile/7a1b2c3d-0000-4000-8000-000000000002",
               "role": ROLE_ARN}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0, help="HTTP port (0 = free port)")
    ap.add_argument("--https-port", type=int, default=None, help="also serve HTTPS (0 = free port); needs --pki")
    ap.add_argument("--pki", default=None, help="test PKI directory (created with pki.py when missing)")
    ap.add_argument("--anchor", default="issuing", choices=["issuing", "root", "both"])
    ap.add_argument("--log", default=None, help="append every request as a JSON line")
    ap.add_argument("--scenarios", action="append", default=[], help="extra scenario directory (repeatable)")
    a = ap.parse_args()
    import uvicorn  # noqa: PLC0415
    import pki as _pki  # noqa: PLC0415
    pki_dir = a.pki or os.path.join(os.environ.get("TMPDIR", "/tmp"), f"bedrock-pki-{os.getpid()}")
    man = _pki.make_pki(pki_dir)
    state = MockState(pki_dir=pki_dir, log_file=a.log, scenario_dirs=a.scenarios, anchor=a.anchor)
    app = create_app(state)
    port = a.port or _free_port()
    servers = [uvicorn.Server(uvicorn.Config(app, host=a.host, port=port, log_level="warning", lifespan="off"))]
    https_port = None
    if a.https_port is not None:
        https_port = a.https_port or _free_port()
        servers.append(uvicorn.Server(uvicorn.Config(app, host=a.host, port=https_port, log_level="warning",
                                                     lifespan="off", ssl_certfile=man["files"]["server-bundle.pem"],
                                                     ssl_keyfile=man["files"]["server.key"])))

    async def run() -> None:
        tasks = [asyncio.create_task(s.serve()) for s in servers]
        while not all(s.started for s in servers):
            await asyncio.sleep(0.05)
        print("READY " + json.dumps({"port": port, "url": f"http://{a.host}:{port}", "https_port": https_port,
                                     "https_url": f"https://localhost:{https_port}" if https_port else None,
                                     "pki": pki_dir}), flush=True)
        await asyncio.gather(*tasks)

    asyncio.run(run())


if __name__ == "__main__":
    main()
