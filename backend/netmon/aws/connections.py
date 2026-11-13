"""[4] Shared AWS connections: storage, validation, secrets, the connection view, the legacy
mirror of the 1.4.x assistant keys (architecture § 15.1.3 / § 15.1.4, data-model § [4] "AWS
connections (migration P4-1)").

A connection is a named base identity (`roles_anywhere` | `access_key` | `default_chain`) with
a region, a proxy mode and endpoint overrides. Secrets in `settings` are AES-GCM encrypted with
AAD `f"{aad_scope}.{field}"`: the row created by migration P4-1 has `aad_scope = 'assistant'`,
so its AADs are byte-identical to 1.4.x and its ciphertexts were copied verbatim.

Consumers choose a connection (`connection_id`, null = the default) and an access
`{mode: direct | assume_role | ra_role, role_arn, external_id}` (one hop at most)."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets as pysecrets
from typing import Any

from sqlalchemy import text

from netmon.aws import certs, endpoints
from netmon.aws.credentials import (
    DefaultChainProvider,
    RaMaterial,
    RolesAnywhereProvider,
    StaticProvider,
)
from netmon.aws.errors import AssistantError, make
from netmon.services import crypto

AUTH_MODES = ("roles_anywhere", "access_key", "default_chain")
ACCESS_MODES = ("direct", "assume_role", "ra_role")
PROXY_MODES = ("env", "direct", "custom")
LEGACY_SCOPE = "assistant"  # aad_scope of the row created by P4-1 (1.4.x AADs)

ENV_CERT_FILE = "NETMON_AWS_RA_CERT_FILE"
ENV_KEY_FILE = "NETMON_AWS_RA_KEY_FILE"
ENV_CHAIN_FILE = "NETMON_AWS_RA_CHAIN_FILE"

# 3b patterns, narrowed to the `aws` partition (cut C1)
ACCESS_KEY_RE = re.compile(r"^(AKIA|ASIA)[A-Z0-9]{16}$")
SESSION_TOKEN_MAX = 4096
TRUST_ANCHOR_RE = re.compile(
    r"^arn:aws:rolesanywhere:[a-z0-9-]+:\d{12}:trust-anchor/[0-9a-zA-Z-]+$"
)
PROFILE_RE = re.compile(r"^arn:aws:rolesanywhere:[a-z0-9-]+:\d{12}:profile/[0-9a-zA-Z-]+$")
ROLE_RE = re.compile(r"^arn:aws:iam::\d{12}:role/[\w+=,.@/-]{1,512}$")
EXTERNAL_ID_RE = re.compile(r"^[\w+=,.@:/-]{2,1224}$")
DOUBLED_ARN = "the ARN appears twice — paste it once"
NAME_MAX = 64

# settings keys (the 3b `assistant` names) and their defaults
SETTINGS_DEFAULTS: dict[str, Any] = {
    "access_key_id": None,
    "secret_access_key_enc": None,
    "session_token_enc": None,
    "access_key_created_at": None,
    "ra_region": None,
    "ra_trust_anchor_arn": None,
    "ra_profile_arn": None,
    "ra_role_arn": None,
    "ra_session_seconds": 3600,
    "ra_cert_pem": None,
    "ra_chain_pem": None,
    "ra_key_enc": None,
    "ra_cert_uploaded_at": None,
    "ra_csr_pem": None,
    "ra_csr_key_enc": None,
    "ra_csr_subject": None,
    "ra_csr_created_at": None,
}
# stored ciphertext field -> AAD field name
SECRET_FIELDS: dict[str, str] = {
    "secret_access_key_enc": "secret_access_key",
    "session_token_enc": "session_token",
    "ra_key_enc": "ra_key",
    "ra_csr_key_enc": "ra_csr_key",
}
# write-only API inputs -> stored field
SECRET_INPUTS = {"secret_access_key": "secret_access_key_enc", "session_token": "session_token_enc"}
COLUMN_FIELDS = ("name", "auth_mode", "region", "proxy_mode", "proxy_url", "endpoint_overrides")
SETTINGS_INPUTS = (
    "access_key_id",
    "access_key_created_at",
    "ra_region",
    "ra_trust_anchor_arn",
    "ra_profile_arn",
    "ra_role_arn",
    "ra_session_seconds",
)
WRITABLE_FIELDS = (*COLUMN_FIELDS, *SETTINGS_INPUTS, *SECRET_INPUTS)
# fields whose change is a credential change (everything but the name)
CREDENTIAL_FIELDS = tuple(f for f in WRITABLE_FIELDS if f != "name")
CERT_KEYS = ("ra_cert_pem", "ra_chain_pem", "ra_key_enc", "ra_cert_uploaded_at")
CSR_KEYS = ("ra_csr_pem", "ra_csr_key_enc", "ra_csr_subject", "ra_csr_created_at")
# 1.4.x authentication keys of the `assistant` settings row (mirrored, § 15.1.4)
LEGACY_KEYS = (
    "auth_mode",
    "access_key_id",
    "secret_access_key_enc",
    "session_token_enc",
    "access_key_created_at",
    "ra_region",
    "ra_endpoint_url",
    "ra_trust_anchor_arn",
    "ra_profile_arn",
    "ra_role_arn",
    "ra_session_seconds",
    "ra_cert_pem",
    "ra_chain_pem",
    "ra_key_enc",
    "ra_cert_uploaded_at",
    "ra_csr_pem",
    "ra_csr_key_enc",
    "ra_csr_subject",
    "ra_csr_created_at",
    "proxy_mode",
    "proxy_url",
)
DEFAULT_ACCESS: dict[str, Any] = {"mode": "direct", "role_arn": None, "external_id": None}


class Unreadable(ValueError):
    """A stored secret cannot be decrypted (secret.key replaced)."""


# ---------------------------------------------------------------- secrets
def aad(scope: str, field: str) -> bytes:
    return f"{scope}.{field}".encode()


def new_scope() -> str:
    return "aws.conn." + pysecrets.token_hex(8)


def decrypt_field(view: dict[str, Any], stored_field: str, secret_key: str) -> str | None:
    value = view.get(stored_field)
    if not value:
        return None
    try:
        return crypto.decrypt(
            secret_key, value, aad(view["_aad_scope"], SECRET_FIELDS[stored_field])
        )
    except crypto.DecryptError as exc:
        raise Unreadable(str(exc)) from None


def encrypt_field(scope: str, stored_field: str, plaintext: str, secret_key: str) -> str:
    return crypto.encrypt(secret_key, plaintext, aad(scope, SECRET_FIELDS[stored_field]))


def unreadable(view: dict[str, Any], stored_field: str, secret_key: str) -> bool:
    try:
        decrypt_field(view, stored_field, secret_key)
    except Unreadable:
        return True
    return False


def is_temporary_key(access_key_id: str | None) -> bool:
    return bool(access_key_id) and str(access_key_id).startswith("ASIA")


# ---------------------------------------------------------------- rows and the view
def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _row(m: Any) -> dict[str, Any]:
    d = dict(m)
    for k in ("endpoint_overrides", "settings", "last_test"):
        d[k] = _json(d.get(k))
    d["settings"] = {**SETTINGS_DEFAULTS, **(d.get("settings") or {})}
    d["endpoint_overrides"] = list(d.get("endpoint_overrides") or [])
    return d


_SELECT = (
    "SELECT id, name, is_default, auth_mode, region, proxy_mode, proxy_url, endpoint_overrides, "
    "settings, aad_scope, last_test, created_at, updated_at FROM aws_connections"
)


async def list_rows(session: Any) -> list[dict[str, Any]]:
    res = await session.execute(text(_SELECT + " ORDER BY is_default DESC, name, id"))
    return [_row(m) for m in res.mappings()]


async def get_row(session: Any, conn_id: int, *, lock: bool = False) -> dict[str, Any] | None:
    sql = _SELECT + " WHERE id = :id" + (" FOR UPDATE" if lock else "")
    m = (await session.execute(text(sql), {"id": conn_id})).mappings().first()
    return _row(m) if m is not None else None


async def default_row(session: Any) -> dict[str, Any] | None:
    m = (await session.execute(text(_SELECT + " WHERE is_default"))).mappings().first()
    return _row(m) if m is not None else None


async def resolve(session: Any, connection_id: int | None) -> dict[str, Any] | None:
    """The row a consumer uses (`connection_id` null = the default); None = no connection."""
    if connection_id is None:
        return await default_row(session)
    return await get_row(session, int(connection_id))


def _params(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row["name"],
        "auth_mode": row["auth_mode"],
        "region": row["region"],
        "proxy_mode": row.get("proxy_mode") or "env",
        "proxy_url": row.get("proxy_url"),
        "endpoint_overrides": json.dumps(row.get("endpoint_overrides") or []),
        "settings": json.dumps(row.get("settings") or {}),
        "last_test": json.dumps(row["last_test"]) if row.get("last_test") is not None else None,
    }


async def insert(session: Any, row: dict[str, Any]) -> dict[str, Any]:
    """Insert; the first connection becomes the default."""
    has_default = await default_row(session) is not None
    p = _params(row)
    p["aad_scope"] = row.get("aad_scope") or new_scope()
    p["is_default"] = not has_default
    res = await session.execute(
        text(
            "INSERT INTO aws_connections (name, is_default, auth_mode, region, proxy_mode, "
            "proxy_url, endpoint_overrides, settings, aad_scope, last_test) VALUES (:name, "
            ":is_default, :auth_mode, :region, :proxy_mode, :proxy_url, "
            "CAST(:endpoint_overrides AS jsonb), CAST(:settings AS jsonb), :aad_scope, "
            "CAST(:last_test AS jsonb)) RETURNING id"
        ),
        p,
    )
    new_id = int(res.scalar_one())
    out = await get_row(session, new_id)
    assert out is not None
    return out


async def update(session: Any, row: dict[str, Any]) -> None:
    p = _params(row)
    p["id"] = row["id"]
    await session.execute(
        text(
            "UPDATE aws_connections SET name = :name, auth_mode = :auth_mode, region = :region, "
            "proxy_mode = :proxy_mode, proxy_url = :proxy_url, endpoint_overrides = "
            "CAST(:endpoint_overrides AS jsonb), settings = CAST(:settings AS jsonb), last_test = "
            "CAST(:last_test AS jsonb), updated_at = now() WHERE id = :id"
        ),
        p,
    )


async def set_last_test(session: Any, conn_id: int, last_test: dict[str, Any]) -> None:
    await session.execute(
        text("UPDATE aws_connections SET last_test = CAST(:t AS jsonb) WHERE id = :id"),
        {"t": json.dumps(last_test), "id": conn_id},
    )


async def delete(session: Any, conn_id: int) -> None:
    await session.execute(text("DELETE FROM aws_connections WHERE id = :id"), {"id": conn_id})


async def set_default(session: Any, conn_id: int) -> None:
    await session.execute(text("UPDATE aws_connections SET is_default = false WHERE is_default"))
    await session.execute(
        text("UPDATE aws_connections SET is_default = true WHERE id = :id"), {"id": conn_id}
    )


async def name_taken(session: Any, name: str, exclude_id: int | None = None) -> bool:
    res = await session.execute(
        text("SELECT 1 FROM aws_connections WHERE lower(name) = lower(:n) AND id <> :x"),
        {"n": name, "x": exclude_id or 0},
    )
    return res.first() is not None


def view(row: dict[str, Any]) -> dict[str, Any]:
    """The flat dictionary the 3b code consumes (architecture § 15.1.3 "Connection view")."""
    settings = {**SETTINGS_DEFAULTS, **(row.get("settings") or {})}
    out: dict[str, Any] = {
        "auth_mode": row.get("auth_mode"),
        "region": row.get("region"),
        "proxy_mode": row.get("proxy_mode") or "env",
        "proxy_url": row.get("proxy_url"),
        "endpoint_overrides": list(row.get("endpoint_overrides") or []),
        **settings,
        "_id": row.get("id"),
        "_name": row.get("name"),
        "_is_default": bool(row.get("is_default")),
        "_aad_scope": row.get("aad_scope") or LEGACY_SCOPE,
        "_last_test": row.get("last_test"),
    }
    region = out["region"] or ""
    ra_reg = out.get("ra_region") or region
    overrides = out["endpoint_overrides"]
    out["ra_endpoint_url"] = endpoints.override_for("rolesanywhere", ra_reg, overrides)
    out["sts_endpoint_url"] = endpoints.override_for("sts", region, overrides)
    return out


def new_row(fields: dict[str, Any], secret_key: str) -> dict[str, Any]:
    """A row for `insert` from create fields (secrets encrypted with a fresh AAD scope)."""
    row = {
        "id": None,
        "name": "",
        "is_default": False,
        "auth_mode": "roles_anywhere",
        "region": "",
        "proxy_mode": "env",
        "proxy_url": None,
        "endpoint_overrides": [],
        "settings": dict(SETTINGS_DEFAULTS),
        "aad_scope": new_scope(),
        "last_test": None,
    }
    return merge(row, fields, secret_key)


def merge(row: dict[str, Any], fields: dict[str, Any], secret_key: str) -> dict[str, Any]:
    """Validated input fields over a row (secret rule: None keep, "" clear, string replace)."""
    out = copy.deepcopy(row)
    settings = out.setdefault("settings", dict(SETTINGS_DEFAULTS))
    scope = out.get("aad_scope") or LEGACY_SCOPE
    for key, value in fields.items():
        if key in SECRET_INPUTS:
            if value is None:
                continue
            target = SECRET_INPUTS[key]
            settings[target] = (
                None if value == "" else encrypt_field(scope, target, value, secret_key)
            )
        elif key in COLUMN_FIELDS:
            if key == "name" and isinstance(value, str):
                value = value.strip()
            out[key] = (
                copy.deepcopy(value) if value is not None or key == "proxy_url" else out.get(key)
            )
            if key == "endpoint_overrides" and value is None:
                out[key] = []
        elif key in SETTINGS_INPUTS:
            settings[key] = copy.deepcopy(value)
    return out


# ---------------------------------------------------------------- validation (pure)
def arn_doubled(value: Any) -> bool:
    return isinstance(value, str) and value.count("arn:") > 1


def field_errors(v: dict[str, Any], *, test_mode: bool) -> list[dict[str, Any]]:
    """Syntax errors of a connection view (shape `{"loc", "msg"}`)."""
    errs: list[dict[str, Any]] = []

    def bad(field: str, msg: str) -> None:
        errs.append({"loc": ["body", field], "msg": msg})

    name = v.get("_name")
    if name is not None:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= NAME_MAX:
            bad("name", f"1-{NAME_MAX} characters")
        elif not name.isprintable():
            bad("name", "printable characters only")
    if v.get("auth_mode") not in AUTH_MODES:
        bad("auth_mode", "invalid auth mode")
    if not v.get("region"):
        bad("region", "required")
    elif not endpoints.REGION_RE.match(str(v["region"])):
        bad("region", "invalid AWS region")
    if v.get("ra_region") and not endpoints.REGION_RE.match(str(v["ra_region"])):
        bad("ra_region", "invalid AWS region")
    if v.get("proxy_mode") not in PROXY_MODES:
        bad("proxy_mode", "invalid proxy mode")
    if v.get("proxy_mode") == "custom":
        e = endpoints.proxy_error(v.get("proxy_url"), test_mode=test_mode)
        if e:
            bad("proxy_url", e)
    for e in endpoints.override_errors(v.get("endpoint_overrides"), test_mode=test_mode):
        bad("endpoint_overrides", e)
    if v.get("access_key_id") and not ACCESS_KEY_RE.match(str(v["access_key_id"])):
        bad("access_key_id", "must be AKIA/ASIA + 16 characters")
    for f, rx in (
        ("ra_trust_anchor_arn", TRUST_ANCHOR_RE),
        ("ra_profile_arn", PROFILE_RE),
        ("ra_role_arn", ROLE_RE),
    ):
        if not v.get(f):
            continue
        if arn_doubled(v[f]):
            bad(f, DOUBLED_ARN)
        elif not rx.match(str(v[f])):
            bad(f, "invalid ARN")
    secs = v.get("ra_session_seconds")
    if not isinstance(secs, int) or not 900 <= secs <= 43200:
        bad("ra_session_seconds", "must be 900-43200")
    return errs


def required_errors(
    v: dict[str, Any], secret_key: str, *, has_cert: bool, has_key: bool
) -> list[dict[str, Any]]:
    """Missing values for testing / using the connection in its auth mode (3b rules without
    the Bedrock `model_id`)."""
    errs: list[dict[str, Any]] = []

    def missing(field: str, msg: str = "required") -> None:
        errs.append({"loc": ["body", field], "msg": msg})

    if not v.get("region"):
        missing("region")
    mode = v.get("auth_mode")
    if mode == "access_key":
        if not v.get("access_key_id"):
            missing("access_key_id")
        try:
            if not decrypt_field(v, "secret_access_key_enc", secret_key):
                missing("secret_access_key")
        except Unreadable:
            missing("secret_access_key", "stored secret cannot be decrypted; enter it again")
        try:
            decrypt_field(v, "session_token_enc", secret_key)
        except Unreadable:
            missing("session_token", "stored session token cannot be decrypted; enter it again")
    elif mode == "roles_anywhere":
        for f in ("ra_trust_anchor_arn", "ra_profile_arn", "ra_role_arn"):
            if not v.get(f):
                missing(f)
        if not has_cert:
            missing("certificate", "upload the Roles Anywhere certificate")
        elif not has_key:
            missing("certificate", "the private key is missing or cannot be decrypted")
    return errs


def normalize_access(access: Any) -> dict[str, Any]:
    """`{"mode", "role_arn", "external_id"}` with empty strings as null; `direct` drops both."""
    a = dict(access) if isinstance(access, dict) else {}
    mode = a.get("mode") or "direct"
    role = (a.get("role_arn") or "").strip() or None
    ext = (a.get("external_id") or "").strip() or None
    if mode == "direct":
        role, ext = None, None
    if mode == "ra_role":
        ext = ext  # validated below (must be null)
    return {"mode": mode, "role_arn": role, "external_id": ext}


def access_errors(access: dict[str, Any], v: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Consumer access rules (data-model "Consumer access"); `v` = the connection view used."""
    errs: list[dict[str, Any]] = []

    def bad(field: str, msg: str) -> None:
        errs.append({"loc": ["body", "access", field], "msg": msg})

    mode = access.get("mode")
    if mode not in ACCESS_MODES:
        bad("mode", "must be direct, assume_role or ra_role")
        return errs
    role, ext = access.get("role_arn"), access.get("external_id")
    if mode in ("assume_role", "ra_role"):
        if not role:
            bad("role_arn", "required")
        elif arn_doubled(role):
            bad("role_arn", DOUBLED_ARN)
        elif not ROLE_RE.match(role):
            bad("role_arn", "invalid role ARN (arn:aws:iam::<account>:role/<name>)")
    if mode == "assume_role" and ext is not None and not EXTERNAL_ID_RE.match(ext):
        bad("external_id", "2-1224 characters of letters, digits and _+=,.@:/-")
    if mode == "ra_role":
        if ext is not None:
            bad("external_id", "must be empty for another role of the same Roles Anywhere profile")
        if v is not None and v.get("auth_mode") != "roles_anywhere":
            bad("mode", "only with a Roles Anywhere connection")
        elif v is not None and role and ROLE_RE.match(role):
            prof = str(v.get("ra_profile_arn") or "")
            prof_acct = prof.split(":")[4] if len(prof.split(":")) > 4 else None
            if prof_acct and role.split(":")[4] != prof_acct:
                bad("role_arn", "must be in the profile's account")
    return errs


