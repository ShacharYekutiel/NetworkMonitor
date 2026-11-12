"""[2b-3] Event rules: pure matching and evaluation (data-model "Rule evaluation"), plus the
validation used by the API (422 details)."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from netmon.ingest import oid_names

IF_INDEX_OID = oid_names.IF_INDEX_COLUMN
MAX_KEY = 128
MAX_REGEX = 200
MAX_TEXT_SCAN = 8192
OID_RE = re.compile(r"^[0-2](\.\d+){1,127}$")
VARBIND_OPS = ("eq", "ne", "contains", "regex", "exists", "absent")


@dataclass
class Rule:
    id: int
    name: str
    kind: str  # trap | syslog
    priority: int
    scope: str
    group_id: int | None
    device_id: int | None
    match: dict[str, Any]
    action: str  # warning | down | ignore | [3] config_backup
    key_varbind_oid: str | None = None
    key_regex: str | None = None
    clear_match: dict[str, Any] | None = None
    clear_after_min: int | None = None
    enabled: bool = True
    builtin_key: str | None = None  # [1.3] (LAG degraded follows the built-in link-down rule)
    _compiled: dict[str, re.Pattern[str] | None] = field(default_factory=dict, repr=False)

    def regex(self, pattern: str | None) -> re.Pattern[str] | None:
        if not pattern:
            return None
        if pattern not in self._compiled:
            try:
                self._compiled[pattern] = re.compile(pattern, re.IGNORECASE)
            except re.error:
                self._compiled[pattern] = None
        return self._compiled[pattern]

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> Rule:
        return cls(
            id=int(r["id"]),
            name=str(r["name"]),
            kind=str(r["kind"]),
            priority=int(r["priority"]),
            scope=str(r["scope"]),
            group_id=r.get("group_id"),
            device_id=r.get("device_id"),
            match=dict(r["match"] or {}),
            action=str(r["action"]),
            key_varbind_oid=r.get("key_varbind_oid"),
            key_regex=r.get("key_regex"),
            clear_match=dict(r["clear_match"]) if r.get("clear_match") else None,
            clear_after_min=r.get("clear_after_min"),
            enabled=bool(r.get("enabled", True)),
            builtin_key=r.get("builtin_key"),
        )


class Context:
    """Per-record context: the important-port predicate and the flapping ports of the
    record's device."""

    def __init__(
        self,
        important: Callable[[int], bool] | None = None,
        flapping: Callable[[int], bool] | None = None,
    ) -> None:
        self._important = important
        self._flapping = flapping

    def important(self, if_index: int) -> bool:
        return True if self._important is None else self._important(if_index)

    def flapping(self, if_index: int) -> bool:
        return False if self._flapping is None else self._flapping(if_index)


@dataclass
class Outcome:
    clears: list[tuple[Rule, str]] = field(default_factory=list)
    raised: tuple[Rule, str] | None = None
    ignored: Rule | None = None
    suppressed: tuple[Rule, str] | None = None  # raise suppressed because the port flaps
    matched_rule_ids: list[int] = field(default_factory=list)


# ------------------------------------------------------------------ matching
def _first_varbind(varbinds: list[dict[str, Any]], oid: str) -> dict[str, Any] | None:
    for vb in varbinds:
        if oid_names.under(str(vb.get("oid", "")), oid):
            return vb
    return None


def _oid_prefix(trap_oid: str, prefix: str) -> bool:
    return trap_oid == prefix or trap_oid.startswith(prefix + ".")


def _if_index(varbinds: list[dict[str, Any]]) -> int | None:
    vb = _first_varbind(varbinds, IF_INDEX_OID)
    if vb is None:
        return None
    try:
        return int(str(vb.get("value")))
    except ValueError:
        return None


def varbind_condition(cond: Mapping[str, Any], varbinds: list[dict[str, Any]], rule: Rule) -> bool:
    vb = _first_varbind(varbinds, str(cond.get("oid") or ""))
    op = cond.get("op")
    if vb is None:
        return op in ("ne", "absent")
    value = str(vb.get("value") or "")
    want = cond.get("value")
    want_s = "" if want is None else str(want)
    if op == "eq":
        return value == want_s
    if op == "ne":
        return value != want_s
    if op == "contains":
        return want_s.casefold() in value.casefold()
    if op == "regex":
        rx = rule.regex(want_s)
        return bool(rx and rx.search(value[:MAX_TEXT_SCAN]))
    if op == "exists":
        return True
    return False  # absent, unknown op


