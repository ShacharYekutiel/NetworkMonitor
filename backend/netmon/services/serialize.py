"""Model -> API JSON (field names mirror docs/api.md exactly)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from netmon.poller.sensor_types import SENSOR_TYPES
from netmon.services.status import Inventory, empty_counts

SNMP_FIELDS = [
    "version",
    "port",
    "timeout_s",
    "retries",
    "community",
    "v3_username",
    "v3_security_level",
    "v3_auth_protocol",
    "v3_auth_key",
    "v3_priv_protocol",
    "v3_priv_key",
]


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> datetime:
    return datetime.now(UTC)


def user_out(u: Any) -> dict[str, Any]:
    return {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "role": u.role,
        "language": u.language,
        "is_active": u.is_active,
        "last_login_at": iso(u.last_login_at),
        "created_at": iso(u.created_at),
    }


def snmp_out(creds: dict[str, Any] | None) -> dict[str, Any] | None:
    if creds is None:
        return None
    return {
        "version": creds.get("version", "v2c"),
        "port": creds.get("port", 161),
        "timeout_s": creds.get("timeout_s", 2),
        "retries": creds.get("retries", 1),
        "community": None,
        "community_set": bool(creds.get("community")),
        "v3_username": creds.get("v3_username"),
        "v3_security_level": creds.get("v3_security_level"),
        "v3_auth_protocol": creds.get("v3_auth_protocol"),
        "v3_auth_key": None,
        "v3_auth_key_set": bool(creds.get("v3_auth_key")),
        "v3_priv_protocol": creds.get("v3_priv_protocol"),
        "v3_priv_key": None,
        "v3_priv_key_set": bool(creds.get("v3_priv_key")),
    }


def group_out(g: Any, inv: Inventory | None) -> dict[str, Any]:
    from netmon.services.dependencies import dependency_out

    return {
        "id": g.id,
        "parent_id": g.parent_id,
        "name": g.name,
        "description": g.description,
        "snmp_credentials": snmp_out(g.snmp_credentials),
        "sort_order": g.sort_order,
        "status": inv.group_status.get(g.id, "unknown") if inv else "unknown",
        "status_counts": dict(inv.group_counts.get(g.id, empty_counts()))
        if inv
        else empty_counts(),
        "dependency": dependency_out(inv or Inventory(), g),
        "created_at": iso(g.created_at),
        "updated_at": iso(g.updated_at),
    }


def device_out(d: Any, inv: Inventory, maint: Any = None) -> dict[str, Any]:
    from netmon.services.dependencies import dependency_out, effective_dependency_out

    source, gid = inv.effective_snmp(d.id) if d.id in inv.devices else ("default", None)
    in_maint = bool(maint is not None and maint.in_maintenance(d.id))
    return {
        "id": d.id,
        "group_id": d.group_id,
        "name": d.name,
        "host": d.host,
        "description": d.description,
        "enabled": d.enabled,
        "snmp_credentials": snmp_out(d.snmp_credentials),
        "effective_snmp_source": source,
        "effective_snmp_group_id": gid,
        "sort_order": d.sort_order,
        "status": inv.device_status.get(d.id, "unknown"),
        "status_counts": dict(inv.device_counts.get(d.id, empty_counts())),
        "dependency": dependency_out(inv, d),
        "effective_dependency": effective_dependency_out(inv, d.id)
        if d.id in inv.devices
        else None,
        "in_maintenance": in_maint,
        "maintenance": maint.maintenance_ref(d.id) if in_maint else None,
        "identity": d.identity,
        "created_at": iso(d.created_at),
        "updated_at": iso(d.updated_at),
    }


def channel_out(c: Any) -> dict[str, Any]:
    return {
        "key": c.key,
        "unit": c.unit,
        "warning_min": c.warning_min,
        "warning_max": c.warning_max,
        "down_min": c.down_min,
        "down_max": c.down_max,
        "last_value": c.last_value,
        "status": c.status,
    }


def ordered_channels(sensor: Any) -> list[Any]:
    tdef = SENSOR_TYPES.get(sensor.type)
    order = {ch.key: i for i, ch in enumerate(tdef.channels)} if tdef else {}
    return sorted(sensor.channels, key=lambda c: order.get(c.key, 999))


MASK = "********"


def public_config(type_: str, config: dict[str, Any], reveal_secrets: bool) -> dict[str, Any]:
    """Sensor config as shown to a user. HTTP header values (tokens, basic auth) are masked for
    non-admins; the header names stay visible."""
    out = dict(config)
    if not reveal_secrets and type_ == "http" and isinstance(out.get("headers"), dict):
        out["headers"] = {k: MASK for k in out["headers"]}
    return out


def acknowledgement_out(s: Any) -> dict[str, Any] | None:
    if getattr(s, "ack_at", None) is None:
        return None
    return {
        "by": s.ack_by,
        "at": iso(s.ack_at),
        "comment": s.ack_comment or "",
        "status": s.ack_status,
    }


def sensor_out(
    s: Any,
    device_name: str,
    group_id: int,
    reveal_secrets: bool = True,
    in_maintenance: bool = False,
) -> dict[str, Any]:
    tdef = SENSOR_TYPES.get(s.type)
    return {
        "id": s.id,
        "device_id": s.device_id,
        "device_name": device_name,
        "group_id": group_id,
        "name": s.name,
        "type": s.type,
        "config": public_config(s.type, s.config, reveal_secrets),
        "interval_s": s.interval_s,
        "enabled": s.enabled,
        "status": s.status,
        "status_message": s.status_message,
        "last_check_at": iso(s.last_check_at),
        "last_status_change_at": iso(s.last_status_change_at),
        "primary_channel": tdef.primary_channel if tdef else "",
        "channels": [channel_out(c) for c in ordered_channels(s)],
        "sort_order": s.sort_order,
        "acknowledgement": acknowledgement_out(s),
        "in_maintenance": bool(in_maintenance),
        "created_at": iso(s.created_at),
        "updated_at": iso(s.updated_at),
    }


def event_out(e: Any) -> dict[str, Any]:
    return {
        "id": e.id,
        "time": iso(e.time),
        "kind": e.kind,
        "sensor_id": e.sensor_id,
        "device_id": e.device_id,
        "group_id": e.group_id,
        "sensor_name": e.sensor_name,
        "device_name": e.device_name,
        "old_status": e.old_status,
        "new_status": e.new_status,
        "message": e.message,
        "in_maintenance": bool(getattr(e, "in_maintenance", False)),
    }


def public_channel_config(type_: str, config: dict[str, Any]) -> dict[str, Any]:
    """Channel config as returned by the API (secrets replaced by *_set flags)."""
    if type_ == "telegram":
        return {
            "bot_token": None,
            "bot_token_set": bool(config.get("bot_token")),
            "chat_id": config.get("chat_id"),
            "parse_mode": config.get("parse_mode", "HTML"),
            "disable_notification": bool(config.get("disable_notification", False)),
        }
    return dict(config)


def notification_channel_out(c: Any) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "type": c.type,
        "enabled": c.enabled,
        "config": public_channel_config(c.type, c.config),
        "created_at": iso(c.created_at),
        "updated_at": iso(c.updated_at),
    }


def check_result_out(ev: Any) -> dict[str, Any]:
    return {
        "ok": ev.ok,
        "status": ev.status,
        "message": ev.message,
        "duration_ms": round(ev.duration_ms, 3),
        "checked_at": iso(ev.checked_at),
        "channels": [
            {"key": c.key, "unit": c.unit, "value": c.value, "status": c.status}
            for c in ev.channels
        ],
    }


def page(items: list[Any], total: int, page_no: int, page_size: int) -> dict[str, Any]:
    return {"items": items, "total": total, "page": page_no, "page_size": page_size}