def arn_account_warning(v: dict[str, Any]) -> bool:
    accounts = set()
    for f in ("ra_trust_anchor_arn", "ra_profile_arn", "ra_role_arn"):
        parts = str(v.get(f) or "").split(":")
        if len(parts) > 4 and parts[4]:
            accounts.add(parts[4])
    return len(accounts) > 1


# ---------------------------------------------------------------- certificate material
def host_files() -> dict[str, str] | None:
    cert, key = os.environ.get(ENV_CERT_FILE), os.environ.get(ENV_KEY_FILE)
    if not cert or not key:
        return None
    out = {"cert": cert, "key": key}
    chain = os.environ.get(ENV_CHAIN_FILE)
    if chain:
        out["chain"] = chain
    return out


def uses_host_files(v: dict[str, Any]) -> bool:
    """Host certificate files apply to the default connection only (when it is
    `roles_anywhere`)."""
    return (
        bool(v.get("_is_default"))
        and v.get("auth_mode") == "roles_anywhere"
        and (host_files() is not None)
    )


def load_material(v: dict[str, Any], secret_key: str) -> certs.Material | None:
    """Certificate (+ chain, + key when readable) from host files or the upload. Raises
    `certs.CertError` / OSError for broken host files."""
    if uses_host_files(v):
        files = host_files()
        assert files is not None
        with open(files["cert"], "rb") as fh:
            cert_data = fh.read()
        with open(files["key"], "rb") as fh:
            key_data = fh.read()
        chain_data = b""
        if files.get("chain"):
            with open(files["chain"], "rb") as fh:
                chain_data = fh.read()
        found = certs.load_pem_certs(cert_data + b"\n" + chain_data)
        if not found:
            raise certs.CertError("invalid_certificate", "host certificate file unreadable")
        leaf, chain = certs._split_leaf(found)  # noqa: SLF001
        return certs.Material(leaf, chain, certs.load_key(key_data, None))
    if not v.get("ra_cert_pem"):
        return None
    found = certs.load_pem_certs((v["ra_cert_pem"] + "\n" + (v.get("ra_chain_pem") or "")).encode())
    if not found:
        return None
    leaf = found[0]
    chain = certs.order_chain(leaf, found[1:])
    key = None
    try:
        pem = decrypt_field(v, "ra_key_enc", secret_key)
        key = certs.key_from_pkcs8_pem(pem) if pem else None
    except (Unreadable, ValueError):
        key = None
    return certs.Material(leaf, chain, key)


