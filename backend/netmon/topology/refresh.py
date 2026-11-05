"""[2b-1] Topology refresh: walk SNMP devices, match neighbours, upsert links, stale handling,
neighbour promotion. One refresh at a time (docs/data-model.md "Refresh")."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from netmon.poller.sensor_types import SENSOR_TYPES
from netmon.poller.snmp_client import resolve_credentials
from netmon.services.serialize import iso
from netmon.topology.collect import (
    DeviceData,
    collect_with_budget,
    name_keys,
    norm_ip,
    port_name,
    resolve_local_port,
    resolve_remote_port,
)

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.topology")

CONCURRENCY = 8
DEVICE_BUDGET_S = 30.0
STALE_AFTER = 3
REFRESH_BUDGET_S = 900.0  # backstop for a whole refresh (devices have their own 30 s)
KEEP_REFRESHES = 20
SNMP_TYPES = tuple(t for t, d in SENSOR_TYPES.items() if d.requires_snmp)


class RefreshRunningError(Exception):
    def __init__(self, refresh_id: int | None) -> None:
        super().__init__("A topology refresh is already running")
        self.refresh_id = refresh_id


def refresh_out(row: Any) -> dict[str, Any]:
    m = row._mapping if hasattr(row, "_mapping") else row  # noqa: SLF001
    return {
        "id": m["id"],
        "trigger": m["trigger"],
        "status": m["status"],
        "started_at": iso(m["started_at"]),
        "finished_at": iso(m["finished_at"]),
        "devices_total": m["devices_total"],
        "devices_ok": m["devices_ok"],
        "devices_failed": m["devices_failed"],
        "links_found": m["links_found"],
        "error": m["error"],
    }


@dataclass
class _Dev:
    id: int
    name: str
    host: str
    chassis: str | None
    sys_name: str | None


class Matcher:
    """Neighbour -> device matching (first rule that yields a device; ties -> lowest id)."""

    def __init__(self, devices: list[_Dev]) -> None:
        self.by_host: dict[str, list[int]] = {}
        self.by_chassis: dict[str, list[int]] = {}
        self.by_name: dict[str, list[int]] = {}
        for d in sorted(devices, key=lambda x: x.id):
            ip = norm_ip(d.host)
            if ip:
                self.by_host.setdefault(ip, []).append(d.id)
            if d.chassis:
                self.by_chassis.setdefault(d.chassis.lower(), []).append(d.id)
            for n in (d.sys_name, d.name, d.host):
                for k in name_keys(n):
                    lst = self.by_name.setdefault(k, [])
                    if d.id not in lst:
                        lst.append(d.id)

    def match(
        self, mgmt: list[str], chassis: str | None, sys_name: str | None, exclude: int | None
    ) -> int | None:
        def first(ids: list[int]) -> int | None:
            ids = [i for i in ids if i != exclude]
            return min(ids) if ids else None

        for addr in mgmt:
            ip = norm_ip(addr)
            if ip and (hit := first(self.by_host.get(ip, []))) is not None:
                return hit
        if chassis and (hit := first(self.by_chassis.get(chassis.lower(), []))) is not None:
            return hit
        keys = name_keys(sys_name)
        for k in sorted(keys, key=len, reverse=True):
            if (hit := first(self.by_name.get(k, []))) is not None:
                return hit
        return None


class TopologyRunner:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._task: asyncio.Task[None] | None = None
        self._current: int | None = None
        self._lock = asyncio.Lock()
        self.device_budget_s = DEVICE_BUDGET_S
        self.total_budget_s = REFRESH_BUDGET_S

    @property
    def running_id(self) -> int | None:
        if self._task is not None and not self._task.done():
            return self._current
        return None

    async def mark_interrupted(self) -> int:
        async with self.rt.sessionmaker() as session:
            res = await session.execute(
                text(
                    "UPDATE topology_refreshes SET status = 'failed', "
                    "error = 'Interrupted by restart', finished_at = now() "
                    "WHERE status = 'running'"
                )
            )
            await session.commit()
            return res.rowcount or 0

    async def start(self, trigger: str = "manual", created_by: int | None = None) -> dict[str, Any]:
        async with self._lock:
            if self.running_id is not None:
                raise RefreshRunningError(self.running_id)
            async with self.rt.sessionmaker() as session:
                try:
                    row = (
                        await session.execute(
                            text(
                                "INSERT INTO topology_refreshes (trigger, status, created_by) "
                                "VALUES (:t, 'running', :u) RETURNING *"
                            ),
                            {"t": trigger, "u": created_by},
                        )
                    ).one()
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    # no refresh runs in this process (checked above): a `running` row is a
                    # leftover (a previous process died, or its final write failed)
                    n = await self.mark_interrupted()
                    log.warning("Cleared %d stale running topology refresh row(s)", n)
                    try:
                        row = (
                            await session.execute(
                                text(
                                    "INSERT INTO topology_refreshes (trigger, status, "
                                    "created_by) VALUES (:t, 'running', :u) RETURNING *"
                                ),
                                {"t": trigger, "u": created_by},
                            )
                        ).one()
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()
                        rid = (
                            await session.execute(
                                text("SELECT id FROM topology_refreshes WHERE status = 'running'")
                            )
                        ).scalar()
                        raise RefreshRunningError(rid) from None
            out = refresh_out(row)
            self._current = int(row.id)
            self._task = asyncio.create_task(self._run(int(row.id)), name="netmon-topology")
            self.rt.hub.broadcast("topology.refresh", {"refresh": out}, admin_only=True)
            return out

    async def wait(self, timeout: float = 120.0) -> None:
        if self._task is not None:
            await asyncio.wait_for(asyncio.shield(self._task), timeout)

    async def shutdown(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task

    async def _run(self, refresh_id: int) -> None:
        stats = {"devices_total": 0, "devices_ok": 0, "devices_failed": 0, "links_found": 0}
        status, error = "completed", None
        log.info("Topology refresh %s started", refresh_id)
        try:
            stats = await asyncio.wait_for(self.refresh(stats), self.total_budget_s)
        except asyncio.CancelledError:
            status, error = "failed", "Cancelled by shutdown"
        except TimeoutError:
            log.warning("Topology refresh %s timed out", refresh_id)
            status, error = "failed", f"Timed out after {self.total_budget_s:g} s"
        except Exception as exc:  # noqa: BLE001
            log.exception("Topology refresh %s failed", refresh_id)
            status, error = "failed", (str(exc) or type(exc).__name__)[:1000]
        row = None
        for attempt in range(3):
            try:
                row = await self._finish(refresh_id, status, error, stats)
                break
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Cannot finish topology refresh %s (attempt %d)", refresh_id, attempt)
                await asyncio.sleep(0.5 * (attempt + 1))
        log.info(
            "Topology refresh %s %s: %d/%d devices ok, %d links",
            refresh_id,
            status,
            stats["devices_ok"],
            stats["devices_total"],
            stats["links_found"],
        )
        if row is None:
            return  # the row stays `running`; the next start() clears it
        self.rt.hub.broadcast("topology.refresh", {"refresh": refresh_out(row)}, admin_only=True)
        self.rt.hub.broadcast("topology.updated", {"reason": "refresh", "refresh_id": refresh_id})

    async def _finish(
        self, refresh_id: int, status: str, error: str | None, stats: dict[str, int]
    ) -> Any:
        async with self.rt.sessionmaker() as session:
            row = (
                await session.execute(
                    text(
                        "UPDATE topology_refreshes SET status = :st, error = :err, "
                        "finished_at = now(), devices_total = :t, devices_ok = :ok, "
                        "devices_failed = :f, links_found = :l WHERE id = :id RETURNING *"
                    ),
                    {
                        "st": status,
                        "err": error,
                        "t": stats["devices_total"],
                        "ok": stats["devices_ok"],
                        "f": stats["devices_failed"],
                        "l": stats["links_found"],
                        "id": refresh_id,
                    },
                )
            ).one()
            await session.execute(
                text(
                    "DELETE FROM topology_refreshes WHERE status <> 'running' AND id NOT IN "
                    "(SELECT id FROM topology_refreshes ORDER BY id DESC LIMIT :k)"
                ),
                {"k": KEEP_REFRESHES},
            )
            await session.commit()
        return row

    # ---------------------------------------------------------------- the refresh
    async def refresh(self, stats: dict[str, int]) -> dict[str, int]:
        from netmon.services.status import load_inventory

        async with self.rt.sessionmaker() as session:
            inv = await load_inventory(session)
            rows = (
                await session.execute(
                    text(
                        "SELECT id, name, host, enabled, group_id, snmp_credentials, "
                        "lldp_chassis_id, lldp_sys_name FROM devices ORDER BY id"
                    )
                )
            ).all()
            snmp_devices = set(
                (
                    await session.execute(
                        text("SELECT DISTINCT device_id FROM sensors WHERE type = ANY(:t)"),
                        {"t": list(SNMP_TYPES)},
                    )
                ).scalars()
            )
        targets = []
        for r in rows:
            if not r.enabled or r.id not in snmp_devices:
                continue
            chain = [(g, inv.groups[g]["snmp_credentials"]) for g in inv.ancestors(r.group_id)]
            creds, _, _ = resolve_credentials(r.snmp_credentials, chain)
            targets.append((r, creds))
        stats["devices_total"] = len(targets)
        sem = asyncio.Semaphore(CONCURRENCY)

        async def walk(r: Any, creds: dict[str, Any]) -> tuple[int, DeviceData]:
            async with sem:
                return r.id, await collect_with_budget(r.host, creds, self.device_budget_s)

        results = dict(await asyncio.gather(*(walk(r, c) for r, c in targets)))
        ok_ids = sorted(i for i, d in results.items() if d.ok)
        stats["devices_ok"] = len(ok_ids)
        stats["devices_failed"] = len(results) - len(ok_ids)

        devs = {r.id: _Dev(r.id, r.name, r.host, r.lldp_chassis_id, r.lldp_sys_name) for r in rows}
        for did in ok_ids:
            d = results[did]
            devs[did].chassis = d.chassis_id or devs[did].chassis
            devs[did].sys_name = d.sys_name or devs[did].sys_name
        matcher = Matcher(list(devs.values()))

        # candidate links keyed by the unique index columns
        links: dict[tuple[Any, ...], dict[str, Any]] = {}
        neighbors: dict[str, dict[str, Any]] = {}

        def add_link(
            source: str,
            a: int,
            a_if: int | None,
            a_port: str,
            b_dev: int | None,
            b_nb: str | None,
            b_if: int | None,
            b_port: str,
        ) -> None:
            if b_dev is not None and b_dev < a:
                a, a_if, a_port, b_dev, b_if, b_port = b_dev, b_if, b_port, a, a_if, a_port
            key = (source, a, a_port[:255], b_dev or 0, b_nb or "", b_port[:255])
            cur = links.get(key)
            if cur is None:
                links[key] = {
                    "source": source,
                    "a": a,
                    "a_if": a_if,
                    "a_port": a_port[:255],
                    "b_dev": b_dev,
                    "b_nb": b_nb,
                    "b_if": b_if,
                    "b_port": b_port[:255],
                }
            else:
                cur["a_if"] = cur["a_if"] if cur["a_if"] is not None else a_if
                cur["b_if"] = cur["b_if"] if cur["b_if"] is not None else b_if

        for did in ok_ids:
            d = results[did]
            for rem in d.remotes:
                a_if, a_port = resolve_local_port(
                    d.local_ports.get(rem.local_port), rem.local_port, d.if_names, d.if_descrs
                )
                target = matcher.match(rem.mgmt_addresses, rem.chassis_id, rem.sys_name, did)
                if target is not None:
                    other = results.get(target)
                    b_if = None
                    b_port = rem.port_id or rem.port_desc
                    if other is not None:
                        b_if = resolve_remote_port(
                            rem.port_subtype,
                            rem.port_id,
                            rem.port_desc,
                            other.if_names,
                            other.if_descrs,
                        )
                        if b_if is not None:
                            b_port = port_name(b_if, other.if_names, other.if_descrs) or b_port
                    add_link("lldp", did, a_if, a_port, target, None, b_if, b_port)
                    continue
                if self._is_self(rem.chassis_id, devs[did]):
                    continue
                chassis = rem.chassis_id or (f"name:{rem.sys_name}" if rem.sys_name else "")
                if not chassis:
                    continue
                neighbors[chassis] = {
                    "chassis_id": chassis[:255],
                    "sys_name": rem.sys_name[:255] or None,
                    "sys_descr": rem.sys_descr[:1000] or None,
                    "mgmt_address": rem.mgmt_addresses[0][:64] if rem.mgmt_addresses else None,
                }
                add_link(
                    "lldp", did, a_if, a_port, None, chassis, None, rem.port_id or rem.port_desc
                )
            for c in d.cdp:
                a_port = port_name(c.if_index, d.if_names, d.if_descrs) or str(c.if_index)
                a_if = (
                    c.if_index if (c.if_index in d.if_names or c.if_index in d.if_descrs) else None
                )
                target = matcher.match([c.address] if c.address else [], None, c.device_id, did)
                if target is not None:
                    other = results.get(target)
                    b_if = None
                    if other is not None:
                        b_if = resolve_remote_port(
                            5, c.device_port, "", other.if_names, other.if_descrs
                        )
                    add_link("cdp", did, a_if, a_port, target, None, b_if, c.device_port)
                    continue
                chassis = f"cdp:{c.device_id}"[:255]
                neighbors[chassis] = {
                    "chassis_id": chassis,
                    "sys_name": c.device_id[:255] or None,
                    "sys_descr": c.platform[:1000] or None,
                    "mgmt_address": c.address,
                }
                add_link("cdp", did, a_if, a_port, None, chassis, None, c.device_port)

        async with self.rt.sessionmaker() as session:
            for did in ok_ids:
                d = results[did]
                await session.execute(
                    text(
                        "UPDATE devices SET lldp_chassis_id = COALESCE(:c, lldp_chassis_id), "
                        "lldp_sys_name = COALESCE(:n, lldp_sys_name) WHERE id = :id"
                    ),
                    {"c": d.chassis_id, "n": d.sys_name, "id": did},
                )
            nb_ids: dict[str, int] = {}
            for chassis, nb in neighbors.items():
                nb_ids[chassis] = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO topology_neighbors (chassis_id, sys_name, sys_descr,
                                mgmt_address)
                            VALUES (:chassis_id, :sys_name, :sys_descr, :mgmt_address)
                            ON CONFLICT (chassis_id) DO UPDATE SET
                                sys_name = COALESCE(EXCLUDED.sys_name, topology_neighbors.sys_name),
                                sys_descr = COALESCE(EXCLUDED.sys_descr,
                                                     topology_neighbors.sys_descr),
                                mgmt_address = COALESCE(EXCLUDED.mgmt_address,
                                                        topology_neighbors.mgmt_address),
                                last_seen = now()
                            RETURNING id
                            """
                        ),
                        nb,
                    )
                ).scalar_one()
            seen: set[int] = set()
            for ln in links.values():
                nb_id = nb_ids.get(ln["b_nb"]) if ln["b_nb"] else None
                seen.add(await _upsert_link(session, ln, nb_id))
            seen |= await self._promote(session, matcher, seen, results)
            # a miss counts only for an endpoint that was walked ok AND returned LLDP/CDP data
            # (an agent whose LLDP tables came back empty says nothing about its links)
            lldp_ok = [
                i for i in ok_ids if results[i].local_ports or results[i].remotes or results[i].cdp
            ]
            if lldp_ok:
                await session.execute(
                    text(
                        "UPDATE topology_links SET missed_refreshes = missed_refreshes + 1, "
                        "stale = (missed_refreshes + 1 >= :n) "
                        "WHERE source <> 'manual' AND NOT (id = ANY(:seen)) "
                        "AND (a_device_id = ANY(:ok) OR b_device_id = ANY(:ok))"
                    ),
                    {"n": STALE_AFTER, "seen": sorted(seen), "ok": lldp_ok},
                )
            await session.execute(
                text(
                    "DELETE FROM topology_neighbors n WHERE NOT EXISTS "
                    "(SELECT 1 FROM topology_links l WHERE l.b_neighbor_id = n.id)"
                )
            )
            await cleanup_layout(session)
            await session.commit()
        stats["links_found"] = len(seen)
        return stats

    @staticmethod
    def _is_self(chassis: str, dev: _Dev) -> bool:
        return bool(chassis and dev.chassis and chassis.lower() == dev.chassis.lower())

    async def _promote(
        self,
        session: Any,
        matcher: Matcher,
        seen: set[int],
        results: dict[int, DeviceData] | None = None,
    ) -> set[int]:
        """Neighbours that now match a device: re-point their links to the device (merging
        duplicates, keeping the oldest first_seen), move the layout row, delete them."""
        out: set[int] = set()
        nbs = (
            await session.execute(
                text("SELECT id, chassis_id, sys_name, mgmt_address FROM topology_neighbors")
            )
        ).all()
        for nb in nbs:
            chassis = None if nb.chassis_id.startswith(("cdp:", "name:")) else nb.chassis_id
            target = matcher.match(
                [nb.mgmt_address] if nb.mgmt_address else [], chassis, nb.sys_name, None
            )
            if target is None:
                continue
            links = (
                await session.execute(
                    text("SELECT * FROM topology_links WHERE b_neighbor_id = :n"), {"n": nb.id}
                )
            ).all()
            for ln in links:
                if ln.a_device_id == target:
                    await session.execute(
                        text("DELETE FROM topology_links WHERE id = :id"), {"id": ln.id}
                    )
                    continue
                local, local_port = ln.a_device_id, ln.a_port
                a, a_if, a_port = ln.a_device_id, ln.a_if_index, ln.a_port
                b, b_if, b_port = target, ln.b_if_index, ln.b_port
                other = (results or {}).get(target)
                if other is not None:  # resolve the port on the newly matched device
                    idx = resolve_remote_port(5, b_port, b_port, other.if_names, other.if_descrs)
                    if idx is not None:
                        b_if = idx
                        b_port = port_name(idx, other.if_names, other.if_descrs) or b_port
                if b < a:
                    a, a_if, a_port, b, b_if, b_port = b, b_if, b_port, a, a_if, a_port
                # a parallel link between the same devices on the same local port (e.g. the
                # direct link this refresh found) absorbs it: no duplicate edges
                dup = (
                    await session.execute(
                        text(
                            "SELECT id, first_seen FROM topology_links WHERE source = :s AND "
                            "id <> :id AND ((a_device_id = :l AND a_port = :lp "
                            "AND b_device_id = :t) OR (a_device_id = :t AND b_device_id = :l "
                            "AND b_port = :lp)) ORDER BY id LIMIT 1"
                        ),
                        {"s": ln.source, "l": local, "lp": local_port, "t": target, "id": ln.id},
                    )
                ).first()
                if dup is not None:
                    await session.execute(
                        text(
                            "UPDATE topology_links SET first_seen = LEAST(first_seen, :fs), "
                            "a_if_index = COALESCE(a_if_index, :ai), "
                            "b_if_index = COALESCE(b_if_index, :bi) WHERE id = :id"
                        ),
                        {"fs": ln.first_seen, "ai": a_if, "bi": b_if, "id": dup.id},
                    )
                    await session.execute(
                        text("DELETE FROM topology_links WHERE id = :id"), {"id": ln.id}
                    )
                    if ln.id in seen:
                        out.add(dup.id)
                else:
                    await session.execute(
                        text(
                            "UPDATE topology_links SET a_device_id = :a, a_if_index = :ai, "
                            "a_port = :ap, b_device_id = :b, b_neighbor_id = NULL, "
                            "b_if_index = :bi, b_port = :bp WHERE id = :id"
                        ),
                        {
                            "a": a,
                            "ai": a_if,
                            "ap": a_port,
                            "b": b,
                            "bi": b_if,
                            "bp": b_port,
                            "id": ln.id,
                        },
                    )
            has_layout = (
                await session.execute(
                    text("SELECT 1 FROM topology_layout WHERE node_key = :k"),
                    {"k": f"d:{target}"},
                )
            ).first()
            if has_layout is None:
                await session.execute(
                    text("UPDATE topology_layout SET node_key = :d WHERE node_key = :n"),
                    {"d": f"d:{target}", "n": f"n:{nb.id}"},
                )
            await session.execute(
                text("DELETE FROM topology_neighbors WHERE id = :id"), {"id": nb.id}
            )
            log.info("Topology neighbour %s promoted to device %s", nb.chassis_id, target)
        return out


