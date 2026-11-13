"""[4] Shared helpers of the PR 4-0a tests: the Bedrock + Roles Anywhere + STS mock
(`tests/sim/bedrock/mock_bedrock.py` with `tests/sim/aws/mock_sts.py`), the two owner layouts and
connection builders over the API.

Layout (a) — today's: Roles Anywhere in the Bedrock account (111122223333), assistant `direct`.
Layout (b) — the owner's target: Roles Anywhere in a network account (444455556666, base role
`netmon-base`), the assistant `assume_role` into the Bedrock account (`netmon-bedrock`)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

SIM = Path(__file__).resolve().parents[2] / "tests" / "sim" / "bedrock"
if str(SIM) not in sys.path:
    sys.path.insert(0, str(SIM))

from mock_bedrock import ACCOUNT, ROLE_ARN, STATIC_KEYS, MockBedrock  # noqa: E402
from mock_sts import STATIC_USER  # noqa: E402

MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
AKID, SECRET = next(iter(STATIC_KEYS.items()))
BEDROCK_ACCT = ACCOUNT
BEDROCK_ROLE = ROLE_ARN
NET_ACCT = "444455556666"
NET_TA = (
    f"arn:aws:rolesanywhere:us-east-1:{NET_ACCT}:trust-anchor/9a1b2c3d-0000-4000-8000-000000000001"
)
NET_PROFILE = (
    f"arn:aws:rolesanywhere:us-east-1:{NET_ACCT}:profile/9a1b2c3d-0000-4000-8000-000000000002"
)
NET_BASE_ROLE = f"arn:aws:iam::{NET_ACCT}:role/netmon-base"
NET_OTHER_ROLE = f"arn:aws:iam::{NET_ACCT}:role/netmon-readonly"
EXT_ID = "Qx7Rm2Lp9Vn4Tc8Wb1Zd6Hk3Js5Fg0Ay"
LEAF_SI = "CN=netmonitor-bedrock.mnidom.net"
STATIC_ARN = STATIC_USER


def start_mock(tmp_path_factory: pytest.TempPathFactory) -> Any:
    m = MockBedrock(pki_dir=str(tmp_path_factory.mktemp("p4-pki")))
    m.start()
    return m


async def env_setup(rt: Any, mock: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mock.reset()
    monkeypatch.setattr(rt.settings, "netmon_test_mode", True)
    for k in [k for k in os.environ if k.startswith("AWS_")]:
        monkeypatch.delenv(k, raising=False)
    for k in ("NETMON_AWS_RA_CERT_FILE", "NETMON_AWS_RA_KEY_FILE", "NETMON_NODE_NAME"):
        monkeypatch.delenv(k, raising=False)
    rt.aws.tests.clear()
    rt.aws.invalidate()
    rt.assistant.state_error = None
    await rt.assistant.load()


def pki_file(mock: Any, name: str) -> bytes:
    return Path(mock.pki["files"][name]).read_bytes()


def overrides(mock: Any, region: str = "us-east-1") -> list[dict[str, str]]:
    return [
        {"service": "sts", "region": region, "url": mock.url},
        {"service": "rolesanywhere", "region": region, "url": mock.url},
    ]


async def create(c: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    r = await c.post("/api/aws/connections", json=body)
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def create_ak(c: httpx.AsyncClient, mock: Any, name: str = "Keys", **kw: Any) -> dict:
    return await create(
        c,
        {
            "name": name,
            "auth_mode": "access_key",
            "region": "us-east-1",
            "proxy_mode": "direct",
            "endpoint_overrides": overrides(mock),
            "access_key_id": AKID,
            "secret_access_key": SECRET,
            **kw,
        },
    )


async def create_ra(
    c: httpx.AsyncClient,
    mock: Any,
    name: str = "Default",
    *,
    ta: str | None = None,
    profile: str | None = None,
    role: str | None = None,
    cert: str = "leaf-rsa",
    **kw: Any,
) -> dict:
    out = await create(
        c,
        {
            "name": name,
            "auth_mode": "roles_anywhere",
            "region": "us-east-1",
            "proxy_mode": "direct",
            "endpoint_overrides": overrides(mock),
            "ra_trust_anchor_arn": ta or MockBedrock.ra_arns["trust_anchor"],
            "ra_profile_arn": profile or MockBedrock.ra_arns["profile"],
            "ra_role_arn": role or MockBedrock.ra_arns["role"],
            **kw,
        },
    )
    if cert:
        r = await c.post(
            f"/api/aws/connections/{out['id']}/cert",
            files={
                "file": ("c.pem", pki_file(mock, f"{cert}.pem")),
                "key": ("c.key", pki_file(mock, f"{cert}.key")),
            },
        )
        assert r.status_code == 200, r.text
        out = (await c.get(f"/api/aws/connections/{out['id']}")).json()
    return out


async def create_network(c: httpx.AsyncClient, mock: Any, name: str = "Network account") -> dict:
    """Layout (b): Roles Anywhere in the network account."""
    return await create_ra(c, mock, name, ta=NET_TA, profile=NET_PROFILE, role=NET_BASE_ROLE)


def trust_model(
    mock: Any,
    *,
    caller: str = NET_BASE_ROLE,
    external_id: str | None = EXT_ID,
    caller_allows: list[str] | None = None,
    trust_allows: list[str] | None = None,
    require_source_identity: str | None = LEAF_SI,
    **role_kw: Any,
) -> None:
    """The Bedrock-account role trusts `caller` (ExternalId, source identity)."""
    mock.sts_config(
        reset=True,
        roles=[
            {
                "arn": BEDROCK_ROLE,
                "trusted_principals": [caller],
                "external_id": external_id,
                "trust_allows": trust_allows or ["sts:AssumeRole", "sts:SetSourceIdentity"],
                "require_source_identity": require_source_identity,
                **role_kw,
            }
        ],
        callers=[
            {
                "arn": caller,
                "allows": caller_allows or ["sts:AssumeRole", "sts:SetSourceIdentity"],
                "resources": [BEDROCK_ROLE],
            }
        ],
    )


def hop_access(external_id: str | None = EXT_ID, role: str = BEDROCK_ROLE) -> dict[str, Any]:
    return {"mode": "assume_role", "role_arn": role, "external_id": external_id}


def assistant_fields(mock: Any, **kw: Any) -> dict[str, Any]:
    return {
        "region": "us-east-1",
        "model_id": MODEL,
        "endpoint_url": mock.url,
        "control_endpoint_url": mock.url,
        **kw,
    }


async def feats(c: httpx.AsyncClient, **kw: bool) -> None:
    r = await c.put("/api/settings", json={f"feature_{k}_enabled": v for k, v in kw.items()})
    assert r.status_code == 200, r.text


async def enable_assistant(c: httpx.AsyncClient, settings: dict[str, Any]) -> dict[str, Any]:
    await feats(c, audit=True)
    r = await c.post("/api/assistant/test", json={"settings": settings})
    res = r.json()
    assert res["ok"], res
    r = await c.put(
        "/api/assistant/settings", json={**settings, "enabled": True, "test_id": res["test_id"]}
    )
    assert r.status_code == 200, r.text
    return res  # type: ignore[no-any-return]


def steps(res: dict[str, Any]) -> list[tuple[str, Any, bool]]:
    return [(s["step"], s["hop"], s["ok"]) for s in res["steps"]]


def hint_codes(step: dict[str, Any]) -> list[str]:
    return [h["code"] for h in step["hints"]]
