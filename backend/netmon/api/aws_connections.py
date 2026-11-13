"""[4] Shared AWS connections (api.md § [4] "Shared AWS connections", architecture § 15.1.6).

Admin and session only (API tokens are barred on `/api/aws/*`). Always available: a connection
is configuration, not a feature (no toggle). Secrets are write-only (`*_set`,
`*_unreadable`) and never logged or audited; the ExternalId never reaches the audit log."""

from __future__ import annotations

import asyncio
import secrets
import string
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from netmon.api.assistant_settings import _read_form
from netmon.api.deps import DB, RT, Admin, Audit, SessionUser
from netmon.api.errors import ApiError
from netmon.aws import certs, policies
from netmon.aws import connections as conns
from netmon.aws import testconn as awstest
from netmon.aws import transport as tp
from netmon.schemas.assistant import CsrIn
from netmon.schemas.aws import (
    AwsAccessIn,
    AwsConnectionFields,
    AwsConnectionPatch,
    AwsTestIn,
    CertCopyIn,
    DefaultIn,
)
from netmon.services import audit
from netmon.services.serialize import iso, now_utc

router = APIRouter()

ACCESS_KEY_OLD_DAYS = 90
EXTERNAL_ID_ALPHABET = string.ascii_letters + string.digits
AUDIT_SECRET_INPUTS = ("secret_access_key", "session_token")


# ---------------------------------------------------------------- helpers
def _test_mode(rt: Any) -> bool:
    return bool(getattr(rt.settings, "netmon_test_mode", False))


def _validation(errors: list[dict[str, Any]], message: str = "Invalid AWS connection") -> None:
    if errors:
        raise ApiError(422, "validation_error", message, errors)


async def _row_or_404(session: Any, conn_id: int, *, lock: bool = False) -> dict[str, Any]:
    row = await conns.get_row(session, conn_id, lock=lock)
    if row is None:
        raise ApiError(404, "not_found", "AWS connection not found")
    return row


def _assistant_on(rt: Any) -> bool:
    return bool(rt.features.enabled("assistant"))


async def _after_write(rt: Any, conn_id: int | None) -> None:
    """Drop the connection's chains and let the assistant pick up the change."""
    aws = getattr(rt, "aws", None)
    if aws is not None:
        aws.invalidate(conn_id)
    svc = getattr(rt, "assistant", None)
    if svc is not None:
        await svc.reload()


def _warnings(rt: Any, v: dict[str, Any], cert_warns: list[str]) -> list[str]:
    out = list(cert_warns)
    if v.get("auth_mode") == "roles_anywhere" and conns.arn_account_warning(v):
        out.append("arn_accounts_differ")
    if v.get("auth_mode") == "access_key":
        if v.get("session_token_enc"):
            out.append("temporary_credentials")
        created = v.get("access_key_created_at")
        if created:
            try:
                age = (datetime.now(UTC).date() - date.fromisoformat(str(created)[:10])).days
                if age >= ACCESS_KEY_OLD_DAYS:
                    out.append("access_key_old")
            except ValueError:
                pass
    for key in ("sts_endpoint_url", "ra_endpoint_url"):
        url = v.get(key)
        if url and tp.is_vpce(url) and tp.via(url, v) != "direct":
            out.append("vpce_via_proxy")
    return list(dict.fromkeys(out))


