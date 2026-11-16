"""ScanRunner: owns the single running discovery scan.

Hosts are probed with the scanner's own semaphore (NETMON_DISCOVERY_CONCURRENCY) and a start
rate limit (NETMON_DISCOVERY_RATE), so the poller keeps running. Probing happens in separate
tasks that cancel cleanly; DB writes are never cancelled half-way."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from netmon.discovery import options as opts_mod
from netmon.discovery.classify import classify_host, suggest_template
from netmon.discovery.fingerprint import FINGERPRINT_PORTS, fingerprint_host
from netmon.discovery.probe import HOST_TIMEOUT_S, ProbeResult, probe_host, resolve_hostname
from netmon.discovery.serialize import ip_text, result_out
from netmon.discovery.targets import HostTarget, expand_targets, rejected_reason
from netmon.poller.snmp_client import resolve_credentials
from netmon.services import events as events_svc
from netmon.services import secretbox
from netmon.services import settings as settings_svc
from netmon.services.sensors import group_chain
from netmon.services.serialize import iso, now_utc

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.discovery")

KEEP_SCANS = 20
FINGERPRINT_BACKSTOP_S = 30.0
CANCEL_WAIT_S = 4.5  # the API answers within 5 s
MAX_NOTIFY_HOSTS = 50
EXISTING_DEVICE_SQL = (
    "(SELECT min(d.id) FROM devices d WHERE d.host = host({ip}) "
    "OR ({hn} IS NOT NULL AND lower(d.host) = lower({hn})))"
)


class ScanRunningError(Exception):
    def __init__(self, scan_id: int | None) -> None:
        super().__init__("A discovery scan is already running")
        self.scan_id = scan_id


@dataclass
class _Scan:
    id: int
    hosts: list[HostTarget]
    options: dict[str, Any]
    credentials: list[dict[str, Any]]
    trigger: str
    profile_id: int | None
    profile_name: str | None
    group_id: int
    baseline: bool
    total: int = 0
    scanned: int = 0
    found: int = 0
    new: int = 0
    cancel: bool = False
    probes: set[asyncio.Task[Any]] = field(default_factory=set)
    task: asyncio.Task[None] | None = None
    flushed: tuple[int, int, int, int] | None = None

    def counters(self) -> dict[str, int]:
        return {
            "total_hosts": self.total,
            "scanned_hosts": self.scanned,
            "found_hosts": self.found,
            "new_hosts": self.new,
        }


class ScanRunner:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._current: _Scan | None = None
        self.last_started: Any = None
        self._lock = asyncio.Lock()
        self.concurrency = int(rt.settings.netmon_discovery_concurrency)
        self.rate = int(rt.settings.netmon_discovery_rate)
        self.host_timeout_s = HOST_TIMEOUT_S
        self.progress_interval_s = 1.0

    # ------------------------------------------------------------------ state
    @property
    def running_scan_id(self) -> int | None:
        cur = self._current
        if cur is not None and cur.task is not None and not cur.task.done():
            return cur.id
        return None

    @property
    def busy(self) -> bool:
        return self.running_scan_id is not None

    async def mark_interrupted(self) -> int:
        """Startup: scans left `running` by a crash become `failed`."""
        async with self.rt.sessionmaker() as session:
            res = await session.execute(
                text(
                    "UPDATE discovery_scans SET status = 'failed', "
                    "error = 'Interrupted by restart', finished_at = now() "
                    "WHERE status = 'running'"
                )
            )
            await session.commit()
            return res.rowcount or 0

    async def _db_running_id(self) -> int | None:
        async with self.rt.sessionmaker() as session:
            return (
                await session.execute(
                    text("SELECT id FROM discovery_scans WHERE status = 'running' LIMIT 1")
                )
            ).scalar_one_or_none()

    # ------------------------------------------------------------------ start
    async def start(
        self,
        *,
        targets: list[str],
        group_id: int,
        options: dict[str, Any],
        trigger: str = "manual",
        profile_id: int | None = None,
        profile_name: str | None = None,
        created_by: int | None = None,
        baseline: bool = False,
    ) -> int:
        """Create the scan row (`running`) and start the task. Raises ScanRunningError, or
        TargetError / TooManyHostsError for invalid targets."""
        async with self._lock:
            if self.busy:
                raise ScanRunningError(self.running_scan_id)
            hosts = expand_targets(targets)
            full = opts_mod.merge_options(None, options)
            async with self.rt.sessionmaker() as session:
                chain = await group_chain(session, group_id)
                group_creds, _, _ = resolve_credentials(None, chain)
                credentials = opts_mod.build_credentials(group_creds, full) if full["snmp"] else []
                try:
                    row = (
                        await session.execute(
                            text(
                                """
                                INSERT INTO discovery_scans (profile_id, profile_name, trigger,
                                    status, group_id, targets, options, credentials, total_hosts,
                                    created_by)
                                VALUES (:pid, :pname, :trigger, 'running', :gid, :targets,
                                    CAST(:options AS jsonb), CAST(:creds AS jsonb), :total, :uid)
                                RETURNING id, profile_id, profile_name, trigger, status,
                                    group_id, targets, options, total_hosts, scanned_hosts,
                                    found_hosts, new_hosts, error, created_by, started_at,
                                    finished_at
                                """
                            ),
                            {
                                "pid": profile_id,
                                "pname": profile_name,
                                "trigger": trigger,
                                "gid": group_id,
                                "targets": list(targets),
                                "options": _json(opts_mod.scan_snapshot(full)),
                                # [1.4.2] written below, encrypted with the scan id in the AAD
                                "creds": "[]",
                                "total": len(hosts),
                                "uid": created_by,
                            },
                        )
                    ).one()
                    scan_id = row.id
                    if credentials:
                        await session.execute(
                            text(
                                "UPDATE discovery_scans SET credentials = CAST(:c AS jsonb) "
                                "WHERE id = :i"
                            ),
                            {
                                "c": _json(
                                    secretbox.seal_scan_credentials(int(scan_id), credentials)
                                ),
                                "i": scan_id,
                            },
                        )
                    if profile_id is not None:
                        await session.execute(
                            text(
                                "UPDATE discovery_profiles SET last_run_at = now(), "
                                "last_scan_id = :sid WHERE id = :pid"
                            ),
                            {"sid": scan_id, "pid": profile_id},
                        )
                    await session.commit()
                except IntegrityError as exc:
                    await session.rollback()
                    if "ux_discovery_scans_running" in str(exc.orig):
                        raise ScanRunningError(await self._db_running_id()) from None
                    raise
            scan = _Scan(
                id=int(scan_id),
                hosts=hosts,
                options=full,
                credentials=credentials,
                trigger=trigger,
                profile_id=profile_id,
                profile_name=profile_name,
                group_id=group_id,
                baseline=baseline,
                total=len(hosts),
            )
            self._current = scan
            self.last_started = row  # the row as created (status running), for the 202 response
            scan.task = asyncio.create_task(self._run(scan), name=f"netmon-scan-{scan.id}")
            self._publish_progress(scan)
            log.info("Discovery scan %s started (%d hosts, %s)", scan.id, scan.total, trigger)
            return scan.id

    # ------------------------------------------------------------------ cancel / shutdown
    async def cancel(self, scan_id: int, wait_s: float | None = None) -> bool:
        """Stop the running scan. Waits at most `wait_s` for the task; if it is still
        finishing, the row is marked `cancelled` here (the runner's final write keeps it)."""
        cur = self._current
        if cur is None or cur.id != scan_id or cur.task is None or cur.task.done():
            return False
        cur.cancel = True
        wait_s = CANCEL_WAIT_S if wait_s is None else wait_s
        deadline = asyncio.get_running_loop().time() + wait_s
        while not cur.task.done() and asyncio.get_running_loop().time() < deadline:
            for t in list(cur.probes):
                t.cancel()
            await asyncio.wait({cur.task}, timeout=0.1)
        if not cur.task.done():
            async with self.rt.sessionmaker() as session:
                await session.execute(
                    text(
                        "UPDATE discovery_scans SET status = 'cancelled', "
                        "finished_at = COALESCE(finished_at, now()) "
                        "WHERE id = :id AND status = 'running'"
                    ),
                    {"id": scan_id},
                )
                await session.commit()
        return True

    async def shutdown(self) -> None:
        cur = self._current
        if cur is not None and cur.task is not None and not cur.task.done():
            await self.cancel(cur.id)
            if not cur.task.done():
                cur.task.cancel()
                with contextlib.suppress(BaseException):
                    await cur.task

    async def wait(self, timeout: float = 120.0) -> None:
        """Wait for the running scan to finish (tests)."""
        cur = self._current
        if cur is not None and cur.task is not None:
            await asyncio.wait_for(asyncio.shield(cur.task), timeout)

    # ------------------------------------------------------------------ run
    async def _run(self, scan: _Scan) -> None:
        status, error = "completed", None
        flusher = asyncio.create_task(self._flush_loop(scan))
        try:
            await self._scan_hosts(scan)
            status = "cancelled" if scan.cancel else "completed"
        except asyncio.CancelledError:
            status, error = "cancelled", None
        except Exception as exc:  # noqa: BLE001
            log.exception("Discovery scan %s failed", scan.id)
            status, error = "failed", (str(exc) or type(exc).__name__)[:1000]
        finally:
            flusher.cancel()
            with contextlib.suppress(BaseException):
                await flusher
        try:
            await self._finish(scan, status, error)
        except Exception:  # noqa: BLE001
            log.exception("Cannot finish discovery scan %s", scan.id)

    async def _scan_hosts(self, scan: _Scan) -> None:
        loop = asyncio.get_running_loop()
        sem = asyncio.Semaphore(self.concurrency)
        interval = 1.0 / max(1, self.rate)
        next_start = loop.time()
        tasks: set[asyncio.Task[None]] = set()
        try:
            for host in scan.hosts:
                if scan.cancel:
                    break
                await sem.acquire()
                if scan.cancel:
                    sem.release()
                    break
                now = loop.time()
                if next_start > now:
                    await asyncio.sleep(next_start - now)
                if scan.cancel:
                    sem.release()
                    break
                next_start = max(next_start, now) + interval
                task = asyncio.create_task(self._one_host(scan, host))
                tasks.add(task)
                task.add_done_callback(lambda t: (sem.release(), tasks.discard(t)))
            if tasks:
                await asyncio.gather(*list(tasks), return_exceptions=True)
        finally:
            if tasks:
                for t in list(scan.probes):
                    t.cancel()
                await asyncio.gather(*list(tasks), return_exceptions=True)

    async def _probe_target(
        self, scan: _Scan, host: HostTarget, holder: dict[str, ProbeResult]
    ) -> ProbeResult | None:
        ip = host.address
        if ip is None:
            assert host.hostname is not None
            ip = await resolve_hostname(host.hostname)
            if ip is None:
                return None
            try:
                if rejected_reason(ipaddress.ip_address(ip)):
                    return None
            except ValueError:
                return None
        # the result object is shared with the caller, so a backstop timeout keeps what the
        # probes already found (ping / TCP / SNMP identity)
        holder["res"] = ProbeResult(ip=ip, hostname=host.hostname, target_hostname=host.hostname)
        return await probe_host(
            ip, scan.options, scan.credentials, hostname=host.hostname, result=holder["res"]
        )

    async def _one_host(self, scan: _Scan, host: HostTarget) -> None:
        if scan.cancel:
            return
        holder: dict[str, ProbeResult] = {}
        probe = asyncio.create_task(
            asyncio.wait_for(self._probe_target(scan, host, holder), self.host_timeout_s)
        )
        scan.probes.add(probe)
        res: ProbeResult | None
        try:
            res = await probe
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            return  # the scan was cancelled: this host did not finish
        except TimeoutError:
            res = holder.get("res")  # backstop hit: keep the partial result
            log.info("Probe of %s hit the %ss backstop", host.label, self.host_timeout_s)
        except Exception as exc:  # noqa: BLE001
            log.warning("Probe of %s failed: %s", host.label, type(exc).__name__)
            res = holder.get("res")
        finally:
            scan.probes.discard(probe)
        scan.scanned += 1
        if res is None or not res.found:
            return
        try:
            await self._record(scan, res)
        except Exception:  # noqa: BLE001
            log.exception("Cannot store discovery result for %s", res.ip)

    async def _record(self, scan: _Scan, res: ProbeResult) -> None:
        fp = None
        if scan.options.get("fingerprint", True) and set(res.open_ports) & set(FINGERPRINT_PORTS):
            try:
                fp = await asyncio.wait_for(
                    fingerprint_host(
                        res.ip,
                        res.target_hostname or res.ip,
                        res.open_ports,
                        int(scan.options.get("fingerprint_timeout_ms", 2000)),
                    ),
                    FINGERPRINT_BACKSTOP_S,
                )
            except TimeoutError:
                fp = None
            except Exception as exc:  # noqa: BLE001
                log.debug("Fingerprint of %s failed: %s", res.ip, type(exc).__name__)
        c = classify_host(
            res.sys_object_id,
            res.sys_descr,
            res.sys_name,
            res.probes,
            res.pf,
            res.open_ports,
            res.snmp_ok,
            fp,
        )
        device_class, vendor = c.device_class, c.vendor
        template = suggest_template(
            {
                "device_class": device_class,
                "vendor": vendor,
                "snmp_ok": res.snmp_ok,
                "open_ports": res.open_ports,
                "sys_object_id": res.sys_object_id,
                "sys_descr": res.sys_descr,
            }
        )
        async with self.rt.sessionmaker() as session:
            pre = (
                await session.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM discovery_known_hosts "
                        "WHERE ip = CAST(:ip AS inet)) AS known, "
                        + EXISTING_DEVICE_SQL.format(ip="CAST(:ip AS inet)", hn="CAST(:hn AS text)")
                        + " AS existing"
                    ),
                    {"ip": res.ip, "hn": res.hostname},
                )
            ).one()
            is_new = (not pre.known) and pre.existing is None
            row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO discovery_results (scan_id, ip, hostname, mac, ping_ok,
                            ping_rtt_ms, snmp_ok, snmp_version, snmp_credential_index, sys_name,
                            sys_descr, sys_object_id, open_ports, device_class, vendor,
                            suggested_template, is_new, fingerprint, model, classified_by,
                            confidence)
                        VALUES (:scan_id, CAST(:ip AS inet), :hostname, :mac, :ping_ok,
                            :ping_rtt_ms, :snmp_ok, :snmp_version, :cred_idx, :sys_name,
                            :sys_descr, :sys_object_id, :open_ports, :device_class, :vendor,
                            :template, :is_new, CAST(:fingerprint AS jsonb), :model,
                            :classified_by, :confidence)
                        ON CONFLICT (scan_id, ip) DO NOTHING
                        RETURNING *
                        """
                    ),
                    {
                        "scan_id": scan.id,
                        "ip": res.ip,
                        "hostname": res.hostname,
                        "mac": res.mac,
                        "ping_ok": res.ping_ok,
                        "ping_rtt_ms": res.ping_rtt_ms,
                        "snmp_ok": res.snmp_ok,
                        "snmp_version": res.snmp_version,
                        "cred_idx": res.snmp_credential_index,
                        "sys_name": res.sys_name,
                        "sys_descr": res.sys_descr,
                        "sys_object_id": res.sys_object_id,
                        "open_ports": list(res.open_ports),
                        "device_class": device_class,
                        "vendor": vendor,
                        "template": template,
                        "is_new": is_new,
                        "fingerprint": _json(fp) if fp is not None else None,
                        "model": c.model,
                        "classified_by": c.classified_by,
                        "confidence": c.confidence,
                    },
                )
            ).first()
            await session.commit()
        if row is None:
            return  # the same address was already found through another target
        scan.found += 1
        if is_new:
            scan.new += 1
        self.rt.hub.broadcast(
            "discovery.result",
            {"scan_id": scan.id, "result": result_out(row, pre.existing)},
            admin_only=True,
        )

    # ------------------------------------------------------------------ progress
    def _publish_progress(self, scan: _Scan) -> None:
        self.rt.hub.broadcast(
            "discovery.progress",
            {"scan_id": scan.id, "status": "running", **scan.counters()},
            admin_only=True,
        )

    async def _flush(self, scan: _Scan) -> bool:
        snap = (scan.total, scan.scanned, scan.found, scan.new)
        if snap == scan.flushed:
            return False
        async with self.rt.sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE discovery_scans SET scanned_hosts = :s, found_hosts = :f, "
                    "new_hosts = :n WHERE id = :id AND status = 'running'"
                ),
                {"s": scan.scanned, "f": scan.found, "n": scan.new, "id": scan.id},
            )
            await session.commit()
        scan.flushed = snap
        return True

    async def _flush_loop(self, scan: _Scan) -> None:
        scan.flushed = (scan.total, 0, 0, 0)
        while True:
            await asyncio.sleep(self.progress_interval_s)
            try:
                if await self._flush(scan):
                    self._publish_progress(scan)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Cannot flush discovery progress: %s", exc)

    # ------------------------------------------------------------------ finish
    async def _finish(self, scan: _Scan, status: str, error: str | None) -> None:
        if scan.cancel:
            status, error = "cancelled", None  # a cancel always wins
        async with self.rt.sessionmaker() as session:
            new_count = (
                await session.execute(
                    text(
                        "SELECT count(*) FILTER (WHERE is_new), count(*) "
                        "FROM discovery_results WHERE scan_id = :id"
                    ),
                    {"id": scan.id},
                )
            ).one()
            scan.new, scan.found = int(new_count[0]), int(new_count[1])
            await session.execute(
                text(
                    "UPDATE discovery_scans SET "
                    "status = CASE WHEN status = 'cancelled' THEN 'cancelled' ELSE :st END, "
                    "error = CASE WHEN status = 'cancelled' THEN NULL ELSE :err END, "
                    "finished_at = now(), "
                    "scanned_hosts = :s, found_hosts = :f, new_hosts = :n WHERE id = :id"
                ),
                {
                    "st": status,
                    "err": error,
                    "s": scan.scanned,
                    "f": scan.found,
                    "n": scan.new,
                    "id": scan.id,
                },
            )
            if status in ("completed", "cancelled"):
                await session.execute(
                    text(
                        """
                        INSERT INTO discovery_known_hosts (ip, first_seen_at, last_seen_at,
                            last_scan_id)
                        SELECT ip, now(), now(), scan_id FROM discovery_results
                        WHERE scan_id = :id
                        ON CONFLICT (ip) DO UPDATE SET last_seen_at = now(),
                            last_scan_id = EXCLUDED.last_scan_id
                        """
                    ),
                    {"id": scan.id},
                )
            await session.commit()
        if self._current is scan:
            self._current = None
        self.rt.hub.broadcast(
            "discovery.finished",
            {"scan_id": scan.id, "status": status, **scan.counters(), "error": error},
            admin_only=True,
        )
        log.info(
            "Discovery scan %s %s: %d scanned, %d found, %d new",
            scan.id,
            status,
            scan.scanned,
            scan.found,
            scan.new,
        )
        if scan.trigger == "scheduled":
            try:
                await self._after_scheduled(scan, status, error)
            except Exception:  # noqa: BLE001
                log.exception("Discovery notifications for scan %s failed", scan.id)
        try:
            await self.prune()
        except Exception:  # noqa: BLE001
            log.exception("Cannot prune discovery scans")

    async def prune(self, keep: int = KEEP_SCANS) -> int:
        """Delete all scans except the newest `keep` by id (never the running one)."""
        async with self.rt.sessionmaker() as session:
            res = await session.execute(
                text(
                    "DELETE FROM discovery_scans WHERE status <> 'running' AND id NOT IN "
                    "(SELECT id FROM discovery_scans ORDER BY id DESC LIMIT :keep)"
                ),
                {"keep": keep},
            )
            await session.commit()
            return res.rowcount or 0

    # ------------------------------------------------------------------ notifications
    async def _after_scheduled(self, scan: _Scan, status: str, error: str | None) -> None:
        name = scan.profile_name or f"#{scan.profile_id}"
        if status == "failed":
            await self._write_discovery_event(f'Discovery "{name}" failed: {error or ""}')
            return
        if status != "completed" or scan.new <= 0 or scan.baseline:
            return
        async with self.rt.sessionmaker() as session:
            hosts = (
                await session.execute(
                    text(
                        "SELECT ip, hostname, device_class, vendor FROM discovery_results "
                        "WHERE scan_id = :id AND is_new ORDER BY ip"
                    ),
                    {"id": scan.id},
                )
            ).all()
            profile = None
            if scan.profile_id is not None:
                profile = (
                    await session.execute(
                        text(
                            "SELECT p.id, p.name, p.notify_new_hosts, p.notify_channel_ids, "
                            "g.id AS gid, g.name AS gname FROM discovery_profiles p "
                            "LEFT JOIN groups g ON g.id = p.group_id WHERE p.id = :id"
                        ),
                        {"id": scan.profile_id},
                    )
                ).first()
            cfg = await settings_svc.get_all(session)
        host_dicts = [
            {
                "ip": ip_text(h.ip),
                "hostname": h.hostname,
                "device_class": h.device_class,
                "vendor": h.vendor,
            }
            for h in hosts
        ]
        if profile is not None:
            name = profile.name
        total = len(host_dicts)
        listed = []
        for h in host_dicts[:20]:
            parts = [p for p in (h["device_class"], h["vendor"]) if p and p != "unknown"]
            listed.append(h["ip"] + (f" ({', '.join(parts)})" if parts else ""))
        more = f", +{total - 20} more" if total > 20 else ""
        noun = "host" if total == 1 else "hosts"
        ev_id = await self._write_discovery_event(
            f'Discovery "{name}": {total} new {noun}: ' + ", ".join(listed) + more
        )
        if profile is None or not profile.notify_new_hosts or not profile.notify_channel_ids:
            return
        base = cfg.get("public_base_url")
        payload = {
            "event": "discovery.new_hosts",
            "event_id": ev_id,
            "time": iso(now_utc()),
            "old_status": None,
            "new_status": None,
            "message": f'Discovery "{name}": {total} new {noun}',
            "sensor": None,
            "device": None,
            "group": {"id": profile.gid, "name": profile.gname} if profile.gid else None,
            "rule": None,
            "profile": {"id": profile.id, "name": profile.name},
            "scan_id": scan.id,
            "hosts": host_dicts[:MAX_NOTIFY_HOSTS],
            "url": f"{base.rstrip('/')}/discovery/scans/{scan.id}" if base else None,
        }
        await self.send_notifications(list(profile.notify_channel_ids), payload, cfg)

    async def send_notifications(
        self, channel_ids: list[int], payload: dict[str, Any], cfg: dict[str, Any]
    ) -> None:
        from netmon.alerts.engine import send_to_channel

        async with self.rt.sessionmaker() as session:
            channels = (
                await session.execute(
                    text(
                        "SELECT id, name, type, config FROM notification_channels "
                        "WHERE id = ANY(:ids) AND enabled ORDER BY id"
                    ),
                    {"ids": channel_ids},
                )
            ).all()
        sender = self.rt.alerts.sender if self.rt.alerts is not None else send_to_channel
        for ch in channels:
            channel = {
                "id": ch.id,
                "name": ch.name,
                "type": ch.type,
                "config": secretbox.open_channel(ch.type, ch.id, ch.config),  # [1.4.2]
            }
            try:
                await sender(channel, payload, cfg["smtp"], cfg["default_language"])
            except Exception as exc:  # noqa: BLE001
                log.warning("Discovery notification via %s failed: %s", ch.name, exc)
                await events_svc.write_notification_event(self.rt, f"{ch.name}: {exc}"[:1000])

    async def _write_discovery_event(self, message: str) -> int | None:
        try:
            async with self.rt.sessionmaker() as session:
                ev = await events_svc.add_event(session, "discovery", message[:4000])
                await session.commit()
            events_svc.published(self.rt, [ev])
            return int(ev.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Cannot write discovery event: %s", exc)
            return None


def _json(value: Any) -> str:
    import json

    return json.dumps(value)


def next_run(now: datetime, hours: int) -> datetime:
    return now + timedelta(hours=hours)