def trap_matches(
    match: Mapping[str, Any], rec: Mapping[str, Any], ctx: Context, rule: Rule
) -> bool:
    trap_oid = str(rec.get("trap_oid") or "")
    oids = list(match.get("trap_oids") or [])
    if oids and not any(_oid_prefix(trap_oid, str(p)) for p in oids):
        return False
    varbinds = list(rec.get("varbinds") or [])
    for cond in match.get("varbinds") or []:
        if not varbind_condition(cond, varbinds, rule):
            return False
    if match.get("ports") == "important":
        idx = _if_index(varbinds)
        if idx is not None and not ctx.important(idx):
            return False
    return True


def syslog_matches(match: Mapping[str, Any], rec: Mapping[str, Any], rule: Rule) -> bool:
    sev_max = match.get("severity_max")
    if sev_max is not None and int(rec.get("severity", 7)) > int(sev_max):
        return False
    facilities = list(match.get("facilities") or [])
    if facilities and int(rec.get("facility", -1)) not in facilities:
        return False
    programs = [str(p).casefold() for p in match.get("programs") or []]
    if programs and str(rec.get("program") or "").casefold() not in programs:
        return False
    text = match.get("text")
    if text:
        message = str(rec.get("message") or "")[:MAX_TEXT_SCAN]
        if match.get("text_regex"):
            rx = rule.regex(str(text))
            if rx is None or rx.search(message) is None:
                return False
        elif str(text).casefold() not in message.casefold():
            return False
    return True


def matches(
    match: Mapping[str, Any] | None, kind: str, rec: Mapping[str, Any], ctx: Context, rule: Rule
) -> bool:
    if match is None:
        return False
    if kind == "trap":
        return trap_matches(match, rec, ctx, rule)
    return syslog_matches(match, rec, rule)


def rule_key(rule: Rule, rec: Mapping[str, Any]) -> str:
    if rule.kind == "trap":
        if not rule.key_varbind_oid:
            return ""
        vb = _first_varbind(list(rec.get("varbinds") or []), rule.key_varbind_oid)
        return str(vb.get("value") or "")[:MAX_KEY] if vb is not None else ""
    if not rule.key_regex:
        return ""
    rx = rule.regex(rule.key_regex)
    if rx is None:
        return ""
    m = rx.search(str(rec.get("message") or "")[:MAX_TEXT_SCAN])
    if m is None or not m.groups():
        return ""
    return (m.group(1) or "")[:MAX_KEY]


def evaluate(rec: Mapping[str, Any], rules: list[Rule], ctx: Context) -> Outcome:
    """`rules`: the enabled rules of the record's kind in scope for its device, sorted by
    (priority, id). [3] `config_backup` rules never take part (see `backup_triggers`)."""
    out = Outcome()
    kind = "trap" if "trap_oid" in rec else "syslog"
    rules = [r for r in rules if r.action != BACKUP_ACTION]
    for rule in rules:
        if rule.clear_match is not None and rule.action != "ignore":
            if matches(rule.clear_match, kind, rec, ctx, rule):
                out.clears.append((rule, rule_key(rule, rec)))
                out.matched_rule_ids.append(rule.id)
    for rule in rules:
        if not matches(rule.match, kind, rec, ctx, rule):
            continue
        if rule.action == "ignore":
            out.ignored = rule
        else:
            key = rule_key(rule, rec)
            idx = _if_index(list(rec.get("varbinds") or [])) if kind == "trap" else None
            if (
                kind == "trap"
                and rule.key_varbind_oid == IF_INDEX_OID
                and idx is not None
                and ctx.flapping(idx)
            ):
                out.suppressed = (rule, key)
            else:
                out.raised = (rule, key)
        if rule.id not in out.matched_rule_ids:
            out.matched_rule_ids.append(rule.id)
        break
    return out


BACKUP_ACTION = "config_backup"  # [3]


def backup_triggers(rec: Mapping[str, Any], rules: list[Rule]) -> list[int]:
    """[3] Ids of the enabled `config_backup` rules (in scope, as `evaluate`) whose `match`
    matches the record; non-empty -> queue a config backup of the device."""
    kind = "trap" if "trap_oid" in rec else "syslog"
    ctx = Context()
    return [
        r.id
        for r in rules
        if r.action == BACKUP_ACTION and r.enabled and matches(r.match, kind, rec, ctx, r)
    ]


