"""[4] The 1.4.2 code that needs the secrets-at-rest form (startup synchronisation, `netmon
secrets ...`) runs at revision 0012 **and every later revision of this release** (phase 4 adds
0013+), and stays off on an unmigrated / older database or one migrated by a newer release."""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest

from netmon.db.migrate import (
    SECRETS_AT_REST_REVISION,
    db_revision,
    revision_at_or_after,
    run_migrations,
)
from netmon.runtime import Runtime
from tests.conftest import TEST_DATABASE_URL, _plain, db_url_for, migration_head, requires_db

COMMUNITY = "gate-comm-4411"


def test_revision_at_or_after() -> None:
    head = migration_head()
    assert head != SECRETS_AT_REST_REVISION  # phase 4 adds revisions after 1.4.2's 0012
    assert revision_at_or_after("0012", "0012")
    assert revision_at_or_after(head, "0012")
    assert revision_at_or_after(head, head)
    assert not revision_at_or_after(None, "0012")  # unmigrated
    assert not revision_at_or_after("0011", "0012")  # older
    assert not revision_at_or_after("0001", "0012")
    assert not revision_at_or_after("0012", head)
    assert not revision_at_or_after("0099", "0012")  # migrated by a newer release
    assert not revision_at_or_after("not-a-revision", "0012")


@pytest.fixture
async def fresh_db() -> AsyncIterator[tuple[str, Any]]:
    from netmon.db.engine import make_engine

    assert TEST_DATABASE_URL
    name = f"netmon_test_gate_{secrets.token_hex(3)}"
    admin = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await admin.execute(f'CREATE DATABASE "{name}"')
    url = db_url_for(name)
    engine = make_engine(url.replace("postgresql://", "postgresql+asyncpg://"))
    try:
        yield url, engine
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _device_as_141(url: str) -> int:
    """A device row written the 1.4.1 way (plaintext only, no encrypted copy)."""
    c = await asyncpg.connect(url)
    try:
        gid = await c.fetchval("INSERT INTO groups (name) VALUES ('g') RETURNING id")
        return int(
            await c.fetchval(
                "INSERT INTO devices (group_id, name, host, snmp_credentials) "
                "VALUES ($1, 'd', 'h', $2::jsonb) RETURNING id",
                gid,
                json.dumps({"version": "v2c", "community": COMMUNITY}),
            )
        )
    finally:
        await c.close()


async def _creds(url: str, did: int) -> dict[str, Any]:
    c = await asyncpg.connect(url)
    try:
        return dict(
            json.loads(await c.fetchval("SELECT snmp_credentials FROM devices WHERE id=$1", did))
        )
    finally:
        await c.close()


async def _set_revision(url: str, rev: str) -> None:
    c = await asyncpg.connect(url)
    try:
        await c.execute("UPDATE alembic_version SET version_num = $1", rev)
    finally:
        await c.close()


async def _sync(engine: Any, key: str) -> None:
    await Runtime._secrets_at_rest(SimpleNamespace(engine=engine, secret_key=key))  # type: ignore[arg-type]  # noqa: SLF001


@requires_db
@pytest.mark.parametrize("target", ["0012", "head"])
async def test_startup_sync_runs_at_0012_and_head(target: str, fresh_db: Any) -> None:
    url, engine = fresh_db
    key = secrets.token_urlsafe(64)
    await run_migrations(engine, secret_key=key, target=None if target == "head" else target)
    assert await db_revision(engine) == (migration_head() if target == "head" else "0012")
    did = await _device_as_141(url)
    assert "community_enc" not in await _creds(url, did)
    await _sync(engine, key)
    creds = await _creds(url, did)
    assert creds["community"] == COMMUNITY and creds["community_enc"].startswith("v1:")


@requires_db
@pytest.mark.parametrize("revision", ["0011", "0099"])
async def test_startup_sync_skipped_when_older_or_unknown(revision: str, fresh_db: Any) -> None:
    url, engine = fresh_db
    key = secrets.token_urlsafe(64)
    await run_migrations(engine, target="0011")
    did = await _device_as_141(url)
    if revision != "0011":  # a newer release migrated it
        await _set_revision(url, revision)
    await _sync(engine, key)
    assert await _creds(url, did) == {"version": "v2c", "community": COMMUNITY}


async def _cli(url: str, tmp_path: Any, monkeypatch: Any, key: str) -> int:
    from netmon import cli

    (tmp_path / "secret.key").write_text(key, encoding="utf-8")
    monkeypatch.setenv("NETMON_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", url)
    return await cli._secrets("status")  # noqa: SLF001


@requires_db
@pytest.mark.parametrize("target", ["0012", "head"])
async def test_secrets_cli_works_at_0012_and_head(
    target: str, fresh_db: Any, tmp_path: Any, monkeypatch: Any, capsys: Any
) -> None:
    url, engine = fresh_db
    key = secrets.token_urlsafe(64)
    await run_migrations(engine, secret_key=key, target=None if target == "head" else target)
    assert await _cli(url, tmp_path, monkeypatch, key) == 0
    assert "Plaintext copies: kept" in capsys.readouterr().out


@requires_db
@pytest.mark.parametrize(
    ("revision", "message"),
    [
        (None, "the database is not migrated; this command needs migration 0012 or later"),
        ("0011", "the database is at migration 0011; this command needs migration 0012 or later"),
        ("0099", "migration 0099, which this version does not know"),
    ],
)
async def test_secrets_cli_refuses_older_or_unknown(
    revision: str | None,
    message: str,
    fresh_db: Any,
    tmp_path: Any,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    url, engine = fresh_db
    key = secrets.token_urlsafe(64)
    if revision is not None:
        await run_migrations(engine, target="0011")
        if revision != "0011":
            await _set_revision(url, revision)
    assert await _cli(url, tmp_path, monkeypatch, key) == 1
    assert message in capsys.readouterr().err
