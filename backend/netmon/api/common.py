"""Helpers shared by routers."""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.api.errors import ApiError
from netmon.schemas import SnmpIn

SECRET_SNMP = ("community", "v3_auth_key", "v3_priv_key")


def merge_secret(incoming: str | None, stored: str | None) -> str | None:
    """Write-only secret rules: None/omitted keep, "" clear, other replace."""
    if incoming is None:
        return stored
    if incoming == "":
        return None
    return incoming


def merge_snmp(incoming: SnmpIn, stored: dict[str, Any] | None) -> dict[str, Any]:
    data = incoming.model_dump()
    stored = stored or {}
    for key in SECRET_SNMP:
        data[key] = merge_secret(data.get(key), stored.get(key))
    if data.get("community") is None and incoming.community is None and not stored:
        data["community"] = "public"  # new credentials object: data-model default
    if data["version"] == "v3":
        errors = []
        if not data.get("v3_username"):
            errors.append(
                {"loc": ["body", "snmp_credentials", "v3_username"], "msg": "required for SNMP v3"}
            )
        level = data.get("v3_security_level") or "noAuthNoPriv"
        data["v3_security_level"] = level
        if level in ("authNoPriv", "authPriv"):
            data["v3_auth_protocol"] = data.get("v3_auth_protocol") or "SHA"
            if not data.get("v3_auth_key"):
                errors.append(
                    {
                        "loc": ["body", "snmp_credentials", "v3_auth_key"],
                        "msg": "required for this security level",
                    }
                )
        if level == "authPriv":
            data["v3_priv_protocol"] = data.get("v3_priv_protocol") or "AES128"
            if not data.get("v3_priv_key"):
                errors.append(
                    {
                        "loc": ["body", "snmp_credentials", "v3_priv_key"],
                        "msg": "required for this security level",
                    }
                )
        if errors:
            raise ApiError(422, "validation_error", "Invalid SNMP v3 credentials", errors)
    return data


async def commit_or_conflict(session: AsyncSession, field: str = "name") -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        msg = str(exc.orig) if exc.orig else str(exc)
        if "unique" in msg.lower() or "duplicate" in msg.lower():
            raise ApiError(
                409, "conflict", f"An item with this {field} already exists", [{"field": field}]
            ) from exc
        if "foreign key" in msg.lower():
            raise ApiError(422, "validation_error", "Referenced item does not exist") from exc
        raise


async def flush_or_conflict(session: AsyncSession, field: str = "name") -> None:
    """[2b-3] Flush (the new row gets its id for the audit record) with commit_or_conflict's
    error mapping; the transaction is rolled back on a conflict."""
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        msg = str(exc.orig) if exc.orig else str(exc)
        if "unique" in msg.lower() or "duplicate" in msg.lower():
            raise ApiError(
                409, "conflict", f"An item with this {field} already exists", [{"field": field}]
            ) from exc
        if "foreign key" in msg.lower():
            raise ApiError(422, "validation_error", "Referenced item does not exist") from exc
        raise


def csv_list(value: str | None) -> list[str] | None:
    if value is None or value == "":
        return None
    return [v.strip() for v in value.split(",") if v.strip()]
