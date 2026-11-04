"""Discovery rows -> API JSON (docs/api.md "Phase 2a"). Secrets never leave this module."""

from __future__ import annotations

from typing import Any

from netmon.discovery.options import public_options
from netmon.services.serialize import iso


def ip_text(v: Any) -> str:
    """inet value (str / ipaddress object / 'a.b.c.d/32') -> plain address text."""
    s = str(v)
    return s.split("/", 1)[0]


def _get(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    if hasattr(row, "_mapping"):
        return row._mapping.get(key)  # noqa: SLF001
    return getattr(row, key)


def result_out(row: Any, existing_device_id: int | None = None) -> dict[str, Any]:
    idx = _get(row, "snmp_credential_index")
    snmp_ok = bool(_get(row, "snmp_ok"))
    cred = None
    if snmp_ok and idx is not None:
        cred = "group" if int(idx) == 0 else "extra"
    if existing_device_id is None:
        try:
            existing_device_id = _get(row, "existing_device_id")
        except (AttributeError, KeyError):
            existing_device_id = None
    return {
        "id": _get(row, "id"),
        "scan_id": _get(row, "scan_id"),
        "ip": ip_text(_get(row, "ip")),
        "hostname": _get(row, "hostname"),
        "mac": _get(row, "mac"),
        "ping_ok": bool(_get(row, "ping_ok")),
        "ping_rtt_ms": _get(row, "ping_rtt_ms"),
        "snmp_ok": snmp_ok,
        "snmp_version": _get(row, "snmp_version"),
        "snmp_credential": cred,
        "sys_name": _get(row, "sys_name"),
        "sys_descr": _get(row, "sys_descr"),
        "sys_object_id": _get(row, "sys_object_id"),
        "open_ports": sorted(int(p) for p in (_get(row, "open_ports") or [])),
        "device_class": _get(row, "device_class"),
        "vendor": _get(row, "vendor"),
        "suggested_template": _get(row, "suggested_template"),
        "existing_device_id": existing_device_id,
        "is_new": bool(_get(row, "is_new")),
        "imported_device_id": _get(row, "imported_device_id"),
    }


def scan_out(scan: Any, created_by: str | None) -> dict[str, Any]:
    return {
        "id": scan.id,
        "profile_id": scan.profile_id,
        "profile_name": scan.profile_name,
        "trigger": scan.trigger,
        "status": scan.status,
        "group_id": scan.group_id,
        "targets": list(scan.targets or []),
        "options": public_options(scan.options or {}),
        "total_hosts": scan.total_hosts,
        "scanned_hosts": scan.scanned_hosts,
        "found_hosts": scan.found_hosts,
        "new_hosts": scan.new_hosts,
        "error": scan.error,
        "created_by": created_by,
        "started_at": iso(scan.started_at),
        "finished_at": iso(scan.finished_at),
    }


def profile_out(p: Any) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "targets": list(p.targets or []),
        "group_id": p.group_id,
        "options": public_options(p.options or {}),
        "schedule_enabled": p.schedule_enabled,
        "schedule_every_hours": p.schedule_every_hours,
        "notify_new_hosts": p.notify_new_hosts,
        "notify_channel_ids": list(p.notify_channel_ids or []),
        "last_run_at": iso(p.last_run_at),
        "next_run_at": iso(p.next_run_at),
        "last_scan_id": p.last_scan_id,
        "created_at": iso(p.created_at),
        "updated_at": iso(p.updated_at),
    }
