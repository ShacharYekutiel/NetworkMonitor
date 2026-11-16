"""[2b-3] Active Directory login against the in-process fake AD (tests/sim/ldap/fake_ad.py):
fixture users, three name forms, LDAPS and StartTLS, wrong certificate, failover, hang budget
with a parallel local login, empty password never sent, directory guard, encrypted bind
password, ldap_managed refusals, 12 h session cap, test endpoints, local precedence."""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any

import asyncpg
import httpx
import pytest

from tests.conftest import ADMIN, _plain, requires_db
from tests.fixtures_2b3_auth import ldap_settings

pytestmark = requires_db


async def _configure(admin: httpx.AsyncClient, ad: Any, **kw: Any) -> None:
    r = await admin.put("/api/settings", json={"ldap": ldap_settings(ad, **kw)})
    assert r.status_code == 200, r.text
    out = r.json()["ldap"]
    assert out["bind_password"] is None and out["bind_password_set"] is True


async def _login(app: Any, username: str, password: str = "Passw0rd!") -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.post("/api/auth/login", json={"username": username, "password": password})
        if r.status_code == 200:
            me = await c.get("/api/auth/me")
            assert me.status_code == 200
        return r


async def _audit_rows(rt: Any, action: str) -> list[Any]:
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        return await conn.fetch("SELECT * FROM audit_log WHERE action = $1 ORDER BY id", action)
    finally:
        await conn.close()


@pytest.fixture
async def ad_on(admin: httpx.AsyncClient, fake_ad: Any, feats: Any) -> Any:
    feats(ldap=True, audit=True)
    await _configure(admin, fake_ad)
    return fake_ad


async def test_name_forms_and_roles(
    app: Any, admin: httpx.AsyncClient, ad_on: Any, rt: Any
) -> None:
    for name in (
        "jdoe",
        "MNIDOM\\jdoe",
        "mnidom\\JDOE",
        "John.Doe@mnidom.net",
        "john.doe@MNIDOM.NET",
    ):
        r = await _login(app, name)
        assert r.status_code == 200, (name, r.text)
        user = r.json()["user"]
        assert user["username"] == "jdoe" and user["role"] == "viewer"
        assert user["source"] == "ldap" and user["display_name"] == "John Doe"
        assert user["email"] == "john.doe@mnidom.net" and user["ldap_synced_at"]
    r = await _login(app, "asmith")  # IT-Team ∈ NetMon-Admins (nested)
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    r = await _login(app, "bhelp")  # Helpdesk ∈ IT-Team ∈ NetMon-Admins
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    users = (await admin.get("/api/users")).json()["items"]
    jdoe = [u for u in users if u["username"] == "jdoe"]
    assert len(jdoe) == 1  # one app user for every name form (objectGUID)
    creates = await _audit_rows(rt, "user.ldap_create")
    assert sorted(r["entity_name"] for r in creates) == ["asmith", "bhelp", "jdoe"]
    logins = await _audit_rows(rt, "auth.login")
    assert {r["source"] for r in logins} == {"ldap"}


@pytest.mark.parametrize(
    ("username", "password", "reason"),
    [
        ("nogroup", "Passw0rd!", "not_in_group"),
        ("olduser", "Passw0rd!", "account_disabled"),
        ("lockeduser", "Passw0rd!", "account_locked"),
        ("expired", "Passw0rd!", "password_expired"),
        ("jdoe", "wrong-pass", "bad_password"),
        ("nobody", "Passw0rd!", "unknown_user"),
        ("OTHER\\jdoe", "Passw0rd!", "unknown_user"),
    ],
)
async def test_denied_users(
    app: Any, ad_on: Any, rt: Any, username: str, password: str, reason: str
) -> None:
    r = await _login(app, username, password)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_credentials"
    rows = await _audit_rows(rt, "auth.login_failed")
    assert rows and rows[-1]["reason"] == reason
    assert rows[-1]["actor_type"] == "anonymous" and rows[-1]["actor_name"] == username


async def test_filter_injection_is_escaped(app: Any, ad_on: Any, rt: Any) -> None:
    # unescaped, "*@mnidom.net" would match every account with a UPN (ambiguous/first hit)
    r = await _login(app, "*@mnidom.net")
    assert r.status_code == 401
    assert (await _audit_rows(rt, "auth.login_failed"))[-1]["reason"] == "unknown_user"
    r = await _login(app, "x*)(sAMAccountName=jdoe@mnidom.net")
    assert r.status_code == 401
    assert (await _audit_rows(rt, "auth.login_failed"))[-1]["reason"] == "unknown_user"