def in_scope(rule: Rule, device_id: int, group_chain: list[int] | set[int]) -> bool:
    """`group_chain`: the device's group and its ancestors."""
    if rule.scope == "global":
        return True
    if rule.scope == "device":
        return rule.device_id == device_id
    return rule.group_id in group_chain


def sort_rules(rules: list[Rule]) -> list[Rule]:
    return sorted(rules, key=lambda r: (r.priority, r.id))


# ------------------------------------------------------------------ validation
def _err(errors: list[dict[str, Any]], loc: list[str | int], msg: str) -> None:
    errors.append({"loc": ["body", *loc], "msg": msg})


def check_regex(pattern: str, loc: list[str | int], errors: list[dict[str, Any]]) -> None:
    if len(pattern) > MAX_REGEX:
        _err(errors, loc, f"at most {MAX_REGEX} characters")
        return
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        _err(errors, loc, f"invalid regular expression: {exc}")


def check_oid(value: Any, loc: list[str | int], errors: list[dict[str, Any]]) -> str | None:
    text = str(value or "").strip().lstrip(".")
    if not OID_RE.match(text):
        _err(errors, loc, "must be a numeric dotted OID")
        return None
    return text


def validate_match(
    kind: str, match: Any, loc: list[str | int], errors: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not isinstance(match, dict):
        _err(errors, loc, "must be an object")
        return None
    if kind == "trap":
        allowed = {"trap_oids", "varbinds", "ports"}
        for k in match:
            if k not in allowed:
                _err(errors, [*loc, k], "unknown key")
        oids = match.get("trap_oids") or []
        if not isinstance(oids, list) or len(oids) > 32:
            _err(errors, [*loc, "trap_oids"], "must be a list of at most 32 OIDs")
            oids = []
        norm_oids = [check_oid(o, [*loc, "trap_oids", i], errors) for i, o in enumerate(oids)]
        vbs = match.get("varbinds") or []
        if not isinstance(vbs, list) or len(vbs) > 8:
            _err(errors, [*loc, "varbinds"], "must be a list of at most 8 conditions")
            vbs = []
        norm_vbs = []
        for i, c in enumerate(vbs):
            cl = [*loc, "varbinds", i]
            if not isinstance(c, dict):
                _err(errors, cl, "must be an object")
                continue
            for k in c:
                if k not in ("oid", "op", "value"):
                    _err(errors, [*cl, k], "unknown key")
            oid = check_oid(c.get("oid"), [*cl, "oid"], errors)
            op = c.get("op")
            if op not in VARBIND_OPS:
                _err(errors, [*cl, "op"], f"must be one of {list(VARBIND_OPS)}")
            value = c.get("value")
            if value is not None and not isinstance(value, str):
                value = str(value)
            if op in ("eq", "ne", "contains", "regex") and value is None:
                _err(errors, [*cl, "value"], "required for this operator")
            if op == "regex" and value is not None:
                check_regex(value, [*cl, "value"], errors)
            if value is not None and len(value) > 1024:
                _err(errors, [*cl, "value"], "at most 1024 characters")
            norm_vbs.append({"oid": oid, "op": op, "value": value})
        ports = match.get("ports", "any")
        if ports not in ("any", "important"):
            _err(errors, [*loc, "ports"], "must be any or important")
        return {"trap_oids": [o for o in norm_oids if o], "varbinds": norm_vbs, "ports": ports}
    allowed = {"severity_max", "facilities", "programs", "text", "text_regex"}
    for k in match:
        if k not in allowed:
            _err(errors, [*loc, k], "unknown key")
    sev = match.get("severity_max")
    if sev is not None and (not isinstance(sev, int) or isinstance(sev, bool) or not 0 <= sev <= 7):
        _err(errors, [*loc, "severity_max"], "must be 0-7 or null")
    facs = match.get("facilities") or []
    if not isinstance(facs, list) or any(
        not isinstance(f, int) or isinstance(f, bool) or not 0 <= f <= 23 for f in facs
    ):
        _err(errors, [*loc, "facilities"], "must be a list of facilities 0-23")
        facs = []
    progs = match.get("programs") or []
    if (
        not isinstance(progs, list)
        or len(progs) > 16
        or any(not isinstance(p, str) or not p or len(p) > 128 for p in progs)
    ):
        _err(errors, [*loc, "programs"], "must be a list of at most 16 program names")
        progs = []
    text = match.get("text")
    text_regex = bool(match.get("text_regex", False))
    if text is not None:
        if not isinstance(text, str):
            _err(errors, [*loc, "text"], "must be a string")
            text = None
        elif text == "":
            text = None
        elif text_regex:
            check_regex(text, [*loc, "text"], errors)
        elif len(text) > 1024:
            _err(errors, [*loc, "text"], "at most 1024 characters")
    return {
        "severity_max": sev,
        "facilities": sorted(set(facs)),
        "programs": list(progs),
        "text": text,
        "text_regex": text_regex,
    }


def validate_rule(data: dict[str, Any]) -> dict[str, Any]:
    """Validate a complete rule (writable fields, already merged for a PATCH). Returns the
    normalized fields or raises ApiError(422)."""
    from netmon.api.errors import ApiError

    errors: list[dict[str, Any]] = []
    kind = data.get("kind")
    if kind not in ("trap", "syslog"):
        _err(errors, ["kind"], "must be trap or syslog")
        raise ApiError(422, "validation_error", "Invalid event rule", errors)
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 128:
        _err(errors, ["name"], "1-128 characters")
    priority = data.get("priority", 100)
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100000:
        _err(errors, ["priority"], "must be 0-100000")
    scope = data.get("scope", "global")
    group_id, device_id = data.get("group_id"), data.get("device_id")
    if scope not in ("global", "group", "device"):
        _err(errors, ["scope"], "must be global, group or device")
    if scope == "group" and group_id is None:
        _err(errors, ["group_id"], "required for scope group")
    if scope == "device" and device_id is None:
        _err(errors, ["device_id"], "required for scope device")
    if scope != "group":
        group_id = None
    if scope != "device":
        device_id = None
    action = data.get("action")
    if action not in ("warning", "down", "ignore", BACKUP_ACTION):
        _err(errors, ["action"], "must be warning, down, ignore or config_backup")
    match = validate_match(kind, data.get("match"), ["match"], errors)
    key_oid = data.get("key_varbind_oid") or None
    key_regex = data.get("key_regex") or None
    clear_match_in = data.get("clear_match")
    clear_after = data.get("clear_after_min")
    clear_match = None
    if action in ("ignore", BACKUP_ACTION):
        what = "an ignore" if action == "ignore" else "a config_backup"
        if key_oid or key_regex:
            _err(errors, ["action"], f"{what} rule has no key")
        if clear_match_in is not None or clear_after is not None:
            _err(errors, ["action"], f"{what} rule has no clearing")
    else:
        if clear_match_in is None and clear_after is None:
            _err(
                errors,
                ["clear_after_min"],
                "a warning/down rule needs clear_match or clear_after_min",
            )
        if clear_match_in is not None:
            clear_match = validate_match(kind, clear_match_in, ["clear_match"], errors)
        if clear_after is not None and (
            not isinstance(clear_after, int)
            or isinstance(clear_after, bool)
            or not 1 <= clear_after <= 10080
        ):
            _err(errors, ["clear_after_min"], "must be 1-10080")
    if kind == "trap":
        if key_regex:
            _err(errors, ["key_regex"], "only for syslog rules")
        if key_oid:
            key_oid = check_oid(key_oid, ["key_varbind_oid"], errors)
    else:
        if key_oid:
            _err(errors, ["key_varbind_oid"], "only for trap rules")
        if key_regex:
            check_regex(key_regex, ["key_regex"], errors)
            rx = None
            try:
                rx = re.compile(key_regex)
            except re.error:
                pass
            if rx is not None and rx.groups != 1:
                _err(errors, ["key_regex"], "must have exactly one capture group")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        _err(errors, ["enabled"], "must be a boolean")
    if errors:
        raise ApiError(422, "validation_error", "Invalid event rule", errors)
    return {
        "name": name,
        "kind": kind,
        "enabled": enabled,
        "priority": priority,
        "scope": scope,
        "group_id": group_id,
        "device_id": device_id,
        "match": match,
        "action": action,
        "key_varbind_oid": key_oid if kind == "trap" else None,
        "key_regex": key_regex if kind == "syslog" else None,
        "clear_match": clear_match,
        "clear_after_min": clear_after if action not in ("ignore", BACKUP_ACTION) else None,
    }
