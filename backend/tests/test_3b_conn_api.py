"""[3b] Connection layer against the Bedrock mock (tests/sim/bedrock): feature registration and
dependencies, settings + secrets, Test connection and the configure-first `test_id`, access
key / Roles Anywhere (upload, PKCS#12, CSR) / default chain (IMDSv2), error mapping and the
health state, usage caps, Knowledge Base, List models, access control, migration 0008."""

from __future__ import annotations

import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from netmon.assistant.errors import AssistantError
from netmon.runtime import Runtime
from netmon.services.features import FEATURES, PHASE3B_FEATURES
from tests.conftest import TEST_DATABASE_URL, _plain, db_url_for, requires_db

pytestmark = requires_db

SIM = Path(__file__).resolve().parents[2] / "tests" / "sim" / "bedrock"
if str(SIM) not in sys.path:
    sys.path.insert(0, str(SIM))

from mock_bedrock import STATIC_KEYS, MockBedrock  # noqa: E402

MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
AKID, SECRET = next(iter(STATIC_KEYS.items()))


@pytest.fixture(scope="module")
def mock(tmp_path_factory: pytest.TempPathFactory) -> Any:
    m = MockBedrock(pki_dir=str(tmp_path_factory.mktemp("bedrock-pki")))
    m.start()
    yield m
    m.stop()


