"""[3] New-device detector (architecture § 13.4): called by the port-inventory runner after a
successful FDB write of a switch; keeps the known-MAC register, writes `new_device` events and
`new_mac` conditions on the switch's passive `new_mac` sensor.

A component of `rt.features` (feature `new_device_alerts`); `rt.newdev` is the instance."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from netmon.newdev import detector
from netmon.services import events as events_svc
from netmon.services import settings as settings_svc
from netmon.services.serialize import iso

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.newdev")

SENSOR_TYPE = "new_mac"
OFF_MESSAGE = "New-device alerts are turned off"
IDLE_MESSAGE = "No new devices"


def parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class NewDeviceDetector:
    features = ("new_device_alerts",)

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.enabled = False
        self.last_detection_at: datetime | None = None
        self._sync_dirty = False
        self._sync_running = False

    # ================================================================ component interface
    async def start(self, snapshot: dict[str, bool]) -> None:
        self.enabled = bool(snapshot.get("new_device_alerts"))
        if self.enabled:
            await self.sync_sensors()

    async def stop(self) -> None:
        self.enabled = False

    async def feature_changed(self, name: str, on: bool) -> None:
        if name != "new_device_alerts":
            return
        if on:
            await self._turned_on()
        else:
            self.enabled = False
            if self.rt.event_sensors is not None:
                await self.rt.event_sensors.remove_conditions(
                    "source = 'new_mac'", {"types": [SENSOR_TYPE]}, OFF_MESSAGE
                )

    async def settings_changed(self) -> None:
        # settings are read on every detection run; a scope change may add switches
        if self.enabled:
            await self.sync_sensors()

    async def _turned_on(self) -> None:
        now = self.rt.clock.now()
        async with self.rt.sessionmaker() as session:
            # rule 4: every switch baselines again on its first FDB read after ON
            await session.execute(
                text("UPDATE port_inventory_status SET newdev_baselined_at = NULL")
            )
            until = await settings_svc.get(session, "newdev_learning_until")
            if until is None:  # the first time ever: start the learning period
                days = int(await settings_svc.get(session, "newdev_learning_days"))
                await settings_svc.put(
                    session, "newdev_learning_until", (now + timedelta(days=days)).isoformat()
                )
            await session.commit()
        self.enabled = True
        await self.sync_sensors()
        if self.rt.event_sensors is not None:  # the OFF message goes away
            await self.rt.event_sensors.remove_conditions(
                "false", {"types": [SENSOR_TYPE]}, IDLE_MESSAGE
            )

    # ================================================================ sensors
    async def sync_sensors(self) -> int:
        """Create the passive `new_mac` sensor ("New devices") of every in-scope switch (port
        inventory effective, enabled, in `newdev_scope_group_ids`) while the feature is ON
        (§ 13.4), like the `config_backup` sensors. A sensor the user deleted is not re-created
        (device `event_sensor_optout`, enforced by `get_or_create`). Never raises."""
        if self.rt.event_sensors is None:
            return 0
        try:
            from netmon.ports.eligibility import load_flags

            async with self.rt.sessionmaker() as session:
                scope_ids = await settings_svc.get(session, "newdev_scope_group_ids")
                scope = await self.scope_device_ids(session, list(scope_ids or []))
                flags = await load_flags(session)
                existing = set(
                    (
                        await session.execute(
                            text("SELECT device_id FROM sensors WHERE type = :t"),
                            {"t": SENSOR_TYPE},
                        )
                    ).scalars()
                )
            n = 0
            for did, f in sorted(flags.items()):
                if did in existing or not f.ports or (scope is not None and did not in scope):
                    continue
                n += int(await self._ensure_sensor(did))
            return n
        except Exception:  # noqa: BLE001
            log.exception("Cannot create the New devices sensors")
            return 0

    def mark_dirty(self) -> None:
        """Devices / groups changed (sync): create missing sensors soon (one pass at a time;
        a change during a pass triggers another one)."""
        if not self.enabled:
            return
        self._sync_dirty = True
        if self._sync_running:
            return
        self._sync_running = True

        async def run() -> None:
            try:
                while self._sync_dirty and self.enabled:
                    self._sync_dirty = False
                    await self.sync_sensors()
            finally:
                self._sync_running = False

        self.rt.spawn(run())

    async def _ensure_sensor(self, device_id: int) -> bool:
        es = self.rt.event_sensors
        if es is None:
            return False
        from netmon.models import Device

        created: list[int] = []
        async with es._lock, self.rt.sessionmaker() as session:  # noqa: SLF001
            device = await session.get(Device, device_id)
            if device is None:
                return False
            sensor, was_created = await es.get_or_create(session, device, SENSOR_TYPE)
            await session.commit()
            if was_created and sensor is not None:
                created.append(sensor.id)
        for sid in created:
            self.rt.config_changed("sensor", sid, "created")
        return bool(created)

    # ================================================================ scope
    async def scope_device_ids(self, session: Any, scope: list[int]) -> set[int] | None:
        """None = every device; else the devices of the scope groups and their subgroups."""
        if not scope:
            return None
        rows = await session.execute(
            text(
                "WITH RECURSIVE sub(id) AS (SELECT id FROM groups WHERE id = ANY(:g) "
                "UNION SELECT g.id FROM groups g JOIN sub s ON g.parent_id = s.id) "
                "SELECT d.id FROM devices d WHERE d.group_id IN (SELECT id FROM sub)"
            ),
            {"g": list(scope)},
        )
        return {int(r.id) for r in rows}

    # ================================================================ detection
    async def after_fdb(self, device_id: int, now: datetime) -> dict[str, Any] | None:
        """Run detection for one switch after its FDB was stored. Never raises."""
        if not self.enabled:
            return None
        try:
            return await self._detect(device_id, now)
        except Exception:  # noqa: BLE001
            log.exception("New-device detection for device %s failed", device_id)
            return None

    async def _detect(self, device_id: int, now: datetime) -> dict[str, Any] | None:
        from netmon.ports import oui
        from netmon.ports import read as ports_read
        from netmon.ports import view as ports_view
        from netmon.services.status import load_inventory

        rt = self.rt
        async with rt.sessionmaker() as session:
            cfg = await settings_svc.get_all(session)
            scope = await self.scope_device_ids(session, list(cfg["newdev_scope_group_ids"] or []))
            if scope is not None and device_id not in scope:
                return None
            dev = (
                await session.execute(
                    text(
                        "SELECT d.id, d.name, d.group_id, d.enabled, d.newdev_excluded_ports, "
                        "d.event_sensor_optout, s.newdev_baselined_at, s.sources FROM devices d "
                        "LEFT JOIN port_inventory_status s ON s.device_id = d.id WHERE d.id = :d"
                    ),
                    {"d": device_id},
                )
            ).first()
            if dev is None or not dev.enabled:
                return None
            has_sensor = (
                await session.execute(
                    text("SELECT 1 FROM sensors WHERE device_id = :d AND type = :t"),
                    {"d": device_id, "t": SENSOR_TYPE},
                )
            ).first() is not None
        if not has_sensor and SENSOR_TYPE not in (dev.event_sensor_optout or []):
            # an in-scope switch: its sensor exists while ON (a deleted one stays deleted)
            await self._ensure_sensor(device_id)
        async with rt.sessionmaker() as session:
            if not isinstance(dev.sources, dict) or dev.sources.get("fdb") not in ("ok", "empty"):
                return None  # no FDB data in this run
            ports = [
                dict(r)
                for r in (
                    await session.execute(
                        text("SELECT * FROM switch_ports WHERE device_id = :d"), {"d": device_id}
                    )
                ).mappings()
            ]
            counts = {
                int(r.if_index): int(r.n)
                for r in await session.execute(
                    text(
                        "SELECT if_index, count(*) AS n FROM port_macs WHERE device_id = :d "
                        "AND present GROUP BY if_index"
                    ),
                    {"d": device_id},
                )
            }
            inv = await load_inventory(session)
            pctx = await ports_read.load_context(session, inv, [device_id])
            access: dict[int, dict[str, Any]] = {}
            excluded = list(dev.newdev_excluded_ports or [])
            for p in ports:
                p["mac_count"] = counts.get(int(p["if_index"]), 0)
                nbs = ports_read.neighbors_of(pctx, device_id, p)
                up, _ = ports_view.uplink(nbs, p["mac_count"], pctx.threshold)
                if detector.is_access_port(p, is_uplink=up, excluded=excluded):
                    access[int(p["if_index"])] = p
            if not access:
                await self._mark_baselined(session, device_id, dev.newdev_baselined_at, now)
                await session.commit()
                return None
            rows = (
                await session.execute(
                    text(
                        "SELECT mac, if_index, vlan FROM port_macs WHERE device_id = :d "
                        "AND present AND if_index = ANY(:idx) ORDER BY if_index, mac, vlan"
                    ),
                    {"d": device_id, "idx": sorted(access)},
                )
            ).all()
            sightings = [
                detector.Sighting(
                    r.mac,
                    int(r.if_index),
                    r.vlan,
                    ports_view.display_port(
                        access[int(r.if_index)].get("name"), int(r.if_index), None
                    ),
                )
                for r in rows
            ]
            macs = sorted({s.mac for s in sightings})
            known = set(
                (
                    await session.execute(
                        text("SELECT mac FROM known_macs WHERE mac = ANY(:m)"), {"m": macs}
                    )
                ).scalars()
            )
            allow = [
                (r.prefix, int(r.bits))
                for r in await session.execute(text("SELECT prefix, bits FROM mac_allowlist"))
            ]
            until = parse_dt(cfg.get("newdev_learning_until"))
            ctx = detector.ClassifyCtx(
                first_read=dev.newdev_baselined_at is None,
                learning=until is not None and now < until,
                ignore_random=bool(cfg["newdev_ignore_random_macs"]),
            )
            decisions = detector.classify_sightings(sightings, known, allow, ctx)
            new_macs = [s.mac for s, d in decisions if d == detector.NEW]
            details = await ports_read.mac_details(
                session, new_macs, bool(cfg.get("reverse_dns", True))
            )
            new_items: list[dict[str, Any]] = []
            for s, decision in decisions:
                if decision == detector.SKIP_RANDOM:
                    continue
                if decision == detector.UPDATE_KNOWN:
                    await session.execute(
                        text(
                            "UPDATE known_macs SET last_seen = :now, last_device_id = :d, "
                            "last_if_index = :i, last_vlan = :v WHERE mac = :m"
                        ),
                        {"now": now, "d": device_id, "i": s.if_index, "v": s.vlan, "m": s.mac},
                    )
                    continue
                det = details.get(s.mac, {}) if decision == detector.NEW else {}
                vendor = oui.vendor(s.mac)
                await session.execute(
                    text(
                        "INSERT INTO known_macs (mac, state, vendor, first_seen, first_device_id, "
                        "first_if_index, first_port_name, first_vlan, first_ip, first_hostname, "
                        "last_seen, last_device_id, last_if_index, last_vlan, detected_at) VALUES "
                        "(:m, :st, :vendor, :now, :d, :i, :pn, :v, :ip, :hn, :now, :d, :i, :v, "
                        ":det) ON CONFLICT (mac) DO NOTHING"
                    ),
                    {
                        "m": s.mac,
                        "st": decision,
                        "vendor": (vendor or None) and vendor[:64],
                        "now": now,
                        "d": device_id,
                        "i": s.if_index,
                        "pn": s.port_name[:255],
                        "v": s.vlan,
                        "ip": det.get("ip"),
                        "hn": (det.get("hostname") or None) and det["hostname"][:255],
                        "det": now if decision == detector.NEW else None,
                    },
                )
                if decision == detector.NEW:
                    new_items.append(
                        {
                            "sighting": s,
                            "vendor": vendor,
                            "ip": det.get("ip"),
                            "hostname": det.get("hostname"),
                        }
                    )
            await self._mark_baselined(session, device_id, dev.newdev_baselined_at, now)
            reported, more = detector.flood_split(new_items)
            evs = []
            for it in reported:
                s = it["sighting"]
                msg = detector.device_message(
                    s.mac, it["vendor"], dev.name, s.port_name, s.vlan, it["ip"], it["hostname"]
                )
                it["message"] = msg
                evs.append(
                    await events_svc.add_event(
                        session,
                        "new_device",
                        msg,
                        time=now,
                        device_id=device_id,
                        group_id=dev.group_id,
                        device_name=dev.name,
                    )
                )
            if more:
                evs.append(
                    await events_svc.add_event(
                        session,
                        "new_device",
                        detector.summary_message(more, dev.name),
                        time=now,
                        device_id=device_id,
                        group_id=dev.group_id,
                        device_name=dev.name,
                    )
                )
            await session.commit()
        self.last_detection_at = now
        if not new_items:
            return {"new": 0}
        clear = int(cfg["newdev_condition_clear_min"])
        es = rt.event_sensors
        if es is not None:
            exp = now + timedelta(minutes=clear)
            items = []
            for it in new_items:
                s = it["sighting"]
                msg = it.get("message") or detector.device_message(
                    s.mac, it["vendor"], dev.name, s.port_name, s.vlan, it["ip"], it["hostname"]
                )
                items.append(("new_mac", s.mac, "warning", msg, exp))
            await es.set_conditions(device_id, SENSOR_TYPE, items)
            sid = await self._sensor_id(device_id)
            if sid is not None and evs:
                async with rt.sessionmaker() as session:
                    await session.execute(
                        text(
                            "UPDATE events SET sensor_id = :s, sensor_name = 'New devices' "
                            "WHERE id = ANY(:ids)"
                        ),
                        {"s": sid, "ids": [e.id for e in evs]},
                    )
                    await session.commit()
                for e in evs:
                    e.sensor_id = sid
        events_svc.published(rt, evs)
        async with rt.sessionmaker() as session:
            out = await known_macs_out(session, [it["sighting"].mac for it in reported])
        rt.hub.broadcast("new_device.detected", {"items": out, "summary_count": more})
        return {"new": len(new_items), "summary_count": more}

    async def _mark_baselined(
        self, session: Any, device_id: int, current: Any, now: datetime
    ) -> None:
        if current is None:
            await session.execute(
                text(
                    "UPDATE port_inventory_status SET newdev_baselined_at = :now "
                    "WHERE device_id = :d"
                ),
                {"now": now, "d": device_id},
            )

    async def _sensor_id(self, device_id: int) -> int | None:
        async with self.rt.sessionmaker() as session:
            return (
                await session.execute(
                    text("SELECT id FROM sensors WHERE device_id = :d AND type = :t"),
                    {"d": device_id, "t": SENSOR_TYPE},
                )
            ).scalar()

    # ================================================================ admin actions
    async def acknowledge(self, macs: list[str], comment: str, user: str, ctx: Any) -> int:
        from netmon.services import audit

        now = self.rt.clock.now()
        async with self.rt.sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "UPDATE known_macs SET state = 'known', ack_at = :now, ack_by = :by, "
                            "ack_comment = :c WHERE mac = ANY(:m) RETURNING mac"
                        ),
                        {"now": now, "by": user[:64], "c": comment[:500], "m": macs},
                    )
                )
                .scalars()
                .all()
            )
            await audit.record(
                session,
                ctx,
                "newdev.acknowledge",
                audit.entity("known_mac", None, ", ".join(rows)[:256]),
                {"macs": [None, list(rows)], "comment": [None, comment]},
            )
            await session.commit()
        if rows and self.rt.event_sensors is not None:
            await self.rt.event_sensors.remove_device_conditions(
                None, SENSOR_TYPE, ["new_mac"], keys=list(rows)
            )
        return len(rows)

    async def forget(self, mac: str) -> bool:
        async with self.rt.sessionmaker() as session:
            n = (
                await session.execute(text("DELETE FROM known_macs WHERE mac = :m"), {"m": mac})
            ).rowcount
            await session.commit()
        if n and self.rt.event_sensors is not None:
            await self.rt.event_sensors.remove_device_conditions(
                None, SENSOR_TYPE, ["new_mac"], keys=[mac]
            )
        return bool(n)

    async def restart_learning(self) -> datetime:
        now = self.rt.clock.now()
        async with self.rt.sessionmaker() as session:
            days = int(await settings_svc.get(session, "newdev_learning_days"))
            until = now + timedelta(days=days)
            await settings_svc.put(session, "newdev_learning_until", until.isoformat())
            await session.commit()
        return until

    async def status(self, session: Any) -> dict[str, Any]:
        from netmon.ports.eligibility import load_flags

        cfg = await settings_svc.get_all(session)
        until = parse_dt(cfg.get("newdev_learning_until"))
        scope_ids = list(cfg["newdev_scope_group_ids"] or [])
        scope = await self.scope_device_ids(session, scope_ids)
        flags = await load_flags(session)
        switches = {
            did
            for did, f in flags.items()
            if getattr(f, "ports", False) and (scope is None or did in scope)
        }
        baselined = {
            int(r.device_id)
            for r in await session.execute(
                text(
                    "SELECT device_id FROM port_inventory_status "
                    "WHERE newdev_baselined_at IS NOT NULL"
                )
            )
        }
        counts = {
            r.state: int(r.n)
            for r in await session.execute(
                text("SELECT state, count(*) AS n FROM known_macs GROUP BY state")
            )
        }
        last = (await session.execute(text("SELECT max(detected_at) FROM known_macs"))).scalar()
        now = self.rt.clock.now()
        return {
            "learning": until is not None and now < until,
            "learning_until": iso(until),
            "scope_group_ids": scope_ids,
            "switches_in_scope": len(switches),
            "switches_baselined": len(switches & baselined),
            "known": sum(counts.values()),
            "new_unacknowledged": counts.get("new", 0),
            "last_detection_at": iso(self.last_detection_at or last),
        }


async def known_macs_out(session: Any, macs: list[str] | None = None, rows: Any = None) -> list:
    """api.md `KnownMac` for the given MACs (or the given rows), with current IP / hostname."""
    from netmon.ports import read as ports_read

    if rows is None:
        if not macs:
            return []
        rows = (
            (
                await session.execute(
                    text("SELECT * FROM known_macs WHERE mac = ANY(:m) ORDER BY mac"),
                    {"m": list(macs)},
                )
            )
            .mappings()
            .all()
        )
    rows = [dict(r) for r in rows]
    if not rows:
        return []
    dev_ids = {r["first_device_id"] for r in rows} | {r["last_device_id"] for r in rows}
    names = {
        int(r.id): r.name
        for r in await session.execute(
            text("SELECT id, name FROM devices WHERE id = ANY(:ids)"),
            {"ids": sorted(i for i in dev_ids if i is not None)},
        )
    }
    pnames = await ports_read.port_names(session, sorted(i for i in dev_ids if i is not None))
    rdns = bool(await settings_svc.get(session, "reverse_dns"))
    details = await ports_read.mac_details(session, [r["mac"] for r in rows], rdns)
    out = []
    for r in rows:
        det = details.get(r["mac"], {})
        last_port = None
        if r["last_device_id"] is not None and r["last_if_index"] is not None:
            last_port = pnames.get((r["last_device_id"], r["last_if_index"])) or (
                f"ifIndex {r['last_if_index']}"
            )
        out.append(
            {
                "mac": r["mac"],
                "state": r["state"],
                "vendor": r["vendor"],
                "locally_administered": detector.locally_administered(r["mac"]),
                "first_seen": iso(r["first_seen"]),
                "first_device_id": r["first_device_id"],
                "first_device_name": names.get(r["first_device_id"]),
                "first_if_index": r["first_if_index"],
                "first_port_name": r["first_port_name"],
                "first_vlan": r["first_vlan"],
                "first_ip": r["first_ip"],
                "first_hostname": r["first_hostname"],
                "last_seen": iso(r["last_seen"]),
                "last_device_id": r["last_device_id"],
                "last_device_name": names.get(r["last_device_id"]),
                "last_port_name": last_port,
                "last_vlan": r["last_vlan"],
                "ip": det.get("ip"),
                "hostname": det.get("hostname"),
                "detected_at": iso(r["detected_at"]),
                "ack_at": iso(r["ack_at"]),
                "ack_by": r["ack_by"],
                "ack_comment": r["ack_comment"] or "",
            }
        )
    return out


async def run_forget(rt: Any) -> int:
    """Daily (hourly loop): delete register rows not seen for `newdev_forget_after_days`
    (0 = never)."""
    async with rt.sessionmaker() as session:
        days = int(await settings_svc.get(session, "newdev_forget_after_days"))
        if days <= 0:
            return 0
        n = (
            await session.execute(
                text("DELETE FROM known_macs WHERE last_seen < :cut"),
                {"cut": rt.clock.now() - timedelta(days=days)},
            )
        ).rowcount
        await session.commit()
    return int(n or 0)
