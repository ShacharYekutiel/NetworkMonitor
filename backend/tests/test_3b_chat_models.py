"""[3b owner] Usage per model and the model choice in the chat (api.md § [3b] Ask / AI features
`UsageReport.by_model`, data-model § [3b] Settings `chat_models`, `assistant_usage.model_id`)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text

from netmon.assistant import config, usage
from netmon.schemas.assistant import AssistantSettingsIn
from tests.conftest import requires_db
from tests.fixtures_3b_agent import MODEL, configure, done_message, mock_settings, parse_sse, seed

LIGHT = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
OTHER = "us.anthropic.claude-opus-4-1-20250805-v1:0"


# --------------------------------------------------------------------------- pure
def test_chat_models_effective_defaults_and_order() -> None:
    assert config.chat_models_effective({"model_id": ""}) == []
    assert config.chat_models_effective({"model_id": MODEL}) == [{"id": MODEL, "label": None}]
    assert config.chat_models_effective({"model_id": MODEL, "model_id_light": LIGHT}) == [
        {"id": MODEL, "label": None},
        {"id": LIGHT, "label": None},
    ]
    # light == main: listed once
    assert len(config.chat_models_effective({"model_id": MODEL, "model_id_light": MODEL})) == 1
    # configured: the main model always first (default), labels kept, light not added
    cfg = {
        "model_id": MODEL,
        "model_id_light": LIGHT,
        "chat_models": [{"id": OTHER, "label": "Big"}, {"id": MODEL, "label": "Main"}],
    }
    assert config.chat_models_effective(cfg) == [
        {"id": MODEL, "label": "Main"},
        {"id": OTHER, "label": "Big"},
    ]


def test_chat_models_validation() -> None:
    assert config.chat_models_error([]) is None
    assert config.chat_models_error([{"id": MODEL, "label": None}]) is None
    assert "at most 8" in str(config.chat_models_error([{"id": f"m{i}"} for i in range(9)]))
    assert "duplicate" in str(config.chat_models_error([{"id": MODEL}, {"id": MODEL}]))
    assert "invalid" in str(config.chat_models_error([{"id": "bad id!"}]))
    assert "label" in str(config.chat_models_error([{"id": MODEL, "label": "x" * 61}]))
    # request model
    body = AssistantSettingsIn.model_validate(
        {"chat_models": [{"id": f" {LIGHT} ", "label": "  Fast   one "}, {"id": MODEL}]}
    )
    assert body.changes()["chat_models"] == [
        {"id": LIGHT, "label": "Fast one"},
        {"id": MODEL, "label": None},
    ]
    assert AssistantSettingsIn.model_validate({"chat_models": None}).changes() == {
        "chat_models": []
    }
    for bad in (
        [{"id": f"m{i}"} for i in range(9)],
        [{"id": MODEL}, {"id": MODEL}],
        [{"id": "no spaces allowed"}],
        [{"id": MODEL, "label": "x" * 61}],
        [{"id": MODEL, "extra": 1}],
    ):
        with pytest.raises(ValidationError):
            AssistantSettingsIn.model_validate({"chat_models": bad})


def _row(mid: str, calls: int, i: int, o: int, cr: int = 0, c: float | None = 0.0) -> dict:
    return {"model_id": mid, "calls": calls, "input_tokens": i, "output_tokens": o,
            "cache_read_tokens": cr, "cache_write_tokens": 0, "c": c}  # fmt: skip


def test_by_model_rows_sort_cost_and_unknown() -> None:
    rows = [
        _row(LIGHT, 4, 1000, 100, c=0.0015),
        _row(MODEL, 2, 5000, 500, cr=100, c=0.0225),
        _row("", 0, 0, 0, c=0),  # question counters only: left out
        _row(OTHER, 0, 0, 0, c=0),  # errors only: left out
        _row("", 0, 300, 0, c=0.0009),  # tokens without a model (unknown): cost null
    ]
    out = usage.by_model_rows(rows, has_cost=True)
    assert [r["model_id"] for r in out] == [MODEL, LIGHT, ""]
    assert out[0]["tokens"] == 5000 + 500 + 10 and out[0]["calls"] == 2
    assert out[0]["cache_read_tokens"] == 100 and out[0]["cost_estimate"] == pytest.approx(0.0225)
    assert out[1]["cost_estimate"] == pytest.approx(0.0015)
    assert out[2]["cost_estimate"] is None
    # no prices: every cost null
    assert all(r["cost_estimate"] is None for r in usage.by_model_rows(rows, has_cost=False))
    # ties by model id
    tie = usage.by_model_rows([_row("b", 1, 10, 0), _row("a", 1, 10, 0)], has_cost=False)
    assert [r["model_id"] for r in tie] == ["a", "b"]


# --------------------------------------------------------------------------- API (mock)


async def _ask(c: httpx.AsyncClient, question: str, model_id: str | None = None) -> Any:
    body: dict[str, Any] = {"conversation_id": None, "question": question}
    if model_id is not None:
        body["model_id"] = model_id
    async with c.stream("POST", "/api/assistant/ask", json=body, timeout=60) as r:
        raw = (await r.aread()).decode()
        if r.status_code != 200:
            return r.status_code, json.loads(raw)
        return 200, parse_sse(raw)


async def _wait_idle(rt: Any) -> None:
    await rt.drain()


@requires_db
async def test_chat_models_settings_status_and_ask(
    rt: Any, admin: httpx.AsyncClient, asst: Any
) -> None:
    await seed(admin, rt)
    await configure(rt, mock_settings(asst, model_id_light=LIGHT))
    # default list: main + light
    s = (await admin.get("/api/assistant/settings")).json()
    assert s["chat_models"] == []
    assert [m["id"] for m in s["chat_models_effective"]] == [MODEL, LIGHT]
    st = (await admin.get("/api/assistant/status")).json()
    assert [m["id"] for m in st["chat_models"]] == [MODEL, LIGHT]
    assert st["default_model_id"] == MODEL

    # editing the list while ON needs no new connection test (not a connection field)
    r = await admin.put(
        "/api/assistant/settings",
        json={"chat_models": [{"id": LIGHT, "label": "Fast"}, {"id": OTHER, "label": None}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["chat_models"] == [{"id": LIGHT, "label": "Fast"}, {"id": OTHER, "label": None}]
    eff = r.json()["chat_models_effective"]
    assert eff == [{"id": MODEL, "label": None}, {"id": LIGHT, "label": "Fast"},
                   {"id": OTHER, "label": None}]  # fmt: skip
    r = await admin.put("/api/assistant/settings", json={"chat_models": [{"id": "x y"}]})
    assert r.status_code == 422
    r = await admin.put(
        "/api/assistant/settings", json={"chat_models": [{"id": f"m{i}"} for i in range(9)]}
    )
    assert r.status_code == 422
    # a changed main model still needs a test (rule unchanged)
    r = await admin.put("/api/assistant/settings", json={"model_id": OTHER})
    assert r.status_code == 422 and r.json()["error"]["code"] == "assistant_test_required"

    # not allowed → 422 model_not_allowed, nothing sent to Bedrock
    n = len(asst.requests("converse_stream"))
    status, body = await _ask(admin, "Which devices are down?", "us.anthropic.claude-nope-v1:0")
    assert status == 422 and body["error"]["code"] == "model_not_allowed"
    assert body["error"]["details"][0]["allowed"] == [MODEL, LIGHT, OTHER]
    assert len(asst.requests("converse_stream")) == n

    # the chosen model reaches every model call of the answer (tool loop included)
    status, events = await _ask(admin, "Which devices are down?", LIGHT)
    assert status == 200
    msg = done_message(events)
    assert msg["status"] == "complete" and msg["model_id"] == LIGHT and msg["model_calls"] == 2
    reqs = asst.requests("converse_stream")[n:]
    assert len(reqs) == 2 and {r["model_id"] for r in reqs} == {LIGHT}
    await _wait_idle(rt)
    # the title stays on the light model; audit and usage record the chosen model
    titles = [r for r in asst.requests("converse") if r["purpose"] == "title"]
    assert titles and titles[-1]["model_id"] == LIGHT
    async with rt.sessionmaker() as session:
        ans = (await session.execute(text(
            "SELECT changes FROM audit_log WHERE action = 'assistant.answer' ORDER BY id DESC "
            "LIMIT 1"))).scalar()  # fmt: skip
        rows = (await session.execute(text(
            "SELECT model_id, sum(model_calls) m FROM assistant_usage WHERE purpose = 'ask' "
            "GROUP BY model_id"))).all()  # fmt: skip
    assert ans["model_id"][1] == LIGHT
    assert {r.model_id: int(r.m) for r in rows}.get(LIGHT) == 2

    # without model_id → the main model
    status, events = await _ask(admin, "Which devices are down?")
    assert status == 200 and done_message(events)["model_id"] == MODEL
    assert asst.requests("converse_stream")[-1]["model_id"] == MODEL
    # the main model is always allowed explicitly too
    status, events = await _ask(admin, "Which devices are down?", MODEL)
    assert status == 200 and done_message(events)["model_id"] == MODEL
    await _wait_idle(rt)


@requires_db
async def test_usage_report_by_model(rt: Any, admin: httpx.AsyncClient, asst: Any) -> None:
    await seed(admin, rt)
    await configure(rt, mock_settings(asst, model_id_light=LIGHT))
    # no prices: tokens per model, cost null
    status, _ = await _ask(admin, "Which devices are down?")
    assert status == 200
    await _wait_idle(rt)
    rep = (await admin.get("/api/assistant/usage")).json()
    bm = {m["model_id"]: m for m in rep["by_model"]}
    assert set(bm) == {MODEL, LIGHT}  # main (ask) + light (title)
    assert bm[MODEL]["calls"] == 2 and bm[MODEL]["input_tokens"] > 0
    assert bm[LIGHT]["calls"] == 1 and bm[LIGHT]["output_tokens"] == 5
    assert all(m["cost_estimate"] is None for m in rep["by_model"])
    assert [m["tokens"] for m in rep["by_model"]] == sorted(
        (m["tokens"] for m in rep["by_model"]), reverse=True
    )
    assert sum(m["tokens"] for m in rep["by_model"]) == rep["tokens"]
    for m in rep["by_model"]:
        assert set(m) == {"model_id", "calls", "tokens", "cost_estimate", *usage.TOKEN_KEYS}

    # with prices: the recorded estimates per model
    r = await admin.put(
        "/api/assistant/settings", json={"prices": {"input_per_mtok": 3, "output_per_mtok": 15}}
    )
    assert r.status_code == 200
    await rt.assistant.usage.record(0, "digest", {"input_tokens": 1_000_000}, model_id=LIGHT)
    rep = (await admin.get("/api/assistant/usage")).json()
    bm = {m["model_id"]: m for m in rep["by_model"]}
    assert rep["by_model"][0]["model_id"] == LIGHT  # now the largest
    assert bm[LIGHT]["cost_estimate"] == pytest.approx(3.0)
    assert bm[MODEL]["cost_estimate"] == 0.0  # counted before prices were set
    # another month: empty
    assert (await admin.get("/api/assistant/usage", params={"month": "2025-01"})).json()[
        "by_model"
    ] == []
