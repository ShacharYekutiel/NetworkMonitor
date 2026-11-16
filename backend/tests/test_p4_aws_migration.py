"""[4] Migration P4-1 (shared AWS connections) and the legacy mirror (architecture § 15.1.4,
§ 15.1.9 rule R2).

- up / down / up on TimescaleDB and plain PostgreSQL; 1.4.x `assistant` rows in each auth mode
  (access key + session token, Roles Anywhere with `ra_endpoint_url` / `ra_region`, CSR pending,
  host files, default chain) become "Default" with `aad_scope = 'assistant'`, the ciphertexts
  copied verbatim (they decrypt with the same `secret.key` and the 1.4.x AADs); no
  authentication values → no connection; the legacy keys byte-identical; downgrade refusal and
  `NETMON_FORCE_DOWNGRADE=1`.
- the rollback rule: an image of 1.4.x runs on the migrated database (1.4.1's migrate guard),
  so after every write of the new code the 1.4.x reader (`assistant.config.load` + the 1.4.x
  provider logic) still builds working credentials from the legacy keys — "old code reads after
  new code wrote" — until the assistant moves to a hop.

The migration is found by its placeholder (`revision_of("P4-1")`): its number is re-assigned at
merge time."""

from __future__ import annotations

import copy
import json
import secrets
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest

from netmon.assistant import config as acfg
from netmon.aws import connections as conns
from netmon.aws.credentials import (
    DefaultChainProvider,
    RaMaterial,
    RolesAnywhereProvider,
    StaticProvider,
)
from netmon.runtime import Runtime
from netmon.services import crypto
from tests.conftest import (
    TEST_DATABASE_URL,
    _plain,
    db_url_for,
    migration_head,
    requires_db,
    revision_of,
)
from tests.p4_aws import (
    AKID,
    BEDROCK_ROLE,
    MODEL,
    SECRET,
    create_network,
    env_setup,
    feats,
    hop_access,
    pki_file,
    start_mock,
    trust_model,
)

pytestmark = requires_db
SK = "p4-migration-secret-key-000000000000"
TOKEN = "IQoJb3JpZ2luX2VjEJr//////////wEaCXVzLWVhc3QtMSJHMEUCIQ" + "B" * 40


# ---------------------------------------------------------------- 1.4.x rows
def _row14(**fields: Any) -> dict[str, Any]:
    """A 1.4.x `assistant` row as 1.4.x saved it (defaults merged, secrets encrypted with the
    1.4.x AADs)."""
    base = {
        k: v
        for k, v in copy.deepcopy(acfg.DEFAULT_ASSISTANT).items()
        if k not in ("connection_id", "access")
    }
    return acfg.merge(base, fields, SK)


def _cert_material() -> tuple[str, str, str]:
    from cryptography.hazmat.primitives import serialization

    from netmon.aws import certs

    here = Path(__file__).resolve().parents[2] / "tests" / "sim" / "bedrock"
    import sys

    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    import tempfile

    import pki  # type: ignore[import-not-found]

    man = pki.make_pki(tempfile.mkdtemp(prefix="p4-mig-pki-"))
    cert = Path(man["files"]["leaf-rsa.pem"]).read_text()
    key = serialization.load_pem_private_key(Path(man["files"]["leaf-rsa.key"]).read_bytes(), None)
    chain = Path(man["files"]["issuing.pem"]).read_text()
    return cert, chain, certs.key_to_pkcs8_pem(key)


CASES = ["access_key", "roles_anywhere", "csr_pending", "host_files", "default_chain", "none"]


