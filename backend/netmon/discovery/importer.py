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
from netmon.poller.snmp_client import DEFAULT_SNMP
from netmon.services import conn_profiles, secretbox
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


def snmp_choice(
    cred: dict[str, Any],
    index: int,
    group_effective: dict[str, Any],
    group_profile_id: int | None,
    profile_now: conn_profiles.Profile | None,
    port_used: int,
) -> tuple[str, int | None]:
    """[profiles] Pure import decision for an item whose SNMP answered (data-model
    "[profiles] Discovery"). `cred` = the credential that answered (scan snapshot, with
    `_profile_id`), `group_profile_id` = the profile the import group's effective SNMP comes
    from (or None), `profile_now` = that `_profile_id`'s profile as it is now (None =
    deleted). Returns ("inherit" | "profile" | "manual", profile id)."""
    pid = cred.get("_profile_id")
    # 1. credential 0 equal to the group's current effective credentials (the 2a rule)
    if index == 0 and _comparable(cred) == _comparable(group_effective):
        return "inherit", None
    if pid is not None:
        # 2. the import group already inherits that very profile
        if group_profile_id == pid:
            return "inherit", None
        # 3. the profile still exists and answered on its own port
        if profile_now is not None and profile_now.kind == "snmp":
            data = secretbox.open_conn_profile_snmp(profile_now.data, profile_now.id) or {}
            if int(port_used) == int(data.get("port") or 161):
                return "profile", int(pid)
    # 4. a copy of what answered
    return "manual", None


def ssh_choice(
    save: bool,
    ssh_ok: bool,
    candidate: dict[str, Any] | None,
    group_profile_id: int | None,
    profile_now: conn_profiles.Profile | None,
) -> tuple[str, int | None]:
    """[profiles] Pure SSH import decision (owner decision Q3). Returns ("inherit" |
    "profile" | "manual", profile id)."""
    if not save or not ssh_ok or candidate is None:
        return "inherit", None
    pid = candidate.get("_profile_id")
    if pid is not None:
        if group_profile_id == pid:
            return "inherit", None
        if profile_now is not None and profile_now.kind == "ssh":
            return "profile", int(pid)
    return "manual", None


def snmp_inherits_same(
    cred: dict[str, Any], group_effective: dict[str, Any], group_profile_id: int | None
) -> bool:
    """[profiles] Would a device imported into the group (SNMP "inherit") get the credential
    that answered in the scan? The same profile (by id), or the same resolved credential
    (every field except timeout_s / retries, port included). Pure; plaintext in, bool out."""
    pid = cred.get("_profile_id")
    if pid is not None and group_profile_id is not None and int(pid) == int(group_profile_id):
        return True
    return _comparable(cred) == _comparable(group_effective)


def ssh_inherits_same(
    cand: dict[str, Any],
    group_stored: dict[str, Any] | None,
    group_profile_id: int | None,
    secret_key: str,
) -> bool:
    """[profiles] The SSH counterpart: the same profile (by id), or the group's effective
    stored credentials decrypt to the very login that worked (username, port, secrets,
    legacy algorithms). Unreadable / incomplete credentials never count as the same."""
    from netmon.configbackup import credentials as ssh_creds

    pid = cand.get("_profile_id")
    if pid is not None and group_profile_id is not None and int(pid) == int(group_profile_id):
        return True
    if not group_stored:
        return False
    plain = {k: v for k, v in cand.items() if not k.startswith("_")}
    try:
        return ssh_creds.decrypt(plain, secret_key) == ssh_creds.decrypt(group_stored, secret_key)
    except ssh_creds.CredentialsUnreadable:
        return False


