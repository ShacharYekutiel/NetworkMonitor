"""Template-draft SNMP walk (§ 14.8.7): a budget per subtree (standard subtrees first, the vendor
subtree last) with partial results and a per-subtree outcome, and the real SNMP error (auth
failure / unreachable / timeout) instead of a generic "timeout".

Unit tests need no database; the walk tests run the real walker against snmpsim agents on
127.0.14.x (the HPE Comware recording of the E2E lab, and an SNMPv3 authNoPriv agent)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from netmon.assistant.features import templates as tmod
from netmon.assistant.features import templates_pure as tp
from netmon.poller.snmp_client import SnmpError, snmp_get
from tests.conftest import make_device, requires_db
from tests.snmpsim_agents import snmpsim_exe, start_agent, system, write_snmprec

COMWARE_REC = Path(__file__).parents[2] / "tests" / "sim" / "hpe" / "comware" / "public.snmprec"
COMWARE_IP, V3_IP, DEAD_IP, PORT = "127.0.14.10", "127.0.14.11", "127.0.14.12", 1161
H3C = "1.3.6.1.4.1.25506"


# ------------------------------------------------------------------ pure
@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("Wrong SNMP PDU digest", "auth_failed"),
        ("Unknown USM user", "auth_failed"),
        ("Unsupported SNMP security level", "auth_failed"),
        ("SNMP error: authorizationError", "auth_failed"),
        ("SNMP error: noAccess", "auth_failed"),
        ("No SNMP response received before timeout", "unreachable"),
        ("[Errno 101] Network is unreachable", "unreachable"),
        ("SNMP error: genErr", "snmp_error"),
        ("", "snmp_error"),
    ],
)
def test_classify_snmp_error(message: str, code: str) -> None:
    assert tp.classify_snmp_error(message) == code


def test_order_and_budget() -> None:
    ent, std = f"{H3C}", "1.3.6.1.2.1.1"
    order = tp.order_subtrees([ent, std, "1.3.6.1.2.1.47.1.1.1", "1.3.6.1.4.1.9.9"])
    assert order == [std, "1.3.6.1.2.1.47.1.1.1", ent, "1.3.6.1.4.1.9.9"]
    assert tp.subtree_budget(std) == tp.SUBTREE_TIMEOUT_S
    assert tp.subtree_budget(ent) == tp.VENDOR_SUBTREE_TIMEOUT_S
    # each budget fits in the overall cap with room for the standard subtrees before it
    assert tp.VENDOR_SUBTREE_TIMEOUT_S + tp.SUBTREE_TIMEOUT_S <= tp.WALK_TIMEOUT_S


def test_walk_summary() -> None:
    ok = {"subtree": "a", "status": "ok", "error_code": None, "error": None}
    trunc = {**ok, "status": "truncated"}
    tout = {"subtree": "b", "status": "timeout", "error_code": "timeout", "error": "slow"}
    auth = {"subtree": "a", "status": "error", "error_code": "auth_failed", "error": "digest"}
    skip = {"subtree": "c", "status": "skipped", "error_code": "auth_failed", "error": "x"}
    assert tp.walk_summary([ok, trunc], 10) == ("ok", None, None)
    assert tp.walk_summary([ok, tout], 10) == ("partial", "timeout", "slow")
    assert tp.walk_summary([auth, skip], 0) == ("failed", "auth_failed", "digest")


# ------------------------------------------------------------------ agents
def _records(path: Path) -> list[tuple[str, object, str]]:
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            o, t, v = line.split("|", 2)
            out.append((o, t, v))
    return out


@pytest.fixture(scope="module")
def comware_agent() -> Iterator[Any]:
    if snmpsim_exe() is None:
        pytest.skip("snmpsim not installed")
    agent = start_agent(COMWARE_IP, PORT, {"public": _records(COMWARE_REC)})
    if agent is None:
        pytest.skip("snmpsim agent did not start")
    yield agent
    agent.stop()


V3_USER, V3_KEY = "netmon", "NetmonAuth2026"


@pytest.fixture(scope="module")
def v3_agent() -> Iterator[Any]:
    """An SNMPv3 authNoPriv (SHA) agent; `public` (v2c) is only used to see it is up."""
    exe = snmpsim_exe()
    if exe is None:
        pytest.skip("snmpsim not installed")
    data = Path(tempfile.mkdtemp(prefix="netmon-v3walk-"))
    data.chmod(0o755)
    recs = system("Comware v3", f"{H3C}.1.1.42", "sw-v3")
    write_snmprec(data / "self.snmprec", recs)
    write_snmprec(data / "public.snmprec", recs)
    cache = data / "cache"
    cache.mkdir()
    args = [
        exe, f"--data-dir={data}", f"--cache-dir={cache}", f"--agent-udpv4-endpoint={V3_IP}:{PORT}",
        f"--v3-user={V3_USER}", "--v3-auth-proto=SHA", f"--v3-auth-key={V3_KEY}",
    ]  # fmt: skip
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        shutil.chown(cache, "nobody")
        args += ["--process-user=nobody", "--process-group=nogroup"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603

    async def ready() -> bool:
        creds = {"version": "v2c", "port": PORT, "timeout_s": 1, "retries": 0}
        try:
            await snmp_get(V3_IP, {**creds, "community": "public"}, ["1.3.6.1.2.1.1.5.0"])
            return True
        except SnmpError:
            return False

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and proc.poll() is None:
        if asyncio.run(ready()):
            break
    else:
        proc.kill()
        shutil.rmtree(data, ignore_errors=True)
        pytest.skip("snmpsim v3 agent did not start")
    yield proc
    proc.terminate()
    proc.wait(10)
    shutil.rmtree(data, ignore_errors=True)


def v3_creds(**over: Any) -> dict[str, Any]:
    return {
        "version": "v3", "port": PORT, "timeout_s": 1, "retries": 0,
        "v3_username": V3_USER, "v3_security_level": "authNoPriv",
        "v3_auth_protocol": "SHA", "v3_auth_key": V3_KEY, **over,
    }  # fmt: skip


async def _walk(admin: httpx.AsyncClient, rt: Any, device_id: int, presets: list[str]) -> Any:
    r = await admin.post(
        "/api/assistant/template-drafts/walks", json={"device_id": device_id, "presets": presets}
    )
    assert r.status_code == 202, r.text
    await rt.ai_features.templates.idle()
    w = (await admin.get(f"/api/assistant/template-drafts/walks/{r.json()['id']}")).json()
    assert w["status"] != "running"
    return w


@pytest.fixture
def real_walker(rt: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime is shared by the session and other tests replace the walker: use the real one."""
    tpl = rt.ai_features.templates
    monkeypatch.setattr(tpl, "walker", tmod.typed_walk)
    monkeypatch.setattr(tpl, "sys_object_id", None)


