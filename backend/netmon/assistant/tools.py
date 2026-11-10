"""[3b] The closed, read-only tool allow-list (architecture § 14.4, api.md § [3b] Tools).

There is no generic tool (no SQL, no HTTP, no "call any API"). A tool is a `ToolDef`:

- a pydantic args model (``extra="forbid"``) whose JSON schema is offered to the model,
- a feature gate (`gate(GateInfo) -> reason | None`; the model only sees tools whose gate is
  open at question time),
- an async handler that reads through a **read-only** DB transaction (``SET TRANSACTION READ
  ONLY`` + ``statement_timeout 8s``) and returns an explicit projection (`ToolOutput`).

`run_tool()` turns a model ``toolUse`` into the result envelope
``{"ok": true, "tool", "rows", "truncated", "data", "untrusted_fields", "note"}`` /
``{"ok": false, "tool", "error", "detail"}``: unknown/not-offered tool → ``unknown_tool`` (nothing
runs), invalid arguments or an unknown IP placeholder → ``invalid_arguments``, 10 s timeout →
``timeout``; the result is redacted (secrets always, internal IPs per policy) and capped to
16 KiB (rows dropped from the end, ``truncated: true``).

Handlers live in ``tools_core``, ``tools_logs``, ``tools_flows`` and ``tools_misc``; each module
calls `register()` at import. `ensure_loaded()` imports them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import text

from netmon.assistant import links, redact

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("netmon.assistant.tools")

TOOL_TIMEOUT_S = 10.0
STATEMENT_TIMEOUT = "8s"
RESULT_MAX_BYTES = 16 * 1024
NAME_CAP = 200
MESSAGE_CAP = 200
ERRORS = (
    "feature_disabled",
    "not_found",
    "invalid_arguments",
    "range_too_large",
    "timeout",
    "unknown_tool",
    "internal",
)


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolError(Exception):
    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(detail or code)
        self.code = code if code in ERRORS else "internal"
        self.detail = detail


@dataclass
class ToolOutput:
    data: dict[str, Any]
    rows: int
    truncated: bool = False
    note: str | None = None


@dataclass
class GateInfo:
    """What the gates look at (question time)."""

    features: Mapping[str, bool]
    assistant: Mapping[str, Any] = field(default_factory=dict)  # the `assistant` settings object
    settings: Mapping[str, Any] = field(default_factory=dict)  # scalar settings
    purpose: str = "ask"

    def on(self, name: str) -> bool:
        return bool(self.features.get(name, False))


@dataclass
class ToolCtx:
    rt: Any
    session: AsyncSession
    user_id: int
    username: str
    role: str
    now: datetime
    timezone: str
    purpose: str
    gate: GateInfo
    kb_sources: dict[str, str] = field(default_factory=dict)  # kb/<n> -> url (per message)


Handler = Callable[[ToolCtx, Any], Awaitable[ToolOutput]]
Gate = Callable[[GateInfo], str | None]


def no_gate(_: GateInfo) -> str | None:
    return None


def feature_gate(*names: str) -> Gate:
    def gate(g: GateInfo) -> str | None:
        for n in names:
            if not g.on(n):
                return f"feature_off:{n}"
        return None

    return gate


def config_gate(g: GateInfo) -> str | None:
    """Config tools: `config_backup` ON, `send_config_excerpts` ON and masking ON."""
    if not g.on("config_backup"):
        return "feature_off:config_backup"
    if not g.assistant.get("send_config_excerpts"):
        return "setting_off:send_config_excerpts"
    if not g.settings.get("config_backup_mask_secrets", True):
        return "setting_off:config_backup_mask_secrets"
    return None


def purpose_gate(*purposes: str) -> Gate:
    def gate(g: GateInfo) -> str | None:
        return None if g.purpose in purposes else f"purpose_only:{','.join(purposes)}"

    return gate


@dataclass
class ToolDef:
    name: str
    description: str
    args: type[Args]
    handler: Handler
    gate: Gate = no_gate
    untrusted: tuple[str, ...] = ()
    source_key: str | None = None  # data-source key for the prompt's "Available data" line
    _schema: dict[str, Any] | None = None

    def schema(self) -> dict[str, Any]:
        if self._schema is None:
            self._schema = clean_schema(self.args.model_json_schema())
        return self._schema

    def spec(self) -> dict[str, Any]:
        return {
            "toolSpec": {
                "name": self.name,
                "description": self.description,
                "inputSchema": {"json": self.schema()},
            }
        }


REGISTRY: dict[str, ToolDef] = {}
# api.md order (the order the tools are offered in; stable → prompt caching)
ORDER = (
    "search_devices",
    "get_device",
    "list_problems",
    "search_sensors",
    "get_sensor_history",
    "list_events",
    "list_maintenance",
    "topology_neighbors",
    "topology_path",
    "get_device_ports",
    "find_mac_or_ip",
    "availability",
    "list_groups",
    "get_timeline",
    "search_syslog",
    "syslog_stats",
    "search_traps",
    "flow_top",
    "flow_interface_series",
    "list_new_devices",
    "config_versions",
    "config_diff",
    "config_search",
    "list_notification_channels",
    "search_knowledge",
)


def register(tool: ToolDef) -> ToolDef:
    if tool.name not in ORDER:
        raise ValueError(f"tool {tool.name} is not in the api.md allow-list")
    REGISTRY[tool.name] = tool
    return tool


_loaded = False


def ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    from netmon.assistant import tools_core, tools_flows, tools_logs, tools_misc  # noqa: F401

    _loaded = True


def all_tools() -> list[ToolDef]:
    ensure_loaded()
    return [REGISTRY[n] for n in ORDER if n in REGISTRY]


# --------------------------------------------------------------------------- JSON schema
def clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """pydantic JSON schema → compact Bedrock tool schema: inline ``$defs``, no titles,
    ``Optional[X]`` → X, every object ``additionalProperties: false``."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = defs.get(node["$ref"].rsplit("/", 1)[-1], {})
            merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return walk(merged)
        out: dict[str, Any] = {}
        any_of = node.get("anyOf")
        if isinstance(any_of, list):
            non_null = [x for x in any_of if not (isinstance(x, dict) and x.get("type") == "null")]
            if len(non_null) == 1:
                base = walk(non_null[0])
                rest = {
                    k: walk(v) for k, v in node.items() if k not in ("anyOf", "title", "default")
                }
                if node.get("default") is not None:
                    rest["default"] = node["default"]
                return {**base, **rest}
        for k, v in node.items():
            if k in ("title", "$defs"):
                continue
            if k == "default" and v is None:
                continue
            if k == "properties" and isinstance(v, dict):
                out[k] = {pk: walk(pv) for pk, pv in v.items()}
            else:
                out[k] = walk(v)
        if out.get("type") == "object":
            out["additionalProperties"] = False
            out.setdefault("properties", {})
        return out

    return walk(schema)


