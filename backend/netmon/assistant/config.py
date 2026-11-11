"""[3b] Assistant settings: defaults, storage, secret handling, validation, connection hash
(data-model "[3b] Settings").

Stored rows: `assistant` (connection and data-sharing object), `assistant_kb` (Knowledge Base
object) and the scalar keys of `SCALAR_DEFAULTS` (registered in
`netmon.services.settings.DEFAULTS`, so they appear in `GET/PUT /api/settings`). The two
objects are never part of `GET /api/settings`; they are served by `/api/assistant/settings`
and `/api/assistant/knowledge` (secrets write-only)."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

from netmon.services import crypto

AAD_SECRET_ACCESS_KEY = b"assistant.secret_access_key"
AAD_SESSION_TOKEN = b"assistant.session_token"
AAD_RA_KEY = b"assistant.ra_key"
AAD_RA_CSR_KEY = b"assistant.ra_csr_key"

ENV_CERT_FILE = "NETMON_AWS_RA_CERT_FILE"
ENV_KEY_FILE = "NETMON_AWS_RA_KEY_FILE"
ENV_CHAIN_FILE = "NETMON_AWS_RA_CHAIN_FILE"

DEFAULT_ASSISTANT: dict[str, Any] = {
    "auth_mode": "roles_anywhere",
    # owner decision (3b): recommended setup is us-east-1 with a US geographic profile (us.);
    # il-central-1 with a global. profile is fully supported. Never a hard-coded model id.
    "region": "us-east-1",
    "model_id": "",
    "model_id_light": None,
    "endpoint_url": None,
    "control_endpoint_url": None,
    "proxy_mode": "env",
    "proxy_url": None,
    "prompt_caching": True,
    "access_key_id": None,
    "secret_access_key_enc": None,
    # optional session token of temporary credentials (ASIA… keys, e.g. IAM Identity Center)
    "session_token_enc": None,
    "access_key_created_at": None,
    "ra_region": None,
    "ra_endpoint_url": None,
    "ra_trust_anchor_arn": None,
    "ra_profile_arn": None,
    "ra_role_arn": None,
    "ra_session_seconds": 3600,
    "ra_cert_pem": None,
    "ra_chain_pem": None,
    "ra_key_enc": None,
    "ra_cert_uploaded_at": None,
    # owner decision (3b): "Generate CSR" — key generated and kept on the server (encrypted)
    "ra_csr_pem": None,
    "ra_csr_key_enc": None,
    "ra_csr_subject": None,
    "ra_csr_created_at": None,
    "send_config_excerpts": False,
    "redact_internal_ips": False,
    "internal_cidrs": [],
    "chat_redact_ips": True,
    "prices": {
        "currency": "USD",
        "input_per_mtok": None,
        "output_per_mtok": None,
        "cache_read_per_mtok": None,
        "cache_write_per_mtok": None,
    },
    "last_test": None,  # {"ok", "at", "by", "error_code"}
}

DEFAULT_KB: dict[str, Any] = {
    "knowledge_base_id": "",
    "region": None,
    "endpoint_url": None,
    "results": 5,
    "min_score": 0.0,
    "description": "",
    "last_test": None,
}

# scalar keys (GET/PUT /api/settings). Owner decision (3b): the question text is never written
# to the audit log, so there is no `assistant_audit_question_text` key.
SCALAR_DEFAULTS: dict[str, Any] = {
    "assistant_daily_questions_per_user": 50,
    "assistant_monthly_token_cap": 5_000_000,
    "assistant_monthly_cost_cap": None,
    "assistant_background_stop_pct": 80,
    "assistant_max_model_calls": 8,
    "assistant_max_tool_calls": 12,
    "assistant_max_output_tokens": 2000,
    "assistant_question_timeout_s": 120,
    "assistant_max_concurrent": 4,
    "assistant_context_budget_tokens": 40000,
    "assistant_retention_days": 90,
    "assistant_artifact_retention_days": 180,
    "correlation_window_min": 5,
    "correlation_min_alerts": 3,
    "correlation_attach_to_alert": False,
    "correlation_daily_summaries": 50,
    "alert_note_timeout_s": 45,
    "alert_notes_daily_cap": 100,
    "config_diff_auto": False,
    "config_diff_daily_cap": 50,
    "digest_time": "07:17",
    "digest_language": None,
    "digest_email_to": [],
    "digest_channel_ids": [],
    "chat_ask_per_user_hour": 10,
}

CONNECTION_FIELDS = (
    "auth_mode",
    "region",
    "model_id",
    "model_id_light",
    "endpoint_url",
    "control_endpoint_url",
    "proxy_mode",
    "proxy_url",
    "prompt_caching",
    "access_key_id",
    "ra_region",
    "ra_endpoint_url",
    "ra_trust_anchor_arn",
    "ra_profile_arn",
    "ra_role_arn",
    "ra_session_seconds",
)
KB_CONNECTION_FIELDS = ("knowledge_base_id", "region", "endpoint_url", "results", "min_score")
# writable through PUT /api/assistant/settings (plus the secrets `secret_access_key` and
# `session_token`)
WRITABLE_FIELDS = (
    *CONNECTION_FIELDS,
    "access_key_created_at",
    "send_config_excerpts",
    "redact_internal_ips",
    "internal_cidrs",
    "chat_redact_ips",
    "prices",
)
KB_WRITABLE_FIELDS = (*KB_CONNECTION_FIELDS, "description")

REGION_RE = re.compile(r"^[a-z]{2}(-gov|-iso[a-z]*)?-[a-z]+-\d$")
MODEL_RE = re.compile(r"^[A-Za-z0-9.:/_-]{1,256}$")
ACCESS_KEY_RE = re.compile(r"^(AKIA|ASIA)[A-Z0-9]{16}$")
SESSION_TOKEN_MAX = 4096
# write-only secrets of PUT /api/assistant/settings: field -> (stored field, AAD)
SECRET_INPUTS: dict[str, tuple[str, bytes]] = {
    "secret_access_key": ("secret_access_key_enc", AAD_SECRET_ACCESS_KEY),
    "session_token": ("session_token_enc", AAD_SESSION_TOKEN),
}
TRUST_ANCHOR_RE = re.compile(
    r"^arn:aws[a-z-]*:rolesanywhere:[a-z0-9-]+:\d{12}:trust-anchor/[0-9a-zA-Z-]+$"
)
PROFILE_RE = re.compile(r"^arn:aws[a-z-]*:rolesanywhere:[a-z0-9-]+:\d{12}:profile/[0-9a-zA-Z-]+$")
ROLE_RE = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]{1,512}$")
KB_ID_RE = re.compile(r"^[0-9A-Z]{10}$")


# ---------------------------------------------------------------- storage
async def load(session: Any) -> dict[str, Any]:
    from netmon.models import Setting

    row = await session.get(Setting, "assistant")
    out = copy.deepcopy(DEFAULT_ASSISTANT)
    if row is not None and isinstance(row.value, dict):
        out.update(row.value)
        out["prices"] = {**DEFAULT_ASSISTANT["prices"], **(row.value.get("prices") or {})}
    return out


async def load_kb(session: Any) -> dict[str, Any]:
    from netmon.models import Setting

    row = await session.get(Setting, "assistant_kb")
    out = copy.deepcopy(DEFAULT_KB)
    if row is not None and isinstance(row.value, dict):
        out.update(row.value)
    return out


async def save(session: Any, value: dict[str, Any]) -> None:
    from netmon.services import settings as settings_svc

    await settings_svc.put(session, "assistant", value)


async def save_kb(session: Any, value: dict[str, Any]) -> None:
    from netmon.services import settings as settings_svc

    await settings_svc.put(session, "assistant_kb", value)


async def scalars(session: Any) -> dict[str, Any]:
    """The 3b scalar keys (stored value or default)."""
    from sqlalchemy import select

    from netmon.models import Setting

    out = copy.deepcopy(SCALAR_DEFAULTS)
    rows = await session.execute(select(Setting).where(Setting.key.in_(list(SCALAR_DEFAULTS))))
    for row in rows.scalars():
        out[row.key] = row.value
    return out


# ---------------------------------------------------------------- secrets
class Unreadable(ValueError):
    """A stored secret cannot be decrypted (secret.key replaced)."""


def decrypt_opt(secret_key: str, value: str | None, aad: bytes) -> str | None:
    if not value:
        return None
    try:
        return crypto.decrypt(secret_key, value, aad)
    except crypto.DecryptError as exc:
        raise Unreadable(str(exc)) from None


def secret_access_key(stored: dict[str, Any], secret_key: str) -> str | None:
    return decrypt_opt(secret_key, stored.get("secret_access_key_enc"), AAD_SECRET_ACCESS_KEY)


def session_token(stored: dict[str, Any], secret_key: str) -> str | None:
    return decrypt_opt(secret_key, stored.get("session_token_enc"), AAD_SESSION_TOKEN)


def is_temporary_key(access_key_id: str | None) -> bool:
    """`ASIA…` ids are temporary (STS / IAM Identity Center): they need a session token."""
    return bool(access_key_id) and str(access_key_id).startswith("ASIA")


def temporary_credentials(cfg: dict[str, Any]) -> bool:
    """Access-key mode with a stored session token (warning `temporary_credentials`)."""
    return cfg.get("auth_mode") == "access_key" and bool(cfg.get("session_token_enc"))


def secret_unreadable(stored: dict[str, Any], secret_key: str, field: str, aad: bytes) -> bool:
    try:
        decrypt_opt(secret_key, stored.get(field), aad)
    except Unreadable:
        return True
    return False


def host_files() -> dict[str, str] | None:
    """`{"cert", "key", "chain"?}` paths when both NETMON_AWS_RA_CERT_FILE and _KEY_FILE are
    set (they take precedence over the uploaded certificate)."""
    cert, key = os.environ.get(ENV_CERT_FILE), os.environ.get(ENV_KEY_FILE)
    if not cert or not key:
        return None
    out = {"cert": cert, "key": key}
    chain = os.environ.get(ENV_CHAIN_FILE)
    if chain:
        out["chain"] = chain
    return out


def merge(stored: dict[str, Any], fields: dict[str, Any], secret_key: str) -> dict[str, Any]:
    """Merge validated writable fields over the stored object (secret rules for
    `secret_access_key` / `session_token`: None keep, "" clear, string replace — encrypted)."""
    out = copy.deepcopy(stored)
    for key, value in fields.items():
        if key in SECRET_INPUTS:
            if value is None:
                continue
            target, aad = SECRET_INPUTS[key]
            out[target] = None if value == "" else crypto.encrypt(secret_key, value, aad)
            continue
        if key == "prices" and isinstance(value, dict):
            out["prices"] = {**out.get("prices", {}), **value}
            continue
        if key in WRITABLE_FIELDS:
            out[key] = copy.deepcopy(value)
    return out


def merge_kb(stored: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(stored)
    for key, value in fields.items():
        if key in KB_WRITABLE_FIELDS:
            out[key] = value
    return out


# ---------------------------------------------------------------- validation (pure)
def endpoint_error(url: str | None, *, test_mode: bool) -> str | None:
    """`https://host[:port]` only (no path, query or user info); `http://` only in test mode."""
    if url is None or url == "":
        return None
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return "invalid URL"
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return "must be https://host"
    if parts.scheme == "http" and not test_mode:
        return "must use https://"
    if parts.username or parts.password:
        return "must not contain credentials"
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return "must not contain a path"
    return None


def proxy_error(url: str | None) -> str | None:
    if not url:
        return "required for proxy mode custom"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "invalid URL"
    if parts.scheme not in ("http", "https") or not parts.hostname or port is None:
        return "must be http(s)://host:port"
    if parts.username or parts.password:
        return "credentials in the proxy URL are not supported"
    if parts.path not in ("", "/"):
        return "must not contain a path"
    return None


def cidr_error(values: list[str]) -> str | None:
    if len(values) > 64:
        return "at most 64 entries"
    for v in values:
        try:
            ipaddress.ip_network(v, strict=False)
        except ValueError:
            return f"invalid CIDR {v!r}"
    return None


def field_errors(cfg: dict[str, Any], *, test_mode: bool) -> list[dict[str, Any]]:
    """Syntax errors of the stored/effective values (shape `{"loc", "msg"}`)."""
    errs: list[dict[str, Any]] = []

    def bad(field: str, msg: str) -> None:
        errs.append({"loc": ["body", field], "msg": msg})

    if cfg.get("auth_mode") not in ("roles_anywhere", "access_key", "default_chain"):
        bad("auth_mode", "invalid auth mode")
    if cfg.get("region") and not REGION_RE.match(cfg["region"]):
        bad("region", "invalid AWS region")
    if cfg.get("ra_region") and not REGION_RE.match(cfg["ra_region"]):
        bad("ra_region", "invalid AWS region")
    for f in ("model_id", "model_id_light"):
        if cfg.get(f) and not MODEL_RE.match(cfg[f]):
            bad(f, "invalid model / inference profile id")
    for f in ("endpoint_url", "control_endpoint_url", "ra_endpoint_url"):
        e = endpoint_error(cfg.get(f), test_mode=test_mode)
        if e:
            bad(f, e)
    if cfg.get("proxy_mode") not in ("env", "direct", "custom"):
        bad("proxy_mode", "invalid proxy mode")
    if cfg.get("proxy_mode") == "custom":
        e = proxy_error(cfg.get("proxy_url"))
        if e:
            bad("proxy_url", e)
    if cfg.get("access_key_id") and not ACCESS_KEY_RE.match(cfg["access_key_id"]):
        bad("access_key_id", "must be AKIA/ASIA + 16 characters")
    for f, rx in (
        ("ra_trust_anchor_arn", TRUST_ANCHOR_RE),
        ("ra_profile_arn", PROFILE_RE),
        ("ra_role_arn", ROLE_RE),
    ):
        if cfg.get(f) and not rx.match(cfg[f]):
            bad(f, "invalid ARN")
    secs = cfg.get("ra_session_seconds")
    if not isinstance(secs, int) or not 900 <= secs <= 43200:
        bad("ra_session_seconds", "must be 900-43200")
    e = cidr_error(list(cfg.get("internal_cidrs") or []))
    if e:
        bad("internal_cidrs", e)
    return errs


def required_errors(
    cfg: dict[str, Any], secret_key: str, *, has_cert: bool, has_key: bool
) -> list[dict[str, Any]]:
    """Missing values for enabling / testing the connection in the chosen auth mode."""
    errs: list[dict[str, Any]] = []

    def missing(field: str, msg: str = "required") -> None:
        errs.append({"loc": ["body", field], "msg": msg})

    if not cfg.get("region"):
        missing("region")
    if not cfg.get("model_id"):
        missing("model_id")
    mode = cfg.get("auth_mode")
    if mode == "access_key":
        if not cfg.get("access_key_id"):
            missing("access_key_id")
        try:
            secret = secret_access_key(cfg, secret_key)
        except Unreadable:
            missing("secret_access_key", "stored secret cannot be decrypted; enter it again")
        else:
            if not secret:
                missing("secret_access_key")
        try:
            session_token(cfg, secret_key)
        except Unreadable:
            missing("session_token", "stored session token cannot be decrypted; enter it again")
    elif mode == "roles_anywhere":
        for f in ("ra_trust_anchor_arn", "ra_profile_arn", "ra_role_arn"):
            if not cfg.get(f):
                missing(f)
        if not has_cert:
            missing("certificate", "upload the Roles Anywhere certificate")
        elif not has_key:
            missing("certificate", "the private key is missing or cannot be decrypted")
    return errs


def arn_account_warning(cfg: dict[str, Any]) -> bool:
    accounts = set()
    for f in ("ra_trust_anchor_arn", "ra_profile_arn", "ra_role_arn"):
        v = cfg.get(f) or ""
        parts = v.split(":")
        if len(parts) > 4 and parts[4]:
            accounts.add(parts[4])
    return len(accounts) > 1


# ---------------------------------------------------------------- connection hash (pure)
def _sha(value: str | bytes | None) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def connection_hash(
    cfg: dict[str, Any],
    *,
    secret_plain: str | None,
    cert_pem: str | None,
    chain_pem: str | None,
    key_pem: str | None,
    token_plain: str | None = None,
) -> str:
    """SHA-256 of the canonical connection settings (test_id binding, § 14.2.6). Secrets and
    certificate material enter only as SHA-256."""
    data = {f: cfg.get(f) for f in CONNECTION_FIELDS}
    data["secret_access_key"] = _sha(secret_plain)
    data["session_token"] = _sha(token_plain)
    data["ra_cert"] = _sha(cert_pem)
    data["ra_chain"] = _sha(chain_pem)
    data["ra_key"] = _sha(key_pem)
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def kb_connection_hash(kb: dict[str, Any], assistant_hash: str) -> str:
    data = {f: kb.get(f) for f in KB_CONNECTION_FIELDS}
    data["assistant"] = assistant_hash
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def kb_field_errors(kb: dict[str, Any], *, test_mode: bool) -> list[dict[str, Any]]:
    errs = []
    if kb.get("knowledge_base_id") and not KB_ID_RE.match(kb["knowledge_base_id"]):
        errs.append({"loc": ["body", "knowledge_base_id"], "msg": "must be 10 characters A-Z0-9"})
    if kb.get("region") and not REGION_RE.match(kb["region"]):
        errs.append({"loc": ["body", "region"], "msg": "invalid AWS region"})
    e = endpoint_error(kb.get("endpoint_url"), test_mode=test_mode)
    if e:
        errs.append({"loc": ["body", "endpoint_url"], "msg": e})
    return errs


# ---------------------------------------------------------------- endpoints (pure)
def runtime_endpoint(cfg: dict[str, Any]) -> str:
    url = cfg.get("endpoint_url")
    if url:
        return url.rstrip("/")
    return f"https://bedrock-runtime.{cfg['region']}.amazonaws.com"


def control_endpoint(cfg: dict[str, Any]) -> str:
    url = cfg.get("control_endpoint_url")
    if url:
        return url.rstrip("/")
    return f"https://bedrock.{cfg['region']}.amazonaws.com"


def ra_region(cfg: dict[str, Any]) -> str:
    return cfg.get("ra_region") or cfg["region"]


def ra_endpoint(cfg: dict[str, Any]) -> str:
    url = cfg.get("ra_endpoint_url")
    if url:
        return url.rstrip("/")
    return f"https://rolesanywhere.{ra_region(cfg)}.amazonaws.com"


def kb_region(kb: dict[str, Any], cfg: dict[str, Any]) -> str:
    return kb.get("region") or cfg["region"]


def kb_endpoint(kb: dict[str, Any], cfg: dict[str, Any]) -> str:
    url = kb.get("endpoint_url")
    if url:
        return url.rstrip("/")
    return f"https://bedrock-agent-runtime.{kb_region(kb, cfg)}.amazonaws.com"


def prices_set(prices: dict[str, Any] | None) -> bool:
    p = prices or {}
    return p.get("input_per_mtok") is not None and p.get("output_per_mtok") is not None
