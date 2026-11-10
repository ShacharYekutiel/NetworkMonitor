"""[3b] Conversation storage (data-model § [3b] `assistant_conversations`, `assistant_messages`,
`assistant_tool_calls`) and the api.md shapes `AssistantConversation` / `AssistantMessage` /
`AssistantStep`.

Plain SQL through the caller's session; the caller commits. Only the owner sees a conversation
(every read filters by ``user_id``)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.assistant import redact

TITLE_LEN = 80
MAX_EXCHANGES = 40  # "conversation length 40 turns" (one turn = one question and its answer)
CONVERSE_TURNS_MAX = 512 * 1024


def _iso(dt: datetime | None) -> str | None:
    from netmon.services.serialize import iso

    return iso(dt)


def _json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


# --------------------------------------------------------------------------- audit titles
# Owner rule: the audit log never holds question text. It gets a neutral topic title that the
# light model writes (`purpose = title`), post-processed here; on any failure (or while it is
# being generated) the generic fallback below.
AUDIT_TITLE_WORDS = 6
AUDIT_TITLE_LEN = 60
FALLBACK_AUDIT_TITLES = {"en": "Assistant question", "he": "שאלה לעוזר"}
_TITLE_STRIP = re.compile(r"[\"'`*_#<>\[\]{}()|~“”„«»]")
_MAC_RE = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])"
                     r"|(?<![0-9a-f.])(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}(?![0-9a-f])")  # fmt: skip
_TITLE_LABEL = re.compile(r"(?i)^\s*(?:title|כותרת)\s*[:：-]\s*")
_BAD_TOKEN = re.compile(r"[\d.@/\\:=$%&+^]")


def fallback_audit_title(language: str | None) -> str:
    return FALLBACK_AUDIT_TITLES.get(language or "en", FALLBACK_AUDIT_TITLES["en"])


def neutral_title(raw: str | None, language: str | None = "en") -> str:
    """Post-process a model-written topic title for the audit log: one line, secrets / IPs /
    MACs removed, every token that looks like an identifier (digits, dots, slashes, ``@``, ``:``
    … — addresses, hostnames, serials, ids, paths) dropped, ≤ 6 words and ≤ 60 characters.
    Empty → the generic fallback."""
    t = redact.clean_control(raw or "").strip()
    t = next((ln for ln in t.splitlines() if ln.strip()), "")
    t = _TITLE_LABEL.sub("", t)
    t = redact.redact_secrets(t)
    for mark in redact.MARKS:
        t = t.replace(mark, " ")
    t = _MAC_RE.sub(" ", t)
    t = redact._IPV4.sub(" ", t)
    t = redact._IPV6.sub(" ", t)
    t = redact.PLACEHOLDER.sub(" ", t)
    t = _TITLE_STRIP.sub(" ", t)
    words: list[str] = []
    for w in t.split():
        w = w.strip(",;!?-–—")
        if not w or _BAD_TOKEN.search(w) or len(w) > 30:
            continue
        words.append(w)
        if len(words) >= AUDIT_TITLE_WORDS:
            break
    out = " ".join(words)
    if len(out) > AUDIT_TITLE_LEN:
        out = out[:AUDIT_TITLE_LEN].rsplit(" ", 1)[0]
    out = out.strip(" ,;:-–—")
    return out or fallback_audit_title(language)


def make_title(question: str) -> str:
    t = " ".join(redact.redact_secrets(redact.clean_control(question or "")).split())
    if len(t) > TITLE_LEN:
        t = t[: TITLE_LEN - 1].rstrip() + "…"
    return t or "Conversation"


# --------------------------------------------------------------------------- shapes
def conversation_out(r: Any) -> dict[str, Any]:
    g = r if isinstance(r, dict) else dict(r)
    ref = None
    if g.get("ref_sensor_id") is not None or g.get("ref_event_id") is not None:
        ref = {"sensor_id": g.get("ref_sensor_id"), "event_id": g.get("ref_event_id")}
    return {
        "id": int(g["id"]),
        "kind": g["kind"],
        "channel": g["channel"],
        "title": g["title"],
        "language": g["language"],
        "message_count": int(g["message_count"]),
        "ref": ref,
        "created_at": _iso(g["created_at"]),
        "last_message_at": _iso(g["last_message_at"]),
    }


def step_out(r: Any, ipmap: redact.IpMap | None = None) -> dict[str, Any]:
    g = r if isinstance(r, dict) else dict(r)
    args = _json(g.get("arguments")) or {}
    return {
        "call_id": g["tool_use_id"],
        "round": int(g["round"]),
        "tool": g["tool"],
        "label_key": f"assistant.tool.{g['tool']}",
        "args": redact.detokenize(args, ipmap) if ipmap is not None else args,
        "state": "done",
        "ok": bool(g["ok"]),
        "rows": g.get("rows"),
        "truncated": bool(g.get("truncated")),
        "ms": int(g["duration_ms"]) if g.get("duration_ms") is not None else None,
        "error": g.get("error_code"),
    }


ERROR_MESSAGES = {
    "interrupted": "The answer was interrupted by a server restart.",
    "assistant_timeout": "The answer took too long.",
}


def message_out(
    r: Any,
    steps: list[dict[str, Any]] | None = None,
    *,
    currency: str | None = None,
) -> dict[str, Any]:
    g = r if isinstance(r, dict) else dict(r)
    err = None
    if g.get("error_code"):
        code = g["error_code"]
        msg = ERROR_MESSAGES.get(code)
        if msg is None:
            try:
                from netmon.assistant.errors import CODES

                msg = CODES.get(code, (0, code))[1].format(region="", host="", aws=code)
            except Exception:  # noqa: BLE001
                msg = code
        err = {"code": code, "message": msg}
    cost = g.get("cost_estimate")
    sources = _json(g.get("sources")) or []
    for s in sources:
        s.setdefault("url", None)
    return {
        "id": int(g["id"]),
        "conversation_id": int(g["conversation_id"]),
        "seq": int(g["seq"]),
        "role": g["role"],
        "purpose": g["purpose"],
        "content": g.get("content") or "",
        "notes": _json(g.get("notes")) or [],
        "steps": steps or [],
        "sources": sources,
        "status": g["status"],
        "error": err,
        "notices": list(g.get("notices") or []),
        "model_id": g.get("model_id"),
        "model_calls": int(g.get("model_calls") or 0),
        "tool_calls": int(g.get("tool_calls") or 0),
        "usage": {
            "input_tokens": int(g.get("input_tokens") or 0),
            "output_tokens": int(g.get("output_tokens") or 0),
            "cache_read_tokens": int(g.get("cache_read_tokens") or 0),
            "cache_write_tokens": int(g.get("cache_write_tokens") or 0),
        },
        "cost_estimate": float(cost) if cost is not None else None,
        "currency": currency if cost is not None else None,
        "duration_ms": g.get("duration_ms"),
        "feedback": g.get("feedback"),
        "feedback_comment": g.get("feedback_comment"),
        "created_at": _iso(g["created_at"]),
        "finished_at": _iso(g.get("finished_at")),
    }


# --------------------------------------------------------------------------- reads
async def get_conversation(
    session: AsyncSession, conversation_id: int, user_id: int, *, lock: bool = False
) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text(
                    "SELECT * FROM assistant_conversations WHERE id = :id AND user_id = :u"
                    + (" FOR UPDATE" if lock else "")
                ),
                {"id": conversation_id, "u": user_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def get_message_row(
    session: AsyncSession, message_id: int, user_id: int
) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text(
                    "SELECT m.*, c.user_id, c.redaction_map FROM assistant_messages m "
                    "JOIN assistant_conversations c ON c.id = m.conversation_id "
                    "WHERE m.id = :id AND c.user_id = :u"
                ),
                {"id": message_id, "u": user_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def steps_for(
    session: AsyncSession, message_ids: list[int], ipmap: redact.IpMap | None = None
) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    if not message_ids:
        return out
    for r in (
        await session.execute(
            text(
                "SELECT * FROM assistant_tool_calls WHERE message_id = ANY(:ids) "
                "ORDER BY message_id, seq"
            ),
            {"ids": message_ids},
        )
    ).mappings():
        out.setdefault(int(r["message_id"]), []).append(step_out(r, ipmap))
    return out


async def currency(session: AsyncSession) -> str | None:
    from netmon.assistant.features.common import assistant_settings

    try:
        a = await assistant_settings(session)
        return (a.get("prices") or {}).get("currency") or None
    except Exception:  # noqa: BLE001
        return None


async def message_full(session: AsyncSession, message_id: int, user_id: int) -> dict | None:
    row = await get_message_row(session, message_id, user_id)
    if row is None:
        return None
    ipmap = redact.IpMap.from_stored(_json(row.get("redaction_map")) or {})
    steps = (await steps_for(session, [message_id], ipmap)).get(message_id, [])
    return message_out(row, steps, currency=await currency(session))


async def conversation_full(
    session: AsyncSession, conversation_id: int, user_id: int
) -> dict[str, Any] | None:
    conv = await get_conversation(session, conversation_id, user_id)
    if conv is None:
        return None
    rows = (
        (
            await session.execute(
                text("SELECT * FROM assistant_messages WHERE conversation_id = :c ORDER BY seq"),
                {"c": conversation_id},
            )
        )
        .mappings()
        .all()
    )
    ipmap = redact.IpMap.from_stored(_json(conv.get("redaction_map")) or {})
    steps = await steps_for(session, [int(r["id"]) for r in rows], ipmap)
    cur = await currency(session)
    out = conversation_out(conv)
    out["messages"] = [message_out(r, steps.get(int(r["id"]), []), currency=cur) for r in rows]
    return out


async def list_conversations(
    session: AsyncSession,
    user_id: int,
    *,
    q: str | None,
    kind: str | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    conds = ["user_id = :u"]
    p: dict[str, Any] = {"u": user_id}
    if q:
        conds.append("title ILIKE :q ESCAPE '\\'")
        p["q"] = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    if kind:
        conds.append("kind = :k")
        p["k"] = kind
    where = " AND ".join(conds)
    total = (
        await session.execute(
            text(f"SELECT count(*) FROM assistant_conversations WHERE {where}"),
            p,  # noqa: S608
        )
    ).scalar()
    rows = (
        (
            await session.execute(
                text(
                    f"SELECT * FROM assistant_conversations WHERE {where} "  # noqa: S608
                    "ORDER BY last_message_at DESC, id DESC LIMIT :lim OFFSET :off"
                ),
                {**p, "lim": page_size, "off": (page - 1) * page_size},
            )
        )
        .mappings()
        .all()
    )
    from netmon.services.serialize import page as page_out

    return page_out([conversation_out(r) for r in rows], int(total or 0), page, page_size)


async def history(session: AsyncSession, conversation_id: int) -> list[list[dict[str, Any]]]:
    """The stored Converse exchanges of the conversation (oldest first)."""
    rows = (
        await session.execute(
            text(
                "SELECT converse_turns FROM assistant_messages WHERE conversation_id = :c "
                "AND role = 'assistant' AND converse_turns IS NOT NULL ORDER BY seq"
            ),
            {"c": conversation_id},
        )
    ).all()
    out = []
    for r in rows:
        v = _json(r[0])
        if isinstance(v, list) and v:
            out.append(v)
    return out


async def running_message(session: AsyncSession, user_id: int) -> int | None:
    return (
        await session.execute(
            text(
                "SELECT m.id FROM assistant_messages m JOIN assistant_conversations c "
                "ON c.id = m.conversation_id WHERE c.user_id = :u AND m.status = 'running' "
                "ORDER BY m.id DESC LIMIT 1"
            ),
            {"u": user_id},
        )
    ).scalar()


# --------------------------------------------------------------------------- writes
async def create_conversation(
    session: AsyncSession,
    *,
    user_id: int,
    title: str,
    language: str,
    kind: str = "chat",
    channel: str = "web",
    ref_sensor_id: int | None = None,
    ref_event_id: int | None = None,
) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text(
                    "INSERT INTO assistant_conversations (user_id, kind, channel, title, language, "
                    "ref_sensor_id, ref_event_id) VALUES (:u, :k, :ch, :t, :l, :rs, :re) "
                    "RETURNING *"
                ),
                {
                    "u": user_id,
                    "k": kind,
                    "ch": channel,
                    "t": title[:200],
                    "l": language,
                    "rs": ref_sensor_id,
                    "re": ref_event_id,
                },
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def add_message(
    session: AsyncSession,
    conversation_id: int,
    *,
    role: str,
    purpose: str,
    content: str = "",
    status: str = "complete",
) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text(
                    "INSERT INTO assistant_messages (conversation_id, seq, role, purpose, content, "
                    "status) VALUES (:c, (SELECT coalesce(max(seq), 0) + 1 FROM assistant_messages "
                    "WHERE conversation_id = :c), :r, :p, :t, :s) RETURNING *"
                ),
                {"c": conversation_id, "r": role, "p": purpose, "t": content, "s": status},
            )
        )
        .mappings()
        .one()
    )
    await session.execute(
        text(
            "UPDATE assistant_conversations SET message_count = message_count + 1, "
            "last_message_at = now(), updated_at = now() WHERE id = :c"
        ),
        {"c": conversation_id},
    )
    return dict(row)


async def add_tool_call(
    session: AsyncSession,
    *,
    message_id: int,
    seq: int,
    round_no: int,
    tool_use_id: str,
    tool: str,
    arguments: dict[str, Any],
    ok: bool,
    error_code: str | None,
    rows: int | None,
    result_bytes: int,
    truncated: bool,
    duration_ms: int,
    started_at: datetime,
) -> None:
    await session.execute(
        text(
            "INSERT INTO assistant_tool_calls (message_id, seq, round, tool_use_id, tool, "
            "arguments, ok, error_code, rows, result_bytes, truncated, duration_ms, started_at) "
            "VALUES (:m, :s, :r, :u, :t, CAST(:a AS jsonb), :ok, :e, :rows, :b, :tr, :d, :at)"
        ),
        {
            "m": message_id,
            "s": seq,
            "r": round_no,
            "u": (tool_use_id or "")[:64],
            "t": tool[:64],
            "a": json.dumps(arguments, ensure_ascii=False, default=str),
            "ok": ok,
            "e": error_code,
            "rows": rows,
            "b": result_bytes,
            "tr": truncated,
            "d": duration_ms,
            "at": started_at,
        },
    )


def _cap_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """≤ 512 KiB: stub the oldest tool results until it fits."""
    from netmon.assistant import context

    raw = json.dumps(turns, ensure_ascii=False)
    if len(raw.encode()) <= CONVERSE_TURNS_MAX:
        return turns
    ex = json.loads(raw)
    context.stub_results(ex)
    raw = json.dumps(ex, ensure_ascii=False)
    if len(raw.encode()) <= CONVERSE_TURNS_MAX:
        return ex
    return ex[:1] + [{"role": "assistant", "content": [{"text": "(omitted)"}]}]


async def finish_message(session: AsyncSession, message_id: int, values: dict[str, Any]) -> None:
    v = dict(values)
    v["notes"] = json.dumps(v.get("notes") or [], ensure_ascii=False)
    v["sources"] = json.dumps(v.get("sources") or [], ensure_ascii=False)
    turns = v.get("converse_turns")
    v["converse_turns"] = (
        json.dumps(_cap_turns(turns), ensure_ascii=False) if turns is not None else None
    )
    v["id"] = message_id
    await session.execute(
        text(
            "UPDATE assistant_messages SET content = :content, notes = CAST(:notes AS jsonb), "
            "sources = CAST(:sources AS jsonb), converse_turns = CAST(:converse_turns AS jsonb), "
            "status = :status, error_code = :error_code, notices = :notices, "
            "stop_reason = :stop_reason, model_id = :model_id, model_calls = :model_calls, "
            "tool_calls = :tool_calls, input_tokens = :input_tokens, "
            "output_tokens = :output_tokens, cache_read_tokens = :cache_read_tokens, "
            "cache_write_tokens = :cache_write_tokens, cost_estimate = :cost_estimate, "
            "duration_ms = :duration_ms, finished_at = now() WHERE id = :id"
        ),
        v,
    )


async def update_conversation_state(
    session: AsyncSession,
    conversation_id: int,
    *,
    known_refs: list[str],
    redaction_map: dict[str, str],
    language: str,
) -> None:
    await session.execute(
        text(
            "UPDATE assistant_conversations SET known_refs = :k, "
            "redaction_map = CAST(:m AS jsonb), language = :l, last_message_at = now(), "
            "updated_at = now() WHERE id = :c"
        ),
        {
            "k": known_refs,
            "m": json.dumps(redaction_map, ensure_ascii=False),
            "l": language,
            "c": conversation_id,
        },
    )


async def set_feedback(
    session: AsyncSession, message_id: int, rating: int, comment: str | None
) -> None:
    await session.execute(
        text("UPDATE assistant_messages SET feedback = :f, feedback_comment = :c WHERE id = :id"),
        {"f": rating or None, "c": (comment or None) if rating else None, "id": message_id},
    )


async def rename(session: AsyncSession, conversation_id: int, title: str) -> None:
    await session.execute(
        text("UPDATE assistant_conversations SET title = :t, updated_at = now() WHERE id = :c"),
        {"t": title[:200], "c": conversation_id},
    )


async def delete_conversations(
    session: AsyncSession, user_id: int, conversation_id: int | None = None
) -> int:
    if conversation_id is None:
        res = await session.execute(
            text("DELETE FROM assistant_conversations WHERE user_id = :u"), {"u": user_id}
        )
    else:
        res = await session.execute(
            text("DELETE FROM assistant_conversations WHERE id = :c AND user_id = :u"),
            {"c": conversation_id, "u": user_id},
        )
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


async def mark_interrupted(session: AsyncSession) -> int:
    """Startup: messages still ``running`` become ``error`` / ``interrupted``."""
    res = await session.execute(
        text(
            "UPDATE assistant_messages SET status = 'error', error_code = 'interrupted', "
            "finished_at = now() WHERE status = 'running'"
        )
    )
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


async def purge_expired(session: AsyncSession, now: datetime, retention_days: int) -> int:
    """Retention: conversations whose last message is older than `retention_days`."""
    cutoff = now - timedelta(days=max(1, int(retention_days)))
    res = await session.execute(
        text("DELETE FROM assistant_conversations WHERE last_message_at < :c"), {"c": cutoff}
    )
    return int(res.rowcount or 0)  # type: ignore[attr-defined]