# --------------------------------------------------------------------------- availability
def gate_reason(tool: ToolDef, g: GateInfo) -> str | None:
    try:
        return tool.gate(g)
    except Exception:  # noqa: BLE001
        log.exception("Tool gate %s failed", tool.name)
        return "internal"


def available(g: GateInfo, subset: Iterable[str] | None = None) -> list[ToolDef]:
    """Tools offered now (gates open), optionally limited to `subset` (names), api.md order."""
    allow = set(subset) if subset is not None else None
    return [
        t for t in all_tools() if (allow is None or t.name in allow) and gate_reason(t, g) is None
    ]


def status(g: GateInfo) -> list[dict[str, Any]]:
    """``GET /api/assistant/status`` ``tools``: name, available, reason."""
    out = []
    for t in all_tools():
        reason = gate_reason(t, g)
        out.append({"name": t.name, "available": reason is None, "reason": reason})
    return out


def sources_on(g: GateInfo) -> list[str]:
    """Optional data-source keys for the system prompt (item 7)."""
    out = []
    for key, feat in (
        ("syslog", "syslog"),
        ("traps", "traps"),
        ("timeline", "timeline"),
        ("flows", "flows"),
        ("new_device_alerts", "new_device_alerts"),
    ):
        if g.on(feat):
            out.append(key)
    if config_gate(g) is None:
        out.append("config")
    if g.on("ai_knowledge") and g.on("assistant"):
        out.append("knowledge")
    return out


