"""[2b-2] One device's port inventory / ARP run, class detection and the daily cleanup
(architecture § 11.2)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from netmon.models import Device
from netmon.poller.snmp_client import SnmpError, as_text, in_subtree, snmp_get, snmp_getnext
from netmon.ports import dns
from netmon.ports.collect import PortWalks, collect_arp, collect_ports, get_uptime, make_walker
from netmon.ports.eligibility import DETECT_MAX_AGE_S, DeviceFlags, load_flags
from netmon.ports.snapshot import PORT_SOURCES, build_snapshot, skipped_sources
from netmon.services.sensors import effective_credentials
from netmon.services.serialize import iso

if TYPE_CHECKING:
    from datetime import datetime

    from netmon.runtime import Runtime

log = logging.getLogger("netmon.ports")

RUN_BUDGET_S = 90.0
DETECT_BUDGET_S = 10.0
SYS_DESCR, SYS_OBJECT_ID, SYS_NAME = "1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.2.0", "1.3.6.1.2.1.1.5.0"

PORT_COLUMNS = (
    "if_index",
    "name",
    "descr",
    "alias",
    "if_type",
    "kind",
    "admin_status",
    "oper_status",
    "speed_bps",
    "duplex",
    "mac",
    "last_change_at",
    "bridge_port",
    "pvid",
    "mode",
    "untagged_vlans",
    "tagged_vlans",
    "poe_admin_enabled",
    "poe_detection",
    "poe_class",
    "poe_power_w",
    "in_octets",
    "out_octets",
    "counter_bits",
    "in_errors",
    "out_errors",
    "in_bps",
    "out_bps",
    "in_errors_delta",
    "out_errors_delta",
    "mac_count",
    "collected_at",
)
_UPSERT_PORT = text(
    f"INSERT INTO switch_ports (device_id, {', '.join(PORT_COLUMNS)}) VALUES (:device_id, "
    + ", ".join(f":{c}" for c in PORT_COLUMNS)
    + ") ON CONFLICT (device_id, if_index) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in PORT_COLUMNS if c != "if_index")
)


async def _credentials(rt: Runtime, device_id: int) -> tuple[str, dict[str, Any]] | None:
    async with rt.sessionmaker() as session:
        d = await session.get(Device, device_id)
        if d is None:
            return None
        return d.host, await effective_credentials(session, d)


# ------------------------------------------------------------------ class detection
DETECT_MAX_AGE = timedelta(seconds=DETECT_MAX_AGE_S)
DETECT_RETRY_AFTER = timedelta(hours=1)  # a failed detection is retried after 1 h


async def _probe_names(host: str, creds: dict[str, Any]) -> set[str]:
    """The discovery subtree probes: one GETNEXT carrying every SUBTREE_PROBES varbind."""
    from netmon.discovery.classify import SUBTREE_PROBES

    names = list(SUBTREE_PROBES)
    try:
        rows = await snmp_getnext(host, creds, [SUBTREE_PROBES[n] for n in names])
    except SnmpError:
        return set()
    out = set()
    for name, (oid, _value, type_name) in zip(names, rows, strict=False):
        if type_name != "EndOfMibView" and in_subtree(oid, SUBTREE_PROBES[name]):
            out.add(name)
    return out


async def detect_class(rt: Runtime, device_id: int) -> str | None:
    """GET sysDescr/sysObjectID/sysName plus the subtree-probe GETNEXT (the device's own
    retries, at least 1) and run the pure classify rules 1-12 (no FreeBSD walk, snmp_ok).
    A failure keeps the previous class (only a first-ever failure stores `unknown`) and is
    retried after 1 h. Stores detected_class/detected_at; returns the stored class."""
    from netmon.discovery.classify import classify_rule

    target = await _credentials(rt, device_id)
    if target is None:
        return None
    host, creds = target
    creds = {**creds, "retries": max(1, int(creds.get("retries") or 0))}
    cls: str | None = None
    try:

        async def identify() -> tuple[dict[str, Any], set[str]]:
            async with rt.semaphore:
                got = await snmp_get(host, creds, [SYS_DESCR, SYS_OBJECT_ID, SYS_NAME])
                return got, await _probe_names(host, creds)

        vals, probes = await asyncio.wait_for(identify(), DETECT_BUDGET_S)
        oid = vals.get(SYS_OBJECT_ID)
        cls, _, _ = classify_rule(
            str(oid) if oid is not None else None,
            as_text(vals.get(SYS_DESCR)) or None,
            as_text(vals.get(SYS_NAME)) or None,
            probes,
            False,
            [],
            True,
        )
    except (SnmpError, TimeoutError):
        cls = None
    now = rt.clock.now()
    async with rt.sessionmaker() as session:
        if cls is None:  # failed: keep the previous class, retry in 1 h
            row = (
                await session.execute(
                    text(
                        "INSERT INTO port_inventory_status (device_id, detected_class, "
                        "detected_at) SELECT :d, 'unknown', :t WHERE EXISTS "
                        "(SELECT 1 FROM devices WHERE id = :d) ON CONFLICT (device_id) DO UPDATE "
                        "SET detected_class = COALESCE(port_inventory_status.detected_class, "
                        "'unknown'), detected_at = EXCLUDED.detected_at RETURNING detected_class"
                    ),
                    {"d": device_id, "t": now - DETECT_MAX_AGE + DETECT_RETRY_AFTER},
                )
            ).first()
            cls = row.detected_class if row is not None else None
        else:
            await session.execute(
                text(
                    "INSERT INTO port_inventory_status (device_id, detected_class, detected_at) "
                    "SELECT :d, :c, :t WHERE EXISTS (SELECT 1 FROM devices WHERE id = :d) "
                    "ON CONFLICT (device_id) DO UPDATE SET "
                    "detected_class = EXCLUDED.detected_class, detected_at = EXCLUDED.detected_at"
                ),
                {"d": device_id, "c": cls, "t": now},
            )
        await session.commit()
    return cls


# ------------------------------------------------------------------ one run
async def _walk_all(rt: Runtime, host: str, creds: dict[str, Any], flags: DeviceFlags) -> PortWalks:
    walks = PortWalks()
    walk = make_walker(host, creds, rt.semaphore)
    walks.sys_uptime = await get_uptime(host, creds, rt.semaphore)
    if flags.ports:
        await collect_ports(walk, str(creds.get("version") or "v2c"), walks)
    if flags.arp:
        opt_errors: list[str] = []

        async def arp_walk(oid: str, cap: int) -> dict[str, Any]:
            try:
                return await walk(oid, cap)
            except SnmpError as exc:
                opt_errors.append(str(exc) or "SNMP error")
                raise

        await collect_arp(arp_walk, walks)
        if walks.sources.get("arp") == "error" and opt_errors:
            walks.sources["_arp_error"] = opt_errors[-1]
    return walks


async def run_device(rt: Runtime, device_id: int, trigger: str = "scheduled") -> dict[str, Any]:
    """Ports (if effective) and ARP (if effective) for one device, one DB transaction, reverse
    DNS, then `ports.updated`. Returns the WS payload."""
    started = rt.clock.now()
    t0 = time.monotonic()
    async with rt.sessionmaker() as session:
        flags = (await load_flags(session, device_id)).get(device_id)
    target = await _credentials(rt, device_id)
    if flags is None or target is None:
        return {"device_id": device_id, "ok": False, "collected_at": iso(started)}
    host, creds = target
    error: str | None = None
    try:
        walks = await asyncio.wait_for(_walk_all(rt, host, creds, flags), RUN_BUDGET_S)
    except TimeoutError:
        walks = PortWalks(error="Timeout")
        error = "Timeout"
    if error is None:
        if flags.ports and walks.error is not None:
            error = walks.error
        elif not flags.ports and flags.arp and walks.sources.get("arp") == "error":
            error = str(walks.sources.get("_arp_error") or "ARP table not available")
    ok = error is None
    duration_ms = int((time.monotonic() - t0) * 1000)
    await _store(rt, device_id, flags, walks, started, ok, error, duration_ms)
    log.info(
        "Port inventory of device %s (%s): %s in %d ms",
        device_id,
        trigger,
        "ok" if ok else f"failed ({error})",
        duration_ms,
    )
    if ok and flags.arp and walks.sources.get("arp") in ("ok", "empty"):
        try:
            await dns.refresh(rt, device_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Reverse DNS for device %s failed", device_id)
    payload = {"device_id": device_id, "ok": ok, "collected_at": iso(started)}
    rt.hub.broadcast("ports.updated", payload)
    return payload


async def _store(
    rt: Runtime,
    device_id: int,
    flags: DeviceFlags,
    walks: PortWalks,
    now: datetime,
    ok: bool,
    error: str | None,
    duration_ms: int,
) -> None:
    async with rt.sessionmaker() as session:
        exists = (
            await session.execute(
                text("SELECT 1 FROM devices WHERE id = :d FOR UPDATE"), {"d": device_id}
            )
        ).first()
        if exists is None:
            return  # deleted meanwhile
        prev_status = (
            await session.execute(
                text("SELECT sources, ports FROM port_inventory_status WHERE device_id = :d"),
                {"d": device_id},
            )
        ).first()
        sources = skipped_sources()
        if prev_status is not None and isinstance(prev_status.sources, dict):
            sources.update(prev_status.sources)
        n_ports = prev_status.ports if prev_status is not None else 0
        if ok:
            if flags.ports:
                snap = await _store_ports(session, device_id, walks, now)
                sources.update({k: snap.sources.get(k) for k in PORT_SOURCES})
                sources["fdb_mib"] = snap.sources.get("fdb_mib")
                sources["vlan_mib"] = snap.sources.get("vlan_mib")
                n_ports = len(snap.rows)
            else:
                sources.update({k: "skipped" for k in PORT_SOURCES})
                sources["fdb_mib"] = sources["vlan_mib"] = None
            if flags.arp:
                state = walks.sources.get("arp", "error")
                sources["arp"] = state
                if state in ("ok", "empty"):
                    await _store_arp(session, device_id, walks, now)
            else:
                sources["arp"] = "skipped"
        sources.pop("_arp_error", None)
        macs = (
            await session.execute(
                text("SELECT count(*) FROM port_macs WHERE device_id = :d AND present"),
                {"d": device_id},
            )
        ).scalar_one()
        arps = (
            await session.execute(
                text("SELECT count(*) FROM arp_entries WHERE source_device_id = :d AND present"),
                {"d": device_id},
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO port_inventory_status (device_id, last_run_at, last_ok_at, "
                "duration_ms, error, ports, macs, arp_entries, sources) VALUES (:d, :run, "
                ":okat, :ms, :err, :ports, :macs, :arps, CAST(:src AS jsonb)) "
                "ON CONFLICT (device_id) DO UPDATE SET last_run_at = EXCLUDED.last_run_at, "
                "last_ok_at = COALESCE(EXCLUDED.last_ok_at, port_inventory_status.last_ok_at), "
                "duration_ms = EXCLUDED.duration_ms, error = EXCLUDED.error, "
                "ports = EXCLUDED.ports, macs = EXCLUDED.macs, "
                "arp_entries = EXCLUDED.arp_entries, sources = EXCLUDED.sources"
            ),
            {
                "d": device_id,
                "run": now,
                "okat": now if ok else None,
                "ms": duration_ms,
                "err": (error or "")[:1000] or None,
                "ports": n_ports,
                "macs": macs,
                "arps": arps,
                "src": json.dumps(sources),
            },
        )
        await session.commit()


async def _store_ports(session: Any, device_id: int, walks: PortWalks, now: datetime) -> Any:
    prev = {
        r["if_index"]: dict(r)
        for r in (
            await session.execute(
                text("SELECT * FROM switch_ports WHERE device_id = :d"), {"d": device_id}
            )
        ).mappings()
    }
    snap = build_snapshot(prev, walks, now)
    indexes = [r["if_index"] for r in snap.rows]
    await session.execute(
        text("DELETE FROM switch_ports WHERE device_id = :d AND NOT (if_index = ANY(:idx))"),
        {"d": device_id, "idx": indexes},
    )
    if snap.rows:
        await session.execute(_UPSERT_PORT, [{"device_id": device_id, **r} for r in snap.rows])
    if snap.fdb is not None:
        if snap.fdb:
            await session.execute(
                text(
                    "INSERT INTO port_macs (device_id, if_index, mac, vlan, present, first_seen, "
                    "last_seen) SELECT :d, x.if_index, x.mac, x.vlan, true, :now, :now FROM "
                    "unnest(CAST(:ifs AS integer[]), CAST(:macs AS varchar[]), "
                    "CAST(:vlans AS integer[])) AS x(if_index, mac, vlan) "
                    "ON CONFLICT (device_id, mac, (COALESCE(vlan, -1))) DO UPDATE SET "
                    "present = true, last_seen = EXCLUDED.last_seen, "
                    "first_seen = CASE WHEN port_macs.if_index <> EXCLUDED.if_index "
                    "THEN EXCLUDED.first_seen ELSE port_macs.first_seen END, "
                    "if_index = EXCLUDED.if_index"
                ),
                {
                    "d": device_id,
                    "now": now,
                    "ifs": [e.if_index for e in snap.fdb],
                    "macs": [e.mac for e in snap.fdb],
                    "vlans": [e.vlan for e in snap.fdb],
                },
            )
        await session.execute(
            text(
                "UPDATE port_macs SET present = false WHERE device_id = :d AND present "
                "AND last_seen <> :now"
            ),
            {"d": device_id, "now": now},
        )
    return snap


async def _store_arp(session: Any, device_id: int, walks: PortWalks, now: datetime) -> None:
    if walks.arp:
        await session.execute(
            text(
                "INSERT INTO arp_entries (source_device_id, ip, mac, if_index, present, "
                "first_seen, last_seen) SELECT :d, x.ip, x.mac, x.if_index, true, :now, :now "
                "FROM unnest(CAST(:ips AS varchar[]), CAST(:macs AS varchar[]), "
                "CAST(:ifs AS integer[])) AS x(ip, mac, if_index) "
                "ON CONFLICT (source_device_id, ip) DO UPDATE SET present = true, "
                "last_seen = EXCLUDED.last_seen, "
                "first_seen = CASE WHEN arp_entries.mac <> EXCLUDED.mac "
                "THEN EXCLUDED.first_seen ELSE arp_entries.first_seen END, "
                "mac = EXCLUDED.mac, if_index = EXCLUDED.if_index"
            ),
            {
                "d": device_id,
                "now": now,
                "ips": [e.ip for e in walks.arp],
                "macs": [e.mac for e in walks.arp],
                "ifs": [e.if_index for e in walks.arp],
            },
        )
    await session.execute(
        text(
            "UPDATE arp_entries SET present = false WHERE source_device_id = :d AND present "
            "AND last_seen <> :now"
        ),
        {"d": device_id, "now": now},
    )


# ------------------------------------------------------------------ daily cleanup
DNS_CACHE_DAYS = 7


async def cleanup(rt: Runtime) -> dict[str, int]:
    from netmon.services import settings as settings_svc

    now = rt.clock.now()
    async with rt.sessionmaker() as session:
        days = int(await settings_svc.get(session, "port_mac_retention_days"))
        out = {}
        for key, sql in (
            (
                "port_macs",
                "DELETE FROM port_macs WHERE NOT present AND last_seen < "
                "CAST(:now AS timestamptz) - make_interval(days => :days)",
            ),
            (
                "arp_entries",
                "DELETE FROM arp_entries WHERE NOT present AND last_seen < "
                "CAST(:now AS timestamptz) - make_interval(days => :days)",
            ),
            (
                "dns_cache",
                "DELETE FROM dns_cache WHERE resolved_at < "
                "CAST(:now AS timestamptz) - make_interval(days => :dns_days)",
            ),
        ):
            res = await session.execute(
                text(sql), {"now": now, "days": days, "dns_days": DNS_CACHE_DAYS}
            )
            out[key] = res.rowcount or 0
        await session.commit()
    return out
