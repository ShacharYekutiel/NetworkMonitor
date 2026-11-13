from __future__ import annotations

from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any

import aiosmtplib

from netmon.tlsutil import client_ssl_context


class SmtpError(Exception):
    pass


async def send_email(
    smtp: dict[str, Any],
    to: list[str],
    subject: str,
    text: str,
    html: str | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    """Send one message; returns its Message-ID. [3b] `headers`: extra headers (the AI note
    reply's In-Reply-To / References)."""
    if not smtp.get("host"):
        raise SmtpError("SMTP server is not configured")
    if not smtp.get("from_address"):
        raise SmtpError("SMTP from address is not configured")
    msg = EmailMessage()
    msg["From"] = formataddr((smtp.get("from_name") or "NetworkMonitor", smtp["from_address"]))
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=smtp["from_address"].split("@")[-1] or None)
    for name, value in (headers or {}).items():
        msg[name] = value
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    security = smtp.get("security", "starttls")
    try:
        await aiosmtplib.send(
            msg,
            hostname=smtp["host"],
            port=int(smtp.get("port") or 587),
            username=smtp.get("username") or None,
            password=smtp.get("password") or None,
            use_tls=security == "ssl",
            start_tls=True if security == "starttls" else False,
            tls_context=client_ssl_context() if security in ("ssl", "starttls") else None,
            timeout=20,
        )
    except (aiosmtplib.SMTPException, OSError, TimeoutError) as exc:
        raise SmtpError(str(exc) or type(exc).__name__) from exc
    return str(msg["Message-ID"])