# --------------------------------------------------------------------------- helpers for handlers
def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None, field_name: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ToolError("invalid_arguments", f"{field_name}: not an ISO-8601 time") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def time_range(
    since: str | None,
    until: str | None,
    now: datetime,
    *,
    max_days: float,
    default: timedelta = timedelta(hours=24),
) -> tuple[datetime, datetime]:
    u = parse_time(until, "until") or now
    s = parse_time(since, "since") or (u - default)
    if s > u:
        raise ToolError("invalid_arguments", "since must be before until")
    if u - s > timedelta(days=max_days):
        raise ToolError("range_too_large", f"the range is longer than {max_days:g} days")
    return s, u


def cap(value: Any, n: int = NAME_CAP) -> Any:
    if isinstance(value, str) and len(value) > n:
        return value[: n - 1] + "…"
    return value


async def read_only(session: AsyncSession) -> None:
    await session.execute(text("SET TRANSACTION READ ONLY"))
    await session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))


async def require_device(session: AsyncSession, device_id: int) -> Any:
    row = (
        await session.execute(
            text("SELECT id, name, group_id FROM devices WHERE id = :id"), {"id": device_id}
        )
    ).first()
    if row is None:
        raise ToolError("not_found", f"device {device_id} not found")
    return row


async def group_scope(session: AsyncSession, group_id: int) -> list[int]:
    """Device ids of a group and its descendants (not_found when the group does not exist)."""
    rows = (
        await session.execute(
            text(
                "WITH RECURSIVE g AS (SELECT id FROM groups WHERE id = :gid UNION "
                "SELECT c.id FROM groups c JOIN g ON c.parent_id = g.id) "
                "SELECT d.id FROM devices d WHERE d.group_id IN (SELECT id FROM g)"
            ),
            {"gid": group_id},
        )
    ).all()
    if not rows:
        exists = (
            await session.execute(text("SELECT 1 FROM groups WHERE id = :g"), {"g": group_id})
        ).first()
        if exists is None:
            raise ToolError("not_found", f"group {group_id} not found")
    return [int(r[0]) for r in rows]


# --------------------------------------------------------------------------- envelope, caps
def _size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode())


def _largest_list(data: Any) -> list[Any] | None:
    best: list[Any] | None = None
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and v and (best is None or len(v) > len(best)):
                best = v
        if best is None:
            for v in data.values():
                inner = _largest_list(v) if isinstance(v, dict) else None
                if inner is not None and (best is None or len(inner) > len(best)):
                    best = inner
    return best


