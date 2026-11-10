"""pytest fixtures for backend tests against the Bedrock mock.

In backend/tests/conftest.py (or a test module):

    import sys; from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "sim" / "bedrock"))
    from pytest_fixture import bedrock_mock, bedrock_mock_session, assistant_settings  # noqa: F401  (fixtures)

    def test_ask(bedrock_mock, assistant_settings):
        settings = assistant_settings("access_key")          # dict for PUT /api/assistant/settings
        bedrock_mock.mode("throttle:1")
        ...
        assert bedrock_mock.requests("converse_stream")[0]["sig"]["ok"]

`bedrock_mock` is one server per test session, reset before every test. Backends must run with
NETMON_TEST_MODE=1 (http:// endpoint URLs are only accepted then). For TLS start a separate
`MockBedrock(https=True, pki_dir=...)` and trust its `pki["files"]["trust.pem"]` (NETMON_EXTRA_CA_FILE).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from mock_bedrock import ACCOUNT, STATIC_KEYS, MockBedrock  # noqa: E402

DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
LIGHT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
IL_MODEL = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"


@pytest.fixture(scope="session")
def bedrock_mock_session(tmp_path_factory) -> MockBedrock:
    m = MockBedrock(pki_dir=str(tmp_path_factory.mktemp("bedrock-pki")))
    m.start()
    yield m
    m.stop()


@pytest.fixture()
def bedrock_mock(bedrock_mock_session: MockBedrock) -> MockBedrock:
    bedrock_mock_session.reset()
    return bedrock_mock_session


@pytest.fixture()
def assistant_settings(bedrock_mock: MockBedrock) -> Callable[..., dict[str, Any]]:
    """assistant_settings(auth_mode, **overrides) -> the `assistant` settings object pointing at the mock."""

    def make(auth_mode: str = "access_key", **over: Any) -> dict[str, Any]:
        ak, sk = next(iter(STATIC_KEYS.items()))
        s: dict[str, Any] = {
            "auth_mode": auth_mode, "region": "us-east-1", "model_id": DEFAULT_MODEL, "model_id_light": None,
            "endpoint_url": bedrock_mock.url, "control_endpoint_url": bedrock_mock.url, "proxy_mode": "direct",
            "proxy_url": None, "prompt_caching": True,
        }
        if auth_mode == "access_key":
            s.update({"access_key_id": ak, "secret_access_key": sk})
        elif auth_mode == "roles_anywhere":
            s.update({"ra_region": "us-east-1", "ra_endpoint_url": bedrock_mock.url,
                      "ra_trust_anchor_arn": MockBedrock.ra_arns["trust_anchor"],
                      "ra_profile_arn": MockBedrock.ra_arns["profile"], "ra_role_arn": MockBedrock.ra_arns["role"],
                      "ra_session_seconds": 3600})
        s.update(over)
        return s

    return make


def ra_upload_files(m: MockBedrock, leaf: str = "leaf-rsa", pfx: bool = False) -> dict[str, Any]:
    """Multipart parts for POST /api/assistant/cert: {"file": (name, bytes), "key": ..., "password": ...}."""
    f = m.pki["files"]
    if pfx:
        return {"file": (f"{leaf}.pfx", Path(f[f"{leaf}.pfx"]).read_bytes()), "password": m.pki["pfx_password"]}
    return {"file": (f"{leaf}.pem", Path(f[f"{leaf}.pem"]).read_bytes()),
            "key": (f"{leaf}.key", Path(f[f"{leaf}.key"]).read_bytes())}


__all__ = ["ACCOUNT", "DEFAULT_MODEL", "IL_MODEL", "LIGHT_MODEL", "MockBedrock", "assistant_settings", "bedrock_mock",
           "bedrock_mock_session", "ra_upload_files"]
