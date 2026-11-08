"""Create devices from discovery results and apply templates."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from netmon.discovery.identify import identity_from_result
from netmon.discovery.runner import EXISTING_DEVICE_SQL
from netmon.discovery.serialize import ip_text
from netmon.models import Device
from netmon.poller.snmp_client import DEFAULT_SNMP, resolve_credentials
from netmon.services.sensors import group_chain
from netmon.services.serialize import SNMP_FIELDS
from netmon.templates.apply import apply_template
from netmon.templates.builtin import get_template

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.discovery.import")

CONCURRENCY = 8
IMPORT_BUDGET_S = float(os.environ.get("NETMON_IMPORT_BUDGET_S") or 90.0)
APPLY_BUDGET_S = 120.0
BUDGET_MESSAGE = "Import time budget exceeded; retry the remaining hosts"
INTERNAL_MESSAGE = "Internal error"
_IGNORED_FIELDS = ("timeout_s", "retries")


def _comparable(creds: dict[str, Any]) -> dict[str, Any]:
    full = {**DEFAULT_SNMP, **creds}
    return {k: full.get(k) for k in SNMP_FIELDS if k not in _IGNORED_FIELDS}


def device_credentials(
    result: Any,
    scan_credentials: list[dict[str, Any]],
    group_effective: dict[str, Any],
) -> dict[str, Any] | None:
    """None = inherit from the import group; else the device's own copy of the credential
    that answered. Inherit only when it was credential 0 and it equals the import group's
    current effective credentials in every field except timeout_s/retries (port included)."""
    if not result.snmp_ok:
        return None
    idx = result.snmp_credential_index
    if idx is None or idx < 0 or idx >= len(scan_credentials):
        return None
    stored = scan_credentials[idx]
    if idx == 0 and _comparable(stored) == _comparable(group_effective):
        return None
    creds = {k: stored.get(k) for k in SNMP_FIELDS}
    creds["timeout_s"] = 2
    creds["retries"] = 1
    return creds


def _error(rid: int, code: str, message: str, device_id: int | None = None) -> dict[str, Any]:
    return {"result_id": rid, "code": code, "message": message, "device_id": device_id}


async def import_results(
    rt: Runtime,
    scan: Any,
    group_id: int,
    items: list[dict[str, Any]],
    lang: str,
    budget_s: float | None = None,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (IMPORT_BUDGET_S if budget_s is None else budget_s)
    async with rt.sessionmaker() as session:
        chain = await group_chain(session, group_id)
    group_creds, _, _ = resolve_credentials(None, chain)
    credentials = list(scan.credentials or [])
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        rid = item["result_id"]
        async with sem:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return "error", _error(rid, "internal_error", BUDGET_MESSAGE)
            info: dict[str, Any] = {}
            try:
                return await _import_one(
                    rt,
                    scan.id,
                    group_id,
                    item,
                    lang,
                    credentials,
                    group_creds,
                    min(APPLY_BUDGET_S, remaining),
                    info,
                )
            except Exception as exc:  # noqa: BLE001
                # type and host only: messages may carry statement details
                log.warning(
                    "Import of discovery result %s (host %s) failed: %s",
                    rid,
                    info.get("host", "?"),
                    type(exc).__name__,
                )
                return "error", _error(rid, "internal_error", INTERNAL_MESSAGE)

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
    group_creds: dict[str, Any],
    apply_budget_s: float,
    info: dict[str, Any],
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
            return "error", _error(rid, "not_found", "Result not found in this scan")
        if row.existing_device_id is not None:
            return "error", _error(
                rid,
                "device_exists",
                "A device with this host already exists",
                row.existing_device_id,
            )
        ip = ip_text(row.ip)
        info["host"] = ip
        name = (item.get("name") or row.sys_name or row.hostname or ip).strip()[:128] or ip
        device = Device(
            group_id=group_id,
            name=name,
            host=ip,
            description=(row.sys_descr or "")[:500],
            enabled=True,
            snmp_credentials=device_credentials(row, credentials, group_creds),
            sort_order=0,
            identity=identity_from_result(row),
        )
        session.add(device)
        await session.flush()
        device_id = device.id
        # same transaction: a retry after any later failure sees device_exists
        await session.execute(
            text("UPDATE discovery_results SET imported_device_id = :d WHERE id = :id"),
            {"d": device_id, "id": rid},
        )
        await session.commit()
    rt.config_changed("device", device_id, "created")
    template = get_template(item["template_id"])
    assert template is not None
    result = await apply_template(
        rt, device_id, template, lang, budget_s=apply_budget_s, broadcast=False
    )
    return "created", {
        "result_id": rid,
        "device_id": device_id,
        "device_name": name,
        "template_id": item["template_id"],
        "sensors_created": len(result["created"]),
        "sensors_skipped": result["skipped"],
    }
