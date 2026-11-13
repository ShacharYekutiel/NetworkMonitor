"""[3b] Pure parts of template drafts (§ 14.8.7): walk subtrees (presets, forbidden subtrees),
`compact_walk` (what the model sees) and draft post-processing."""

from __future__ import annotations

import json
import re
from typing import Any

MAX_VARBINDS = 5000
MAX_PER_SUBTREE = 2000
MAX_SUBTREES = 8
MAX_CUSTOM = 8
# [maint-1.4] walk budgets: each subtree has its own budget, so a huge vendor subtree (e.g.
# hh3c) can no longer starve the standard ones; the walk as a whole stays capped. Standard
# (mib-2) subtrees are walked first.
SUBTREE_TIMEOUT_S = 20.0  # a standard subtree (1.3.6.1.2.1.*)
VENDOR_SUBTREE_TIMEOUT_S = 45.0  # anything else (the enterprise subtree, vendor OIDs)
WALK_TIMEOUT_S = 90.0  # overall cap
COMPACT_MAX_BYTES = 32 * 1024
VALUE_MAX = 64
STORED_VALUE_MAX = 256

PRESETS: dict[str, list[str]] = {
    "system": ["1.3.6.1.2.1.1"],
    "entity": ["1.3.6.1.2.1.47.1.1.1"],
    "host_resources": ["1.3.6.1.2.1.25.1", "1.3.6.1.2.1.25.2", "1.3.6.1.2.1.25.3"],
    "enterprise": [],  # resolved from sysObjectID: 1.3.6.1.4.1.<PEN>
    "if_sample": ["1.3.6.1.2.1.2.2", "1.3.6.1.2.1.31.1.1"],
    "entity_sensor": ["1.3.6.1.2.1.99.1.1"],
}
# subtrees whose rows are capped to the first 8 table rows (IF-MIB)
ROW_CAPPED = {"1.3.6.1.2.1.2.2": 8, "1.3.6.1.2.1.31.1.1": 8}

FORBIDDEN = (
    "1.3.6.1.6.3.15",  # USM users
    "1.3.6.1.6.3.18",  # communities
    "1.3.6.1.6.3.16",  # VACM
    "1.3.6.1.2.1.4.21",  # ipRouteTable
    "1.3.6.1.2.1.4.24",  # ipForward
    "1.3.6.1.2.1.4.22",  # ipNetToMedia (ARP)
    "1.3.6.1.2.1.4.35",  # ipNetToPhysical
    "1.3.6.1.2.1.17.4",  # dot1dTpFdb
    "1.3.6.1.2.1.17.7",  # Q-BRIDGE (FDB)
)
OID_RE = re.compile(r"^\.?[0-2](\.\d+){1,127}$")


def norm_oid(oid: str) -> str:
    return str(oid).strip().lstrip(".")


def _within(oid: str, prefix: str) -> bool:
    return oid == prefix or oid.startswith(prefix + ".")


def forbidden_reason(oid: str) -> str | None:
    """`never_walked` when the subtree is, contains or lies inside a forbidden subtree;
    `invalid_oid` for syntax; None when allowed."""
    o = norm_oid(oid)
    if not OID_RE.match(o):
        return "invalid_oid"
    for f in FORBIDDEN:
        if _within(o, f) or _within(f, o):
            return "never_walked"
    return None


def is_forbidden_varbind(oid: str) -> bool:
    o = norm_oid(oid)
    return any(_within(o, f) for f in FORBIDDEN)


def enterprise_subtree(sys_object_id: str | None) -> str | None:
    o = norm_oid(sys_object_id or "")
    m = re.match(r"^1\.3\.6\.1\.4\.1\.(\d+)", o)
    return f"1.3.6.1.4.1.{m.group(1)}" if m else None


def resolve_subtrees(
    presets: list[str], oids: list[str], sys_object_id: str | None
) -> tuple[list[str], list[dict[str, Any]]]:
    """(ordered unique subtrees, errors[{oid, reason}])."""
    out: list[str] = []
    errors: list[dict[str, Any]] = []
    for p in presets:
        if p not in PRESETS:
            errors.append({"oid": p, "reason": "unknown_preset"})
            continue
        if p == "enterprise":
            ent = enterprise_subtree(sys_object_id)
            if ent:
                out.append(ent)
            continue
        out.extend(PRESETS[p])
    if len(oids) > MAX_CUSTOM:
        errors.append({"oid": "", "reason": "too_many_custom_oids"})
    for o in oids[:MAX_CUSTOM]:
        reason = forbidden_reason(o)
        if reason:
            errors.append({"oid": o, "reason": reason})
            continue
        out.append(norm_oid(o))
    uniq: list[str] = []
    for o in out:
        if o not in uniq:
            uniq.append(o)
    if len(uniq) > MAX_SUBTREES:
        errors.append({"oid": "", "reason": "too_many_subtrees"})
    return uniq, errors


