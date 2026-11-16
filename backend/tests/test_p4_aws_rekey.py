"""[4] `netmon secrets rekey` (1.4.2, netmon.services.rekey) re-encrypts the secrets of the
shared AWS connections (`aws_connections.settings`, AAD `<aad_scope>.<field>`) together with
everything else; without it a re-key left every connection unreadable."""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest
from sqlalchemy import text

from netmon.assistant import config as acfg
from netmon.aws import connections as conns
from netmon.services import crypto, rekey
from tests.conftest import requires_db

pytestmark = requires_db

PLAIN = {
    "secret_access_key_enc": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "session_token_enc": "FwoGZXIvYXdzEXAMPLETOKEN",
    "ra_key_enc": "-----BEGIN PRIVATE KEY-----\nMIIEXAMPLE\n-----END PRIVATE KEY-----\n",
    "ra_csr_key_enc": "-----BEGIN PRIVATE KEY-----\nMIICSREXAMPLE\n-----END PRIVATE KEY-----\n",
}


async def _insert(rt: Any, name: str, scope: str, is_default: bool) -> int:
    settings = {f: conns.encrypt_field(scope, f, v, rt.secret_key) for f, v in PLAIN.items()}
    settings["access_key_id"] = "AKIAEXAMPLE000000001"
    async with rt.engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO aws_connections (name, is_default, auth_mode, region, "
                        "settings, aad_scope) VALUES (:n, :d, 'access_key', 'us-east-1', "
                        "CAST(:s AS jsonb), :a) RETURNING id"
                    ),
                    {"n": name, "d": is_default, "s": json.dumps(settings), "a": scope},
                )
            ).scalar_one()
        )


async def _view(rt: Any, cid: int) -> dict[str, Any]:
    async with rt.engine.connect() as conn:
        r = (
            await conn.execute(
                text("SELECT aad_scope, settings FROM aws_connections WHERE id = :i"), {"i": cid}
            )
        ).one()
    return {**r.settings, "_aad_scope": r.aad_scope}


async def _rekey(rt: Any, old: str, new: str) -> int:
    async with rt.engine.begin() as conn:
        return int(await conn.run_sync(lambda c: rekey.rekey_all(c, old, new)))


async def test_rekey_includes_aws_connections(rt: Any, clean: Any) -> None:
    default_id = await _insert(rt, "Default", conns.LEGACY_SCOPE, True)
    other_id = await _insert(rt, "Network account", conns.new_scope(), False)
    # the 1.4.x assistant keys mirrored from "Default" (verbatim ciphertexts, same AAD)
    mirrored = {k: v for k, v in (await _view(rt, default_id)).items() if k in PLAIN}
    async with rt.engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO settings (key, value) VALUES ('assistant', CAST(:v AS jsonb))"),
            {"v": json.dumps({"auth_mode": "access_key", **mirrored})},
        )
    old, new = rt.secret_key, secrets.token_urlsafe(64)
    before = {cid: await _view(rt, cid) for cid in (default_id, other_id)}
    try:
        assert await _rekey(rt, old, new) >= 3  # assistant row + both connections
        for cid in (default_id, other_id):
            v = await _view(rt, cid)
            for field, plain in PLAIN.items():
                assert v[field] != before[cid][field]
                assert conns.decrypt_field(v, field, new) == plain
                assert conns.unreadable(v, field, old)
            assert v["access_key_id"] == "AKIAEXAMPLE000000001"
        async with rt.engine.connect() as conn:
            asst = (
                await conn.execute(text("SELECT value FROM settings WHERE key = 'assistant'"))
            ).scalar_one()
        assert (
            acfg.decrypt_opt(new, asst["secret_access_key_enc"], acfg.AAD_SECRET_ACCESS_KEY)
            == PLAIN["secret_access_key_enc"]
        )
        # the legacy mirror stays verbatim: the same ciphertexts as "Default"
        default_after = await _view(rt, default_id)
        assert all(asst[k] == default_after[k] for k in PLAIN)
    finally:  # back, so the session runtime keeps working
        await _rekey(rt, new, old)
    for cid in (default_id, other_id):
        assert conns.decrypt_field(await _view(rt, cid), "ra_key_enc", old) == PLAIN["ra_key_enc"]


async def test_rekey_aborts_on_unreadable_connection_secret(rt: Any, clean: Any) -> None:
    cid = await _insert(rt, "Keys", conns.new_scope(), True)
    async with rt.engine.begin() as conn:  # a value the current key cannot open
        await conn.execute(
            text(
                "UPDATE aws_connections SET settings = jsonb_set(settings, "
                "'{secret_access_key_enc}', to_jsonb(CAST(:v AS text))) WHERE id = :i"
            ),
            {"v": crypto.encrypt(secrets.token_urlsafe(64), "x", b"other"), "i": cid},
        )
    before = await _view(rt, cid)
    with pytest.raises(rekey.RekeyError, match=f"aws_connection:{cid}"):
        await _rekey(rt, rt.secret_key, secrets.token_urlsafe(64))
    assert await _view(rt, cid) == before  # nothing changed


async def test_rekey_at_0012_without_aws_connections(fresh_db: Any) -> None:
    """A database at 1.4.2's 0012 (e.g. after `netmon migrate --to 0012`) has no
    `aws_connections` table: the re-key skips it."""
    from netmon.db.migrate import run_migrations

    _, engine = fresh_db
    key = secrets.token_urlsafe(64)
    await run_migrations(engine, secret_key=key, target="0012")
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO settings (key, value) VALUES ('oidc', CAST(:v AS jsonb))"),
            {
                "v": json.dumps(
                    {"client_secret_enc": crypto.encrypt(key, "s", b"oidc.client_secret")}
                )
            },
        )
        assert await conn.run_sync(lambda c: rekey.rekey_all(c, key, key + "x")) == 1
