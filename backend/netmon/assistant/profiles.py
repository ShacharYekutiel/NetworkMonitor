"""[3b] Pure: where an inference profile / model id processes data (owner decision: region and
inference-profile prefix are settings; the settings API explains the data-location trade-off).

Recommended default: region `us-east-1` with a US geographic profile (`us.` prefix) — processing
stays in US regions. Fully supported alternative: region `il-central-1` with a `global.` profile
(in that region Claude models are reachable only through global cross-region inference). A
`global.` profile is accepted with a *warning* (`global_inference_profile`), never refused."""

from __future__ import annotations

import re
from typing import Any

RECOMMENDED_REGION = "us-east-1"
RECOMMENDED_PREFIX = "us"
GLOBAL_WARNING = "global_inference_profile"

GEO_NAMES: dict[str, tuple[str, str]] = {
    "us": ("the United States", "ארצות הברית"),
    "us-gov": ("AWS GovCloud (US)", "AWS GovCloud (ארה״ב)"),
    "eu": ("the European Union", "האיחוד האירופי"),
    "apac": ("Asia Pacific", "אסיה-פסיפיק"),
    "jp": ("Japan", "יפן"),
    "au": ("Australia", "אוסטרליה"),
    "ca": ("Canada", "קנדה"),
}

SCOPES: list[dict[str, Any]] = [
    {
        "scope": "global",
        "prefix": "global.",
        "explanation": {
            "en": "Global cross-region inference: AWS may process each request in any "
            "commercial AWS region worldwide. Needed where Claude is offered only through "
            "global profiles (e.g. il-central-1).",
            "he": "הסקה גלובלית בין-אזורית: AWS עשויה לעבד כל בקשה בכל אזור מסחרי של AWS "
            "בעולם. נדרש כאשר Claude זמין רק דרך פרופילים גלובליים (למשל il-central-1).",
        },
    },
    {
        "scope": "geo",
        "prefix": "us. / eu. / apac. / …",
        "explanation": {
            "en": "Geographic cross-region inference: requests are processed only in AWS "
            "regions inside that geography (e.g. us. = US regions). Recommended: us-east-1 "
            "with a us. profile.",
            "he": "הסקה גאוגרפית בין-אזורית: הבקשות מעובדות רק באזורי AWS בתוך אותו אזור "
            "גאוגרפי (למשל us. = אזורי ארה״ב). מומלץ: us-east-1 עם פרופיל us.",
        },
    },
    {
        "scope": "in_region",
        "prefix": "anthropic. (bare model ID)",
        "explanation": {
            "en": "In-region: processed only in the selected region. Works only where the "
            "model is offered on demand in that region; many current models need a profile.",
            "he": "באזור בלבד: העיבוד מתבצע רק באזור שנבחר. עובד רק כאשר המודל זמין לפי "
            "דרישה באותו אזור; מודלים עדכניים רבים דורשים פרופיל.",
        },
    },
    {
        "scope": "arn",
        "prefix": "arn:aws:bedrock:…",
        "explanation": {
            "en": "Inference-profile or provisioned-throughput ARN: processed where that "
            "profile routes (application profiles copy the regions of their source).",
            "he": "ARN של פרופיל הסקה או קיבולת שמורה: העיבוד מתבצע היכן שהפרופיל מנתב.",
        },
    },
]
_BY_SCOPE = {s["scope"]: s for s in SCOPES}
_PREFIX_RE = re.compile(r"^([a-z]{2,6}(?:-gov)?)\.")


def classify(model_id: str | None) -> dict[str, Any]:
    """`{"scope", "prefix", "geography", "explanation": {"en", "he"}, "warning"}` for a model
    id / profile id / ARN (None or empty → scope null)."""
    mid = (model_id or "").strip()
    if not mid:
        return {"scope": None, "prefix": None, "geography": None, "explanation": None,
                "warning": None}  # fmt: skip
    if mid.startswith("arn:"):
        arn_scope = _arn_profile_scope(mid)
        if arn_scope is not None:
            return arn_scope
        return _out("arn", None, None)
    m = _PREFIX_RE.match(mid)
    if m:
        prefix = m.group(1)
        if prefix == "global":
            return _out("global", "global", None, GLOBAL_WARNING)
        if prefix in GEO_NAMES:
            return _out("geo", prefix, prefix)
    return _out("in_region", None, None)


def _arn_profile_scope(arn: str) -> dict[str, Any] | None:
    # arn:aws:bedrock:<region>:<acct>:inference-profile/<prefix>.anthropic...
    tail = arn.rsplit("/", 1)[-1]
    if ":inference-profile/" in arn and _PREFIX_RE.match(tail):
        return classify(tail)
    return None


def _out(scope: str, prefix: str | None, geo: str | None, warning: str | None = None) -> dict:
    base = _BY_SCOPE[scope]
    explanation = dict(base["explanation"])
    if geo:
        en, he = GEO_NAMES[geo]
        explanation = {
            "en": f"Geographic cross-region inference ({geo}.): requests are processed only in "
            f"AWS regions in {en}.",
            "he": f"הסקה גאוגרפית בין-אזורית ({geo}.): הבקשות מעובדות רק באזורי AWS ב{he}.",
        }
    return {
        "scope": scope,
        "prefix": prefix,
        "geography": geo,
        "explanation": explanation,
        "warning": warning,
    }


def warnings_for(model_ids: list[str | None]) -> list[str]:
    return [GLOBAL_WARNING] if any(classify(m)["warning"] for m in model_ids if m) else []