def _case(name: str) -> dict[str, Any]:
    if name == "access_key":
        return _row14(
            auth_mode="access_key",
            access_key_id="ASIA" + "Q" * 16,
            secret_access_key=SECRET,
            session_token=TOKEN,
            model_id=MODEL,
            proxy_mode="custom",
            proxy_url="http://proxy.corp:3128",
            access_key_created_at="2026-01-02",
        )
    if name == "roles_anywhere":
        cert, chain, key = _cert_material()
        row = _row14(
            auth_mode="roles_anywhere",
            region="us-east-1",
            ra_region="il-central-1",
            ra_endpoint_url="https://10.20.0.5",
            model_id=MODEL,
            ra_trust_anchor_arn="arn:aws:rolesanywhere:il-central-1:111122223333:trust-anchor/abc",
            ra_profile_arn="arn:aws:rolesanywhere:il-central-1:111122223333:profile/def",
            ra_role_arn=BEDROCK_ROLE,
            ra_session_seconds=7200,
        )
        row.update(
            ra_cert_pem=cert,
            ra_chain_pem=chain,
            ra_key_enc=crypto.encrypt(SK, key, acfg.AAD_RA_KEY),
            ra_cert_uploaded_at="2026-11-01T10:00:00Z",
        )
        return row
    if name == "csr_pending":
        row = _row14(model_id=MODEL)
        row.update(
            ra_csr_pem="-----BEGIN CERTIFICATE REQUEST-----\nx\n-----END CERTIFICATE "
            "REQUEST-----\n",
            ra_csr_key_enc=crypto.encrypt(SK, "k", acfg.AAD_RA_CSR_KEY),
            ra_csr_subject="CN=netmonitor",
            ra_csr_created_at="2026-11-01T10:00:00Z",
        )
        return row
    if name == "host_files":  # certificate from NETMON_AWS_RA_* files: only the ARNs are stored
        return _row14(
            model_id=MODEL,
            ra_trust_anchor_arn="arn:aws:rolesanywhere:us-east-1:111122223333:trust-anchor/abc",
        )
    if name == "default_chain":
        return _row14(auth_mode="default_chain", model_id=MODEL)
    return _row14(region="eu-west-1", model_id="")  # saved once, never authenticated


async def _db() -> tuple[str, str]:
    name = f"netmon_test_p41_{secrets.token_hex(3)}"
    adm = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await adm.execute(f'CREATE DATABASE "{name}"')
    await adm.close()
    return name, db_url_for(name)


def _alembic(action: str, rev: str) -> Any:
    from alembic import command

    from netmon.db.migrate import _alembic_config

    def run(sync_conn: Any) -> None:
        cfg = _alembic_config()
        cfg.attributes["connection"] = sync_conn
        getattr(command, action)(cfg, rev)

    return run


async def _assistant(c: asyncpg.Connection) -> dict[str, Any]:
    v = await c.fetchval("SELECT value FROM settings WHERE key = 'assistant'")
    return json.loads(v) if isinstance(v, str) else v


