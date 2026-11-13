"""[4] PR 4-0a API against the Bedrock + Roles Anywhere + STS mock: connection CRUD, default,
delete rules, write-only secrets, `test_required`, audit without secrets / ExternalId, rendered
policies, the chain Test per mode and layout with the hint table, egress (save and connect
time) and certificates (PKCS#7, `key_mismatch` details, CN warning, copy)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from netmon.runtime import Runtime
from tests.conftest import requires_db
from tests.p4_aws import (
    AKID,
    BEDROCK_ACCT,
    BEDROCK_ROLE,
    EXT_ID,
    LEAF_SI,
    NET_ACCT,
    NET_BASE_ROLE,
    NET_OTHER_ROLE,
    NET_PROFILE,
    NET_TA,
    SECRET,
    STATIC_ARN,
    assistant_fields,
    create_ak,
    create_network,
    create_ra,
    enable_assistant,
    env_setup,
    feats,
    hint_codes,
    hop_access,
    overrides,
    pki_file,
    start_mock,
    steps,
    trust_model,
)

pytestmark = requires_db


@pytest.fixture(scope="module")
def mock(tmp_path_factory: pytest.TempPathFactory) -> Any:
    m = start_mock(tmp_path_factory)
    yield m
    m.stop()


@pytest.fixture
async def env(rt: Runtime, admin: httpx.AsyncClient, mock: Any, monkeypatch: pytest.MonkeyPatch):
    await env_setup(rt, mock, monkeypatch)
    yield admin
    rt.aws.tests.clear()


async def ctest(c: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    r = await c.post("/api/aws/connections/test", json=body)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------- CRUD
async def test_crud_default_delete_and_secrets(
    rt: Runtime, app: Any, env: httpx.AsyncClient, viewer: Any, mock: Any
) -> None:
    c = env
    await feats(c, audit=True)
    assert (await c.get("/api/aws/connections")).json() == []
    a = await create_ak(c, mock, "Keys")
    assert a["is_default"] is True  # the first connection becomes the default
    assert a["secret_access_key"] is None and a["secret_access_key_set"] is True
    assert SECRET not in str(a) and not any(k.endswith("_enc") for k in a)
    b = await create_ra(c, mock, "Bedrock RA")
    assert b["is_default"] is False and b["certificate"]["source_identity"] == LEAF_SI
    lst = (await c.get("/api/aws/connections")).json()
    assert [x["name"] for x in lst] == ["Keys", "Bedrock RA"]  # default first
    # validation and conflicts
    r = await c.post(
        "/api/aws/connections",
        json={"name": "keys", "auth_mode": "access_key", "region": "us-east-1"},
    )
    assert r.status_code == 409  # duplicate name (case-insensitive)
    r = await c.post("/api/aws/connections", json={"name": "x"})
    assert r.status_code == 422 and {d["loc"][-1] for d in r.json()["error"]["details"]} == {
        "auth_mode",
        "region",
    }
    doubled = "arn:aws:rolesanywhere:us-east-1:111122223333:trust-anchor/" + NET_TA
    r = await c.patch(f"/api/aws/connections/{b['id']}", json={"ra_trust_anchor_arn": doubled})
    assert r.status_code == 422 and "the ARN appears twice — paste it once" in r.text
    assert r.json()["error"]["details"][0]["loc"] == ["body", "ra_trust_anchor_arn"]
    assert (await c.get("/api/aws/connections/999")).status_code == 404
    # write-only secrets: null keeps, "" clears, string replaces
    r = await c.patch(f"/api/aws/connections/{a['id']}", json={"name": "Keys 2"})
    assert r.json()["secret_access_key_set"] is True and r.json()["name"] == "Keys 2"
    # set default; the default cannot be deleted while another connection exists
    r = await c.post(f"/api/aws/connections/{b['id']}/default")
    assert [x["is_default"] for x in r.json()] == [True, False] and r.json()[0]["id"] == b["id"]
    r = await c.delete(f"/api/aws/connections/{b['id']}")
    assert r.status_code == 409 and r.json()["error"]["details"] == [{"reason": "default"}]
    # in use (the assistant names it) -> 409 with used_by
    await c.put("/api/assistant/settings", json={"connection_id": a["id"]})
    r = await c.delete(f"/api/aws/connections/{a['id']}")
    assert r.status_code == 409 and r.json()["error"]["details"] == [{"used_by": ["assistant"]}]
    assert (await c.get(f"/api/aws/connections/{a['id']}")).json()["used_by"] == [
        {"kind": "assistant", "id": None, "name": "Assistant", "access_mode": "direct"}
    ]
    await c.put("/api/assistant/settings", json={"connection_id": None, "model_id": ""})
    assert (await c.delete(f"/api/aws/connections/{a['id']}")).status_code == 204
    assert (await c.delete(f"/api/aws/connections/{b['id']}")).status_code == 204  # the last one
    # audit: actions, no secret
    items = (await c.get("/api/audit", params={"page_size": 200})).json()["items"]
    acts = {i["action"] for i in items}
    assert {
        "aws_connection.create",
        "aws_connection.update",
        "aws_connection.set_default",
        "aws_connection.delete",
        "aws_connection.cert_upload",
    } <= acts
    assert SECRET not in str(items) and "PRIVATE" not in str(items)
    # access control: viewers and API tokens are refused
    assert (await viewer.get("/api/aws/connections")).status_code == 403
    r = await c.post("/api/tokens", json={"name": "script", "role": "admin"})
    tok = r.json()["token"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {tok}"},
    ) as t:
        assert (await t.get("/api/aws/connections")).status_code == 403
        assert (await t.post("/api/aws/external-id")).status_code == 403
    ext = (await c.post("/api/aws/external-id")).json()["external_id"]
    assert len(ext) == 32 and ext.isalnum()


async def test_patch_needs_test_while_the_assistant_uses_it(
    rt: Runtime, env: httpx.AsyncClient, mock: Any
) -> None:
    c = env
    a = await create_ak(c, mock, "Default")
    await enable_assistant(c, assistant_fields(mock))
    assert rt.features.enabled("assistant")
    # the name is not a credential field
    assert (
        await c.patch(f"/api/aws/connections/{a['id']}", json={"name": "Main"})
    ).status_code == 200
    # the same secret again is no change (the hash binds the plaintext digest)
    r = await c.patch(f"/api/aws/connections/{a['id']}", json={"secret_access_key": SECRET})
    assert r.status_code == 200
    r = await c.patch(f"/api/aws/connections/{a['id']}", json={"proxy_mode": "env"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "test_required"
    assert r.json()["error"]["details"] == [{"connection_id": a["id"], "used_by": ["assistant"]}]
    # a test of other values does not unlock it; a test of these values does
    res = await ctest(c, {"connection_id": a["id"], "connection": {"region": "us-west-2"}})
    r = await c.patch(
        f"/api/aws/connections/{a['id']}", json={"proxy_mode": "env", "test_id": res["test_id"]}
    )
    assert r.status_code == 422
    res = await ctest(c, {"connection_id": a["id"], "connection": {"proxy_mode": "env"}})
    assert res["ok"], res
    r = await c.patch(
        f"/api/aws/connections/{a['id']}", json={"proxy_mode": "env", "test_id": res["test_id"]}
    )
    assert r.status_code == 200, r.text
    # last_test of a saved connection: account and ARN of the identity step
    lt = (await c.get(f"/api/aws/connections/{a['id']}")).json()["last_test"]
    assert lt["ok"] and lt["account"] == BEDROCK_ACCT and lt["arn"] == STATIC_ARN
    # the live chain is reported (no secrets)
    await rt.assistant.client.converse(
        {
            "messages": [{"role": "user", "content": [{"text": "x"}]}],
            "inferenceConfig": {"maxTokens": 20},
        }
    )
    chains = (await c.get(f"/api/aws/connections/{a['id']}")).json()["chains"]
    assert chains[0]["consumer"] == "assistant" and chains[0]["state"] == "ok"
    assert chains[0]["hops"][0]["kind"] == "access_key"
    await feats(c, assistant=False)


async def test_set_default_needs_a_test_while_an_on_consumer_uses_the_default_implicitly(
    rt: Runtime, env: httpx.AsyncClient, mock: Any
) -> None:
    """Coordinator decision (PR 4-0a): `POST …/{id}/default` follows "configure first" when an
    ON consumer uses the default implicitly (`connection_id: null`)."""
    c = env
    a = await create_ak(c, mock, "Default")
    b = await create_ak(c, mock, "Second")
    # assistant OFF: no test needed
    r = await c.post(f"/api/aws/connections/{b['id']}/default")
    assert r.status_code == 200 and r.json()[0]["id"] == b["id"]
    r = await c.post(f"/api/aws/connections/{a['id']}/default")
    assert r.status_code == 200 and r.json()[0]["id"] == a["id"]
    await enable_assistant(c, assistant_fields(mock))
    assert (await c.get("/api/assistant/settings")).json()["connection_id"] is None
    # ON + implicit default -> test_required, without and with a foreign / wrong test_id
    r = await c.post(f"/api/aws/connections/{b['id']}/default")
    assert r.status_code == 422 and r.json()["error"]["code"] == "test_required"
    assert r.json()["error"]["details"] == [{"connection_id": b["id"], "used_by": ["assistant"]}]
    # a test of other values (unsaved edits) does not count
    other = await ctest(c, {"connection_id": b["id"], "connection": {"proxy_mode": "env"}})
    assert other["ok"], other
    r = await c.post(f"/api/aws/connections/{b['id']}/default", json={"test_id": other["test_id"]})
    assert r.status_code == 422
    res = await ctest(c, {"connection_id": b["id"]})
    assert res["ok"], res
    r = await c.post(f"/api/aws/connections/{b['id']}/default", json={"test_id": res["test_id"]})
    assert r.status_code == 200, r.text
    assert r.json()[0]["id"] == b["id"] and r.json()[0]["is_default"] is True
    # the test_id is consumed; making the current default the default again needs nothing
    assert (await c.post(f"/api/aws/connections/{b['id']}/default")).status_code == 200
    # an explicit connection_id: the default no longer matters to the assistant
    await c.put("/api/assistant/settings", json={"connection_id": b["id"]})
    t = await c.post("/api/assistant/test", json={"settings": {"connection_id": b["id"]}})
    assert t.json()["ok"], t.json()
    r = await c.put(
        "/api/assistant/settings",
        json={"connection_id": b["id"], "test_id": t.json()["test_id"]},
    )
    assert r.status_code == 200, r.text
    r = await c.post(f"/api/aws/connections/{a['id']}/default")
    assert r.status_code == 200 and r.json()[0]["id"] == a["id"]
    await feats(c, assistant=False)


# ---------------------------------------------------------------- Test: layouts and modes
async def test_layout_a_direct_roles_anywhere_in_the_bedrock_account(
    env: httpx.AsyncClient, mock: Any
) -> None:
    c = env
    a = await create_ra(c, mock)
    res = await ctest(c, {"connection_id": a["id"]})
    assert res["ok"] and res["test_id"], res
    assert steps(res) == [("config", None, True), ("credentials", 0, True), ("identity", 0, True)]
    assert f"source identity {LEAF_SI}" in res["steps"][1]["detail"]
    assert f"account {BEDROCK_ACCT}" in res["steps"][2]["detail"]
    assert "arn_accounts_differ" not in res["warnings"]


async def test_layout_b_network_base_and_assume_role(env: httpx.AsyncClient, mock: Any) -> None:
    c = env
    n = await create_network(c, mock)
    trust_model(mock)
    res = await ctest(c, {"connection_id": n["id"], "access": hop_access()})
    assert res["ok"], res
    assert steps(res) == [
        ("config", None, True),
        ("credentials", 0, True),
        ("identity", 0, True),
        ("hop", 1, True),
        ("hop_identity", 1, True),
    ]
    assert f"account {NET_ACCT}" in res["steps"][2]["detail"]
    assert f"source identity {LEAF_SI}" in res["steps"][3]["detail"]
    assert f"account {BEDROCK_ACCT}" in res["steps"][4]["detail"]
    sent = mock.requests("sts_assume_role")[-1]
    assert sent["sig"]["ok"] and sent["external_id_sent"] == EXT_ID
    assert sent["body"]["DurationSeconds"] == "3600" and "forbidden_param" not in sent
    assert sent["body"]["RoleSessionName"] == "netmon-test"


async def test_access_key_base_with_a_hop_and_ra_role(env: httpx.AsyncClient, mock: Any) -> None:
    c = env
    k = await create_ak(c, mock)
    trust_model(mock, caller=STATIC_ARN, require_source_identity=None)
    res = await ctest(c, {"connection_id": k["id"], "access": hop_access()})
    assert res["ok"] and steps(res)[-1] == ("hop_identity", 1, True), res
    # ra_role: another role of the same profile (a second CreateSession, no STS hop)
    n = await create_network(c, mock)
    mock.config(profile_roles={NET_PROFILE: [NET_BASE_ROLE, NET_OTHER_ROLE]})
    res = await ctest(
        c, {"connection_id": n["id"], "access": {"mode": "ra_role", "role_arn": NET_OTHER_ROLE}}
    )
    assert res["ok"], res
    assert [s for s, _, _ in steps(res)] == [
        "config",
        "credentials",
        "identity",
        "hop",
        "hop_identity",
    ]
    assert "netmon-readonly" in res["steps"][4]["detail"]
    assert mock.requests("create_session")[-1]["body"]["roleArn"] == NET_OTHER_ROLE
    # a role outside the profile account / not listed in the profile
    res = await ctest(
        c, {"connection_id": n["id"], "access": {"mode": "ra_role", "role_arn": BEDROCK_ROLE}}
    )
    assert (
        steps(res)[0] == ("config", None, False) and "profile's account" in res["error"]["message"]
    )
    mock.config(profile_roles={NET_PROFILE: [NET_BASE_ROLE]})
    res = await ctest(
        c, {"connection_id": n["id"], "access": {"mode": "ra_role", "role_arn": NET_OTHER_ROLE}}
    )
    assert steps(res)[-1] == ("hop", 1, False) and res["error"]["code"] == "rolesanywhere_failed"
    assert "ra_profile_role" in hint_codes(res["steps"][-1])
    # ra_role only with a Roles Anywhere connection
    res = await ctest(
        c, {"connection_id": k["id"], "access": {"mode": "ra_role", "role_arn": NET_OTHER_ROLE}}
    )
    assert steps(res)[0] == ("config", None, False)


async def test_unsaved_form_and_consumer_hops(env: httpx.AsyncClient, mock: Any) -> None:
    c = env
    form = {
        "name": "New",
        "auth_mode": "access_key",
        "region": "us-east-1",
        "proxy_mode": "direct",
        "endpoint_overrides": overrides(mock),
        "access_key_id": AKID,
        "secret_access_key": SECRET,
    }
    res = await ctest(c, {"connection_id": None, "connection": form})
    assert res["ok"] and len(res["steps"]) == 3, res
    assert (await c.get("/api/aws/connections")).json() == []  # nothing stored
    # a saved connection without `access`: one hop per distinct access of its consumers
    k = await create_ak(c, mock)
    trust_model(mock, caller=STATIC_ARN, require_source_identity=None)
    r = await c.put(
        "/api/assistant/settings", json={**assistant_fields(mock), "access": hop_access()}
    )
    assert r.status_code == 200, r.text
    res = await ctest(c, {"connection_id": k["id"]})
    assert [s for s, _, _ in steps(res)][-2:] == ["hop", "hop_identity"], res


# ---------------------------------------------------------------- Test: the hint table
async def _hop_fail(c: httpx.AsyncClient, cid: int, access: dict[str, Any]) -> dict[str, Any]:
    res = await ctest(c, {"connection_id": cid, "access": access})
    assert not res["ok"] and res["test_id"] is None, res
    assert steps(res)[:3] == [
        ("config", None, True),
        ("credentials", 0, True),
        ("identity", 0, True),
    ]
    assert steps(res)[-1] == ("hop", 1, False), res
    return res["steps"][-1]  # type: ignore[no-any-return]


async def test_hop_failures_name_the_hop_with_hints(env: httpx.AsyncClient, mock: Any) -> None:
    c = env
    n = await create_network(c, mock)
    cid = n["id"]
    # SetSourceIdentity missing in the caller's permissions policy
    trust_model(mock, caller_allows=["sts:AssumeRole"])
    st = await _hop_fail(c, cid, hop_access())
    assert st["error_code"] == "aws_assume_role_denied"
    assert hint_codes(st) == ["set_source_identity"]
    assert st["hints"][0]["values"] == {
        "source_identity": LEAF_SI,
        "role_arn": BEDROCK_ROLE,
        "from_arn": f"arn:aws:sts::{NET_ACCT}:assumed-role/netmon-base/1234",
    }
    # ... and in the target role's trust policy
    trust_model(mock, trust_allows=["sts:AssumeRole"])
    assert hint_codes(await _hop_fail(c, cid, hop_access())) == ["set_source_identity"]
    # wrong ExternalId
    trust_model(mock)
    st = await _hop_fail(c, cid, hop_access("Wrong-external-id-000"))
    assert hint_codes(st) == [
        "caller_policy",
        "trust_policy",
        "external_id",
        "set_source_identity",
        "scp",
    ]
    assert st["hints"][1]["values"]["base_role_arn"] == NET_BASE_ROLE
    # principal not trusted (the trust policy names another role)
    trust_model(mock, caller=NET_OTHER_ROLE)
    assert hint_codes(await _hop_fail(c, cid, hop_access()))[0] == "caller_policy"
    # unknown role ARN
    trust_model(mock)
    missing = f"arn:aws:iam::{BEDROCK_ACCT}:role/does-not-exist"
    st = await _hop_fail(c, cid, hop_access(role=missing))
    assert st["error_code"] == "aws_assume_role_denied" and hint_codes(st)[0] == "caller_policy"
    # TagSession demanded (transitive tags in the organisation)
    trust_model(mock, require_tag_session=True)
    assert hint_codes(await _hop_fail(c, cid, hop_access())) == ["tag_session"]
    # wrong source identity condition (CN in the trust policy != certificate CN)
    trust_model(mock, require_source_identity="CN=netmonitor.mnidom.net")
    assert hint_codes(await _hop_fail(c, cid, hop_access()))[0] == "caller_policy"
    # STS not activated in the region
    trust_model(mock)
    mock.sts_config(disabled_regions=["us-east-1"])
    res = await ctest(c, {"connection_id": cid, "access": hop_access()})
    # GetCallerIdentity is refused as well: with a hop the identity step is fatal
    assert steps(res)[-1] == ("identity", 0, False) and res["error"]["code"] == (
        "aws_assume_role_invalid"
    )
    assert hint_codes(res["steps"][-1]) == ["sts_region"]


async def test_roles_anywhere_failures_and_sts_unreachable(
    env: httpx.AsyncClient, mock: Any
) -> None:
    c = env
    a = await create_ra(c, mock)
    for mode, codes in (
        (
            "unable_to_assume",
            ["ra_trust_policy", "ra_subject_cn", "ra_issuer_cn", "ra_profile_role"],
        ),
        ("untrusted_cert", ["ra_untrusted_cert"]),
        ("profile_disabled", ["ra_disabled"]),
    ):
        mock.mode(mode, "sessions", times=1)
        res = await ctest(c, {"connection_id": a["id"]})
        assert steps(res)[-1] == ("credentials", 0, False), (mode, res)
        assert hint_codes(res["steps"][-1]) == codes, mode
    vals: dict[str, str] = {}
    mock.mode("unable_to_assume", "sessions", times=1)
    for h in (await ctest(c, {"connection_id": a["id"]}))["steps"][-1]["hints"]:
        vals.update(h["values"])
    assert vals["cert_cn"] == "netmonitor-bedrock.mnidom.net"
    assert (
        vals["issuer_cn"] == "QA Mnidom-PKI-SERV-CA" and vals["trust_anchor_arn"].count("arn:") == 1
    )
    # STS unreachable: only a warning with access direct, fatal with a hop
    dead = [
        {"service": "sts", "region": "us-east-1", "url": "http://127.0.0.1:9"},
        {"service": "rolesanywhere", "region": "us-east-1", "url": mock.url},
    ]
    r = await c.patch(f"/api/aws/connections/{a['id']}", json={"endpoint_overrides": dead})
    assert r.status_code == 200, r.text
    res = await ctest(c, {"connection_id": a["id"]})
    assert res["ok"] and "sts_unreachable" in res["warnings"], res
    assert hint_codes(res["steps"][-1]) == ["vpc_endpoint_or_proxy"]
    res = await ctest(c, {"connection_id": a["id"], "access": hop_access()})
    assert steps(res)[-1] == ("identity", 0, False) and res["error"]["code"] == "aws_network"
    assert res["steps"][-1]["hints"][0]["values"]["host"] == "127.0.0.1"


# ---------------------------------------------------------------- policies
async def test_rendered_policies(env: httpx.AsyncClient, mock: Any) -> None:
    c = env
    n = await create_network(c, mock)
    r = await c.get(
        f"/api/aws/connections/{n['id']}/policies",
        params={"role_arn": BEDROCK_ROLE, "external_id": EXT_ID},
    )
    assert r.status_code == 200, r.text
    p = r.json()
    base = p["base_trust_policy"]["Statement"][0]["Condition"]
    assert base["ArnEquals"]["aws:SourceArn"] == NET_TA and str(p).count(NET_TA) == 1
    assert (
        base["StringEquals"]["aws:PrincipalTag/x509Subject/CN"] == "netmonitor-bedrock.mnidom.net"
    )
    assert base["StringEquals"]["aws:PrincipalTag/x509Issuer/CN"] == "QA Mnidom-PKI-SERV-CA"
    hop = p["hop_trust_policy"]["Statement"][0]
    assert hop["Principal"] == {"AWS": NET_BASE_ROLE}
    assert hop["Condition"]["StringEquals"] == {
        "sts:ExternalId": EXT_ID,
        "aws:SourceIdentity": LEAF_SI,
    }
    assert p["hop_caller_policy"]["Statement"][0]["Resource"] == BEDROCK_ROLE
    r = await c.get(f"/api/aws/connections/{n['id']}/policies", params={"role_arn": "nope"})
    assert r.status_code == 422
    # the policies rendered for the mock's trust model make the hop pass
    trust_model(mock)
    assert (await ctest(c, {"connection_id": n["id"], "access": hop_access()}))["ok"]


# ---------------------------------------------------------------- egress
async def test_egress_save_time_and_connect_time(env: httpx.AsyncClient, mock: Any) -> None:
    c = env
    base = {"name": "E", "auth_mode": "default_chain", "region": "us-east-1"}
    for url in ("https://169.254.169.254", "https://metadata.google.internal", "https://[fe80::1]"):
        ov = [{"service": "sts", "region": "us-east-1", "url": url}]
        r = await c.post("/api/aws/connections", json={**base, "endpoint_overrides": ov})
        assert r.status_code == 422, url
        r = await c.post(
            "/api/aws/connections",
            json={
                **base,
                "proxy_mode": "custom",
                "proxy_url": url.replace("https", "http") + ":3128",
            },
        )
        assert r.status_code == 422, url
    # coordinator decision (PR 4-0a): the DNS check runs off the event loop and is lenient —
    # a name that does not resolve now is only a warning; a name resolving to a blocked
    # address is still refused (and the connect-time guard stays the enforcement)
    from netmon.util import netguard

    unresolved = "https://vpce-0abc-sts.us-east-1.vpce.example.invalid"
    ov = [{"service": "sts", "region": "us-east-1", "url": unresolved}]
    r = await c.post(
        "/api/aws/connections", json={**base, "name": "unresolved", "endpoint_overrides": ov}
    )
    assert r.status_code == 201, r.text
    assert "endpoint_unresolved" in r.json()["warnings"]
    assert r.json()["unresolved_hosts"] == ["vpce-0abc-sts.us-east-1.vpce.example.invalid"]
    r = await c.post(
        "/api/aws/connections",
        json={
            **base,
            "name": "unresolved proxy",
            "proxy_mode": "custom",
            "proxy_url": "http://proxy.example.invalid:3128",
        },
    )
    assert r.status_code == 201, r.text
    assert "endpoint_unresolved" in r.json()["warnings"]

    def fake_resolver(host: str) -> list[Any]:
        if host == "imds-alias.example.net":
            return [(2, 1, 6, "", ("169.254.169.254", 0))]
        raise OSError("no such host")

    monkeypatch_resolver = netguard._default_resolver
    netguard._default_resolver = fake_resolver  # type: ignore[assignment]
    try:
        ov = [{"service": "sts", "region": "us-east-1", "url": "https://imds-alias.example.net"}]
        r = await c.post(
            "/api/aws/connections", json={**base, "name": "alias", "endpoint_overrides": ov}
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["details"][0]["loc"] == ["body", "endpoint_overrides"]
        assert "imds-alias.example.net" in r.json()["error"]["details"][0]["msg"]
    finally:
        netguard._default_resolver = monkeypatch_resolver  # type: ignore[assignment]
    for ok in ("https://10.0.0.5", "https://172.16.4.4:8443"):
        ov = [{"service": "sts", "region": "us-east-1", "url": ok}]
        r = await c.post(
            "/api/aws/connections", json={**base, "name": ok, "endpoint_overrides": ov}
        )
        assert r.status_code == 201, r.text
    # connect time: loopback is refused by the egress guard unless allowed (production)
    from netmon.util import egress

    k = await create_ak(c, mock)
    egress.override_loopback(False)
    try:
        res = await ctest(c, {"connection_id": k["id"]})
    finally:
        egress.override_loopback(True)
    st = res["steps"][-1]
    assert (
        st["step"] == "identity" and "warning" in st["detail"] and "Blocked target" in st["detail"]
    )
    res2_access = hop_access()
    egress.override_loopback(False)
    try:
        res = await ctest(c, {"connection_id": k["id"], "access": res2_access})
    finally:
        egress.override_loopback(True)
    assert res["error"]["code"] == "aws_network" and "Blocked target" in res["error"]["message"]


# ---------------------------------------------------------------- certificates
def _issue(mock: Any, csr_pem: bytes, serial: int) -> x509.Certificate:
    csr = x509.load_pem_x509_csr(csr_pem)
    ca = x509.load_pem_x509_certificate(pki_file(mock, "issuing.pem"))
    ca_key = serialization.load_pem_private_key(pki_file(mock, "issuing.key"), None)
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca.subject)
        .public_key(csr.public_key())
        .serial_number(serial)
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=200))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())  # type: ignore[arg-type]
    )


async def test_certificates_pkcs7_csr_mismatch_cn_and_copy(
    rt: Runtime, env: httpx.AsyncClient, mock: Any
) -> None:
    c = env
    await feats(c, audit=True)
    a = await create_ra(c, mock, "A", cert="")
    # PKCS#7 PEM and DER with their own key
    for name in ("leaf-rsa.p7b", "leaf-rsa.p7b.der"):
        r = await c.post(
            f"/api/aws/connections/{a['id']}/cert",
            files={
                "file": ("c.p7b", pki_file(mock, name)),
                "key": ("c.key", pki_file(mock, "leaf-rsa.key")),
            },
        )
        assert r.status_code == 200, (name, r.text)
        assert r.json()["serial_decimal"] == "4660" and r.json()["chain"]
    # CSR mode: the issued certificate as PKCS#7 without a key
    r = await c.post(
        f"/api/aws/connections/{a['id']}/cert/csr",
        json={"common_name": "NetMonitor Server", "key_type": "ec"},
    )
    assert r.status_code == 200 and r.json()["warnings"] == ["cn_not_source_identity_safe"]
    csr_pem = r.json()["pem"].encode()
    issued = _issue(mock, csr_pem, 0xBEEF)
    from cryptography.hazmat.primitives.serialization import pkcs7

    p7 = pkcs7.serialize_certificates([issued], serialization.Encoding.DER)
    # a certificate issued for another key -> key_mismatch with the CSR / certificate subjects
    r = await c.post(
        f"/api/aws/connections/{a['id']}/cert",
        files={"file": ("x.pem", pki_file(mock, "leaf-ec.pem"))},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "key_mismatch"
    d = r.json()["error"]["details"][0]
    assert d["csr_subject"] == "CN=NetMonitor Server" and d["csr_created_at"]
    assert "netmonitor-bedrock.mnidom.net" in d["cert_subject"]
    r = await c.post(f"/api/aws/connections/{a['id']}/cert", files={"file": ("i.p7b", p7)})
    assert r.status_code == 200, r.text
    info = r.json()
    assert (
        info["subject_cn"] == "NetMonitor Server"
        and info["source_identity"] == "CN=NetMonitor Server"
    )
    assert info["warnings"] == ["cn_not_source_identity_safe"]
    s = (await c.get(f"/api/aws/connections/{a['id']}")).json()
    assert s["csr"] is None and "cn_not_source_identity_safe" in s["warnings"]
    # copy to another connection: decrypted with A's AAD, encrypted with B's
    b = await create_ra(c, mock, "B", cert="")
    r = await c.post(
        f"/api/aws/connections/{b['id']}/cert/copy", json={"from_connection_id": a["id"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["certificate"]["serial_decimal"] == str(0xBEEF)
    from netmon.aws import connections as conns

    async with rt.sessionmaker() as session:
        ra, rb = await conns.get_row(session, a["id"]), await conns.get_row(session, b["id"])
    assert ra and rb and ra["aad_scope"] != rb["aad_scope"]
    assert ra["settings"]["ra_key_enc"] != rb["settings"]["ra_key_enc"]
    va, vb = conns.view(ra), conns.view(rb)
    assert conns.decrypt_field(va, "ra_key_enc", rt.secret_key) == conns.decrypt_field(
        vb, "ra_key_enc", rt.secret_key
    )
    with pytest.raises(conns.Unreadable):
        conns.decrypt_field({**vb, "_aad_scope": ra["aad_scope"]}, "ra_key_enc", rt.secret_key)
    # copy from a connection without certificate -> 422
    k = await create_ak(c, mock, "K")
    r = await c.post(
        f"/api/aws/connections/{b['id']}/cert/copy", json={"from_connection_id": k["id"]}
    )
    assert r.status_code == 422
    d = await c.get(f"/api/aws/connections/{a['id']}/cert/csr")
    assert d.status_code == 404
    acts = {
        i["action"] for i in (await c.get("/api/audit", params={"page_size": 200})).json()["items"]
    }
    assert {"aws_connection.csr_create", "aws_connection.cert_copy"} <= acts


async def test_audit_never_holds_the_external_id(
    rt: Runtime, env: httpx.AsyncClient, mock: Any
) -> None:
    c = env
    await feats(c, audit=True)
    n = await create_network(c, mock)
    trust_model(mock)
    r = await c.put(
        "/api/assistant/settings",
        json={**assistant_fields(mock), "connection_id": n["id"], "access": hop_access()},
    )
    assert r.status_code == 200 and r.json()["access"]["external_id"] == EXT_ID  # admins see it
    await ctest(c, {"connection_id": n["id"], "access": hop_access()})
    items = (await c.get("/api/audit", params={"page_size": 200})).json()["items"]
    assert EXT_ID not in str(items) and "external_id_set" in str(items)
    assert SECRET not in str(items)