def safe_material(v: dict[str, Any], secret_key: str) -> certs.Material | None:
    try:
        return load_material(v, secret_key)
    except (OSError, certs.CertError, ValueError):
        return None


def material_pems(v: dict[str, Any], secret_key: str) -> tuple[str | None, str | None, str | None]:
    mat = safe_material(v, secret_key)
    if mat is None:
        return None, None, None
    return mat.cert_pem, mat.chain_pem, mat.key_pem


def has_cert_and_key(v: dict[str, Any], secret_key: str) -> tuple[bool, bool]:
    cert_pem, _, key_pem = material_pems(v, secret_key)
    return cert_pem is not None, key_pem is not None


def ra_material(v: dict[str, Any], secret_key: str) -> RaMaterial:
    mat = load_material(v, secret_key)
    if mat is None or mat.key is None:
        raise make("rolesanywhere_failed", aws="No Roles Anywhere certificate and key configured")
    return RaMaterial(
        certs.der(mat.cert), mat.cert.serial_number, [certs.der(c) for c in mat.chain], mat.key
    )


def cert_values(v: dict[str, Any], secret_key: str) -> dict[str, str | None]:
    """CN, issuer CN and source identity of the connection's certificate (hints, policies)."""
    mat = safe_material(v, secret_key) if v.get("auth_mode") == "roles_anywhere" else None
    if mat is None:
        return {"cert_cn": None, "issuer_cn": None, "source_identity": None}
    return {
        "cert_cn": certs.subject_cn(mat.cert),
        "issuer_cn": certs.issuer_cn(mat.cert),
        "source_identity": certs.source_identity(mat.cert),
    }


