"""[profiles] Connection profiles (api.md "[profiles] Connection profiles"). Admin only: every
route depends on `require_admin` (a viewer or viewer token gets `403 forbidden`).

Secrets are write-only (responses carry null + `*_set`), never logged and never audited (the
audit masks them as "***"). A connection test is read-only probing: one SNMP GET of the system
group, or an SSH login (plus, with a known driver, its prompt / session-only setup commands);
no backup or other command, nothing stored, no host key pinned."""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select, text

from netmon.api.common import commit_or_conflict, flush_or_conflict
from netmon.api.deps import DB, RT, Admin, Audit, Page, require_admin
from netmon.api.errors import ApiError, not_found, validation_error
from netmon.models import ConnectionProfile, Device
from netmon.schemas import (
    ConnectionProfileIn,
    ConnectionProfilePatch,
    ProfileTestIn,
    ProfileTestNewIn,
)
from netmon.services import audit, conn_profiles
from netmon.services.serialize import page

router = APIRouter(dependencies=[Depends(require_admin)])

SNMP_TEST_OIDS = ("1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.2.0", "1.3.6.1.2.1.1.5.0")
SNMP_TEST_MAX_S = 30.0
SSH_TEST_MAX_S = 60.0
SYS_DESCR_MAX = 300


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _check_kind(kind: str, loc: list[str | int]) -> conn_profiles.KindSpec:
    sp = conn_profiles.KINDS.get(kind)
    if sp is None:
        raise validation_error(loc, f"unknown kind (one of {sorted(conn_profiles.KINDS)})")
    return sp


async def _unique_name(session: DB, kind: str, name: str, exclude: int | None) -> None:
    q = select(ConnectionProfile.id).where(
        ConnectionProfile.kind == kind, func.lower(ConnectionProfile.name) == name.lower()
    )
    if exclude is not None:
        q = q.where(ConnectionProfile.id != exclude)
    if (await session.execute(q)).first():
        raise ApiError(
            409,
            "conflict",
            "A connection profile of this kind already has this name",
            [{"field": "name"}],
        )


def _entity(p: Any) -> audit.Entity:
    return audit.entity("connection_profile", p.id, p.name)


def _audit_values(p: ConnectionProfile) -> dict[str, Any]:
    sp = conn_profiles.KINDS[p.kind]
    # SNMP: plaintext (community / v3 keys masked as "***" by the audit); SSH: the stored
    # form (every `*_enc` is masked as "***")
    creds = sp.audit_view(p.data, p.id) if p.kind == "snmp" else dict(p.data or {})
    return {
        "kind": p.kind,
        "name": p.name,
        "description": p.description,
        "credentials": creds,
    }


async def _counts(session: DB) -> tuple[dict[int, dict[str, int]], dict[int, int]]:
    from netmon.services.status import load_inventory

    direct = await conn_profiles.direct_counts(session)
    eff = conn_profiles.effective_device_counts(await load_inventory(session))
    return direct, eff


async def _out(session: DB, p: ConnectionProfile) -> dict[str, Any]:
    direct, eff = await _counts(session)
    return conn_profiles.profile_out(p, conn_profiles.usage_out(p.id, direct, eff))


async def _get(session: DB, profile_id: int) -> ConnectionProfile:
    p = await session.get(ConnectionProfile, profile_id)
    if p is None:
        raise not_found("Connection profile")
    return p


# ---------------------------------------------------------------- CRUD
@router.get("/api/connection-profiles")
async def list_profiles(
    _: Admin,
    session: DB,
    pg: Page,
    kind: str | None = Query(default=None, max_length=32),
    q: str | None = Query(default=None, max_length=128),
) -> dict[str, Any]:
    stmt = select(ConnectionProfile)
    if kind:
        _check_kind(kind, ["query", "kind"])
        stmt = stmt.where(ConnectionProfile.kind == kind)
    if q:
        stmt = stmt.where(ConnectionProfile.name.ilike(f"%{_like(q)}%", escape="\\"))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(
                    ConnectionProfile.kind,
                    func.lower(ConnectionProfile.name),
                    ConnectionProfile.id,
                )
                .offset(pg.offset)
                .limit(pg.page_size)
            )
        )
        .scalars()
        .all()
    )
    direct, eff = await _counts(session)
    items = [conn_profiles.profile_out(p, conn_profiles.usage_out(p.id, direct, eff)) for p in rows]
    return page(items, total, pg.page, pg.page_size)