def _cert_info(rt: Any, v: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    info, warns = rt.assistant.cert_status_view(v)
    if info is not None:
        warns = [*warns, *info.get("warnings", [])]
    return info, warns


async def connection_out(rt: Any, session: Any, row: dict[str, Any]) -> dict[str, Any]:
    v = conns.view(row)
    sk = rt.secret_key
    info, cert_warns = (
        _cert_info(rt, v)
        if v.get("auth_mode") == "roles_anywhere" or v.get("ra_cert_pem")
        else (None, [])
    )
    secret_set = bool(v.get("secret_access_key_enc"))
    token_set = bool(v.get("session_token_enc"))
    csr = None
    if v.get("ra_csr_pem"):
        csr = {
            "subject": v.get("ra_csr_subject"),
            "created_at": v.get("ra_csr_created_at"),
            "pem": v.get("ra_csr_pem"),
            "key_unreadable": conns.unreadable(v, "ra_csr_key_enc", sk),
        }
    aws = getattr(rt, "aws", None)
    _, unresolved = await conns.host_checks(v, test_mode=_test_mode(rt))
    warnings = _warnings(rt, v, cert_warns)
    if unresolved:
        warnings.append(conns.UNRESOLVED_WARNING)
    return {
        "id": row["id"],
        "name": row["name"],
        "is_default": bool(row["is_default"]),
        "auth_mode": row["auth_mode"],
        "region": row["region"],
        "proxy_mode": row["proxy_mode"],
        "proxy_url": row.get("proxy_url"),
        "endpoint_overrides": list(row.get("endpoint_overrides") or []),
        "access_key_id": v.get("access_key_id"),
        "secret_access_key": None,
        "secret_access_key_set": secret_set,
        "secret_access_key_unreadable": secret_set
        and conns.unreadable(v, "secret_access_key_enc", sk),
        "session_token": None,
        "session_token_set": token_set,
        "session_token_unreadable": token_set and conns.unreadable(v, "session_token_enc", sk),
        "access_key_created_at": v.get("access_key_created_at"),
        "ra_region": v.get("ra_region"),
        "ra_trust_anchor_arn": v.get("ra_trust_anchor_arn"),
        "ra_profile_arn": v.get("ra_profile_arn"),
        "ra_role_arn": v.get("ra_role_arn"),
        "ra_session_seconds": v.get("ra_session_seconds"),
        "certificate": info,
        "certificate_host_files": conns.uses_host_files(v),
        "csr": csr,
        "used_by": await conns.used_by(session, row, assistant_on=_assistant_on(rt)),
        "chains": aws.cache.status_for(row["id"]) if aws is not None else [],
        "last_test": row.get("last_test"),
        "warnings": warnings,
        "unresolved_hosts": unresolved,
        "created_at": iso(row["created_at"]) if row.get("created_at") else None,
        "updated_at": iso(row["updated_at"]) if row.get("updated_at") else None,
    }


def _audit_changes(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    """Changed field names with values; secrets only as `*_set`; no certificate material."""
    b = conns.view(before) if before is not None else {}
    a = conns.view(after)
    out: dict[str, Any] = {}
    if (before or {}).get("name") != after.get("name"):
        out["name"] = [(before or {}).get("name"), after.get("name")]
    for f in ("auth_mode", "region", "proxy_mode", "proxy_url", "endpoint_overrides",
              "access_key_id", "access_key_created_at", "ra_region", "ra_trust_anchor_arn",
              "ra_profile_arn", "ra_role_arn", "ra_session_seconds"):  # fmt: skip
        if b.get(f) != a.get(f):
            out[f] = [b.get(f), a.get(f)]
    for stored, name in (("secret_access_key_enc", "secret_access_key_set"),
                         ("session_token_enc", "session_token_set")):  # fmt: skip
        if b.get(stored) != a.get(stored):
            out[name] = [bool(b.get(stored)), bool(a.get(stored))]
    return out


def _entity(row: dict[str, Any]) -> Any:
    return audit.entity("aws_connection", row.get("id"), row.get("name"))


def _missing_create(fields: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"loc": ["body", f], "msg": "required"}
        for f in ("name", "auth_mode", "region")
        if not fields.get(f)
    ]


async def _validate_row(rt: Any, row: dict[str, Any]) -> dict[str, Any]:
    """Syntax (no DNS), then the lenient DNS check off the event loop: a blocked address is
    refused, a name that does not resolve now is only the `endpoint_unresolved` warning."""
    v = conns.view(row)
    _validation(conns.field_errors(v, test_mode=_test_mode(rt)))
    errs, _ = await conns.host_checks(v, test_mode=_test_mode(rt))
    _validation(errs)
    return v


async def _in_use_by_on_consumer(rt: Any, session: Any, row: dict[str, Any]) -> list[str]:
    """Configure-first consumers that are ON and use the connection (4-0a: the assistant)."""
    if not _assistant_on(rt):
        return []
    return [u["kind"] for u in await conns.used_by(session, row, assistant_on=True)]


# ---------------------------------------------------------------- CRUD
@router.get("/api/aws/connections")
async def list_connections(_: Admin, __: SessionUser, rt: RT, session: DB) -> list[dict]:
    return [await connection_out(rt, session, r) for r in await conns.list_rows(session)]


@router.post("/api/aws/connections", status_code=201)
async def create_connection(
    body: AwsConnectionFields, _: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit
) -> dict:
    fields = body.changes()
    _validation(_missing_create(fields))
    row = conns.new_row(fields, rt.secret_key)
    await _validate_row(rt, {**row, "id": None})
    if await conns.name_taken(session, row["name"]):
        raise ApiError(409, "conflict", "A connection with this name exists", [{"field": "name"}])
    new = await conns.insert(session, row)
    await audit.record(
        session, ctx, "aws_connection.create", _entity(new), _audit_changes(None, new)
    )
    await conns.sync_legacy(session)
    await session.commit()
    await _after_write(rt, new["id"])
    return await connection_out(rt, session, new)


@router.get("/api/aws/connections/{conn_id}")
async def get_connection(conn_id: int, _: Admin, __: SessionUser, rt: RT, session: DB) -> dict:
    return await connection_out(rt, session, await _row_or_404(session, conn_id))


@router.patch("/api/aws/connections/{conn_id}")
async def patch_connection(
    conn_id: int,
    body: AwsConnectionPatch,
    user: Admin,
    __: SessionUser,
    rt: RT,
    session: DB,
    ctx: Audit,
) -> dict:
    row = await _row_or_404(session, conn_id, lock=True)
    fields = body.changes()
    fields.pop("test_id", None)
    merged = conns.merge(row, fields, rt.secret_key)
    v_new = await _validate_row(rt, merged)
    if merged["name"] != row["name"] and await conns.name_taken(session, merged["name"], conn_id):
        raise ApiError(409, "conflict", "A connection with this name exists", [{"field": "name"}])
    sk = rt.secret_key
    new_hash = conns.connection_hash(v_new, sk)
    credential_change = conns.connection_hash(conns.view(row), sk) != new_hash
    needs_test = False
    if credential_change:
        users = await _in_use_by_on_consumer(rt, session, row)
        if users:
            needs_test = True
            if not rt.aws.tests.valid("aws_connection", body.test_id, user.id, new_hash):
                raise ApiError(
                    422,
                    "test_required",
                    "Run a successful Test first",
                    [{"connection_id": conn_id, "used_by": users}],
                )
    await conns.update(session, merged)
    changes = _audit_changes(row, merged)
    if changes:
        await audit.record(session, ctx, "aws_connection.update", _entity(merged), changes)
    await conns.sync_legacy(session)
    await session.commit()
    if needs_test and body.test_id:
        rt.aws.tests.consume(body.test_id)
    await _after_write(rt, conn_id)
    return await connection_out(rt, session, await _row_or_404(session, conn_id))


@router.delete("/api/aws/connections/{conn_id}", status_code=204)
async def delete_connection(
    conn_id: int, _: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit
) -> Response:
    row = await _row_or_404(session, conn_id, lock=True)
    used = await conns.used_by(session, row, assistant_on=_assistant_on(rt))
    if used:
        raise ApiError(
            409, "conflict", "The connection is in use", [{"used_by": [u["kind"] for u in used]}]
        )
    if row["is_default"] and len(await conns.list_rows(session)) > 1:
        raise ApiError(
            409, "conflict", "Make another connection the default first", [{"reason": "default"}]
        )
    await conns.delete(session, conn_id)
    await audit.record(session, ctx, "aws_connection.delete", _entity(row))
    await session.commit()
    await _after_write(rt, conn_id)
    return Response(status_code=204)


@router.post("/api/aws/connections/{conn_id}/default")
async def make_default(
    conn_id: int,
    user: Admin,
    __: SessionUser,
    rt: RT,
    session: DB,
    ctx: Audit,
    body: DefaultIn | None = None,
) -> list[dict]:
    row = await _row_or_404(session, conn_id, lock=True)
    before = await conns.default_row(session)
    test_id = body.test_id if body is not None else None
    needs_test = False
    if not row["is_default"]:
        # coordinator decision (PR 4-0a): an ON consumer that uses the default implicitly
        # (`connection_id: null`) would switch to these credentials, so the same rule as
        # "configure first" applies: a successful Test of this connection in this session
        users = await conns.implicit_default_users(session, assistant_on=_assistant_on(rt))
        if users:
            needs_test = True
            h = conns.connection_hash(conns.view(row), rt.secret_key)
            if not rt.aws.tests.valid("aws_connection", test_id, user.id, h):
                raise ApiError(
                    422,
                    "test_required",
                    "Run a successful Test of this connection first",
                    [{"connection_id": conn_id, "used_by": users}],
                )
        await conns.set_default(session, conn_id)
        await audit.record(
            session,
            ctx,
            "aws_connection.set_default",
            _entity(row),
            {"is_default": [False, True], "previous_default": [before and before["name"], None]},
        )
        await conns.sync_legacy(session)
        await session.commit()
        if needs_test and test_id:
            rt.aws.tests.consume(test_id)
        await _after_write(rt, None)
    return [await connection_out(rt, session, r) for r in await conns.list_rows(session)]


# ---------------------------------------------------------------- test
async def _consumer_accesses(session: Any, rt: Any, row: dict[str, Any]) -> list[dict[str, Any]]:
    """One hop per distinct access of the connection's current consumers."""
    out: list[dict[str, Any]] = []
    if await conns.used_by(session, row, assistant_on=_assistant_on(rt)):
        asst = await conns.load_assistant_row(session) or {}
        a = conns.normalize_access(asst.get("access"))
        if a["mode"] != "direct" and a not in out:
            out.append(a)
    return out


@router.post("/api/aws/connections/test")
async def test_connection(
    body: AwsTestIn, user: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit
) -> dict:
    tests = rt.aws.tests
    if not tests.allow(user.id):
        raise ApiError(429, "rate_limited", "Too many connection tests; try again in a few minutes")
    fields = body.connection.changes() if body.connection is not None else {}
    row: dict[str, Any] | None = None
    if body.connection_id is not None:
        row = await _row_or_404(session, body.connection_id)
        test_row = conns.merge(row, fields, rt.secret_key)
    elif body.connection is not None:
        test_row = conns.new_row(fields, rt.secret_key)
        if not fields.get("name"):
            test_row["name"] = None
    else:
        raise ApiError(
            422, "validation_error", "connection_id or connection is required",
            [{"loc": ["body", "connection"], "msg": "required"}],
        )  # fmt: skip
    v = conns.view(test_row)
    if body.access is not None:
        accesses = [body.access.model_dump()]
    elif row is not None:
        accesses = await _consumer_accesses(session, rt, row)
    else:
        accesses = []
    await session.close()
    run = await awstest.run_chain_test(
        v, accesses, secret_key=rt.secret_key, test_mode=_test_mode(rt)
    )
    result = awstest.result(run)
    if result["ok"]:
        tid, exp = tests.issue("aws_connection", user.id, conns.connection_hash(v, rt.secret_key))
        result["test_id"], result["expires_at"] = tid, iso(exp)
    ident = run.identity or {}
    async with rt.sessionmaker() as s2:
        if row is not None:
            await conns.set_last_test(
                s2,
                row["id"],
                {
                    "ok": bool(result["ok"]),
                    "at": iso(now_utc()),
                    "by": user.username,
                    "error_code": (result.get("error") or {}).get("code"),
                    "account": ident.get("account"),
                    "arn": ident.get("arn"),
                },
            )
        await audit.record(
            s2,
            ctx,
            "aws_connection.test",
            audit.entity("aws_connection", row["id"] if row else None,
                         row["name"] if row else (fields.get("name") or None)),
            {
                "ok": [None, bool(result["ok"])],
                "error_code": [None, (result.get("error") or {}).get("code")],
                "access_mode": [None, [a.get("mode") for a in accesses]],
                "steps": [None, [{"step": st["step"], "ok": st["ok"]} for st in result["steps"]]],
            },
        )  # fmt: skip
        await s2.commit()
    return result


# ---------------------------------------------------------------- policies
@router.get("/api/aws/connections/{conn_id}/policies")
async def get_policies(
    conn_id: int,
    _: Admin,
    __: SessionUser,
    rt: RT,
    session: DB,
    role_arn: str | None = Query(default=None, max_length=600),
    external_id: str | None = Query(default=None, max_length=1224),
) -> dict:
    row = await _row_or_404(session, conn_id)
    v = conns.view(row)
    role_arn = (role_arn or "").strip() or None
    external_id = (external_id or "").strip() or None
    if role_arn is not None or external_id is not None:
        access = {"mode": "assume_role", "role_arn": role_arn, "external_id": external_id}
        errs = conns.access_errors(access, v) if role_arn else []
        errs = [{**e, "loc": ["query", e["loc"][-1]]} for e in errs]
        _validation(errs, "Invalid role ARN or ExternalId")
    return policies.render(v, conns.cert_values(v, rt.secret_key), role_arn=role_arn,
                           external_id=external_id)  # fmt: skip


# ---------------------------------------------------------------- certificate
def _host_files_conflict(v: dict[str, Any]) -> None:
    if conns.uses_host_files(v):
        raise ApiError(
            409, "conflict", "The certificate comes from host files (NETMON_AWS_RA_CERT_FILE)"
        )


@router.post("/api/aws/connections/{conn_id}/cert")
async def upload_cert(
    conn_id: int, request: Request, _: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit
) -> dict:
    row = await _row_or_404(session, conn_id, lock=True)
    v = conns.view(row)
    _host_files_conflict(v)
    form = await _read_form(request, certs.MAX_UPLOAD)
    if not form.get("file"):
        raise ApiError(
            422, "validation_error", "Field 'file' is required", [{"loc": ["body", "file"]}]
        )
    sk = rt.secret_key
    try:
        pending = conns.decrypt_field(v, "ra_csr_key_enc", sk)
    except conns.Unreadable:
        pending = None
    password = form.get("password", b"").decode("utf-8", "replace") or None
    try:
        mat = certs.parse_upload(
            form["file"], form.get("key") or None, password, pending_key_pem=pending
        )
    except certs.CertError as exc:
        details: list[dict[str, Any]] | None = None
        if exc.code == "invalid_certificate":
            details = [{"reason": exc.reason}]
        elif exc.code == "key_mismatch" and pending and not form.get("key"):
            details = [
                {
                    "csr_subject": v.get("ra_csr_subject"),
                    "csr_created_at": v.get("ra_csr_created_at"),
                    "cert_subject": certs.peek_subject(form["file"]),
                }
            ]
        raise ApiError(422, exc.code, exc.reason, details) from None
    before = conns.safe_material(v, sk)
    settings = row["settings"]
    settings["ra_cert_pem"] = mat.cert_pem
    settings["ra_chain_pem"] = mat.chain_pem
    settings["ra_key_enc"] = conns.encrypt_field(
        row["aad_scope"], "ra_key_enc", mat.key_pem or "", sk
    )
    settings["ra_cert_uploaded_at"] = iso(now_utc())
    used_csr_key = pending is not None and mat.key_pem == certs.key_to_pkcs8_pem(
        certs.key_from_pkcs8_pem(pending)
    )
    if used_csr_key:
        for k in conns.CSR_KEYS:
            settings[k] = None
    await conns.update(session, row)
    info = certs.cert_info(
        mat.cert, mat.chain, source="uploaded", uploaded_at=settings["ra_cert_uploaded_at"],
        key_unreadable=False,
    )  # fmt: skip
    old = certs.cert_info(before.cert, [], source="uploaded", uploaded_at=None,
                          key_unreadable=False) if before else None  # fmt: skip
    await audit.record(
        session,
        ctx,
        "aws_connection.cert_upload",
        _entity(row),
        {
            "subject": [old and old["subject"], info["subject"]],
            "serial_hex": [old and old["serial_hex"], info["serial_hex"]],
            "not_after": [old and old["not_after"], info["not_after"]],
            "sha256": [old and old["sha256"], info["sha256"]],
            "key_from_csr": [None, used_csr_key],
        },
    )
    await conns.sync_legacy(session)
    await session.commit()
    await _after_write(rt, conn_id)
    return info


@router.delete("/api/aws/connections/{conn_id}/cert", status_code=204)
async def delete_cert(
    conn_id: int, _: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit
) -> Response:
    row = await _row_or_404(session, conn_id, lock=True)
    if row["auth_mode"] == "roles_anywhere" and await _in_use_by_on_consumer(rt, session, row):
        raise ApiError(
            409, "conflict", "Turn the consumer off (or use another connection) before deleting"
        )
    had = row["settings"].get("ra_cert_pem")
    for k in conns.CERT_KEYS:
        row["settings"][k] = None
    await conns.update(session, row)
    if had:
        await audit.record(session, ctx, "aws_connection.cert_delete", _entity(row))
    await conns.sync_legacy(session)
    await session.commit()
    await _after_write(rt, conn_id)
    return Response(status_code=204)


def _csr_out(v: dict[str, Any], sk: str) -> dict[str, Any]:
    return {
        "subject": v.get("ra_csr_subject"),
        "created_at": v.get("ra_csr_created_at"),
        "pem": v.get("ra_csr_pem"),
        "key_unreadable": conns.unreadable(v, "ra_csr_key_enc", sk),
    }


@router.post("/api/aws/connections/{conn_id}/cert/csr")
async def make_csr(
    conn_id: int, body: CsrIn, _: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit
) -> dict:
    row = await _row_or_404(session, conn_id, lock=True)
    _host_files_conflict(conns.view(row))
    err = certs.validate_cn(body.common_name)
    if err:
        raise ApiError(
            422, "validation_error", f"common_name: {err}", [{"loc": ["body", "common_name"]}]
        )
    size = body.key_size or (2048 if body.key_type == "rsa" else 256)
    try:
        key_pem, csr_pem = await asyncio.to_thread(
            certs.generate_csr, body.common_name, body.key_type, size, body.organization,
            body.organizational_unit,
        )  # fmt: skip
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc), [{"loc": ["body", "key_size"]}]) from None
    s = row["settings"]
    s["ra_csr_pem"] = csr_pem
    s["ra_csr_key_enc"] = conns.encrypt_field(
        row["aad_scope"], "ra_csr_key_enc", key_pem, rt.secret_key
    )
    s["ra_csr_subject"] = f"CN={body.common_name.strip()}"
    s["ra_csr_created_at"] = iso(now_utc())
    await conns.update(session, row)
    await audit.record(
        session,
        ctx,
        "aws_connection.csr_create",
        _entity(row),
        {"subject": [None, s["ra_csr_subject"]], "key_type": [None, f"{body.key_type}-{size}"]},
    )
    await conns.sync_legacy(session)
    await session.commit()
    await _after_write(rt, conn_id)
    out = _csr_out(conns.view(row), rt.secret_key)
    out["warnings"] = certs.cn_warnings(body.common_name.strip())
    return out