MIB2 = "1.3.6.1.2.1"
# per-subtree outcome of a walk
OUTCOMES = ("ok", "truncated", "timeout", "error", "skipped")
# why a subtree (or the walk) failed: the device rejected the credentials, the device did not
# answer at all, the walk budget ran out, any other SNMP error
ERROR_CODES = ("auth_failed", "unreachable", "timeout", "snmp_error")
_AUTH_MARKERS = (
    "wrong snmp pdu digest",
    "wrongdigest",
    "unknown usm user",
    "unknownusername",
    "unknown security name",
    "unsupported snmp security level",
    "unsupportedseclevel",
    "decryption error",
    "decryptionerror",
    "authorizationerror",
    "authentication failure",
    "noaccess",
)
_UNREACHABLE_MARKERS = (
    "no snmp response received",
    "request timed out",
    "requesttimedout",
    "network is unreachable",
    "no route to host",
    "connection refused",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
)


def is_standard_subtree(oid: str) -> bool:
    return _within(norm_oid(oid), MIB2)


def subtree_budget(oid: str) -> float:
    return SUBTREE_TIMEOUT_S if is_standard_subtree(oid) else VENDOR_SUBTREE_TIMEOUT_S


def order_subtrees(subtrees: list[str]) -> list[str]:
    """Standard (mib-2) subtrees first, in the given order; then the vendor ones."""
    return [s for s in subtrees if is_standard_subtree(s)] + [
        s for s in subtrees if not is_standard_subtree(s)
    ]


def classify_snmp_error(message: str) -> str:
    """`auth_failed` (the device rejected the credentials: SNMPv3 wrong digest / unknown user /
    security level, decryption, authorizationError, noAccess), `unreachable` (no answer at all;
    for v1/v2c also a wrong community, which agents do not answer) or `snmp_error`."""
    m = (message or "").lower().replace("_", " ")
    if any(k in m for k in _AUTH_MARKERS):
        return "auth_failed"
    if any(k in m for k in _UNREACHABLE_MARKERS):
        return "unreachable"
    return "snmp_error"


def walk_summary(outcomes: list[dict[str, Any]], total: int) -> tuple[str, str | None, str | None]:
    """(status, error_code, error) of a finished walk from its per-subtree outcomes:
    ok when every subtree finished (or stopped at its varbind cap), failed when nothing was
    collected and a subtree did not finish, partial otherwise. The error is the first
    subtree that did not finish."""
    bad = [o for o in outcomes if o["status"] not in ("ok", "truncated")]
    if not bad:
        return "ok", None, None
    first = next((o for o in bad if o["status"] != "skipped"), bad[0])
    code = first.get("error_code") or ("timeout" if first["status"] == "timeout" else None)
    status = "failed" if total == 0 else "partial"
    return status, code, first.get("error")


def row_cap_key(subtree: str, oid: str) -> str | None:
    """For row-capped tables: the row index part (after `<subtree>.1.<column>.`)."""
    cap = ROW_CAPPED.get(subtree)
    if cap is None:
        return None
    rest = norm_oid(oid)[len(subtree) + 1 :].split(".")
    if len(rest) < 3:
        return None
    return ".".join(rest[2:])


def _value(v: Any, n: int) -> str:
    s = "" if v is None else str(v)
    s = "".join(ch for ch in s if ch in "\t\n" or ord(ch) >= 32)
    return s if len(s) <= n else s[: n - 1] + "…"