@router.post("/api/connection-profiles", status_code=201)
async def create_profile(
    body: ConnectionProfileIn, _: Admin, rt: RT, session: DB, ctx: Audit
) -> dict[str, Any]:
    sp = _check_kind(body.kind, ["body", "kind"])
    await _unique_name(session, body.kind, body.name, None)
    incoming = conn_profiles.validate_credentials(
        body.kind, body.credentials, ("body", "credentials")
    )
    pending = sp.prepare(incoming, None, None, rt.secret_key, ("body", "credentials"))
    p = ConnectionProfile(
        kind=body.kind,
        name=body.name,
        description=body.description,
        data=sp.strip(pending),  # secrets sealed after the flush (the id is in the AAD)
    )
    session.add(p)
    await flush_or_conflict(session)
    p.data = sp.store(pending, p.id)
    await audit.record(
        session, ctx, "connection_profile.create", _entity(p), audit.created(_audit_values(p))
    )
    await commit_or_conflict(session)
    await session.refresh(p)
    rt.config_changed("connection_profile", p.id, "created")
    return await _out(session, p)


@router.get("/api/connection-profiles/{profile_id}")
async def get_profile(profile_id: int, _: Admin, session: DB) -> dict[str, Any]:
    return await _out(session, await _get(session, profile_id))


@router.patch("/api/connection-profiles/{profile_id}")
async def patch_profile(
    profile_id: int, body: ConnectionProfilePatch, _: Admin, rt: RT, session: DB, ctx: Audit
) -> dict[str, Any]:
    p = await _get(session, profile_id)
    fields = body.model_fields_set
    if "kind" in fields and body.kind != p.kind:
        raise validation_error(["body", "kind"], "the kind of a profile cannot be changed")
    sp = conn_profiles.KINDS[p.kind]
    before = _audit_values(p)
    if "name" in fields and body.name is not None and body.name != p.name:
        if body.name.lower() != p.name.lower():
            await _unique_name(session, p.kind, body.name, p.id)
        p.name = body.name
    if "description" in fields and body.description is not None:
        p.description = body.description
    if "credentials" in fields and body.credentials is not None:
        incoming = conn_profiles.validate_credentials(
            p.kind, body.credentials, ("body", "credentials")
        )
        pending = sp.prepare(incoming, p.data, p.id, rt.secret_key, ("body", "credentials"))
        new = sp.store(pending, p.id)
        if p.kind == "snmp":
            old_plain = sp.audit_view(p.data, p.id)
            if pending != old_plain:  # unchanged values re-sent by a form: no rewrite
                p.data = new
        elif new != p.data:
            p.data = new
    changes = audit.diff(before, _audit_values(p))
    if changes:
        await audit.record(session, ctx, "connection_profile.update", _entity(p), changes)
    await commit_or_conflict(session)
    await session.refresh(p)
    rt.config_changed("connection_profile", p.id, "updated")
    return await _out(session, p)


