from __future__ import annotations

from typing import Any

import httpx

from netmon import __version__


class WebhookError(Exception):
    pass


async def send_webhook(config: dict[str, Any], payload: dict[str, Any]) -> None:
    headers = {
        **(config.get("headers") or {}),
        "Content-Type": "application/json",
        "User-Agent": f"NetworkMonitor/{__version__}",
    }
    try:
        async with httpx.AsyncClient(
            verify=bool(config.get("verify_tls", True)),
            timeout=float(config.get("timeout_s", 10)),
        ) as client:
            resp = await client.post(config["url"], json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise WebhookError("Webhook request timed out") from exc
    except httpx.HTTPError as exc:
        raise WebhookError(str(exc) or type(exc).__name__) from exc
    except (httpx.InvalidURL, ValueError, KeyError, TypeError) as exc:
        raise WebhookError(f"Invalid webhook URL: {exc}") from exc
    if not (200 <= resp.status_code < 300):
        raise WebhookError(f"Webhook returned HTTP {resp.status_code}")
