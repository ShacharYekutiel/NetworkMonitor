"""[1.4.2] Secrets at rest (pentest finding 9, static review item 5): encrypted copies with a
row-bound AAD, the 1.4.1-readable plaintext kept in sync for one release (automatic rollback),
`remove-plaintext` / `restore-plaintext`, re-key, migration 0012 up / down / up, audit."""

from __future__ import annotations

import json
import secrets
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy import text

from netmon.poller.snmp_client import DEFAULT_SNMP, resolve_credentials
from netmon.services import crypto, rekey, secretbox
from netmon.services import settings as settings_svc
from tests.conftest import TEST_DATABASE_URL, _plain, db_url_for, default_group_id, requires_db

pytestmark = requires_db

COMMUNITY = "Comm-7f3a9-secret"
V3_AUTH, V3_PRIV = "authkey-5521x", "privkey-8812y"
BOT = "123456:" + "A" * 35
TEAMS = "https://prod-00.westeurope.logic.azure.com/workflows/abc?sig=TeamsSig0123456789"
HOOK = "https://hooks.example.test/T000/B000/HookToken987654"
HOOK_HDR = {"Authorization": "Bearer HdrToken-31337"}
SMTP_PW = "smtp-pass-0042"
TRAP_C = "trap-comm-77"
TRAP_AUTH, TRAP_PRIV = "trapauth-123", "trappriv-456"
EXTRA_C = "extra-comm-99"
ALL = (COMMUNITY, V3_AUTH, V3_PRIV, BOT, TEAMS, HOOK, "HdrToken-31337", SMTP_PW, TRAP_C,
       TRAP_AUTH, TRAP_PRIV, EXTRA_C)  # fmt: skip


# ---------------------------------------------------------------- the 1.4.1 read paths
# The exact expressions release 1.4.1 uses to read these values (it never decrypts them).
def v141_effective_snmp(device_creds: dict | None, chain: list[tuple[int, Any]]) -> dict:
    """1.4.1 netmon.poller.snmp_client.resolve_credentials."""
    if device_creds is not None:
        return {**DEFAULT_SNMP, **device_creds}
    for _gid, creds in chain:
        if creds is not None:
            return {**DEFAULT_SNMP, **creds}
    return dict(DEFAULT_SNMP)


def v141_trap_user(c: dict) -> dict:
    """1.4.1 netmon.ingest.trap_receiver.snmp_creds_to_user."""
    return {"auth_key": c.get("v3_auth_key"), "priv_key": c.get("v3_priv_key")}


async def _raw(rt: Any, sql: str, **params: Any) -> Any:
    async with rt.sessionmaker() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def _all_secret_storage(rt: Any) -> str:
    """Every column that holds a secret, as one text (to look for plaintext)."""
    parts = []
    for sql in (
        "SELECT coalesce(json_agg(snmp_credentials)::text, '') FROM groups",
        "SELECT coalesce(json_agg(snmp_credentials)::text, '') FROM devices",
        "SELECT coalesce(json_agg(options)::text, '') FROM discovery_profiles",
        "SELECT coalesce(json_agg(credentials)::text, '') FROM discovery_scans",
        "SELECT coalesce(json_agg(config)::text, '') FROM notification_channels",
        "SELECT coalesce(json_agg(value)::text, '') FROM settings",
    ):
        parts.append(await _raw(rt, sql))
    return "\n".join(parts)