def by_subtree(w: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {o["subtree"]: o for o in w["outcomes"]}


# ------------------------------------------------------------------ walks
@requires_db
async def test_slow_vendor_subtree_does_not_starve_the_standard_ones(
    rt: Any, admin: httpx.AsyncClient, asst: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vendor subtree is walked last with its own budget; when it runs out, what it
    collected is kept and the walk is partial with outcome `timeout` (was: one shared budget,
    the standard subtrees behind it never walked, the subtree's rows dropped)."""
    await rt.features.apply({"ai_template_drafts": True})
    monkeypatch.setattr(tp, "VENDOR_SUBTREE_TIMEOUT_S", 0.5)
    tpl = rt.ai_features.templates
    walked: list[str] = []

    async def walker(host, creds, base, max_rows, deadline, sink=None):  # noqa: ANN001
        walked.append(base)
        sink = sink if sink is not None else []
        if base == "1.3.6.1.2.1.1":
            sink.append(("1.3.6.1.2.1.1.2.0", "ObjectIdentifier", f"{H3C}.1.1.42"))
            return sink
        if base == H3C:
            sink.extend((f"{H3C}.2.{i}.0", "Integer", str(i)) for i in range(1, 4))
            await asyncio.sleep(30)  # a huge vendor subtree
        sink.append((f"{base}.1.1.1", "Integer", "1"))
        return sink

    monkeypatch.setattr(tpl, "walker", walker)
    dev = await make_device(admin, name="slow-vendor", host="127.0.14.20")
    t0 = time.monotonic()
    # the UI order puts enterprise before if_sample / entity_sensor
    w = await _walk(admin, rt, dev["id"], ["system", "enterprise", "if_sample", "entity_sensor"])
    assert time.monotonic() - t0 < 10
    assert walked == [
        "1.3.6.1.2.1.1", "1.3.6.1.2.1.2.2", "1.3.6.1.2.1.31.1.1", "1.3.6.1.2.1.99.1.1", H3C,
    ]  # fmt: skip
    o = by_subtree(w)
    assert [x["status"] for x in w["outcomes"]] == ["ok", "ok", "ok", "ok", "timeout"]
    assert o[H3C] == {
        "subtree": H3C, "status": "timeout", "count": 3, "error_code": "timeout",
        "error": o[H3C]["error"],
    }  # fmt: skip
    assert w["status"] == "partial" and w["error_code"] == "timeout"
    assert w["counts"][H3C] == 3 and w["total"] == 7
    assert w["subtrees"][-1] == H3C
    assert sum(1 for v in w["varbinds"] if v[0].startswith(H3C + ".")) == 3


@requires_db
@pytest.mark.usefixtures("real_walker")
async def test_comware_snmpsim_outcomes_ok_truncated_timeout(
    rt: Any,
    admin: httpx.AsyncClient,
    asst: Any,
    comware_agent: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real walker against the E2E lab's Comware recording: per-subtree outcomes."""
    await rt.features.apply({"ai_template_drafts": True})
    creds = {"version": "v2c", "port": PORT, "community": "public", "retries": 0}
    dev = await make_device(admin, name="cw-walk", host=COMWARE_IP, snmp_credentials=creds)
    presets = ["system", "entity", "if_sample", "enterprise"]
    w = await _walk(admin, rt, dev["id"], presets)
    assert w["status"] == "ok" and w["error_code"] is None and w["error"] is None, w
    o = by_subtree(w)
    assert all(x["status"] == "ok" for x in w["outcomes"]), w["outcomes"]
    assert o[H3C]["count"] > 0 and o["1.3.6.1.2.1.47.1.1.1"]["count"] > 0
    assert w["subtrees"] == [
        "1.3.6.1.2.1.1", "1.3.6.1.2.1.47.1.1.1", "1.3.6.1.2.1.2.2", "1.3.6.1.2.1.31.1.1", H3C,
    ]  # fmt: skip

    # the per-subtree varbind cap: truncated, still a usable (ok) walk
    monkeypatch.setattr(tmod, "MAX_PER_SUBTREE", 5)
    w = await _walk(admin, rt, dev["id"], presets)
    o = by_subtree(w)
    assert o["1.3.6.1.2.1.47.1.1.1"]["status"] == "truncated"
    assert o["1.3.6.1.2.1.47.1.1.1"]["count"] == 5
    assert w["status"] == "ok"
    monkeypatch.setattr(tmod, "MAX_PER_SUBTREE", tp.MAX_PER_SUBTREE)

    # no time left for the vendor subtree: timeout there, the standard ones complete
    monkeypatch.setattr(tp, "VENDOR_SUBTREE_TIMEOUT_S", 0.001)
    w = await _walk(admin, rt, dev["id"], presets)
    o = by_subtree(w)
    assert o[H3C]["status"] == "timeout" and o[H3C]["error_code"] == "timeout"
    assert all(o[s]["status"] == "ok" for s in w["subtrees"][:-1])
    assert w["status"] == "partial" and w["error_code"] == "timeout"


@requires_db
@pytest.mark.usefixtures("real_walker")
async def test_wrong_v3_password_reports_auth_failure(
    rt: Any, admin: httpx.AsyncClient, asst: Any, v3_agent: Any
) -> None:
    """A wrong SNMPv3 password: the walk fails with `auth_failed` and the device's own reason
    (was: a generic timeout); the remaining subtrees are not tried with the same keys."""
    await rt.features.apply({"ai_template_drafts": True})
    presets = ["system", "entity", "enterprise"]
    good = await make_device(admin, name="v3-good", host=V3_IP, snmp_credentials=v3_creds())
    w = await _walk(admin, rt, good["id"], presets)
    assert w["status"] in ("ok", "partial") and w["counts"]["1.3.6.1.2.1.1"] > 0, w

    bad = await make_device(
        admin, name="v3-bad", host=V3_IP, snmp_credentials=v3_creds(v3_auth_key="WrongAuthKey99")
    )
    t0 = time.monotonic()
    w = await _walk(admin, rt, bad["id"], presets)
    assert time.monotonic() - t0 < 10
    assert w["status"] == "failed" and w["error_code"] == "auth_failed", w
    assert "digest" in w["error"].lower()
    first, *rest = w["outcomes"]
    assert first["subtree"] == "1.3.6.1.2.1.1" and first["status"] == "error"
    assert first["error_code"] == "auth_failed"
    assert [x["status"] for x in rest] == ["skipped", "skipped"]
    assert all(x["error_code"] == "auth_failed" for x in rest)

    user = await make_device(
        admin, name="v3-user", host=V3_IP, snmp_credentials=v3_creds(v3_username="nosuchuser")
    )
    w = await _walk(admin, rt, user["id"], ["system"])
    assert w["status"] == "failed" and w["error_code"] == "auth_failed"
    assert "usm user" in w["error"].lower()


@requires_db
@pytest.mark.usefixtures("real_walker")
async def test_unreachable_device_reports_unreachable(
    rt: Any, admin: httpx.AsyncClient, asst: Any
) -> None:
    """Nothing answers: `unreachable`, after the first subtree (the rest are skipped)."""
    await rt.features.apply({"ai_template_drafts": True})
    creds = {"version": "v2c", "port": PORT, "community": "public", "timeout_s": 1, "retries": 0}
    dev = await make_device(admin, name="dead", host=DEAD_IP, snmp_credentials=creds)
    t0 = time.monotonic()
    w = await _walk(admin, rt, dev["id"], ["system", "entity", "host_resources"])
    assert time.monotonic() - t0 < 10
    assert w["status"] == "failed" and w["error_code"] == "unreachable", w
    assert [x["status"] for x in w["outcomes"]] == ["error"] + ["skipped"] * 4  # host_resources: 3
