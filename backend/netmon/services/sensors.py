"""Sensor helpers shared by the API and the poller."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.models import Device, Event, Sensor, SensorChannel
from netmon.poller.sensor_types import PASSIVE_TYPES, THRESHOLD_KEYS, channel_unit, get_type
from netmon.poller.snmp_client import resolve_credentials
from netmon.poller.types import CheckContext
from netmon.services import events as events_svc
from netmon.services.serialize import now_utc


def build_channels(
    type_: str,
    overrides: dict[str, dict[str, float | None]],
    config: dict[str, Any] | None = None,
) -> list[SensorChannel]:
    tdef = get_type(type_)
    out = []
    for ch in tdef.channels:
        t = {k: getattr(ch, k) for k in THRESHOLD_KEYS}
        t.update(overrides.get(ch.key, {}))
        unit = channel_unit(type_, ch, config)
        out.append(SensorChannel(key=ch.key, unit=unit, status="unknown", last_value=None, **t))
    return out


def sync_channel_units(sensor: Sensor) -> None:
    """snmp_custom: keep the `value` channel unit equal to config.unit."""
    tdef = get_type(sensor.type)
    by_key = {c.key: c for c in sensor.channels}
    for ch in tdef.channels:
        row = by_key.get(ch.key)
        if row is not None:
            unit = channel_unit(sensor.type, ch, sensor.config)
            if row.unit != unit:
                row.unit = unit


def apply_channel_overrides(sensor: Sensor, overrides: dict[str, dict[str, float | None]]) -> None:
    by_key = {c.key: c for c in sensor.channels}
    for key, vals in overrides.items():
        ch = by_key.get(key)
        if ch is None:
            continue
        for k, v in vals.items():
            setattr(ch, k, v)


def channel_thresholds(sensor: Sensor) -> list[tuple[str, str, dict[str, float | None]]]:
    tdef = get_type(sensor.type)
    by_key = {c.key: c for c in sensor.channels}
    out = []
    for ch in tdef.channels:
        row = by_key.get(ch.key)
        if row is not None:
            t = {k: getattr(row, k) for k in THRESHOLD_KEYS}
            out.append((ch.key, row.unit, t))
        else:
            out.append((ch.key, ch.unit, {k: getattr(ch, k) for k in THRESHOLD_KEYS}))
    return out


def default_thresholds(
    type_: str,
    overrides: dict[str, dict[str, float | None]] | None = None,
    config: dict[str, Any] | None = None,
) -> list[tuple[str, str, dict[str, float | None]]]:
    tdef = get_type(type_)
    out = []
    for ch in tdef.channels:
        t = {k: getattr(ch, k) for k in THRESHOLD_KEYS}
        if overrides:
            t.update(overrides.get(ch.key, {}))
        out.append((ch.key, channel_unit(type_, ch, config), t))
    return out


async def group_chain(session: AsyncSession, group_id: int) -> list[tuple[int, Any]]:
    rows = await session.execute(
        text(
            """
            WITH RECURSIVE chain(id, parent_id, snmp_credentials, depth) AS (
                SELECT id, parent_id, snmp_credentials, 0 FROM groups WHERE id = :gid
                UNION ALL
                SELECT g.id, g.parent_id, g.snmp_credentials, c.depth + 1
                FROM groups g JOIN chain c ON g.id = c.parent_id WHERE c.depth < 100
            )
            SELECT id, snmp_credentials FROM chain ORDER BY depth
            """
        ),
        {"gid": group_id},
    )
    return [(r.id, r.snmp_credentials) for r in rows]


async def effective_credentials(session: AsyncSession, device: Device) -> dict[str, Any]:
    chain = await group_chain(session, device.group_id)
    creds, _, _ = resolve_credentials(device.snmp_credentials, chain)
    return creds


async def build_context(session: AsyncSession, sensor: Sensor, device: Device) -> CheckContext:
    creds = (
        await effective_credentials(session, device) if get_type(sensor.type).requires_snmp else {}
    )
    timeout = float(min(sensor.interval_s, 120))
    lang = await check_language(session, sensor.config)
    return CheckContext(
        sensor_id=sensor.id,
        type=sensor.type,
        config=dict(sensor.config),
        host=device.host,
        snmp=creds,
        state=dict(sensor.state or {}),
        timeout_s=timeout,
        lang=lang,
    )


async def check_language(session: AsyncSession, config: dict[str, Any] | None) -> str:
    """[1.3.1] The language of a check's message texts: the server `default_language` for a
    sensor with a named value map (read only then), else "en"."""
    if not (config or {}).get("value_map_ref"):
        return "en"
    from netmon.services import settings as settings_svc

    return str(await settings_svc.get(session, "default_language") or "en")


def effective_paused(sensor: Sensor, device: Device) -> bool:
    return (not sensor.enabled) or (not device.enabled)


async def set_sensor_status(
    session: AsyncSession,
    sensor: Sensor,
    device: Device,
    new_status: str,
    message: str = "",
    in_maintenance: bool = False,
) -> Event | None:
    """Change a sensor's stored status (pause/resume) and write the status_change event."""
    if sensor.status == new_status:
        return None
    old = sensor.status
    now = now_utc()
    sensor.status = new_status
    sensor.status_message = message
    sensor.last_status_change_at = now
    if new_status in ("paused", "unknown"):
        for ch in sensor.channels:
            ch.status = "unknown"
    return await events_svc.add_status_change(
        session,
        sensor=sensor,
        device=device,
        old_status=old,
        new_status=new_status,
        message=message,
        time=now,
        in_maintenance=in_maintenance,
    )


async def sync_device_sensors_status(
    session: AsyncSession, device: Device, in_maintenance: bool = False
) -> list[Event]:
    """After a device enable/disable: set paused/unknown on its sensors."""
    sensors = (
        (await session.execute(select(Sensor).where(Sensor.device_id == device.id))).scalars().all()
    )
    out = []
    for s in sensors:
        target = (
            "paused"
            if effective_paused(s, device)
            else ("unknown" if s.status == "paused" else s.status)
        )
        if s.type in PASSIVE_TYPES:  # [2b-3] passive event sensors
            if target == "paused" and s.status != "paused":
                await session.execute(
                    text("DELETE FROM event_conditions WHERE sensor_id = :s"), {"s": s.id}
                )
            elif target == "unknown":
                target = "up"
        ev = await set_sensor_status(session, s, device, target, in_maintenance=in_maintenance)
        if ev is not None:
            out.append(ev)
    return out
