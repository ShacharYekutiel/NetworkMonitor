"""[premium] Home screen: the network map (`GET /api/overview/map`) and its setting
(`GET/PUT /api/settings/home-map`). Read-only towards devices; no SNMP."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from netmon.api.deps import DB, Admin, Audit, Viewer
from netmon.api.errors import not_found, validation_error
from netmon.services import audit
from netmon.services import home_map as hm
from netmon.services import settings as settings_svc
from netmon.services.status import load_inventory

router = APIRouter()


class HomeMapIn(BaseModel):
    """Omitted keys keep their current value."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["auto", "backbone"] | None = None
    show_internet: bool | None = None
    backbone_device_ids: Annotated[list[int], Field(max_length=hm.MAX_BACKBONE)] | None = None
    max_groups: Annotated[int, Field(ge=hm.MAX_GROUPS[0], le=hm.MAX_GROUPS[1])] | None = None


async def _setting(session: DB) -> dict:
    return hm.normalize(await settings_svc.get(session, hm.KEY))


@router.get("/api/settings/home-map")
async def get_home_map(_: Viewer, session: DB) -> dict:
    return await _setting(session)


@router.put("/api/settings/home-map")
async def put_home_map(body: HomeMapIn, _: Admin, session: DB, ctx: Audit) -> dict:
    current = await _setting(session)
    new = dict(current)
    for key in body.model_fields_set:
        value = getattr(body, key)
        if value is not None:
            new[key] = value
    ids = list(new["backbone_device_ids"])
    if len(set(ids)) != len(ids):
        raise validation_error(["body", "backbone_device_ids"], "Duplicate device ids")
    if "backbone_device_ids" in body.model_fields_set and ids:
        known = {
            r.id
            for r in await session.execute(
                text("SELECT id FROM devices WHERE id = ANY(:i)"), {"i": ids}
            )
        }
        for pos, did in enumerate(ids):
            if did not in known:
                raise validation_error(
                    ["body", "backbone_device_ids", pos], f"Device {did} does not exist"
                )
    new = hm.normalize(new)
    await settings_svc.put(session, hm.KEY, new)
    await audit.record_settings(session, ctx, {hm.KEY: current}, {hm.KEY: new})
    await session.commit()
    return new


@router.get("/api/overview/map")
async def overview_map(
    user: Viewer,
    session: DB,
    group_id: int | None = Query(default=None),
) -> dict:
    inv = await load_inventory(session)
    if group_id is not None and group_id not in inv.groups:
        raise not_found("Group")
    setting = await _setting(session)
    wanted = [d for d in setting["backbone_device_ids"] if d in inv.devices]
    if group_id is not None:
        wanted += inv.group_devices.get(group_id, [])
    builder = hm.Builder(
        inv,
        await hm.load_problems(session, inv),
        await hm.load_identities(session, wanted),
        await hm.load_links(session) if setting["mode"] == "auto" else [],
        getattr(user, "language", "en") or "en",
    )
    name = inv.groups[group_id]["name"] if group_id is not None else ""
    return builder.build(group_id, name, setting)