async def _populate(admin: httpx.AsyncClient) -> dict[str, int]:
    gid = await default_group_id(admin)
    r = await admin.post(
        "/api/groups",
        json={
            "name": "v3grp",
            "parent_id": gid,
            "snmp_credentials": {
                "version": "v3",
                "v3_username": "mon",
                "v3_security_level": "authPriv",
                "v3_auth_key": V3_AUTH,
                "v3_priv_key": V3_PRIV,
            },
        },
    )
    assert r.status_code == 201, r.text
    g3 = r.json()["id"]
    r = await admin.post(
        "/api/devices",
        json={
            "group_id": gid,
            "name": "sw1",
            "host": "192.0.2.10",
            "snmp_credentials": {"version": "v2c", "community": COMMUNITY},
        },
    )
    assert r.status_code == 201, r.text
    dev = r.json()["id"]
    assert r.json()["snmp_credentials"]["community_set"] is True
    ids = {"group": g3, "device": dev, "root": gid}
    for name, typ, cfg in (
        ("tg", "telegram", {"bot_token": BOT, "chat_id": "-100123"}),
        ("teams", "teams", {"url": TEAMS}),
        ("hook", "webhook", {"url": HOOK, "headers": HOOK_HDR}),
    ):
        r = await admin.post(
            "/api/notification-channels", json={"name": name, "type": typ, "config": cfg}
        )
        assert r.status_code == 201, r.text
        ids[typ] = r.json()["id"]
    r = await admin.put(
        "/api/settings",
        json={
            "smtp": {"host": "mail.example.test", "username": "u", "password": SMTP_PW},
            "trap_communities": [TRAP_C],
            "trap_v3_users": [
                {"username": "tu", "security_level": "authPriv", "auth_key": TRAP_AUTH,
                 "priv_key": TRAP_PRIV}
            ],
        },
    )  # fmt: skip
    assert r.status_code == 200, r.text
    r = await admin.post(
        "/api/discovery/profiles",
        json={
            "name": "lan",
            "targets": ["192.0.2.0/30"],
            "group_id": gid,
            "options": {"extra_communities": [EXTRA_C]},
        },
    )
    assert r.status_code == 201, r.text
    ids["profile"] = r.json()["id"]
    return ids


def _dec(rt: Any, value: str, owner: str, field: str) -> Any:
    return json.loads(crypto.decrypt(rt.secret_key, value, secretbox.aad(owner, field)))


# ---------------------------------------------------------------- 1.4.2 writes, 1.4.1 reads
async def test_writes_keep_141_form_and_add_encrypted_copy(rt: Any, admin: Any) -> None:
    ids = await _populate(admin)
    dev = await _raw(rt, "SELECT snmp_credentials FROM devices WHERE id = :i", i=ids["device"])
    grp = await _raw(rt, "SELECT snmp_credentials FROM groups WHERE id = :i", i=ids["group"])
    # the plaintext 1.4.1 reads is there and in sync with the encrypted copy (row-bound AAD)
    assert v141_effective_snmp(dev, [])["community"] == COMMUNITY
    assert _dec(rt, dev["community_enc"], f"device:{ids['device']}", "community") == COMMUNITY
    assert v141_trap_user(v141_effective_snmp(None, [(ids["group"], grp)])) == {
        "auth_key": V3_AUTH,
        "priv_key": V3_PRIV,
    }
    assert _dec(rt, grp["v3_priv_key_enc"], f"group:{ids['group']}", "v3_priv_key") == V3_PRIV
    for typ, field, want in (
        ("telegram", "bot_token", BOT),
        ("teams", "url", TEAMS),
        ("webhook", "url", HOOK),
        ("webhook", "headers", HOOK_HDR),
    ):
        cfg = await _raw(rt, "SELECT config FROM notification_channels WHERE id = :i", i=ids[typ])
        assert cfg[field] == want  # 1.4.1: config["bot_token"] / config["url"] ...
        assert _dec(rt, cfg[f"{field}_enc"], f"channel:{ids[typ]}", field) == want
    smtp = await _raw(rt, "SELECT value FROM settings WHERE key = 'smtp'")
    assert smtp["password"] == SMTP_PW
    assert _dec(rt, smtp["password_enc"], "settings:smtp", "password") == SMTP_PW
    assert await _raw(rt, "SELECT value FROM settings WHERE key = 'trap_communities'") == [TRAP_C]
    users = await _raw(rt, "SELECT value FROM settings WHERE key = 'trap_v3_users'")
    assert users[0]["auth_key"] == TRAP_AUTH
    assert _dec(rt, users[0]["priv_key_enc"], "settings:trap_v3_users:tu", "priv_key") == TRAP_PRIV
    opts = await _raw(rt, "SELECT options FROM discovery_profiles WHERE id = :i", i=ids["profile"])
    assert opts["extra_communities"] == [EXTRA_C] and opts["extra_communities_enc"]
    # the API never returns a secret or a ciphertext
    for path in (
        f"/api/devices/{ids['device']}",
        f"/api/groups/{ids['group']}",
        "/api/settings",
        f"/api/discovery/profiles/{ids['profile']}",
        f"/api/notification-channels/{ids['telegram']}",
        f"/api/notification-channels/{ids['teams']}",
    ):
        body = (await admin.get(path)).text
        assert "v1:" not in body and "_enc" not in body, path
        assert not [s for s in ALL if s in body], path


