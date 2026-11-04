from __future__ import annotations

import time

import httpx
import jwt
import pytest

from tests.conftest import ADMIN, requires_db

pytestmark = requires_db


async def test_setup_flow(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/setup/status")
    assert r.json() == {"setup_required": True}
    # gated endpoints answer setup_required while there are no users
    r = await client.get("/api/tree")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "setup_required"
    # exempt endpoints
    assert (await client.get("/api/health")).status_code == 200
    assert (await client.get("/api/settings/public")).json() == {"default_language": "en"}
    r = await client.get("/api/auth/me")
    assert r.status_code == 401

    r = await client.post("/api/setup", json={**ADMIN, "language": "he"})
    assert r.status_code == 201, r.text
    user = r.json()["user"]
    assert set(user) == {"id", "username", "email", "role", "language", "is_active",
                         "last_login_at", "created_at"}
    assert user["role"] == "admin" and user["language"] == "he"
    assert "netmon_session" in r.cookies
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=43200" in cookie
    assert "secure" not in cookie  # plain http

    assert (await client.get("/api/setup/status")).json() == {"setup_required": False}
    assert (await client.get("/api/settings/public")).json() == {"default_language": "he"}
    r = await client.get("/api/groups")
    assert [g["name"] for g in r.json()["items"]] == ["Default"]
    r = await client.post("/api/setup", json=ADMIN)
    assert r.status_code == 409 and r.json()["error"]["code"] == "setup_already_done"


async def test_setup_validation(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/setup", json={"username": "a", "password": "short"})
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "validation_error"
    locs = {tuple(d["loc"]) for d in body["details"]}
    assert ("body", "username") in locs and ("body", "password") in locs


async def test_login_logout_me(admin: httpx.AsyncClient) -> None:
    r = await admin.post("/api/auth/logout")
    assert r.status_code == 204
    assert "max-age=0" in r.headers["set-cookie"].lower()
    admin.cookies.clear()
    assert (await admin.get("/api/auth/me")).status_code == 401
    r = await admin.post("/api/auth/login", json={"username": "ADMIN", "password": "wrong-pass"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    r = await admin.post("/api/auth/login", json={"username": "Admin", "password": ADMIN["password"]})
    assert r.status_code == 200
    me = (await admin.get("/api/auth/me")).json()["user"]
    assert me["username"] == "admin" and me["last_login_at"] is not None
    r = await admin.patch("/api/auth/me", json={"language": "he", "email": "a@example.com"})
    assert r.json()["user"]["language"] == "he"
    assert r.json()["user"]["email"] == "a@example.com"


async def test_login_rate_limit(admin: httpx.AsyncClient) -> None:
    for _ in range(10):
        r = await admin.post("/api/auth/login", json={"username": "admin", "password": "nopenope"})
        assert r.status_code == 401
    r = await admin.post("/api/auth/login", json={"username": "admin", "password": ADMIN["password"]})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    # other usernames from the same IP are unaffected
    r = await admin.post("/api/auth/login", json={"username": "other", "password": "nopenope"})
    assert r.status_code == 401


async def test_change_password_invalidates_old_tokens(admin: httpx.AsyncClient) -> None:
    old_cookie = admin.cookies.get("netmon_session")
    r = await admin.post(
        "/api/auth/change-password",
        json={"current_password": "wrong-password", "new_password": "N3w-password"},
    )
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    r = await admin.post(
        "/api/auth/change-password",
        json={"current_password": ADMIN["password"], "new_password": "N3w-password"},
    )
    assert r.status_code == 204
    assert (await admin.get("/api/auth/me")).status_code == 200  # re-issued cookie
    admin.cookies.set("netmon_session", old_cookie)
    assert (await admin.get("/api/auth/me")).status_code == 401


async def test_csrf_origin_and_content_type(admin: httpx.AsyncClient) -> None:
    r = await admin.post(
        "/api/groups", json={"name": "X"}, headers={"Origin": "http://evil.example.com"}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    r = await admin.post("/api/groups", json={"name": "X"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 201
    r = await admin.post(
        "/api/groups", content=b"name=Y", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert r.status_code == 415 and r.json()["error"]["code"] == "unsupported_media_type"
    r = await admin.post("/api/groups", content=b"{bad json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"


async def test_viewer_role(viewer: httpx.AsyncClient) -> None:
    assert (await viewer.get("/api/tree")).status_code == 200
    r = await viewer.post("/api/groups", json={"name": "nope"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert (await viewer.get("/api/users")).status_code == 403
    assert (await viewer.get("/api/settings")).status_code == 403


async def test_users_crud_and_last_admin(admin: httpx.AsyncClient) -> None:
    r = await admin.post("/api/users", json={"username": "bob", "password": "bob-pass1",
                                             "role": "viewer"})
    assert r.status_code == 201
    bob = r.json()
    r = await admin.post("/api/users", json={"username": "BOB", "password": "bob-pass1",
                                             "role": "viewer"})
    assert r.status_code == 409 and r.json()["error"]["details"] == [{"field": "username"}]
    r = await admin.get("/api/users")
    assert [u["username"] for u in r.json()["items"]] == ["admin", "bob"]
    assert r.json()["total"] == 2 and r.json()["page"] == 1 and r.json()["page_size"] == 100
    me = (await admin.get("/api/auth/me")).json()["user"]
    r = await admin.delete(f"/api/users/{me['id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "last_admin"
    r = await admin.patch(f"/api/users/{me['id']}", json={"role": "viewer"})
    assert r.status_code == 409
    r = await admin.patch(f"/api/users/{bob['id']}", json={"role": "admin"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    r = await admin.patch(f"/api/users/{bob['id']}", json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert (await admin.delete(f"/api/users/{bob['id']}")).status_code == 204
    assert (await admin.get(f"/api/users/{bob['id']}")).status_code == 404


async def test_deactivated_user_session_invalid(app, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    await admin.post("/api/users", json={"username": "carol", "password": "carol-pass",
                                         "role": "viewer"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        await c.post("/api/auth/login", json={"username": "carol", "password": "carol-pass"})
        assert (await c.get("/api/auth/me")).status_code == 200
        users = (await admin.get("/api/users")).json()["items"]
        carol = next(u for u in users if u["username"] == "carol")
        await admin.patch(f"/api/users/{carol['id']}", json={"is_active": False})
        assert (await c.get("/api/auth/me")).status_code == 401


async def test_sliding_renewal(rt, admin: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    me = (await admin.get("/api/auth/me")).json()["user"]
    now = int(time.time())
    token = jwt.encode(
        {"sub": str(me["id"]), "role": "admin", "tv": 0, "iat": now - 40000, "exp": now + 3000},
        rt.secret_key,
        algorithm="HS256",
    )
    admin.cookies.set("netmon_session", token)
    r = await admin.get("/api/auth/me")
    assert r.status_code == 200
    assert "netmon_session=" in r.headers.get("set-cookie", "")
    new = r.cookies.get("netmon_session")
    claims = jwt.decode(new, rt.secret_key, algorithms=["HS256"])
    assert claims["exp"] - now > 40000
    # an expired token is rejected
    expired = jwt.encode(
        {"sub": str(me["id"]), "role": "admin", "tv": 0, "iat": now - 50000, "exp": now - 10},
        rt.secret_key,
        algorithm="HS256",
    )
    admin.cookies.set("netmon_session", expired)
    assert (await admin.get("/api/auth/me")).status_code == 401


@pytest.mark.parametrize("path", ["/api/nope", "/api/groups/abc/xyz"])
async def test_unknown_api_path(admin: httpx.AsyncClient, path: str) -> None:
    r = await admin.get(path)
    assert r.status_code == 404
    assert r.json() == {"error": {"code": "not_found", "message": "Not found", "details": None}}


async def test_method_not_allowed(admin: httpx.AsyncClient) -> None:
    r = await admin.put("/api/groups", json={})
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "method_not_allowed"
