"""Scripted "model" of the Bedrock mock: scenario selection, templating, Converse request validation.

The model is **stateless**: the position inside a scenario is derived from the request itself (the number of
assistant messages after the last *question* - the last user message that has text and no toolResult). Retries,
re-sends and parallel conversations therefore behave deterministically. See README.md for the YAML format.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
SCENARIO_DIR = HERE / "scenarios"

# Purpose detection (docs/data-model.md `assistant_purpose`). Preferred: an explicit marker line in the system
# prompt, e.g. "Purpose: alert_note" or "[purpose:alert_note]". Fallbacks: forced tool names, then keywords.
PURPOSES = ("ask", "explain", "incident", "alert_note", "diff_explain", "digest", "rule_draft", "template_draft",
            "chat_ask", "report_summary", "test")
MARKER_RE = re.compile(r"(?:^|\n|\[)\s*purpose\s*[:=]\s*`?([a-z_]+)`?", re.I)
FORCED_PURPOSE = {"ping": "test", "propose_alert_rule": "rule_draft", "propose_event_rule": "rule_draft",
                  "propose_template": "template_draft", "report_diff_explanation": "diff_explain",
                  "report_incident_summary": "incident", "report_alert_note": "alert_note",
                  "report_summary": "report_summary", "report_digest": "digest"}
KEYWORDS = [  # (regex on the system prompt, purpose) - only used without a marker
    (r"\balert[- ]note|1[–-]2 line|one or two lines", "alert_note"),
    (r"\bincident\b.*\b(summary|title)|correlat", "incident"),
    (r"\bsyslog digest|daily digest|what was unusual", "digest"),
    (r"\breport\b.*\b(summary|paragraph|narrative)", "report_summary"),
    (r"\bconfig(uration)? diff|risk flags|explain (the )?changes", "diff_explain"),
    (r"\b(alert|event)[- ]rule draft|propose_alert_rule|propose_event_rule", "rule_draft"),
    (r"\btemplate draft|propose_template|snmp walk", "template_draft"),
    (r"\bexplain (this|the) alert|what preceded it|likely cause", "explain"),
]
FINAL_RE = re.compile(r"do not call (any )?tools|answer with what you have", re.I)


# ------------------------------------------------------------------------------------------------ templating
class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"


MISSING = _Missing()
TOKEN_RE = re.compile(r"\.([A-Za-z_][\w-]*)|\[(\d+|-\d+|\*)\]|\[['\"]([^'\"]+)['\"]\]")


def resolve(path: str, ctx: dict[str, Any]) -> Any:
    """`$.a.b[0]`, `$results[1].data`, `$tool.list_problems.data.items[*].ref`, `$question`, `$input.x`."""
    path = path.strip()
    m = re.match(r"^\$([A-Za-z_]\w*)?", path)
    if not m:
        return path  # literal
    root = m.group(1) or ""
    rest = path[m.end():]
    if root == "tool":
        tm = re.match(r"^\.([\w-]+)", rest)
        if not tm:
            return MISSING
        cur: Any = ctx.get("tools", {}).get(tm.group(1), MISSING)
        rest = rest[tm.end():]
    elif root == "":
        cur = ctx.get("last", MISSING)
    else:
        cur = ctx.get(root, MISSING)
    pos = 0
    mapping = False
    while pos < len(rest):
        t = TOKEN_RE.match(rest, pos)
        if not t:
            return MISSING
        pos = t.end()
        key = t.group(1) or t.group(3)
        idx = t.group(2)

        def step(v: Any) -> Any:
            if v is MISSING:
                return MISSING
            if key is not None:
                return v.get(key, MISSING) if isinstance(v, dict) else MISSING
            if isinstance(v, list):
                i = int(idx)
                return v[i] if -len(v) <= i < len(v) else MISSING
            return MISSING

        if idx == "*":
            if mapping:
                cur = [x for sub in cur for x in (sub if isinstance(sub, list) else [])]
            else:
                cur = cur if isinstance(cur, list) else MISSING
                mapping = True
            continue
        if mapping and isinstance(cur, list):
            cur = [step(x) for x in cur]
            cur = [x for x in cur if x is not MISSING]
        else:
            cur = step(cur)
        if cur is MISSING:
            return MISSING
    return cur


def _filter(val: Any, name: str, arg: str | None) -> Any:
    if name == "len":
        return len(val) if isinstance(val, (list, dict, str)) else 0
    if name == "json":
        return json.dumps(val, ensure_ascii=False)
    if name == "join":
        return (arg if arg is not None else ", ").join(str(x) for x in (val if isinstance(val, list) else [val]))
    if name == "first":
        return val[0] if isinstance(val, list) and val else MISSING
    if name == "last":
        return val[-1] if isinstance(val, list) and val else MISSING
    if name == "default":  # default(1) -> int 1, default("x") -> "x"
        if val is MISSING or val is None or val == []:
            try:
                return json.loads(arg) if arg is not None else None
            except ValueError:
                return arg
        return val
    if name == "int":
        try:
            return int(val)
        except (TypeError, ValueError):
            return MISSING
    if name == "upper":
        return str(val).upper()
    if name == "each":  # each("- [{name}](nm:{ref})") with str.format_map per item (missing keys -> "?")
        fmt = arg or "{}"
        items = val if isinstance(val, list) else []

        class D(dict):
            def __missing__(self, k):  # noqa: D105
                return "?"
        return "\n".join(fmt.format_map(D(x)) if isinstance(x, dict) else fmt.format(x) for x in items)
    if name == "take":
        return val[: int(arg or 1)] if isinstance(val, list) else val
    if name == "re":  # re("pattern"): group 1 (or the whole match) of the first match, else missing
        mm = re.search(arg or "", "" if val is MISSING else str(val), re.I)
        return (mm.group(1) if mm and mm.groups() else mm.group(0)) if mm else MISSING
    if name == "where":  # where("type=telegram"): items whose field equals the value (as text)
        k, _, want = (arg or "").partition("=")
        return [x for x in (val if isinstance(val, list) else []) if isinstance(x, dict) and str(x.get(k)) == want]
    if name == "get":  # get("id"): a field of a dict (or of every dict in a list)
        if isinstance(val, list):
            return [x.get(arg) for x in val if isinstance(x, dict) and arg in x]
        return val.get(arg, MISSING) if isinstance(val, dict) else MISSING
    if name == "lower":
        return str(val).lower()
    raise ValueError(f"unknown filter {name}")


EXPR_RE = re.compile(r"\{\{(.*?)\}\}", re.S)
FILTER_RE = re.compile(r"^\s*([a-z_]+)\s*(?:\((.*)\))?\s*$", re.S)


def _split_pipes(expr: str) -> list[str]:
    out, cur, q, esc = [], "", None, False
    for ch in expr:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            cur += ch
            esc = True
        elif q:
            cur += ch
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            cur += ch
        elif ch == "|":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def eval_expr(expr: str, ctx: dict[str, Any], errors: list[str]) -> Any:
    parts = _split_pipes(expr)
    val = resolve(parts[0], ctx)
    for f in parts[1:]:
        m = FILTER_RE.match(f)
        if not m:
            errors.append(f"bad filter {f!r}")
            continue
        arg = m.group(2)
        if arg is not None:
            arg = arg.strip()
            if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "\"'":
                arg = arg[1:-1].replace("\\n", "\n")  # only \\n; regex escapes and Hebrew stay as written
        try:
            val = _filter(val, m.group(1), arg)
        except ValueError as e:
            errors.append(str(e))
    if val is MISSING:
        errors.append(f"missing {parts[0].strip()}")
    return val


def render(value: Any, ctx: dict[str, Any], errors: list[str]) -> Any:
    """Strings are templated; a string that is exactly one `{{ expr }}` keeps the native type (ints for ids)."""
    if isinstance(value, str):
        m = EXPR_RE.fullmatch(value.strip()) if value.count("{{") == 1 else None
        if m:
            v = eval_expr(m.group(1), ctx, errors)
            return "?" if v is MISSING else v
        return EXPR_RE.sub(lambda mm: _to_text(eval_expr(mm.group(1), ctx, errors)), value)
    if isinstance(value, list):
        return [render(v, ctx, errors) for v in value]
    if isinstance(value, dict):
        return {k: render(v, ctx, errors) for k, v in value.items()}
    return value


def _to_text(v: Any) -> str:
    if v is MISSING or v is None:
        return "?"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


# ------------------------------------------------------------------------------------------------ scenarios
@dataclass
class Scenario:
    name: str
    purposes: list[str]
    match: re.Pattern | None
    turns: list[dict[str, Any]]
    priority: int = 0
    requires_tools: list[str] = field(default_factory=list)
    final: str | None = None
    forced: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] | None = None
    stream: dict[str, Any] = field(default_factory=dict)
    source: str = "file"
    order: int = 0
    lang: str | None = None          # "he" / "en": only for requests in that language (see detect_lang)
    match_system: re.Pattern | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any], source: str = "file", order: int = 0) -> "Scenario":
        p = d.get("purpose", "ask")
        purposes = [p] if isinstance(p, str) else list(p)
        m = d.get("match")
        return cls(name=d["name"], purposes=purposes, match=re.compile(m, re.I | re.S) if m else None,
                   turns=list(d.get("turns") or []), priority=int(d.get("priority", 0)),
                   requires_tools=list(d.get("requires_tools") or []), final=d.get("final"),
                   forced=dict(d.get("forced") or {}), usage=d.get("usage"), stream=dict(d.get("stream") or {}),
                   source=source, order=order, lang=d.get("lang"),
                   match_system=re.compile(d["match_system"], re.I | re.S) if d.get("match_system") else None)


def load_scenarios(dirs: list[Path] | None = None) -> list[Scenario]:
    out: list[Scenario] = []
    n = 0
    for d in dirs or [SCENARIO_DIR]:
        for f in sorted(Path(d).glob("*.yaml")):
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            for s in doc.get("scenarios", [doc] if "name" in doc else []):
                n += 1
                out.append(Scenario.from_dict(s, source=f.name, order=n))
    return out


# ------------------------------------------------------------------------------------------------ request view
@dataclass
class RequestView:
    body: dict[str, Any]
    system_text: str
    messages: list[dict[str, Any]]
    offered: list[str]
    tool_specs: dict[str, dict[str, Any]]
    forced_tool: str | None
    tool_choice: dict[str, Any] | None
    question: str
    question_index: int
    turn_index: int
    results: list[Any]            # tool results of the last user message (json content), in order
    exchange_results: list[tuple[str, Any]]  # (tool name, result) since the question
    has_cache_point: bool
    is_final_call: bool
    max_tokens: int | None
    purpose: str
    lang: str = "en"


def _texts(content: list[dict[str, Any]]) -> list[str]:
    return [b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]


def _result_json(tr: dict[str, Any]) -> Any:
    for c in tr.get("content") or []:
        if "json" in c:
            return c["json"]
        if "text" in c:
            try:
                return json.loads(c["text"])
            except ValueError:
                return c["text"]
    return None


def view(body: dict[str, Any]) -> RequestView:
    system_text = "\n".join(s.get("text", "") for s in body.get("system") or [] if isinstance(s, dict))
    msgs = body.get("messages") or []
    tc = body.get("toolConfig") or None
    tools = [t for t in (tc or {}).get("tools", []) if isinstance(t, dict) and "toolSpec" in t]
    offered = [t["toolSpec"].get("name") for t in tools]
    specs = {t["toolSpec"].get("name"): t["toolSpec"] for t in tools}
    choice = (tc or {}).get("toolChoice")
    forced = (choice or {}).get("tool", {}).get("name") if isinstance(choice, dict) else None
    qi = -1
    for i in range(len(msgs) - 1, -1, -1):
        c = msgs[i].get("content") or []
        if msgs[i].get("role") == "user" and _texts(c) and not any("toolResult" in b for b in c):
            qi = i
            break
    question = "\n".join(_texts(msgs[qi]["content"])) if qi >= 0 else ""
    turn_index = sum(1 for m in msgs[qi + 1:] if m.get("role") == "assistant")
    id2name: dict[str, str] = {}
    exch: list[tuple[str, Any]] = []
    for m in msgs[qi + 1:]:
        for b in m.get("content") or []:
            if "toolUse" in b:
                id2name[b["toolUse"].get("toolUseId")] = b["toolUse"].get("name")
            if "toolResult" in b:
                tr = b["toolResult"]
                exch.append((id2name.get(tr.get("toolUseId"), "?"), _result_json(tr)))
    last_results = []
    if msgs and msgs[-1].get("role") == "user":
        last_results = [_result_json(b["toolResult"]) for b in msgs[-1].get("content") or [] if "toolResult" in b]
    cache = any("cachePoint" in s for s in body.get("system") or [] if isinstance(s, dict)) or any(
        "cachePoint" in t for t in (tc or {}).get("tools", []) if isinstance(t, dict)) or any(
        "cachePoint" in b for m in msgs for b in m.get("content") or [] if isinstance(b, dict))
    last_user_text = "\n".join(_texts(msgs[-1].get("content") or [])) if msgs else ""
    has_tool_history = any("toolUse" in b for m in msgs for b in m.get("content") or [] if isinstance(b, dict))
    is_final = bool(FINAL_RE.search(system_text) or (FINAL_RE.search(last_user_text) and turn_index > 0)
                    or (tc is not None and choice is None and has_tool_history))
    v = RequestView(body=body, system_text=system_text, messages=msgs, offered=offered, tool_specs=specs,
                    forced_tool=forced, tool_choice=choice, question=question, question_index=qi,
                    turn_index=turn_index, results=last_results, exchange_results=exch, has_cache_point=cache,
                    is_final_call=is_final, max_tokens=(body.get("inferenceConfig") or {}).get("maxTokens"),
                    purpose="ask")
    v.purpose = detect_purpose(v)
    v.lang = detect_lang(v)
    return v


LANG_RE = re.compile(r"(answer|write|respond|reply)\s+in\s+(hebrew|english)", re.I)


def detect_lang(v: RequestView) -> str:
    """The language the product asked for: an explicit "Answer/Write in Hebrew|English" (question wrapper or
    system prompt, the last one wins), else >= 30 % Hebrew letters in the question."""
    found = LANG_RE.findall(v.system_text + "\n" + v.question)
    if found:
        return "he" if found[-1][1].lower() == "hebrew" else "en"
    letters = [c for c in v.question if c.isalpha()]
    heb = sum(1 for c in letters if "\u0590" <= c <= "\u05ff")
    return "he" if letters and heb / len(letters) >= 0.3 else "en"


def detect_purpose(v: RequestView) -> str:
    m = MARKER_RE.search(v.system_text)
    if m and m.group(1).lower() in PURPOSES:
        return m.group(1).lower()
    if v.forced_tool in FORCED_PURPOSE:
        return FORCED_PURPOSE[v.forced_tool]
    for rx, p in KEYWORDS:
        if re.search(rx, v.system_text, re.I | re.S):
            return p
    return "ask"


def context(v: RequestView) -> dict[str, Any]:
    tools: dict[str, Any] = {}
    for name, res in v.exchange_results:
        tools[name] = res
    inp: Any = MISSING
    try:
        inp = json.loads(v.question)
    except ValueError:
        mm = re.search(r"(\{.*\}|\[.*\])", v.question, re.S)
        if mm:
            try:
                inp = json.loads(mm.group(1))
            except ValueError:
                inp = MISSING
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    ago = lambda d: (now - _dt.timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return {"now": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "hour_ago": (now - _dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "day_ago": ago(1), "week_ago": ago(7), "month_ago": ago(30),
            "last": v.results[0] if v.results else MISSING, "results": v.results,
            "all": [r for _, r in v.exchange_results], "tools": tools, "question": v.question,
            "input": inp, "system": v.system_text, "offered": v.offered, "purpose": v.purpose}


# ------------------------------------------------------------------------------------------------ validation
TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def validate(body: dict[str, Any]) -> str | None:
    """The subset of Bedrock's Converse validation that catches client bugs (exact AWS wording where known)."""
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return "The model returned the following errors: messages: at least one message is required"
    tc = body.get("toolConfig")
    if tc is not None:
        tools = tc.get("tools")
        if not isinstance(tools, list) or not [t for t in tools if "toolSpec" in t]:
            return "The value at toolConfig.tools must contain at least one toolSpec."
        names = []
        for i, t in enumerate(tools):
            if "toolSpec" in t:
                ts = t["toolSpec"]
                if not TOOL_NAME_RE.match(str(ts.get("name", ""))):
                    return f"1 validation error detected: Value at 'toolConfig.tools.{i + 1}.member.toolSpec.name' failed to satisfy constraint: Member must satisfy regular expression pattern: ^[a-zA-Z0-9_-]{{1,64}}$"
                if not isinstance((ts.get("inputSchema") or {}).get("json"), dict):
                    return f"The value at toolConfig.tools.{i}.toolSpec.inputSchema.json is required."
                names.append(ts["name"])
            elif "cachePoint" not in t:
                return f"The value at toolConfig.tools.{i} must be a toolSpec or cachePoint."
        if len(set(names)) != len(names):
            return "The toolConfig contains duplicate tool names."
        ch = tc.get("toolChoice")
        if isinstance(ch, dict) and "tool" in ch and ch["tool"].get("name") not in names:
            return f"The tool {ch['tool'].get('name')} specified in toolChoice is not defined in toolConfig.tools."
    if msgs[0].get("role") != "user":
        return "A conversation must start with a user message. Try again with a conversation that starts with a user message."
    prev_role = None
    prev_tool_ids: list[str] = []
    for i, m in enumerate(msgs):
        role = m.get("role")
        if role not in ("user", "assistant"):
            return f"messages.{i}.role must be user or assistant"
        if role == prev_role:
            return "A conversation must alternate between user and assistant roles. Make sure the conversation alternates between user and assistant roles and try again."
        content = m.get("content")
        if not isinstance(content, list) or not content:
            return f"The content field in the Message object at messages.{i} is empty. Add a ContentBlock object to the content field and try again."
        results = []
        for j, b in enumerate(content):
            if "text" in b and not str(b["text"]).strip():
                return f"The text field in the ContentBlock object at messages.{i}.content.{j} is blank. Add text to the text field, and try again."
            if ("toolUse" in b or "toolResult" in b) and tc is None:
                return "The toolConfig field must be defined when using toolUse and toolResult content blocks."
            if "toolResult" in b:
                if role != "user":
                    return f"toolResult blocks are only allowed in user messages (messages.{i}.content.{j})"
                tr = b["toolResult"]
                results.append(tr.get("toolUseId"))
                if tr.get("status") not in (None, "success", "error"):
                    return f"messages.{i}.content.{j}.toolResult.status must be success or error"
                for c in tr.get("content") or []:
                    if "json" in c and not isinstance(c["json"], (dict, list)):
                        return f"messages.{i}.content.{j}.toolResult.content.json must be a JSON object"
            if "toolUse" in b and role != "assistant":
                return f"toolUse blocks are only allowed in assistant messages (messages.{i}.content.{j})"
        if role == "user" and prev_tool_ids:
            if len(results) > len(prev_tool_ids):
                return f"The number of toolResult blocks at messages.{i}.content exceeds the number of toolUse blocks of previous turn."
            missing = [t for t in prev_tool_ids if t not in results]
            if missing:
                return f"Expected toolResult blocks at messages.{i}.content for the following Ids: {', '.join(missing)}"
        elif role == "user" and results:
            return f"messages.{i}.content contains toolResult blocks without a preceding toolUse."
        prev_tool_ids = [b["toolUse"].get("toolUseId") for b in content if "toolUse" in b] if role == "assistant" else []
        prev_role = role
    if msgs[-1].get("role") != "user":
        return "A conversation must end with a user message. Try again with a conversation that ends with a user message."
    mt = (body.get("inferenceConfig") or {}).get("maxTokens")
    if mt is not None and (not isinstance(mt, int) or mt < 1):
        return "The maximum tokens you requested exceeds the model limit or is below 1."
    return None