async def test_rollback_to_141_and_forward_again(rt: Any, admin: Any) -> None:
    """The exact sequence: 1.4.2 writes -> automatic rollback, 1.4.1 runs on the database
    and edits secrets (its own write shapes) -> 1.4.2 again reads the newer values and
    re-synchronises the encrypted copies at startup."""
    ids = await _populate(admin)
    async with rt.sessionmaker() as session:
        # 1.4.1 merge_snmp: model_dump() of the request, without the *_enc keys
        await session.execute(
            text("UPDATE devices SET snmp_credentials = CAST(:v AS jsonb) WHERE id = :i"),
            {
                "v": json.dumps({"version": "v2c", "community": "changed-by-141"}),
                "i": ids["device"],
            },
        )
        # 1.4.1 PUT /api/settings smtp: dict(current["smtp"]) keeps the stale password_enc
        await session.execute(
            text(
                "UPDATE settings SET value = jsonb_set(value, '{password}', '\"new-141-pw\"') "
                "WHERE key = 'smtp'"
            )
        )
        # 1.4.1 PATCH of a Teams channel: a new config without url_enc
        await session.execute(
            text("UPDATE notification_channels SET config = CAST(:v AS jsonb) WHERE id = :i"),
            {"v": json.dumps({"url": TEAMS + "-141", "ack_link": True}), "i": ids["teams"]},
        )
        await session.commit()
    async with rt.sessionmaker() as session:
        dev = await _raw(rt, "SELECT snmp_credentials FROM devices WHERE id = :i", i=ids["device"])
        creds, _, _ = resolve_credentials(dev, [], ids["device"])
        assert creds["community"] == "changed-by-141"
        assert (await settings_svc.get(session, "smtp"))["password"] == "new-141-pw"
    cfg = await _raw(rt, "SELECT config FROM notification_channels WHERE id = :i", i=ids["teams"])
    assert secretbox.open_channel("teams", ids["teams"], cfg)["url"] == TEAMS + "-141"
    await rt._secrets_at_rest()  # noqa: SLF001 - what every 1.4.2 start does
    dev = await _raw(rt, "SELECT snmp_credentials FROM devices WHERE id = :i", i=ids["device"])
    assert _dec(rt, dev["community_enc"], f"device:{ids['device']}", "community") == (
        "changed-by-141"
    )
    smtp = await _raw(rt, "SELECT value FROM settings WHERE key = 'smtp'")
    assert _dec(rt, smtp["password_enc"], "settings:smtp", "password") == "new-141-pw"
    cfg = await _raw(rt, "SELECT config FROM notification_channels WHERE id = :i", i=ids["teams"])
    assert _dec(rt, cfg["url_enc"], f"channel:{ids['teams']}", "url") == TEAMS + "-141"


async def _form(rt: Any, keep: bool | None) -> dict[str, int]:
    assert rt.engine is not None
    async with rt.engine.begin() as conn:
        return await conn.run_sync(lambda c: secretbox.transform_all(c, rt.secret_key, keep))


