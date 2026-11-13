"""[3b] Pure conversation context: rebuild the Converse message list from stored exchanges and
trim it deterministically to the budget (architecture § 14.3.4, data-model § [3b] Context trim
vectors).

An *exchange* is the list of Converse messages of one question as sent: the user question,
then assistant ``toolUse`` turns and user ``toolResult`` turns, then (for finished exchanges)
the assistant's final text. Earlier exchanges come from ``assistant_messages.converse_turns``.

Trimming (no model call), when the estimate exceeds the budget:
1. ``toolResult`` contents of **earlier** exchanges become stubs
   ``{"omitted": true, "tool": "search_syslog", "rows": 37}`` (oldest exchange first);
2. whole earlier exchanges are dropped (oldest first) keeping the first exchange (the topic) and
   the last 4 exchanges (the current one included);
3. the current exchange's oldest tool results are stubbed.
The current exchange is never dropped.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

CHARS_PER_TOKEN = 3.2
KEEP_LAST = 4
NO_ANSWER = "(no answer)"

Message = dict[str, Any]
Exchange = list[Message]


def _chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def estimate_tokens(messages: Sequence[Message], system_chars: int = 0, factor: float = 1.0) -> int:
    return int((_chars(list(messages)) + system_chars) / CHARS_PER_TOKEN * max(factor, 0.1))


def correction(last_input_tokens: int | None, last_estimate: int | None) -> float:
    """Factor that turns our estimate into the model's real count (clamped 0.5–3)."""
    if not last_input_tokens or not last_estimate:
        return 1.0
    return min(3.0, max(0.5, last_input_tokens / last_estimate))


# --------------------------------------------------------------------------- sanitising
def _clean_content(content: Any) -> list[dict[str, Any]]:
    out = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if "text" in block:
            if isinstance(block["text"], str) and block["text"].strip():
                out.append({"text": block["text"]})
        elif "toolUse" in block or "toolResult" in block:
            out.append(block)
    return out


def _tool_use_ids(msg: Message) -> list[str]:
    return [b["toolUse"]["toolUseId"] for b in msg["content"] if "toolUse" in b]


def _tool_result_ids(msg: Message) -> list[str]:
    return [b["toolResult"]["toolUseId"] for b in msg["content"] if "toolResult" in b]


def sanitize_exchange(exchange: Sequence[Message], *, current: bool = False) -> Exchange:
    """Make a stored exchange valid for Converse: alternating roles, no empty text blocks, every
    ``toolUse`` answered by a ``toolResult`` in the next user turn; a finished (earlier) exchange
    ends with an assistant text."""
    msgs: Exchange = []
    for m in exchange or []:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        content = _clean_content(m.get("content"))
        if not content:
            continue
        if msgs and msgs[-1]["role"] == m["role"]:
            msgs[-1]["content"].extend(content)
        else:
            msgs.append({"role": m["role"], "content": content})
    if not msgs or msgs[0]["role"] != "user":
        return []
    # pair toolUse / toolResult
    fixed: Exchange = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m["role"] == "assistant" and _tool_use_ids(m):
            nxt = msgs[i + 1] if i + 1 < len(msgs) else None
            answered = set(_tool_result_ids(nxt)) if nxt and nxt["role"] == "user" else set()
            uses = set(_tool_use_ids(m))
            if uses <= answered:
                fixed.append(m)
                results = [
                    b
                    for b in nxt["content"]  # type: ignore[index]
                    if "toolResult" not in b or b["toolResult"]["toolUseId"] in uses
                ]
                fixed.append({"role": "user", "content": results})
                i += 2
                continue
            # unanswered tool calls (cancelled mid-tool): keep only the text of that turn
            text = [b for b in m["content"] if "text" in b]
            if text:
                fixed.append({"role": "assistant", "content": text})
            i += 1
            if nxt is not None and nxt["role"] == "user":
                rest = [b for b in nxt["content"] if "toolResult" not in b]
                if rest:
                    fixed.append({"role": "user", "content": rest})
                i += 1
            continue
        if m["role"] == "user":
            m = {"role": "user", "content": [b for b in m["content"] if "toolResult" not in b]}
            if not m["content"]:
                i += 1
                continue
        fixed.append(m)
        i += 1
    # re-merge equal roles created by the pairing fixes
    merged: Exchange = []
    for m in fixed:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"].extend(m["content"])
        else:
            merged.append(m)
    if not merged or merged[0]["role"] != "user":
        return []
    if not current and merged[-1]["role"] != "assistant":
        merged.append({"role": "assistant", "content": [{"text": NO_ANSWER}]})
    return merged


