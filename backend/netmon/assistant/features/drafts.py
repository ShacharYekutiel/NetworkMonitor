"""[3b] Alert-rule / event-rule drafts from a description (`ai_rule_drafts`, § 14.8.6).

The model may call the read tools `search_devices`, `search_sensors`, `list_groups`,
`list_notification_channels` (≤ 6 calls) and must finish with the forced tool
`propose_alert_rule` / `propose_event_rule` whose input schema is the writable fields of the
existing create models (generated from pydantic, so a draft cannot use fields the API does not
accept). The draft is validated like the normal API would (pydantic + reference checks + the
2b-3 event-rule validator) **without saving**; one automatic repair round. Nothing is written
except the conversation (kind `rule_draft`) and the artifact."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text

from netmon.api.errors import ApiError
from netmon.assistant.features import common
from netmon.assistant.features.templates_pure import PLACEHOLDER_RE

log = logging.getLogger("netmon.assistant.drafts")

READ_TOOLS = ("search_devices", "search_sensors", "list_groups", "list_notification_channels")
MAX_TOOL_CALLS = 6
DEADLINE_S = 45.0
MAX_ROUNDS = 8

BLOCK = (
    "Task: draft a NetworkMonitor {what} from the administrator's description. Look up ids with "
    "the read tools when the description names devices, sensors, groups or channels (never "
    "invent ids). Finish by calling {tool} exactly once: its input is the draft (only the "
    "fields of the schema), plus `explanation` (what the rule does, 1–3 sentences) and "
    "`assumptions` (anything you had to guess). The administrator reviews the draft in the "
    "normal form before saving; nothing is saved by you. "
    "If the description does not describe {subject}, or it cannot be expressed with the fields "
    "of the schema, do not invent a draft and never use placeholders such as <UNKNOWN>: call "
    "{tool} with `cannot_draft: true` and `reason` (1–2 sentences: what is missing and what a "
    "usable description would say); the other fields are then ignored (give required ones "
    "minimal values). "
    "Language: write `name`, `explanation`, `assumptions` and `reason` in the language the "
    "message names; technical values (ids, OIDs, regexes, program names, kinds, enum values, "
    "field names) stay exactly as they are."
)
SUBJECT = {
    "alert_rule": "a sensor status condition to alert on",
    "event_rule": "a trap or syslog event to match",
}
EVENT_HELP = (
    "Event rules: kind `trap` or `syslog`; `match` for syslog = {severity_max: integer 0-7 or "
    "null, facilities: [integers 0-23], programs: [program names], text: string matched "
    "against the message (substring, case-insensitive; a regular expression when text_regex "
    "is true), text_regex: boolean}; the pattern always goes in `text`, never in text_regex. "
    "For traps = {trap_oids [], varbinds [{oid, op (eq|contains|regex), value}], ports "
    "(any|important)}; action warning | down | ignore; scope global | group (group_id) | device "
    "(device_id); optional clear_match and clear_after_min. Event rules have no count or "
    "time-window threshold (every matching message counts): say so in `assumptions` when the "
    "description asks for one. "
    "Vendor syslog reference (data): H3C/HPE Comware `%%10SSHS/5/SSHS_AUTH_FAIL: <message>` "
    "→ program `SSHS` (the module; the mnemonic before ':' is not part of the matched text), "
    "login module `LOGIN`; Linux/OpenSSH → program `sshd`, message `Failed password for ...`."
)
CANNOT_REASON = {
    "en": (
        "The description does not say what to watch for (a status, event or message to "
        "match), so no draft was made."
    ),
    "he": "התיאור לא מציין מה לנטר (מצב, אירוע או הודעה להתאמה), ולכן לא נוצרה טיוטה.",
}
_PLACEHOLDER_WORDS = {"", "unknown", "placeholder", "todo", "tbd", "n/a", "?", "none", "null"}


def _strip_titles(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    defs = schema.pop("$defs", {})

    def walk(n: Any) -> Any:
        if isinstance(n, dict):
            if "$ref" in n and isinstance(n["$ref"], str):
                name = n["$ref"].rsplit("/", 1)[-1]
                return walk(copy.deepcopy(defs.get(name, {})))
            return {k: walk(v) for k, v in n.items()}
        if isinstance(n, list):
            return [walk(v) for v in n]
        return n

    return walk(schema)  # type: ignore[no-any-return]


def models(kind: str) -> tuple[Any, str]:
    if kind == "alert_rule":
        from netmon.schemas import AlertRuleCreate

        return AlertRuleCreate, "propose_alert_rule"
    from netmon.schemas.ingest import EventRuleIn

    return EventRuleIn, "propose_event_rule"


def propose_tool(kind: str) -> dict[str, Any]:
    """The forced final tool: the writable fields of the create model + explanation +
    assumptions (pure)."""
    model, name = models(kind)
    schema = _strip_titles(_inline_refs(model.model_json_schema()))
    props = dict(schema.get("properties") or {})
    props["explanation"] = {"type": "string", "maxLength": 1000}
    props["assumptions"] = {"type": "array", "items": {"type": "string"}, "maxItems": 10}
    props["cannot_draft"] = {
        "type": "boolean",
        "description": "true when the description cannot be turned into this kind of rule",
    }
    props["reason"] = {
        "type": "string",
        "maxLength": 500,
        "description": "why no draft is possible (only with cannot_draft)",
    }
    if kind == "event_rule":
        for k in ("match", "clear_match"):
            if k in props:
                props[k] = {**props[k], "description": MATCH_DESCRIPTION}
    required = list(schema.get("required") or []) + ["explanation"]
    return {
        "name": name,
        "description": (
            f"Return the {kind.replace('_', ' ')} draft (not executed: its input is the draft)."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "additionalProperties": False,
                "properties": props,
                "required": required,
            }
        },
    }


MATCH_DESCRIPTION = (
    "syslog: {severity_max: 0-7|null, facilities: [0-23], programs: [str], text: str|null "
    "(the pattern), text_regex: bool}; trap: {trap_oids: [oid], varbinds: [{oid, op, value}], "
    "ports: any|important}"
)
_TEXT_ALIASES = ("pattern", "regex", "message", "text_pattern", "message_regex", "msg")


def normalize_match(m: Any) -> Any:
    """Repair the usual model slips in an event-rule match (pure): the pattern put in
    `text_regex` (a string) or under an alias key instead of `text`, a single `program`,
    a non-boolean `text_regex`. Everything else is left to the validator."""
    if not isinstance(m, dict):
        return m
    m = dict(m)
    tr = m.get("text_regex")
    if isinstance(tr, str):
        bool_word = tr.strip().lower() in ("true", "false", "1", "0", "yes", "no")
        if tr.strip() and not bool_word and not m.get("text"):
            m["text"] = tr
            m["text_regex"] = True
        else:
            m["text_regex"] = tr.strip().lower() in ("true", "1", "yes")
    if not m.get("text"):
        for alias in _TEXT_ALIASES:
            v = m.get(alias)
            if isinstance(v, str) and v.strip():
                m["text"] = v
                if alias in ("pattern", "regex", "text_pattern", "message_regex"):
                    m.setdefault("text_regex", True)
                break
    for alias in _TEXT_ALIASES:
        m.pop(alias, None)
    prog = m.pop("program", None)
    if isinstance(prog, str) and prog.strip() and not m.get("programs"):
        m["programs"] = [prog.strip()]
    if "text_regex" in m and not isinstance(m["text_regex"], bool):
        m["text_regex"] = bool(m["text_regex"])
    return m


def _strings(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return []


def _has_criteria(m: Any) -> bool:
    if not isinstance(m, dict):
        return False
    if m.get("severity_max") is not None:
        return True
    return any(m.get(k) for k in ("facilities", "programs", "text", "trap_oids", "varbinds"))


def is_placeholder(kind: str, draft: dict[str, Any]) -> bool:
    """Deterministic backstop (pure): an empty draft, a `<UNKNOWN>`-style value anywhere, a
    placeholder name, or nothing to match (alert rule: no statuses; global event rule: an
    empty match) is not a draft the admin can use."""
    if not draft:
        return True
    if any(PLACEHOLDER_RE.search(s) for s in _strings(draft)):
        return True
    name = draft.get("name")
    if name is not None and str(name).strip().lower() in _PLACEHOLDER_WORDS:
        return True
    if kind == "alert_rule":
        st = draft.get("on_statuses")
        return not (isinstance(st, list) and st)
    if draft.get("scope") in (None, "global") and not _has_criteria(draft.get("match")):
        return True
    return False


def cannot_draft_of(raw: Any) -> str | None:
    """The model's own "cannot draft" answer: its reason ('' when it gave none), else None."""
    if isinstance(raw, dict) and raw.get("cannot_draft") is True:
        return str(raw.get("reason") or "").strip()[:500]
    return None