@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_p4_1_up_down_up_per_auth_mode(
    mode: str, rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from netmon.db import timescale
    from netmon.db.engine import make_engine

    assert TEST_DATABASE_URL
    if mode == "timescale" and rt.settings.netmon_disable_timescale:
        pytest.skip("suite runs in forced fallback mode")
    p41, before = revision_of("P4-1")
    assert p41 == migration_head()  # the last migration of this branch
    name, url = await _db()
    engine = make_engine(url.replace("postgresql://", "postgresql+asyncpg://"))
    try:
        async with engine.connect() as ac:
            await ac.run_sync(_alembic("upgrade", before))
            await ac.commit()
        enabled = await timescale.bootstrap(engine, 90, disabled=(mode == "fallback"))
        if mode == "timescale" and not enabled:
            pytest.skip("TimescaleDB not available")
        for case in CASES:
            c = await asyncpg.connect(url)
            try:
                legacy = _case(case)
                await c.execute("DELETE FROM settings WHERE key = 'assistant'")
                await c.execute(
                    "INSERT INTO settings (key, value) VALUES ('assistant', $1::jsonb)",
                    json.dumps(legacy),
                )
            finally:
                await c.close()
            async with engine.connect() as ac:
                await ac.run_sync(_alembic("upgrade", p41))
                await ac.commit()
            c = await asyncpg.connect(url)
            try:
                rows = await c.fetch("SELECT * FROM aws_connections")
                after = await _assistant(c)
                # the legacy keys are byte-identical
                assert {k: after.get(k) for k in legacy} == legacy, case
                if case == "none":
                    assert rows == [] and "connection_id" not in after
                else:
                    assert len(rows) == 1, case
                    row = dict(rows[0])
                    settings = json.loads(row["settings"])
                    assert row["name"] == "Default" and row["is_default"] is True
                    assert row["aad_scope"] == "assistant"
                    assert after["connection_id"] == row["id"]
                    assert after["access"] == {
                        "mode": "direct",
                        "role_arn": None,
                        "external_id": None,
                    }
                    assert row["auth_mode"] == (legacy["auth_mode"] or "roles_anywhere")
                    for k in (
                        "secret_access_key_enc",
                        "session_token_enc",
                        "ra_key_enc",
                        "ra_csr_key_enc",
                        "ra_cert_pem",
                        "ra_chain_pem",
                        "ra_csr_pem",
                        "ra_trust_anchor_arn",
                        "access_key_id",
                    ):
                        assert settings.get(k) == legacy.get(k), (case, k)  # verbatim
                    assert settings["ra_region"] is None
                    assert row["region"] == (legacy.get("ra_region") or legacy["region"])
                    assert row["proxy_mode"] == legacy["proxy_mode"]
                    assert row["proxy_url"] == legacy["proxy_url"]
                    v = conns.view(
                        {
                            **row,
                            "settings": settings,
                            "endpoint_overrides": json.loads(row["endpoint_overrides"]),
                        }
                    )
                    if case == "access_key":
                        assert conns.decrypt_field(v, "secret_access_key_enc", SK) == SECRET
                        assert conns.decrypt_field(v, "session_token_enc", SK) == TOKEN
                    if case == "roles_anywhere":
                        assert v["ra_endpoint_url"] == "https://10.20.0.5"
                        assert json.loads(row["endpoint_overrides"]) == [
                            {
                                "service": "rolesanywhere",
                                "region": "il-central-1",
                                "url": "https://10.20.0.5",
                            }
                        ]
                        assert "BEGIN PRIVATE KEY" in (
                            conns.decrypt_field(v, "ra_key_enc", SK) or ""
                        )
                        assert settings["ra_session_seconds"] == 7200
                        # the mirror of this row equals the 1.4.x keys (nothing to write back)
                        lv = conns.legacy_values(
                            {
                                **row,
                                "settings": settings,
                                "endpoint_overrides": json.loads(row["endpoint_overrides"]),
                            }
                        )
                        assert {k: lv[k] for k in conns.LEGACY_KEYS} == {
                            k: legacy.get(k) for k in conns.LEGACY_KEYS
                        }
                    if case == "csr_pending":
                        assert conns.decrypt_field(v, "ra_csr_key_enc", SK) == "k"
            finally:
                await c.close()
            # idempotent re-run of the data step (upgrade again after a downgrade below)
            async with engine.connect() as ac:
                await ac.run_sync(_alembic("downgrade", before))
                await ac.commit()
            c = await asyncpg.connect(url)
            try:
                assert await c.fetchval("SELECT to_regclass('aws_connections')") is None
                assert await _assistant(c) == legacy, case  # exactly the 1.4.x row again
            finally:
                await c.close()
        # downgrade refusal: another connection, or the assistant on a hop
        async with engine.connect() as ac:
            await ac.run_sync(_alembic("upgrade", p41))
            await ac.commit()
        c = await asyncpg.connect(url)
        try:
            await c.execute(
                "INSERT INTO aws_connections (name, auth_mode, region, aad_scope) VALUES "
                "('Other', 'default_chain', 'us-east-1', 'aws.conn.0123456789abcdef')"
            )
        finally:
            await c.close()
        monkeypatch.delenv("NETMON_FORCE_DOWNGRADE", raising=False)
        with pytest.raises(RuntimeError, match="P4-1 downgrade refused"):
            async with engine.connect() as ac:
                await ac.run_sync(_alembic("downgrade", before))
        c = await asyncpg.connect(url)
        try:
            await c.execute("DELETE FROM aws_connections WHERE name = 'Other'")
            await c.execute(
                "UPDATE settings SET value = jsonb_set(value, '{access}', $1::jsonb) "
                "WHERE key = 'assistant'",
                json.dumps(hop_access()),
            )
        finally:
            await c.close()
        with pytest.raises(RuntimeError, match="P4-1 downgrade refused"):
            async with engine.connect() as ac:
                await ac.run_sync(_alembic("downgrade", before))
        monkeypatch.setenv("NETMON_FORCE_DOWNGRADE", "1")
        async with engine.connect() as ac:
            await ac.run_sync(_alembic("downgrade", before))
            await ac.commit()
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == before
            assert "access" not in await _assistant(c)
        finally:
            await c.close()
        monkeypatch.delenv("NETMON_FORCE_DOWNGRADE")
        from netmon.db.migrate import run_migrations

        await run_migrations(engine)  # and up again
        c = await asyncpg.connect(url)
        try:
            assert await c.fetchval("SELECT version_num FROM alembic_version") == migration_head()
        finally:
            await c.close()
    finally:
        await engine.dispose()
        adm = await asyncpg.connect(_plain(TEST_DATABASE_URL))
        await adm.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await adm.close()


# ---------------------------------------------------------------- the legacy mirror (R2)
@pytest.fixture(scope="module")
def mock(tmp_path_factory: pytest.TempPathFactory) -> Any:
    m = start_mock(tmp_path_factory)
    yield m
    m.stop()


@pytest.fixture
async def env(rt: Runtime, admin: httpx.AsyncClient, mock: Any, monkeypatch: pytest.MonkeyPatch):
    await env_setup(rt, mock, monkeypatch)
    yield admin
    await feats(admin, assistant=False)


async def _upgraded_install(rt: Runtime, mock: Any) -> int:
    """The state P4-1 leaves on an upgraded 1.4.x install with an access-key assistant: the
    "Default" row with `aad_scope = 'assistant'` and the 1.4.x keys in the `assistant` row."""
    from netmon.services import settings as settings_svc

    legacy = acfg.merge(
        {
            k: v
            for k, v in copy.deepcopy(acfg.DEFAULT_ASSISTANT).items()
            if k not in ("connection_id", "access")
        },
        {
            "auth_mode": "access_key",
            "access_key_id": AKID,
            "secret_access_key": SECRET,
            "region": "us-east-1",
            "model_id": MODEL,
            "endpoint_url": mock.url,
            "control_endpoint_url": mock.url,
            "proxy_mode": "direct",
        },
        rt.secret_key,
    )
    async with rt.sessionmaker() as s:
        row = await conns.insert(
            s,
            {
                "name": "Default",
                "auth_mode": "access_key",
                "region": "us-east-1",
                "proxy_mode": "direct",
                "proxy_url": None,
                "endpoint_overrides": [
                    {"service": "sts", "region": "us-east-1", "url": mock.url},
                    {"service": "rolesanywhere", "region": "us-east-1", "url": mock.url},
                ],
                "settings": {
                    **conns.SETTINGS_DEFAULTS,
                    **{k: legacy.get(k) for k in conns.SETTINGS_DEFAULTS},
                },
                "aad_scope": "assistant",
                "last_test": None,
            },
        )
        legacy.update(
            connection_id=row["id"],
            access={"mode": "direct", "role_arn": None, "external_id": None},
        )
        await settings_svc.put(s, "assistant", legacy)
        await conns.sync_legacy(s)  # the overrides add ra_endpoint_url / ra_region
        await s.commit()
    await rt.assistant.reload()
    return int(row["id"])


async def legacy_client_call(rt: Runtime, mock: Any) -> dict[str, Any]:
    """The 1.4.x reader: `assistant.config.load` + the 1.4.x `make_provider` logic (secrets
    with the 1.4.x AADs, the Roles Anywhere material from the legacy keys), then one Bedrock
    call through the legacy proxy settings. Returns the 1.4.x object."""
    from netmon.assistant.bedrock import BedrockClient
    from netmon.aws import certs
    from netmon.aws import transport as tp
    from netmon.aws.credentials import CachingProvider

    async with rt.sessionmaker() as s:
        cfg = await acfg.load(s)
    sk = rt.secret_key
    http = tp.make_client(cfg)
    try:
        mode = cfg["auth_mode"]
        if mode == "access_key":
            provider: Any = StaticProvider(
                cfg["access_key_id"],
                acfg.secret_access_key(cfg, sk) or "",
                acfg.session_token(cfg, sk),
            )
        elif mode == "roles_anywhere":
            found = certs.load_pem_certs(
                (cfg["ra_cert_pem"] + "\n" + (cfg.get("ra_chain_pem") or "")).encode()
            )
            key = certs.key_from_pkcs8_pem(
                acfg.decrypt_opt(sk, cfg["ra_key_enc"], acfg.AAD_RA_KEY) or ""
            )
            mat = RaMaterial(
                certs.der(found[0]),
                found[0].serial_number,
                [certs.der(x) for x in certs.order_chain(found[0], found[1:])],
                key,
            )
            provider = RolesAnywhereProvider(cfg, lambda: mat, http)
        else:
            provider = DefaultChainProvider()
        client = BedrockClient(cfg, CachingProvider(provider), http)  # as 1.4.x built it
        out = await client.converse(
            {
                "messages": [{"role": "user", "content": [{"text": "x"}]}],
                "inferenceConfig": {"maxTokens": 20},
            }
        )
        assert out["stopReason"]
    finally:
        await http.aclose()
    return cfg


async def test_old_code_reads_after_new_code_wrote(
    rt: Runtime, env: httpx.AsyncClient, mock: Any
) -> None:
    c = env
    await feats(c, audit=True)
    cid = await _upgraded_install(rt, mock)
    cfg = await legacy_client_call(rt, mock)
    assert cfg["auth_mode"] == "access_key"
    # the assistant (new code) works on the migrated connection with zero admin action
    r = await c.post("/api/assistant/test", json={})
    assert r.json()["ok"], r.json()
    # new code changes the secret and the proxy -> the 1.4.x keys follow (same transaction)
    other = "AKIAMOCKSTATIC000002"
    from tests.p4_aws import STATIC_KEYS

    r = await c.patch(
        f"/api/aws/connections/{cid}",
        json={"access_key_id": other, "secret_access_key": STATIC_KEYS[other], "proxy_mode": "env"},
    )
    assert r.status_code == 200, r.text
    async with rt.sessionmaker() as s:
        row = await conns.get_row(s, cid)
        legacy = await conns.load_assistant_row(s)
    assert row and legacy
    assert legacy["access_key_id"] == other and legacy["proxy_mode"] == "env"
    assert legacy["secret_access_key_enc"] == row["settings"]["secret_access_key_enc"]  # verbatim
    await c.patch(f"/api/aws/connections/{cid}", json={"proxy_mode": "direct"})
    cfg = await legacy_client_call(rt, mock)
    assert cfg["access_key_id"] == other
    # switch the connection to Roles Anywhere: certificate upload and CSR are mirrored too
    r = await c.patch(
        f"/api/aws/connections/{cid}",
        json={
            "auth_mode": "roles_anywhere",
            "ra_trust_anchor_arn": "arn:aws:rolesanywhere:us-east-1:111122223333:"
            "trust-anchor/7a1b2c3d-0000-4000-8000-000000000001",
            "ra_profile_arn": "arn:aws:rolesanywhere:us-east-1:111122223333:"
            "profile/7a1b2c3d-0000-4000-8000-000000000002",
            "ra_role_arn": BEDROCK_ROLE,
        },
    )
    assert r.status_code == 200, r.text
    r = await c.post(f"/api/aws/connections/{cid}/cert/csr", json={"common_name": "netmon-x"})
    assert r.status_code == 200
    async with rt.sessionmaker() as s:
        legacy = await conns.load_assistant_row(s)
    assert legacy and legacy["ra_csr_pem"] == r.json()["pem"] and legacy["ra_csr_key_enc"]
    assert acfg.decrypt_opt(rt.secret_key, legacy["ra_csr_key_enc"], acfg.AAD_RA_CSR_KEY)
    r = await c.post(
        f"/api/aws/connections/{cid}/cert",
        files={
            "file": ("c.pem", pki_file(mock, "leaf-rsa.pem")),
            "key": ("c.key", pki_file(mock, "leaf-rsa.key")),
        },
    )
    assert r.status_code == 200, r.text
    cfg = await legacy_client_call(rt, mock)  # 1.4.x CreateSession with the mirrored key
    assert cfg["auth_mode"] == "roles_anywhere" and cfg["ra_endpoint_url"] == mock.url
    assert cfg["ra_region"] == "us-east-1"
    # a certificate copied INTO the migrated connection is re-encrypted with the 1.4.x AAD
    n = await create_network(c, mock)
    r = await c.post(f"/api/aws/connections/{cid}/cert/copy", json={"from_connection_id": n["id"]})
    assert r.status_code == 200
    await legacy_client_call(rt, mock)
    async with rt.sessionmaker() as s:
        frozen = await conns.load_assistant_row(s)
    # the assistant moves to a hop: mirroring stops, the legacy keys keep the last state
    trust_model(mock)
    good = {
        "region": "us-east-1",
        "model_id": MODEL,
        "endpoint_url": mock.url,
        "control_endpoint_url": mock.url,
        "connection_id": n["id"],
        "access": hop_access(),
    }
    r = await c.put("/api/assistant/settings", json=good)
    assert r.status_code == 200, r.text
    await c.patch(f"/api/aws/connections/{cid}", json={"proxy_mode": "env"})
    async with rt.sessionmaker() as s:
        now = await conns.load_assistant_row(s)
    assert now and frozen
    assert {k: now.get(k) for k in conns.LEGACY_KEYS} == {
        k: frozen.get(k) for k in conns.LEGACY_KEYS
    }
    await c.patch(f"/api/aws/connections/{cid}", json={"proxy_mode": "direct"})
    # back to "Default" / direct: mirroring resumes on the next save
    r = await c.put(
        "/api/assistant/settings", json={"connection_id": cid, "access": {"mode": "direct"}}
    )
    assert r.status_code == 200
    await legacy_client_call(rt, mock)
