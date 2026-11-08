"""Runtime settings stored in the `settings` table, with code defaults."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.models import Setting

DEFAULTS: dict[str, Any] = {
    "smtp": {
        "host": "",
        "port": 587,
        "security": "starttls",
        "username": None,
        "password": None,
        "from_address": "",
        "from_name": "NetworkMonitor",
    },
    "retention_days": 90,
    "event_retention_days": 365,
    "default_interval_s": 60,
    "default_language": "en",
    "public_base_url": None,
    # [2b-1]; the timezone default is computed (TZ when valid, else UTC), see defaults()
    "timezone": None,
    "topology_refresh_min": 30,
}


def defaults() -> dict[str, Any]:
    from netmon.services.maintenance import default_timezone

    out = copy.deepcopy(DEFAULTS)
    out["timezone"] = default_timezone()
    return out


async def get_all(session: AsyncSession) -> dict[str, Any]:
    out = defaults()
    for row in (await session.execute(select(Setting))).scalars():
        if row.key == "smtp" and isinstance(row.value, dict):
            out["smtp"] = {**out["smtp"], **row.value}
        elif row.key in out:
            out[row.key] = row.value
    return out


async def get(session: AsyncSession, key: str) -> Any:
    row = await session.get(Setting, key)
    if row is None:
        return defaults()[key]
    if key == "smtp":
        return {**DEFAULTS["smtp"], **row.value}
    return row.value


async def put(session: AsyncSession, key: str, value: Any) -> None:
    stmt = insert(Setting).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(index_elements=[Setting.key], set_={"value": value})
    await session.execute(stmt)


async def ensure_defaults(session: AsyncSession) -> None:
    existing = set((await session.execute(select(Setting.key))).scalars())
    for key, value in defaults().items():
        if key not in existing:
            await put(session, key, copy.deepcopy(value))


def settings_out(values: dict[str, Any]) -> dict[str, Any]:
    smtp = dict(values["smtp"])
    smtp["password_set"] = bool(smtp.get("password"))
    smtp["password"] = None
    return {
        "smtp": {
            k: smtp.get(k)
            for k in (
                "host",
                "port",
                "security",
                "username",
                "password",
                "password_set",
                "from_address",
                "from_name",
            )
        },
        "retention_days": values["retention_days"],
        "event_retention_days": values["event_retention_days"],
        "default_interval_s": values["default_interval_s"],
        "default_language": values["default_language"],
        "public_base_url": values["public_base_url"],
        "timezone": values["timezone"],
        "topology_refresh_min": values["topology_refresh_min"],
    }