def cap_envelope(env: dict[str, Any], max_bytes: int = RESULT_MAX_BYTES) -> dict[str, Any]:
    """Drop rows from the end of the largest list until the JSON fits; mark truncated."""
    if _size(env) <= max_bytes:
        return env
    data = env.get("data")
    guard = 0
    while _size(env) > max_bytes and guard < 100000:
        guard += 1
        lst = _largest_list(data)
        if not lst:
            break
        # drop in chunks for big lists
        drop = max(1, len(lst) // 20) if _size(env) > 2 * max_bytes else 1
        del lst[-drop:]
        env["truncated"] = True
    if _size(env) > max_bytes:
        # last resort: shorten strings
        env["data"] = _shorten(env.get("data"), 200)
        env["truncated"] = True
        if _size(env) > max_bytes:
            env["data"] = {"omitted": True, "reason": "too_large"}
    return env


def _shorten(value: Any, n: int) -> Any:
    if isinstance(value, str):
        return cap(value, n)
    if isinstance(value, dict):
        return {k: _shorten(v, n) for k, v in value.items()}
    if isinstance(value, list):
        return [_shorten(v, n) for v in value]
    return value


def ok_envelope(tool: ToolDef, out: ToolOutput) -> dict[str, Any]:
    return {
        "ok": True,
        "tool": tool.name,
        "rows": int(out.rows),
        "truncated": bool(out.truncated),
        "data": out.data,
        "untrusted_fields": list(tool.untrusted),
        "note": out.note,
    }


def error_envelope(name: str, code: str, detail: str | None = None) -> dict[str, Any]:
    return {"ok": False, "tool": name, "error": code, "detail": detail}


@dataclass
class ToolRun:
    """Outcome of one tool call (what the agent sends, stores and audits)."""

    name: str
    envelope: dict[str, Any]
    ok: bool
    error: str | None
    rows: int | None
    truncated: bool
    result_bytes: int
    duration_ms: int
    refs: list[str]
    labels: dict[str, str]
    args: dict[str, Any]  # as validated, in the model's form (placeholders kept)


async def run_tool(
    rt: Any,
    *,
    name: str,
    raw_args: Any,
    offered: Sequence[str],
    user_id: int,
    username: str,
    role: str,
    gate: GateInfo,
    policy: redact.Policy,
    now: datetime,
    timezone: str,
    kb_sources: dict[str, str] | None = None,
    timeout_s: float = TOOL_TIMEOUT_S,
) -> ToolRun:
    t0 = time.monotonic()

    def done(env: dict[str, Any], args: dict[str, Any]) -> ToolRun:
        env = cap_envelope(env)
        data = env.get("data")
        refs = links.extract_refs(data) if env.get("ok") else []
        return ToolRun(
            name=name,
            envelope=env,
            ok=bool(env.get("ok")),
            error=env.get("error"),
            rows=env.get("rows"),
            truncated=bool(env.get("truncated")),
            result_bytes=_size(env),
            duration_ms=int((time.monotonic() - t0) * 1000),
            refs=refs,
            labels=links.labels_of(data) if env.get("ok") else {},
            args=args,
        )

    ensure_loaded()
    safe_args = raw_args if isinstance(raw_args, dict) else {}
    tool = REGISTRY.get(name) if name in offered else None
    if tool is None:
        return done(error_envelope(name, "unknown_tool", "this tool is not available"), safe_args)
    reason = gate_reason(tool, gate)
    if reason is not None:
        return done(error_envelope(name, "feature_disabled", reason), safe_args)
    try:
        args = tool.args.model_validate(safe_args if isinstance(raw_args, dict) else raw_args)
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg', '')}"
            for e in exc.errors()[:5]
        )
        return done(error_envelope(name, "invalid_arguments", detail[:500]), safe_args)
    try:
        real = tool.args.model_validate(
            redact.translate_args(args.model_dump(), policy.ipmap if policy.redact_ips else None)
        )
    except redact.UnknownPlaceholder as exc:
        return done(error_envelope(name, "invalid_arguments", str(exc)), safe_args)
    except ValidationError as exc:
        return done(error_envelope(name, "invalid_arguments", str(exc)[:300]), safe_args)

    async def call() -> ToolOutput:
        async with rt.sessionmaker() as session:
            try:
                await read_only(session)
                ctx = ToolCtx(
                    rt=rt,
                    session=session,
                    user_id=user_id,
                    username=username,
                    role=role,
                    now=now,
                    timezone=timezone,
                    purpose=gate.purpose,
                    gate=gate,
                    kb_sources=kb_sources if kb_sources is not None else {},
                )
                return await tool.handler(ctx, real)
            finally:
                await session.rollback()

    try:
        out = await asyncio.wait_for(call(), timeout_s)
    except TimeoutError:
        return done(
            error_envelope(name, "timeout", f"the tool took longer than {timeout_s:g} s"),
            args.model_dump(),
        )
    except ToolError as exc:
        return done(error_envelope(name, exc.code, exc.detail), args.model_dump())
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        if "statement timeout" in str(exc) or "canceling statement" in str(exc):
            return done(
                error_envelope(name, "timeout", "the query took too long"), args.model_dump()
            )
        log.exception("Tool %s failed", name)
        return done(error_envelope(name, "internal", type(exc).__name__), args.model_dump())
    env = ok_envelope(tool, out)
    env["data"] = redact.apply(env["data"], policy)
    return done(env, args.model_dump(exclude_none=True))


def summarize_args(args: Mapping[str, Any], max_bytes: int = 2048) -> dict[str, Any]:
    """Audit form of tool arguments: secret patterns redacted, ≤ 2 KiB."""
    safe = redact.apply(dict(args))
    raw = json.dumps(safe, ensure_ascii=False, default=str)
    if len(raw.encode()) <= max_bytes:
        return safe
    return {"_truncated": raw.encode()[: max_bytes - 64].decode(errors="ignore")}