async def test_remove_and_restore_plaintext(rt: Any, admin: Any) -> None:
    ids = await _populate(admin)
    try:
        await _form(rt, False)  # `netmon secrets remove-plaintext`
        secretbox.set_plaintext_copy(False)
        stored = await _all_secret_storage(rt)
        assert not [s for s in ALL if s in stored], "plaintext left after remove-plaintext"
        # 1.4.2 reads everything from the encrypted copies
        dev = await _raw(rt, "SELECT snmp_credentials FROM devices WHERE id = :i", i=ids["device"])
        assert resolve_credentials(dev, [], ids["device"])[0]["community"] == COMMUNITY
        async with rt.sessionmaker() as session:
            values = await settings_svc.get_all(session)
        assert values["smtp"]["password"] == SMTP_PW
        assert values["trap_communities"] == [TRAP_C]
        assert values["trap_v3_users"][0]["priv_key"] == TRAP_PRIV
        cfg = await _raw(
            rt, "SELECT config FROM notification_channels WHERE id = :i", i=ids["webhook"]
        )
        assert secretbox.open_channel("webhook", ids["webhook"], cfg)["headers"] == HOOK_HDR
        # new writes stay encrypted-only, and the API still reports the secrets as set
        r = await admin.patch(
            f"/api/devices/{ids['device']}",
            json={"snmp_credentials": {"version": "v2c", "community": COMMUNITY + "2"}},
        )
        assert r.json()["snmp_credentials"]["community_set"] is True
        assert COMMUNITY + "2" not in await _all_secret_storage(rt)
        # `netmon secrets restore-plaintext`, then the 1.4.1 form (0012 downgrade)
        await _form(rt, True)
        secretbox.set_plaintext_copy(True)
        dev = await _raw(rt, "SELECT snmp_credentials FROM devices WHERE id = :i", i=ids["device"])
        assert v141_effective_snmp(dev, [])["community"] == COMMUNITY + "2"
        await _form(rt, None)
        stored = await _all_secret_storage(rt)
        assert "v1:" not in stored and "_sealed" not in stored
        assert all(s in stored for s in (TRAP_C, SMTP_PW, BOT, EXTRA_C, COMMUNITY + "2"))
    finally:
        secretbox.set_plaintext_copy(True)


async def test_ciphertext_bound_to_row_and_key(rt: Any, admin: Any) -> None:
    ids = await _populate(admin)
    r = await admin.post(
        "/api/devices",
        json={"group_id": ids["root"], "name": "sw2", "host": "192.0.2.11",
              "snmp_credentials": {"version": "v2c", "community": "other"}},
    )  # fmt: skip
    other = r.json()["id"]
    try:
        await _form(rt, False)
        secretbox.set_plaintext_copy(False)
        async with rt.sessionmaker() as session:  # copy device 1's ciphertext to device 2
            await session.execute(
                text(
                    "UPDATE devices SET snmp_credentials = (SELECT snmp_credentials FROM devices "
                    "WHERE id = :a) WHERE id = :b"
                ),
                {"a": ids["device"], "b": other},
            )
            await session.commit()
        dev2 = await _raw(rt, "SELECT snmp_credentials FROM devices WHERE id = :i", i=other)
        creds = resolve_credentials(dev2, [], other)[0]
        assert creds["community"] is None  # unreadable: never the copied value, never "public"
        out = (await admin.get(f"/api/devices/{other}")).json()["snmp_credentials"]
        assert out["credentials_unreadable"] is True and out["community_set"] is False
        # another secret.key opens nothing; the 1.4.1 form cannot be produced with it
        wrong = secrets.token_urlsafe(64)
        dev1 = await _raw(rt, "SELECT snmp_credentials FROM devices WHERE id = :i", i=ids["device"])
        assert secretbox.open_with(
            wrong, dev1, secretbox.SNMP_FIELDS, f"device:{ids['device']}"
        ) == (
            {**{k: v for k, v in dev1.items() if not k.endswith("_enc")}, "community": None},
            True,
        )
        assert rt.engine is not None
        async with rt.engine.begin() as conn:
            with pytest.raises(secretbox.SecretsUnreadable):
                await conn.run_sync(lambda c: secretbox.transform_all(c, wrong, None))
    finally:
        secretbox.set_plaintext_copy(True)