def _slug(name: str) -> str:
    out = "".join(ch.lower() if ch.isascii() and ch.isalnum() else "-" for ch in name)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "connection"


@router.get("/api/aws/connections/{conn_id}/cert/csr")
async def download_csr(conn_id: int, _: Admin, __: SessionUser, rt: RT, session: DB) -> Response:
    row = await _row_or_404(session, conn_id)
    pem = row["settings"].get("ra_csr_pem")
    if not pem:
        raise ApiError(404, "not_found", "No CSR has been generated")
    return Response(
        pem,
        media_type="application/pkcs10",
        headers={
            "Content-Disposition": f'attachment; filename="netmonitor-{_slug(row["name"])}.csr"'
        },
    )


@router.delete("/api/aws/connections/{conn_id}/cert/csr", status_code=204)
async def delete_csr(
    conn_id: int, _: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit
) -> Response:
    row = await _row_or_404(session, conn_id, lock=True)
    if row["settings"].get("ra_csr_pem"):
        for k in conns.CSR_KEYS:
            row["settings"][k] = None
        await conns.update(session, row)
        await audit.record(session, ctx, "aws_connection.csr_delete", _entity(row))
        await conns.sync_legacy(session)
        await session.commit()
        await _after_write(rt, conn_id)
    return Response(status_code=204)