@pytest.fixture
async def env(rt: Runtime, admin: httpx.AsyncClient, mock: Any, monkeypatch: pytest.MonkeyPatch):
    mock.reset()
    monkeypatch.setattr(rt.settings, "netmon_test_mode", True)
    # the host's AWS_* variables (CI runners, developer shells) must not leak into the
    # default-chain tests: clear every one of them
    for k in [k for k in os.environ if k.startswith("AWS_")]:
        monkeypatch.delenv(k, raising=False)
    for k in ("NETMON_AWS_RA_CERT_FILE", "NETMON_AWS_RA_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    svc = rt.assistant
    svc.tests.clear()
    svc.state_error = None
    await svc.load()
    yield admin
    svc.tests.clear()


def conn(mock: Any, mode: str = "access_key", **kw: Any) -> dict[str, Any]:
    s: dict[str, Any] = {
        "auth_mode": mode,
        "region": "us-east-1",
        "model_id": MODEL,
        "endpoint_url": mock.url,
        "control_endpoint_url": mock.url,
        "proxy_mode": "direct",
    }
    if mode == "access_key":
        s.update(access_key_id=AKID, secret_access_key=SECRET)
    if mode == "roles_anywhere":
        s.update(
            ra_endpoint_url=mock.url,
            ra_trust_anchor_arn=MockBedrock.ra_arns["trust_anchor"],
            ra_profile_arn=MockBedrock.ra_arns["profile"],
            ra_role_arn=MockBedrock.ra_arns["role"],
        )
    s.update(kw)
    return s


async def feats(c: httpx.AsyncClient, **kw: bool) -> dict[str, Any]:
    r = await c.put("/api/settings", json={f"feature_{k}_enabled": v for k, v in kw.items()})
    assert r.status_code == 200, r.text
    return r.json()


async def enable(c: httpx.AsyncClient, settings: dict[str, Any]) -> dict[str, Any]:
    await feats(c, audit=True)
    r = await c.post("/api/assistant/test", json={"settings": settings})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"], res
    r = await c.put(
        "/api/assistant/settings", json={**settings, "enabled": True, "test_id": res["test_id"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True
    return res


def upload(c: httpx.AsyncClient, files: dict[str, Any]) -> Any:
    return c.post("/api/assistant/cert", files=files)


def pki_file(mock: Any, name: str) -> bytes:
    return Path(mock.pki["files"][name]).read_bytes()


# ---------------------------------------------------------------- registration / toggles
async def test_feature_registration_and_dependencies(rt: Runtime, env: httpx.AsyncClient) -> None:
    c = env
    assert (
        len(FEATURES) == 28 and PHASE3B_FEATURES[0] == "assistant" and len(PHASE3B_FEATURES) == 11
    )
    pub = (await c.get("/api/settings/public")).json()["features"]
    assert set(PHASE3B_FEATURES) <= set(pub) and not any(pub[n] for n in PHASE3B_FEATURES)
    s = (await c.get("/api/settings")).json()
    assert "assistant" not in s and "assistant_kb" not in s
    assert s["assistant_daily_questions_per_user"] == 50 and s["assistant_monthly_cost_cap"] is None
    assert "assistant_audit_question_text" not in s  # owner decision: no question text in audit
    for name in ("assistant", "ai_knowledge"):
        r = await c.put("/api/settings", json={f"feature_{name}_enabled": True})
        assert r.status_code == 422 and r.json()["error"]["code"] == "assistant_test_required"
    out = await feats(c, ai_explain_alert=True, ai_config_diff=True)  # ON but idle
    assert out["feature_ai_explain_alert_enabled"] is True
    r = await c.put(
        "/api/settings",
        json={"assistant_monthly_cost_cap": 25.5, "digest_time": "06:41", "digest_language": "he"},
    )
    assert r.status_code == 200 and r.json()["assistant_monthly_cost_cap"] == 25.5
    r = await c.put("/api/settings", json={"assistant_monthly_cost_cap": None})
    assert r.json()["assistant_monthly_cost_cap"] is None and r.json()["digest_language"] == "he"
    for bad in (
        {"digest_time": "25:00"},
        {"assistant_max_model_calls": 1},
        {"assistant_monthly_token_cap": 10},
    ):
        assert (await c.put("/api/settings", json=bad)).status_code == 422, bad
    # AI feature status while the assistant is OFF: the status endpoint answers 409
    r = await c.get("/api/assistant/status")
    assert r.status_code == 409 and r.json()["error"]["code"] == "feature_disabled"
    from netmon.api.errors import ApiError
    from netmon.assistant.gates import require_ai

    with pytest.raises(ApiError) as e:
        require_ai(rt, "ai_explain_alert")
    assert e.value.code == "feature_idle" and e.value.details[0]["needs"] == ["assistant"]
    assert (await c.get("/api/health")).json()["assistant"] == "disabled"


async def test_access_control(app: Any, env: httpx.AsyncClient, viewer: httpx.AsyncClient) -> None:
    assert (await viewer.get("/api/assistant/settings")).status_code == 403
    assert (await viewer.get("/api/assistant/usage")).status_code == 403
    r = await env.post("/api/tokens", json={"name": "script", "role": "admin"})
    assert r.status_code == 201, r.text
    tok = r.json()["token"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {tok}"},
    ) as t:
        for path in ("/api/assistant/settings", "/api/assistant/usage", "/api/incidents"):
            r = await t.get(path)
            assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden", path


# ---------------------------------------------------------------- settings / test / enable
async def test_settings_secrets_and_profiles(env: httpx.AsyncClient, mock: Any) -> None:
    c = env
    s = (await c.get("/api/assistant/settings")).json()
    assert s["enabled"] is False and s["state"] == "disabled" and s["region"] == "us-east-1"
    assert s["recommended"] == {"region": "us-east-1", "profile_prefix": "us"}
    assert {p["scope"] for p in s["profile_scopes"]} >= {"global", "geo", "in_region"}
    r = await c.put("/api/assistant/settings", json=conn(mock))
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["secret_access_key"] is None and s["secret_access_key_set"] is True
    assert not any(k.endswith("_enc") for k in s) and SECRET not in r.text
    assert s["profile"]["scope"] == "geo" and s["profile"]["geography"] == "us"
    # owner option: il-central-1 + global. profile -> warning, not refused
    r = await c.put(
        "/api/assistant/settings",
        json={
            "region": "il-central-1",
            "model_id": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        },
    )
    assert r.status_code == 200 and "global_inference_profile" in r.json()["warnings"]
    assert r.json()["profile"]["scope"] == "global"
    # validation
    for bad in (
        {"region": "Europe"},
        {"access_key_id": "nope"},
        {"ra_session_seconds": 10},
        {"endpoint_url": "ftp://x"},
        {"internal_cidrs": ["10.0.0.0/33"]},
        {"bogus": 1},
    ):
        assert (await c.put("/api/assistant/settings", json=bad)).status_code == 422, bad
    r = await c.put("/api/assistant/settings", json={"secret_access_key": ""})
    assert r.json()["secret_access_key_set"] is False
    rows = (await c.get("/api/audit", params={"action": "assistant.settings_update"})).json()
    assert SECRET not in str(rows)


async def test_access_key_test_and_enable(
    rt: Runtime, app: Any, env: httpx.AsyncClient, mock: Any
) -> None:
    c = env
    settings = conn(mock)
    r = await c.post("/api/assistant/test", json={"settings": settings})
    res = r.json()
    assert res["ok"] and res["test_id"] and res["via"] == "direct", res
    assert [s["step"] for s in res["steps"]] == ["config", "credentials", "endpoint", "converse"]
    assert all(s["ok"] for s in res["steps"]) and res["usage"]["input_tokens"] > 0
    log = mock.requests("converse_stream")
    assert log and log[-1]["sig"]["ok"] and log[-1]["forced_tool"] == "ping"
    # nothing was saved by the test; enabling with audit OFF -> 422 feature_dependency
    assert (await c.get("/api/assistant/settings")).json()["secret_access_key_set"] is False
    r = await c.put(
        "/api/assistant/settings", json={**settings, "enabled": True, "test_id": res["test_id"]}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "feature_dependency"
    await feats(c, audit=True)
    r = await c.put("/api/assistant/settings", json={**settings, "enabled": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "assistant_test_required"
    # a test_id of another admin is refused
    r = await c.post(
        "/api/users", json={"username": "admin2", "password": "Adm1n-pass-2", "role": "admin"}
    )
    assert r.status_code == 201, r.text
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c2:
        await c2.post("/api/auth/login", json={"username": "admin2", "password": "Adm1n-pass-2"})
        other = (await c2.post("/api/assistant/test", json={"settings": settings})).json()
        assert other["ok"]
    r = await c.put(
        "/api/assistant/settings", json={**settings, "enabled": True, "test_id": other["test_id"]}
    )
    assert r.status_code == 422
    # a test of other values does not unlock these
    r = await c.put(
        "/api/assistant/settings",
        json={**settings, "region": "us-west-2", "enabled": True, "test_id": res["test_id"]},
    )
    assert r.status_code == 422
    r = await c.put(
        "/api/assistant/settings", json={**settings, "enabled": True, "test_id": res["test_id"]}
    )
    assert r.status_code == 200 and r.json()["enabled"] is True and r.json()["state"] == "ready"
    assert rt.features.enabled("assistant")
    assert (await c.get("/api/health")).json()["assistant"] == "ready"
    st = (await c.get("/api/assistant/status")).json()
    assert st["state"] == "ready" and st["me"]["daily_limit"] == 50
    assert {f["name"] for f in st["features"]} >= {"ai_explain_alert", "ai_knowledge"}
    # the live client works (the interface of the agent loop / AI features)
    out = await rt.assistant.client.converse(
        {
            "messages": [{"role": "user", "content": [{"text": "hello"}]}],
            "inferenceConfig": {"maxTokens": 50},
        }
    )
    assert out["stopReason"] and out["_meta"]["model_id"] == MODEL
    # connection change while ON needs a test; other fields do not
    r = await c.put("/api/assistant/settings", json={"model_id_light": MODEL})
    assert r.status_code == 422 and r.json()["error"]["code"] == "assistant_test_required"
    r = await c.put(
        "/api/assistant/settings", json={"prices": {"input_per_mtok": 3, "output_per_mtok": 15}}
    )
    assert r.status_code == 200 and r.json()["prices"]["input_per_mtok"] == 3
    acts = {
        r["action"] for r in (await c.get("/api/audit", params={"page_size": 500})).json()["items"]
    }
    assert {"assistant.test", "assistant.settings_update", "feature.enable"} <= acts
    # audit cannot be turned off while the assistant is ON; the assistant can
    r = await c.put("/api/settings", json={"feature_audit_enabled": False})
    assert r.status_code == 422 and r.json()["error"]["code"] == "feature_dependency"
    out = await feats(c, assistant=False, audit=False)
    assert out["feature_assistant_enabled"] is False
    assert (await c.get("/api/health")).json()["assistant"] == "disabled"
    with pytest.raises(AssistantError):
        _ = rt.assistant.client


async def test_rate_limit(env: httpx.AsyncClient, mock: Any) -> None:
    for i in range(10):
        r = await env.post("/api/assistant/test", json={"settings": {"region": "us-east-1"}})
        assert r.status_code == 200, i
    r = await env.post("/api/assistant/test", json={})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("access_denied", "bedrock_access_denied"),
        ("model_access", "bedrock_model_access"),
        ("on_demand_unsupported", "bedrock_model_invalid"),
        ("quota", "bedrock_quota"),
        ("throttle:5", "bedrock_throttled"),
    ],
)
async def test_test_error_modes(env: httpx.AsyncClient, mock: Any, mode: str, code: str) -> None:
    mock.mode(mode, "converse")
    res = (await env.post("/api/assistant/test", json={"settings": conn(mock)})).json()
    assert not res["ok"] and res["test_id"] is None
    assert res["steps"][-1]["step"] == "converse" and res["steps"][-1]["error_code"] == code
    assert res["error"]["code"] == code


async def test_bad_secret_and_network(env: httpx.AsyncClient, mock: Any) -> None:
    res = (
        await env.post(
            "/api/assistant/test",
            json={
                "settings": conn(mock, secret_access_key="wrong-secret-0000000000000000000000000")
            },
        )
    ).json()
    assert res["error"]["code"] == "aws_auth_failed", res
    res = (
        await env.post(
            "/api/assistant/test", json={"settings": conn(mock, endpoint_url="http://127.0.0.1:9")}
        )
    ).json()
    assert res["steps"][-1]["step"] == "endpoint" and res["error"]["code"] == "aws_network"
    res = (
        await env.post(
            "/api/assistant/test",
            json={"settings": conn(mock, access_key_id=None, secret_access_key="")},
        )
    ).json()
    assert res["steps"][0]["step"] == "config" and not res["steps"][0]["ok"]


async def test_live_errors_update_health(rt: Runtime, env: httpx.AsyncClient, mock: Any) -> None:
    await enable(env, conn(mock))
    mock.mode("access_denied", "converse", times=1)
    req = {
        "messages": [{"role": "user", "content": [{"text": "x"}]}],
        "inferenceConfig": {"maxTokens": 20},
    }
    with pytest.raises(AssistantError) as e:
        await rt.assistant.client.converse(req)
    assert e.value.code == "bedrock_access_denied"
    assert (await env.get("/api/health")).json()["assistant"] == "error"
    st = (await env.get("/api/assistant/settings")).json()
    assert st["state"] == "error" and st["state_error"]["code"] == "bedrock_access_denied"
    await rt.assistant.client.converse(req)  # works again
    assert (await env.get("/api/health")).json()["assistant"] == "ready"
    mock.mode("throttle", "converse", times=1)
    out = await rt.assistant.client.converse(req)  # retried
    assert out["_meta"]["retries"] == ["bedrock_throttled"]
    assert rt.assistant.state() == "degraded"
    # streaming through the live client
    asm, end = await rt.assistant.client.converse_stream_collect(req)
    assert asm.stop_reason and end["time_to_first_event_ms"] is not None


# ---------------------------------------------------------------- Roles Anywhere
async def test_roles_anywhere_upload_and_session_cache(
    rt: Runtime, env: httpx.AsyncClient, mock: Any
) -> None:
    c = env
    await feats(c, audit=True)
    r = await upload(
        c,
        {
            "file": ("leaf.pem", pki_file(mock, "leaf-rsa.pem")),
            "key": ("leaf.key", pki_file(mock, "leaf-rsa.key")),
        },
    )
    assert r.status_code == 200, r.text
    info = r.json()
    assert (
        info["serial_decimal"] == "4660"
        and info["key_type"] == "rsa"
        and info["source"] == "uploaded"
    )
    assert "PRIVATE" not in r.text
    for bad, code in (
        ("leaf-catrue", "invalid_certificate"),
        ("leaf-expired", "invalid_certificate"),
        ("leaf-rsa1024", "invalid_certificate"),
    ):
        r = await upload(
            c,
            {
                "file": ("x.pem", pki_file(mock, f"{bad}.pem")),
                "key": ("x.key", pki_file(mock, f"{bad}.key")),
            },
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == code, bad
    r = await upload(
        c,
        {
            "file": ("x.pem", pki_file(mock, "leaf-rsa.pem")),
            "key": ("x.key", pki_file(mock, "leaf-ec.key")),
        },
    )
    assert r.json()["error"]["code"] == "key_mismatch"
    r = await upload(
        c,
        {
            "file": ("x.pfx", pki_file(mock, "leaf-ec.pfx")),
            "password": (None, mock.pki["pfx_password"]),
        },
    )
    assert r.status_code == 200 and r.json()["key_type"] == "ec", r.text
    s = (await c.get("/api/assistant/settings")).json()
    assert s["certificate"]["key_type"] == "ec" and s["certificate"]["chain"]
    settings = conn(mock, "roles_anywhere")
    res = await enable(c, settings)
    assert "source roles_anywhere" in res["steps"][1]["detail"]
    sessions = mock.requests("create_session")
    assert (
        sessions and sessions[-1]["sig"]["ok"] and sessions[-1]["body"]["durationSeconds"] == 3600
    )
    n = len(sessions)
    req = {
        "messages": [{"role": "user", "content": [{"text": "x"}]}],
        "inferenceConfig": {"maxTokens": 20},
    }
    await rt.assistant.client.converse(req)
    await rt.assistant.client.converse(req)
    assert len(mock.requests("create_session")) == n + 1  # one session for the live client
    # deleting the certificate while ON in roles_anywhere mode is refused
    assert (await c.delete("/api/assistant/cert")).status_code == 409
    # untrusted certificate -> rolesanywhere_failed with a hint
    mock.mode("untrusted_cert", "sessions")
    res = (await c.post("/api/assistant/test", json={"settings": settings})).json()
    assert (
        res["error"]["code"] == "rolesanywhere_failed" and "trust anchor" in res["error"]["message"]
    )
    rows = (await c.get("/api/audit", params={"action": "assistant.cert_upload"})).json()["items"]
    assert rows and "PRIVATE" not in str(rows)


async def test_csr_generate_and_upload(env: httpx.AsyncClient, mock: Any) -> None:
    c = env
    await feats(c, audit=True)
    r = await c.post(
        "/api/assistant/cert/csr",
        json={"common_name": "netmonitor-bedrock.mnidom.net", "key_type": "ec"},
    )
    assert r.status_code == 200, r.text
    assert "BEGIN CERTIFICATE REQUEST" in r.json()["pem"] and "PRIVATE" not in r.text
    d = await c.get("/api/assistant/cert/csr")
    assert d.status_code == 200 and "attachment" in d.headers["content-disposition"]
    s = (await c.get("/api/assistant/settings")).json()
    assert s["csr"]["subject"] == "CN=netmonitor-bedrock.mnidom.net"
    # the internal CA (the mock's issuing CA) signs the CSR
    csr = x509.load_pem_x509_csr(d.content)
    ca = x509.load_pem_x509_certificate(pki_file(mock, "issuing.pem"))
    ca_key = serialization.load_pem_private_key(pki_file(mock, "issuing.key"), None)
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca.subject)
        .public_key(csr.public_key())
        .serial_number(0xBEEF)
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=200))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(csr.extensions.get_extension_for_class(x509.KeyUsage).value, critical=True)
        .add_extension(
            csr.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value, critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    r = await upload(c, {"file": ("issued.pem", cert.public_bytes(serialization.Encoding.PEM))})
    assert r.status_code == 200, r.text
    assert r.json()["serial_decimal"] == str(0xBEEF)
    s = (await c.get("/api/assistant/settings")).json()
    assert s["csr"] is None and s["certificate"]["serial_decimal"] == str(0xBEEF)
    res = (
        await c.post("/api/assistant/test", json={"settings": conn(mock, "roles_anywhere")})
    ).json()
    assert res["ok"], res
    assert (await c.delete("/api/assistant/cert/csr")).status_code == 204
    assert (await c.get("/api/assistant/cert/csr")).status_code == 404
    acts = {
        r["action"] for r in (await c.get("/api/audit", params={"page_size": 200})).json()["items"]
    }
    assert {"assistant.csr_create", "assistant.cert_upload"} <= acts


async def test_default_chain_imds(
    env: httpx.AsyncClient, mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_EC2_METADATA_SERVICE_ENDPOINT", mock.url)
    monkeypatch.delenv("AWS_EC2_METADATA_DISABLED", raising=False)
    res = (
        await env.post("/api/assistant/test", json={"settings": conn(mock, "default_chain")})
    ).json()
    assert res["ok"], res
    assert "source imds" in res["steps"][1]["detail"]
    assert mock.requests("imds_token")[-1]["ttl"] == 21600
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    res = (
        await env.post("/api/assistant/test", json={"settings": conn(mock, "default_chain")})
    ).json()
    assert res["error"]["code"] == "aws_auth_failed" and "environment" in res["error"]["message"]


# ---------------------------------------------------------------- usage and caps
async def test_usage_caps_and_report(rt: Runtime, env: httpx.AsyncClient, mock: Any) -> None:
    c = env
    await c.put(
        "/api/assistant/settings", json={"prices": {"input_per_mtok": 3, "output_per_mtok": 15}}
    )
    await c.put(
        "/api/settings",
        json={"assistant_daily_questions_per_user": 2, "assistant_monthly_token_cap": 100_000},
    )
    me = (await c.get("/api/auth/me")).json()
    uid = int(me.get("id") or me.get("user", {}).get("id"))
    u = rt.assistant.usage
    await u.check_and_reserve(uid, "ask")
    await u.check_and_reserve(uid, "explain")
    with pytest.raises(AssistantError) as e:
        await u.check_and_reserve(uid, "ask")
    assert e.value.code == "assistant_cap_reached" and e.value.details[0]["cap"] == "user_daily"
    assert e.value.status == 429
    await u.check(uid, "ask", question=False)  # follow-up call of a counted question
    cost = await u.record(
        uid, "ask", {"inputTokens": 6000, "outputTokens": 600, "cacheReadInputTokens": 6000}
    )
    assert float(cost) == pytest.approx(0.0288)
    # per-purpose daily cap (AI features)
    await u.check_and_reserve(0, "alert_note", purpose_daily_cap=1)
    with pytest.raises(AssistantError) as e:
        await u.check_and_reserve(0, "alert_note", purpose_daily_cap=1)
    assert e.value.details[0]["cap"] == "purpose_daily"
    # background stops at 80 % of the monthly cap, interactive at 100 %
    assert await u.background_allowed()
    await u.record(0, "digest", {"input_tokens": 80_000})
    assert not await u.background_allowed()
    await u.check(uid, "ask", question=False)
    await u.record(0, "digest", {"input_tokens": 20_000})
    with pytest.raises(AssistantError) as e:
        await u.check(uid, "ask", question=False)
    assert e.value.details[0]["cap"] == "monthly_tokens" and e.value.details[0]["resets_at"]
    # monthly USD cost cap (admin setting, effective with prices)
    await c.put(
        "/api/settings",
        json={"assistant_monthly_token_cap": 1_000_000_000, "assistant_monthly_cost_cap": 0.01},
    )
    with pytest.raises(AssistantError) as e:
        await u.check(uid, "ask", question=False)
    assert e.value.details[0]["cap"] == "monthly_cost"
    await u.record_error(uid, "ask", "bedrock_throttled")
    rep = (await c.get("/api/assistant/usage")).json()
    assert rep["tokens"] == 106_600 + 600 and rep["currency"] == "USD"
    assert rep["cost_cap"] == 0.01 and rep["pct_of_cap"] > 100
    assert {p["purpose"] for p in rep["by_purpose"]} >= {"ask", "digest", "alert_note"}
    assert {x["username"] for x in rep["by_user"]} >= {"system", "admin"}
    assert len(rep["last_12_months"]) == 12 and rep["by_day"][0]["questions"] >= 3
    assert (await c.get("/api/assistant/usage", params={"month": "2026-13"})).status_code == 422
    assert (await c.get("/api/assistant/usage", params={"month": "2025-01"})).json()["tokens"] == 0


# ---------------------------------------------------------------- KB / models
async def test_knowledge_base_and_models(rt: Runtime, env: httpx.AsyncClient, mock: Any) -> None:
    c = env
    r = await c.put(
        "/api/assistant/knowledge",
        json={"knowledge_base_id": "QAKBTEST01", "description": "runbooks"},
    )
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert (
        await c.put("/api/assistant/knowledge", json={"knowledge_base_id": "bad"})
    ).status_code == 422
    r = await c.put("/api/assistant/knowledge", json={"enabled": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "assistant_test_required"
    assert (await c.post("/api/assistant/knowledge/test", json={})).status_code == 409
    await enable(c, conn(mock, endpoint_url=mock.url))
    await c.put("/api/assistant/knowledge", json={"endpoint_url": mock.url})
    res = (await c.post("/api/assistant/knowledge/test", json={})).json()
    assert res["ok"] and res["results"] >= 1 and res["test_id"], res
    r = await c.put("/api/assistant/knowledge", json={"enabled": True, "test_id": res["test_id"]})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert rt.features.enabled("ai_knowledge")
    passages = await rt.assistant.client.retrieve("core switch", rt.assistant.kb)
    assert passages and passages[0]["text"] and all("source_url" in p for p in passages)
    mock.mode("kb_error")
    res = (await c.post("/api/assistant/knowledge/test", json={})).json()
    assert not res["ok"] and res["error"]["code"] == "kb_unavailable"
    models = (await c.post("/api/assistant/models", json={})).json()
    assert models["error"] is None
    ids = {m["id"] for m in models["items"]}
    assert MODEL in ids and not any("nova" in i for i in ids)
    assert {m["kind"] for m in models["items"]} == {"inference_profile", "foundation_model"}


async def test_ssrf_blocked_endpoints_reject_before_outbound(
    env: httpx.AsyncClient, mock: Any
) -> None:
    """A metadata/link-local endpoint is rejected (422) before any outbound request; the
    mock Bedrock sees no traffic. 169.254.169.254 and metadata.google.internal are blocked
    even in test mode (loopback is not, so the harness keeps using 127.0.0.1)."""
    c = env
    await enable(c, conn(mock, endpoint_url=mock.url))
    await c.put("/api/assistant/knowledge", json={"knowledge_base_id": "QAKBTEST01"})
    before = len(mock.requests())  # count only the blocked calls below
    blocked: list[tuple[str, dict[str, Any]]] = [
        ("/api/assistant/models", {"settings": conn(mock, endpoint_url="https://169.254.169.254")}),
        ("/api/assistant/test", {"settings": conn(mock, endpoint_url="http://169.254.169.254")}),
        (
            "/api/assistant/test",
            {"settings": conn(mock, proxy_mode="custom", proxy_url="http://169.254.169.254:80")},
        ),
        (
            "/api/assistant/knowledge/test",
            {"settings": {"endpoint_url": "https://metadata.google.internal"}},
        ),
    ]
    for path, body in blocked:
        r = await c.post(path, json=body)
        assert r.status_code == 422, (path, r.text)
        assert r.json()["error"]["code"] == "validation_error", r.text
    assert len(mock.requests()) == before, "a blocked endpoint must not reach the network"


async def test_redact_internal_ips_defaults_true(rt: Runtime, admin: httpx.AsyncClient) -> None:
    """[3b Finding B] masking defaults ON: a fresh install and a stored row that predates
    the key both resolve `redact_internal_ips` to True."""
    from netmon.assistant import config as acfg

    assert acfg.DEFAULT_ASSISTANT["redact_internal_ips"] is True
    async with rt.sessionmaker() as s:
        assert (await acfg.load(s))["redact_internal_ips"] is True
        # a row written before the key existed (upgrade path)
        await acfg.save(s, {"region": "eu-west-1", "model_id": "m"})
        await s.commit()
    async with rt.sessionmaker() as s:
        cfg = await acfg.load(s)
    assert cfg["redact_internal_ips"] is True and cfg["region"] == "eu-west-1"


# ------------------------------------------------- temporary credentials (ASIA + session token)
ASIA_ID = "ASIA" + "T" * 16
ASIA_SECRET = "tmpSecret0000000000000000000000000000000"
ASIA_TOKEN = "IQoJb3JpZ2luX2VjEJr//////////wEaCXVzLWVhc3QtMSJHMEUCIQ" + "A" * 400 + "=="


def _register_session(mock: Any, *, expired: bool = False) -> None:
    import time

    with mock.state.lock:
        mock.state.sessions[ASIA_ID] = {
            "access_key_id": ASIA_ID,
            "secret": ASIA_SECRET,
            "token": ASIA_TOKEN,
            "source": "control",
            "expires_at": time.time() + (-5 if expired else 3600),
            "issued_at": time.time(),
        }


def temp_conn(mock: Any, **kw: Any) -> dict[str, Any]:
    return conn(mock, access_key_id=ASIA_ID, secret_access_key=ASIA_SECRET, **kw)


async def test_session_token_settings(
    rt: Runtime, env: httpx.AsyncClient, mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = env
    await feats(c, audit=True)
    r = await c.put("/api/assistant/settings", json=temp_conn(mock, session_token=ASIA_TOKEN))
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["session_token"] is None and s["session_token_set"] is True
    assert s["session_token_unreadable"] is False and s["access_key_id"] == ASIA_ID
    assert ASIA_TOKEN not in r.text and not any(k.endswith("_enc") for k in s)
    assert "temporary_credentials" in s["warnings"]
    rows = (await c.get("/api/audit", params={"action": "assistant.settings_update"})).json()
    assert ASIA_TOKEN not in str(rows) and "session_token_enc" in str(rows)
    # omitted keeps; too long / invalid characters -> 422; "" clears
    r = await c.put("/api/assistant/settings", json={"model_id_light": MODEL})
    assert r.json()["session_token_set"] is True
    for bad in ("A" * 4097, "abc\r\nX-Evil: 1"):
        r = await c.put("/api/assistant/settings", json={"session_token": bad})
        assert r.status_code == 422, bad
    r = await c.put("/api/assistant/settings", json={"session_token": ""})
    s = r.json()
    assert s["session_token_set"] is False and "temporary_credentials" not in s["warnings"]
    # a token stored under another secret.key is reported unreadable (re-enter)
    await c.put("/api/assistant/settings", json={"session_token": ASIA_TOKEN})
    monkeypatch.setattr(rt, "secret_key", "another-secret-key-0000000000000000")
    s = rt.assistant.settings_out()  # (the API session itself depends on secret.key)
    assert s["session_token_set"] is True and s["session_token_unreadable"] is True


async def test_temporary_key_needs_session_token(env: httpx.AsyncClient, mock: Any) -> None:
    _register_session(mock)
    n = len(mock.requests())
    res = (await env.post("/api/assistant/test", json={"settings": temp_conn(mock)})).json()
    assert not res["ok"] and res["test_id"] is None
    assert [s["step"] for s in res["steps"]] == ["config", "credentials"]
    assert res["steps"][0]["ok"] and res["steps"][1]["error_code"] == "aws_session_token_required"
    assert res["error"]["code"] == "aws_session_token_required"
    assert "ASIA" in res["error"]["message"] and "session token" in res["error"]["message"]
    assert len(mock.requests()) == n  # failed before any AWS call
    # an AKIA key without a token is unaffected
    res = (await env.post("/api/assistant/test", json={"settings": conn(mock)})).json()
    assert res["ok"] and "temporary_credentials" not in res["warnings"], res


async def test_session_token_sent_and_expired(
    rt: Runtime, env: httpx.AsyncClient, mock: Any
) -> None:
    c = env
    _register_session(mock)
    settings = temp_conn(mock, session_token=ASIA_TOKEN)
    res = (await c.post("/api/assistant/test", json={"settings": settings})).json()
    assert res["ok"], res
    assert "temporary_credentials" in res["warnings"]
    sig = mock.requests("converse_stream")[-1]["sig"]
    assert sig["ok"] and sig["access_key_id"] == ASIA_ID
    assert "x-amz-security-token" in sig["signed_headers"] and "warning" not in sig
    # a test of token A does not unlock token B (the token is a connection field)
    await feats(c, audit=True)
    r = await c.put(
        "/api/assistant/settings",
        json={**settings, "session_token": ASIA_TOKEN[:-2] + "QQ", "enabled": True,
              "test_id": res["test_id"]},
    )  # fmt: skip
    assert r.status_code == 422 and r.json()["error"]["code"] == "assistant_test_required"
    r = await c.put(
        "/api/assistant/settings", json={**settings, "enabled": True, "test_id": res["test_id"]}
    )
    assert r.status_code == 200 and r.json()["state"] == "ready", r.text
    # while ON, a new token needs a new test
    r = await c.put("/api/assistant/settings", json={"session_token": ASIA_TOKEN + "QQ=="})
    assert r.status_code == 422 and r.json()["error"]["code"] == "assistant_test_required"
    # the token expires -> aws_token_expired (distinct message), health state error
    _register_session(mock, expired=True)
    res = (await c.post("/api/assistant/test", json={"settings": settings})).json()
    assert not res["ok"] and res["steps"][-1]["step"] == "converse"
    assert res["error"]["code"] == "aws_token_expired", res
    assert "session token has expired" in res["error"]["message"]
    with pytest.raises(AssistantError) as e:
        await rt.assistant.client.converse(
            {
                "messages": [{"role": "user", "content": [{"text": "hello"}]}],
                "inferenceConfig": {"maxTokens": 50},
            }
        )
    assert e.value.code == "aws_token_expired"
    st = (await c.get("/api/assistant/settings")).json()
    assert st["state"] == "error" and st["state_error"]["code"] == "aws_token_expired"
    await feats(c, assistant=False)


# ---------------------------------------------------------------- migration 0008
@pytest.mark.parametrize("mode", ["timescale", "fallback"])
async def test_migration_0008_up_down_up(mode: str, rt: Runtime) -> None:
    from alembic import command

    from netmon.db import timescale
    from netmon.db.engine import make_engine
    from netmon.db.migrate import _alembic_config, run_migrations

    assert TEST_DATABASE_URL
    if mode == "timescale" and rt.settings.netmon_disable_timescale:
        pytest.skip("suite runs in forced fallback mode")
    name = f"netmon_test_m8_{secrets.token_hex(3)}"
    adm = await asyncpg.connect(_plain(TEST_DATABASE_URL))
    await adm.execute(f'CREATE DATABASE "{name}"')
    url = db_url_for(name)
    engine = make_engine(url.replace("postgresql://", "postgresql+asyncpg://"))
    tables = {
        "assistant_conversations",
        "assistant_messages",
        "assistant_tool_calls",
        "assistant_usage",
        "assistant_artifacts",
        "assistant_incidents",
        "assistant_incident_members",
        "assistant_note_targets",
        "assistant_syslog_signatures",
        "assistant_snmp_walks",
    }

    def downgrade(sync_conn: Any, rev: str) -> None:
        cfg = _alembic_config()
        cfg.attributes["connection"] = sync_conn
        command.downgrade(cfg, rev)

    async def present(conn: Any) -> set[str]:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {r["tablename"] for r in rows}

    try:
        await run_migrations(engine)
        enabled = await timescale.bootstrap(engine, 90, disabled=(mode == "fallback"))
        if mode == "timescale" and not enabled:
            pytest.skip("TimescaleDB not available")
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0012"
            assert tables <= await present(conn)
            uid = await conn.fetchval(
                "INSERT INTO users (username, password_hash, role) VALUES ('user1', 'x', 'admin') "
                "RETURNING id"
            )
            cid = await conn.fetchval(
                "INSERT INTO assistant_conversations (user_id, title) VALUES ($1, 't') "
                "RETURNING id",
                uid,
            )
            await conn.execute(
                "INSERT INTO assistant_messages (conversation_id, seq, role, purpose) "
                "VALUES ($1, 1, 'user', 'ask')",
                cid,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO assistant_messages (conversation_id, seq, role, purpose) "
                    "VALUES ($1, 1, 'assistant', 'ask')",
                    cid,
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO assistant_messages (conversation_id, seq, role, purpose) "
                    "VALUES ($1, 2, 'assistant', 'bogus')",
                    cid,
                )
            await conn.execute(
                "INSERT INTO assistant_usage (day, user_id, purpose, input_tokens) "
                "VALUES (current_date, 0, 'digest', 5)"
            )
            assert await conn.fetchval("SELECT count(*) FROM alert_rules WHERE ai_note") == 0
            cols = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_name IN "
                    "('alert_rules', 'reports', 'report_runs')"
                )
            }
            assert {"ai_note", "ai_summary", "ai_summary_status"} <= cols
            await conn.execute(
                "INSERT INTO audit_log (actor_type, action, source) VALUES "
                "('user', 'assistant.question', 'telegram')"
            )
            await conn.execute("DELETE FROM users WHERE id = $1", uid)  # cascades
            assert await conn.fetchval("SELECT count(*) FROM assistant_messages") == 0
        finally:
            await conn.close()
        async with engine.connect() as cx:
            await cx.run_sync(lambda sc: downgrade(sc, "0007"))
            await cx.commit()
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0007"
            assert not (tables & await present(conn))
            assert await conn.fetch("SELECT source FROM audit_log") == [(None,)]
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO audit_log (actor_type, action, source) VALUES "
                    "('user', 'x', 'telegram')"
                )
            with pytest.raises(asyncpg.PostgresError):  # append-only trigger back on
                await conn.execute("UPDATE audit_log SET action = 'y'")
        finally:
            await conn.close()
        await run_migrations(engine)
        conn = await asyncpg.connect(_plain(url))
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0012"
            assert tables <= await present(conn)
        finally:
            await conn.close()
    finally:
        await engine.dispose()
        await adm.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await adm.close()
