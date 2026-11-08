"""[3] Settings keys of SSO, reports, NOC and chat acknowledge (data-model § [3] Settings keys).

`get(session, key)` reads the stored value, falling back to the code default, independent of
whether the key is registered in `netmon.services.settings.DEFAULTS` (stored objects are merged
over their defaults, like `smtp`/`ldap`)."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

DEFAULTS: dict[str, Any] = {
    "kerberos": {
        "realms": [],
        "auto_login": True,
        "keytab_enc": None,
        "keytab_uploaded_at": None,
    },
    "oidc": {
        "tenant_id": "",
        "client_id": "",
        "client_secret_enc": None,
        "client_secret_expires_at": None,
        "role_source": "app_roles",
        "admin_values": ["NetMon.Admin"],
        "viewer_values": ["NetMon.Viewer"],
        "button_label": None,
    },
    "report_retention_days": 400,
    "chat_ack_ttl_h": 168,
    "telegram_poll_timeout_s": 50,
}
OBJECTS = ("kerberos", "oidc")


async def get(session: AsyncSession, key: str) -> Any:
    from netmon.models import Setting

    row = await session.get(Setting, key)
    default = copy.deepcopy(DEFAULTS[key])
    if row is None:
        return default
    if key in OBJECTS and isinstance(row.value, dict):
        return {**default, **row.value}
    return row.value


async def put(session: AsyncSession, key: str, value: Any) -> None:
    from netmon.services import settings as settings_svc

    await settings_svc.put(session, key, value)


async def public_base_url(session: AsyncSession) -> str | None:
    from netmon.models import Setting

    row = await session.get(Setting, "public_base_url")
    v = row.value if row is not None else None
    return v.rstrip("/") if isinstance(v, str) and v.strip() else None


async def timezone(session: AsyncSession) -> str:
    from netmon.services import settings as settings_svc

    return str(await settings_svc.get(session, "timezone") or "UTC")
