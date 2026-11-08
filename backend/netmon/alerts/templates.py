"""Notification texts (en / he)."""

from __future__ import annotations

import contextvars
import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

# [2b-1] the zone used to render "Problem since" (settings.timezone), set by the alert engine
DISPLAY_TZ: contextvars.ContextVar[str] = contextvars.ContextVar("netmon_display_tz", default="UTC")

STATUS_NAMES = {
    "en": {
        "up": "UP",
        "warning": "WARNING",
        "down": "DOWN",
        "paused": "PAUSED",
        "unknown": "UNKNOWN",
        "unreachable": "UNREACHABLE",
    },
    "he": {
        "up": "תקין",
        "warning": "אזהרה",
        "down": "תקלה",
        "paused": "מושהה",
        "unknown": "לא ידוע",
        "unreachable": "לא נגיש",
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
        "problem_since": "Problem since",
        "escalation": "ESCALATION",
        "reminder": "REMINDER",
        "after_maintenance": " (after maintenance)",
        "link": "Open in NetworkMonitor",
        "test_subject": "[NetworkMonitor] Test notification",
        "test_body": "This is a test notification from NetworkMonitor.",
        "new_hosts_subject": "[NetworkMonitor] New hosts discovered: {name} ({count})",
        "more": "+{n} more",
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
        "problem_since": "בעיה מאז",
        "escalation": "הסלמה",
        "reminder": "תזכורת",
        "after_maintenance": " (לאחר תחזוקה)",
        "link": "פתיחה ב-NetworkMonitor",
        "test_subject": "[NetworkMonitor] הודעת בדיקה",
        "test_body": "זוהי הודעת בדיקה מ-NetworkMonitor.",
        "new_hosts_subject": "[NetworkMonitor] התגלו מארחים חדשים: {name} ({count})",
        "more": "ועוד {n}",
    },
}


DEVICE_CLASS_NAMES = {
    "en": {
        "router": "router",
        "switch": "switch",
        "access_point": "access point",
        "printer": "printer",
        "nas": "NAS",
        "server": "server",
        "workstation": "workstation",
        "unknown": "unknown",
    },
    "he": {
        "router": "נתב/חומת אש",
        "switch": "מתג",
        "access_point": "נקודת גישה",
        "printer": "מדפסת",
        "nas": "NAS",
        "server": "שרת",
        "workstation": "תחנת עבודה",
        "unknown": "לא ידוע",
    },
}

TELEGRAM_LIMIT = 4096


def _lang(lang: str) -> str:
    return lang if lang in LABELS else "en"


def subject(payload: dict[str, Any], lang: str) -> str:
    lang = _lang(lang)
    if payload.get("event") == "test":
        return LABELS[lang]["test_subject"]
    if payload.get("event") == "discovery.new_hosts":
        return LABELS[lang]["new_hosts_subject"].format(
            name=(payload.get("profile") or {}).get("name", "?"),
            count=new_hosts_count(payload),
        )
    # status names in the subject stay as in Phase 1 (DOWN/WARNING/UP in both languages)
    status = STATUS_NAMES["en"].get(payload.get("new_status") or "", payload.get("new_status"))
    if lang != "en":
        status = STATUS_NAMES[lang].get(payload.get("new_status") or "", status)
    device = (payload.get("device") or {}).get("name", "?")
    sensor = (payload.get("sensor") or {}).get("name", "?")
    stage = payload.get("stage")
    lb = LABELS[lang]
    if stage in ("escalation", "reminder"):
        status = f"{lb[stage]} {STATUS_NAMES['en'].get(payload.get('new_status') or '', status)}"
    suffix = lb["after_maintenance"] if payload.get("after_maintenance") else ""
    return f"[NetworkMonitor] {status}{suffix}: {device} / {sensor}"


def format_local(value: str | None, tz_name: str | None = None) -> str:
    """ISO-8601 UTC -> `YYYY-MM-DD HH:MM (Zone)` in the display zone."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        name = tz_name or DISPLAY_TZ.get()
        local = dt.astimezone(ZoneInfo(name))
    except (ValueError, OSError, KeyError):
        return value
    return f"{local:%Y-%m-%d %H:%M} ({name})"


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
    if payload.get("problem_since") and payload.get("stage") != "recovery":
        rows.append((lb["problem_since"], format_local(payload["problem_since"])))
    if payload.get("rule"):
        rows.append((lb["rule"], payload["rule"]["name"]))
    return rows


def new_hosts_count(payload: dict[str, Any]) -> int:
    """Full new-host count: `hosts` is capped at 50, the message always has the total."""
    import re

    m = re.search(r"(\d+) new host", str(payload.get("message") or ""))
    if m:
        return int(m.group(1))
    return len(payload.get("hosts") or [])


def host_lines(payload: dict[str, Any], lang: str) -> list[str]:
    """`10.0.0.5 — brn3c2af4.lan — printer — brother` (missing parts omitted), then `+N more`."""
    names = DEVICE_CLASS_NAMES[lang]
    lines = []
    hosts = payload.get("hosts") or []
    for h in hosts:
        parts = [h.get("ip"), h.get("hostname")]
        cls = h.get("device_class")
        parts.append(names.get(cls, cls) if cls else None)
        parts.append(h.get("vendor"))
        lines.append(" — ".join(str(p) for p in parts if p))
    extra = new_hosts_count(payload) - len(hosts)
    if extra > 0:
        lines.append(LABELS[lang]["more"].format(n=extra))
    return lines


def text_body(payload: dict[str, Any], lang: str) -> str:
    lang = _lang(lang)
    if payload.get("event") == "discovery.new_hosts":
        lines = host_lines(payload, lang)
        if payload.get("url"):
            lines.append(f"{LABELS[lang]['link']}: {payload['url']}")
        return "\n".join(lines) + "\n"
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
    if payload.get("event") == "discovery.new_hosts":
        items = "".join(f"<li>{html.escape(x)}</li>" for x in host_lines(payload, lang))
        link = ""
        if payload.get("url"):
            url = html.escape(payload["url"])
            link = f'<p><a href="{url}">{html.escape(LABELS[lang]["link"])}</a></p>'
        return (
            f'<!doctype html><html lang="{lang}" dir="{direction}"><body '
            f'style="font-family:sans-serif"><ul>{items}</ul>{link}</body></html>'
        )
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


def truncate_telegram(text: str, limit: int = TELEGRAM_LIMIT) -> str:
    """Cut to Telegram's limit on a line boundary, with an ellipsis appended."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    return cut + "…"


def telegram_text(payload: dict[str, Any], lang: str, parse_mode: str | None) -> str:
    lang = _lang(lang)
    subj = subject(payload, lang)
    if parse_mode != "HTML":
        return truncate_telegram(f"{subj}\n\n" + text_body(payload, lang).rstrip("\n"))

    def esc(v: Any) -> str:
        return html.escape(str(v), quote=False)

    lines = [f"<b>{esc(subj)}</b>"]
    if payload.get("event") == "test":
        lines.append(esc(LABELS[lang]["test_body"]))
    elif payload.get("event") == "discovery.new_hosts":
        lines += [esc(x) for x in host_lines(payload, lang)]
    else:
        lines += [f"<b>{esc(k)}:</b> {esc(v)}" for k, v in _rows(payload, lang)]
    if payload.get("url"):
        url = html.escape(payload["url"], quote=True)
        lines.append(f'<a href="{url}">{esc(LABELS[lang]["link"])}</a>')
    return truncate_telegram("\n".join(lines))
