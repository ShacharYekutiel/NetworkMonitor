"""Telegram Bot API sender (sendMessage). The bot token never appears in errors or logs."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from netmon import __version__

log = logging.getLogger("netmon.alerts.telegram")

DEFAULT_API_BASE = "https://api.telegram.org"
TIMEOUT_S = 10.0
_api_base = DEFAULT_API_BASE

# httpx/httpcore log request URLs (which contain the token) at INFO/DEBUG
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class TelegramError(Exception):
    pass


def configure(api_base: str | None) -> None:
    global _api_base
    _api_base = (api_base or DEFAULT_API_BASE).rstrip("/")


def api_base() -> str:
    return _api_base


def scrub(text: str, token: str | None) -> str:
    if token:
        text = text.replace(token, "***")
    return text


async def send_telegram(config: dict[str, Any], text: str, parse_mode: str | None = "HTML") -> None:
    """POST {base}/bot{token}/sendMessage. Raises TelegramError with a token-free message."""
    token = str(config.get("bot_token") or "")
    if not token:
        raise TelegramError("No bot token configured")
    body: dict[str, Any] = {
        "chat_id": str(config.get("chat_id") or ""),
        "text": text,
        "disable_web_page_preview": True,
        "disable_notification": bool(config.get("disable_notification", False)),
    }
    if parse_mode:
        body["parse_mode"] = parse_mode
    url = f"{_api_base}/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.post(
                url, json=body, headers={"User-Agent": f"NetworkMonitor/{__version__}"}
            )
    except httpx.TimeoutException:
        raise TelegramError("Telegram request timed out") from None
    except Exception as exc:  # noqa: BLE001
        raise TelegramError(scrub(str(exc) or type(exc).__name__, token)) from None
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        if data.get("ok") is True and resp.status_code == 200:
            return
        if data.get("ok") is False or "description" in data:
            desc = str(data.get("description") or f"Telegram returned HTTP {resp.status_code}")
            raise TelegramError(scrub(desc, token))
    raise TelegramError(f"Telegram returned HTTP {resp.status_code}")
