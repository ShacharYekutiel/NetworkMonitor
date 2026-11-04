"""Notification texts (en / he)."""

from __future__ import annotations

import html
from typing import Any

STATUS_NAMES = {
    "en": {
        "up": "UP",
        "warning": "WARNING",
        "down": "DOWN",
        "paused": "PAUSED",
        "unknown": "UNKNOWN",
    },
    "he": {
        "up": "תקין",
        "warning": "אזהרה",
        "down": "תקלה",
        "paused": "מושהה",
        "unknown": "לא ידוע",
    },
}

LABELS = {
    "en": {
        "sensor": "Sensor",
        "device": "Device",
        "group": "Group",
        "status": "Status",
        "previous": "Previous status",
        "message": "Message",
        "time": "Time",
        "rule": "Rule",
        "link": "Open in NetworkMonitor",
        "test_subject": "[NetworkMonitor] Test notification",
        "test_body": "This is a test notification from NetworkMonitor.",
    },
    "he": {
        "sensor": "חיישן",
        "device": "התקן",
        "group": "קבוצה",
        "status": "סטטוס",
        "previous": "סטטוס קודם",
        "message": "הודעה",
        "time": "זמן",
        "rule": "כלל",
        "link": "פתיחה ב-NetworkMonitor",
        "test_subject": "[NetworkMonitor] הודעת בדיקה",
        "test_body": "זוהי הודעת בדיקה מ-NetworkMonitor.",
    },
}


def _lang(lang: str) -> str:
    return lang if lang in LABELS else "en"


def subject(payload: dict[str, Any], lang: str) -> str:
    lang = _lang(lang)
    if payload.get("event") == "test":
        return LABELS[lang]["test_subject"]
    status = STATUS_NAMES[lang].get(payload.get("new_status") or "", payload.get("new_status"))
    device = (payload.get("device") or {}).get("name", "?")
    sensor = (payload.get("sensor") or {}).get("name", "?")
    return f"[NetworkMonitor] {status}: {device} / {sensor}"


def _rows(payload: dict[str, Any], lang: str) -> list[tuple[str, str]]:
    lb, st = LABELS[lang], STATUS_NAMES[lang]
    rows: list[tuple[str, str]] = []
    if payload.get("sensor"):
        rows.append((lb["sensor"], f"{payload['sensor']['name']} ({payload['sensor']['type']})"))
    if payload.get("device"):
        rows.append((lb["device"], f"{payload['device']['name']} ({payload['device']['host']})"))
    if payload.get("group"):
        rows.append((lb["group"], payload["group"]["name"]))
    if payload.get("new_status"):
        rows.append((lb["status"], st.get(payload["new_status"], payload["new_status"])))
    if payload.get("old_status"):
        rows.append((lb["previous"], st.get(payload["old_status"], payload["old_status"])))
    if payload.get("message"):
        rows.append((lb["message"], payload["message"]))
    rows.append((lb["time"], payload.get("time") or ""))
    if payload.get("rule"):
        rows.append((lb["rule"], payload["rule"]["name"]))
    return rows


def text_body(payload: dict[str, Any], lang: str) -> str:
    lang = _lang(lang)
    lines = []
    if payload.get("event") == "test":
        lines.append(LABELS[lang]["test_body"])
    lines += [f"{k}: {v}" for k, v in _rows(payload, lang)]
    if payload.get("url"):
        lines.append(f"{LABELS[lang]['link']}: {payload['url']}")
    return "\n".join(lines) + "\n"


def html_body(payload: dict[str, Any], lang: str) -> str:
    lang = _lang(lang)
    direction = "rtl" if lang == "he" else "ltr"
    intro = ""
    if payload.get("event") == "test":
        intro = f"<p>{html.escape(LABELS[lang]['test_body'])}</p>"
    rows = "".join(
        f'<tr><th style="text-align:start;padding:4px 12px">{html.escape(k)}</th>'
        f'<td style="padding:4px 12px">{html.escape(str(v))}</td></tr>'
        for k, v in _rows(payload, lang)
    )
    link = ""
    if payload.get("url"):
        url = html.escape(payload["url"])
        link = f'<p><a href="{url}">{html.escape(LABELS[lang]["link"])}</a></p>'
    return (
        f'<!doctype html><html lang="{lang}" dir="{direction}"><body '
        f'style="font-family:sans-serif">{intro}<table>{rows}</table>{link}</body></html>'
    )
