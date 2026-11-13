"""[1.4.1] Session handling (findings 5, 7), login throttling (6), security headers (8) and the
OIDC landing page (10). Each pentest reproduction is a regression test here."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest

from netmon.api.sso import success_page
from netmon.services import security as security_svc
from netmon.services.auth import LoginLimiter
from tests.conftest import ADMIN, requires_db

SWAGGER_CDN = "https://cdn.jsdelivr.net"  # the only external source, /api/docs only

pytestmark = requires_db
COOKIE = "netmon_session"
LOGIN = {"username": ADMIN["username"], "password": ADMIN["password"]}


def _client(app: Any, **kw: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", **kw
    )


@pytest.fixture
async def second(app: Any, admin: httpx.AsyncClient) -> AsyncIterator[httpx.AsyncClient]:
    """A second browser of the same user."""
    async with _client(app) as c:
        assert (await c.post("/api/auth/login", json=LOGIN)).status_code == 200
        yield c


def _claims(c: httpx.AsyncClient) -> dict[str, Any]:
    return jwt.decode(c.cookies[COOKIE], options={"verify_signature": False})


def _forge(rt: Any, user_id: int, **over: Any) -> str:
    now = int(time.time())
    claims = {
        "sub": str(user_id),
        "role": "admin",
        "tv": 0,
        "iat": now,
        "exp": now + 3600,
        "src": "local",
        "at": now,
        "sid": "forged-session",
        **over,
    }
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, rt.secret_key, "HS256")


# ---------------------------------------------------------------- finding 5: logout
async def test_pentest_reproduction_logout_revokes_the_cookie(
    app: Any, rt: Any, admin: httpx.AsyncClient, second: httpx.AsyncClient
) -> None:
    """login -> keep the cookie -> me 200 -> logout 204 -> me with the OLD cookie was 200."""
    stolen = admin.cookies[COOKIE]
    assert (await admin.get("/api/auth/me")).status_code == 200
    assert (await admin.post("/api/auth/logout")).status_code == 204
    async with _client(app, cookies={COOKIE: stolen}) as thief:
        r = await thief.get("/api/auth/me")
        assert r.status_code == 401, r.text
        assert (await thief.get("/api/devices")).status_code == 401
    # only that session ended: the user's other browser stays signed in
    assert (await second.get("/api/auth/me")).status_code == 200
    assert _claims(second)["sid"] != jwt.decode(stolen, options={"verify_signature": False})["sid"]
    # the revocation is stored: it survives a restart of the server
    sid = jwt.decode(stolen, options={"verify_signature": False})["sid"]
    rt.revoked_sessions.clear()
    assert await security_svc.load_revoked(rt) == {sid}
    async with _client(app, cookies={COOKIE: stolen}) as thief:
        assert (await thief.get("/api/auth/me")).status_code == 401
    # logging in again gives a fresh session
    async with _client(app) as c:
        assert (await c.post("/api/auth/login", json=LOGIN)).status_code == 200
        assert (await c.get("/api/auth/me")).status_code == 200


async def test_renewed_cookie_of_a_logged_out_session_is_dead(
    app: Any, rt: Any, admin: httpx.AsyncClient
) -> None:
    """A thief who kept the session alive by renewals holds the same session id."""
    me = (await admin.get("/api/auth/me")).json()["user"]
    now = int(time.time())
    sid = _claims(admin)["sid"]
    near_expiry = _forge(rt, me["id"], sid=sid, iat=now - 40000, at=now - 40000, exp=now + 600)
    async with _client(app, cookies={COOKIE: near_expiry}) as thief:
        r = await thief.get("/api/auth/me")
        assert r.status_code == 200
        renewed = r.cookies[COOKIE]
        new = jwt.decode(renewed, options={"verify_signature": False})
        assert new["sid"] == sid and new["at"] == now - 40000  # renewal keeps both
        assert (await admin.post("/api/auth/logout")).status_code == 204
        thief.cookies.set(COOKIE, renewed)
        assert (await thief.get("/api/auth/me")).status_code == 401


async def test_logout_everywhere(
    app: Any, admin: httpx.AsyncClient, second: httpx.AsyncClient
) -> None:
    r = await admin.post("/api/auth/logout", json={"everywhere": True})
    assert r.status_code == 204
    assert (await second.get("/api/auth/me")).status_code == 401
    assert (await admin.get("/api/auth/me")).status_code == 401


async def test_cookie_without_session_id_is_refused(
    app: Any, rt: Any, admin: httpx.AsyncClient
) -> None:
    """A pre-1.4.1 cookie cannot be revoked, so it is not accepted (one sign-in after the
    upgrade)."""
    me = (await admin.get("/api/auth/me")).json()["user"]
    async with _client(app, cookies={COOKIE: _forge(rt, me["id"], sid=None)}) as c:
        assert (await c.get("/api/auth/me")).status_code == 401
    async with _client(app, cookies={COOKIE: _forge(rt, me["id"])}) as c:
        assert (await c.get("/api/auth/me")).status_code == 200


async def test_api_token_is_not_affected_by_logout(
    app: Any, admin: httpx.AsyncClient, feats: Any
) -> None:
    feats(audit=True)
    r = await admin.post("/api/tokens", json={"name": "ci", "role": "viewer"})
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    assert (await admin.post("/api/auth/logout", json={"everywhere": True})).status_code == 204
    async with _client(app, headers={"Authorization": f"Bearer {token}"}) as c:
        assert (await c.get("/api/devices")).status_code == 200


async def test_websocket_of_the_session_is_closed(rt: Any) -> None:
    closed: list[tuple[str, int]] = []

    class Ws:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self, code: int = 1000) -> None:
            closed.append((self.name, code))

        async def send_text(self, _: str) -> None:
            pass

    a = rt.hub.register(Ws("a"), user_id=1, sid="s-a")  # type: ignore[arg-type]
    b = rt.hub.register(Ws("b"), user_id=1, sid="s-b")  # type: ignore[arg-type]
    try:
        assert await rt.hub.close_session("s-a") == 1
        assert closed == [("a", 4401)]
    finally:
        await rt.hub.unregister(a)
        await rt.hub.unregister(b)


# ---------------------------------------------------------------- finding 7: absolute lifetime
async def test_local_session_has_an_absolute_lifetime(
    app: Any, rt: Any, admin: httpx.AsyncClient
) -> None:
    me = (await admin.get("/api/auth/me")).json()["user"]
    now = int(time.time())
    week = 168 * 3600
    c0 = _claims(admin)
    assert c0["sid"] and c0["at"] <= now + 1 and c0["exp"] - now <= 12 * 3600 + 5
    assert (await admin.get("/api/settings")).json()["session_max_age_h"] == 168
    # an active session one hour before its 7th day: renewed, but never past at + 7 d
    at = now - week + 3600
    async with _client(app, cookies={COOKIE: _forge(rt, me["id"], at=at, exp=now + 600)}) as c:
        r = await c.get("/api/auth/me")
        assert r.status_code == 200
        new = jwt.decode(r.cookies[COOKIE], options={"verify_signature": False})
        assert new["at"] == at and new["sid"] == "forged-session"
        assert new["exp"] <= at + week + 5 < now + 12 * 3600
    # past the maximum age: refused although the cookie itself has not expired
    old = _forge(rt, me["id"], at=now - week - 60, exp=now + 3600)
    async with _client(app, cookies={COOKIE: old}) as c:
        r = await c.get("/api/auth/me")
        assert r.status_code == 401 and "expired" in r.json()["error"]["message"]
    # the maximum is a setting
    r = await admin.put("/api/settings", json={"session_max_age_h": 2})
    assert r.status_code == 200 and r.json()["session_max_age_h"] == 2
    assert rt.security.session_max_age_s == 7200
    async with _client(app, cookies={COOKIE: _forge(rt, me["id"], at=now - 3 * 3600)}) as c:
        assert (await c.get("/api/auth/me")).status_code == 401
    async with _client(app) as c:  # a new login is capped by it as well
        assert (await c.post("/api/auth/login", json=LOGIN)).status_code == 200
        assert _claims(c)["exp"] - now <= 7200 + 5
    for bad in (0, -1, 721):
        r = await admin.put("/api/settings", json={"session_max_age_h": bad})
        assert r.status_code == 422, bad


def test_directory_sessions_keep_the_12_hour_rule() -> None:
    pol = security_svc.policy_from({"session_max_age_h": 168})
    assert pol.session_max_s("local") == 168 * 3600
    assert pol.session_max_s("ldap") == pol.session_max_s("oidc") == 12 * 3600
    low = security_svc.policy_from({"session_max_age_h": 4})
    assert low.session_max_s("ldap") == low.session_max_s("local") == 4 * 3600


# ---------------------------------------------------------------- finding 6: password spraying
async def _attempt(c: httpx.AsyncClient, username: str, password: str = "Winter2026!") -> int:
    r = await c.post("/api/auth/login", json={"username": username, "password": password})
    return r.status_code


async def test_pentest_reproduction_password_spraying(app: Any, admin: httpx.AsyncClient) -> None:
    """15 user names x 1 guess (all 401), 12 guesses at victimX (10 x 401, then 429) and then
    victimY from the same address was 401. Now the address itself is throttled."""
    async with _client(app) as attacker:
        assert [await _attempt(attacker, f"user{i}") for i in range(15)] == [401] * 15
        codes = [await _attempt(attacker, "victimX") for _ in range(12)]
        assert codes[:5] == [401] * 5 and codes[-2:] == [429, 429]
        assert await _attempt(attacker, "victimY") == 429
        # also a correct password is refused from that address while it is throttled
        assert await _attempt(attacker, LOGIN["username"], LOGIN["password"]) == 429
    # sessions that already exist are not affected
    assert (await admin.get("/api/auth/me")).status_code == 200


async def test_ip_cap_is_a_setting_and_counts_failures_only(
    app: Any, rt: Any, admin: httpx.AsyncClient
) -> None:
    assert (await admin.get("/api/settings")).json()["login_ip_max_failures"] == 20
    async with _client(app) as office:
        # ordinary use behind one NAT address: successes never count, a few typos are fine
        for _ in range(8):
            assert await _attempt(office, LOGIN["username"], LOGIN["password"]) == 200
        assert [await _attempt(office, f"colleague{i}") for i in range(19)] == [401] * 19
        assert await _attempt(office, LOGIN["username"], LOGIN["password"]) == 200
        # a success does not reset the address counter (one valid account buys nothing)
        assert await _attempt(office, "colleague-x") == 401
        assert await _attempt(office, "colleague-y") == 429
    # 0 turns the address-wide cap off; the per-(address, user) limit stays
    r = await admin.put("/api/settings", json={"login_ip_max_failures": 0})
    assert r.status_code == 200 and rt.security.login_ip_max_failures == 0
    async with _client(app) as c:
        assert [await _attempt(c, f"other{i}") for i in range(25)] == [401] * 25
        assert [await _attempt(c, "one") for _ in range(11)][-1] == 429
    r = await admin.put("/api/settings", json={"login_ip_max_failures": 3})
    app.state.login_limiter.clear()
    async with _client(app) as c:
        assert [await _attempt(c, f"n{i}") for i in range(4)] == [401, 401, 401, 429]
    assert (await admin.put("/api/settings", json={"login_ip_max_failures": -1})).status_code == 422


async def test_ip_cap_uses_the_proxy_address_only_when_trusted(
    app: Any, rt: Any, admin: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await admin.put("/api/settings", json={"login_ip_max_failures": 3})

    async def spray(c: httpx.AsyncClient, tag: str, xff: str) -> list[int]:
        out = []
        for i in range(4):
            r = await c.post(
                "/api/auth/login",
                json={"username": f"{tag}{i}", "password": "x"},
                headers={"X-Forwarded-For": xff},
            )
            out.append(r.status_code)
        return out

    async with _client(app) as c:
        # not trusted: a forged X-Forwarded-For does not give the attacker a fresh counter
        monkeypatch.setattr(rt.settings, "netmon_trust_proxy", False)
        assert await spray(c, "a", "203.0.113.1") == [401, 401, 401, 429]
        assert await spray(c, "b", "203.0.113.2") == [429] * 4
        # trusted proxy: the counter is per forwarded client (the rightmost entry)
        monkeypatch.setattr(rt.settings, "netmon_trust_proxy", True)
        app.state.login_limiter.clear()
        assert await spray(c, "c", "198.51.100.7, 203.0.113.1") == [401, 401, 401, 429]
        assert await spray(c, "d", "203.0.113.1, 203.0.113.9") == [401, 401, 401, 429]
        assert await spray(c, "e", "203.0.113.9, 203.0.113.1") == [429] * 4


async def test_change_password_guessing_is_throttled(app: Any, admin: httpx.AsyncClient) -> None:
    async def guess(current: str) -> int:
        r = await admin.post(
            "/api/auth/change-password",
            json={"current_password": current, "new_password": "N3w-password!"},
        )
        return r.status_code

    assert [await guess(f"wrong-{i}") for i in range(10)] == [401] * 10
    assert await guess("wrong-again") == 429
    assert await guess(ADMIN["password"]) == 429  # also the right one, until the window ends
    app.state.login_limiter.clear()
    assert await guess(ADMIN["password"]) == 204


def test_kerberos_failures_stay_out_of_the_address_counter() -> None:
    limiter = LoginLimiter(ip_max=lambda: 3)
    for _ in range(9):
        limiter.fail("10.0.0.1", "\x00sso", ip_wide=False)
    assert not limiter.ip_blocked("10.0.0.1")
    assert not limiter.blocked("10.0.0.1", "alice")
    for name in ("a", "b", "c"):
        limiter.fail("10.0.0.1", name)
    assert limiter.blocked("10.0.0.1", "alice") and limiter.ip_blocked("10.0.0.1")
    assert not limiter.blocked("10.0.0.1", "\x00sso", ip_wide=False)
    assert not limiter.blocked("10.0.0.2", "alice")


# ---------------------------------------------------------------- finding 8: headers
def _csp(r: httpx.Response) -> dict[str, str]:
    out = {}
    for part in r.headers["content-security-policy"].split(";"):
        name, _, value = part.strip().partition(" ")
        out[name] = value
    return out


async def test_security_headers_on_every_response(app: Any, admin: httpx.AsyncClient) -> None:
    responses = [
        await admin.get("/"),
        await admin.get("/noc/abc"),
        await admin.get("/api/health"),
        await admin.get("/api/devices"),
        await admin.get("/api/nope"),  # 404 envelope
        await admin.post("/api/devices", json={}, headers={"Origin": "https://evil.example"}),
    ]
    assert responses[-1].status_code == 403  # answered by the guard itself
    for r in responses:
        csp = _csp(r)
        assert csp["default-src"] == "'self'"
        assert csp["script-src"].split()[0] == "'self'"
        assert (
            "unsafe-inline" not in csp["script-src"]
            and "unsafe-eval" not in r.headers["content-security-policy"]
        )
        assert csp["style-src"] == "'self'"
        assert csp["object-src"] == "'none'" and csp["base-uri"] == "'self'"
        assert csp["frame-ancestors"] == "'self'"
        assert r.headers["x-frame-options"] == "SAMEORIGIN"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"
        assert "strict-transport-security" not in r.headers  # plain HTTP request
        assert len(r.headers.get_list("content-security-policy")) == 1


async def test_hsts_only_over_https(
    app: Any, rt: Any, admin: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fwd = {"X-Forwarded-Proto": "https"}
    assert "strict-transport-security" not in (await admin.get("/api/health", headers=fwd)).headers
    monkeypatch.setattr(rt.settings, "netmon_trust_proxy", True)
    r = await admin.get("/api/health", headers=fwd)
    assert r.headers["strict-transport-security"] == "max-age=31536000"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        assert "strict-transport-security" in (await c.get("/api/health")).headers


async def test_frame_ancestors_setting(app: Any, rt: Any, admin: httpx.AsyncClient) -> None:
    assert (await admin.get("/api/settings")).json()["frame_ancestors"] == []
    r = await admin.put(
        "/api/settings",
        json={"frame_ancestors": ["https://Wall.example.com/", "https://*.noc.example:8443", " "]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["frame_ancestors"] == ["https://wall.example.com", "https://*.noc.example:8443"]
    r = await admin.get("/noc/abc")
    assert _csp(r)["frame-ancestors"] == (
        "'self' https://wall.example.com https://*.noc.example:8443"
    )
    # X-Frame-Options cannot express an allow-list: omitted, frame-ancestors governs
    assert "x-frame-options" not in r.headers
    for bad in (
        ["*"],
        ["https://a.example; script-src *"],
        ["https://a.example 'unsafe-inline'"],
        ["javascript:alert(1)"],
        ["https://a.example/path"],
        ["'none'"],
    ):
        r = await admin.put("/api/settings", json={"frame_ancestors": bad})
        assert r.status_code == 422, bad
    r = await admin.put("/api/settings", json={"frame_ancestors": []})
    assert "x-frame-options" in (await admin.get("/")).headers


def test_csp_builder_and_inline_script_hashes() -> None:
    html = (
        '<html><head><script>var a = 1;</script><script type="module" src="/assets/x.js">'
        "</script><script>var a = 1;</script><script>\n  </script></head></html>"
    )
    hashes = security_svc.inline_script_hashes(html)
    assert len(hashes) == 1 and hashes[0].startswith("'sha256-") and hashes[0].endswith("'")
    assert hashes[0] == "'sha256-+dZ6udsWxNVoGfScAq7t5IIF5UJb4F6RhjbN6oe1p4w='"
    csp = security_svc.build_csp(security_svc.DEFAULT_POLICY, hashes)
    assert f"script-src 'self' {hashes[0]};" in csp
    assert "unsafe-eval" not in csp and "script-src 'self' 'unsafe-inline'" not in csp
    # a tampered stored value can never inject a directive
    pol = security_svc.policy_from({"frame_ancestors": ["https://ok.example", "x; script-src *"]})
    assert pol.frame_ancestors == ("https://ok.example",)
    docs = security_svc.security_headers(pol, csp=csp, https=False, path="/api/docs")
    sources = docs["Content-Security-Policy"].replace(";", " ").split()
    assert set(sources) >= {SWAGGER_CDN}
    assert docs["X-Frame-Options"] == "DENY"


# ---------------------------------------------------------------- finding 10: OIDC landing page
def test_oidc_landing_page_cannot_be_broken_out_of() -> None:
    """`next` is a same-origin path the attacker chooses: it must not be able to close the
    <script> element of the page shown after Microsoft sign-in."""
    evil = "/x</script><script>alert(document.domain)</script><!--&\"'"
    page, csp = success_page(evil)
    assert page.count("<script>") == 1 and page.count("</script>") == 1
    assert "alert(document.domain)</script>" not in page
    assert "\\u003c/script\\u003e" in page
    body = page.split("<script>")[1].split("</script>")[0]
    assert security_svc.script_hash(body) in csp
    assert "unsafe-inline" not in csp and "default-src 'none'" in csp
    plain, _ = success_page("/devices/5?tab=ports")
    assert 'window.location.replace("/devices/5?tab=ports");' in plain


# ---------------------------------------------------------------- migration 0011
@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_0011_up_down_up(mode: str, rt: Any) -> None:
    import secrets

    import asyncpg
    from alembic import command

    from netmon.db import timescale
    from netmon.db.engine import make_engine
    from netmon.db.migrate import _alembic_config, run_migrations
    from tests.conftest import TEST_DATABASE_URL, _plain, db_url_for

    assert TEST_DATABASE_URL
    name = f"netmon_test_m11_{secrets.token_hex(3)}"
    conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await conn.execute(f'CREATE DATABASE "{name}"')
    await conn.close()
    url = db_url_for(name)
    engine = make_engine(url.replace("postgresql://", "postgresql+asyncpg://"))

    async def columns(c: asyncpg.Connection) -> dict[str, str]:
        rows = await c.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'revoked_sessions'"
        )
        return {r["column_name"]: f"{r['data_type']} {r['is_nullable']}" for r in rows}

    def migrate(action: str, rev: str) -> Any:
        def run(sync_conn: Any) -> None:
            cfg = _alembic_config()
            cfg.attributes["connection"] = sync_conn
            getattr(command, action)(cfg, rev)

        return run

    try:
        await run_migrations(engine)
        await timescale.bootstrap_status(engine, 90, disabled=mode == "fallback")
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == "0012"
            assert await columns(c) == {
                "sid": "character varying NO",
                "user_id": "integer YES",
                "revoked_at": "timestamp with time zone NO",
                "expires_at": "timestamp with time zone NO",
            }
            uid = await c.fetchval(
                "INSERT INTO users (username, password_hash, role, language, is_active, "
                "token_version, source, created_at, updated_at) VALUES ('user141', 'x', 'viewer', "
                "'en', true, 0, 'local', now(), now()) RETURNING id"
            )
            await c.execute(
                "INSERT INTO revoked_sessions (sid, user_id, expires_at) "
                "VALUES ('s1', $1, now() + interval '1 day')",
                uid,
            )
            await c.execute("DELETE FROM users WHERE id = $1", uid)  # cascades
            assert await c.fetchval("SELECT count(*) FROM revoked_sessions") == 0
        finally:
            await c.close()
        async with engine.connect() as ac:
            await ac.run_sync(migrate("downgrade", "0010"))
            await ac.commit()
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == "0010"
            assert await columns(c) == {}
        finally:
            await c.close()
        await run_migrations(engine)  # and up again
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == "0012"
            assert "sid" in await columns(c)
        finally:
            await c.close()
    finally:
        await engine.dispose()
        conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.close()
