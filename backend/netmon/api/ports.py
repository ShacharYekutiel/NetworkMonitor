"""[2b-2] Switch ports view: ports snapshot, port detail, manual refresh, MAC / IP search.

GETs read the last snapshot only (no SNMP)."""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import text

from netmon.api.deps import DB, RT, Admin, Viewer
from netmon.api.errors import ApiError, not_found
from netmon.models import Device, Sensor
from netmon.ports import read
from netmon.ports.eligibility import load_flags
from netmon.ports.scheduler import RefreshRunningError
from netmon.ports.search import search_mac
from netmon.schemas.ports import DevicePorts, MacSearchResult, PortDetail, PortsRefreshResponse
from netmon.services import settings as settings_svc
from netmon.services.serialize import sensor_out
from netmon.services.status import load_inventory

router = APIRouter()

KINDS = ("physical", "lag", "vlan", "mgmt", "other")


def _running(rt: RT, device_id: int) -> bool:
    return rt.ports is not None and rt.ports.running(device_id)


@router.get("/api/devices/{device_id}/ports", response_model=DevicePorts)
async def device_ports(
    device_id: int,
    _: Viewer,
    rt: RT,
    session: DB,
    kind: list[str] | None = Query(default=None),
) -> dict:
    if await session.get(Device, device_id) is None:
        raise not_found("Device")
    bad = [k for k in kind or [] if k not in KINDS]
    if bad:
        raise ApiError(
            422,
            "validation_error",
            "Invalid kind",
            [{"loc": ["query", "kind"], "msg": f"must be one of {list(KINDS)}"}],
        )
    inv = await load_inventory(session)
    ctx = await read.load_context(session, inv, [device_id])
    flags = ctx.flags[device_id]
    interval = int(await settings_svc.get(session, "port_inventory_min"))
    status_row = (
        await session.execute(
            text("SELECT * FROM port_inventory_status WHERE device_id = :d"), {"d": device_id}
        )
    ).first()
    rows = [
        dict(r)
        for r in (
            await session.execute(
                text("SELECT * FROM switch_ports WHERE device_id = :d ORDER BY if_index"),
                {"d": device_id},
            )
        ).mappings()
    ]
    lag = read.lag_ctx(rows) if rt.features.enabled("lag") else None  # [2b-3]
    if kind:
        rows = [r for r in rows if r["kind"] in kind]
    # MAC details (ARP / DNS / OUI) only for ports that can show them: a port above the
    # uplink threshold is an uplink and lists its count only
    wanted = {r["if_index"] for r in rows if r["mac_count"] <= ctx.threshold}
    if lag is not None:  # members show their LAG's MACs too
        for r in rows:
            lag_if = lag.lag_of.get(r["if_index"])
            if lag_if is not None and lag.rows[lag_if]["mac_count"] <= ctx.threshold:
                wanted.add(lag_if)
    macs = await read.present_macs(session, device_id, ctx.reverse_dns, if_indexes=sorted(wanted))
    if lag is not None:
        lag.macs = macs
    sensors = await read.interface_sensors(session, device_id)
    ports = [
        read.strip_private(
            read.port_out(
                device_id, r, ctx, macs.get(r["if_index"], []), sensors.get(r["if_index"]), lag
            )
        )
        for r in rows
    ]
    arp_sources = sum(1 for f in ctx.flags.values() if f.arp)
    return {
        "device_id": device_id,
        "ports_effective": flags.ports,
        "arp_effective": flags.arp,
        "port_inventory": flags.port_inventory,
        "arp_collect": flags.arp_collect,
        "effective_class": flags.effective_class,
        "status": read.status_out(
            status_row,
            flags,
            _running(rt, device_id),
            rt.clock.now(),
            interval,
            arp_sources,
            lag_on=rt.features.enabled("lag"),
        ),
        "settings": {"interval_min": interval, "uplink_mac_threshold": ctx.threshold},
        "health": await read.device_health(session, device_id),
        "ports": ports,
    }


