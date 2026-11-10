"""[3] Reports: rendering (architecture § 13.6.3–13.6.4). Pure functions of `ReportData`.

- `html_report(data, name, lang, link)`: a self-contained HTML document (inline CSS, no
  external resources or scripts, bars as table cells, `dir="rtl"` for Hebrew, Outlook-safe
  tables) — the email body and the `/api/reports/runs/{id}/html` download (≤ 1 MB: tables are
  cut when larger).
- `text_summary`, `subject`, `teams_card`, `telegram_text`, `webhook_payload`."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from netmon.reports import compute

MAX_HTML_BYTES = 1_000_000
TELEGRAM_MAX = 4096

T: dict[str, dict[str, str]] = {
    "en": {
        "weekly": "Weekly report",
        "monthly": "Monthly report",
        "none": "Report",
        "period": "Period",
        "scope": "Scope",
        "generated": "Generated",
        "availability": "Availability",
        "overall": "Overall availability",
        "worst": "Worst devices",
        "all_devices": "All devices",
        "groups": "Groups",
        "device": "Device",
        "group": "Group",
        "downtime": "Downtime",
        "outages": "Outages",
        "longest": "Longest outage",
        "devices": "Devices",
        "not_measured": "Not measured",
        "top_alerts": "Top alerts",
        "sensor": "Sensor",
        "notifications": "Notifications",
        "problem_time": "Problem time",
        "flapping_ports": "Flapping ports",
        "port": "Port",
        "episodes": "Episodes",
        "transitions": "Transitions",
        "port_errors": "Port errors",
        "errors": "Errors",
        "source": "Source",
        "config_changes": "Configuration changes",
        "changes": "Changes",
        "last_change": "Last change",
        "lines": "+/− lines",
        "new_devices": "New devices",
        "mac": "MAC",
        "vendor": "Vendor",
        "ip": "IP",
        "switch": "Switch / port",
        "detected": "Detected",
        "acknowledged": "Acknowledged",
        "yes": "yes",
        "no": "no",
        "top_talkers": "Top talkers",
        "hosts": "Hosts",
        "applications": "Applications",
        "bytes": "Bytes",
        "share": "Share",
        "feature_off": "Not included: the feature is turned off.",
        "no_data": "No data in this period.",
        "open": "Open the full report",
        "na": "n/a",
        "alerts": "Problem notifications",
        "config_changed": "Configuration changes",
        "new_devs": "New devices",
        "worst5": "Worst devices",
    },
    "he": {
        "weekly": "דוח שבועי",
        "monthly": "דוח חודשי",
        "none": "דוח",
        "period": "תקופה",
        "scope": "היקף",
        "generated": "הופק",
        "availability": "זמינות",
        "overall": "זמינות כוללת",
        "worst": "ההתקנים הגרועים ביותר",
        "all_devices": "כל ההתקנים",
        "groups": "קבוצות",
        "device": "התקן",
        "group": "קבוצה",
        "downtime": "זמן השבתה",
        "outages": "השבתות",
        "longest": "ההשבתה הארוכה ביותר",
        "devices": "התקנים",
        "not_measured": "לא נמדד",
        "top_alerts": "התראות מובילות",
        "sensor": "חיישן",
        "notifications": "התראות",
        "problem_time": "זמן תקלה",
        "flapping_ports": "פורטים מרפרפים",
        "port": "פורט",
        "episodes": "אירועים",
        "transitions": "מעברים",
        "port_errors": "שגיאות פורטים",
        "errors": "שגיאות",
        "source": "מקור",
        "config_changes": "שינויי תצורה",
        "changes": "שינויים",
        "last_change": "שינוי אחרון",
        "lines": "+/− שורות",
        "new_devices": "התקנים חדשים",
        "mac": "MAC",
        "vendor": "יצרן",
        "ip": "IP",
        "switch": "מתג / פורט",
        "detected": "זוהה",
        "acknowledged": "אושר",
        "yes": "כן",
        "no": "לא",
        "top_talkers": "המשתמשים העיקריים ברשת",
        "hosts": "מארחים",
        "applications": "יישומים",
        "bytes": "בתים",
        "share": "נתח",
        "feature_off": "לא נכלל: התכונה כבויה.",
        "no_data": "אין נתונים בתקופה זו.",
        "open": "פתיחת הדוח המלא",
        "na": "לא זמין",
        "alerts": "התראות תקלה",
        "config_changed": "שינויי תצורה",
        "new_devs": "התקנים חדשים",
        "worst5": "ההתקנים הגרועים ביותר",
    },
}

CSS_TD = "padding:4px 8px;border-bottom:1px solid #e5e7eb;font-size:13px;"
CSS_TH = "padding:4px 8px;border-bottom:2px solid #9ca3af;font-size:13px;font-weight:bold;"


def _t(lang: str) -> dict[str, str]:
    return T.get(lang, T["en"])


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _local(ts: str | None, tz: str, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo(tz)).strftime(fmt)
    except (ValueError, KeyError):
        return ts


def _pct(v: float | None, lang: str) -> str:
    return _t(lang)["na"] if v is None else f"{v:.2f} %"


def _bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000.0
    return f"{n:.1f} PB"


def period_text(data: dict[str, Any]) -> str:
    try:
        s = datetime.fromisoformat(data["period_start"].replace("Z", "+00:00"))
        e = datetime.fromisoformat(data["period_end"].replace("Z", "+00:00"))
        return compute.period_label(s, e, data.get("timezone") or "UTC")
    except (KeyError, ValueError, AttributeError):
        return ""


def subject(data: dict[str, Any], name: str, schedule: str, lang: str) -> str:
    kind = _t(lang).get(schedule, _t(lang)["none"])
    return f"[NetworkMonitor] {kind}: {name} ({period_text(data)})"


def _bar(pct: float | None) -> str:
    """A horizontal bar of table cells (no images; Outlook-safe)."""
    if pct is None:
        return ""
    width = max(0, min(100, int(round(pct))))
    color = "#16a34a" if pct >= 99.9 else "#d97706" if pct >= 99 else "#dc2626"
    cells = f'<td width="{width}" style="background:{color};height:8px;font-size:1px;">&nbsp;</td>'
    if width < 100:
        cells += f'<td width="{100 - width}" style="background:#e5e7eb;font-size:1px;">&nbsp;</td>'
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100" '
        f'style="border-collapse:collapse;"><tr>{cells}</tr></table>'
    )


def _table(headers: list[str], rows: list[list[str]], align_end: set[int] | None = None) -> str:
    align_end = align_end or set()
    head = "".join(f'<th align="left" style="{CSS_TH}">{_e(h)}</th>' for h in headers)
    body = []
    for r in rows:
        cells = "".join(
            f'<td style="{CSS_TD}"{' align="right"' if i in align_end else ""}>{c}</td>'
            for i, c in enumerate(r)
        )
        body.append(f"<tr>{cells}</tr>")
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
        'style="border-collapse:collapse;margin:4px 0 16px 0;">'
        f"<tr>{head}</tr>{''.join(body)}</table>"
    )


def _section_html(sec: str, data: dict[str, Any], lang: str, limit: int) -> str:
    t = _t(lang)
    tz = data.get("timezone") or "UTC"
    v = data.get(sec)
    if sec == "availability":
        out = [f"<p><b>{_e(t['overall'])}: {_e(_pct(v.get('overall'), lang))}</b></p>"]
        devs = v.get("devices") or []
        worst = [d for d in devs if d.get("availability") is not None][:20]

        def row(d: dict[str, Any]) -> list[str]:
            return [
                _e(d["device_name"]),
                _e(d.get("group_name")),
                _e(_pct(d.get("availability"), lang)),
                _bar(d.get("availability")),
                _e(compute.fmt_duration(d.get("downtime_s") or 0)),
                _e(d.get("outages")),
                _e(compute.fmt_duration(d.get("longest_outage_s") or 0)),
            ]

        hdr = [t["device"], t["group"], t["availability"], "", t["downtime"], t["outages"]]
        hdr.append(t["longest"])
        if worst:
            out.append(f"<h3 style='font-size:14px;margin:8px 0 4px 0;'>{_e(t['worst'])}</h3>")
            out.append(_table(hdr, [row(d) for d in worst], {2, 4, 5, 6}))
        groups = v.get("groups") or []
        if groups:
            out.append(f"<h3 style='font-size:14px;margin:8px 0 4px 0;'>{_e(t['groups'])}</h3>")
            out.append(
                _table(
                    [t["group"], t["availability"], "", t["devices"]],
                    [
                        [
                            _e(g["group_name"]),
                            _e(_pct(g.get("availability"), lang)),
                            _bar(g.get("availability")),
                            _e(g["devices"]),
                        ]
                        for g in groups
                    ],
                    {1, 3},
                )
            )
        full = sorted(devs, key=lambda d: str(d["device_name"]).lower())[:limit]
        if full:
            out.append(
                f"<h3 style='font-size:14px;margin:8px 0 4px 0;'>{_e(t['all_devices'])}</h3>"
            )
            out.append(_table(hdr, [row(d) for d in full], {2, 4, 5, 6}))
        nm = v.get("not_measured") or []
        if nm:
            names = ", ".join(_e(x["device_name"]) for x in nm[:limit])
            out.append(
                f"<p style='font-size:12px;color:#6b7280;'>{_e(t['not_measured'])}: {names}</p>"
            )
        return "".join(out)
    if sec == "top_alerts":
        return _table(
            [t["sensor"], t["device"], t["notifications"], t["problem_time"]],
            [
                [
                    _e(r["sensor_name"]),
                    _e(r["device_name"]),
                    _e(r["notifications"]),
                    _e(compute.fmt_duration(r["problem_s"])),
                ]
                for r in v
            ],
            {2, 3},
        )
    if sec == "flapping_ports":
        return _table(
            [t["device"], t["port"], t["episodes"], t["transitions"]],
            [
                [_e(r["device_name"]), _e(r["port_name"]), _e(r["episodes"]), _e(r["transitions"])]
                for r in v
            ],
            {2, 3},
        )
    if sec == "port_errors":
        return _table(
            [t["device"], t["port"], t["errors"], t["source"]],
            [
                [_e(r["device_name"]), _e(r["port_name"]), _e(r["errors"]), _e(r["source"])]
                for r in v
            ],
            {2},
        )
    if sec == "config_changes":
        return _table(
            [t["device"], t["changes"], t["last_change"], t["lines"]],
            [
                [
                    _e(r["device_name"]),
                    _e(r["changes"]),
                    _e(_local(r["last_change_at"], tz)),
                    _e(f"+{r['added']} −{r['removed']}"),
                ]
                for r in v
            ],
            {1},
        )
    if sec == "new_devices":
        return _table(
            [t["mac"], t["vendor"], t["ip"], t["switch"], t["detected"], t["acknowledged"]],
            [
                [
                    _e(r["mac"]),
                    _e(r.get("vendor")),
                    _e(" ".join(x for x in (r.get("ip"), r.get("hostname")) if x)),
                    _e(" / ".join(x for x in (r.get("switch_name"), r.get("port_name")) if x)),
                    _e(_local(r["detected_at"], tz)),
                    _e(t["yes"] if r["acknowledged"] else t["no"]),
                ]
                for r in v[:limit]
            ],
        )
    if sec == "top_talkers":
        parts = []
        for key, title in (("hosts", t["hosts"]), ("applications", t["applications"])):
            items = v.get(key) or []
            if items:
                parts.append(f"<h3 style='font-size:14px;margin:8px 0 4px 0;'>{_e(title)}</h3>")
                parts.append(
                    _table(
                        [title, t["bytes"], t["share"]],
                        [
                            [
                                _e(i["label"]),
                                _e(_bytes(i["bytes"])),
                                _e(f"{i['share'] * 100:.1f} %"),
                            ]
                            for i in items
                        ],
                        {1, 2},
                    )
                )
        return "".join(parts)
    return ""


def html_report(
    data: dict[str, Any], name: str, lang: str, schedule: str = "none", link: str | None = None
) -> str:
    for limit in (2000, 500, 100, 20):
        doc = _html(data, name, lang, schedule, link, limit)
        if len(doc.encode("utf-8")) <= MAX_HTML_BYTES:
            return doc
    return doc


def _html(
    data: dict[str, Any], name: str, lang: str, schedule: str, link: str | None, limit: int
) -> str:
    t = _t(lang)
    rtl = lang == "he"
    direction = "rtl" if rtl else "ltr"
    tz = data.get("timezone") or "UTC"
    notes = {n["section"]: n["note"] for n in data.get("notes") or []}
    title = f"{t.get(schedule, t['none'])}: {name}"
    parts = [
        "<!DOCTYPE html>",
        f'<html lang="{_e(lang)}" dir="{direction}"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_e(title)}</title>",
        "<style>@media print{h2{page-break-before:auto}.section{page-break-inside:auto}"
        " .section+.section{page-break-before:always}}</style>",
        "</head>",
        f'<body dir="{direction}" style="margin:0;padding:16px;background:#ffffff;'
        "font-family:Segoe UI,Arial,sans-serif;color:#111827;"
        f'text-align:{"right" if rtl else "left"};">',
        f'<h1 style="font-size:20px;margin:0 0 8px 0;">{_e(title)}</h1>',
        f'<p style="font-size:13px;color:#374151;margin:0 0 16px 0;">{_e(t["period"])}: '
        f"{_e(period_text(data))} &middot; {_e(t['scope'])}: {_e(data.get('scope_label'))}"
        f" &middot; {_e(t['generated'])}: {_e(_local(data.get('generated_at'), tz))}</p>",
    ]
    if link:
        parts.append(
            f'<p style="margin:0 0 16px 0;"><a href="{_e(link)}" style="color:#2563eb;">'
            f"{_e(t['open'])}</a></p>"
        )
    for sec in data.get("sections") or []:
        parts.append('<div class="section">')
        parts.append(
            f'<h2 style="font-size:16px;margin:16px 0 4px 0;border-bottom:1px solid #d1d5db;">'
            f"{_e(t.get(sec, sec))}</h2>"
        )
        if sec in notes or data.get(sec) is None:
            note = notes.get(sec, "no_data")
            parts.append(f'<p style="font-size:13px;color:#6b7280;">{_e(t[note])}</p>')
        else:
            parts.append(_section_html(sec, data, lang, limit))
        parts.append("</div>")
    parts.append("</body></html>")
    return "\n".join(parts)


# ---------------------------------------------------------------- summaries
def headline(data: dict[str, Any]) -> dict[str, Any]:
    av = data.get("availability") or {}
    worst = [d for d in av.get("devices") or [] if d.get("availability") is not None][:5]
    return {
        "overall": av.get("overall") if av else None,
        "worst": [(d["device_name"], d["availability"]) for d in worst],
        "alerts": sum(int(r.get("notifications") or 0) for r in data.get("top_alerts") or []),
        "config_changes": sum(int(r.get("changes") or 0) for r in data.get("config_changes") or []),
        "new_devices": len(data.get("new_devices") or []),
    }


def text_summary(
    data: dict[str, Any], name: str, lang: str, schedule: str, link: str | None
) -> str:
    t = _t(lang)
    h = headline(data)
    lines = [
        f"{t.get(schedule, t['none'])}: {name}",
        f"{t['period']}: {period_text(data)}",
        f"{t['scope']}: {data.get('scope_label') or ''}",
        "",
    ]
    secs = data.get("sections") or []
    if "availability" in secs:
        lines.append(f"{t['overall']}: {_pct(h['overall'], lang)}")
        for dname, pct in h["worst"]:
            lines.append(f"  - {dname}: {_pct(pct, lang)}")
    if "top_alerts" in secs:
        lines.append(f"{t['alerts']}: {h['alerts']}")
    if "config_changes" in secs:
        lines.append(f"{t['config_changed']}: {h['config_changes']}")
    if "new_devices" in secs:
        lines.append(f"{t['new_devs']}: {h['new_devices']}")
    if link:
        lines += ["", f"{t['open']}: {link}"]
    return "\n".join(lines)


def telegram_text(
    data: dict[str, Any], name: str, lang: str, schedule: str, link: str | None
) -> str:
    text = text_summary(data, name, lang, schedule, link)
    if len(text) > TELEGRAM_MAX:
        text = text[: TELEGRAM_MAX - 1] + "…"
    return text


def teams_card(
    data: dict[str, Any], name: str, lang: str, schedule: str, link: str | None
) -> dict[str, Any]:
    t = _t(lang)
    h = headline(data)
    secs = data.get("sections") or []
    facts = [{"title": t["period"], "value": period_text(data)}]
    if "availability" in secs:
        facts.append({"title": t["overall"], "value": _pct(h["overall"], lang)})
        if h["worst"]:
            facts.append(
                {
                    "title": t["worst5"],
                    "value": "\n".join(f"{n}: {_pct(p, lang)}" for n, p in h["worst"]),
                }
            )
    if "top_alerts" in secs:
        facts.append({"title": t["alerts"], "value": str(h["alerts"])})
    if "config_changes" in secs:
        facts.append({"title": t["config_changed"], "value": str(h["config_changes"])})
    if "new_devices" in secs:
        facts.append({"title": t["new_devs"], "value": str(h["new_devices"])})
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5" if lang == "he" else "1.4",
        "msteams": {"width": "Full"},
        "body": [
            {
                "type": "TextBlock",
                "text": f"{t.get(schedule, t['none'])}: {name}",
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            {"type": "FactSet", "facts": facts},
        ],
    }
    if lang == "he":
        card["rtl"] = True
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": t["open"], "url": link}]
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }
