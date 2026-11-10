"""[3b] Shared plumbing of the AI features (§ 14.8): gates, settings, the single-completion
helper over the Bedrock client, artifacts, audit and redaction.

Interfaces used from the other 3b components (architecture § 14.1):
- `rt.assistant.client` — the Bedrock client (`converse(...)`, `retrieve(...)`);
- `rt.assistant.usage` — caps and usage counters;
- `netmon.assistant.agent` — the agent loop for streamed answers with tools.
Everything here degrades: when the client is missing or fails, `complete()` raises `AiError`
with a code, and the background features skip their AI part."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.api.errors import ApiError

log = logging.getLogger("netmon.assistant.features")

AI_FEATURES = (
    "ai_explain_alert",
    "ai_correlation",
    "ai_alert_notes",
    "ai_config_diff",
    "ai_syslog_digest",
    "ai_rule_drafts",
    "ai_template_drafts",
    "ai_knowledge",
    "ai_chat_ask",
    "ai_report_summary",
)
BACKGROUND_PURPOSES = frozenset({"incident", "alert_note", "digest", "report_summary"})
# features each AI feature needs besides `assistant` (§ 14.0.1); `send_config_excerpts` and
# `config_backup_mask_secrets` are settings, checked separately
FEATURE_NEEDS: dict[str, tuple[str, ...]] = {
    "ai_config_diff": ("config_backup",),
    "ai_syslog_digest": ("syslog",),
    "ai_chat_ask": ("chat_ack",),
    "ai_report_summary": ("reports",),
}

# scalar settings of § [3b] Settings used by the features (code defaults; the stored value
# wins); read with `setting()` so the features work whatever registers the keys
DEFAULTS: dict[str, Any] = {
    "assistant_daily_questions_per_user": 50,
    "assistant_monthly_token_cap": 5_000_000,
    "assistant_monthly_cost_cap": None,
    "assistant_background_stop_pct": 80,
    "assistant_artifact_retention_days": 180,
    "assistant_audit_question_text": True,
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
ASSISTANT_DEFAULTS: dict[str, Any] = {
    "model_id": "",
    "model_id_light": None,
    "prompt_caching": True,
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
}

AI_LABEL = {"en": "AI-generated", "he": "נוצר ע״י AI"}


# ------------------------------------------------------------------ errors
class AiError(ApiError):
    """A model call that did not produce a result. `code` is an api.md error code or a skip
    reason (`cap`, `background_stop`, `timeout`, `assistant_unavailable`, …). An ApiError, so
    an interactive endpoint that lets it escape answers with the api.md envelope."""

    def __init__(
        self, code: str, message: str = "", status: int = 502, details: Any = None
    ) -> None:
        super().__init__(status, code, message or code, details)

    def api_error(self) -> ApiError:
        return ApiError(self.status, self.code, self.message, self.details)


def to_ai_error(exc: BaseException) -> AiError:
    """Map any exception of the client/usage layer to an AiError (keeps code/status when the
    exception carries them, e.g. A's AssistantError)."""
    if isinstance(exc, AiError):
        return exc
    if isinstance(exc, ApiError):
        return AiError(exc.code, exc.message, exc.status, exc.details)
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return AiError("assistant_timeout", "The answer took too long.", 504)
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        status = getattr(exc, "status", None) or getattr(exc, "http_status", None) or 502
        return AiError(
            code,
            str(getattr(exc, "message", None) or exc),
            int(status),
            list(getattr(exc, "details", None) or []) or None,
        )
    return AiError("bedrock_unavailable", "Amazon Bedrock could not answer right now.")


# ------------------------------------------------------------------ gates
def on(rt: Any, name: str) -> bool:
    features = getattr(rt, "features", None)
    return bool(features is not None and features.enabled(name))


def feature_disabled_error(name: str) -> ApiError:
    return ApiError(
        409, "feature_disabled", f"The feature {name} is turned off", [{"feature": name}]
    )


async def idle_needs(rt: Any, name: str, session: AsyncSession | None = None) -> list[str]:
    """Missing dependencies of an ON AI feature (empty = not idle)."""
    needs = [n for n in FEATURE_NEEDS.get(name, ()) if not on(rt, n)]
    if name == "ai_config_diff":
        own = session is None
        s = session if session is not None else rt.sessionmaker()
        try:
            if own:
                s = await s.__aenter__()  # type: ignore[union-attr]
            a = await assistant_settings(s)
            if not a.get("send_config_excerpts"):
                needs.append("send_config_excerpts")
            if not await setting(s, "config_backup_mask_secrets", True):
                needs.append("config_backup_mask_secrets")
        finally:
            if own:
                await s.__aexit__(None, None, None)  # type: ignore[union-attr]
    return needs


async def require(rt: Any, name: str, session: AsyncSession | None = None) -> None:
    """Endpoint gate: `assistant` OFF → 409 feature_disabled(assistant); the feature OFF →
    409 feature_disabled(name); ON with a missing dependency → 409 feature_idle."""
    if not on(rt, "assistant"):
        raise feature_disabled_error("assistant")
    if not on(rt, name):
        raise feature_disabled_error(name)
    needs = await idle_needs(rt, name, session)
    if needs:
        raise ApiError(
            409,
            "feature_idle",
            f"The feature {name} is waiting for: {', '.join(needs)}",
            [{"feature": name, "needs": needs}],
        )


async def active(rt: Any, name: str, session: AsyncSession | None = None) -> bool:
    """Background gate (no exception)."""
    if not (on(rt, "assistant") and on(rt, name)):
        return False
    try:
        return not await idle_needs(rt, name, session)
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ settings
async def _stored(session: AsyncSession, key: str) -> tuple[bool, Any]:
    row = (
        await session.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": key})
    ).first()
    if row is None:
        return False, None
    v = row[0]
    if isinstance(v, str):
        with contextlib.suppress(ValueError):
            v = json.loads(v)
    return True, v


async def setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    found, v = await _stored(session, key)
    if found and v is not None:
        return v
    if key in DEFAULTS:
        return DEFAULTS[key]
    if not found:
        try:
            from netmon.services import settings as settings_svc

            return await settings_svc.get(session, key)
        except Exception:  # noqa: BLE001
            return default
    return default


async def assistant_settings(session: AsyncSession) -> dict[str, Any]:
    found, v = await _stored(session, "assistant")
    out = dict(ASSISTANT_DEFAULTS)
    if found and isinstance(v, dict):
        out.update({k: x for k, x in v.items() if x is not None or k in ("model_id_light",)})
        prices = dict(ASSISTANT_DEFAULTS["prices"])
        prices.update(v.get("prices") or {})
        out["prices"] = prices
    return out


async def setting_bool(rt: Any, key: str) -> bool:
    async with rt.sessionmaker() as session:
        return bool(await setting(session, key))


async def int_setting(session: AsyncSession, key: str) -> int:
    v = await setting(session, key)
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(DEFAULTS[key])


async def language_default(session: AsyncSession) -> str:
    v = await setting(session, "default_language", "en")
    return "he" if v == "he" else "en"


async def tz_name(session: AsyncSession) -> str:
    try:
        from netmon.services import settings_p3c

        return await settings_p3c.timezone(session)
    except Exception:  # noqa: BLE001
        return "UTC"


async def public_base(session: AsyncSession) -> str | None:
    try:
        from netmon.services import settings_p3c

        return await settings_p3c.public_base_url(session)
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ redaction
def redact_policy(
    a: dict[str, Any], stored_map: dict[str, str] | None = None, max_len: int | None = None
) -> Any:
    from netmon.assistant import redact

    return redact.Policy.make(
        redact_ips=bool(a.get("redact_internal_ips")),
        stored_map=stored_map,
        internal_cidrs=list(a.get("internal_cidrs") or []),
        max_len=max_len,
    )


def redact_value(value: Any, policy: Any = None) -> Any:
    from netmon.assistant import redact

    return redact.apply(value, policy)


def redact_text(s: str, policy: Any = None, max_len: int | None = None) -> str:
    from netmon.assistant import redact

    out = redact.apply_text(s or "", policy)
    return redact.cap(out, max_len) if max_len else out


def detokenize(value: Any, policy: Any) -> Any:
    from netmon.assistant import redact

    return redact.detokenize(value, getattr(policy, "ipmap", None))


# ------------------------------------------------------------------ usage and caps
def local_day(rt: Any, tz: str) -> Any:
    from zoneinfo import ZoneInfo

    try:
        z = ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        z = ZoneInfo("UTC")
    return rt.clock.now().astimezone(z).date()


async def purpose_count_today(
    rt: Any, session: AsyncSession, kind: str, prefix: str | None = None, status: str | None = "ok"
) -> int:
    """Artifacts of `kind` created today (local) — the per-purpose daily caps."""
    tz = await tz_name(session)
    sql = (
        "SELECT count(*) FROM assistant_artifacts WHERE kind = :k "
        "AND (created_at AT TIME ZONE :tz)::date = :d"
    )
    params: dict[str, Any] = {"k": kind, "tz": tz, "d": local_day(rt, tz)}
    if status:
        sql += " AND status = :s"
        params["s"] = status
    if prefix:
        sql += " AND key LIKE :p"
        params["p"] = prefix + "%"
    return int((await session.execute(text(sql), params)).scalar() or 0)


def _usage_obj(rt: Any) -> Any:
    return getattr(getattr(rt, "assistant", None), "usage", None)


async def check_caps(
    rt: Any, purpose: str, user_id: int = 0, *, question: bool = False, reserve: bool = False
) -> bool:
    """Caps before a model call. With developer A's `rt.assistant.usage`: `question` and
    `reserve` → `check_and_reserve(user_id, purpose)` (checks every cap and counts the
    question; returns True = counted), else `check(user_id, purpose, question=question)`.
    Without it (unit setups): the same arithmetic in SQL. Background purposes stop at
    `assistant_background_stop_pct` (AiError `background_stop`); interactive purposes raise
    AiError `assistant_cap_reached` (429)."""
    usage = _usage_obj(rt)
    if usage is not None:
        try:
            if question and reserve:
                await usage.check_and_reserve(user_id, purpose, count_question=True)
                return True
            await usage.check(user_id, purpose, question=question)
            return False
        except TypeError:
            raise  # a wrong call is a bug: fail loudly
        except Exception as exc:  # noqa: BLE001
            err = to_ai_error(exc)
            if purpose in BACKGROUND_PURPOSES and err.code == "assistant_cap_reached":
                raise AiError("background_stop", err.message, 429, err.details) from None
            raise err from None
    async with rt.sessionmaker() as session:
        await _check_caps_sql(rt, session, purpose, user_id, question)
    return False


async def _check_caps_sql(
    rt: Any, session: AsyncSession, purpose: str, user_id: int, question: bool
) -> None:
    tz = await tz_name(session)
    today = local_day(rt, tz)
    month_start = today.replace(day=1)
    row = (
        await session.execute(
            text(
                "SELECT coalesce(sum(input_tokens + output_tokens + cache_write_tokens "
                "+ ceil(cache_read_tokens / 10.0)), 0) AS tokens, "
                "coalesce(sum(cost_estimate), 0) AS cost FROM assistant_usage WHERE day >= :m"
            ),
            {"m": month_start},
        )
    ).one()
    token_cap = await int_setting(session, "assistant_monthly_token_cap")
    cost_cap = await setting(session, "assistant_monthly_cost_cap")
    a = await assistant_settings(session)
    prices = a.get("prices") or {}
    has_prices = (
        prices.get("input_per_mtok") is not None and prices.get("output_per_mtok") is not None
    )
    pct = float(row.tokens) / token_cap * 100 if token_cap else 0.0
    which = "monthly_tokens"
    if cost_cap is not None and has_prices and float(cost_cap) > 0:
        cpct = float(row.cost) / float(cost_cap) * 100
        if cpct > pct:
            pct, which = cpct, "monthly_cost"
    if purpose in BACKGROUND_PURPOSES:
        stop = await int_setting(session, "assistant_background_stop_pct")
        if pct >= stop:
            raise AiError("background_stop", "Background AI stopped near the monthly cap", 429)
        return
    if pct >= 100:
        raise AiError(
            "assistant_cap_reached",
            "The monthly assistant cap is reached",
            429,
            [
                {
                    "cap": which,
                    "limit": token_cap if which == "monthly_tokens" else cost_cap,
                    "used": int(row.tokens) if which == "monthly_tokens" else float(row.cost),
                    "resets_at": None,
                }
            ],
        )
    if question and user_id:
        used = (
            await session.execute(
                text(
                    "SELECT coalesce(sum(questions), 0) FROM assistant_usage "
                    "WHERE day = :d AND user_id = :u"
                ),
                {"d": today, "u": user_id},
            )
        ).scalar()
        limit = await int_setting(session, "assistant_daily_questions_per_user")
        if int(used or 0) >= limit:
            raise AiError(
                "assistant_cap_reached",
                "Your daily question limit is reached",
                429,
                [{"cap": "user_daily", "limit": limit, "used": int(used or 0), "resets_at": None}],
            )


def estimate_cost(usage: dict[str, int], prices: dict[str, Any]) -> float | None:
    try:
        from netmon.assistant import pricing  # A's pure module

        return pricing.as_float(pricing.estimate(usage, prices))
    except Exception:  # noqa: BLE001
        log.debug("pricing.estimate failed", exc_info=True)
    inp, out = prices.get("input_per_mtok"), prices.get("output_per_mtok")
    if inp is None or out is None:
        return None
    cr = prices.get("cache_read_per_mtok")
    cw = prices.get("cache_write_per_mtok")
    cr = float(inp) * 0.1 if cr is None else float(cr)
    cw = float(inp) * 1.25 if cw is None else float(cw)
    total = (
        usage.get("input_tokens", 0) * float(inp)
        + usage.get("output_tokens", 0) * float(out)
        + usage.get("cache_read_tokens", 0) * cr
        + usage.get("cache_write_tokens", 0) * cw
    ) / 1_000_000
    return round(total, 6)


async def record_usage(
    rt: Any,
    purpose: str,
    user_id: int,
    usage: dict[str, int],
    *,
    questions: int = 0,
    model_calls: int = 1,
    tool_calls: int = 0,
    errors: int = 0,
    throttles: int = 0,
    cost: float | None = None,
) -> None:
    """Usage counters (`assistant_usage` upsert). Delegates to `rt.assistant.usage.record(...)`
    when present. Never raises."""
    u = _usage_obj(rt)
    if u is not None:
        try:
            await u.record(
                user_id,
                purpose,
                usage,
                questions=questions,
                model_calls=model_calls,
                tool_calls=tool_calls,
                errors=errors,
                throttles=throttles,
            )
        except TypeError:
            raise  # a wrong call is a bug: fail loudly
        except Exception:  # noqa: BLE001 - counting never breaks a feature
            log.warning("Recording assistant usage failed", exc_info=True)
        return
    try:
        async with rt.sessionmaker() as session:
            tz = await tz_name(session)
            await session.execute(
                text(
                    "INSERT INTO assistant_usage (day, user_id, purpose, questions, model_calls, "
                    "tool_calls, errors, throttles, input_tokens, output_tokens, "
                    "cache_read_tokens, cache_write_tokens, cost_estimate) VALUES (:d, :u, :p, "
                    ":q, :mc, :tc, :e, :t, :i, :o, :cr, :cw, :c) ON CONFLICT (day, user_id, "
                    "purpose) DO UPDATE SET questions = assistant_usage.questions + EXCLUDED."
                    "questions, model_calls = assistant_usage.model_calls + EXCLUDED.model_calls, "
                    "tool_calls = assistant_usage.tool_calls + EXCLUDED.tool_calls, errors = "
                    "assistant_usage.errors + EXCLUDED.errors, throttles = assistant_usage."
                    "throttles + EXCLUDED.throttles, input_tokens = assistant_usage.input_tokens "
                    "+ EXCLUDED.input_tokens, output_tokens = assistant_usage.output_tokens + "
                    "EXCLUDED.output_tokens, cache_read_tokens = assistant_usage."
                    "cache_read_tokens + EXCLUDED.cache_read_tokens, cache_write_tokens = "
                    "assistant_usage.cache_write_tokens + EXCLUDED.cache_write_tokens, "
                    "cost_estimate = assistant_usage.cost_estimate + EXCLUDED.cost_estimate"
                ),
                {
                    "d": local_day(rt, tz),
                    "u": int(user_id),
                    "p": purpose,
                    "q": questions,
                    "mc": model_calls,
                    "tc": tool_calls,
                    "e": errors,
                    "t": throttles,
                    "i": usage.get("input_tokens", 0),
                    "o": usage.get("output_tokens", 0),
                    "cr": usage.get("cache_read_tokens", 0),
                    "cw": usage.get("cache_write_tokens", 0),
                    "c": cost or 0,
                },
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        log.debug("Recording assistant usage failed", exc_info=True)


# ------------------------------------------------------------------ completion
@dataclass
class Completion:
    text: str = ""
    tool_name: str | None = None
    tool_input: Any = None
    stop_reason: str | None = None
    model_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    cost: float | None = None
    message: dict[str, Any] | None = None  # the assistant message as returned (for follow-ups)
    tool_uses: list[dict[str, Any]] = field(default_factory=list)


def parse_response(resp: dict[str, Any]) -> Completion:
    """Bedrock Converse response JSON → Completion."""
    msg = ((resp or {}).get("output") or {}).get("message") or {}
    texts: list[str] = []
    tool_uses: list[dict[str, Any]] = []
    for block in msg.get("content") or []:
        if "text" in block:
            texts.append(str(block["text"]))
        elif "toolUse" in block:
            tool_uses.append(block["toolUse"])
    u = (resp or {}).get("usage") or {}
    usage = {
        "input_tokens": int(u.get("inputTokens") or 0),
        "output_tokens": int(u.get("outputTokens") or 0),
        "cache_read_tokens": int(u.get("cacheReadInputTokens") or 0),
        "cache_write_tokens": int(u.get("cacheWriteInputTokens") or 0),
    }
    first = tool_uses[0] if tool_uses else {}
    return Completion(
        text="".join(texts).strip(),
        tool_name=first.get("name"),
        tool_input=first.get("input"),
        stop_reason=(resp or {}).get("stopReason"),
        usage=usage,
        message=msg or None,
        tool_uses=tool_uses,
    )


def client_of(rt: Any) -> Any:
    asst = getattr(rt, "assistant", None)
    if asst is None:
        return None
    try:
        return asst.client
    except Exception:  # noqa: BLE001 - AssistantError while OFF / unconfigured
        return None


async def _converse(
    rt: Any, model_id: str, body: dict[str, Any], timeout_s: float
) -> dict[str, Any]:
    client = client_of(rt)
    if client is None or not hasattr(client, "converse"):
        raise AiError("assistant_unavailable", "The assistant connection is not ready", 503)
    resp = await asyncio.wait_for(
        client.converse(body, model_id=model_id, timeout=timeout_s), timeout_s + 1
    )
    if hasattr(resp, "raw"):
        resp = resp.raw
    if not isinstance(resp, dict):
        raise AiError("bedrock_unavailable", "Unexpected response from Amazon Bedrock")
    return resp


async def complete(
    rt: Any,
    *,
    purpose: str,
    system: str,
    messages: list[dict[str, Any]],
    user_id: int = 0,
    light: bool = False,
    max_tokens: int = 1000,
    timeout_s: float = 60.0,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    question: bool = False,
    check: bool = True,
    temperature: float = 0.2,
) -> Completion:
    """One non-streamed Converse call with caps, usage and cost. `tools` are Converse
    `toolSpec` bodies (without the wrapper); `tool_choice` a tool name (forced) or "auto".
    Raises AiError."""
    if not on(rt, "assistant"):
        raise AiError("feature_disabled", "The assistant is turned off", 409)
    if check:
        counted = await check_caps(rt, purpose, user_id, question=question, reserve=question)
    else:
        counted = False
    async with rt.sessionmaker() as session:
        a = await assistant_settings(session)
    model_id = (a.get("model_id_light") if light else None) or a.get("model_id")
    if not model_id:
        raise AiError("assistant_unavailable", "No model is configured", 503)
    from netmon.assistant import prompt as prompt_mod

    specs = [{"toolSpec": t} for t in (tools or [])]
    body = prompt_mod.converse_request(
        system_text=system,
        messages=messages,
        specs=specs,
        max_tokens=max_tokens,
        caching=bool(a.get("prompt_caching", True)),
        choice=tool_choice or ("auto" if specs else None),
        temperature=temperature,
    )
    t0 = time.monotonic()
    try:
        resp = await _converse(rt, model_id, body, timeout_s)
    except Exception as exc:  # noqa: BLE001
        err = to_ai_error(exc)
        await record_usage(
            rt,
            purpose,
            user_id,
            {},
            questions=1 if question and not counted else 0,
            model_calls=1,
            errors=1,
            throttles=1 if err.code == "bedrock_throttled" else 0,
        )
        raise err from None
    comp = parse_response(resp)
    comp.model_id = model_id
    comp.cost = estimate_cost(comp.usage, a.get("prices") or {})
    await record_usage(
        rt,
        purpose,
        user_id,
        comp.usage,
        questions=1 if question and not counted else 0,
        cost=comp.cost,
    )
    log.debug("AI %s completion in %.0f ms", purpose, (time.monotonic() - t0) * 1000)
    return comp


async def system_prompt(
    rt: Any,
    purpose: str,
    block: str,
    *,
    redaction: bool = False,
    session: AsyncSession | None = None,
) -> str:
    from netmon.assistant import prompt as prompt_mod

    tz = "UTC"
    if session is not None:
        tz = await tz_name(session)
    ctx = prompt_mod.PromptCtx(
        now=rt.clock.now(), timezone=tz, redaction=redaction, purpose_block=block
    )
    return prompt_mod.system_prompt(purpose, ctx)


def user_message(text_: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"text": text_}]}