def language_line(lang: str) -> str:
    """Appended to the description (as the chat appends its answer instruction)."""
    from netmon.assistant.lang import LANGUAGE_NAMES, answer_instruction

    name = LANGUAGE_NAMES.get(lang, "English")
    return (
        f"{answer_instruction(lang)} Write name, explanation, assumptions and reason in {name}; "
        "keep technical values (ids, OIDs, regexes, program names, kinds) as they are."
    )


def split_draft(kind: str, raw: Any) -> tuple[dict[str, Any], str, list[str]]:
    """(draft = exactly the writable fields, explanation, assumptions)."""
    model, _ = models(kind)
    raw = raw if isinstance(raw, dict) else {}
    fields = set(model.model_fields)
    draft = {k: v for k, v in raw.items() if k in fields}
    if kind == "event_rule":
        for k in ("match", "clear_match"):
            if k in draft:
                draft[k] = normalize_match(draft[k])
    explanation = str(raw.get("explanation") or "")[:1000]
    assumptions = [str(a)[:300] for a in (raw.get("assumptions") or []) if str(a).strip()][:10]
    return draft, explanation, assumptions


def _err(loc: list[Any], msg: str) -> dict[str, Any]:
    return {"loc": loc, "msg": msg}


async def validate(session: Any, kind: str, draft: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate without saving: the same pydantic model and reference checks as the API."""
    model, _ = models(kind)
    try:
        body = model.model_validate(draft)
    except ValidationError as exc:
        return [
            _err(
                ["body", *[p for p in e.get("loc", ()) if p is not None]],
                str(e.get("msg", "invalid")),
            )
            for e in exc.errors()
        ]
    errors: list[dict[str, Any]] = []
    try:
        if kind == "alert_rule":
            from netmon.api import alerting

            channel_ids = list(dict.fromkeys(body.channel_ids))
            await alerting._check_refs(session, body.sensor_id, channel_ids)  # noqa: SLF001
            await alerting._check_escalation(  # noqa: SLF001
                session,
                list(body.on_statuses),
                body.delay_s,
                body.escalation_after_s,
                list(dict.fromkeys(body.escalation_channel_ids)),
                body.repeat_every_s,
                True,
            )
        else:
            from netmon.api import ingest as ingest_api
            from netmon.ingest import rules

            data = body.model_dump(exclude_unset=True)
            data.setdefault("enabled", True)
            data.setdefault("priority", 100)
            data.setdefault("scope", "global")
            v = rules.validate_rule(data)
            await ingest_api._check_refs(session, v)  # noqa: SLF001
    except ApiError as exc:
        for d in exc.details or [{"loc": ["body"], "msg": exc.message}]:
            errors.append(_err(list(d.get("loc") or ["body"]), str(d.get("msg") or exc.message)))
    return errors


async def run_draft(rt: Any, user: Any, ctx: Any, kind: str, description: str) -> dict[str, Any]:
    if kind not in ("alert_rule", "event_rule"):
        raise ApiError(
            422,
            "validation_error",
            "kind must be alert_rule or event_rule",
            [{"loc": ["body", "kind"], "msg": "must be alert_rule or event_rule"}],
        )
    async with rt.sessionmaker() as session:
        await common.require(rt, "ai_rule_drafts", session)
        if kind == "event_rule" and not (common.on(rt, "traps") or common.on(rt, "syslog")):
            raise ApiError(
                409,
                "feature_idle",
                "Event rules need traps or syslog",
                [{"feature": "ai_rule_drafts", "needs": ["traps", "syslog"]}],
            )
        a = await common.assistant_settings(session)
        tz = await common.tz_name(session)
        what = "alert rule" if kind == "alert_rule" else "event rule"
        tool = propose_tool(kind)
        block = BLOCK.format(what=what, tool=tool["name"], subject=SUBJECT[kind])
        if kind == "event_rule":
            block += " " + EVENT_HELP
        system = await common.system_prompt(
            rt, "rule_draft", block, redaction=bool(a.get("redact_internal_ips")), session=session
        )
    await common.check_caps(rt, "rule_draft", user.id, question=True, reserve=True)
    from netmon.assistant import redact
    from netmon.assistant import tools as tools_mod

    tools_mod.ensure_loaded()
    gate = tools_mod.GateInfo(
        features=rt.features.snapshot() if getattr(rt, "features", None) else {},
        assistant=a,
        settings={},
        purpose="rule_draft",
    )
    offered = [t.name for t in tools_mod.available(gate, READ_TOOLS)]
    specs = [t.spec()["toolSpec"] for t in tools_mod.available(gate, READ_TOOLS)]
    policy = common.redact_policy(a)
    question = redact.apply_text(description, policy)
    from netmon.assistant.lang import detect_language

    lang = detect_language(description, getattr(user, "language", None) or "en")
    messages: list[dict[str, Any]] = [
        common.user_message(f"Description: {question}\n\n{language_line(lang)}")
    ]
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    tool_calls = 0
    model_calls = 0
    started = time.monotonic()
    final: Any = None
    last = None
    repair_rounds = 0
    errors: list[dict[str, Any]] = []
    cannot: dict[str, str] | None = None

    def remaining() -> float:
        return DEADLINE_S - (time.monotonic() - started)

    async def call(forced: bool) -> Any:
        nonlocal model_calls, last
        if remaining() <= 1:
            raise common.AiError("assistant_timeout", "The draft took too long.", 504)
        comp = await common.complete(
            rt,
            purpose="rule_draft",
            system=system,
            messages=messages,
            user_id=user.id,
            max_tokens=1500,
            timeout_s=remaining(),
            tools=[*specs, tool],
            tool_choice=tool["name"] if forced else "auto",
            question=False,
            check=True,
        )
        model_calls += 1
        for k in totals:
            totals[k] += int(comp.usage.get(k, 0))
        last = comp
        return comp

    try:
        for _ in range(MAX_ROUNDS):
            comp = await call(forced=tool_calls >= MAX_TOOL_CALLS)
            messages.append(
                comp.message or {"role": "assistant", "content": [{"text": comp.text or "."}]}
            )
            proposal = next((u for u in comp.tool_uses if u.get("name") == tool["name"]), None)
            if proposal is not None:
                final = proposal
                break
            reads = [u for u in comp.tool_uses if u.get("name") != tool["name"]]
            if not reads:
                # answered in text: force the proposal
                messages.append(common.user_message(f"Call {tool['name']} now with the draft."))
                comp = await call(forced=True)
                messages.append(comp.message or {"role": "assistant", "content": []})
                final = next((u for u in comp.tool_uses if u.get("name") == tool["name"]), None)
                break
            results = []
            for u in reads:
                tool_calls += 1
                if tool_calls > MAX_TOOL_CALLS:
                    env = {
                        "ok": False,
                        "tool": u.get("name"),
                        "error": "limit_reached",
                        "detail": "no more lookups; propose the draft now",
                    }
                    results.append(_tool_result(u, env, ok=False))
                    continue
                run = await tools_mod.run_tool(
                    rt,
                    name=str(u.get("name")),
                    raw_args=u.get("input"),
                    offered=offered,
                    user_id=user.id,
                    username=user.username,
                    role=user.role,
                    gate=gate,
                    policy=policy,
                    now=rt.clock.now(),
                    timezone=tz,
                )
                results.append(_tool_result(u, run.envelope, ok=run.ok))
            messages.append({"role": "user", "content": results})
        if final is None:
            raise common.AiError("invalid_output", "The model did not return a draft")
        raw = common.detokenize(final.get("input"), policy)
        draft, explanation, assumptions = split_draft(kind, raw)
        reason = cannot_draft_of(raw)
        if reason is not None:
            cannot = {"reason": reason or CANNOT_REASON[lang], "source": "model"}
        elif is_placeholder(kind, draft):
            cannot = {"reason": CANNOT_REASON[lang], "source": "placeholder"}
        if cannot is None:
            async with rt.sessionmaker() as session:
                errors = await validate(session, kind, draft)
                await session.rollback()
        if cannot is None and errors and remaining() > 3:
            repair_rounds = 1
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": final.get("toolUseId"),
                                "status": "error",
                                "content": [{"json": {"ok": False, "validation_errors": errors}}],
                            }
                        },
                        {"text": f"Fix these validation errors and call {tool['name']} again."},
                    ],
                }
            )
            comp = await call(forced=True)
            again = next((u for u in comp.tool_uses if u.get("name") == tool["name"]), None)
            if again is not None:
                raw2 = common.detokenize(again.get("input"), policy)
                d2, e2, a2 = split_draft(kind, raw2)
                reason2 = cannot_draft_of(raw2)
                if reason2 is not None:
                    cannot = {"reason": reason2 or CANNOT_REASON[lang], "source": "model"}
                elif is_placeholder(kind, d2):
                    cannot = {"reason": CANNOT_REASON[lang], "source": "placeholder"}
                else:
                    async with rt.sessionmaker() as session:
                        errs2 = await validate(session, kind, d2)
                        await session.rollback()
                    draft, explanation, assumptions, errors = (
                        d2,
                        e2 or explanation,
                        a2 or assumptions,
                        errs2,
                    )
    except common.AiError as err:
        await common.audit_user(
            rt,
            ctx,
            "assistant.rule_draft",
            None,
            None,
            {
                "kind": kind,
                "purpose": "rule_draft",
                "status": "failed",
                "error_code": err.code,
                "model_calls": model_calls,
                "tool_calls": tool_calls,
            },
        )
        raise err.api_error() from None
    except Exception as exc:  # noqa: BLE001
        err = common.to_ai_error(exc)
        await common.audit_user(
            rt,
            ctx,
            "assistant.rule_draft",
            None,
            None,
            {"kind": kind, "purpose": "rule_draft", "status": "failed", "error_code": err.code},
        )
        raise err.api_error() from None
    if cannot is not None:
        draft, explanation, assumptions, errors = {}, "", [], []
    validation = {"ok": cannot is None and not errors, "errors": errors}
    conv_id = await store_conversation(
        rt,
        user,
        "rule_draft",
        "rule_draft",
        description,
        cannot["reason"] if cannot else explanation,
        last,
        totals,
        model_calls,
        tool_calls,
    )
    async with rt.sessionmaker() as session:
        await common.put_artifact(
            session,
            "rule_draft",
            f"draft:{uuid.uuid4()}",
            "ok",
            content=cannot["reason"] if cannot else explanation,
            data={
                "kind": kind,
                "draft": None if cannot else draft,
                "cannot_draft": cannot,
                "assumptions": assumptions,
                "validation": validation,
                "language": lang,
            },
            conversation_id=conv_id,
            created_by=user.id,
            comp=last,
        )
        await session.commit()
    await common.audit_user(
        rt,
        ctx,
        "assistant.rule_draft",
        "assistant_conversation",
        conv_id,
        {
            "kind": kind,
            "purpose": "rule_draft",
            "status": "ok",
            "valid": validation["ok"],
            "cannot_draft": cannot["source"] if cannot else None,
            "repair_rounds": repair_rounds,
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
        },
    )
    return {
        "kind": kind,
        "draft": None if cannot else draft,
        "cannot_draft": cannot,
        "language": lang,
        "explanation": explanation,
        "assumptions": assumptions,
        "validation": validation,
        "conversation_id": conv_id,
        "usage": {"input_tokens": totals["input_tokens"], "output_tokens": totals["output_tokens"]},
    }


def _tool_result(use: dict[str, Any], env: dict[str, Any], *, ok: bool) -> dict[str, Any]:
    return {
        "toolResult": {
            "toolUseId": use.get("toolUseId"),
            "status": "success" if ok else "error",
            "content": [{"json": env}],
        }
    }


async def store_conversation(
    rt: Any,
    user: Any,
    kind: str,
    purpose: str,
    question: str,
    answer: str,
    comp: Any,
    totals: dict[str, int],
    model_calls: int,
    tool_calls: int,
    *,
    title_prefix: str = "",
) -> int:
    """A finished two-message conversation (drafts: the admin can read it later)."""
    from netmon.assistant.lang import detect_language

    try:
        lang = detect_language(question, user.language or "en")
    except Exception:  # noqa: BLE001
        lang = "en"
    title = (title_prefix + " ".join(question.split()))[:80] or kind
    now = rt.clock.now()
    async with rt.sessionmaker() as session:
        cid = (
            await session.execute(
                text(
                    "INSERT INTO assistant_conversations (user_id, kind, channel, title, language, "
                    "message_count, created_at, updated_at, last_message_at) VALUES (:u, :k, "
                    "'web', "
                    ":t, :l, 2, :now, :now, :now) RETURNING id"
                ),
                {"u": user.id, "k": kind, "t": title, "l": lang, "now": now},
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO assistant_messages (conversation_id, seq, role, purpose, content, "
                "status, created_at, finished_at) VALUES (:c, 1, 'user', :p, :q, 'complete', "
                ":now, :now)"
            ),
            {"c": cid, "p": purpose, "q": question[:2000], "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO assistant_messages (conversation_id, seq, role, purpose, content, "
                "status, "
                "model_id, model_calls, tool_calls, input_tokens, output_tokens, "
                "cache_read_tokens, "
                "cache_write_tokens, cost_estimate, created_at, finished_at) VALUES (:c, 2, "
                "'assistant', :p, :a, 'complete', :m, :mc, :tc, :i, :o, :cr, :cw, :cost, :now, "
                ":now)"
            ),
            {
                "c": cid,
                "p": purpose,
                "a": answer or "",
                "m": comp.model_id if comp else None,
                "mc": model_calls,
                "tc": tool_calls,
                "i": totals.get("input_tokens", 0),
                "o": totals.get("output_tokens", 0),
                "cr": totals.get("cache_read_tokens", 0),
                "cw": totals.get("cache_write_tokens", 0),
                "cost": comp.cost if comp else None,
                "now": now,
            },
        )
        await session.commit()
    return int(cid)


_ = (asyncio, json)