async def test_rekey(rt: Any, admin: Any) -> None:
    ids = await _populate(admin)
    old = rt.secret_key
    new = secrets.token_urlsafe(64)
    try:
        await _form(rt, False)
        assert rt.engine is not None
        async with rt.engine.begin() as conn:
            n = await conn.run_sync(lambda c: rekey.rekey_all(c, old, new))
        assert n >= 8
        secretbox.configure(new, plaintext_copy=False)
        dev = await _raw(rt, "SELECT snmp_credentials FROM devices WHERE id = :i", i=ids["device"])
        assert resolve_credentials(dev, [], ids["device"])[0]["community"] == COMMUNITY
        async with rt.sessionmaker() as session:
            assert (await settings_svc.get(session, "smtp"))["password"] == SMTP_PW
        async with rt.engine.begin() as conn:  # back, so the session runtime keeps working
            await conn.run_sync(lambda c: rekey.rekey_all(c, new, old))
    finally:
        secretbox.configure(old, plaintext_copy=True)
    async with rt.engine.begin() as conn:
        with pytest.raises(rekey.RekeyError):
            await conn.run_sync(lambda c: rekey.rekey_all(c, new, old))


async def test_audit_never_holds_secrets(rt: Any, admin: Any) -> None:
    r = await admin.put("/api/settings", json={"feature_audit_enabled": True})
    assert r.status_code == 200
    ids = await _populate(admin)
    await admin.patch(
        f"/api/devices/{ids['device']}",
        json={"snmp_credentials": {"version": "v2c", "community": COMMUNITY + "x"}},
    )
    await admin.patch(
        f"/api/notification-channels/{ids['webhook']}",
        json={"config": {"url": HOOK + "x", "headers": HOOK_HDR}},
    )
    log = await _raw(rt, "SELECT coalesce(json_agg(changes)::text, '') FROM audit_log")
    assert "snmp_credentials.community" in log or "community" in log
    assert not [s for s in ALL if s in log] and "v1:" not in log


