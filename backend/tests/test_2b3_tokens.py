"""[2b-3] API tokens: once-only display, hashed storage, role cap, effective role, revoked /
expired / inactive owner → 401, barred endpoints, no CSRF check, throttle. Always available
(no feature toggle)."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

import asyncpg
import httpx

from tests.conftest import _plain, make_device, requires_db

pytestmark = requires_db


def _bearer(app: Any, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _create(c: httpx.AsyncClient, **kw: Any) -> dict:
    r = await c.post("/api/tokens", json={"name": "script", "role": "viewer", **kw})
    assert r.status_code == 201, r.text
    return r.json()


async def test_always_available(admin: httpx.AsyncClient, app: Any, feats: Any) -> None:
    """No toggle: with every feature OFF tokens can be created, listed and used."""
    tok = (await _create(admin))["token"]
    assert (await admin.get("/api/tokens")).status_code == 200
    async with _bearer(app, tok) as c:
        assert (await c.get("/api/devices")).status_code == 200


async def test_create_use_list_revoke(
    admin: httpx.AsyncClient, app: Any, feats: Any, rt: Any
) -> None:
    feats(audit=True)
    body = await _create(admin, name="backup-script", role="admin", expires_days=30)
    token, item = body["token"], body["item"]
    assert token.startswith("nmt_") and len(token) == 47
    assert item["prefix"] == token[:12] and item["state"] == "active"
    assert item["role"] == "admin" and item["username"] == "admin"
    assert set(item) == {
        "id",
        "name",
        "prefix",
        "role",
        "user_id",
        "username",
        "created_at",
        "expires_at",
        "last_used_at",
        "last_used_ip",
        "revoked_at",
        "state",
    }
    # stored hashed only
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        row = await conn.fetchrow("SELECT * FROM api_tokens WHERE id = $1", item["id"])
    finally:
        await conn.close()
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(dict(row))
    # list never returns the token text
    listed = (await admin.get("/api/tokens")).json()["items"]
    assert [t["id"] for t in listed] == [item["id"]] and "token" not in listed[0]
    # duplicate name (case-insensitive) per user
    r = await admin.post("/api/tokens", json={"name": "BACKUP-script", "role": "viewer"})
    assert r.status_code == 409
    for bad in ({"expires_days": 0}, {"expires_days": 366}, {"role": "root"}, {"name": ""}):
        r = await admin.post("/api/tokens", json={"name": "n2", **bad})
        assert r.status_code == 422, bad
    # use it
    async with _bearer(app, token) as c:
        d = await make_device(c, name="via-token")
        r = await c.get(f"/api/devices/{d['id']}")
        assert r.status_code == 200 and "set-cookie" not in r.headers
        # the CSRF Origin check does not apply to bearer requests
        r = await c.patch(
            f"/api/devices/{d['id']}",
            json={"name": "x2"},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 200
        # barred endpoints
        for method, path in (
            ("GET", "/api/auth/me"),
            ("POST", "/api/auth/logout"),
            ("GET", "/api/users"),
            ("GET", "/api/users/1"),
            ("GET", "/api/tokens"),
            ("POST", "/api/tokens"),
        ):
            r = await c.request(method, path, json={} if method == "POST" else None)
            assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden", path
    listed = (await admin.get("/api/tokens")).json()["items"][0]
    assert listed["last_used_at"] is not None and listed["last_used_ip"]
    # a change made with a token is audited as the token
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        audits = await conn.fetch(
            "SELECT actor_type, token_id, actor_name, action FROM audit_log "
            "WHERE action LIKE 'device.%' OR action LIKE 'api_token.%' ORDER BY id"
        )
    finally:
        await conn.close()
    assert [a["action"] for a in audits] == ["api_token.create", "device.create", "device.update"]
    assert audits[1]["actor_type"] == "token" and audits[1]["token_id"] == item["id"]
    assert audits[1]["actor_name"] == "admin"
    # revoke (idempotent) → 401
    assert (await admin.delete(f"/api/tokens/{item['id']}")).status_code == 204
    assert (await admin.delete(f"/api/tokens/{item['id']}")).status_code == 204
    async with _bearer(app, token) as c:
        assert (await c.get("/api/devices")).status_code == 401
    assert (await admin.get("/api/tokens")).json()["items"][0]["state"] == "revoked"


async def test_role_cap_and_effective_role(
    admin: httpx.AsyncClient, viewer: httpx.AsyncClient, app: Any, feats: Any
) -> None:
    r = await viewer.post("/api/tokens", json={"name": "v", "role": "admin"})
    assert r.status_code == 422
    vtok = (await _create(viewer, name="v"))["token"]
    async with _bearer(app, vtok) as c:
        assert (await c.get("/api/devices")).status_code == 200
        r = await c.post("/api/groups", json={"name": "nope"})
        assert r.status_code == 403
    # an admin's viewer token is a viewer
    atok_v = (await _create(admin, name="ro"))["token"]
    async with _bearer(app, atok_v) as c:
        assert (await c.post("/api/groups", json={"name": "nope"})).status_code == 403
    # an admin token of a user who is later demoted acts as viewer
    r = await admin.post(
        "/api/users", json={"username": "ops", "password": "ops-pass-12", "role": "admin"}
    )
    ops_id = r.json()["id"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ops:
        await ops.post("/api/auth/login", json={"username": "ops", "password": "ops-pass-12"})
        otok = (await _create(ops, name="ops", role="admin"))["token"]
    async with _bearer(app, otok) as c:
        assert (await c.post("/api/groups", json={"name": "g-ok"})).status_code == 201
    await admin.patch(f"/api/users/{ops_id}", json={"role": "viewer"})
    async with _bearer(app, otok) as c:
        assert (await c.post("/api/groups", json={"name": "g-no"})).status_code == 403
        assert (await c.get("/api/groups")).status_code == 200
    # owner inactive → 401
    await admin.patch(f"/api/users/{ops_id}", json={"is_active": False})
    async with _bearer(app, otok) as c:
        assert (await c.get("/api/groups")).status_code == 401
    # listing: own only; admin ?all=true sees every token; a viewer may not
    assert len((await viewer.get("/api/tokens")).json()["items"]) == 1
    assert (await viewer.get("/api/tokens", params={"all": "true"})).status_code == 403
    every = (await admin.get("/api/tokens", params={"all": "true"})).json()["items"]
    assert {t["username"] for t in every} == {"admin", "viewer", "ops"}
    # a viewer cannot revoke someone else's token; an admin can revoke any
    vid = (await viewer.get("/api/tokens")).json()["items"][0]["id"]
    aid = (await admin.get("/api/tokens")).json()["items"][0]["id"]
    assert (await viewer.delete(f"/api/tokens/{aid}")).status_code == 404
    assert (await admin.delete(f"/api/tokens/{vid}")).status_code == 204


async def test_expiry_and_cookie_ignored(
    admin: httpx.AsyncClient, app: Any, feats: Any, rt: Any
) -> None:
    tok = (await _create(admin, expires_days=1))["token"]
    async with _bearer(app, tok) as c:
        assert (await c.get("/api/devices")).status_code == 200
    rt.clock.set_offset(timedelta(days=1, seconds=5).total_seconds())
    try:
        async with _bearer(app, tok) as c:
            assert (await c.get("/api/devices")).status_code == 401
        listed = (await admin.get("/api/tokens")).json()["items"][0]
        assert listed["state"] == "expired"
    finally:
        rt.clock.set_offset(0)
    # a bearer header wins over a valid session cookie
    admin.headers["Authorization"] = "Bearer nmt_unknown"
    try:
        assert (await admin.get("/api/devices")).status_code == 401
    finally:
        del admin.headers["Authorization"]


async def test_invalid_token_throttle_and_audit(
    admin: httpx.AsyncClient, app: Any, feats: Any, rt: Any
) -> None:
    feats(audit=True)
    async with _bearer(app, "nmt_" + "x" * 43) as c:
        for _ in range(20):
            assert (await c.get("/api/devices")).status_code == 401
        r = await c.get("/api/devices")
        assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        rows = await conn.fetch(
            "SELECT * FROM audit_log WHERE action = 'auth.login_failed' ORDER BY id"
        )
    finally:
        await conn.close()
    assert len(rows) == 20
    assert {(r["source"], r["reason"], r["actor_type"]) for r in rows} == {
        ("token", "token_invalid", "anonymous")
    }
    assert all("x" * 20 not in r["actor_name"] for r in rows)  # never the token text
