"""[2b-3] Active Directory test endpoints (admin). They work while the `ldap` feature is OFF so
the configuration can be verified before it is enabled."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from netmon.api.deps import DB, RT, Admin
from netmon.api.errors import ApiError
from netmon.auth import ldap_config, ldap_pure
from netmon.auth.ldap_config import LdapSettingsIn
from netmon.services import settings as settings_svc

router = APIRouter()


class LdapTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ldap: LdapSettingsIn | None = None


class LdapUserTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(max_length=1024)
    ldap: LdapSettingsIn | None = None


async def _config(session: DB, rt: RT, partial: LdapSettingsIn | None) -> ldap_config.LdapConfig:
    stored = await settings_svc.get(session, "ldap")
    merged = ldap_config.merge(stored, partial, rt.secret_key)
    await session.close()
    return ldap_config.to_config(merged, rt.secret_key)


@router.post("/api/ldap/test")
async def ldap_test(body: LdapTestIn, request: Request, _: Admin, rt: RT, session: DB) -> dict:
    cfg = await _config(session, rt, body.ldap)
    return await request.app.state.directory.test_connection(cfg)


@router.post("/api/ldap/test-user")
async def ldap_test_user(
    body: LdapUserTestIn, request: Request, _: Admin, rt: RT, session: DB
) -> dict:
    cfg = await _config(session, rt, body.ldap)
    directory = request.app.state.directory
    name = ldap_pure.parse_login(body.username, cfg.domain, cfg.netbios_domain)
    key = ((name.sam or name.upn) if name else body.username).lower()
    if directory.guard.blocked(key):
        raise ApiError(429, "rate_limited", "Too many failed directory logins for this user")
    outcome = await directory.authenticate(cfg, body.username, body.password, now=rt.clock.now())
    if not outcome.ok and not outcome.unavailable and outcome.reason != "empty_password":
        directory.guard.fail(key)
    return {
        "ok": outcome.ok,
        "steps": [s.out() for s in outcome.steps],
        "user": outcome.user.out() if outcome.user else None,
        "role": outcome.role,
        "reason": outcome.reason,
    }
