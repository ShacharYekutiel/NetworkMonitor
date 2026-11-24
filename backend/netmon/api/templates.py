"""[2a] Device templates and apply-template; [2b-2] library status and reload."""

from __future__ import annotations

from fastapi import APIRouter, Query

from netmon.api.deps import DB, RT, Admin, Audit, Viewer
from netmon.api.errors import not_found, validation_error
from netmon.models import Device
from netmon.schemas import ApplyTemplateRequest
from netmon.templates import registry
from netmon.templates.apply import apply_template
from netmon.templates.builtin import get_template, list_templates
from netmon.templates.replaces import replace_candidates
from netmon.templates.selection import REFRESH_WAIT_S, interface_choices

router = APIRouter()


@router.get("/api/device-templates")
async def device_templates(user: Viewer) -> dict:
    reg = registry.current()
    return {
        "items": list_templates(),
        "library": registry.library_status(reg, user.role == "admin"),
    }


@router.post("/api/device-templates/reload")
async def reload_templates(_: Admin, rt: RT) -> dict:
    reg = await registry.reload(rt)
    return registry.library_status(reg, True)


@router.get("/api/device-templates/{template_id}")
async def device_template(template_id: str, _: Viewer) -> dict:
    t = get_template(template_id)
    if t is None:
        raise not_found("Template")
    return t


@router.post("/api/devices/{device_id}/apply-template")
async def apply_device_template(
    device_id: int, body: ApplyTemplateRequest, user: Admin, rt: RT, session: DB, ctx: Audit
) -> dict:
    if await session.get(Device, device_id) is None:
        raise not_found("Device")
    template = get_template(body.template_id)
    if template is None:
        raise validation_error(["body", "template_id"], "unknown template")
    lang = user.language
    await session.close()
    return await apply_template(
        rt,
        device_id,
        template,
        lang,
        audit_ctx=ctx,
        interfaces=body.interfaces,
        remove_unselected=body.remove_unselected_interfaces,
        refresh_s=REFRESH_WAIT_S,
        remove_sensor_ids=body.remove_sensor_ids,
    )


@router.get("/api/devices/{device_id}/apply-template/replaces")
async def apply_template_replaces(
    device_id: int,
    _: Admin,
    session: DB,
    template_id: str = Query(..., max_length=64),
) -> dict:
    """[template-switch] The device's sensors this template supersedes (the apply dialog
    offers to delete them; `failing` ones are pre-ticked). No SNMP: database only."""
    if await session.get(Device, device_id) is None:
        raise not_found("Device")
    template = get_template(template_id)
    if template is None:
        raise validation_error(["query", "template_id"], "unknown template")
    return {"items": await replace_candidates(session, device_id, template)}


@router.get("/api/devices/{device_id}/apply-template/interfaces")
async def apply_template_interfaces(
    device_id: int,
    _: Admin,
    rt: RT,
    session: DB,
    template_id: str = Query(..., max_length=64),
    refresh: bool = True,
) -> dict:
    """[1.3] The interfaces the apply dialog offers, with their importance. [if-noise]
    `refresh=false` (the dialog's preview count): never starts a port inventory run."""
    if await session.get(Device, device_id) is None:
        raise not_found("Device")
    template = get_template(template_id)
    if template is None:
        raise validation_error(["query", "template_id"], "unknown template")
    await session.close()
    return await interface_choices(
        rt, device_id, template, refresh_s=REFRESH_WAIT_S if refresh else 0.0
    )