async def test_empty_password_never_sent(app: Any, ad_on: Any, rt: Any) -> None:
    before = len(ad_on.binds)
    r = await _login(app, "jdoe", "")
    assert r.status_code == 401
    assert len(ad_on.binds) == before  # not even the service account bound
    assert not any(b["password_empty"] for b in ad_on.binds)
    assert (await _audit_rows(rt, "auth.login_failed"))[-1]["reason"] == "empty_password"


async def test_directory_guard(app: Any, ad_on: Any) -> None:
    for _ in range(5):
        assert (await _login(app, "jdoe", "bad-one")).status_code == 401
    binds = len(ad_on.binds)
    r = await _login(app, "MNIDOM\\jdoe", "Passw0rd!")  # same account, other name form
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert len(ad_on.binds) == binds  # the directory is not contacted any more


async def test_starttls(app: Any, admin: httpx.AsyncClient, fake_ad: Any, feats: Any) -> None:
    feats(ldap=True)
    await _configure(admin, fake_ad, security="starttls")
    r = await _login(app, "asmith")
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    assert fake_ad.binds and all(b["tls"] for b in fake_ad.binds)


async def test_wrong_certificate_fails_closed(
    app: Any,
    admin: httpx.AsyncClient,
    ad_certs: Any,
    trust_test_ca: None,
    feats: Any,
) -> None:
    from tests.fixtures_2b3_auth import LDAP_SIM, _sim_modules

    fa, _ = _sim_modules()
    ad = fa.FakeAD(fa.load_directory(LDAP_SIM / "mnidom.yaml"), certs=ad_certs, wrong_cert=True)
    await ad.start()
    try:
        feats(ldap=True)
        await _configure(admin, ad)
        r = await _login(app, "jdoe")
        assert r.status_code == 503 and r.json()["error"]["code"] == "directory_unavailable"
        assert ad.binds == []  # no credentials sent over an unverified connection
        res = (await admin.post("/api/ldap/test", json={})).json()
        assert res["ok"] is False
        row = res["servers"][0]
        assert row["ok"] is False and "certificate verify failed" in row["error"]
    finally:
        await ad.stop()


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_failover_to_second_server(
    app: Any,
    admin: httpx.AsyncClient,
    fake_ad: Any,
    feats: Any,
) -> None:
    feats(ldap=True)
    dead = f"localhost:{_closed_port()}"
    await _configure(admin, fake_ad, servers=[dead, f"localhost:{fake_ad.ldaps_port}"], timeout_s=1)
    r = await _login(app, "jdoe")
    assert r.status_code == 200
    res = (await admin.post("/api/ldap/test", json={})).json()
    assert [s["ok"] for s in res["servers"]] == [False, True]
    assert res["ok"] is True and res["service_bind"]["ok"] and res["base_dn"]["ok"]


async def test_hang_budget_and_local_login(
    app: Any,
    admin: httpx.AsyncClient,
    fake_ad: Any,
    feats: Any,
) -> None:
    feats(ldap=True)
    await _configure(admin, fake_ad, timeout_s=1)  # budget min(3*1+2, 30) = 5 s
    fake_ad.hang = True
    started = time.monotonic()
    ldap_task = asyncio.create_task(_login(app, "jdoe"))
    await asyncio.sleep(0.2)
    t0 = time.monotonic()
    local = await _login(app, ADMIN["username"], ADMIN["password"])
    assert local.status_code == 200
    assert time.monotonic() - t0 < 1.0  # a dead DC never delays local logins
    r = await ldap_task
    assert r.status_code == 503 and r.json()["error"]["code"] == "directory_unavailable"
    assert time.monotonic() - started < 5.0 + 1.5


async def test_bind_password_encrypted_at_rest(
    admin: httpx.AsyncClient, ad_on: Any, rt: Any
) -> None:
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        raw = await conn.fetchval("SELECT value::text FROM settings WHERE key = 'ldap'")
    finally:
        await conn.close()
    assert "Passw0rd!" not in raw and '"bind_password_enc": "v1:' in raw
    s = (await admin.get("/api/settings")).json()["ldap"]
    assert s["bind_password"] is None and s["bind_password_set"] is True
    assert s["bind_password_unreadable"] is False
    # settings audit: the password only as "***"
    rows = await _audit_rows(rt, "settings.update")
    text = " ".join(str(r["changes"]) for r in rows)
    assert "Passw0rd!" not in text and "v1:" not in text


