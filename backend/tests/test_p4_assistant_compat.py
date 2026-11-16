"""[4] PR 4-0a: the assistant on the shared AWS connections — the settings shape, the moved
fields, the configure-first rule for `connection_id` / `access`, the chain Test steps, questions,
Knowledge Base and models through a hop (layout b), the live chain in the status, a forced
refresh of hop and base after `ExpiredTokenException`, and the owner's no-downtime move from
layout (a) to layout (b) (architecture § 15.1.9)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from netmon.runtime import Runtime
from tests.conftest import requires_db
from tests.fixtures_3b_agent import ask, done_message
from tests.p4_aws import (
    BEDROCK_ACCT,
    EXT_ID,
    NET_ACCT,
    assistant_fields,
    create_network,
    create_ra,
    enable_assistant,
    env_setup,
    feats,
    hint_codes,
    hop_access,
    start_mock,
    steps,
    trust_model,
)

pytestmark = requires_db
REQ = {
    "messages": [{"role": "user", "content": [{"text": "x"}]}],
    "inferenceConfig": {"maxTokens": 20},
}


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


async def test_settings_shape_and_moved_fields(env: httpx.AsyncClient, mock: Any) -> None:
    c = env
    s = (await c.get("/api/assistant/settings")).json()
    gone = {
        "auth_mode",
        "access_key_id",
        "secret_access_key",
        "secret_access_key_set",
        "session_token",
        "access_key_created_at",
        "ra_region",
        "ra_endpoint_url",
        "ra_trust_anchor_arn",
        "ra_profile_arn",
        "ra_role_arn",
        "ra_session_seconds",
        "proxy_mode",
        "proxy_url",
        "certificate",
        "certificate_host_files",
        "csr",
    }
    assert not gone & set(s)
    assert s["connection_id"] is None and s["connection"] is None
    assert s["access"] == {"mode": "direct", "role_arn": None, "external_id": None}
    for f, v in (
        ("proxy_url", "http://p:3128"),
        ("ra_role_arn", "arn:aws:iam::1:role/x"),
        ("session_token", "abc"),
    ):
        for path, body in (
            ("/api/assistant/settings", {f: v}),
            ("/api/assistant/test", {"settings": {f: v}}),
            ("/api/assistant/models", {"settings": {f: v}}),
        ):
            r = await c.request("PUT" if path.endswith("settings") else "POST", path, json=body)
            assert r.status_code == 422, (path, f)
            d = r.json()["error"]["details"][0]
            assert d["loc"] == ["body", f] and d["msg"] == "moved to Settings → AWS"
    # no connection: the Test fails at config, enabling is refused
    res = (await c.post("/api/assistant/test", json={"settings": assistant_fields(mock)})).json()
    assert steps(res) == [("config", None, False)] and "connection_id" in res["error"]["message"]
    await feats(c, audit=True)
    r = await c.put("/api/assistant/settings", json={**assistant_fields(mock), "enabled": True})
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["loc"] == ["body", "connection_id"]
    # the /api/assistant/cert* endpoints are gone
    assert (await c.post("/api/assistant/cert/csr", json={"common_name": "x"})).status_code in (
        404,
        405,
    )


async def test_layout_b_hop_ask_kb_models_and_status(
    rt: Runtime, env: httpx.AsyncClient, mock: Any
) -> None:
    c = env
    n = await create_network(c, mock)
    trust_model(mock)
    settings = {**assistant_fields(mock), "connection_id": n["id"], "access": hop_access()}
    res = await enable_assistant(c, settings)
    assert steps(res) == [
        ("config", None, True),
        ("credentials", 0, True),
        ("identity", 0, True),
        ("hop", 1, True),
        ("hop_identity", 1, True),
        ("endpoint", None, True),
        ("converse", None, True),
    ]
    assert rt.assistant.state() == "ready"
    s = (await c.get("/api/assistant/settings")).json()
    assert s["connection"]["name"] == "Network account" and s["access"]["external_id"] == EXT_ID
    # a question through the hop: Bedrock sees the assumed-role (Bedrock account) credentials
    status, events = await ask(c, "How is the network?")
    assert status == 200 and done_message(events)["status"] == "complete"
    akid = mock.requests("converse_stream")[-1]["sig"]["access_key_id"]
    hop_keys = {
        e["issued_access_key_id"]
        for e in mock.requests("sts_assume_role")
        if e.get("issued_access_key_id")
    }
    assert akid in hop_keys
    st = (await c.get("/api/assistant/status")).json()["aws"]
    assert st["connection_id"] == n["id"] and st["access_mode"] == "assume_role"
    assert [h["kind"] for h in st["hops"]] == ["roles_anywhere", "assume_role"]
    assert st["hops"][0]["account"] == NET_ACCT and st["hops"][1]["account"] == BEDROCK_ACCT
    assert st["hops"][1]["expires_at"] and st["hops"][1]["last_refresh"]
    assert st["last_error"] is None
    s = (await c.get("/api/assistant/settings")).json()
    assert s["connection"]["account"] == BEDROCK_ACCT and s["connection"]["state"] == "ok"
    # models and the Knowledge Base use the same chain
    models = (await c.post("/api/assistant/models", json={})).json()
    assert models["error"] is None and models["items"]
    auth = mock.requests("list_models,list_profiles")[-1]["headers"]["authorization"]
    assert any(f"Credential={k}/" in auth for k in hop_keys)
    await c.put(
        "/api/assistant/knowledge",
        json={"knowledge_base_id": "QAKBTEST01", "endpoint_url": mock.url},
    )
    kb = (await c.post("/api/assistant/knowledge/test", json={})).json()
    assert kb["ok"], kb
    # ExpiredTokenException from Bedrock: one forced refresh of the hop AND its base, one retry
    n_ra, n_sts = len(mock.requests("create_session")), len(mock.requests("sts_assume_role"))
    mock.mode("expired_token", "converse", times=1)
    out = await rt.assistant.client.converse(REQ)
    assert out["stopReason"]
    assert len(mock.requests("create_session")) == n_ra + 1
    assert len(mock.requests("sts_assume_role")) == n_sts + 1
    # changing the access while ON needs a test
    r = await c.put("/api/assistant/settings", json={"access": hop_access("Another-ext-id-1")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "assistant_test_required"
    # null = the default, which is this same connection: the binding (resolved id) is unchanged
    r = await c.put("/api/assistant/settings", json={"connection_id": None})
    assert r.status_code == 200 and r.json()["connection"]["id"] == n["id"]
    # the Bedrock step hints: model access not enabled
    mock.mode("model_access", "converse", times=1)
    res = (await c.post("/api/assistant/test", json={"settings": settings})).json()
    assert steps(res)[-1] == ("converse", None, False)
    assert hint_codes(res["steps"][-1]) == ["model_access"]


async def test_no_downtime_move_from_layout_a_to_b(
    rt: Runtime, env: httpx.AsyncClient, mock: Any
) -> None:
    """§ 15.1.9: "Default" (Roles Anywhere in the Bedrock account, direct) → a "Network
    account" connection → the assistant assumes the Bedrock role → set default → delete the
    old connection; the assistant answers at every step."""
    c = env
    d = await create_ra(c, mock)  # step 0
    await enable_assistant(c, assistant_fields(mock))
    await rt.assistant.client.converse(REQ)
    n = await create_network(c, mock)  # step 3
    trust_model(mock)
    res = (await c.post("/api/aws/connections/test", json={"connection_id": n["id"]})).json()
    assert res["ok"] and f"account {NET_ACCT}" in res["steps"][2]["detail"]
    await rt.assistant.client.converse(REQ)  # unchanged
    # step 4: a failed test changes nothing
    bad = {**assistant_fields(mock), "connection_id": n["id"], "access": hop_access("wrong-id-00")}
    res = (await c.post("/api/assistant/test", json={"settings": bad})).json()
    assert not res["ok"] and steps(res)[-1] == ("hop", 1, False)
    assert hint_codes(res["steps"][-1])[2] == "external_id"
    await rt.assistant.client.converse(REQ)
    good = {**assistant_fields(mock), "connection_id": n["id"], "access": hop_access()}
    res = (await c.post("/api/assistant/test", json={"settings": good})).json()
    assert res["ok"], res
    r = await c.put("/api/assistant/settings", json={**good, "test_id": res["test_id"]})
    assert r.status_code == 200, r.text
    await rt.assistant.client.converse(REQ)
    assert rt.assistant.aws_status()["access_mode"] == "assume_role"
    # step 6: set as default; delete the old one once nothing uses it
    r = await c.post(f"/api/aws/connections/{n['id']}/default")
    assert r.status_code == 200 and r.json()[0]["name"] == "Network account"
    assert (await c.delete(f"/api/aws/connections/{d['id']}")).status_code == 204
    await rt.assistant.client.converse(REQ)
    assert rt.assistant.state() == "ready"