async def _upsert_link(session: Any, ln: dict[str, Any], nb_id: int | None) -> int:
    return int(
        (
            await session.execute(
                text(
                    """
                    INSERT INTO topology_links (source, a_device_id, a_if_index, a_port,
                        b_device_id, b_neighbor_id, b_if_index, b_port)
                    VALUES (:source, :a, :a_if, :a_port, :b_dev, :b_nb, :b_if, :b_port)
                    ON CONFLICT (source, a_device_id, a_port, (COALESCE(b_device_id, 0)),
                                 (COALESCE(b_neighbor_id, 0)), b_port)
                    DO UPDATE SET last_seen = now(), missed_refreshes = 0, stale = false,
                        a_if_index = COALESCE(EXCLUDED.a_if_index, topology_links.a_if_index),
                        b_if_index = COALESCE(EXCLUDED.b_if_index, topology_links.b_if_index)
                    RETURNING id
                    """
                ),
                {
                    "source": ln["source"],
                    "a": ln["a"],
                    "a_if": ln["a_if"],
                    "a_port": ln["a_port"],
                    "b_dev": ln["b_dev"],
                    "b_nb": nb_id,
                    "b_if": ln["b_if"],
                    "b_port": ln["b_port"],
                },
            )
        ).scalar_one()
    )


async def cleanup_layout(session: Any) -> None:
    await session.execute(
        text(
            "DELETE FROM topology_layout l WHERE "
            "(l.node_key LIKE 'd:%' AND NOT EXISTS (SELECT 1 FROM devices d "
            "WHERE 'd:' || d.id = l.node_key)) OR "
            "(l.node_key LIKE 'n:%' AND NOT EXISTS (SELECT 1 FROM topology_neighbors n "
            "WHERE 'n:' || n.id = l.node_key))"
        )
    )
