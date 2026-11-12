"""[3b] Request models of the assistant connection layer (api.md "[3b] Assistant settings,
test, certificate", "[3b] Type changes") and the 3b keys of `PUT /api/settings`."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from netmon.assistant.config import (
    ACCESS_KEY_RE,
    CHAT_MODEL_LABEL_MAX,
    CHAT_MODELS_MAX,
    KB_ID_RE,
    MODEL_RE,
    PROFILE_RE,
    REGION_RE,
    ROLE_RE,
    SESSION_TOKEN_MAX,
    TRUST_ANCHOR_RE,
    cidr_error,
)

# scalar keys written as they come; `assistant_monthly_cost_cap` and `digest_language` may be
# set to null explicitly
SCALAR_KEYS = (
    "assistant_daily_questions_per_user",
    "assistant_monthly_token_cap",
    "assistant_monthly_cost_cap",
    "assistant_background_stop_pct",
    "assistant_max_model_calls",
    "assistant_max_tool_calls",
    "assistant_max_output_tokens",
    "assistant_question_timeout_s",
    "assistant_max_concurrent",
    "assistant_context_budget_tokens",
    "assistant_retention_days",
    "assistant_artifact_retention_days",
    "correlation_window_min",
    "correlation_min_alerts",
    "correlation_attach_to_alert",
    "correlation_daily_summaries",
    "alert_note_timeout_s",
    "alert_notes_daily_cap",
    "config_diff_auto",
    "config_diff_daily_cap",
    "digest_time",
    "digest_language",
    "digest_email_to",
    "digest_channel_ids",
    "chat_ask_per_user_hour",
)
NULLABLE_KEYS = frozenset({"assistant_monthly_cost_cap", "digest_language"})
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TOKEN = re.compile(r"^[A-Za-z0-9+/=_-]+$")


class Phase3bSettingsMixin(BaseModel):
    """[3b] feature toggles and scalar keys of `PUT /api/settings`. `assistant` and
    `ai_knowledge` cannot be turned ON here (`422 assistant_test_required`)."""

    feature_assistant_enabled: bool | None = None
    feature_ai_explain_alert_enabled: bool | None = None
    feature_ai_correlation_enabled: bool | None = None
    feature_ai_alert_notes_enabled: bool | None = None
    feature_ai_config_diff_enabled: bool | None = None
    feature_ai_syslog_digest_enabled: bool | None = None
    feature_ai_rule_drafts_enabled: bool | None = None
    feature_ai_template_drafts_enabled: bool | None = None
    feature_ai_knowledge_enabled: bool | None = None
    feature_ai_chat_ask_enabled: bool | None = None
    feature_ai_report_summary_enabled: bool | None = None

    assistant_daily_questions_per_user: int | None = Field(default=None, ge=1, le=1000)
    assistant_monthly_token_cap: int | None = Field(default=None, ge=100_000, le=1_000_000_000)
    assistant_monthly_cost_cap: float | None = Field(default=None, ge=0, le=1_000_000)
    assistant_background_stop_pct: int | None = Field(default=None, ge=10, le=100)
    assistant_max_model_calls: int | None = Field(default=None, ge=2, le=15)
    assistant_max_tool_calls: int | None = Field(default=None, ge=1, le=30)
    assistant_max_output_tokens: int | None = Field(default=None, ge=256, le=8192)
    assistant_question_timeout_s: int | None = Field(default=None, ge=30, le=300)
    assistant_max_concurrent: int | None = Field(default=None, ge=1, le=16)
    assistant_context_budget_tokens: int | None = Field(default=None, ge=8000, le=150_000)
    assistant_retention_days: int | None = Field(default=None, ge=1, le=730)
    assistant_artifact_retention_days: int | None = Field(default=None, ge=7, le=730)
    correlation_window_min: int | None = Field(default=None, ge=1, le=30)
    correlation_min_alerts: int | None = Field(default=None, ge=2, le=50)
    correlation_attach_to_alert: bool | None = None
    correlation_daily_summaries: int | None = Field(default=None, ge=0, le=500)
    alert_note_timeout_s: int | None = Field(default=None, ge=10, le=120)
    alert_notes_daily_cap: int | None = Field(default=None, ge=0, le=5000)
    config_diff_auto: bool | None = None
    config_diff_daily_cap: int | None = Field(default=None, ge=0, le=500)
    digest_time: str | None = None
    digest_language: Literal["en", "he"] | None = None
    digest_email_to: list[str] | None = Field(default=None, max_length=20)
    digest_channel_ids: list[int] | None = Field(default=None, max_length=10)
    chat_ask_per_user_hour: int | None = Field(default=None, ge=1, le=60)

    @field_validator("digest_time")
    @classmethod
    def _digest_time(cls, v: str | None) -> str | None:
        if v is not None and not _HHMM.match(v):
            raise ValueError("must be HH:MM")
        return v

    @field_validator("digest_email_to")
    @classmethod
    def _emails(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out = []
        for e in v:
            e = e.strip()
            if not _EMAIL.match(e) or len(e) > 254:
                raise ValueError(f"invalid e-mail address {e!r}")
            if e not in out:
                out.append(e)
        return out

    @field_validator("digest_channel_ids")
    @classmethod
    def _ids(cls, v: list[int] | None) -> list[int] | None:
        return None if v is None else list(dict.fromkeys(v))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PricesIn(_Strict):
    currency: str | None = Field(default=None, min_length=1, max_length=8)
    input_per_mtok: float | None = Field(default=None, ge=0, le=1000)
    output_per_mtok: float | None = Field(default=None, ge=0, le=1000)
    cache_read_per_mtok: float | None = Field(default=None, ge=0, le=1000)
    cache_write_per_mtok: float | None = Field(default=None, ge=0, le=1000)


class ChatModelIn(_Strict):
    """[3b owner] One entry of `chat_models` (a model the chat composer offers)."""

    id: str = Field(min_length=1, max_length=256)
    label: str | None = Field(default=None, max_length=CHAT_MODEL_LABEL_MAX)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        v = v.strip()
        if not MODEL_RE.match(v):
            raise ValueError("invalid model or inference profile id")
        return v

    @field_validator("label")
    @classmethod
    def _label(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = " ".join(v.split())
        return v or None


def _opt(rx: re.Pattern[str], what: str):  # type: ignore[no-untyped-def]
    def check(cls, v: str | None) -> str | None:  # type: ignore[no-untyped-def]
        if v is None:
            return v
        v = v.strip()
        if v == "":
            return None
        if not rx.match(v):
            raise ValueError(f"invalid {what}")
        return v

    return classmethod(check)


class AssistantFields(_Strict):
    """Writable fields of `AssistantSettings` (all optional: partial update / test overlay)."""

    auth_mode: Literal["roles_anywhere", "access_key", "default_chain"] | None = None
    region: str | None = None
    model_id: str | None = Field(default=None, max_length=256)
    model_id_light: str | None = Field(default=None, max_length=256)
    endpoint_url: str | None = Field(default=None, max_length=512)
    control_endpoint_url: str | None = Field(default=None, max_length=512)
    proxy_mode: Literal["env", "direct", "custom"] | None = None
    proxy_url: str | None = Field(default=None, max_length=512)
    prompt_caching: bool | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = Field(default=None, max_length=256)
    # write-only; temporary credentials (ASIA… key ids); "" clears
    session_token: str | None = Field(default=None, max_length=SESSION_TOKEN_MAX)
    access_key_created_at: date | None = None
    ra_region: str | None = None
    ra_endpoint_url: str | None = Field(default=None, max_length=512)
    ra_trust_anchor_arn: str | None = Field(default=None, max_length=512)
    ra_profile_arn: str | None = Field(default=None, max_length=512)
    ra_role_arn: str | None = Field(default=None, max_length=600)
    ra_session_seconds: int | None = Field(default=None, ge=900, le=43200)
    send_config_excerpts: bool | None = None
    redact_internal_ips: bool | None = None
    internal_cidrs: list[str] | None = Field(default=None, max_length=64)
    chat_redact_ips: bool | None = None
    prices: PricesIn | None = None
    chat_models: list[ChatModelIn] | None = Field(default=None, max_length=CHAT_MODELS_MAX)

    _region = field_validator("region", "ra_region")(_opt(REGION_RE, "AWS region"))
    _model = field_validator("model_id", "model_id_light")(
        _opt(MODEL_RE, "model or inference profile id")
    )
    _akid = field_validator("access_key_id")(_opt(ACCESS_KEY_RE, "access key id"))
    _ta = field_validator("ra_trust_anchor_arn")(_opt(TRUST_ANCHOR_RE, "trust anchor ARN"))
    _pa = field_validator("ra_profile_arn")(_opt(PROFILE_RE, "profile ARN"))
    _ra = field_validator("ra_role_arn")(_opt(ROLE_RE, "role ARN"))

    @field_validator("endpoint_url", "control_endpoint_url", "ra_endpoint_url", "proxy_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        return v.rstrip("/") or None

    @field_validator("session_token")
    @classmethod
    def _token(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = "".join(v.split())  # pasted tokens may carry line breaks / spaces
        if v and not _TOKEN.match(v):
            raise ValueError("invalid session token (base64 characters expected)")
        return v

    @field_validator("chat_models")
    @classmethod
    def _chat_models(cls, v: list[ChatModelIn] | None) -> list[ChatModelIn] | None:
        if v is None:
            return v
        ids = [m.id for m in v]
        dup = next((i for i in ids if ids.count(i) > 1), None)
        if dup is not None:
            raise ValueError(f"duplicate model {dup!r}")
        return v

    @field_validator("internal_cidrs")
    @classmethod
    def _cidrs(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        v = [c.strip() for c in v if c.strip()]
        err = cidr_error(v)
        if err:
            raise ValueError(err)
        return list(dict.fromkeys(v))

    def changes(self) -> dict:
        """The fields that were sent (prices as a partial dict)."""
        out = {}
        for k in self.model_fields_set:
            v = getattr(self, k)
            if k == "prices":
                out[k] = v.model_dump(exclude_unset=True) if v is not None else None
                if out[k] is None:
                    out.pop(k)
            elif k == "chat_models":
                out[k] = [m.model_dump() for m in v] if v else []  # null clears
            elif k == "access_key_created_at":
                out[k] = v.isoformat() if v is not None else None
            elif k in ("region", "model_id", "auth_mode", "proxy_mode") and v is None:
                continue  # required values cannot be cleared
            else:
                out[k] = v
        return out


class AssistantSettingsIn(AssistantFields):
    enabled: bool | None = None
    test_id: str | None = Field(default=None, max_length=64)


class AssistantTestIn(_Strict):
    settings: AssistantFields | None = None


class CsrIn(_Strict):
    common_name: str = Field(min_length=1, max_length=64)
    key_type: Literal["rsa", "ec"] = "rsa"
    key_size: int | None = None  # rsa 2048 (default) / 3072 / 4096; ec 256 (default) / 384
    organization: str | None = Field(default=None, max_length=64)
    organizational_unit: str | None = Field(default=None, max_length=64)


class KnowledgeFields(_Strict):
    knowledge_base_id: str | None = None
    region: str | None = None
    endpoint_url: str | None = Field(default=None, max_length=512)
    results: int | None = Field(default=None, ge=1, le=10)
    min_score: float | None = Field(default=None, ge=0, le=1)
    description: str | None = Field(default=None, max_length=300)

    _kb = field_validator("knowledge_base_id")(_opt(KB_ID_RE, "Knowledge Base id"))
    _region = field_validator("region")(_opt(REGION_RE, "AWS region"))

    @field_validator("endpoint_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return v.strip().rstrip("/") or None


class KnowledgeSettingsIn(KnowledgeFields):
    enabled: bool | None = None
    test_id: str | None = Field(default=None, max_length=64)


class KnowledgeTestIn(_Strict):
    settings: KnowledgeFields | None = None
