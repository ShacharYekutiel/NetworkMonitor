"""AWS STS mock (Query API) for NetworkMonitor PR 4-0a (docs/architecture.md § 15.1.11).

Mounted on the shared Starlette app of the Bedrock mock (`tests/sim/bedrock/mock_bedrock.py`):
`POST /` with a form body (`Action=GetCallerIdentity` | `Action=AssumeRole`), signed with SigV4
service `sts` and verified with botocore (`sigv4_check.verify_sigv4`, test-only dependency).

`AssumeRole` evaluates a small trust model configured per test (`POST /_sts/roles` or
`MockBedrock.sts_config(...)`):

    roles:   [{"arn", "trusted_principals": [role ARN], "external_id": str | null,
               "trust_allows": ["sts:AssumeRole", "sts:SetSourceIdentity", "sts:TagSession"],
               "require_source_identity": "CN=…" | null, "require_tag_session": false}]
    callers: [{"arn": <role or user ARN of the caller>, "allows": ["sts:AssumeRole",
               "sts:SetSourceIdentity"], "resources": [role ARN] | ["*"]}]
    disabled_regions: ["eu-south-2"]           # RegionDisabledException

Answers use the real XML error shape and messages ("User: <arn> is not authorized to perform:
sts:SetSourceIdentity on resource: <arn>"), `ValidationError`, `RegionDisabledException`,
`ExpiredToken`. Sessions of a chained call last at most 3600 s on the mock clock; the source
identity of the calling session is inherited.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from typing import Any
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

NS = "https://sts.amazonaws.com/doc/2011-06-15/"
STATIC_USER = "arn:aws:iam::111122223333:user/netmon-static"


def default_config() -> dict[str, Any]:
    return {"roles": {}, "callers": {}, "disabled_regions": [], "chained_max_seconds": 3600,
            "session_seconds": None}


def _xml_error(status: int, code: str, message: str, etype: str = "Sender") -> Response:
    body = (f'<ErrorResponse xmlns="{NS}"><Error><Type>{etype}</Type><Code>{code}</Code>'
            f"<Message>{_esc(message)}</Message></Error><RequestId>{uuid.uuid4()}</RequestId>"
            "</ErrorResponse>")
    return Response(body, status_code=status, media_type="text/xml")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def role_of(arn: str | None) -> str | None:
    """assumed-role session ARN → role ARN; other ARNs unchanged."""
    if not arn or ":assumed-role/" not in arn:
        return arn
    head, _, rest = arn.partition(":assumed-role/")
    return f"arn:aws:iam::{head.rsplit(':', 1)[-1]}:role/{rest.split('/', 1)[0]}"


def denied(caller: str, action: str, resource: str) -> Response:
    return _xml_error(403, "AccessDenied",
                      f"User: {caller} is not authorized to perform: {action} on resource: {resource}")


def configure(state: Any, body: dict[str, Any]) -> dict[str, Any]:
    cfg = state.sts
    if body.get("reset"):
        cfg.clear()
        cfg.update(default_config())
    for r in body.get("roles") or []:
        r = {"trusted_principals": [], "external_id": None,
             "trust_allows": ["sts:AssumeRole", "sts:SetSourceIdentity"],
             "require_source_identity": None, "require_tag_session": False, **r}
        cfg["roles"][r["arn"]] = r
    for c in body.get("callers") or []:
        c = {"allows": ["sts:AssumeRole", "sts:SetSourceIdentity"], "resources": ["*"], **c}
        cfg["callers"][c["arn"]] = c
    for k in ("disabled_regions", "chained_max_seconds", "session_seconds"):
        if k in body:
            cfg[k] = body[k]
    return cfg


def make_handlers(state: Any, finish: Any, base_entry: Any) -> tuple[Any, Any]:
    """(sts route handler, control route handler) bound to the mock state."""

    async def sts(request: Request) -> Response:
        import sigv4_check  # noqa: PLC0415 - the mock's own module path

        body = await request.body()
        entry = state.record(base_entry(request, "sts", b""))
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
        action = form.get("Action", "")
        entry["op"] = "sts_" + ("assume_role" if action == "AssumeRole" else
                                "get_caller_identity" if action == "GetCallerIdentity" else action)
        entry["body"] = {k: ("***" if k == "ExternalId" else v) for k, v in form.items()}
        entry["external_id_sent"] = form.get("ExternalId")
        path = request.scope.get("raw_path", b"/").decode("latin-1").partition("?")[0]
        if state.config.get("verify_signatures", True):
            ok, etype, msg, info = sigv4_check.verify_sigv4(
                "POST", request.headers.get("host", ""), path, "", dict(request.headers), body,
                lookup_secret=state.lookup_secret, now=state.now(), expected_service="sts")
        else:
            a = sigv4_check.parse_auth(request.headers.get("authorization")) or {}
            ok, etype, msg, info = True, None, "", {"region": a.get("region"),
                                                     "access_key_id": a.get("key")}
        entry["sig"] = {"ok": ok, "error": etype, "message": msg}
        region = info.get("region") or "us-east-1"
        entry["region"] = region
        if not ok:
            code = "ExpiredToken" if etype in ("ExpiredTokenException", "ExpiredToken") else (
                "SignatureDoesNotMatch" if etype == "InvalidSignatureException" else
                "InvalidClientTokenId")
            return finish(entry, _xml_error(403, code, msg))
        mode = state.take_mode("sts")
        if mode and mode["name"] == "expired_token":
            return finish(entry, _xml_error(403, "ExpiredToken", "The security token included in the request is expired"))
        if mode and mode["name"] == "unavailable":
            return finish(entry, _xml_error(503, "ServiceUnavailable", "Service unavailable", "Receiver"))
        akid = info.get("access_key_id") or ""
        ident = state.identity_of(akid)
        if region in (state.sts.get("disabled_regions") or []):
            return finish(entry, _xml_error(
                403, "RegionDisabledException",
                f"STS is not activated in this region for account:{ident['account']}. Your account "
                "administrator can activate STS in this region using the IAM Console."))
        if action == "GetCallerIdentity":
            return finish(entry, Response(
                f'<GetCallerIdentityResponse xmlns="{NS}"><GetCallerIdentityResult>'
                f"<Arn>{ident['arn']}</Arn><UserId>{ident['user_id']}</UserId>"
                f"<Account>{ident['account']}</Account></GetCallerIdentityResult>"
                f"<ResponseMetadata><RequestId>{uuid.uuid4()}</RequestId></ResponseMetadata>"
                "</GetCallerIdentityResponse>", media_type="text/xml"))
        if action != "AssumeRole":
            return finish(entry, _xml_error(400, "InvalidAction", f"Could not find operation {action} for version"))
        return finish(entry, assume_role(form, ident, entry))

    def assume_role(form: dict[str, str], ident: dict[str, Any], entry: dict[str, Any]) -> Response:
        cfg = state.sts
        role_arn = form.get("RoleArn", "")
        caller_arn = ident["arn"]
        caller_key = role_of(caller_arn)
        if not role_arn.startswith("arn:aws:iam::") or ":role/" not in role_arn:
            return _xml_error(400, "ValidationError",
                              f"1 validation error detected: Value '{role_arn}' at 'roleArn' failed to "
                              "satisfy constraint: Member must have length greater than or equal to 20")
        for p in ("Tags", "TransitiveTagKeys", "SourceIdentity", "Policy", "PolicyArns"):
            if any(k.startswith(p) for k in form):
                entry["forbidden_param"] = p
        caller = cfg["callers"].get(caller_key) or cfg["callers"].get(caller_arn)
        role = cfg["roles"].get(role_arn)
        src = ident.get("source_identity")

        def allows(c: dict[str, Any] | None, action: str) -> bool:
            return bool(c) and action in c.get("allows", []) and (
                "*" in c.get("resources", []) or role_arn in c.get("resources", []))

        if not allows(caller, "sts:AssumeRole"):
            return denied(caller_arn, "sts:AssumeRole", role_arn)
        if role is None or caller_key not in role.get("trusted_principals", []) or \
                "sts:AssumeRole" not in role.get("trust_allows", []):
            return denied(caller_arn, "sts:AssumeRole", role_arn)
        if role.get("external_id") and form.get("ExternalId") != role["external_id"]:
            return denied(caller_arn, "sts:AssumeRole", role_arn)
        if src:
            if not allows(caller, "sts:SetSourceIdentity") or \
                    "sts:SetSourceIdentity" not in role.get("trust_allows", []):
                return denied(caller_arn, "sts:SetSourceIdentity", role_arn)
        if role.get("require_source_identity") and role["require_source_identity"] != src:
            return denied(caller_arn, "sts:AssumeRole", role_arn)
        if role.get("require_tag_session") and "sts:TagSession" not in role.get("trust_allows", []):
            return denied(caller_arn, "sts:TagSession", role_arn)
        dur = int(form.get("DurationSeconds") or 3600)
        chained = ":assumed-role/" in caller_arn
        if chained and dur > int(cfg.get("chained_max_seconds") or 3600):
            return _xml_error(400, "ValidationError",
                              "The requested DurationSeconds exceeds the 1 hour session limit for roles "
                              "assumed by role chaining.")
        if cfg.get("session_seconds"):
            dur = int(cfg["session_seconds"])
        acct = role_arn.split(":")[4]
        name = role_arn.split(":role/", 1)[1].rsplit("/", 1)[-1]
        sess = form.get("RoleSessionName", "s")
        arn = f"arn:aws:sts::{acct}:assumed-role/{name}/{sess}"
        cred = state.issue_session("assume_role", dur, arn=arn, source_identity=src)
        entry["issued_access_key_id"] = cred["access_key_id"]
        entry["duration_seconds"] = dur
        si = f"<SourceIdentity>{_esc(src)}</SourceIdentity>" if src else ""
        return Response(
            f'<AssumeRoleResponse xmlns="{NS}"><AssumeRoleResult>{si}'
            f"<AssumedRoleUser><AssumedRoleId>AROAMOCK{uuid.uuid4().hex[:12].upper()}:{sess}</AssumedRoleId>"
            f"<Arn>{arn}</Arn></AssumedRoleUser><Credentials>"
            f"<AccessKeyId>{cred['access_key_id']}</AccessKeyId>"
            f"<SecretAccessKey>{cred['secret']}</SecretAccessKey>"
            f"<SessionToken>{cred['token']}</SessionToken>"
            f"<Expiration>{_iso(cred['expires_at'])}</Expiration></Credentials>"
            "<PackedPolicySize>0</PackedPolicySize></AssumeRoleResult>"
            f"<ResponseMetadata><RequestId>{uuid.uuid4()}</RequestId></ResponseMetadata>"
            "</AssumeRoleResponse>", media_type="text/xml")

    async def control(request: Request) -> Response:
        if request.method == "POST":
            b = await request.body()
            import json  # noqa: PLC0415

            configure(state, json.loads(b) if b else {})
        return JSONResponse(state.sts)

    return sts, control


def identity(state: Any, akid: str) -> dict[str, Any]:
    """Who an access key id is: static user, Roles Anywhere / STS session, IMDS, …"""
    with state.lock:
        s = state.sessions.get(akid)
    if s and s.get("arn"):
        arn = s["arn"]
        return {"arn": arn, "account": arn.split(":")[4], "user_id": f"AROAMOCK:{arn.rsplit('/', 1)[-1]}",
                "source_identity": s.get("source_identity")}
    if s:
        arn = "arn:aws:sts::111122223333:assumed-role/netmon-bedrock/i-mock"
        return {"arn": arn, "account": "111122223333", "user_id": "AROAMOCK:i-mock", "source_identity": None}
    return {"arn": STATIC_USER, "account": "111122223333", "user_id": "AIDAMOCKSTATIC",
            "source_identity": None, "static": True, "time": time.time()}