@router.post("/api/aws/connections/{conn_id}/cert/copy")
async def copy_cert(
    conn_id: int, body: CertCopyIn, _: Admin, __: SessionUser, rt: RT, session: DB, ctx: Audit
) -> dict:
    row = await _row_or_404(session, conn_id, lock=True)
    _host_files_conflict(conns.view(row))
    if body.from_connection_id == conn_id:
        raise ApiError(
            422, "validation_error", "Choose another connection",
            [{"loc": ["body", "from_connection_id"]}],
        )  # fmt: skip
    src = await _row_or_404(session, body.from_connection_id)
    sv = conns.view(src)
    sk = rt.secret_key
    try:
        key_pem = conns.decrypt_field(sv, "ra_key_enc", sk)
    except conns.Unreadable:
        key_pem = None
    if not sv.get("ra_cert_pem") or not key_pem:
        raise ApiError(
            422, "validation_error",
            "The source connection has no certificate with a readable private key",
            [{"loc": ["body", "from_connection_id"]}],
        )  # fmt: skip
    s = row["settings"]
    s["ra_cert_pem"] = sv["ra_cert_pem"]
    s["ra_chain_pem"] = sv.get("ra_chain_pem")
    s["ra_key_enc"] = conns.encrypt_field(row["aad_scope"], "ra_key_enc", key_pem, sk)
    s["ra_cert_uploaded_at"] = iso(now_utc())
    await conns.update(session, row)
    found = certs.load_pem_certs(sv["ra_cert_pem"].encode())
    await audit.record(
        session,
        ctx,
        "aws_connection.cert_copy",
        _entity(row),
        {
            "from_connection": [None, src["name"]],
            "serial_hex": [None, format(found[0].serial_number, "x") if found else None],
        },
    )
    await conns.sync_legacy(session)
    await session.commit()
    await _after_write(rt, conn_id)
    return await connection_out(rt, session, await _row_or_404(session, conn_id))


# ---------------------------------------------------------------- ExternalId
@router.post("/api/aws/external-id")
async def external_id(_: Admin, __: SessionUser) -> dict:
    return {"external_id": "".join(secrets.choice(EXTERNAL_ID_ALPHABET) for _ in range(32))}


__all__ = ["AwsAccessIn", "connection_out", "router"]