# ---------------------------------------------------------------- connection hash (pure)
HASH_SETTINGS = (
    "access_key_id",
    "ra_region",
    "ra_trust_anchor_arn",
    "ra_profile_arn",
    "ra_role_arn",
    "ra_session_seconds",
)


def _sha(value: str | bytes | None) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def connection_hash(v: dict[str, Any], secret_key: str) -> str:
    """SHA-256 of the canonical connection fields (test binding and cache key); secrets and
    certificate material enter only as SHA-256."""
    try:
        secret = decrypt_field(v, "secret_access_key_enc", secret_key)
    except Unreadable:
        secret = None
    try:
        token = decrypt_field(v, "session_token_enc", secret_key)
    except Unreadable:
        token = None
    cert_pem, chain_pem, key_pem = material_pems(v, secret_key)
    overrides = sorted(
        (
            {"service": r.get("service"), "region": r.get("region"), "url": r.get("url")}
            for r in v.get("endpoint_overrides") or []
            if isinstance(r, dict)
        ),
        key=lambda r: (str(r["service"]), str(r["region"])),
    )
    data = {
        "auth_mode": v.get("auth_mode"),
        "region": v.get("region"),
        "proxy_mode": v.get("proxy_mode"),
        "proxy_url": v.get("proxy_url"),
        "endpoint_overrides": overrides,
        **{f: v.get(f) for f in HASH_SETTINGS},
        "secret_access_key": _sha(secret),
        "session_token": _sha(token),
        "ra_cert": _sha(cert_pem),
        "ra_chain": _sha(chain_pem),
        "ra_key": _sha(key_pem),
    }
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def configured(v: dict[str, Any] | None) -> bool:
    if v is None or not v.get("region"):
        return False
    mode = v.get("auth_mode")
    if mode == "access_key":
        return bool(v.get("access_key_id") and v.get("secret_access_key_enc"))
    if mode == "roles_anywhere":
        has_cert = bool(v.get("ra_cert_pem") and v.get("ra_key_enc")) or uses_host_files(v)
        return has_cert and all(
            v.get(f) for f in ("ra_trust_anchor_arn", "ra_profile_arn", "ra_role_arn")
        )
    return mode == "default_chain"


