"""[3b] Shared fixtures of the agent / tools / injection tests (developer B): the QA Bedrock mock
(`tests/sim/bedrock`), the assistant switched ON against it, SSE helpers and a small inventory.

Registered as a pytest plugin in `tests/conftest.py`; fixtures: `b_mock_session`, `b_mock`,
`asst` (helpers are imported normally).
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

SIM = Path(__file__).resolve().parents[2] / "tests" / "sim" / "bedrock"
if str(SIM) not in sys.path:
    sys.path.insert(0, str(SIM))
if str(SIM / "injection") not in sys.path:
    sys.path.insert(0, str(SIM / "injection"))

from mock_bedrock import STATIC_KEYS, MockBedrock  # noqa: E402

MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


@pytest.fixture(scope="session")
def b_mock_session(tmp_path_factory: pytest.TempPathFactory) -> Any:
    m = MockBedrock(pki_dir=str(tmp_path_factory.mktemp("bedrock-pki-b")))
    m.start()
    yield m
    m.stop()


@pytest.fixture
def b_mock(b_mock_session: Any) -> Any:
    b_mock_session.reset()
    return b_mock_session


def mock_settings(mock: Any, **over: Any) -> dict[str, Any]:
    ak, sk = next(iter(STATIC_KEYS.items()))
    s: dict[str, Any] = {
        "auth_mode": "access_key",
        "region": "us-east-1",
        "model_id": MODEL,
        "model_id_light": None,
        "endpoint_url": mock.url,
        "control_endpoint_url": mock.url,
        "proxy_mode": "direct",
        "proxy_url": None,
        "prompt_caching": True,
        "access_key_id": ak,
        "secret_access_key": sk,
    }
    s.update(over)
    return s


async def configure(rt: Any, fields: dict[str, Any], **features: bool) -> None:
    """Store the AWS connection "Default" and the `assistant` object (secrets encrypted) and
    switch the features (bypassing the configure-first test_id rule, which is developer A's and
    tested there). [4] The authentication fields go to the shared connection."""
    from netmon.assistant import config
    from netmon.services import settings as settings_svc
    from tests.aws_helpers import split, store_connection

    conn_fields, asst_fields = split(fields)
    cid = await store_connection(rt, conn_fields)
    async with rt.sessionmaker() as session:
        stored = await config.load(session)
        cfg = config.merge_settings(stored, {**asst_fields, "connection_id": cid})
        await settings_svc.put(session, "assistant", cfg)
        await session.commit()
    await rt.features.apply({"audit": True, "assistant": True, **features})
    await rt.assistant.reload()


@pytest.fixture
async def asst(rt: Any, admin: httpx.AsyncClient, b_mock: Any) -> AsyncIterator[Any]:
    """The assistant ON against the mock; yields the mock. The admin client is logged in."""
    from netmon.assistant import agent

    rt.settings.netmon_test_mode = True
    agent.runs_of(rt).clear()
    await configure(rt, mock_settings(b_mock))
    yield b_mock
    await agent.runs_of(rt).cancel_all()
    await rt.drain()


# --------------------------------------------------------------------------- SSE helpers
def parse_sse(raw: str) -> list[tuple[int | None, str, Any]]:
    out: list[tuple[int | None, str, Any]] = []
    for block in raw.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        eid: int | None = None
        event = "message"
        data = ""
        for line in block.split("\n"):
            if line.startswith("id: "):
                eid = int(line[4:])
            elif line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data += line[6:]
        out.append((eid, event, json.loads(data) if data else None))
    return out


async def ask(c: httpx.AsyncClient, question: str, conversation_id: int | None = None) -> Any:
    """POST /api/assistant/ask and read the whole SSE stream → (status, events)."""
    body = {"conversation_id": conversation_id, "question": question}
    async with c.stream("POST", "/api/assistant/ask", json=body, timeout=60) as r:
        raw = (await r.aread()).decode()
        if r.status_code != 200:
            return r.status_code, json.loads(raw)
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-store"
        return 200, parse_sse(raw)


def done_message(events: list[tuple[int | None, str, Any]]) -> dict[str, Any]:
    done = [d for _, e, d in events if e == "done"]
    assert len(done) == 1, [e for _, e, _ in events]
    return done[0]["message"]  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- inventory
async def seed(c: httpx.AsyncClient, rt: Any) -> dict[str, Any]:
    """core-sw (down ping), access-sw (up) linked by topology; returns ids."""
    from tests.conftest import make_device, make_sensor

    core = await make_device(c, name="core-sw", host="10.0.0.9")
    acc = await make_device(c, name="access-sw", host="10.0.0.10")
    s1 = await make_sensor(c, core["id"], "ping")
    s2 = await make_sensor(c, acc["id"], "ping")
    async with rt.sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE sensors SET status = 'down', status_message = 'Request timed out', "
                "last_status_change_at = now() - interval '5 minutes' WHERE id = :s"
            ),
            {"s": s1["id"]},
        )
        await session.execute(
            text("UPDATE sensors SET status = 'up' WHERE id = :s"), {"s": s2["id"]}
        )
        await session.execute(
            text(
                "INSERT INTO topology_links (source, a_device_id, a_port, b_device_id, b_port, "
                "a_if_index, b_if_index) VALUES ('lldp', :a, 'Gi1/0/1', :b, 'Gi0/48', 1, 48)"
            ),
            {"a": core["id"], "b": acc["id"]},
        )
        await session.commit()
    return {"core": core, "access": acc, "core_ping": s1, "access_ping": s2}