async def inheritance(rt: Runtime, scan: Any, group_id: int) -> dict[str, Any]:
    """[profiles] For each result with a working SNMP / SSH credential: does importing it into
    `group_id` WITHOUT saving that credential (the default: the device inherits) give the
    device the same credential? "same" | "differs" per kind, null = nothing worked. Plus the
    group's effective origins (api.md `CredentialOrigin`). Never a secret."""
    async with rt.sessionmaker() as session:
        gres = await conn_profiles.resolve_group(session, "snmp", group_id)
        sres = await conn_profiles.resolve_group(session, "ssh", group_id)
        rows = (
            await session.execute(
                text(
                    "SELECT id, snmp_ok, snmp_credential_index, ssh_ok, ssh_credential, "
                    "ssh_profile_id FROM discovery_results WHERE scan_id = :s ORDER BY ip"
                ),
                {"s": scan.id},
            )
        ).all()
    first = conn_profiles.ChainNode("group", group_id, None)
    group_snmp = conn_profiles.effective(gres)
    group_ssh = conn_profiles.effective(sres)
    snmp_pid = gres.profile_id if gres.mode == "profile" else None
    ssh_pid = sres.profile_id if sres.mode == "profile" else None
    credentials = secretbox.open_scan_credentials(scan.id, scan.credentials)
    items: list[dict[str, Any]] = []
    for row in rows:
        snmp: str | None = None
        idx = row.snmp_credential_index
        if row.snmp_ok and idx is not None and 0 <= idx < len(credentials):
            same = snmp_inherits_same(credentials[idx], group_snmp, snmp_pid)
            snmp = "same" if same else "differs"
        ssh: str | None = None
        cand = ssh_candidate(scan, row)
        if cand is not None:
            same = ssh_inherits_same(cand, group_ssh, ssh_pid, rt.secret_key)
            ssh = "same" if same else "differs"
        if snmp is not None or ssh is not None:
            items.append({"result_id": row.id, "snmp": snmp, "ssh": ssh})
    return {
        "group_id": group_id,
        "snmp_origin": conn_profiles.origin_of(gres, first),
        "ssh_origin": conn_profiles.origin_of(sres, first),
        "items": items,
    }


def ssh_candidate(scan: Any, row: Any) -> dict[str, Any] | None:
    """The scan snapshot candidate that logged in for a result row (None: no login)."""
    if not getattr(row, "ssh_ok", False):
        return None
    origin = getattr(row, "ssh_credential", None)
    pid = getattr(row, "ssh_profile_id", None)
    for cand in list(getattr(scan, "ssh_candidates", None) or []):
        if cand.get("_origin") == origin and cand.get("_profile_id") == pid:
            return dict(cand)
    return None


def _error(rid: int, code: str, message: str, device_id: int | None = None) -> dict[str, Any]:
    return {"result_id": rid, "code": code, "message": message, "device_id": device_id}


async def import_results(
    rt: Runtime,
    scan: Any,
    group_id: int,
    items: list[dict[str, Any]],
    lang: str,
    budget_s: float | None = None,
    audit_ctx: Any = None,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (IMPORT_BUDGET_S if budget_s is None else budget_s)
    async with rt.sessionmaker() as session:
        # [profiles] the import group's effective SNMP / SSH (profile-aware) and their origin
        gres = await conn_profiles.resolve_group(session, "snmp", group_id)
        sres = await conn_profiles.resolve_group(session, "ssh", group_id)
    group_creds = conn_profiles.effective(gres)
    group_pids = {
        "snmp": gres.profile_id if gres.mode == "profile" else None,
        "ssh": sres.profile_id if sres.mode == "profile" else None,
    }
    credentials = secretbox.open_scan_credentials(scan.id, scan.credentials)  # [1.4.2]
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
                    scan=scan,
                    group_pids=group_pids,
                    audit_ctx=audit_ctx,
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
    *,
    scan: Any = None,
    group_pids: dict[str, int | None] | None = None,
    audit_ctx: Any = None,
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
            snmp_credentials=None,  # [1.4.2] sealed below (the row id is part of the AAD)
            sort_order=0,
            identity=identity_from_result(row),
        )
        session.add(device)
        await session.flush()
        device_id = device.id
        modes = await _store_credentials(
            session,
            device,
            row,
            item,
            credentials,
            group_creds,
            group_pids or {},
            scan,
            audit_ctx,
        )
        # same transaction: a retry after any later failure sees device_exists
        await session.execute(
            text("UPDATE discovery_results SET imported_device_id = :d WHERE id = :id"),
            {"d": device_id, "id": rid},
        )
        await session.commit()
    rt.config_changed("device", device_id, "created")
    template = get_template(item["template_id"])
    assert template is not None
    # [1.3] interface sensors on important ports only (a switch: after one bounded port
    # inventory run, see templates.selection)
    from netmon.templates.selection import REFRESH_WAIT_S

    result = await apply_template(
        rt,
        device_id,
        template,
        lang,
        budget_s=apply_budget_s,
        broadcast=False,
        interfaces="important",
        refresh_s=REFRESH_WAIT_S,
    )
    return "created", {
        "result_id": rid,
        "device_id": device_id,
        "device_name": name,
        "template_id": item["template_id"],
        "sensors_created": len(result["created"]),
        "sensors_skipped": result["skipped"],
        **modes,  # [profiles] snmp_mode / snmp_profile_id / ssh_mode / ssh_profile_id
    }