def json_message(prefix: str, data: Any) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [{"text": prefix + "\n" + json.dumps(data, ensure_ascii=False, default=str)}],
    }


# ------------------------------------------------------------------ artifacts
ARTIFACT_COLS = (
    "id, kind, key, status, skip_reason, content, data, conversation_id, created_by, model_id, "
    "input_tokens, output_tokens, cost_estimate, created_at"
)


async def get_artifact(session: AsyncSession, kind: str, key: str) -> Any:
    return (
        await session.execute(
            text(f"SELECT {ARTIFACT_COLS} FROM assistant_artifacts WHERE kind = :k AND key = :key"),
            {"k": kind, "key": key},
        )
    ).first()


async def put_artifact(
    session: AsyncSession,
    kind: str,
    key: str,
    status: str,
    *,
    content: str = "",
    data: Any = None,
    skip_reason: str | None = None,
    conversation_id: int | None = None,
    created_by: int | None = None,
    comp: Completion | None = None,
    created_at: datetime | None = None,
) -> Any:
    """Insert or replace the `(kind, key)` artifact (the caller commits)."""
    return (
        await session.execute(
            text(
                "INSERT INTO assistant_artifacts (kind, key, status, skip_reason, content, data, "
                "conversation_id, created_by, model_id, input_tokens, output_tokens, "
                "cost_estimate, created_at) VALUES (:kind, :key, :status, :skip, :content, "
                "CAST(:data AS jsonb), :conv, :by, :model, :i, :o, :cost, coalesce(:at, now())) "
                "ON CONFLICT (kind, key) DO UPDATE SET status = EXCLUDED.status, skip_reason = "
                "EXCLUDED.skip_reason, content = EXCLUDED.content, data = EXCLUDED.data, "
                "conversation_id = EXCLUDED.conversation_id, created_by = EXCLUDED.created_by, "
                "model_id = EXCLUDED.model_id, input_tokens = EXCLUDED.input_tokens, "
                "output_tokens = EXCLUDED.output_tokens, cost_estimate = EXCLUDED.cost_estimate, "
                f"created_at = EXCLUDED.created_at RETURNING {ARTIFACT_COLS}"
            ),
            {
                "kind": kind,
                "key": key[:200],
                "status": status,
                "skip": skip_reason[:64] if skip_reason else None,
                "content": content or "",
                "data": json.dumps(
                    data if data is not None else {}, ensure_ascii=False, default=str
                ),
                "conv": conversation_id,
                "by": created_by,
                "model": comp.model_id if comp else None,
                "i": comp.usage.get("input_tokens", 0) if comp else 0,
                "o": comp.usage.get("output_tokens", 0) if comp else 0,
                "cost": comp.cost if comp else None,
                "at": created_at,
            },
        )
    ).one()