# ---------------------------------------------------------------- providers
def make_provider(
    v: dict[str, Any],
    http: Any,
    secret_key: str,
    *,
    role_arn: str | None = None,
    unconfigured: str = "aws_connection_unconfigured",
) -> Any:
    """The base provider of a connection view (`role_arn`: access `ra_role`)."""
    mode = v.get("auth_mode")
    if mode == "access_key":
        try:
            secret = decrypt_field(v, "secret_access_key_enc", secret_key)
        except Unreadable:
            raise AssistantError(
                "aws_auth_failed",
                "The stored secret access key cannot be decrypted (secret.key changed?); "
                "enter it again.",
            ) from None
        if not v.get("access_key_id") or not secret:
            raise make(unconfigured)
        try:
            token = decrypt_field(v, "session_token_enc", secret_key)
        except Unreadable:
            raise AssistantError(
                "aws_auth_failed",
                "The stored session token cannot be decrypted (secret.key changed?); "
                "enter it again.",
            ) from None
        if is_temporary_key(v["access_key_id"]) and not token:
            raise make("aws_session_token_required")
        return StaticProvider(v["access_key_id"], secret, token)
    if mode == "roles_anywhere":
        return RolesAnywhereProvider(v, lambda: ra_material(v, secret_key), http, role_arn=role_arn)
    if mode == "default_chain":
        return DefaultChainProvider()
    raise make(unconfigured)


