"""[3b] The 25 read-only tools (developer B): every tool against seeded data, gates, argument
validation, IP placeholders, timeouts, read-only transactions, and the projection test with
seeded secret markers (architecture § 14.4, § 14.6.1, § 14.11)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import text

from netmon.assistant import redact, tools
from tests.conftest import make_device, make_sensor, requires_db
from tests.fixtures_3b_agent import seed

pytestmark = requires_db

MARK = "SECRETMARK"
ALL_ON = {
    "syslog": True,
    "traps": True,
    "flows": True,
    "timeline": True,
    "new_device_alerts": True,
    "config_backup": True,
    "lag": True,
    "assistant": True,
    "ai_knowledge": True,
}


def gate(purpose: str = "ask", **features: bool) -> tools.GateInfo:
    return tools.GateInfo(
        features=features,
        assistant={"send_config_excerpts": True},
        settings={"config_backup_mask_secrets": True},
        purpose=purpose,
    )


async def call(
    rt: Any,
    name: str,
    args: Any,
    *,
    g: tools.GateInfo | None = None,
    policy: redact.Policy | None = None,
    offered: list[str] | None = None,
    kb: dict[str, str] | None = None,
    timeout_s: float = tools.TOOL_TIMEOUT_S,
) -> tools.ToolRun:
    g = g or gate(**ALL_ON)
    return await tools.run_tool(
        rt,
        name=name,
        raw_args=args,
        offered=offered if offered is not None else list(tools.ORDER),
        user_id=1,
        username="admin",
        role="admin",
        gate=g,
        policy=policy or redact.Policy.make(),
        now=rt.clock.now(),
        timezone="UTC",
        kb_sources=kb if kb is not None else {},
        timeout_s=timeout_s,
    )


async def rich_seed(c: httpx.AsyncClient, rt: Any) -> dict[str, Any]:
    """The base inventory plus logs, traps, ports, MACs, config versions, new devices and
    secrets carrying MARK everywhere a careless projection could leak them."""
    inv = await seed(c, rt)
    core, acc = inv["core"]["id"], inv["access"]["id"]
    now = datetime.now(UTC)
    async with rt.sessionmaker() as s:
        q = s.execute

        await q(
            text(
                "UPDATE devices SET identity = CAST(:i AS jsonb), snmp_credentials = "
                "CAST(:sn AS jsonb), ssh_credentials = CAST(:ss AS jsonb) WHERE id = :d"
            ),
            {
                "i": json.dumps({"vendor": "HPE", "model": "5130", "sys_descr": "Comware 7"}),
                "sn": json.dumps({"version": "2c", "community": MARK + "comm"}),
                "ss": json.dumps({"username": "u", "password_enc": MARK + "ssh"}),
                "d": core,
            },
        )
        await q(
            text("UPDATE groups SET snmp_credentials = CAST(:sn AS jsonb)"),
            {"sn": json.dumps({"version": "3", "v3_auth_key": MARK + "auth"})},
        )
        await q(
            text(
                "INSERT INTO notification_channels (name, type, config, enabled) VALUES "
                "('ops', 'telegram', CAST(:c AS jsonb), true)"
            ),
            {"c": json.dumps({"bot_token": MARK + "bot", "chat_id": "1"})},
        )
        await q(
            text(
                "INSERT INTO settings (key, value) VALUES ('smtp', CAST(:v AS jsonb)) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
            ),
            {"v": json.dumps({"host": "mail", "password": MARK + "smtp"})},
        )
        await q(
            text(
                "INSERT INTO syslog_messages (time, source_ip, device_id, transport, format, "
                "facility, severity, hostname, program, message) VALUES "
                "(:t, '10.0.0.9', :d, 'udp', 'rfc3164', 4, 3, 'core-sw', 'sshd', :m), "
                "(:t2, '10.0.0.9', :d, 'udp', 'rfc3164', 4, 6, 'core-sw', 'ifmgr', "
                "'Interface Gi1/0/1 changed state to down')"
            ),
            {
                "t": now - timedelta(minutes=3),
                "t2": now - timedelta(minutes=2),
                "d": core,
                "m": f"login failed password={MARK}syslog from 10.0.0.77",
            },
        )
        await q(
            text(
                "INSERT INTO traps (time, source_ip, device_id, version, pdu, trap_oid, "
                "trap_name, varbinds, summary) VALUES (:t, '10.0.0.9', :d, 'v2c', 'trap', "
                "'1.3.6.1.6.3.1.1.5.3', 'linkDown', CAST(:vb AS jsonb), 'linkDown ifIndex 1')"
            ),
            {
                "t": now - timedelta(minutes=1),
                "d": core,
                "vb": json.dumps([["1.3.6.1.6.3.18.1.4", "OctetString", MARK + "trapcomm"]]),
            },
        )
        await q(text("INSERT INTO switch_ports (device_id, if_index, name, alias, kind, "
                     "admin_status, oper_status, speed_bps, untagged_vlans, tagged_vlans, "
                     "mac_count, collected_at) VALUES (:d, 1, 'Gi1/0/1', 'uplink to access', "
                     "'physical', 'up', 'down', 1000000000, '{10}', '{20,30}', 1, now()), "
                     "(:d, 2, 'Gi1/0/2', 'printer', 'physical', 'up', 'up', 100000000, '{10}', "
                     "'{}', 1, now())"), {"d": core})  # fmt: skip
        await q(
            text(
                "INSERT INTO port_macs (device_id, if_index, mac, vlan, present, first_seen, "
                "last_seen) VALUES (:d, 2, 'aa:bb:cc:00:00:01', 10, true, now(), now())"
            ),
            {"d": core},
        )
        await q(text("INSERT INTO arp_entries (source_device_id, ip, mac, if_index, present, "
                     "first_seen, last_seen) VALUES (:d, '10.0.0.77', 'aa:bb:cc:00:00:01', 2, "
                     "true, now(), now())"), {"d": core})  # fmt: skip
        masked = (f"hostname core\nsnmp-agent community read cipher <secret removed>\n"
                  f"interface Gi1/0/1\n description uplink\nlocal-user admin password "
                  f"cipher {MARK}cfg1\n")  # fmt: skip
        masked2 = masked + "ntp server 10.0.0.1\n"
        for i, (content, latest, m) in enumerate(
            (
                (masked, False, True),
                (masked2, False, True),
                (f"password {MARK}unmasked\n", True, False),
            )
        ):
            await q(text("INSERT INTO config_versions (device_id, collected_at, trigger, driver, "
                         "sha256, content, masked, size_bytes, lines, added, removed, initial, "
                         "is_latest) VALUES (:d, :t, 'schedule', 'hpe_comware', :h, :c, :m, 1, "
                         "1, 1, 0, :ini, :lat)"),
                    {"d": core, "t": now - timedelta(hours=3 - i), "h": f"{i:064d}", "c": content,
                     "m": m, "ini": i == 0, "lat": latest})  # fmt: skip
        await q(text("INSERT INTO known_macs (mac, state, vendor, first_seen, first_device_id, "
                     "first_if_index, first_port_name, first_vlan, first_ip, last_seen, "
                     "last_device_id, last_if_index, last_vlan, detected_at) VALUES "
                     "('aa:bb:cc:00:00:02', 'new', 'Acme', now(), :d, 2, 'Gi1/0/2', 10, "
                     "'10.0.0.78', now(), :d, 2, 10, now())"), {"d": acc})  # fmt: skip
        await q(
            text(
                "INSERT INTO maintenance_windows (name, scope, device_id, kind, starts_at, "
                "ends_at, comment) VALUES ('patching', 'device', :d, 'once', :a, :b, 'x')"
            ),
            {"d": acc, "a": now - timedelta(hours=1), "b": now + timedelta(hours=1)},
        )
        await q(
            text(
                "INSERT INTO events (time, kind, sensor_id, device_id, sensor_name, "
                "device_name, old_status, new_status, message) VALUES "
                "(:t, 'status_change', :s, :d, 'Ping', 'core-sw', 'up', 'down', "
                "'Request timed out')"
            ),
            {"t": now - timedelta(minutes=5), "s": inv["core_ping"]["id"], "d": core},
        )
        await s.commit()
    return inv


# --------------------------------------------------------------------------- every tool
async def test_every_tool_runs_and_cites(rt: Any, admin: httpx.AsyncClient, asst: Any):
    inv = await rich_seed(admin, rt)
    core, acc = inv["core"]["id"], inv["access"]["id"]
    sid = inv["core_ping"]["id"]
    gid = (await admin.get("/api/groups")).json()["items"][0]["id"]
    now = rt.clock.now()
    day = (now - timedelta(days=1)).isoformat()
    cases: dict[str, dict[str, Any]] = {
        "search_devices": {"query": "sw", "status": ["down"]},
        "get_device": {"device_id": core},
        "list_problems": {},
        "search_sensors": {"device_id": core},
        "get_sensor_history": {"sensor_id": sid},
        "list_events": {"device_id": core, "since": day},
        "list_maintenance": {"active_only": True},
        "topology_neighbors": {"device_id": core},
        "topology_path": {"from_device_id": core, "to_device_id": acc},
        "get_device_ports": {"device_id": core, "only_down": True},
        "find_mac_or_ip": {"mac": "aa:bb:cc:00:00:01"},
        "availability": {"since": day, "until": now.isoformat()},
        "list_groups": {},
        "get_timeline": {"device_id": core},
        "search_syslog": {"device_id": core, "severity_max": 4},
        "syslog_stats": {"by": "program"},
        "search_traps": {"trap_oid": "1.3.6.1.6.3.1.1.5"},
        "flow_top": {"kind": "talkers"},
        "flow_interface_series": {"device_id": core, "if_index": 1},
        "list_new_devices": {},
        "config_versions": {"device_id": core},
        "config_search": {"text": "ntp server"},
        "list_notification_channels": {},
    }
    results: dict[str, tools.ToolRun] = {}
    for name, args in cases.items():
        g = gate("rule_draft" if name == "list_notification_channels" else "ask", **ALL_ON)
        res = await call(rt, name, args, g=g)
        assert res.ok, (name, res.envelope)
        assert res.envelope["tool"] == name and res.result_bytes <= tools.RESULT_MAX_BYTES
        results[name] = res
    r = results
    assert r["search_devices"].rows == 1 and f"device/{core}" in r["search_devices"].refs
    dev = r["get_device"].envelope["data"]
    assert dev["vendor"] == "HPE" and dev["problems"][0]["ref"] == f"sensor/{sid}"
    assert r["list_problems"].envelope["data"]["items"][0]["message"] == "Request timed out"
    assert r["list_maintenance"].rows == 1
    assert r["list_maintenance"].envelope["data"]["items"][0]["target_ref"] == f"device/{acc}"
    nb = r["topology_neighbors"].envelope["data"]["items"][0]
    assert nb["neighbor_ref"] == f"device/{acc}" and nb["local_port"] == "Gi1/0/1"
    hops = r["topology_path"].envelope["data"]["hops"]
    assert [h["device_id"] for h in hops] == [core, acc]
    port = r["get_device_ports"].envelope["data"]["items"]
    assert [p["name"] for p in port] == ["Gi1/0/1"] and port[0]["ref"] == f"port/{core}/1"
    assert port[0]["vlans_tagged"] == [20, 30]
    mac = r["find_mac_or_ip"].envelope["data"]["items"][0]
    assert mac["port_ref"] == f"port/{core}/2" and mac["ip"] == "10.0.0.77"
    assert r["get_timeline"].rows >= 2
    assert "syslog/" in json.dumps(r["get_timeline"].refs)
    assert r["search_syslog"].rows == 1 and r["search_syslog"].refs[0].startswith("syslog/")
    assert {i["key"] for i in r["syslog_stats"].envelope["data"]["items"]} == {"sshd", "ifmgr"}
    assert r["search_traps"].envelope["data"]["items"][0]["trap_name"] == "linkDown"
    assert r["flow_top"].envelope["data"]["ref"].startswith("flows/")
    assert r["list_new_devices"].envelope["data"]["items"][0]["mac"] == "aa:bb:cc:00:00:02"
    vers = r["config_versions"].envelope["data"]["items"]
    assert len(vers) == 3 and vers[0]["is_latest"] and not vers[0]["masked"]
    # the latest version is unmasked: never read; the masked pair diffs fine
    diff = await call(rt, "config_diff", {"device_id": core})
    assert not diff.ok and diff.error == "not_found"
    v_ids = sorted(v["id"] for v in vers if v["masked"])
    diff = await call(rt, "config_diff", {"device_id": core, "from_version_id": v_ids[0],
                                          "to_version_id": v_ids[1]})  # fmt: skip
    assert diff.ok and "+ntp server 10.0.0.1" in diff.envelope["data"]["diff"]
    assert diff.refs[0] == f"config-diff/{core}/{v_ids[0]}/{v_ids[1]}"
    search = r["config_search"].envelope["data"]["items"]
    assert search == [] or all("ntp" in ln["text"] for i in search for ln in i["lines"])
    assert r["list_notification_channels"].envelope["data"]["items"][0] == {
        "id": 1, "name": "ops", "type": "telegram", "enabled": True}  # fmt: skip
    groups = r["list_groups"].envelope["data"]["items"]
    assert any(g_["id"] == gid and g_["device_count"] == 2 for g_ in groups)
    av = r["availability"].envelope["data"]
    assert {i["ref"] for i in av["items"]} == {f"device/{core}", f"device/{acc}"}


async def test_secret_markers_never_leave(rt: Any, admin: httpx.AsyncClient, asst: Any):
    """Every tool, broad arguments, every secret seeded with MARK → MARK never in a result."""
    inv = await rich_seed(admin, rt)
    core = inv["core"]["id"]
    async with rt.sessionmaker() as s:
        vids = [r[0] for r in await s.execute(text("SELECT id FROM config_versions ORDER BY id"))]
        await s.execute(text("UPDATE config_versions SET is_latest = false"))
        await s.execute(text("UPDATE config_versions SET is_latest = true WHERE id = :v"),
                        {"v": vids[1]})  # fmt: skip
        await s.commit()
    broad: dict[str, list[dict[str, Any]]] = {
        "search_devices": [{}], "get_device": [{"device_id": core}], "list_problems": [{}],
        "search_sensors": [{}], "get_sensor_history": [{"sensor_id": inv["core_ping"]["id"]}],
        "list_events": [{}], "list_maintenance": [{}], "topology_neighbors": [{"device_id": core}],
        "topology_path": [{"from_device_id": core, "to_device_id": core}],
        "get_device_ports": [{"device_id": core}], "find_mac_or_ip": [{"ip": "10.0.0.77"}],
        "availability": [{"since": "2026-01-01T00:00:00Z", "until": "2026-03-01T00:00:00Z"}],
        "list_groups": [{}], "get_timeline": [{"device_id": core}], "search_syslog": [{}],
        "syslog_stats": [{"by": b} for b in ("severity", "program", "device", "hour")],
        "search_traps": [{}], "flow_top": [{"kind": "applications"}],
        "flow_interface_series": [{"device_id": core, "if_index": 1}],
        "list_new_devices": [{"state": ["new", "known", "allowed", "baseline"]}],
        "config_versions": [{"device_id": core}],
        "config_diff": [{"device_id": core, "from_version_id": vids[0], "to_version_id": vids[1]}],
        "config_search": [{"text": "password"}, {"text": "cipher"}, {"text": MARK}],
        "list_notification_channels": [{}],
    }  # fmt: skip
    assert set(broad) | {"search_knowledge"} == set(tools.ORDER)
    for name, arg_list in broad.items():
        for args in arg_list:
            g = gate("rule_draft" if name == "list_notification_channels" else "ask", **ALL_ON)
            res = await call(rt, name, args, g=g)
            blob = json.dumps(res.envelope)
            assert MARK not in blob, (name, args, blob[:500])
            if name == "config_search" and args["text"] == MARK:
                assert res.rows == 0  # no oracle: a secret value finds nothing
            assert res.ok or res.error in ("not_found",), (name, res.envelope)


# --------------------------------------------------------------------------- envelope rules
async def test_unknown_gated_invalid_and_ranges(rt: Any, admin: httpx.AsyncClient, asst: Any):
    inv = await seed(admin, rt)
    res = await call(rt, "update_device", {"id": 1})
    assert res.envelope == {"ok": False, "tool": "update_device", "error": "unknown_tool",
                            "detail": "this tool is not available"}  # fmt: skip
    # offered at question start but not in this run's offer list → unknown_tool, nothing runs
    res = await call(rt, "list_problems", {}, offered=["get_device"])
    assert res.error == "unknown_tool"
    # gate closed meanwhile → feature_disabled
    res = await call(rt, "search_syslog", {}, g=gate())
    assert res.error == "feature_disabled" and res.envelope["detail"] == "feature_off:syslog"
    res = await call(rt, "get_device", {"device_id": "x"})
    assert res.error == "invalid_arguments" and "device_id" in res.envelope["detail"]
    res = await call(rt, "get_device", {"device_id": 1, "sql": "DROP TABLE devices"})
    assert res.error == "invalid_arguments"
    res = await call(rt, "get_device", "not an object")
    assert res.error == "invalid_arguments"
    res = await call(rt, "get_device", {"device_id": 999999})
    assert res.error == "not_found"
    res = await call(rt, "search_syslog", {"since": "2026-01-01T00:00:00Z",
                                           "until": "2026-02-01T00:00:00Z"})  # fmt: skip
    assert res.error == "range_too_large"
    res = await call(rt, "find_mac_or_ip", {"mac": "aa:bb:cc:00:00:01", "ip": "10.0.0.1"})
    assert res.error == "invalid_arguments"
    res = await call(rt, "list_problems", {"limit": 500})
    assert res.error == "invalid_arguments"
    ok = await call(rt, "list_problems", {"group_id": inv["core"]["group_id"]})
    assert ok.ok and ok.rows == 1 and ok.envelope["untrusted_fields"] == ["message", "ack_comment"]


async def test_ip_placeholders_in_arguments(rt: Any, admin: httpx.AsyncClient, asst: Any):
    await rich_seed(admin, rt)
    policy = redact.Policy.make(redact_ips=True)
    redact.apply_text("host 10.0.0.77", policy)  # IP-1 issued earlier in the conversation
    res = await call(rt, "find_mac_or_ip", {"ip": "IP-1"}, policy=policy)
    assert res.ok and res.envelope["data"]["items"][0]["ip"] == "IP-1"
    assert res.args == {"ip": "IP-1"}  # the model's form is stored
    assert "10.0.0." not in json.dumps(res.envelope)
    res = await call(rt, "find_mac_or_ip", {"ip": "IP-7"}, policy=policy)
    assert res.error == "invalid_arguments" and "IP-7" in res.envelope["detail"]


async def test_timeout_and_read_only(rt: Any, admin: httpx.AsyncClient, asst: Any, monkeypatch):
    async def slow(ctx: Any, a: Any) -> Any:
        await asyncio.sleep(5)

    tools.ensure_loaded()
    monkeypatch.setattr(tools.REGISTRY["list_groups"], "handler", slow)
    res = await call(rt, "list_groups", {}, timeout_s=0.2)
    assert res.error == "timeout"

    async def writer(ctx: Any, a: Any) -> Any:
        await ctx.session.execute(text("DELETE FROM devices"))
        return tools.ToolOutput({}, 0)

    await seed(admin, rt)
    monkeypatch.setattr(tools.REGISTRY["list_groups"], "handler", writer)
    res = await call(rt, "list_groups", {})
    assert res.error == "internal"
    async with rt.sessionmaker() as s:
        assert (await s.execute(text("SELECT count(*) FROM devices"))).scalar() == 2


async def test_truncation_to_16k(rt: Any, admin: httpx.AsyncClient, asst: Any):
    d = await make_device(admin, name="noisy", host="10.9.9.9")
    await make_sensor(admin, d["id"], "ping")
    now = datetime.now(UTC)
    async with rt.sessionmaker() as s:
        for i in range(100):
            await s.execute(
                text("INSERT INTO syslog_messages (time, source_ip, device_id, transport, format, "
                     "facility, severity, message) VALUES (:t, '10.9.9.9', :d, 'udp', 'raw', 1, "
                     "5, :m)"),
                {"t": now - timedelta(seconds=i), "d": d["id"], "m": f"{i} " + "y" * 490},
            )  # fmt: skip
        await s.commit()
    res = await call(rt, "search_syslog", {"limit": 100})
    assert res.ok and res.truncated and res.result_bytes <= tools.RESULT_MAX_BYTES
    items = res.envelope["data"]["items"]
    assert 10 < len(items) < 100 and items[0]["message"].startswith("0 ")


async def test_search_knowledge_against_mock(rt: Any, admin: httpx.AsyncClient, asst: Any):
    from netmon.services import settings as settings_svc

    async with rt.sessionmaker() as s:
        await settings_svc.put(s, "assistant_kb", {"knowledge_base_id": "QAKBTEST01",
                                                   "endpoint_url": asst.url, "results": 3,
                                                   "min_score": 0.0})  # fmt: skip
        await s.commit()
    await rt.assistant.reload()
    kb: dict[str, str] = {}
    res = await call(rt, "search_knowledge", {"query": "core switch runbook"}, kb=kb)
    assert res.ok, res.envelope
    items = res.envelope["data"]["items"]
    assert items and items[0]["ref"] == "kb/1" and items[0]["text"]
    assert set(kb) == {i["ref"] for i in items}
    assert res.refs[0] == "kb/1"
    asst.mode("kb_error", target="kb")
    res = await call(rt, "search_knowledge", {"query": "core switch runbook"})
    assert not res.ok and res.error == "kb_unavailable"
