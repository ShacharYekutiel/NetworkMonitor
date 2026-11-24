"""[premium] Signed-in browsers: `GET /api/auth/sessions`, `DELETE /api/auth/sessions/{id}`
(registry `user_sessions`, migration 0014)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import jwt
import pytest

from netmon.db.migrate import migration_head
from netmon.services import sessions as sessions_svc
from tests.conftest import ADMIN, _plain, requires_db

pytestmark = requires_db
COOKIE = "netmon_session"
LOGIN = {"username": ADMIN["username"], "password": ADMIN["password"]}
UA_ADMIN = "Mozilla/5.0 (X11; Linux x86_64) Firefox/140.0"
UA_SECOND = "Mozilla/5.0 (Windows NT 10.0) Chrome/142.0"


def _client(app: Any, **kw: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", **kw
    )


def _sid(c: httpx.AsyncClient) -> str:
    return str(jwt.decode(c.cookies[COOKIE], options={"verify_signature": False})["sid"])


@pytest.fixture
async def second(app: Any, admin: httpx.AsyncClient) -> AsyncIterator[httpx.AsyncClient]:
    """A second browser of the same user."""
    async with _client(app, headers={"User-Agent": UA_SECOND}) as c:
        assert (await c.post("/api/auth/login", json=LOGIN)).status_code == 200
        assert (await c.get("/api/auth/me")).status_code == 200
        yield c


async def _db(rt: Any) -> asyncpg.Connection:
    return await asyncpg.connect(_plain(rt.settings.database_url))


async def _items(c: httpx.AsyncClient) -> list[dict[str, Any]]:
    r = await c.get("/api/auth/sessions")
    assert r.status_code == 200, r.text
    return list(r.json()["items"])


def _forge(rt: Any, user_id: int, **over: Any) -> str:
    now = int(time.time())
    claims = {
        "sub": str(user_id),
        "role": "admin",
        "tv": 0,
        "iat": now,
        "exp": now + 3600,
        "src": "local",
        "at": now - 3600,
        "sid": "pre-0014-session",
        **over,
    }
    return jwt.encode(claims, rt.secret_key, "HS256")


# ---------------------------------------------------------------- list
async def test_list_shows_own_sessions_and_marks_the_current_one(
    app: Any, rt: Any, admin: httpx.AsyncClient, second: httpx.AsyncClient, viewer: Any
) -> None:
    admin.headers["User-Agent"] = UA_ADMIN
    assert (await viewer.get("/api/auth/me")).status_code == 200
    items = await _items(admin)
    assert len(items) == 2  # the viewer's session is not listed
    assert set(items[0]) == {"id", "created_at", "last_seen_at", "ip", "user_agent", "current"}
    current = [i for i in items if i["current"]]
    assert len(current) == 1
    other = next(i for i in items if not i["current"])
    assert other["user_agent"] == UA_SECOND
    assert other["ip"] == "127.0.0.1"
    # newest activity first
    assert items[0]["last_seen_at"] >= items[1]["last_seen_at"]
    for i in items:
        assert i["created_at"].endswith("Z") and i["last_seen_at"].endswith("Z")
    # the id is no credential: unrelated to the cookie and to the session id inside it
    for c, it in ((admin, current[0]), (second, other)):
        assert it["id"] not in c.cookies[COOKIE]
        assert it["id"] != _sid(c) and _sid(c) not in it["id"]
    # the second browser sees the same two sessions, with itself as current
    seen = await _items(second)
    assert {i["id"] for i in seen} == {i["id"] for i in items}
    assert next(i for i in seen if i["current"])["id"] == other["id"]
    # and the viewer only its own
    v = await _items(viewer)
    assert len(v) == 1 and v[0]["current"] and v[0]["id"] not in {i["id"] for i in items}


async def test_list_order_is_newest_activity_first(
    app: Any, rt: Any, admin: httpx.AsyncClient, second: httpx.AsyncClient
) -> None:
    assert (await admin.get("/api/auth/me")).status_code == 200  # registers this browser
    conn = await _db(rt)
    try:
        old = datetime.now(UTC) - timedelta(minutes=30)
        res = await conn.execute(
            "UPDATE user_sessions SET last_seen_at = $1 WHERE sid = $2", old, _sid(admin)
        )
        assert res == "UPDATE 1"
    finally:
        await conn.close()
    items = await _items(admin)  # throttled: the admin's row is not touched again
    assert [i["current"] for i in items] == [False, True]


# ---------------------------------------------------------------- end one session
async def test_ending_another_session_kills_that_cookie_only(
    app: Any, rt: Any, admin: httpx.AsyncClient, second: httpx.AsyncClient, feats: Any
) -> None:
    feats(audit=True)
    target = next(i for i in await _items(admin) if not i["current"])
    second_cookie = second.cookies[COOKIE]
    r = await admin.delete(f"/api/auth/sessions/{target['id']}")
    assert r.status_code == 204, r.text
    assert "set-cookie" not in r.headers  # this browser keeps its cookie
    assert (await second.get("/api/auth/me")).status_code == 401
    async with _client(app, cookies={COOKIE: second_cookie}) as copy:
        assert (await copy.get("/api/devices")).status_code == 401
    assert (await admin.get("/api/auth/me")).status_code == 200
    items = await _items(admin)
    assert len(items) == 1 and items[0]["current"]
    # a second delete of the same id: gone
    assert (await admin.delete(f"/api/auth/sessions/{target['id']}")).status_code == 404
    # audit: auth.logout with the ended session's public id
    conn = await _db(rt)
    try:
        rows = await conn.fetch(
            "SELECT action, actor_type, actor_name, changes::text AS ch FROM audit_log "
            "WHERE action = 'auth.logout'"
        )
    finally:
        await conn.close()
    assert len(rows) == 1
    assert rows[0]["actor_type"] == "user" and rows[0]["actor_name"] == "admin"
    assert target["id"] in rows[0]["ch"] and '"session"' in rows[0]["ch"]
    assert _sid(admin) not in rows[0]["ch"]


async def test_ending_the_current_session_is_a_logout(
    app: Any, rt: Any, admin: httpx.AsyncClient, second: httpx.AsyncClient
) -> None:
    mine = next(i for i in await _items(admin) if i["current"])
    old_cookie = admin.cookies[COOKIE]
    r = await admin.delete(f"/api/auth/sessions/{mine['id']}")
    assert r.status_code == 204
    assert "max-age=0" in r.headers["set-cookie"].lower()
    async with _client(app, cookies={COOKIE: old_cookie}) as copy:
        assert (await copy.get("/api/auth/me")).status_code == 401
    assert _sid(second) not in rt.revoked_sessions
    assert (await second.get("/api/auth/me")).status_code == 200
    assert len(await _items(second)) == 1


async def test_other_users_and_unknown_ids_are_404(
    app: Any, rt: Any, admin: httpx.AsyncClient, viewer: Any
) -> None:
    v = await _items(viewer)
    foreign = await admin.delete(f"/api/auth/sessions/{v[0]['id']}")
    unknown = await admin.delete("/api/auth/sessions/does-not-exist")
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()
    too_long = await admin.delete("/api/auth/sessions/" + "x" * 200)
    assert too_long.status_code == 404
    # the session id itself (the claim) is not accepted as an id either
    assert (await viewer.delete(f"/api/auth/sessions/{_sid(viewer)}")).status_code == 404
    assert (await viewer.get("/api/auth/me")).status_code == 200


# ---------------------------------------------------------------- interplay
async def test_logout_everywhere_still_ends_everything(
    app: Any, rt: Any, admin: httpx.AsyncClient, second: httpx.AsyncClient
) -> None:
    assert len(await _items(admin)) == 2
    r = await admin.post("/api/auth/logout", json={"everywhere": True})
    assert r.status_code == 204
    assert (await second.get("/api/auth/me")).status_code == 401
    assert (await admin.get("/api/auth/me")).status_code == 401
    conn = await _db(rt)
    try:
        assert await conn.fetchval("SELECT count(*) FROM user_sessions") == 0
    finally:
        await conn.close()
    async with _client(app) as c:
        assert (await c.post("/api/auth/login", json=LOGIN)).status_code == 200
        items = await _items(c)
        assert len(items) == 1 and items[0]["current"]


async def test_plain_logout_removes_the_row(
    app: Any, rt: Any, admin: httpx.AsyncClient, second: httpx.AsyncClient
) -> None:
    assert (await second.post("/api/auth/logout")).status_code == 204
    items = await _items(admin)
    assert len(items) == 1 and items[0]["current"]


async def test_password_change_hides_the_other_sessions(
    app: Any, rt: Any, admin: httpx.AsyncClient, second: httpx.AsyncClient
) -> None:
    r = await admin.post(
        "/api/auth/change-password",
        json={"current_password": ADMIN["password"], "new_password": "An0ther-pass!"},
    )
    assert r.status_code == 204, r.text
    assert (await second.get("/api/auth/me")).status_code == 401
    items = await _items(admin)  # the new cookie of this browser only
    assert len(items) == 1 and items[0]["current"]


async def test_session_from_before_the_migration_keeps_working(
    app: Any, rt: Any, admin: httpx.AsyncClient
) -> None:
    """A cookie issued before 0014 has no registry row: it is accepted as before and listed
    from its next request on, with its original sign-in time."""
    me = (await admin.get("/api/auth/me")).json()["user"]
    at = int(time.time()) - 3600
    token = _forge(rt, me["id"], at=at)
    conn = await _db(rt)
    try:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM user_sessions WHERE sid = $1", "pre-0014-session"
            )
            == 0
        )
    finally:
        await conn.close()
    async with _client(app, cookies={COOKIE: token}) as old:
        assert (await old.get("/api/auth/me")).status_code == 200
        items = await _items(old)
        mine = next(i for i in items if i["current"])
        assert mine["created_at"] == datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert len(items) == 2
        # and it can be ended from the other browser like any other session
        assert (await admin.delete(f"/api/auth/sessions/{mine['id']}")).status_code == 204
        assert (await old.get("/api/auth/me")).status_code == 401


async def test_dead_rows_are_not_listed(
    app: Any, rt: Any, admin: httpx.AsyncClient, second: httpx.AsyncClient
) -> None:
    other = next(i for i in await _items(admin) if not i["current"])
    conn = await _db(rt)
    try:
        # idle longer than any cookie can live
        await conn.execute(
            "UPDATE user_sessions SET last_seen_at = now() - interval '13 hours' "
            "WHERE public_id = $1",
            other["id"],
        )
        items = await _items(admin)
        assert [i["current"] for i in items] == [True]
        assert (await admin.delete(f"/api/auth/sessions/{other['id']}")).status_code == 404
        # the current session is listed even when its row looks idle (touch is throttled)
        await conn.execute(
            "UPDATE user_sessions SET last_seen_at = now() - interval '13 hours' WHERE sid = $1",
            _sid(admin),
        )
        assert [i["current"] for i in await _items(admin)] == [True]
        # past the absolute lifetime (session_max_age_h, default 168 h)
        await conn.execute(
            "UPDATE user_sessions SET created_at = now() - interval '200 hours', "
            "last_seen_at = now() WHERE public_id = $1",
            other["id"],
        )
        assert [i["current"] for i in await _items(admin)] == [True]
        # a revoked session id whose row reappeared (a request racing the revocation)
        await conn.execute(
            "UPDATE user_sessions SET created_at = now() WHERE public_id = $1", other["id"]
        )
        assert len(await _items(admin)) == 2
        await conn.execute(
            "INSERT INTO revoked_sessions (sid, user_id, expires_at) "
            "SELECT sid, user_id, now() + interval '1 day' FROM user_sessions "
            "WHERE public_id = $1",
            other["id"],
        )
        assert [i["current"] for i in await _items(admin)] == [True]
    finally:
        await conn.close()


# ---------------------------------------------------------------- API tokens / CSRF
async def test_api_tokens_cannot_list_or_end_sessions(
    app: Any, rt: Any, admin: httpx.AsyncClient, feats: Any
) -> None:
    feats(audit=True)
    r = await admin.post("/api/tokens", json={"name": "ci", "role": "admin"})
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    mine = (await _items(admin))[0]
    async with _client(app, headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.get("/api/auth/sessions")
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
        r = await c.delete(f"/api/auth/sessions/{mine['id']}")
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
        # the same rule as logout (everywhere)
        assert (await c.post("/api/auth/logout", json={"everywhere": True})).status_code == 403
    assert (await admin.get("/api/auth/me")).status_code == 200
    # a bearer request never registers a browser session
    conn = await _db(rt)
    try:
        assert await conn.fetchval("SELECT count(*) FROM user_sessions") == 1
    finally:
        await conn.close()


async def test_cross_origin_delete_is_rejected(
    app: Any, admin: httpx.AsyncClient, second: httpx.AsyncClient
) -> None:
    other = next(i for i in await _items(admin) if not i["current"])
    r = await admin.delete(
        f"/api/auth/sessions/{other['id']}", headers={"Origin": "http://evil.example"}
    )
    assert r.status_code == 403
    assert (await second.get("/api/auth/me")).status_code == 200


async def test_unauthenticated_is_401(app: Any, client: httpx.AsyncClient, admin: Any) -> None:
    async with _client(app) as anon:
        assert (await anon.get("/api/auth/sessions")).status_code == 401
        assert (await anon.delete("/api/auth/sessions/x")).status_code == 401


# ---------------------------------------------------------------- last_seen throttle
async def test_last_seen_is_written_at_most_once_a_minute(
    app: Any, rt: Any, admin: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mono = [1000.0]
    monkeypatch.setattr(rt.session_touch, "clock", lambda: mono[0])
    rt.session_touch.clear()
    writes: list[str] = []
    real = sessions_svc.touch

    async def spy(rt_: Any, seen: Any, **kw: Any) -> bool:
        wrote = await real(rt_, seen, **kw)
        if wrote:
            writes.append(seen.sid)
        return wrote

    monkeypatch.setattr(sessions_svc, "touch", spy)
    conn = await _db(rt)

    async def seen_at() -> datetime:
        v = await conn.fetchval(
            "SELECT last_seen_at FROM user_sessions WHERE sid = $1", _sid(admin)
        )
        assert isinstance(v, datetime)
        return v

    try:
        for _ in range(5):
            assert (await admin.get("/api/auth/me")).status_code == 200
        assert len(writes) == 1
        first = await seen_at()
        rt.clock.set_offset(30)
        mono[0] += 30
        for _ in range(3):
            assert (await admin.get("/api/auth/me")).status_code == 200
        assert len(writes) == 1 and await seen_at() == first
        # a minute later (both clocks): one more write
        rt.clock.set_offset(65)
        mono[0] += 35
        for _ in range(3):
            assert (await admin.get("/api/auth/me")).status_code == 200
        assert len(writes) == 2
        assert await seen_at() > first + timedelta(seconds=60)
        # the SQL rule alone also refuses a write within the minute (another process / a
        # cleared in-memory throttle)
        second = await seen_at()
        rt.session_touch.clear()
        rt.clock.set_offset(80)
        assert (await admin.get("/api/auth/me")).status_code == 200
        assert len(writes) == 2 and await seen_at() == second
    finally:
        await conn.close()


def test_touch_throttle_unit() -> None:
    now = [0.0]
    th = sessions_svc.TouchThrottle(every_s=60, clock=lambda: now[0])
    assert th.due("a")
    th.done("a")
    assert not th.due("a") and th.due("b")
    now[0] = 59.9
    assert not th.due("a")
    now[0] = 60.0
    assert th.due("a")
    th.done("a")
    th.forget("a")
    assert th.due("a")


def test_user_agent_and_ip_are_cleaned() -> None:
    assert sessions_svc.clean_user_agent("x" * 1000) == "x" * sessions_svc.UA_MAX
    assert sessions_svc.clean_user_agent("a\x00b\nc") == "abc"
    assert sessions_svc.clean_user_agent("") is None
    assert sessions_svc.clean_user_agent(None) is None
    assert sessions_svc.clean_ip("unknown") is None
    assert sessions_svc.clean_ip("2001:db8::1") == "2001:db8::1"


async def test_ip_follows_the_trusted_proxy_rule(
    app: Any, rt: Any, admin: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same address the audit log takes: the peer, or the rightmost X-Forwarded-For entry
    only when NETMON_TRUST_PROXY is set."""
    hdr = {"X-Forwarded-For": "203.0.113.9, 198.51.100.7"}
    async with _client(app, headers=hdr) as c:
        assert (await c.post("/api/auth/login", json=LOGIN)).status_code == 200
        mine = next(i for i in await _items(c) if i["current"])
        assert mine["ip"] == "127.0.0.1"
    monkeypatch.setattr(rt.settings, "netmon_trust_proxy", True)
    async with _client(app, headers=hdr) as c:
        assert (await c.post("/api/auth/login", json=LOGIN)).status_code == 200
        mine = next(i for i in await _items(c) if i["current"])
        assert mine["ip"] == "198.51.100.7"


