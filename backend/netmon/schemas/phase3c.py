"""[3] Settings keys of reports and chat acknowledge in `PUT /api/settings` (data-model § [3]
Settings keys). `kerberos` / `oidc` have their own endpoints (`/api/sso/*`)."""

from __future__ import annotations

from pydantic import BaseModel, Field

PLAIN_KEYS = ("report_retention_days", "chat_ack_ttl_h", "telegram_poll_timeout_s")


class Phase3CSettingsMixin(BaseModel):
    report_retention_days: int | None = Field(default=None, ge=30, le=3650)
    chat_ack_ttl_h: int | None = Field(default=None, ge=1, le=720)
    telegram_poll_timeout_s: int | None = Field(default=None, ge=10, le=50)