# ------------------------------------------------------------------------------------------------ playing
@dataclass
class Reply:
    blocks: list[dict[str, Any]]          # [{"text": ...} | {"toolUse": {...}}]
    stop_reason: str
    usage: dict[str, int]
    scenario: str
    turn_index: int
    template_errors: list[str]
    stream: dict[str, Any]


def schema_sample(schema: dict[str, Any]) -> Any:
    """A minimal valid value for a JSON schema (forced tools without a scripted input)."""
    t = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        return {k: schema_sample(props[k]) for k in schema.get("required", []) if k in props}
    if t == "array":
        return [schema_sample(schema["items"])] * int(schema.get("minItems", 0)) if "items" in schema else []
    if t == "integer":
        return int(schema.get("minimum", 1))
    if t == "number":
        return float(schema.get("minimum", 1))
    if t == "boolean":
        return False
    if t == "string":
        return "mock"[: max(1, schema.get("maxLength", 4))] if not schema.get("minLength") else "m" * schema["minLength"]
    if "anyOf" in schema:
        return schema_sample(schema["anyOf"][0])
    if "oneOf" in schema:
        return schema_sample(schema["oneOf"][0])
    return None


def est_tokens(s: str) -> int:
    return max(1, math.ceil(len(s) / 4))


class Model:
    def __init__(self, scenarios: list[Scenario]):
        self.scenarios = scenarios
        self.runtime: list[Scenario] = []
        self.forced_name: str | None = None
        self.counter = 0

    def all(self) -> list[Scenario]:
        return self.runtime + self.scenarios

    def select(self, v: RequestView) -> Scenario | None:
        if self.forced_name:
            for s in self.all():
                if s.name == self.forced_name and (v.purpose in s.purposes or "*" in s.purposes):
                    return s
        cands = []
        for s in self.all():
            if v.purpose not in s.purposes and "*" not in s.purposes:
                continue
            if s.match and not s.match.search(v.question):
                continue
            if s.match_system and not s.match_system.search(v.system_text):
                continue
            if s.lang and s.lang != v.lang:
                continue
            if any(t not in v.offered for t in s.requires_tools):
                continue
            cands.append(s)
        if not cands:
            return None
        cands.sort(key=lambda s: (-s.priority, 0 if s.source == "runtime" else 1, s.order))
        return cands[0]

    def play(self, v: RequestView, req_no: int) -> Reply:
        s = self.select(v)
        errors: list[str] = []
        ctx = context(v)
        name = s.name if s else "(default)"
        stream = dict(s.stream) if s else {}
        turns = s.turns if s else []
        blocks: list[dict[str, Any]] = []
        stop = "end_turn"

        def tool_block(tu: dict[str, Any], k: int) -> dict[str, Any]:
            inp = render(tu.get("input") or {}, ctx, errors)
            return {"toolUse": {"toolUseId": tu.get("id") or f"tooluse_mock{req_no:04d}_{k}", "name": tu["name"],
                                "input": inp}}

        if v.forced_tool:
            spec = v.tool_specs.get(v.forced_tool) or {}
            tu = None
            for t in turns[v.turn_index:] + turns[: v.turn_index]:
                for x in t.get("tool_use") or []:
                    if x.get("name") == v.forced_tool:
                        tu = x
                        break
                if tu:
                    break
            if tu is None and s and v.forced_tool in s.forced:
                tu = {"name": v.forced_tool, "input": s.forced[v.forced_tool]}
            if tu is None:
                tu = {"name": v.forced_tool, "input": schema_sample((spec.get("inputSchema") or {}).get("json") or {})}
            blocks = [tool_block(tu, 0)]
            stop = "tool_use"
        elif v.is_final_call:
            text = (s.final if s and s.final else None) or "Final answer from the information gathered so far."
            blocks = [{"text": render(text, ctx, errors)}]
        else:
            # past the last turn: a `repeat: true` last turn plays again (limit tests), else the final text
            idx = v.turn_index
            turn = turns[idx] if idx < len(turns) else (turns[-1] if turns and turns[-1].get("repeat") else None)
            if turn is not None and turn.get("tool_use"):
                missing = [x["name"] for x in turn["tool_use"] if x["name"] not in v.offered]
                if missing and turn.get("if_not_offered") is not None:
                    turn = turn["if_not_offered"]
                elif not v.offered and not turn.get("force"):
                    turn = {"text": s.final if s and s.final else "(no tools offered)"}
            if turn is None:
                text = (s.final if s and s.final else None) or (
                    "I could not find that in the monitoring data." if s else
                    "This is the Bedrock mock: no scenario matched the purpose "
                    f"'{v.purpose}' and question {v.question[:80]!r}.")
                turn = {"text": text}
            if turn.get("text_before"):
                blocks.append({"text": render(turn["text_before"], ctx, errors)})
            if turn.get("tool_use"):
                blocks += [tool_block(x, k) for k, x in enumerate(turn["tool_use"])]
                stop = "tool_use"
            if turn.get("text") is not None:
                blocks.append({"text": render(turn["text"], ctx, errors)})
            if turn.get("raw"):
                blocks += render(turn["raw"], ctx, errors)
            if turn.get("stop_reason"):
                stop = turn["stop_reason"]
            stream.update(turn.get("stream") or {})
        # maxTokens: cut text like a real model
        out_text = "".join(b.get("text", "") for b in blocks)
        if v.max_tokens and est_tokens(out_text) > v.max_tokens and stop == "end_turn":
            budget = v.max_tokens * 4
            for b in blocks:
                if "text" in b:
                    b["text"] = b["text"][:budget]
                    budget = max(0, budget - len(b["text"]))
            blocks = [b for b in blocks if "text" not in b or b["text"]]
            stop = "max_tokens"
        usage = self.usage(v, blocks, s)
        return Reply(blocks=blocks, stop_reason=stop, usage=usage, scenario=name, turn_index=v.turn_index,
                     template_errors=errors, stream=stream)

    @staticmethod
    def usage(v: RequestView, blocks: list[dict[str, Any]], s: Scenario | None) -> dict[str, int]:
        if s and s.usage:
            u = {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}
            u.update({k: int(x) for k, x in s.usage.items()})
        else:
            prefix = est_tokens(json.dumps(v.body.get("system") or []) + json.dumps((v.body.get("toolConfig") or {}).get("tools") or []))
            rest = est_tokens(json.dumps(v.messages))
            out = est_tokens(json.dumps(blocks))
            if v.has_cache_point:
                first = v.turn_index == 0
                u = {"inputTokens": rest, "outputTokens": out,
                     "cacheReadInputTokens": 0 if first else prefix, "cacheWriteInputTokens": prefix if first else 0}
            else:
                u = {"inputTokens": rest + prefix, "outputTokens": out, "cacheReadInputTokens": 0,
                     "cacheWriteInputTokens": 0}
        u["totalTokens"] = u["inputTokens"] + u["outputTokens"] + u.get("cacheReadInputTokens", 0) + u.get("cacheWriteInputTokens", 0)
        return u
