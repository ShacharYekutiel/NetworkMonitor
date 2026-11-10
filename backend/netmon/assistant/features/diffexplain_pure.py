"""[3b] Pure core of the config diff explanation (`diffexplain.risk_flags`, § 14.8.4)."""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RULES_FILE = Path(__file__).resolve().parent.parent / "risk_rules.yaml"
SEVERITIES = ("high", "medium", "info")
MAX_DIFF_LINES = 400


@dataclass(frozen=True)
class RiskRule:
    id: str
    severity: str
    side: str
    regex: re.Pattern[str]
    drivers: tuple[str, ...]
    title: dict[str, str]


def parse_rules(data: Any) -> list[RiskRule]:
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise ValueError("risk rules: a mapping with a 'rules' list is required")
    out = []
    for i, r in enumerate(data["rules"]):
        if not isinstance(r, dict):
            raise ValueError(f"risk rules[{i}]: not a mapping")
        sev = r.get("severity")
        side = r.get("side", "any")
        if sev not in SEVERITIES or side not in ("added", "removed", "any"):
            raise ValueError(f"risk rules[{i}]: invalid severity/side")
        title = r.get("title") or {}
        out.append(
            RiskRule(
                id=str(r["id"]),
                severity=sev,
                side=side,
                regex=re.compile(str(r["regex"]), re.IGNORECASE),
                drivers=tuple(r.get("drivers") or ()),
                title={"en": str(title.get("en") or r["id"]), "he": str(title.get("he") or "")},
            )
        )
    return out


@functools.lru_cache(maxsize=1)
def load_rules() -> tuple[RiskRule, ...]:
    import yaml

    return tuple(parse_rules(yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))))


def diff_lines(diff: str) -> list[tuple[int, str, str]]:
    """(1-based line number in the diff text, side, text without the sign) of every added /
    removed line; headers (`---`, `+++`, `@@`) are skipped."""
    out = []
    for n, line in enumerate(diff.splitlines(), start=1):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+"):
            out.append((n, "added", line[1:].strip()))
        elif line.startswith("-"):
            out.append((n, "removed", line[1:].strip()))
    return out


def risk_flags(
    diff: str, driver: str | None, rules: tuple[RiskRule, ...] | list[RiskRule] | None = None
) -> list[dict[str, Any]]:
    """Deterministic flags: `[{rule_id, severity, title_key, title, side, lines}]` grouped per
    (rule, side), ordered by severity then first line."""
    rules = load_rules() if rules is None else rules
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for n, side, txt in diff_lines(diff):
        if not txt:
            continue
        for rule in rules:
            if rule.drivers and (driver or "") not in rule.drivers:
                continue
            if rule.side != "any" and rule.side != side:
                continue
            if not rule.regex.search(txt):
                continue
            key = (rule.id, side)
            flag = found.get(key)
            if flag is None:
                flag = found[key] = {
                    "rule_id": rule.id,
                    "severity": rule.severity,
                    "title_key": f"diffRisk.{rule.id}",
                    "title": rule.title,
                    "side": side,
                    "lines": [],
                }
            flag["lines"].append(n)
            break
    order = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted(found.values(), key=lambda f: (order[f["severity"]], f["lines"][0]))


def cap_diff(
    diff: str, flags: list[dict[str, Any]], max_lines: int = MAX_DIFF_LINES
) -> tuple[str, bool]:
    """≤ `max_lines` lines for the model: the hunks around flagged lines first, then the rest in
    order until the cap. Kept lines are prefixed with their diff line number (`123| +x`), so the
    model can cite them. Returns (text, truncated)."""
    lines = diff.splitlines()
    if len(lines) <= max_lines:
        return "\n".join(f"{i}| {t}" for i, t in enumerate(lines, start=1)), False
    hunks: list[tuple[int, int]] = []
    start = None
    for i, t in enumerate(lines):
        if t.startswith("@@"):
            if start is not None:
                hunks.append((start, i))
            start = i
    if start is not None:
        hunks.append((start, len(lines)))
    if not hunks:
        hunks = [(0, len(lines))]
    flagged = {n - 1 for f in flags for n in f["lines"]}
    first = [h for h in hunks if any(h[0] <= n < h[1] for n in flagged)]
    rest = [h for h in hunks if h not in first]
    keep: set[int] = set()
    for a, b in first + rest:
        for i in range(a, b):
            if len(keep) >= max_lines:
                break
            keep.add(i)
        if len(keep) >= max_lines:
            break
    text = "\n".join(f"{i + 1}| {lines[i]}" for i in sorted(keep))
    return text, True


def validate_ai(result: Any, diff: str) -> dict[str, Any]:
    """The model's `report_diff_explanation` input -> the stored `ai` object; lines are kept
    only when they exist in the diff (and are + / - lines); bounded sizes."""
    if not isinstance(result, dict):
        raise ValueError("invalid explanation")
    valid_lines = {n for n, _, _ in diff_lines(diff)}
    summary = [str(s)[:400] for s in (result.get("summary") or []) if str(s).strip()][:5]
    risks = []
    for r in (result.get("risks") or [])[:10]:
        if not isinstance(r, dict):
            continue
        sev = r.get("severity") if r.get("severity") in SEVERITIES else "info"
        lines = []
        for x in r.get("lines") or []:
            try:
                n = int(x)
            except (TypeError, ValueError):
                continue
            if n in valid_lines and n not in lines:
                lines.append(n)
        risks.append(
            {
                "severity": sev,
                "title": str(r.get("title") or "")[:200],
                "explanation": str(r.get("explanation") or "")[:1000],
                "lines": lines[:50],
            }
        )
    conf = result.get("confidence")
    return {
        "summary": summary,
        "risks": risks,
        "confidence": conf if conf in ("low", "medium", "high") else "low",
    }


EXPLAIN_TOOL = {
    "name": "report_diff_explanation",
    "description": "Report the plain-language explanation of the configuration diff.",
    "inputSchema": {
        "json": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "risks", "confidence"],
            "properties": {
                "summary": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                "risks": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["severity", "title", "explanation", "lines"],
                        "properties": {
                            "severity": {"type": "string", "enum": list(SEVERITIES)},
                            "title": {"type": "string"},
                            "explanation": {"type": "string"},
                            "lines": {"type": "array", "items": {"type": "integer"}},
                        },
                    },
                },
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            },
        }
    },
}