async def _store_credentials(
    session: Any,
    device: Device,
    row: Any,
    item: dict[str, Any],
    credentials: list[dict[str, Any]],
    group_creds: dict[str, Any],
    group_pids: dict[str, int | None],
    scan: Any,
    audit_ctx: Any,
) -> dict[str, Any]:
    """[profiles] SNMP and SSH of an imported device (same transaction): what worked is
    linked (profile), inherited or copied (manual) only when the item says `save_snmp` /
    `save_ssh` true; omitted / null / false = the device inherits from the import group
    (owner decision for 1.6.0: an import never overrides inheritance unless asked)."""
    out: dict[str, Any] = {
        "snmp_mode": None,
        "snmp_profile_id": None,
        "ssh_mode": None,
        "ssh_profile_id": None,
    }
    # ---- SNMP
    idx = row.snmp_credential_index
    if row.snmp_ok and idx is not None and 0 <= idx < len(credentials):
        cred = credentials[idx]
        if not item.get("save_snmp"):
            mode, pid = "inherit", None
        else:
            pid0 = cred.get("_profile_id")
            now = (await conn_profiles.load_profiles(session, [pid0])).get(pid0) if pid0 else None
            mode, pid = snmp_choice(
                cred, idx, group_creds, group_pids.get("snmp"), now, int(cred.get("port") or 161)
            )
        if mode == "profile" and pid is not None:
            if await _try_assign(session, "snmp", device.id, pid):
                device.snmp_credentials = None
            else:  # deleted meanwhile: a copy of what answered
                mode, pid = "manual", None
        if mode == "manual":
            device.snmp_credentials = secretbox.seal_snmp(_copy(cred), "device", device.id)
        out["snmp_mode"], out["snmp_profile_id"] = mode, pid
        if mode == "profile" and pid is not None:
            from netmon.services import audit

            await audit.record(
                session,
                audit_ctx,
                "device.update",
                audit.entity("device", device.id, device.name, device_id=device.id),
                {
                    "snmp_mode": ["inherit", "profile"],
                    "snmp_profile": [None, now.name if now else None],
                },
                reason="discovery_import",
            )
    # ---- SSH (owner decision Q3)
    cand = ssh_candidate(scan, row) if scan is not None else None
    if cand is not None:
        save = bool(item.get("save_ssh"))
        pid0 = cand.get("_profile_id")
        now = (await conn_profiles.load_profiles(session, [pid0])).get(pid0) if pid0 else None
        mode, pid = ssh_choice(save, bool(row.ssh_ok), cand, group_pids.get("ssh"), now)
        if (
            mode == "profile"
            and pid is not None
            and not await _try_assign(session, "ssh", device.id, pid)
        ):
            mode, pid = "manual", None
        if mode == "manual":
            device.ssh_credentials = {k: v for k, v in cand.items() if not k.startswith("_")}
        out["ssh_mode"], out["ssh_profile_id"] = mode, pid
        if save and mode != "inherit":
            from netmon.services import audit

            name = now.name if (now is not None and mode == "profile") else None
            await audit.record(
                session,
                audit_ctx,
                "device.update",
                audit.entity("device", device.id, device.name, device_id=device.id),
                {
                    "ssh_mode": ["inherit", mode],
                    "ssh_profile": [None, name],
                    **(
                        {"ssh_credentials": [None, device.ssh_credentials]}
                        if mode == "manual"
                        else {}
                    ),
                },
                reason="discovery_import",
            )
    return out


def _copy(cred: dict[str, Any]) -> dict[str, Any]:
    creds = {k: cred.get(k) for k in SNMP_FIELDS}
    creds["timeout_s"] = 2
    creds["retries"] = 1
    return creds


async def _try_assign(session: Any, kind: str, device_id: int, profile_id: int) -> bool:
    """Assign when the profile still exists with this kind (`FOR SHARE`); False otherwise."""
    from netmon.api.errors import ApiError

    try:
        await conn_profiles.lock_profile_for_use(session, kind, profile_id, ["profile_id"])
    except ApiError:
        return False
    await conn_profiles.assign(session, kind, "device", device_id, profile_id)
    return True