# ---------------------------------------------------------------- migration 0014
@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_0014_up_down_up(mode: str, rt: Any) -> None:
    import secrets

    from alembic import command

    from netmon.db import timescale
    from netmon.db.engine import make_engine
    from netmon.db.migrate import _alembic_config, run_migrations
    from tests.conftest import TEST_DATABASE_URL, db_url_for

    assert TEST_DATABASE_URL
    name = f"netmon_test_m14_{secrets.token_hex(3)}"
    conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await conn.execute(f'CREATE DATABASE "{name}"')
    await conn.close()
    url = db_url_for(name)
    engine = make_engine(url.replace("postgresql://", "postgresql+asyncpg://"))

    async def columns(c: asyncpg.Connection) -> dict[str, str]:
        rows = await c.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'user_sessions'"
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
            assert await c.fetchval("SELECT version_num FROM alembic_version") == migration_head()
            assert await columns(c) == {
                "id": "bigint NO",
                "public_id": "character varying NO",
                "sid": "character varying NO",
                "user_id": "integer NO",
                "token_version": "integer NO",
                "created_at": "timestamp with time zone NO",
                "last_seen_at": "timestamp with time zone YES",
                "ip": "character varying YES",
                "user_agent": "character varying YES",
            }
            uid = await c.fetchval(
                "INSERT INTO users (username, password_hash, role, language, is_active, "
                "token_version, source, created_at, updated_at) VALUES ('user14', 'x', 'viewer', "
                "'en', true, 0, 'local', now(), now()) RETURNING id"
            )
            await c.execute(
                "INSERT INTO user_sessions (public_id, sid, user_id, token_version, created_at) "
                "VALUES ('p1', 's1', $1, 0, now())",
                uid,
            )
            await c.execute("DELETE FROM users WHERE id = $1", uid)  # cascades
            assert await c.fetchval("SELECT count(*) FROM user_sessions") == 0
        finally:
            await c.close()
        async with engine.connect() as ac:
            await ac.run_sync(migrate("downgrade", "0013"))
            await ac.commit()
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == "0013"
            assert await columns(c) == {}
        finally:
            await c.close()
        await run_migrations(engine)  # and up again
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == migration_head()
            assert "public_id" in await columns(c)
        finally:
            await c.close()
    finally:
        await engine.dispose()
        conn = await asyncpg.connect(_plain(TEST_DATABASE_URL))
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.close()