# ---------------------------------------------------------------- consumers
async def load_assistant_row(session: Any) -> dict[str, Any] | None:
    from netmon.models import Setting

    row = await session.get(Setting, "assistant")
    return dict(row.value) if row is not None and isinstance(row.value, dict) else None


def assistant_is_consumer(asst: dict[str, Any] | None, assistant_on: bool) -> bool:
    """The assistant counts as a consumer when it is ON or configured (a model chosen), or
    when it names a connection explicitly."""
    if asst is None:
        return False
    return bool(assistant_on or asst.get("model_id") or asst.get("connection_id") is not None)


async def used_by(session: Any, row: dict[str, Any], *, assistant_on: bool) -> list[dict[str, Any]]:
    """The consumers of a connection (explicitly, or implicitly as the default)."""
    out: list[dict[str, Any]] = []
    asst = await load_assistant_row(session)
    if assistant_is_consumer(asst, assistant_on):
        assert asst is not None
        cid = asst.get("connection_id")
        if cid == row["id"] or (cid is None and row.get("is_default")):
            access = normalize_access(asst.get("access") or DEFAULT_ACCESS)
            out.append(
                {
                    "kind": "assistant",
                    "id": None,
                    "name": "Assistant",
                    "access_mode": access["mode"],
                }
            )
    return out