@router.get("/api/devices/{device_id}/ports/{if_index}", response_model=PortDetail)
async def port_detail(
    device_id: int,
    if_index: int,
    user: Viewer,
    rt: RT,
    session: DB,
    include_uplink_macs: bool = False,
) -> dict:
    d = await session.get(Device, device_id)
    if d is None:
        raise not_found("Device")
    row = (
        (
            await session.execute(
                text("SELECT * FROM switch_ports WHERE device_id = :d AND if_index = :i"),
                {"d": device_id, "i": if_index},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Port")
    inv = await load_inventory(session)
    ctx = await read.load_context(session, inv, [device_id])
    lag = None
    if rt.features.enabled("lag"):  # [2b-3]
        rows = [
            dict(r)
            for r in (
                await session.execute(
                    text("SELECT * FROM switch_ports WHERE device_id = :d"), {"d": device_id}
                )
            ).mappings()
        ]
        lag = read.lag_ctx(rows)
        lag_if = lag.lag_of.get(if_index)
        if lag_if is not None:
            lag.macs = await read.present_macs(session, device_id, ctx.reverse_dns, lag_if)
    macs = await read.present_macs(session, device_id, ctx.reverse_dns, if_index)
    sensors = await read.interface_sensors(session, device_id)
    sensor = sensors.get(if_index)
    port = read.port_out(device_id, dict(row), ctx, macs.get(if_index, []), sensor, lag)
    hidden = port["is_uplink"] and not include_uplink_macs
    if port["is_uplink"] and include_uplink_macs:
        port["macs"] = [read._public_mac(m) for m in port["_macs_all"][: read.UPLINK_MAC_CAP]]
    full = None
    if sensor is not None:
        s = await session.get(Sensor, sensor[0])
        if s is not None:
            full = sensor_out(
                s,
                d.name,
                d.group_id,
                reveal_secrets=user.role == "admin",
                in_maintenance=bool(rt.maintenance and rt.maintenance.in_maintenance(d.id)),
            )
    return {
        **read.strip_private(port),
        "neighbors": port["_neighbors"],
        "macs_hidden": hidden,
        "sensor": full,
    }


@router.post(
    "/api/devices/{device_id}/ports/refresh", status_code=202, response_model=PortsRefreshResponse
)
async def refresh_ports(device_id: int, _: Admin, rt: RT, session: DB) -> dict:
    if await session.get(Device, device_id) is None:
        raise not_found("Device")
    if _running(rt, device_id):
        raise ApiError(
            409,
            "ports_refresh_running",
            "A port inventory run for this device is in progress",
            [{"device_id": device_id}],
        )
    flags = (await load_flags(session, device_id))[device_id]
    await session.close()
    if flags.needs_detection(rt.clock.now()):
        from netmon.ports.runner import detect_class

        await detect_class(rt, device_id)  # SNMP on a POST only (10 s budget)
        async with rt.sessionmaker() as s2:
            flags = (await load_flags(s2, device_id))[device_id]
    if not (flags.ports or flags.arp):
        raise ApiError(
            409,
            "ports_disabled",
            "Port inventory and ARP collection are not enabled for this device",
            [
                {
                    "port_inventory": flags.port_inventory,
                    "arp_collect": flags.arp_collect,
                    "effective_class": flags.effective_class,
                }
            ],
        )
    try:
        rt.ports.refresh(device_id)
    except RefreshRunningError:
        raise ApiError(
            409,
            "ports_refresh_running",
            "A port inventory run for this device is in progress",
            [{"device_id": device_id}],
        ) from None
    return {"device_id": device_id, "queued": True}


@router.get("/api/search/mac", response_model=MacSearchResult)
async def mac_search(
    _: Viewer,
    rt: RT,
    session: DB,
    q: str | None = Query(default=None, max_length=64),
    ip: str | None = Query(default=None, max_length=64),
) -> dict:
    return await search_mac(session, q, ip, lag_on=rt.features.enabled("lag"))