async def test_unreadable_bind_password(
    app: Any, admin: httpx.AsyncClient, ad_on: Any, rt: Any
) -> None:
    from netmon.services import crypto

    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        other = crypto.encrypt("another secret.key", "Passw0rd!", crypto.AAD_LDAP_BIND_PASSWORD)
        await conn.execute(
            "UPDATE settings SET value = "
            "jsonb_set(value, '{bind_password_enc}', to_jsonb($1::text)) WHERE key = 'ldap'",
            other,
        )
    finally:
        await conn.close()
    s = (await admin.get("/api/settings")).json()["ldap"]
    assert s["bind_password_unreadable"] is True and s["bind_password_set"] is False
    r = await _login(app, "jdoe")
    assert r.status_code == 503
    # saving the AD settings while AD login is on is refused with the missing field
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('feature_ldap_enabled', 'true') "
            "ON CONFLICT (key) DO UPDATE SET value = 'true'"
        )
    finally:
        await conn.close()
    r = await admin.put("/api/settings", json={"ldap": {"timeout_s": 3}})
    assert r.status_code == 422
    assert any(d["loc"][-1] == "bind_password" for d in r.json()["error"]["details"])


async def test_ldap_managed_and_last_local_admin(
    app: Any, admin: httpx.AsyncClient, ad_on: Any
) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.post("/api/auth/login", json={"username": "asmith", "password": "Passw0rd!"})
        assert r.status_code == 200
        uid = r.json()["user"]["id"]
        r = await c.post(
            "/api/auth/change-password",
            json={"current_password": "Passw0rd!", "new_password": "Another-pass1"},
        )
        assert r.status_code == 409 and r.json()["error"]["code"] == "ldap_managed"
        r = await c.patch("/api/auth/me", json={"email": "other@example.com"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "ldap_managed"
        assert (await c.patch("/api/auth/me", json={"language": "he"})).status_code == 200
        # the LDAP admin is an admin, but not a *local* admin: the local one stays protected
        me_local = (await admin.get("/api/auth/me")).json()["user"]
        r = await c.patch(f"/api/users/{me_local['id']}", json={"role": "viewer"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "last_admin"
        r = await c.delete(f"/api/users/{me_local['id']}")
        assert r.status_code == 409 and r.json()["error"]["code"] == "last_admin"
    for body in ({"password": "Another-pass1"}, {"role": "viewer"}, {"email": "x@example.com"}):
        r = await admin.patch(f"/api/users/{uid}", json=body)
        assert r.status_code == 409 and r.json()["error"]["code"] == "ldap_managed", body
    r = await admin.patch(f"/api/users/{uid}", json={"language": "he", "is_active": False})
    assert r.status_code == 200
    r = await _login(app, "asmith")  # deactivated in the app: refused regardless of AD
    assert r.status_code == 401
    # a local user cannot take an LDAP user's name
    r = await admin.post("/api/users", json={"username": "ASMITH", "password": "Passw0rd-123"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"
    # deleting an LDAP user is allowed (re-created at the next login)
    assert (await admin.patch(f"/api/users/{uid}", json={"is_active": True})).status_code == 200
    assert (await admin.delete(f"/api/users/{uid}")).status_code == 204
    assert (await _login(app, "asmith")).status_code == 200


async def test_local_account_wins(app: Any, admin: httpx.AsyncClient, ad_on: Any) -> None:
    r = await admin.post("/api/users", json={"username": "jdoe", "password": "Local-pass-1"})
    assert r.status_code == 201
    binds = len(ad_on.binds)
    assert (await _login(app, "jdoe", "Passw0rd!")).status_code == 401  # AD password: no
    assert (await _login(app, "jdoe", "Local-pass-1")).status_code == 200
    assert len(ad_on.binds) == binds  # AD never contacted for a local name
    # the UPN form resolves to sAMAccountName jdoe, which a local account owns
    assert (await _login(app, "John.Doe@mnidom.net")).status_code == 401


async def test_role_change_at_login(
    app: Any, admin: httpx.AsyncClient, ad_on: Any, rt: Any
) -> None:
    assert (await _login(app, "asmith")).status_code == 200
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        await conn.execute("UPDATE users SET role = 'viewer' WHERE username = 'asmith'")
        tv = await conn.fetchval("SELECT token_version FROM users WHERE username = 'asmith'")
    finally:
        await conn.close()
    r = await _login(app, "asmith")
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        assert (
            await conn.fetchval("SELECT token_version FROM users WHERE username = 'asmith'")
            == tv + 1
        )
    finally:
        await conn.close()
    rows = await _audit_rows(rt, "user.ldap_role_change")
    assert rows and rows[-1]["entity_name"] == "asmith"


async def test_session_capped_at_12h(app: Any, ad_on: Any, rt: Any) -> None:
    import jwt

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.post("/api/auth/login", json={"username": "jdoe", "password": "Passw0rd!"})
        assert r.status_code == 200
        uid = r.json()["user"]["id"]
        claims = jwt.decode(c.cookies["netmon_session"], options={"verify_signature": False})
        assert claims["src"] == "ldap" and claims["exp"] - claims["at"] <= 12 * 3600
        # sliding renewal near the end of the cookie: renewed, but never past at + 12 h
        now = int(time.time())
        old = jwt.encode(
            {
                "sub": str(uid),
                "role": "viewer",
                "tv": 0,
                "iat": now - 10 * 3600,
                "exp": now + 3600,
                "src": "ldap",
                "at": now - 10 * 3600,
                "sid": "ldap-test-session",
            },
            rt.secret_key,
            algorithm="HS256",
        )
        c.cookies.set("netmon_session", old)
        r = await c.get("/api/auth/me")
        assert r.status_code == 200
        new = jwt.decode(r.cookies["netmon_session"], options={"verify_signature": False})
        assert new["at"] == now - 10 * 3600
        assert new["exp"] <= now + 2 * 3600 + 5  # at + 12 h, not now + 12 h
        # the app clock (test clock) past at + 12 h: the session ends
        c.cookies.set("netmon_session", r.cookies["netmon_session"])
        rt.clock.set_offset(2 * 3600 + 60)
        try:
            assert (await c.get("/api/auth/me")).status_code == 401
        finally:
            rt.clock.set_offset(0)
        # local users keep the normal sliding renewal
    assert claims["exp"] - now > 11 * 3600


async def test_feature_off(
    app: Any, admin: httpx.AsyncClient, ad_on: Any, feats: Any, rt: Any
) -> None:
    assert (await _login(app, "jdoe")).status_code == 200
    feats(ldap=False)
    binds = len(ad_on.binds)
    r = await _login(app, "jdoe")
    assert r.status_code == 401
    assert (await _audit_rows(rt, "auth.login_failed"))[-1]["reason"] == "ldap_disabled"
    r = await _login(app, "never-seen")
    assert (await _audit_rows(rt, "auth.login_failed"))[-1]["reason"] == "unknown_user"
    assert len(ad_on.binds) == binds
    assert (await admin.get("/api/settings/public")).json()["ldap"] is None
    # the test endpoints work while the feature is OFF
    res = await admin.post("/api/ldap/test", json={})
    assert res.status_code == 200 and res.json()["ok"] is True
    feats(ldap=True)
    assert (await admin.get("/api/settings/public")).json()["ldap"] == {
        "domain": "mnidom.net",
        "netbios_domain": "MNIDOM",
    }


async def test_test_endpoints(admin: httpx.AsyncClient, ad_on: Any) -> None:
    res = (await admin.post("/api/ldap/test", json={})).json()
    assert res["ok"] is True and res["discovered_via_dns"] is False
    row = res["servers"][0]
    assert row["ok"] and row["security"] == "ldaps" and row["active_directory"] is True
    assert row["tls"]["verified"] is True and row["tls"]["subject"] and row["tls"]["not_after"]
    assert res["admin_group"] == {
        "dn": "CN=NetMon-Admins,OU=Groups,DC=mnidom,DC=net",
        "found": True,
    }
    # a partial config overrides the stored one (not saved)
    res = (
        await admin.post(
            "/api/ldap/test",
            json={"ldap": {"viewer_group_dn": "CN=Nope,OU=Groups,DC=mnidom,DC=net"}},
        )
    ).json()
    assert res["ok"] is False and res["viewer_group"]["found"] is False
    res = (await admin.post("/api/ldap/test", json={"ldap": {"bind_password": "wrong"}})).json()
    assert res["service_bind"]["ok"] is False and res["ok"] is False
    # test user
    r = await admin.post(
        "/api/ldap/test-user", json={"username": "asmith", "password": "Passw0rd!"}
    )
    body = r.json()
    assert body["ok"] is True and body["role"] == "admin" and body["reason"] is None
    assert [s["step"] for s in body["steps"]] == [
        "connect",
        "service_bind",
        "search_user",
        "account_state",
        "user_bind",
        "groups",
    ]
    assert body["user"]["sam_account_name"] == "asmith" and body["user"]["disabled"] is False
    r = await admin.post(
        "/api/ldap/test-user", json={"username": "olduser", "password": "Passw0rd!"}
    )
    body = r.json()
    assert body["ok"] is False and body["reason"] == "account_disabled"
    assert body["user"]["disabled"] is True
    users = (await admin.get("/api/users")).json()["items"]
    assert [u["username"] for u in users] == ["admin"]  # no user created by a test
