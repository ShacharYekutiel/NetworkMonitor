"""[3] Flow settings keys (data-model "[3] Settings keys"): defaults, the `PUT /api/settings`
fields, the API output, and what changes at runtime after a save."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

DEFAULTS: dict[str, Any] = {
    "flow_max_records_per_s": 10000,
    "flow_top_conversations": 500,
    "flow_top_hosts": 200,
    "flow_top_apps": 100,
    "flow_raw_retention_hours": 24,
    "flow_minute_retention_hours": 48,
    "flow_rollup_retention_days": 30,
    "flow_app_overrides": [],
}
KEYS = tuple(DEFAULTS)
RETENTION_KEYS = (
    "flow_raw_retention_hours",
    "flow_minute_retention_hours",
    "flow_rollup_retention_days",
)
RUNTIME_KEYS = (
    "flow_max_records_per_s",
    "flow_top_conversations",
    "flow_top_hosts",
    "flow_top_apps",
    "flow_app_overrides",
)


class AppOverride(BaseModel):
    protocol: int = Field(ge=0, le=255)
    port: int = Field(ge=0, le=65535)
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v


class FlowSettingsMixin(BaseModel):
    """Fields of `PUT /api/settings` (all optional; null/omitted keeps the stored value)."""

    flow_max_records_per_s: int | None = Field(default=None, ge=1000, le=50000)
    flow_top_conversations: int | None = Field(default=None, ge=50, le=5000)
    flow_top_hosts: int | None = Field(default=None, ge=20, le=2000)
    flow_top_apps: int | None = Field(default=None, ge=10, le=1000)
    flow_raw_retention_hours: int | None = Field(default=None, ge=1, le=168)
    flow_minute_retention_hours: int | None = Field(default=None, ge=24, le=336)
    flow_rollup_retention_days: int | None = Field(default=None, ge=7, le=400)
    flow_app_overrides: list[AppOverride] | None = Field(default=None, max_length=200)


def values_of(stored: dict[str, Any]) -> dict[str, Any]:
    """The flow keys of a settings dict (defaults for missing keys)."""
    return {k: copy.deepcopy(stored.get(k, v)) for k, v in DEFAULTS.items()}


def settings_out(values: dict[str, Any]) -> dict[str, Any]:
    return values_of(values)


async def load(session: AsyncSession) -> dict[str, Any]:
    from sqlalchemy import select

    from netmon.models import Setting

    rows = (await session.execute(select(Setting).where(Setting.key.in_(KEYS)))).scalars()
    stored = {r.key: r.value for r in rows}
    return values_of(stored)


async def put(body: Any, fields: set[str], session: AsyncSession) -> None:
    """Store the flow keys present in a `PUT /api/settings` body (same transaction)."""
    from netmon.services import settings as settings_svc

    for key in KEYS:
        if key not in fields:
            continue
        value = getattr(body, key, None)
        if value is None:
            continue
        if key == "flow_app_overrides":
            value = [o.model_dump() if hasattr(o, "model_dump") else dict(o) for o in value]
        await settings_svc.put(session, key, value)


async def after_put(rt: Any, before: dict[str, Any], after: dict[str, Any]) -> None:
    """After the commit: retention policies and the collector's runtime configuration."""
    flows = getattr(rt, "flows", None)
    if flows is None:
        return
    if any(before.get(k) != after.get(k) for k in RETENTION_KEYS):
        await flows.retention_changed(values_of(after))
    if any(before.get(k) != after.get(k) for k in RUNTIME_KEYS):
        flows.configure(values_of(after))
