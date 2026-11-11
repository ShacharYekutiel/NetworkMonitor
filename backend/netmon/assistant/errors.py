"""[3b] Assistant error type and the pure AWS error mapping (architecture § 14.2.4,
api.md "[3b] Errors").

`map_error(status, headers, body, region=...)` → `AssistantError`. The error type comes from
the `x-amzn-ErrorType` header (text before `:`), else the body `__type` (text after `#`),
else the HTTP status; inside a stream from the `:exception-type` header (`map_stream_exception`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# code -> (HTTP status, English message template)
CODES: dict[str, tuple[int, str]] = {
    "bedrock_throttled": (502, "Amazon Bedrock is busy (throttled). Try again in a minute."),
    "bedrock_quota": (502, "Your AWS account's Bedrock quota is exhausted for this model."),
    "bedrock_model_access": (
        502,
        "This model is not enabled for your AWS account in {region} (Bedrock → Model access).",
    ),
    "bedrock_access_denied": (
        502,
        "AWS denied access: the role/user lacks bedrock:InvokeModel… on this model or profile.",
    ),
    "bedrock_model_invalid": (
        502,
        "The model ID is not valid in {region}. Use an inference profile ID whose prefix "
        "matches the region (us. for us-*, eu. for eu-*, apac. for ap-*).",
    ),
    "bedrock_unavailable": (502, "Amazon Bedrock could not answer right now."),
    "bedrock_validation": (502, "{aws}"),
    "aws_auth_failed": (502, "AWS rejected the credentials."),
    "aws_token_expired": (
        502,
        "AWS rejected the credentials: the session token has expired. Temporary credentials "
        "(IAM Identity Center / STS) last only hours; paste a fresh access key id, secret and "
        "session token.",
    ),
    "aws_session_token_required": (
        409,
        "The access key id is a temporary key (ASIA…): it needs the session token. Paste the "
        "session token too, or use an IAM user key (AKIA…).",
    ),
    "aws_clock_skew": (
        502,
        "The server clock differs from AWS time by more than 5 minutes (NTP).",
    ),
    "rolesanywhere_failed": (502, "{aws}"),
    "aws_network": (502, "Cannot reach {host}: {aws}"),
    "assistant_timeout": (504, "The answer took too long."),
    "kb_unavailable": (502, "{aws}"),
    "assistant_unconfigured": (409, "The assistant connection is not configured."),
    "assistant_cap_reached": (429, "{aws}"),
}

# codes that put the assistant state into `error` (non-transient)
ERROR_STATE_CODES = frozenset(
    {
        "bedrock_model_access",
        "bedrock_access_denied",
        "bedrock_model_invalid",
        "aws_auth_failed",
        "aws_token_expired",
        "aws_session_token_required",
        "aws_clock_skew",
        "rolesanywhere_failed",
        "aws_network",
        "assistant_unconfigured",
    }
)

RA_HINT = (
    "Check the trust anchor (issuing CA), the profile and role ARNs, the role's trust policy "
    "(rolesanywhere.amazonaws.com, aws:SourceArn, x509Subject/CN) and an imported CRL."
)


@dataclass
class AssistantError(Exception):
    code: str
    message: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)
    aws_type: str | None = None
    http_status: int | None = None  # the AWS response status, when there was one

    def __post_init__(self) -> None:
        if not self.message:
            self.message = CODES.get(self.code, (502, self.code))[1].format(
                region="?", host="?", aws=self.code
            )
        super().__init__(self.message)

    @property
    def status(self) -> int:
        return CODES.get(self.code, (502, ""))[0]

    @property
    def is_error_state(self) -> bool:
        return self.code in ERROR_STATE_CODES

    @property
    def retry_policy(self) -> str | None:
        """`throttle` (2 retries 1 s / 4 s), `unavailable` (1 retry after 2 s), `auth` (one
        retry after a forced credential refresh), `network` (1 retry), or None."""
        return {
            "bedrock_throttled": "throttle",
            "bedrock_unavailable": "unavailable",
            "aws_auth_failed": "auth",
            "aws_token_expired": "auth",
            "aws_network": "network",
        }.get(self.code)

    def to_api(self) -> Any:
        from netmon.api.errors import ApiError

        return ApiError(self.status, self.code, self.message, self.details or None)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": list(self.details)}


def make(
    code: str, *, region: str = "", host: str = "", aws: str = "", **kw: Any
) -> AssistantError:
    _, tpl = CODES.get(code, (502, "{aws}"))
    msg = tpl.format(region=region or "the region", host=host or "?", aws=_cut(aws or code))
    return AssistantError(code, msg, **kw)


def _cut(text: str, n: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def error_type(headers: Mapping[str, str], body: Any) -> str | None:
    raw = None
    for k, v in headers.items():
        if k.lower() == "x-amzn-errortype" and v:
            raw = str(v).split(":", 1)[0]
            break
    if not raw and isinstance(body, Mapping):
        t = body.get("__type") or body.get("code") or body.get("Code")
        if isinstance(t, str) and t:
            raw = t.rsplit("#", 1)[-1]
    return raw.strip() if raw else None


def error_message(body: Any) -> str:
    if isinstance(body, Mapping):
        for key in ("message", "Message", "errorMessage"):
            v = body.get(key)
            if isinstance(v, str):
                return v
        err = body.get("Error")
        if isinstance(err, Mapping):
            return str(err.get("Message") or "")
    if isinstance(body, str):
        return body
    return ""


def parse_body(raw: bytes | str | None) -> Any:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError:
        return raw.strip()[:500]


_MODEL_ACCESS = re.compile(
    r"access to the model|model access|use case details|not have access to .*model|"
    r"subscription|marketplace",
    re.IGNORECASE,
)
_MODEL_INVALID = re.compile(
    r"model identifier is invalid|on-demand throughput|inference profile|"
    r"invalid model|model .*not (found|supported|exist)|provided model",
    re.IGNORECASE,
)
_SKEW = re.compile(r"signature expired|not yet current|clock|request time|time.*skew", re.I)
AUTH_TYPES = {
    "UnrecognizedClientException",
    "InvalidSignatureException",
    "ExpiredTokenException",
    "ExpiredToken",
    "IncompleteSignature",
    "IncompleteSignatureException",
    "SignatureDoesNotMatch",
    "InvalidClientTokenId",
    "MissingAuthenticationToken",
    "MissingAuthenticationTokenException",
    "InvalidAccessKeyId",
}
EXPIRED_TOKEN_TYPES = {"expiredtokenexception", "expiredtoken"}
UNAVAILABLE_TYPES = {
    "ModelTimeoutException",
    "ModelErrorException",
    "ModelNotReadyException",
    "ServiceUnavailableException",
    "ServiceUnavailable",
    "InternalServerException",
    "InternalFailure",
    "InternalServerError",
    "modelStreamErrorException",
    "ModelStreamErrorException",
    "internalServerException",
    "serviceUnavailableException",
}
_UNAVAILABLE_LOWER = {u.lower() for u in UNAVAILABLE_TYPES}


def classify(
    aws_type: str | None, message: str, status: int | None, *, region: str = ""
) -> AssistantError:
    t = aws_type or ""
    tl = t.lower()
    kw: dict[str, Any] = {"aws_type": aws_type, "http_status": status}
    # Unavailable types first: ModelNotReadyException arrives as HTTP 429 but is not throttling.
    if t in UNAVAILABLE_TYPES or tl in _UNAVAILABLE_LOWER:
        return make("bedrock_unavailable", **kw)
    if tl.startswith("throttling") or tl == "toomanyrequestsexception" or status == 429:
        return make("bedrock_throttled", **kw)
    if tl == "servicequotaexceededexception":
        return make("bedrock_quota", **kw)
    if t in AUTH_TYPES or tl in {a.lower() for a in AUTH_TYPES}:
        if _SKEW.search(message):
            return make("aws_clock_skew", **kw)
        if tl in EXPIRED_TOKEN_TYPES:
            # a distinct code: the admin must paste fresh temporary credentials
            return make("aws_token_expired", **kw)
        err = make("aws_auth_failed", **kw)
        # which check failed matters (unknown key id vs. wrong secret / altered request);
        # AWS messages here never contain the secret
        err.message = f"{err.message} ({t}" + (f": {_cut(message, 200)})" if message else ")")
        return err
    if tl == "accessdeniedexception" or (status == 403 and not t):
        if _MODEL_ACCESS.search(message):
            return make("bedrock_model_access", region=region, **kw)
        err = make("bedrock_access_denied", **kw)
        if message:
            err.message = f"{err.message} ({_cut(message, 200)})"
        return err
    if tl == "resourcenotfoundexception":
        # e.g. "Model use case details have not been submitted" is a ResourceNotFound too
        if _MODEL_ACCESS.search(message):
            return _with_aws(make("bedrock_model_access", region=region, **kw), t, message)
        return _with_aws(make("bedrock_model_invalid", region=region, **kw), t, message)
    if tl == "validationexception":
        if _MODEL_INVALID.search(message):
            return _with_aws(make("bedrock_model_invalid", region=region, **kw), t, message)
        return make("bedrock_validation", aws=message or t, **kw)
    if t in UNAVAILABLE_TYPES or (status is not None and status >= 500):
        return make("bedrock_unavailable", **kw)
    if status == 401:
        return make("aws_auth_failed", **kw)
    return make("bedrock_validation", aws=message or t or f"HTTP {status}", **kw)


def _with_aws(err: AssistantError, aws_type: str, message: str) -> AssistantError:
    """Keep AWS's own wording next to ours (owner's office test: "model ID is not valid"
    alone hid the real reason). AWS error messages never contain credentials."""
    if message:
        err.message = f"{err.message} ({aws_type or 'AWS'}: {_cut(message, 240)})"
    return err


def map_error(
    status: int, headers: Mapping[str, str], body: bytes | str | Any, *, region: str = ""
) -> AssistantError:
    """Pure: an AWS error response → AssistantError (§ 14.2.4)."""
    parsed = body if isinstance(body, Mapping) else parse_body(body)
    return classify(error_type(headers, parsed), error_message(parsed), status, region=region)


def map_stream_exception(
    exception_type: str | None, payload: Any, *, region: str = ""
) -> AssistantError:
    """An in-stream exception frame (`:message-type` = `exception`)."""
    return classify(exception_type, error_message(payload), None, region=region)


def map_rolesanywhere(status: int, headers: Mapping[str, str], body: bytes | str) -> AssistantError:
    parsed = parse_body(body)
    msg = error_message(parsed) or f"HTTP {status}"
    t = error_type(headers, parsed)
    if _SKEW.search(msg):
        return make("aws_clock_skew", aws_type=t, http_status=status)
    if status >= 500 or status == 429:
        err = classify(t, msg, status)
        if err.code in ("bedrock_unavailable", "bedrock_throttled"):
            return make("rolesanywhere_failed", aws=f"{msg} (HTTP {status})", aws_type=t)
    return make(
        "rolesanywhere_failed", aws=f"{t + ': ' if t else ''}{msg}. {RA_HINT}", aws_type=t,
        http_status=status,
    )  # fmt: skip


def map_exception(exc: BaseException, *, host: str, via: str) -> AssistantError:
    """Connect / TLS / proxy / DNS errors and our timeouts."""
    import httpx

    if isinstance(exc, AssistantError):
        return exc
    if isinstance(exc, TimeoutError | httpx.ReadTimeout | httpx.PoolTimeout | httpx.WriteTimeout):
        return make("assistant_timeout")
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    if isinstance(exc, httpx.ProxyError):
        text = f"proxy error: {exc}"
    err = make("aws_network", host=host, aws=text)
    err.details = [{"host": host, "via": via}]
    return err