@router.delete("/api/connection-profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: int, _: Admin, rt: RT, session: DB, ctx: Audit) -> Response:
    row, counts = await conn_profiles.lock_for_delete(session, profile_id)
    if row is None:
        raise not_found("Connection profile")
    if any(counts.values()):
        raise ApiError(409, "profile_in_use", "The connection profile is in use", [counts])
    p = await _get(session, profile_id)
    await audit.record(
        session, ctx, "connection_profile.delete", _entity(p), audit.deleted(_audit_values(p))
    )
    await session.execute(text("DELETE FROM connection_profiles WHERE id = :i"), {"i": profile_id})
    try:
        await session.commit()
    except Exception as exc:  # the FK (RESTRICT) is the last safety net
        await session.rollback()
        if "fk_cpa_profile_kind" in str(exc):
            raise ApiError(409, "profile_in_use", "The connection profile is in use") from None
        raise
    rt.config_changed("connection_profile", profile_id, "deleted")
    return Response(status_code=204)


@router.get("/api/connection-profiles/{profile_id}/usage")
async def profile_usage(profile_id: int, _: Admin, session: DB) -> dict[str, Any]:
    await _get(session, profile_id)
    return await conn_profiles.usage(session, profile_id)


# ---------------------------------------------------------------- tests (read-only)
def _single_host(host: str) -> str:
    from netmon.discovery.targets import TargetError, parse_target

    try:
        kind, value = parse_target(0, host)
    except TargetError as exc:
        raise validation_error(["body", "host"], exc.message) from None
    if kind == "v4":
        lo, hi = cast(tuple[int, int], value)
        if lo != hi or "/" in host:
            raise validation_error(["body", "host"], "a single address or host name is required")
        import ipaddress

        return str(ipaddress.IPv4Address(lo))
    return str(value)


async def _target(session: DB, body: ProfileTestIn) -> tuple[str, Device | None, str]:
    if (body.device_id is None) == (body.host is None):
        raise validation_error(["body", "device_id"], "give device_id or host (one of them)")
    if body.device_id is not None:
        d = await session.get(Device, body.device_id)
        if d is None:
            raise not_found("Device")
        return d.host, d, f"device:{d.id}"
    host = _single_host(body.host or "")
    return host, None, f"host:{host}"


async def _run_test(
    rt: RT,
    session: DB,
    ctx: Any,
    kind: str,
    stored: Any,
    profile_id: int | None,
    profile_name: str | None,
    body: ProfileTestIn,
    entity_id: int | None = None,
) -> dict[str, Any]:
    """`stored`: the profile's stored data (`profile_id` set) or, for unsaved values, the
    pending form (SNMP plaintext, `profile_id` None). `entity_id`: the audited profile."""
    host, device, target = await _target(session, body)
    driver: Any = None
    host_key: dict[str, Any] | None = None
    if kind == "ssh":
        driver, host_key = await _ssh_prepare(rt, session, device, body)
    elif body.driver is not None:
        raise validation_error(["body", "driver"], "driver is only valid for SSH profiles")
    await session.close()
    ok = False
    try:
        if kind == "snmp":
            out = await _snmp_test(rt, host, stored, profile_id)
        else:
            out = await _ssh_test(host, stored, driver, host_key)
        ok = True
        return out
    finally:
        await audit.record_now(
            rt,
            ctx,
            "connection_profile.test",
            audit.entity("connection_profile", entity_id, profile_name),
            {"target": [None, target], "ok": [None, ok]},
        )


async def _snmp_test(rt: RT, host: str, stored: Any, profile_id: int | None) -> dict[str, Any]:
    from netmon.poller.snmp_client import DEFAULT_SNMP, SnmpError, as_text, snmp_get
    from netmon.services import secretbox

    plain = (
        secretbox.open_conn_profile_snmp(stored, profile_id)
        if profile_id is not None
        else dict(stored)
    )
    creds = {**DEFAULT_SNMP, **(plain or {})}
    t0 = time.monotonic()
    try:
        async with rt.semaphore:
            values = await asyncio.wait_for(
                snmp_get(host, creds, list(SNMP_TEST_OIDS)), SNMP_TEST_MAX_S
            )
    except TimeoutError:
        raise ApiError(502, "snmp_error", "SNMP test failed: timeout") from None
    except SnmpError as exc:
        raise ApiError(502, "snmp_error", f"SNMP test failed: {exc}"[:500]) from None
    descr, oid, name = (values.get(o) for o in SNMP_TEST_OIDS)
    return {
        "ok": True,
        "kind": "snmp",
        "host": host,
        "ms": int((time.monotonic() - t0) * 1000),
        "version": creds.get("version") or "v2c",
        "sys_name": as_text(name) or None if name is not None else None,
        "sys_descr": (as_text(descr)[:SYS_DESCR_MAX] or None) if descr is not None else None,
        "sys_object_id": str(oid) if oid is not None else None,
    }


async def _ssh_prepare(
    rt: RT, session: DB, device: Device | None, body: ProfileTestIn
) -> tuple[Any, dict[str, Any] | None]:
    """(driver or None, the device's host-key row or None)."""
    from netmon.configbackup import drivers, service

    if body.driver is not None and drivers.get(body.driver) is None:
        raise validation_error(["body", "driver"], "unknown driver")
    host_key = None
    driver_id = body.driver
    if device is not None:
        ctxs = await service.load_contexts(session, rt.secret_key, [device.id], with_status=False)
        if ctxs:
            host_key = ctxs[0].host_key
            driver_id = driver_id or ctxs[0].driver
    return (drivers.get(driver_id) if driver_id else None), host_key


def _ssh_error(
    code: str, message: str, step: str, steps: list[dict[str, Any]] | None = None
) -> ApiError:
    return ApiError(
        502,
        "ssh_error",
        f"{code}: {message}"[:500],
        [{"error": code, "step": step, "steps": steps or []}],
    )


async def _ssh_test(
    host: str, stored: Any, driver: Any, host_key: dict[str, Any] | None
) -> dict[str, Any]:
    from netmon.configbackup import credentials, normalize
    from netmon.configbackup.session import HostKeyInfo, SessionError, run_session

    try:
        creds = credentials.decrypt(stored or {}, credentials._SECRET_KEY or "")  # noqa: SLF001
    except credentials.CredentialsUnreadable:
        raise _ssh_error(
            "no_credentials", "The SSH credentials cannot be decrypted", "auth"
        ) from None

    def check(info: HostKeyInfo) -> bool:
        # a device's pinned key is enforced; otherwise the key is only reported. Never pins.
        if host_key is None:
            return True
        return normalize.host_key_decision(host_key, info.fingerprint) == "accept"

    t0 = time.monotonic()
    try:
        result = await run_session(
            host, creds, driver, check, total_timeout=SSH_TEST_MAX_S, commands=False
        )
    except SessionError as exc:
        raise _ssh_error(exc.code, exc.message, exc.step, exc.steps) from None
    return {
        "ok": True,
        "kind": "ssh",
        "host": host,
        "ms": int((time.monotonic() - t0) * 1000),
        "steps": result.steps,
        "driver": driver.id if driver is not None else None,
    }


@router.post("/api/connection-profiles/test")
async def test_new_profile(
    body: ProfileTestNewIn, _: Admin, rt: RT, session: DB, ctx: Audit
) -> dict[str, Any]:
    """A profile not saved yet (nothing is stored)."""
    sp = _check_kind(body.kind, ["body", "kind"])
    incoming = conn_profiles.validate_credentials(
        body.kind, body.credentials, ("body", "credentials")
    )
    pending = sp.prepare(incoming, None, None, rt.secret_key, ("body", "credentials"))
    return await _run_test(rt, session, ctx, body.kind, pending, None, None, body)


@router.post("/api/connection-profiles/{profile_id}/test")
async def test_profile(
    profile_id: int, body: ProfileTestIn, _: Admin, rt: RT, session: DB, ctx: Audit
) -> dict[str, Any]:
    p = await _get(session, profile_id)
    sp = conn_profiles.KINDS[p.kind]
    stored: Any = p.data
    pid: int | None = p.id
    if body.credentials is not None:  # unsaved edits merged over the stored profile
        incoming = conn_profiles.validate_credentials(
            p.kind, body.credentials, ("body", "credentials")
        )
        stored = sp.prepare(incoming, p.data, p.id, rt.secret_key, ("body", "credentials"))
        if p.kind == "snmp":
            pid = None  # `stored` is now plaintext
    kind, name = p.kind, p.name
    return await _run_test(rt, session, ctx, kind, stored, pid, name, body, profile_id)