async def sync_legacy(session: Any) -> bool:
    """Legacy mirror (architecture § 15.1.4): while the assistant uses the `aad_scope =
    'assistant'` connection with access `direct`, copy that connection's authentication fields
    into the 1.4.x keys of the `assistant` row (ciphertexts verbatim: same AAD), in the caller's
    transaction. Returns True when it wrote."""
    from netmon.services import settings as settings_svc

    asst = await load_assistant_row(session)
    if asst is None:
        return False
    access = normalize_access(asst.get("access") or DEFAULT_ACCESS)
    if access["mode"] != "direct":
        return False
    row = await resolve(session, asst.get("connection_id"))
    if row is None or row.get("aad_scope") != LEGACY_SCOPE:
        return False
    legacy = legacy_values(row)
    if all(asst.get(k) == legacy[k] for k in LEGACY_KEYS):
        return False
    asst.update(legacy)
    await settings_svc.put(session, "assistant", asst)
    return True


def legacy_values(row: dict[str, Any]) -> dict[str, Any]:
    """The 1.4.x `assistant` authentication keys equivalent to a connection row."""
    v = view(row)
    out = {k: v.get(k) for k in LEGACY_KEYS if k in SETTINGS_DEFAULTS}
    out["auth_mode"] = v["auth_mode"]
    out["proxy_mode"] = v["proxy_mode"]
    out["proxy_url"] = v["proxy_url"]
    out["ra_region"] = v.get("ra_region") or v["region"]
    out["ra_endpoint_url"] = v.get("ra_endpoint_url")
    return out
