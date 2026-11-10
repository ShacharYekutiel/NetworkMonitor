"""[3] Config backup: device context loading (effective SSH credentials, driver, host key,
status), eligibility and the API shapes (`BackupStatus`, `HostKey`, `ConfigVersion`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.configbackup import credentials, drivers, sysoid
from netmon.services.serialize import iso

REASONS = ("ok", "disabled_device", "mode_off", "no_credentials", "no_driver", "host_key_changed")


def eligibility(
    enabled: bool, mode: str, has_creds: bool, driver_id: str | None, host_key_state: str | None
) -> tuple[bool, str]:
    """Pure (architecture § 13.2.4): (eligible, reason)."""
    if not enabled:
        return False, "disabled_device"
    if mode == "off":
        return False, "mode_off"
    if not has_creds:
        return False, "no_credentials"
    if not driver_id:
        return False, "no_driver"
    if host_key_state == "changed":
        return False, "host_key_changed"
    return True, "ok"


@dataclass
class DeviceCtx:
    id: int
    name: str
    group_id: int
    host: str
    enabled: bool
    mode: str
    driver_override: str | None
    templates: list[str]
    sys_object_id: str | None
    ssh: dict[str, Any] | None
    ssh_source: str
    ssh_group_id: int | None
    driver: str | None = None
    driver_source: str | None = None
    host_key: dict[str, Any] | None = None
    status: dict[str, Any] | None = None
    versions: int = 0
    creds_unreadable: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def eligible(self) -> tuple[bool, str]:
        return eligibility(
            self.enabled,
            self.mode,
            self.ssh is not None and not self.creds_unreadable,
            self.driver,
            (self.host_key or {}).get("state"),
        )


def group_chain(parents: dict[int, tuple[int | None, Any]], gid: int | None) -> list[tuple]:
    out: list[tuple[int, Any]] = []
    seen: set[int] = set()
    while gid is not None and gid in parents and gid not in seen:
        seen.add(gid)
        parent, creds = parents[gid]
        out.append((gid, creds))
        gid = parent
    return out


async def load_contexts(
    session: AsyncSession,
    secret_key: str,
    device_ids: list[int] | None = None,
    *,
    with_status: bool = True,
) -> list[DeviceCtx]:
    groups = {
        r.id: (r.parent_id, r.ssh_credentials)
        for r in await session.execute(text("SELECT id, parent_id, ssh_credentials FROM groups"))
    }
    where = "" if device_ids is None else "WHERE id = ANY(:ids)"
    rows = (
        await session.execute(
            text(
                "SELECT id, name, group_id, host, enabled, config_backup, config_backup_driver, "
                f"identity, ssh_credentials FROM devices {where} ORDER BY name, id"  # noqa: S608
            ),
            {"ids": device_ids or []},
        )
    ).all()
    lib = drivers.library().drivers
    out: list[DeviceCtx] = []
    for r in rows:
        ident = r.identity or {}
        templates = [ident["suggested_template"]] if ident.get("suggested_template") else []
        stored, source, gid = credentials.resolve(
            r.ssh_credentials, group_chain(groups, r.group_id)
        )
        ctx = DeviceCtx(
            id=r.id,
            name=r.name,
            group_id=r.group_id,
            host=r.host,
            enabled=r.enabled,
            mode=r.config_backup,
            driver_override=r.config_backup_driver,
            templates=templates,
            # stored identity first; else a live-read sysObjectID (sysoid.probe) if cached
            sys_object_id=ident.get("sys_object_id") or sysoid.cached(r.id),
            ssh=stored,
            ssh_source=source,
            ssh_group_id=gid,
        )
        ctx.creds_unreadable = credentials.unreadable(stored, secret_key) if stored else False
        ctx.driver, ctx.driver_source = drivers.match_driver(
            lib, templates, ctx.sys_object_id, ctx.driver_override
        )
        out.append(ctx)
    if not out:
        return out
    ids = [c.id for c in out]
    by_id = {c.id: c for c in out}
    for r in (
        await session.execute(
            text("SELECT * FROM ssh_host_keys WHERE device_id = ANY(:ids)"), {"ids": ids}
        )
    ).mappings():
        by_id[r["device_id"]].host_key = dict(r)
    if with_status:
        for r in (
            await session.execute(
                text("SELECT * FROM config_backup_status WHERE device_id = ANY(:ids)"),
                {"ids": ids},
            )
        ).mappings():
            by_id[r["device_id"]].status = dict(r)
        for r in await session.execute(
            text(
                "SELECT device_id, count(*) AS n FROM config_versions WHERE device_id = ANY(:ids) "
                "GROUP BY device_id"
            ),
            {"ids": ids},
        ):
            by_id[r.device_id].versions = int(r.n)
    return out


def host_key_out(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "algorithm": row["algorithm"],
        "fingerprint": row["fingerprint"],
        "state": row["state"],
        "pinned_at": iso(row["pinned_at"]),
        "presented_algorithm": row.get("presented_algorithm"),
        "presented_fingerprint": row.get("presented_fingerprint"),
        "changed_at": iso(row.get("changed_at")),
        "accepted_by": row.get("accepted_by"),
    }


def status_out(c: DeviceCtx, running: bool) -> dict[str, Any]:
    st = c.status or {}
    ok, reason = c.eligible
    return {
        "device_id": c.id,
        "device_name": c.name,
        "group_id": c.group_id,
        "eligible": ok,
        "reason": reason,
        "mode": c.mode,
        "driver": c.driver,
        "driver_source": c.driver_source,
        "last_attempt_at": iso(st.get("last_attempt_at")),
        "last_result": st.get("last_result"),
        "last_error": st.get("last_error"),
        "last_message": st.get("last_message"),
        "last_trigger": st.get("last_trigger"),
        "last_duration_ms": st.get("last_duration_ms"),
        "last_success_at": iso(st.get("last_success_at")),
        "last_change_at": iso(st.get("last_change_at")),
        "consecutive_failures": int(st.get("consecutive_failures") or 0),
        "next_due_at": iso(st.get("next_due_at")),
        "running": running,
        "versions": c.versions,
        "host_key": host_key_out(c.host_key),
    }


VERSION_COLUMNS = (
    "id, device_id, collected_at, trigger, driver, sha256, masked, size_bytes, lines, added, "
    "removed, initial, is_latest"
)


def version_out(r: Any, version_no: int) -> dict[str, Any]:
    g = r.get if isinstance(r, dict) else (lambda k: r[k])
    return {
        "id": int(g("id")),
        "device_id": int(g("device_id")),
        "version_no": int(version_no),
        "collected_at": iso(g("collected_at")),
        "trigger": g("trigger"),
        "driver": g("driver"),
        "sha256": str(g("sha256")).strip(),
        "masked": bool(g("masked")),
        "size_bytes": int(g("size_bytes")),
        "lines": int(g("lines")),
        "added": int(g("added")),
        "removed": int(g("removed")),
        "initial": bool(g("initial")),
        "is_latest": bool(g("is_latest")),
    }


async def version_numbers(session: AsyncSession, device_id: int) -> dict[int, int]:
    """id -> version_no (row number per device by collected_at)."""
    rows = await session.execute(
        text(
            "SELECT id, row_number() OVER (ORDER BY collected_at, id) AS n FROM config_versions "
            "WHERE device_id = :d"
        ),
        {"d": device_id},
    )
    return {int(r.id): int(r.n) for r in rows}
