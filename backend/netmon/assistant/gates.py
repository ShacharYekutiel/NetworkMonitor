"""[3b] Feature dependencies of the assistant features (architecture § 14.0.1).

Every AI feature needs `assistant` ON; extra needs below. As in § 13.0 a toggle may be ON while
a dependency is OFF: the feature then *idles* (endpoints `409 feature_idle`, background work
skipped). `assistant` itself needs `audit` (hard rule: `422 feature_dependency`).

    from netmon.assistant.gates import require_ai
    require_ai(rt, "ai_config_diff")     # 409 feature_disabled / 409 feature_idle

`idle_reasons(name, snapshot, cfg)` → the missing needs (pure)."""

from __future__ import annotations

from typing import Any

ASSISTANT = "assistant"
AI_FEATURES: tuple[str, ...] = (
    "ai_explain_alert",
    "ai_correlation",
    "ai_alert_notes",
    "ai_config_diff",
    "ai_syslog_digest",
    "ai_rule_drafts",
    "ai_template_drafts",
    "ai_knowledge",
    "ai_chat_ask",
    "ai_report_summary",
)
ALL: tuple[str, ...] = (ASSISTANT, *AI_FEATURES)
CONFIGURE_FIRST = frozenset({"assistant", "ai_knowledge"})

# extra needs per feature (feature names, or `setting:<key>` for an assistant setting)
NEEDS: dict[str, tuple[str, ...]] = {
    "ai_explain_alert": (),
    "ai_correlation": (),
    "ai_alert_notes": (),
    "ai_config_diff": ("config_backup", "send_config_excerpts"),
    "ai_syslog_digest": ("syslog",),
    "ai_rule_drafts": (),  # event-rule drafts additionally need traps or syslog
    "ai_template_drafts": (),
    "ai_knowledge": (),
    "ai_chat_ask": ("chat_ack",),
    "ai_report_summary": ("reports",),
}
SETTING_NEEDS = frozenset({"send_config_excerpts"})


def idle_reasons(name: str, snapshot: dict[str, bool], cfg: dict[str, Any] | None) -> list[str]:
    """Pure: the needs of `name` that are missing (empty = can work)."""
    if name == ASSISTANT:
        return [] if snapshot.get("audit") else ["audit"]
    missing: list[str] = []
    if not snapshot.get(ASSISTANT):
        missing.append(ASSISTANT)
    for need in NEEDS.get(name, ()):
        if need in SETTING_NEEDS:
            if not (cfg or {}).get(need):
                missing.append(need)
        elif not snapshot.get(need):
            missing.append(need)
    return missing


def _cfg(rt: Any) -> dict[str, Any] | None:
    svc = getattr(rt, "assistant", None)
    return getattr(svc, "cfg", None) if svc is not None else None


def is_active(rt: Any, name: str) -> bool:
    """ON and not idle."""
    features = getattr(rt, "features", None)
    if features is None or not features.enabled(name):
        return False
    return not idle_reasons(name, features.snapshot(), _cfg(rt))


def require_ai(rt: Any, name: str) -> None:
    """Endpoint guard: 409 `feature_disabled` while OFF, 409 `feature_idle` while ON but a
    dependency is missing (`details: [{"feature", "needs"}]`)."""
    from netmon.api.errors import ApiError
    from netmon.services.features import feature_error

    features = getattr(rt, "features", None)
    if features is None or not features.enabled(name):
        raise feature_error(name)
    missing = idle_reasons(name, features.snapshot(), _cfg(rt))
    if missing:
        raise ApiError(
            409,
            "feature_idle",
            f"The feature {name} is waiting for: {', '.join(missing)}",
            [{"feature": name, "needs": missing}],
        )


def ai_guard(name: str) -> Any:
    """Router dependency form of `require_ai`."""
    from fastapi import Request

    if name not in ALL:
        raise ValueError(f"unknown assistant feature {name!r}")

    def dependency(request: Request) -> None:
        require_ai(request.app.state.rt, name)

    dependency.__name__ = f"ai_{name}"
    return dependency
