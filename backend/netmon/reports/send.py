"""[3] Reports: delivery to email recipients and notification channels (§ 13.6.3).

Targets are `email:<address>` (one message per address, SMTP settings) and `channel:<id>`
(email / teams / telegram / webhook channels). Each target gets one result
`{"target", "ok", "error", "at"}`; errors never contain secrets (the channel senders scrub)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from netmon.reports import render
from netmon.services.serialize import iso

log = logging.getLogger("netmon.reports.send")


def targets_of(report: dict[str, Any]) -> list[str]:
    out = [f"email:{a}" for a in report.get("email_to") or []]
    out += [f"channel:{int(c)}" for c in report.get("channel_ids") or []]
    return out


async def deliver(
    rt: Any,
    targets: list[str],
    data: dict[str, Any],
    html_doc: str,
    run: dict[str, Any],
    report: dict[str, Any],
    link: str | None,
) -> list[dict[str, Any]]:
    from netmon.services import settings as settings_svc

    lang = run.get("language") or "en"
    name = run.get("report_name") or ""
    schedule = report.get("schedule") or "none"
    async with rt.sessionmaker() as session:
        smtp = await settings_svc.get(session, "smtp")
        chan_ids = [int(t.split(":", 1)[1]) for t in targets if t.startswith("channel:")]
        channels: dict[int, Any] = {}
        if chan_ids:
            for ch in await session.execute(
                text(
                    "SELECT id, name, type, enabled, config FROM notification_channels "
                    "WHERE id = ANY(:i)"
                ),
                {"i": chan_ids},
            ):
                channels[ch.id] = ch
    subject = render.subject(data, name, schedule, lang)
    plain = render.text_summary(data, name, lang, schedule, link)
    results: list[dict[str, Any]] = []
    for target in targets:
        error: str | None = None
        try:
            if target.startswith("email:"):
                await _email(smtp, [target.split(":", 1)[1]], subject, plain, html_doc)
            elif target.startswith("channel:"):
                cid = int(target.split(":", 1)[1])
                ch = channels.get(cid)
                if ch is None:
                    raise RuntimeError("The notification channel no longer exists")
                if not ch.enabled:
                    raise RuntimeError("The notification channel is disabled")
                await _channel(rt, ch, smtp, subject, plain, html_doc, data, run, report, link)
            else:
                raise RuntimeError("Unknown target")
        except Exception as exc:  # noqa: BLE001 - one failed target never stops the others
            error = (str(exc) or type(exc).__name__)[:500]
            log.warning("Report delivery to %s failed: %s", target, error)
        results.append(
            {"target": target, "ok": error is None, "error": error, "at": iso(rt.clock.now())}
        )
    return results


async def _email(
    smtp: dict[str, Any], to: list[str], subject: str, plain: str, html_doc: str
) -> None:
    from netmon.alerts.email import send_email

    await send_email(smtp, to, subject, plain, html_doc)


async def _channel(
    rt: Any,
    ch: Any,
    smtp: dict[str, Any],
    subject: str,
    plain: str,
    html_doc: str,
    data: dict[str, Any],
    run: dict[str, Any],
    report: dict[str, Any],
    link: str | None,
) -> None:
    lang = run.get("language") or "en"
    name = run.get("report_name") or ""
    schedule = report.get("schedule") or "none"
    cfg = dict(ch.config or {})
    if ch.type == "email":
        await _email(smtp, list(cfg.get("to") or []), subject, plain, html_doc)
    elif ch.type == "teams":
        from netmon.alerts import teams

        await teams.send_teams(cfg, render.teams_card(data, name, lang, schedule, link))
    elif ch.type == "telegram":
        from netmon.alerts.telegram import send_telegram

        await send_telegram(cfg, render.telegram_text(data, name, lang, schedule, link), None)
    elif ch.type == "webhook":
        from netmon.alerts.webhook import send_webhook

        await send_webhook(
            cfg,
            {
                "event": "report",
                "report": {
                    "id": report.get("id"),
                    "name": name,
                    "schedule": schedule,
                    "language": lang,
                },
                "run": {
                    "id": run.get("id"),
                    "trigger": run.get("trigger"),
                    "period_start": run.get("period_start"),
                    "period_end": run.get("period_end"),
                    "url": link,
                },
                "data": data,
            },
        )
    else:
        raise RuntimeError(f"Channel type {ch.type} cannot receive reports")
