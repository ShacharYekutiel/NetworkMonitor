"""Create devices from discovery results and apply templates."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from netmon.discovery.runner import EXISTING_DEVICE_SQL
from netmon.discovery.serialize import ip_text
from netmon.models import Device
from netmon.poller.snmp_client import resolve_credentials
from netmon.services.sensors import group_chain
from netmon.services.serialize import SNMP_FIELDS
from netmon.templates.apply import apply_template
from netmon.templates.builtin import get_template

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.discovery.import")

CONCURRENCY = 8


def device_credentials(
    result: Any,
    scan_credentials: list[dict[str, Any]],
    snmp_port_override: int | None,
    group_effective_port: int,
) -> dict[str, Any] | None:
    """None = inherit from the group; else the device's own credentials."""
    if not result.snmp_ok:
        return None
    idx = result.snmp_credential_index
    if idx is None or idx >= len(scan_credentials):
        return None
    if idx == 0 and (snmp_port_override is None or snmp_port_override == group_effective_port):
        return None
    stored = scan_credentials[idx]
    creds = {k: stored.get(k) for k in SNMP_FIELDS}
    creds["timeout_s"] = 2
    creds["retries"] = 1
    return creds


async def import_results(
    rt: Runtime,
    scan: Any,
    group_id: int,
    items: list[dict[str, Any]],
    lang: str,
) -> dict[str, Any]:
    async with rt.sessionmaker() as session:
        chain = await group_chain(session, group_id)
    group_creds, _, _ = resolve_credentials(None, chain)
    group_port = int(group_creds.get("port") or 161)
    port_override = (scan.options or {}).get("snmp_port")
    credentials = list(scan.credentials or [])
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        async with sem:
            try:
                return await _import_one(
                    rt, scan.id, group_id, item, lang, credentials, port_override, group_port
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Import of discovery result %s failed", item["result_id"])
                return "error", {
                    "result_id": item["result_id"],
                    "code": "internal_error",
                    "message": str(exc)[:500] or "Internal error",
                    "device_id": None,
                }

    outcomes = await asyncio.gather(*(one(i) for i in items))
    return {
        "created": [o for kind, o in outcomes if kind == "created"],
        "errors": [o for kind, o in outcomes if kind == "error"],
    }


async def _import_one(
    rt: Runtime,
    scan_id: int,
    group_id: int,
    item: dict[str, Any],
    lang: str,
    credentials: list[dict[str, Any]],
    port_override: int | None,
    group_port: int,
) -> tuple[str, dict[str, Any]]:
    rid = item["result_id"]
    async with rt.sessionmaker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT r.*, "
                    + EXISTING_DEVICE_SQL.format(ip="r.ip", hn="r.hostname")
                    + " AS existing_device_id FROM discovery_results r "
                    "WHERE r.id = :id AND r.scan_id = :sid"
                ),
                {"id": rid, "sid": scan_id},
            )
        ).first()
        if row is None:
            return "error", {
                "result_id": rid,
                "code": "not_found",
                "message": "Result not found in this scan",
                "device_id": None,
            }
        if row.existing_device_id is not None:
            return "error", {
                "result_id": rid,
                "code": "device_exists",
                "message": "A device with this host already exists",
                "device_id": row.existing_device_id,
            }
        ip = ip_text(row.ip)
        name = (item.get("name") or row.sys_name or row.hostname or ip).strip()[:128] or ip
        device = Device(
            group_id=group_id,
            name=name,
            host=ip,
            description=(row.sys_descr or "")[:500],
            enabled=True,
            snmp_credentials=device_credentials(row, credentials, port_override, group_port),
            sort_order=0,
        )
        session.add(device)
        await session.commit()
        device_id = device.id
    rt.config_changed("device", device_id, "created")
    template = get_template(item["template_id"])
    assert template is not None
    result = await apply_template(rt, device_id, template, lang, broadcast=False)
    async with rt.sessionmaker() as session:
        await session.execute(
            text("UPDATE discovery_results SET imported_device_id = :d WHERE id = :id"),
            {"d": device_id, "id": rid},
        )
        await session.commit()
    return "created", {
        "result_id": rid,
        "device_id": device_id,
        "device_name": name,
        "template_id": item["template_id"],
        "sensors_created": len(result["created"]),
        "sensors_skipped": result["skipped"],
    }
