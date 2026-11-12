"""[3b] Pure core of the daily syslog digest (`digest.signature`, `digest.compare`, § 14.8.5)."""

from __future__ import annotations

import hashlib
import re
from typing import Any

UNUSUAL_RATIO = 3.0
UNUSUAL_ABS = 20
MIN_BASELINE_DAYS = 3
TOP = 20

_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_MAC = re.compile(
    r"(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])"
    r"|(?<![0-9a-f.])[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}(?![0-9a-f.])"
    r"|(?<![0-9a-f-])[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}(?![0-9a-f-])"
)
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9a-z:])[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}(?:/\d{1,3})?(?![0-9a-z:])")
_HEX = re.compile(r"\b0x[0-9a-f]+\b|\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{8,}\b")
_NUM = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")


def _ipv6(m: re.Match[str]) -> str:
    s = m.group(0)
    if "::" in s or s.count(":") >= 7:
        return "<ip>"
    return s


def normalize(message: str) -> str:
    s = (message or "").lower()
    s = _QUOTED.sub("<str>", s)
    s = _MAC.sub("<mac>", s)
    s = _IPV4.sub("<ip>", s)
    s = _IPV6.sub(_ipv6, s)
    s = _HEX.sub("<hex>", s)
    s = _NUM.sub("<n>", s)
    s = _SPACES.sub(" ", s).strip()
    return s[:160]


def signature(message: str) -> tuple[str, str]:
    """(signature = SHA-1 hex first 16 of the normalised text, normalised text)."""
    norm = normalize(message)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16], norm  # noqa: S324 - not security


def compare(baseline: float | None, day: int, *, severe: bool = False) -> tuple[bool, float | None]:
    """(unusual, ratio). Unusual: ratio ≥ 3 and +20 or more; a key new versus the baseline with
    ≥ 20 messages; or a severity ≤ 3 count > 0 where the baseline is 0."""
    if baseline is None or baseline <= 0:
        if severe and day > 0:
            return True, None
        return day >= UNUSUAL_ABS, None
    ratio = day / baseline
    return ratio >= UNUSUAL_RATIO and day - baseline >= UNUSUAL_ABS, round(ratio, 2)


def rows(
    day_counts: dict[str, int],
    baseline_days: list[dict[str, int]],
    *,
    severe_keys: set[str] | None = None,
    refs: dict[str, str | None] | None = None,
    top: int = TOP,
) -> list[dict[str, Any]]:
    """DigestRow list for one dimension: the top `top` keys of the day, plus keys that are
    unusual by the baseline. baseline = mean over the given previous days (None when fewer than
    3 days with data)."""
    learning = len(baseline_days) < MIN_BASELINE_DAYS
    out = []
    keys = sorted(day_counts, key=lambda k: (-day_counts[k], k))
    for i, k in enumerate(keys):
        count = int(day_counts[k])
        base = None if learning else sum(d.get(k, 0) for d in baseline_days) / len(baseline_days)
        if learning:
            unusual, ratio = False, None
        else:
            unusual, ratio = compare(base, count, severe=bool(severe_keys and k in severe_keys))
        if i >= top and not unusual:
            continue
        out.append(
            {
                "key": k,
                "ref": (refs or {}).get(k),
                "count": count,
                "baseline": None if base is None else round(base, 2),
                "ratio": ratio,
                "unusual": unusual,
            }
        )
    return out


def facts_for_model(stats: dict[str, Any]) -> dict[str, Any]:
    """The statistics JSON sent to the model (no raw logs except the ≤ 20 examples)."""

    def slim(rs: list[dict[str, Any]], n: int = 20) -> list[dict[str, Any]]:
        return [
            {k: r.get(k) for k in ("key", "ref", "count", "baseline", "ratio", "unusual")}
            for r in rs[:n]
        ]

    return {
        "day": stats.get("day"),
        "total_messages": stats.get("total_messages"),
        "baseline_days": stats.get("baseline_days"),
        "baseline_learning": stats.get("baseline_days", 0) < MIN_BASELINE_DAYS,
        "by_severity": slim(stats.get("by_severity") or []),
        "by_program": slim(stats.get("by_program") or []),
        "by_device": slim(stats.get("by_device") or []),
        "by_hour": stats.get("by_hour") or [],
        "new_types": [
            {k: t.get(k) for k in ("sample", "count", "example", "example_ref")}
            for t in (stats.get("new_types") or [])[:20]
        ],
        "sampled": bool(stats.get("sampled")),
    }


def unusual_count(stats: dict[str, Any]) -> int:
    return sum(
        1
        for dim in ("by_severity", "by_program", "by_device")
        for r in stats.get(dim) or []
        if r.get("unusual")
    )


def stats_table_text(stats: dict[str, Any], lang: str = "en") -> str:
    """Plain-text statistics (deliveries when the AI summary is unavailable)."""
    he = lang == "he"
    lines = [
        ("סיכום syslog יומי" if he else "Daily syslog digest") + f" {stats.get('day')}",
        ("סה״כ הודעות" if he else "Total messages") + f": {stats.get('total_messages', 0)}",
    ]
    for dim, label in (
        ("by_severity", "Severity"),
        ("by_program", "Program"),
        ("by_device", "Device"),
    ):
        unusual = [r for r in stats.get(dim) or [] if r.get("unusual")]
        for r in unusual[:5]:
            lines.append(f"- {label} {r['key']}: {r['count']} (baseline {r['baseline']})")
    nt = stats.get("new_types") or []
    if nt:
        lines.append(("סוגי הודעות חדשים" if he else "New message types") + f": {len(nt)}")
        for t in nt[:5]:
            lines.append(f"- {t['sample'][:120]} ({t['count']})")
    return "\n".join(lines)
