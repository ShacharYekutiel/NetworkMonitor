"""[severity] Message renderings for the "feature OFF = byte-identical" test.

`render_all()` renders a fixed set of notifications on every channel (email subject / text /
HTML, Telegram HTML and plain, Teams card, webhook JSON body) in both languages.
`tests/fixtures/severity_off_golden.json` holds the output of the code before alert severity
(ui-redesign 8098090), produced with::

    PYTHONPATH=<checkout of 8098090>/backend python tests/severity_golden.py > \\
        tests/fixtures/severity_off_golden.json

This module imports nothing from the test suite, so it runs against any checkout."""

from __future__ import annotations

import json
import sys
from typing import Any

EVENT = {
    "id": 991,
    "time": "2026-11-04T12:00:00Z",
    "sensor_id": 7,
    "old_status": "up",
    "new_status": "down",
    "message": "Host unreachable <x>",
}
CTX = (
    {"id": 7, "name": "Ping", "type": "ping"},
    {"id": 3, "name": "core-sw", "host": "10.0.0.2"},
    {"id": 1, "name": "Default"},
    {"id": 2, "name": "All down"},
)
CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("problem", EVENT, {"stage": "problem"}),
    ("warning", {**EVENT, "new_status": "warning"}, {"stage": "problem"}),
    ("after_maintenance", EVENT, {"stage": "problem", "after_maintenance": True}),
    ("escalation", EVENT, {"stage": "escalation"}),
    ("reminder", EVENT, {"stage": "reminder"}),
    (
        "recovery",
        {**EVENT, "id": 992, "old_status": "down", "new_status": "up", "message": ""},
        {"stage": "recovery"},
    ),
]


def payloads(**extra: Any) -> dict[str, dict[str, Any]]:
    """`extra` goes to build_payload (the severity test passes `severity=`)."""
    from netmon.alerts.engine import build_payload

    out = {}
    for name, event, kw in CASES:
        out[name] = build_payload(
            event,
            *CTX,
            "http://netmon.local:8080",
            problem_since="2026-11-04T11:58:00Z",
            **kw,
            **extra,
        )
    out["test"] = build_payload(None, None, None, None, None, None, "2026-11-04T12:00:00Z")
    return out


def render(payload: dict[str, Any]) -> dict[str, Any]:
    from netmon.alerts import teams, templates

    out: dict[str, Any] = {"webhook": json.dumps(payload, ensure_ascii=False)}
    for lang in ("en", "he"):
        out[lang] = {
            "subject": templates.subject(payload, lang),
            "text": templates.text_body(payload, lang),
            "html": templates.html_body(payload, lang),
            "telegram_html": templates.telegram_text(payload, lang, "HTML"),
            "telegram_plain": templates.telegram_text(payload, lang, None),
            "teams": json.dumps(teams.build_card(payload, lang), ensure_ascii=False),
        }
    return out


def render_all(**extra: Any) -> dict[str, Any]:
    return {name: render(p) for name, p in payloads(**extra).items()}


if __name__ == "__main__":
    json.dump(render_all(), sys.stdout, ensure_ascii=False, indent=1, sort_keys=True)
    sys.stdout.write("\n")