# ---------------------------------------------------------------- migration 0012
@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_0012_up_down_up(mode: str, rt: Any, tmp_path: Any) -> None:
    from netmon.db import timescale
    from netmon.db.engine import make_engine
    from netmon.db.migrate import db_revision, run_migrations

    if mode == "timescale" and rt.settings.netmon_disable_timescale:
        pytest.skip("suite runs in forced fallback mode")
    assert TEST_DATABASE_URL
    name = f"netmon_test_m12_{secrets.token_hex(3)}"
    admin = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await admin.execute(f'CREATE DATABASE "{name}"')
    url = db_url_for(name)
    engine = make_engine(url.replace("postgresql://", "postgresql+asyncpg://"))
    key = secrets.token_urlsafe(64)
    try:
        await run_migrations(engine, target="0011")
        enabled = await timescale.bootstrap(engine, 90, disabled=(mode == "fallback"))
        if mode == "timescale" and not enabled:
            pytest.skip("TimescaleDB not available")
        c = await asyncpg.connect(url)
        try:  # a 1.4.1 database: plaintext only, plus an audit row with a leaked community
            gid = await c.fetchval(
                "INSERT INTO groups (name, snmp_credentials) VALUES ('g', $1::jsonb) RETURNING id",
                json.dumps({"version": "v3", "v3_username": "u", "v3_auth_key": V3_AUTH}),
            )
            did = await c.fetchval(
                "INSERT INTO devices (group_id, name, host, snmp_credentials) "
                "VALUES ($1, 'd', 'h', $2::jsonb) RETURNING id",
                gid,
                json.dumps({"version": "v2c", "community": COMMUNITY}),
            )
            cid = await c.fetchval(
                "INSERT INTO notification_channels (name, type, config) "
                "VALUES ('t', 'telegram', $1::jsonb) RETURNING id",
                json.dumps({"bot_token": BOT, "chat_id": "1"}),
            )
            pid = await c.fetchval(
                "INSERT INTO discovery_profiles (name, targets, group_id, options) "
                "VALUES ('p', ARRAY['192.0.2.0/30'], $1, $2::jsonb) RETURNING id",
                gid,
                json.dumps({"extra_communities": [EXTRA_C]}),
            )
            await c.execute(
                "INSERT INTO settings (key, value) VALUES ('smtp', $1::jsonb), "
                "('trap_communities', $2::jsonb), ('trap_v3_users', $3::jsonb)",
                json.dumps({"host": "m", "password": SMTP_PW}),
                json.dumps([TRAP_C]),
                json.dumps([{"username": "tu", "auth_key": TRAP_AUTH}]),
            )
            await c.execute(
                "INSERT INTO audit_log (actor_type, action, entity_type, changes) VALUES "
                "('system', 'discovery_profile.create', 'discovery_profile', $1::jsonb)",
                json.dumps({"options.extra_communities": [None, [EXTRA_C]]}),
            )
            before = {
                "dev": await c.fetchval("SELECT snmp_credentials FROM devices WHERE id=$1", did),
                "ch": await c.fetchval("SELECT config FROM notification_channels WHERE id=$1", cid),
            }
        finally:
            await c.close()

        async def snapshot() -> dict[str, Any]:
            c = await asyncpg.connect(url)
            try:
                return {
                    "dev": json.loads(
                        await c.fetchval("SELECT snmp_credentials FROM devices WHERE id=$1", did)
                    ),
                    "grp": json.loads(
                        await c.fetchval("SELECT snmp_credentials FROM groups WHERE id=$1", gid)
                    ),
                    "ch": json.loads(
                        await c.fetchval(
                            "SELECT config FROM notification_channels WHERE id=$1", cid
                        )
                    ),
                    "opts": json.loads(
                        await c.fetchval("SELECT options FROM discovery_profiles WHERE id=$1", pid)
                    ),
                    "settings": {
                        r["key"]: json.loads(r["value"])
                        for r in await c.fetch("SELECT key, value FROM settings")
                    },
                    "audit": await c.fetchval("SELECT changes::text FROM audit_log"),
                }
            finally:
                await c.close()

        await run_migrations(engine, secret_key=key)  # up
        assert await db_revision(engine) == "0012"
        s = await snapshot()
        assert s["dev"]["community"] == COMMUNITY  # 1.4.1 keeps reading after a rollback
        assert (
            json.loads(
                crypto.decrypt(
                    key, s["dev"]["community_enc"], secretbox.aad(f"device:{did}", "community")
                )
            )
            == COMMUNITY
        )
        assert s["grp"]["v3_auth_key_enc"] and s["ch"]["bot_token_enc"]
        assert s["opts"]["extra_communities_enc"] and s["settings"]["smtp"]["password_enc"]
        assert s["settings"]["trap_v3_users"][0]["auth_key_enc"]
        assert EXTRA_C not in s["audit"] and '"***"' in s["audit"]  # one-way scrub
        await run_migrations(engine, secret_key=key)  # idempotent: nothing rewritten
        assert (await snapshot())["dev"] == s["dev"]
        # remove the plaintext (CLI), then down: the exact 1.4.1 form comes back
        async with engine.begin() as conn:
            await conn.run_sync(lambda sc: secretbox.transform_all(sc, key, False))
            await conn.execute(
                text("INSERT INTO settings (key, value) VALUES (:k, 'true'::jsonb)"),
                {"k": secretbox.STATE_KEY},
            )
        s = await snapshot()
        assert COMMUNITY not in json.dumps(s["dev"]) and TRAP_C not in json.dumps(s["settings"])
        await run_migrations(engine, secret_key=key, target="0011")  # down
        assert await db_revision(engine) == "0011"
        s = await snapshot()
        assert json.loads(before["dev"]) == s["dev"]
        assert json.loads(before["ch"]) == s["ch"]
        assert s["settings"]["trap_communities"] == [TRAP_C]
        assert s["settings"]["smtp"] == {"host": "m", "password": SMTP_PW}
        assert secretbox.STATE_KEY not in s["settings"]
        assert "_enc" not in json.dumps(s) and "_sealed" not in json.dumps(s)
        await run_migrations(engine, secret_key=key)  # up again
        s = await snapshot()
        assert s["dev"]["community"] == COMMUNITY and s["dev"]["community_enc"]
        # a wrong key cannot take a database without plaintext down: nothing changes
        async with engine.begin() as conn:
            await conn.run_sync(lambda sc: secretbox.transform_all(sc, key, False))
        with pytest.raises(Exception, match="cannot be decrypted"):
            await run_migrations(engine, secret_key=secrets.token_urlsafe(64), target="0011")
        assert await db_revision(engine) == "0012"
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