def flatten(exchanges: Sequence[Exchange]) -> list[Message]:
    out: list[Message] = []
    for ex in exchanges:
        for m in ex:
            if out and out[-1]["role"] == m["role"]:
                out[-1] = {"role": m["role"], "content": [*out[-1]["content"], *m["content"]]}
            else:
                out.append(m)
    return out


def has_tool_blocks(messages: Sequence[Message]) -> bool:
    return any("toolUse" in b or "toolResult" in b for m in messages for b in m.get("content", []))


def strip_tool_blocks(messages: Sequence[Message]) -> list[Message]:
    """For a call without tools: tool blocks become short text notes (Converse requires a
    ``toolConfig`` whenever the history contains tool blocks)."""
    out: list[Message] = []
    for m in messages:
        content = []
        for b in m["content"]:
            if "toolUse" in b:
                content.append({"text": f"[called tool {b['toolUse'].get('name', '?')}]"})
            elif "toolResult" in b:
                payload = b["toolResult"].get("content") or []
                data = payload[0].get("json") if payload and isinstance(payload[0], dict) else None
                content.append(
                    {"text": "[tool result] " + json.dumps(data, ensure_ascii=False)[:4000]}
                )
            else:
                content.append(b)
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"].extend(content)
        else:
            out.append({"role": m["role"], "content": content})
    return out


# --------------------------------------------------------------------------- trimming
def _tool_names(exchange: Exchange) -> dict[str, str]:
    names: dict[str, str] = {}
    for m in exchange:
        for b in m["content"]:
            if "toolUse" in b:
                names[b["toolUse"]["toolUseId"]] = b["toolUse"].get("name", "")
    return names


def _stub_block(block: dict[str, Any], names: dict[str, str]) -> bool:
    tr = block["toolResult"]
    content = tr.get("content") or []
    data = content[0].get("json") if content and isinstance(content[0], dict) else None
    if isinstance(data, dict) and data.get("omitted") is True:
        return False
    rows = data.get("rows") if isinstance(data, dict) else None
    tool = names.get(tr.get("toolUseId", ""), "") or (
        data.get("tool", "") if isinstance(data, dict) else ""
    )
    tr["content"] = [{"json": {"omitted": True, "tool": tool, "rows": rows}}]
    return True


def stub_results(exchange: Exchange, limit: int | None = None) -> int:
    """Stub the tool results of an exchange in place (oldest first, at most `limit`)."""
    names = _tool_names(exchange)
    n = 0
    for m in exchange:
        for b in m["content"]:
            if "toolResult" in b and (limit is None or n < limit) and _stub_block(b, names):
                n += 1
    return n


@dataclass
class TrimResult:
    messages: list[Message]
    estimate: int
    stubbed: int = 0
    dropped: list[int] = field(default_factory=list)  # indices of dropped earlier exchanges

    @property
    def changed(self) -> bool:
        return bool(self.stubbed or self.dropped)


def trim(
    exchanges: Sequence[Exchange],
    budget_tokens: int,
    *,
    system_chars: int = 0,
    factor: float = 1.0,
) -> TrimResult:
    """Deterministic trim (see module docstring). `exchanges[-1]` is the current one."""
    ex: list[Exchange | None] = [copy.deepcopy(list(e)) for e in exchanges]
    n = len(ex)

    def est() -> int:
        return estimate_tokens(flatten([e for e in ex if e]), system_chars, factor)

    total = est()
    res = TrimResult([], total)
    if total > budget_tokens:
        # 1. stub earlier exchanges' tool results, oldest first
        for i in range(n - 1):
            e = ex[i]
            if e:
                res.stubbed += stub_results(e)
            total = est()
            if total <= budget_tokens:
                break
    if total > budget_tokens:
        # 2. drop earlier exchanges, keeping the first and the last KEEP_LAST
        for i in range(1, max(1, n - KEEP_LAST)):
            if i >= n - 1:
                break
            ex[i] = None
            res.dropped.append(i)
            total = est()
            if total <= budget_tokens:
                break
    if total > budget_tokens and n:
        # 3. the current exchange's oldest tool results
        cur = ex[-1]
        if cur:
            names = _tool_names(cur)
            for m in cur:
                for b in m["content"]:
                    if "toolResult" in b and _stub_block(b, names):
                        res.stubbed += 1
                        total = est()
                        if total <= budget_tokens:
                            break
                if total <= budget_tokens:
                    break
    res.messages = flatten([e for e in ex if e])
    res.estimate = total
    return res
