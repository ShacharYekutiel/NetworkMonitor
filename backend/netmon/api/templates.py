"""[2a] Device templates (read-only) and apply-template."""

from __future__ import annotations

from fastapi import APIRouter

from netmon.api.deps import DB, RT, Admin, Viewer
from netmon.api.errors import not_found, validation_error
from netmon.models import Device
from netmon.schemas import ApplyTemplateRequest
from netmon.templates.apply import apply_template
from netmon.templates.builtin import get_template, list_templates

router = APIRouter()


@router.get("/api/device-templates")
async def device_templates(_: Viewer) -> dict:
    return {"items": list_templates()}


@router.get("/api/device-templates/{template_id}")
async def device_template(template_id: str, _: Viewer) -> dict:
    t = get_template(template_id)
    if t is None:
        raise not_found("Template")
    return t


@router.post("/api/devices/{device_id}/apply-template")
async def apply_device_template(
    device_id: int, body: ApplyTemplateRequest, user: Admin, rt: RT, session: DB
) -> dict:
    if await session.get(Device, device_id) is None:
        raise not_found("Device")
    template = get_template(body.template_id)
    if template is None:
        raise validation_error(["body", "template_id"], "unknown template")
    lang = user.language
    await session.close()
    return await apply_template(rt, device_id, template, lang)