def usage_out(comp: Completion | None) -> dict[str, int]:
    if comp is None:
        return {"input_tokens": 0, "output_tokens": 0}
    return {
        "input_tokens": int(comp.usage.get("input_tokens", 0)),
        "output_tokens": int(comp.usage.get("output_tokens", 0)),
    }


def jsonb(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


# ------------------------------------------------------------------ audit
async def audit_system(
    rt: Any,
    action: str,
    entity_type: str | None,
    entity_id: int | None,
    changes: dict[str, Any],
    *,
    name: str | None = None,
    device_id: int | None = None,
) -> None:
    """System audit row (metadata only: ids, model, usage, status, skip reason)."""
    from netmon.services import audit

    ctx = audit.AuditCtx.system(rt)
    await audit.record_now(
        rt,
        ctx,
        action,
        audit.entity(entity_type, entity_id, name, device_id=device_id),
        {k: [None, v] for k, v in changes.items()},
    )


async def audit_user(
    rt: Any,
    ctx: Any,
    action: str,
    entity_type: str | None,
    entity_id: int | None,
    changes: dict[str, Any],
    *,
    name: str | None = None,
    device_id: int | None = None,
    source: str | None = None,
) -> None:
    from netmon.services import audit

    kw: dict[str, Any] = {}
    if source:
        kw["source"] = source
    await audit.record_now(
        rt,
        ctx,
        action,
        audit.entity(entity_type, entity_id, name, device_id=device_id),
        {k: [None, v] for k, v in changes.items()},
        **kw,
    )


def usage_changes(comp: Completion | None) -> dict[str, Any]:
    if comp is None:
        return {}
    return {
        "model_id": comp.model_id,
        "input_tokens": comp.usage.get("input_tokens", 0),
        "output_tokens": comp.usage.get("output_tokens", 0),
        "cost_estimate": comp.cost,
    }


def spawn(rt: Any, coro: Any, name: str | None = None) -> asyncio.Task[Any]:
    """Background task that never raises into the loop's exception handler."""
    fn = getattr(rt, "spawn", None)
    if fn is not None:
        return fn(coro)  # type: ignore[no-any-return]
    return asyncio.create_task(coro, name=name)
