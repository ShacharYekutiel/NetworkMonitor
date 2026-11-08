"""[2b-3] Microsoft Teams channel: an Adaptive Card POSTed to a Teams *Workflows* webhook
("Post to a channel when a webhook request is received"). The URL carries a signature, so it is
a secret: it never appears in errors, logs or the audit log (api.md § Teams)."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from netmon import __version__
from netmon.alerts import templates
from netmon.tlsutil import httpx_verify

log = logging.getLogger("netmon.alerts.teams")

TIMEOUT_S = 15.0
MAX_CARD_BYTES = 24 * 1024
LEGACY_HOST_SUFFIXES = ("webhook.office.com", "outlook.office.com")
OFF_MESSAGE = "Teams notifications are turned off"

TEXTS = {
    "en": {
        "status": "Status",
        "device": "Device",
        "sensor": "Sensor",
        "message": "Message",
        "since": "Since",
        "rule": "Rule",
        "stage": "Stage",
        "group": "Group",
        "hosts": "Hosts",
        "escalation": "ESCALATION",
        "reminder": "REMINDER",
        "recovered": "RECOVERED",
        "stage_escalation": "Escalation",
        "stage_reminder": "Reminder",
        "after_maintenance": " (after maintenance)",
        "after_flapping": " (after flapping)",
        "open": "Open in NetworkMonitor",
        "acknowledge": "Acknowledge",
        "test": "NetworkMonitor test message",
        "new_hosts": "New hosts discovered: {name} ({count})",
    },
    "he": {
        "status": "סטטוס",
        "device": "התקן",
        "sensor": "חיישן",
        "message": "הודעה",
        "since": "מאז",
        "rule": "כלל",
        "stage": "שלב",
        "group": "קבוצה",
        "hosts": "מארחים",
        "escalation": "הסלמה",
        "reminder": "תזכורת",
        "recovered": "חזר לתקין",
        "stage_escalation": "הסלמה",
        "stage_reminder": "תזכורת",
        "after_maintenance": " (לאחר תחזוקה)",
        "after_flapping": " (לאחר ריפרוף)",
        "open": "פתיחה ב-NetworkMonitor",
        "acknowledge": "אישור טיפול",
        "test": "הודעת בדיקה מ-NetworkMonitor",
        "new_hosts": "התגלו מארחים חדשים: {name} ({count})",
    },
}


class TeamsError(Exception):
    pass


def url_host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return (urlsplit(url).hostname or "").lower() or None
    except ValueError:
        return None


def is_legacy(url: str | None) -> bool:
    host = url_host(url) or ""
    return any(host == s or host.endswith("." + s) for s in LEGACY_HOST_SUFFIXES)


def config_out(config: dict[str, Any]) -> dict[str, Any]:
    """API shape of a Teams channel config (the URL is write-only)."""
    url = config.get("url")
    return {
        "url": None,
        "url_set": bool(url),
        "url_host": url_host(url),
        "legacy": is_legacy(url),
        "ack_link": bool(config.get("ack_link", True)),  # [3]
    }


def _color(payload: dict[str, Any]) -> str:
    stage = payload.get("stage")
    if stage == "recovery" or payload.get("new_status") == "up":
        return "Good"
    if stage in ("escalation", "reminder") or payload.get("new_status") == "warning":
        return "Warning"
    return "Attention"


def _status(value: str | None, lang: str) -> str:
    if not value:
        return ""
    if lang == "en":
        return value
    return templates.STATUS_NAMES[lang].get(value, value)


def title(payload: dict[str, Any], lang: str) -> str:
    t = TEXTS[lang]
    if payload.get("event") == "test":
        return t["test"]
    if payload.get("event") == "discovery.new_hosts":
        return t["new_hosts"].format(
            name=(payload.get("profile") or {}).get("name", "?"),
            count=templates.new_hosts_count(payload),
        )
    device = (payload.get("device") or {}).get("name", "?")
    sensor = (payload.get("sensor") or {}).get("name", "?")
    new = payload.get("new_status") or ""
    status = templates.STATUS_NAMES["en"].get(new, new.upper())
    if lang != "en":
        status = templates.STATUS_NAMES[lang].get(new, status)
    stage = payload.get("stage")
    if stage == "recovery" or new == "up":
        head = t["recovered"]
    elif stage in ("escalation", "reminder"):
        head = f"{t[stage]}: {status}"
    else:
        head = status
    suffix = ""
    if payload.get("after_maintenance"):
        suffix += t["after_maintenance"]
    if payload.get("after_flapping"):
        suffix += t["after_flapping"]
    return f"{head}{suffix}: {device} / {sensor}"


def _facts(payload: dict[str, Any], lang: str) -> list[dict[str, str]]:
    t = TEXTS[lang]
    facts: list[dict[str, str]] = []
    if payload.get("event") == "test":
        return facts
    if payload.get("event") == "discovery.new_hosts":
        if payload.get("group"):
            facts.append({"title": t["group"], "value": str(payload["group"]["name"])})
        facts.append({"title": t["message"], "value": str(payload.get("message") or "")})
        lines = templates.host_lines(payload, lang)
        if lines:
            facts.append({"title": t["hosts"], "value": "\n".join(lines)})
        return facts
    old, new = payload.get("old_status"), payload.get("new_status")
    if new:
        value = f"{_status(old, lang)} → {_status(new, lang)}" if old else _status(new, lang)
        facts.append({"title": t["status"], "value": value})
    dev = payload.get("device")
    if dev:
        host = dev.get("host")
        facts.append(
            {"title": t["device"], "value": f"{dev['name']} ({host})" if host else str(dev["name"])}
        )
    if payload.get("sensor"):
        facts.append({"title": t["sensor"], "value": str(payload["sensor"]["name"])})
    if payload.get("message"):
        facts.append({"title": t["message"], "value": str(payload["message"])})
    since = payload.get("problem_since") or payload.get("time")
    if since:
        facts.append({"title": t["since"], "value": templates.format_local(since)})
    if payload.get("rule"):
        facts.append({"title": t["rule"], "value": str(payload["rule"]["name"])})
    if payload.get("stage") in ("escalation", "reminder"):
        facts.append({"title": t["stage"], "value": t[f"stage_{payload['stage']}"]})
    return facts


def _wrap(card: dict[str, Any]) -> dict[str, Any]:
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


def _size(body: dict[str, Any]) -> int:
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def build_card(payload: dict[str, Any], lang: str) -> dict[str, Any]:
    """Pure: the full request body (message with one Adaptive Card attachment), ≤ 24 KB —
    the message fact is cut first, then the other long facts."""
    lang = lang if lang in TEXTS else "en"
    t = TEXTS[lang]
    color = "Good" if payload.get("event") == "test" else _color(payload)
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5" if lang == "he" else "1.4",
        "msteams": {"width": "Full"},
    }
    if lang == "he":
        card["rtl"] = True
    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": title(payload, lang),
            "weight": "Bolder",
            "size": "Medium",
            "color": color,
            "wrap": True,
        }
    ]
    facts = _facts(payload, lang)
    if facts:
        body.append({"type": "FactSet", "facts": facts})
    card["body"] = body
    actions: list[dict[str, Any]] = []
    if payload.get("ack_url"):  # [3] chat_ack: the ack page link (Action.Submit cannot work)
        actions.append(
            {"type": "Action.OpenUrl", "title": t["acknowledge"], "url": payload["ack_url"]}
        )
    if payload.get("url"):
        actions.append({"type": "Action.OpenUrl", "title": t["open"], "url": payload["url"]})
    if actions:
        card["actions"] = actions
    out = _wrap(card)
    if _size(out) <= MAX_CARD_BYTES:
        return out
    # cut the longest facts (message first) until it fits
    order = sorted(
        range(len(facts)),
        key=lambda i: (facts[i]["title"] != t["message"], -len(facts[i]["value"])),
    )
    for i in order:
        excess = _size(out) - MAX_CARD_BYTES
        if excess <= 0:
            break
        value = facts[i]["value"]
        keep = max(0, len(value.encode("utf-8")) - excess - 16)
        cut = value.encode("utf-8")[:keep].decode("utf-8", "ignore")
        facts[i]["value"] = cut + "…"
    if _size(out) > MAX_CARD_BYTES:  # pathological title
        body[0]["text"] = body[0]["text"][:200] + "…"
    return out


async def send_teams(config: dict[str, Any], body: dict[str, Any]) -> None:
    """POST the card. 2xx (Workflows answers 202) = success; else TeamsError with the HTTP
    status and the first 200 chars of the response body (never the URL)."""
    url = config.get("url")
    if not url:
        raise TeamsError("No Teams webhook URL configured")
    headers = {"Content-Type": "application/json", "User-Agent": f"NetworkMonitor/{__version__}"}
    try:
        async with httpx.AsyncClient(
            verify=httpx_verify(True), timeout=TIMEOUT_S, trust_env=True
        ) as client:
            resp = await client.post(
                url,
                content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers=headers,
            )
    except httpx.TimeoutException:
        raise TeamsError("Teams request timed out") from None
    except httpx.HTTPError as exc:
        raise TeamsError(
            _scrub(f"Teams request failed: {type(exc).__name__}: {exc}", url)
        ) from None
    except (ValueError, TypeError) as exc:
        raise TeamsError(_scrub(f"Invalid Teams webhook URL: {exc}", url)) from None
    if not (200 <= resp.status_code < 300):
        text = _scrub(resp.text[:200], url)
        raise TeamsError(f"HTTP {resp.status_code}: {text}".strip())


def _scrub(text: str, url: str) -> str:
    """Never echo the signed URL (or its query string)."""
    if url:
        text = text.replace(url, "<teams-url>")
        query = urlsplit(url).query
        if query:
            text = text.replace(query, "…")
    return text


async def send(config: dict[str, Any], payload: dict[str, Any], lang: str) -> None:
    if payload.get("ack_url") and not config.get("ack_link", True):  # [3] default true
        payload = {**payload, "ack_url": None}
    await send_teams(config, build_card(payload, lang))
