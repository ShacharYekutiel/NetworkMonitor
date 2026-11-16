"""[3b] Assistant error type and the pure AWS error mapping (architecture § 14.2.4,
api.md "[3b] Errors"). [4] Moved from `netmon.assistant.errors` (PR 4-0a): `AwsError` is an
alias of `AssistantError`; adds the STS mapping (`map_sts`) and the Test hint builder
(`hints_for`, architecture § 15.1.7).

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
    # [4] shared AWS connections (architecture § 15.1.8)
    "aws_assume_role_denied": (502, "AWS STS refused the role hop: {aws}"),
    "aws_assume_role_invalid": (502, "AWS STS rejected the role request: {aws}"),
    "aws_connection_unconfigured": (409, "The AWS connection is missing or incomplete."),
    "aws_throttled": (502, "AWS is busy (throttled). Try again in a minute."),
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
        "aws_assume_role_denied",
        "aws_assume_role_invalid",
        "aws_connection_unconfigured",
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


AwsError = AssistantError  # [4] the error type of every AWS call (same class)


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
        # A rejected request *feature* (forced toolChoice, sampling params, prompt caching) also
        # says "... not supported for this model", which _MODEL_INVALID would misread as a bad
        # model id. Classify those as bedrock_validation so the client's compat-retry (drop the
        # feature, try again) fires instead of surfacing a misleading "model id not valid".
        ml = message.lower()
        if (
            "toolchoice" in ml.replace("_", "")
            or any(w in ml for w in ("temperature", "top_p", "topp", "sampling"))
            or "cache" in ml
        ):
            return make("bedrock_validation", aws=message or t, **kw)
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


# ---------------------------------------------------------------- [4] STS (Query protocol)
STS_AUTH_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "SignatureDoesNotMatch",
    "IncompleteSignature",
    "MissingAuthenticationToken",
    "InvalidAccessKeyId",
    "UnrecognizedClientException",
    "RequestExpired",
}
STS_INVALID_CODES = {
    "ValidationError",
    "MalformedPolicyDocument",
    "PackedPolicyTooLarge",
    "RegionDisabledException",
    "InvalidParameterValue",
}


def _between(text: str, start: str, end: str | None) -> str | None:
    """Fixed-string parser (no regular expression on AWS text): the word after `start`."""
    i = text.find(start)
    if i < 0:
        return None
    rest = text[i + len(start) :].strip()
    if end is not None:
        j = rest.find(end)
        if j >= 0:
            rest = rest[:j]
    word = rest.split()[0] if rest.split() else ""
    return word.rstrip(".,;") or None


def denied_parts(message: str) -> dict[str, str | None]:
    """`User: <arn> is not authorized to perform: <action> on resource: <arn>` → parts."""
    action = _between(message, "to perform:", " on ")
    return {
        "from_arn": _between(message, "User:", " is not"),
        "action": action if action and action.startswith("sts:") else None,
        "role_arn": _between(message, "on resource:", None),
    }


def map_sts(
    status: int,
    body: bytes | str | None,
    *,
    op: str = "AssumeRole",
    from_arn: str | None = None,
    role_arn: str | None = None,
) -> AssistantError:
    """Pure: an STS error answer (XML `ErrorResponse`) → AwsError (architecture § 15.1.7)."""
    from netmon.aws import awsxml

    try:
        code, message = awsxml.parse_error(body)
    except awsxml.XmlError as exc:
        return make("aws_network", host="sts", aws=f"invalid response ({exc})")
    code = code or ""
    kw: dict[str, Any] = {"aws_type": code or None, "http_status": status}
    text = f"{code}: {message}" if code else (message or f"HTTP {status}")
    if code in STS_AUTH_CODES or status == 401:
        if _SKEW.search(message or "") or code == "RequestExpired":
            return make("aws_clock_skew", **kw)
        if code.lower() in EXPIRED_TOKEN_TYPES:
            return make("aws_token_expired", **kw)
        err = make("aws_auth_failed", **kw)
        err.message = f"{err.message} ({_cut(text, 200)})"
        return err
    if code.startswith("Throttling") or status == 429:
        return make("aws_throttled", **kw)
    if code == "AccessDenied" or (status == 403 and code not in STS_INVALID_CODES):
        parts = denied_parts(message or "")
        if op != "AssumeRole":
            err = make("aws_auth_failed", **kw)
            err.message = f"{err.message} ({_cut(text, 200)})"
            return err
        return make(
            "aws_assume_role_denied",
            aws=text,
            details=[
                {
                    "hop": 1,
                    "from_arn": parts["from_arn"] or from_arn,
                    "role_arn": parts["role_arn"] or role_arn,
                    "action": parts["action"],
                }
            ],
            **kw,
        )
    if code in STS_INVALID_CODES or status == 400:
        return make(
            "aws_assume_role_invalid",
            aws=text,
            details=[{"hop": 1, "aws_code": code or None, "role_arn": role_arn}],
            **kw,
        )
    return make("aws_network", host="sts", aws=f"{text} (HTTP {status})", **kw)


# ---------------------------------------------------------------- [4] Test hints (§ 15.1.7)
HINT_TEXTS: dict[str, str] = {
    "ra_trust_policy": (
        "The role's trust policy must allow sts:AssumeRole, sts:TagSession, sts:SetSourceIdentity "
        "for rolesanywhere.amazonaws.com, with aws:SourceArn exactly {trust_anchor_arn} — once. A "
        "trust anchor ARN pasted into a template that already contains ':trust-anchor/' gives a "
        "doubled value (':trust-anchor/arn:aws:rolesanywhere:…') that never matches."
    ),
    "ra_subject_cn": (
        "If the trust policy checks aws:PrincipalTag/x509Subject/CN, its value must equal the "
        "certificate's CN: {cert_cn} (from the uploaded certificate). The CN typed in the CSR or "
        "in the template request is not always the CN the CA issued — compare with the certificate."
    ),
    "ra_issuer_cn": ("If it checks aws:PrincipalTag/x509Issuer/CN, the value must be {issuer_cn}."),
    "ra_profile_role": (
        "The profile {profile_arn} must list the role {role_arn}, be enabled, and be in the same "
        "account as the role."
    ),
    "ra_untrusted_cert": (
        "The trust anchor {trust_anchor_arn} does not trust the CA that issued this certificate "
        "(issuer {issuer_cn}). Upload the chain with the intermediate CA, or register the issuing "
        "CA in the trust anchor."
    ),
    "ra_disabled": "Enable the trust anchor / profile in IAM Roles Anywhere.",
    "ra_revoked": ("The certificate is revoked in an imported CRL or a CRL distribution point."),
    "set_source_identity": (
        "Roles Anywhere set the source identity {source_identity}, so this hop needs "
        "sts:SetSourceIdentity in two places: the permissions policy of {from_arn} (resource "
        "{role_arn}) and the trust policy of {role_arn} (in its Action list)."
    ),
    "tag_session": (
        "AWS asks for sts:TagSession on this hop: add it to the trust policy of {role_arn} and to "
        "the permissions policy of {from_arn}. The app passes no session tags; this appears only "
        "when tags were made transitive in your organisation."
    ),
    "caller_policy": "{from_arn} needs sts:AssumeRole on {role_arn}.",
    "trust_policy": (
        "The trust policy of {role_arn} must name the principal {base_role_arn} (the role, not the "
        "assumed-role session ARN)."
    ),
    "external_id": ("The trust policy's sts:ExternalId must equal the ExternalId in this form."),
    "scp": "A service control policy of the organisation may deny sts:AssumeRole.",
    "sts_region": (
        "STS is not activated in {region} for account {account}; activate it (IAM → Account "
        "settings) or choose another connection region."
    ),
    "role_arn_invalid": "Check the role ARN {role_arn}.",
    "max_session": "The role's maximum session duration must be at least 1 hour.",
    "vpc_endpoint_or_proxy": (
        "Allow {host} through the proxy, or add an interface VPC endpoint for {service} and enter "
        "it under Endpoint overrides as ({service}, {region})."
    ),
    "cn_not_source_identity_safe": (
        "The CN {cert_cn} contains characters a source identity cannot carry. Prefer a CN of "
        "letters, digits and . - _ @."
    ),
    # [4-0a, additive] the Bedrock steps of the assistant Test
    "model_access": (
        "Enable access to this model in the Bedrock console of {region} (Bedrock → Model access) "
        "for the account {account}."
    ),
}


class _Fmt(dict):  # type: ignore[type-arg]
    def __missing__(self, key: str) -> str:
        return "?"


def hint(code: str, **values: Any) -> dict[str, Any]:
    """`{code, text, values}`; `values` = the concrete values the text refers to (non-empty)."""
    vals = {k: str(v) for k, v in values.items() if v not in (None, "")}
    text = HINT_TEXTS[code].format_map(_Fmt(vals))
    used = {k: v for k, v in vals.items() if "{" + k + "}" in HINT_TEXTS[code]}
    return {"code": code, "text": text, "values": used}


def hints_for(step: str, err: AssistantError, ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure hint builder (data-model "[4] Pure vectors"): the failing step, its error and the
    values the app already knows (`trust_anchor_arn`, `profile_arn`, `role_arn` (the Roles
    Anywhere role of the step), `cert_cn`, `issuer_cn`, `base_kind`, `base_role_arn`,
    `from_arn`, `hop_role_arn`, `source_identity`, `region`, `account`). Never calls AWS."""
    c = ctx
    msg = (err.message or "").lower()
    if err.code == "aws_network":
        host = (err.details[0].get("host") if err.details else None) or c.get("host") or ""
        service = c.get("service") or (host.split(".", 1)[0] if host else "")
        if service not in ("sts", "rolesanywhere"):
            return []
        return [hint("vpc_endpoint_or_proxy", host=host, service=service, region=c.get("region"))]
    if err.code == "rolesanywhere_failed":
        ra = dict(
            trust_anchor_arn=c.get("trust_anchor_arn"),
            profile_arn=c.get("profile_arn"),
            role_arn=c.get("role_arn"),
            cert_cn=c.get("cert_cn"),
            issuer_cn=c.get("issuer_cn"),
        )
        if "unable to assume role" in msg:
            return [
                hint(h, **ra)
                for h in ("ra_trust_policy", "ra_subject_cn", "ra_issuer_cn", "ra_profile_role")
            ]
        if "untrusted" in msg or "certificate chain" in msg:
            return [hint("ra_untrusted_cert", **ra)]
        if "is disabled" in msg:
            return [hint("ra_disabled")]
        if "revoked" in msg:
            return [hint("ra_revoked")]
        if "not found" in msg or "validation" in msg or "invalid arn" in msg:
            return [
                hint("ra_profile_role", **ra),
                hint("role_arn_invalid", role_arn=ra["role_arn"]),
            ]
        return []
    if err.code == "aws_assume_role_denied":
        d = err.details[0] if err.details else {}
        vals = dict(
            from_arn=d.get("from_arn") or c.get("from_arn"),
            role_arn=d.get("role_arn") or c.get("hop_role_arn"),
            base_role_arn=c.get("base_role_arn"),
            source_identity=c.get("source_identity"),
        )
        action = d.get("action")
        if action == "sts:SetSourceIdentity":
            return [hint("set_source_identity", **vals)]
        if action == "sts:TagSession":
            return [hint("tag_session", **vals)]
        codes = ["caller_policy", "trust_policy", "external_id"]
        if c.get("base_kind") == "roles_anywhere":
            codes.append("set_source_identity")
        codes.append("scp")
        return [hint(h, **vals) for h in codes]
    if err.code == "aws_assume_role_invalid":
        aws_code = (err.details[0].get("aws_code") if err.details else None) or err.aws_type
        if aws_code == "RegionDisabledException":
            return [hint("sts_region", region=c.get("region"), account=c.get("account"))]
        if "durationseconds" in msg or "duration" in msg:
            return [hint("max_session")]
        return [hint("role_arn_invalid", role_arn=c.get("hop_role_arn"))]
    if err.code == "bedrock_model_access":
        return [hint("model_access", region=c.get("bedrock_region"), account=c.get("account"))]
    return []