def compact_walk(
    varbinds: list[list[Any]] | list[tuple[str, str, str]],
    oid_names: dict[str, str] | None = None,
    redact: Any = None,
    max_bytes: int = COMPACT_MAX_BYTES,
) -> dict[str, Any]:
    """Compaction for the model: scalars `{oid, type, value}`; tables detected by a common
    column prefix (`<entry>.<column>.<index>`) → columns + first 3 rows + row count; values
    ≤ 64 chars, passed through `redact` (a str→str callable); MIB names where known. The JSON
    stays ≤ `max_bytes` (items dropped from the end, `truncated`)."""
    names = oid_names or {}
    red = redact or (lambda s: s)
    scalars: list[dict[str, Any]] = []
    tables: dict[str, dict[str, Any]] = {}
    for vb in varbinds:
        oid, typ, val = norm_oid(vb[0]), str(vb[1]), vb[2]
        if is_forbidden_varbind(oid):
            continue
        parts = oid.split(".")
        value = _value(red(_value(val, 1000)), VALUE_MAX)
        if parts[-1] == "0" or len(parts) < 4:
            scalars.append(_named({"oid": oid, "type": typ, "value": value}, names, oid))
            continue
        # table: <entry>.<column>.<index...>; guess the entry as the prefix before the column
        # (the entry OID ends with .1 in SMI tables)
        entry, column, index = _split_table(parts)
        if entry is None:
            scalars.append(_named({"oid": oid, "type": typ, "value": value}, names, oid))
            continue
        t = tables.setdefault(entry, {"entry": entry, "columns": {}, "rows": {}, "row_count": 0})
        col_oid = f"{entry}.{column}"
        if col_oid not in t["columns"]:
            t["columns"][col_oid] = _named({"oid": col_oid, "type": typ}, names, col_oid)
        rows = t["rows"]
        seen = t.setdefault("seen", set())
        if index not in seen:
            seen.add(index)
            t["row_count"] += 1
            if len(rows) < 3:
                rows[index] = {}
        if index in rows:
            rows[index][column] = value
    out_tables = []
    for t in tables.values():
        out_tables.append(
            _named(
                {
                    "entry": t["entry"],
                    "columns": list(t["columns"].values()),
                    "rows": [{"index": k, "values": v} for k, v in t["rows"].items()],
                    "row_count": t["row_count"],
                },
                names,
                t["entry"],
            )
        )
    result: dict[str, Any] = {"scalars": scalars, "tables": out_tables, "truncated": False}
    while len(json.dumps(result, ensure_ascii=False)) > max_bytes:
        result["truncated"] = True
        if result["tables"] and (len(result["tables"]) >= len(result["scalars"]) // 4):
            result["tables"].pop()
        elif result["scalars"]:
            result["scalars"].pop()
        else:
            break
    return result


def _split_table(parts: list[str]) -> tuple[str | None, str, str]:
    # find the last ".1.<col>" such that what follows is the index: SMI entries are <table>.1
    for i in range(len(parts) - 2, 3, -1):
        if parts[i - 1] == "1" and i + 1 < len(parts):
            entry = ".".join(parts[:i])
            return entry, parts[i], ".".join(parts[i + 1 :])
    return None, "", ""


def _named(d: dict[str, Any], names: dict[str, str], oid: str) -> dict[str, Any]:
    n = names.get(oid)
    if n:
        d["name"] = n
    return d


def oids_in_template(data: Any) -> list[str]:
    """Every OID string referenced in a parsed template (config.oid, percent_of_oid,
    walk_oid, label_oid, label2_oid, filters/when oids, match prefixes are ignored)."""
    found: list[str] = []

    def visit(v: Any, key: str = "") -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                visit(x, str(k))
        elif isinstance(v, list):
            for x in v:
                visit(x, key)
        elif isinstance(v, str) and key in (
            "oid",
            "percent_of_oid",
            "walk_oid",
            "label_oid",
            "label2_oid",
        ):
            found.append(norm_oid(v))

    if isinstance(data, dict):
        visit(data.get("entries"))
    return found


def unknown_oid_warnings(data: Any, walked: list[str]) -> list[str]:
    """Warnings for OIDs used by the draft that are not in (or under / above) the walk."""
    walked_set = [norm_oid(o) for o in walked]
    out = []
    for o in oids_in_template(data):
        hit = any(w == o or w.startswith(o + ".") or o.startswith(w + ".") for w in walked_set)
        if not hit:
            out.append(f"OID {o} does not appear in the walk")
    return out


def mark_unverified(data: Any) -> Any:
    """`confidence: unverified`-equivalent: the validator only accepts high/medium/low/null,
    so drafts get `confidence: low` and a note `unverified` on every OID entry."""
    if not isinstance(data, dict):
        return data
    for e in data.get("entries") or []:
        if not isinstance(e, dict):
            continue
        uses_oid = bool(oids_in_template({"entries": [e]}))
        if uses_oid:
            e["confidence"] = "low"
            note = str(e.get("notes") or "")
            if "unverified" not in note:
                e["notes"] = ("unverified (AI draft) " + note).strip()[:500]
    return data


PROPOSE_TEMPLATE_TOOL = {
    "name": "propose_template",
    "description": "Propose the device template (YAML text in the NetworkMonitor template format).",
    "inputSchema": {
        "json": {
            "type": "object",
            "additionalProperties": False,
            "required": ["yaml"],
            "properties": {
                "yaml": {"type": "string"},
                "notes": {"type": "string"},
                "cannot_draft": {
                    "type": "boolean",
                    "description": "true when no useful template can be drafted from this walk",
                },
                "reason": {
                    "type": "string",
                    "maxLength": 500,
                    "description": "why no template is possible (only with cannot_draft)",
                },
                "confidence_per_sensor": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        },
                    },
                },
            },
        }
    },
}


TEMPLATE_CANNOT_REASON = {
    "en": (
        "The walk and the notes do not give enough to build a template (no device-specific "
        "values to monitor), so no draft was made."
    ),
    "he": (
        "הסריקה וההערות לא מספיקות לבניית תבנית (אין ערכים ייחודיים למכשיר לניטור), "
        "ולכן לא נוצרה טיוטה."
    ),
}


def template_is_placeholder(yaml_text: str) -> bool:
    """Deterministic backstop (pure): an empty YAML or `<UNKNOWN>`-style placeholders."""
    return not (yaml_text or "").strip() or bool(PLACEHOLDER_RE.search(yaml_text))


PLACEHOLDER_RE = re.compile(
    r"<\s*(unknown|placeholder|todo|tbd|fill[ _-]?in|n/?a|\.\.\.|\?+)\s*>", re.I
)
