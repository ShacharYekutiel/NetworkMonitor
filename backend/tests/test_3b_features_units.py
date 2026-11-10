"""[3b] Pure cores of the AI features (architecture § 14.8, data-model § [3b] Pure vectors):
correlation, diff risk flags, digest signatures, template walks, report-summary numbers,
Telegram /ask rendering, alert-note builders, rule-draft schemas, explain plan."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from netmon.assistant.features import (
    chatask,
    correlate_pure,
    diffexplain_pure,
    digest_pure,
    drafts,
    explain_gather,
    notes,
    reportsummary,
    templates,
    templates_pure,
)
from netmon.assistant.features.correlate_pure import Graph, Problem, cluster


def T(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 11, 9, h, m, s, tzinfo=UTC)


# ---------------------------------------------------------------- correlation vectors
CORE, A, B, C, X, DIST = 1, 2, 3, 4, 5, 6
NAMES = {CORE: "core", A: "a", B: "b", C: "c", X: "x", DIST: "dist"}


def graph() -> Graph:
    return Graph.build({A: CORE, B: CORE, C: CORE, X: None}, [(CORE, DIST)], NAMES)


def base_problems() -> list[Problem]:
    return [
        Problem(1, CORE, 11, "down", T(12, 0, 0)),
        Problem(2, A, 12, "unreachable", T(12, 0, 30)),
        Problem(3, B, 13, "unreachable", T(12, 0, 30)),
        Problem(4, C, 14, "unreachable", T(12, 0, 30)),
    ]


def test_correlation_vector_root_and_title() -> None:
    g = graph()
    inc = cluster(base_problems(), g, 300, 3)
    assert len(inc) == 1 and len(inc[0].members) == 4
    roots = correlate_pure.root_candidates(inc[0].members, g)
    assert roots[0] == CORE
    tf = correlate_pure.title_fields(inc[0].members, roots[0], g)
    assert correlate_pure.title_en(tf) == "core down — 3 devices unreachable"
    assert tf == {
        "root": "core",
        "root_status": "down",
        "count": 3,
        "unit": "devices",
        "status": "unreachable",
    }
    assert "מכשירים" in correlate_pure.title_he(tf)


def test_correlation_vector_unrelated_and_neighbour_and_window() -> None:
    g = graph()
    ps = [
        *base_problems(),
        Problem(5, X, 15, "down", T(12, 1)),
        Problem(6, DIST, 16, "down", T(12, 3)),
    ]
    inc = cluster(ps, g, 300, 3)
    assert len(inc) == 1
    ids = {m.event_id for m in inc[0].members}
    assert 5 not in ids and 6 in ids  # x has no edge; dist is a topology neighbour of core
    roots = correlate_pure.root_candidates(inc[0].members, g)
    tf = correlate_pure.title_fields(inc[0].members, roots[0], g)
    assert correlate_pure.title_en(tf) == "core down — 3 devices unreachable"  # root unchanged
    # a warning 12:10 alone after the last change 12:03: not a member, no new incident
    late = Problem(7, A, 12, "warning", T(12, 10))
    assert not correlate_pure.joins(late, inc[0].members, inc[0].last_change, g, 300)
    assert len(cluster([*ps, late], g, 300, 3)) == 1


def test_correlation_two_problems_no_incident_and_growth() -> None:
    g = graph()
    assert cluster(base_problems()[:2], g, 300, 3) == []
    assert correlate_pure.growth_due(3, 0, 0)
    assert not correlate_pure.growth_due(4, 3, 1)
    assert correlate_pure.growth_due(5, 3, 1)  # ≥ 50 %
    assert not correlate_pure.growth_due(100, 3, 5)  # at most 5 summaries


def test_correlation_same_port_and_transitive_dependency() -> None:
    g = Graph.build({2: 1, 3: 2}, [], {1: "r", 2: "m", 3: "leaf"})
    assert correlate_pure.linked(
        Problem(1, 1, 1, "down", T(1, 0)), Problem(2, 3, 3, "unreachable", T(1, 0)), g
    )
    p = Problem(1, 7, 1, "down", T(1, 0), port_key="9:1")
    q = Problem(2, 8, 2, "down", T(1, 0), port_key="9:1")
    assert correlate_pure.linked(p, q, Graph())
    # single device → unit sensors
    ms = [Problem(i, 9, i, "down", T(1, 0)) for i in range(1, 4)]
    tf = correlate_pure.title_fields(ms, 9, Graph(names={9: "srv"}))
    assert tf["unit"] == "sensors" and tf["count"] == 3


# ---------------------------------------------------------------- diff risk flags
@pytest.mark.parametrize(
    ("driver", "line", "flag"),
    [
        (
            "hpe_comware",
            "+ snmp-agent community write cipher <secret removed>",
            ("snmp_community_added", "high"),
        ),
        ("hpe_comware", "- ssh server acl 2001", ("mgmt_acl_removed", "high")),
        ("cisco_ios", "+ access-list 10 permit any", ("acl_permit_any", "high")),
        ("hpe_arubaos_switch", "- logging 10.0.0.50", ("syslog_host_removed", "medium")),
        (
            "cisco_ios",
            "+ username tmp privilege 15 secret <secret removed>",
            ("privileged_user_added", "high"),
        ),
        ("aruba_cx", "+ ntp server 1.2.3.4", ("ntp_changed", "info")),
        ("cisco_ios", "+ description uplink", None),
        ("checkpoint_gaia", "+set expert-password-hash $6$x", ("expert_password_changed", "high")),
        ("cisco_ios", "+set expert-password-hash $6$x", None),  # driver-specific rule
        ("cisco_ios", "+ip route 0.0.0.0 0.0.0.0 10.0.0.1", ("routing_changed", "medium")),
        ("hpe_comware", "+ undo vlan 20", ("vlan_removed_cmd", "medium")),
    ],
)
def test_risk_flag_vectors(driver: str, line: str, flag: tuple[str, str] | None) -> None:
    diff = f"--- version 1\n+++ version 2\n@@ -1,1 +1,1 @@\n{line}\n"
    flags = diffexplain_pure.risk_flags(diff, driver)
    if flag is None:
        assert flags == []
    else:
        assert [(f["rule_id"], f["severity"]) for f in flags] == [flag]
        assert flags[0]["lines"] == [4] and flags[0]["title_key"] == f"diffRisk.{flag[0]}"
        assert flags[0]["side"] in ("added", "removed")


def test_risk_rules_file_parses_and_cap_and_validate() -> None:
    rules = diffexplain_pure.load_rules()
    assert len({r.id for r in rules}) == len(rules) >= 10
    lines = (
        ["--- a", "+++ b"]
        + [f"@@ -{i},1 +{i},1 @@\n {i}" for i in range(300)]
        + ["@@ -900 +900 @@", "+snmp-server community x RW"]
    )
    diff = "\n".join(lines)
    flags = diffexplain_pure.risk_flags(diff, "cisco_ios")
    capped, truncated = diffexplain_pure.cap_diff(diff, flags, max_lines=50)
    assert truncated and "snmp-server community" in capped  # flagged hunk first
    assert len(capped.splitlines()) <= 50
    line_no = flags[0]["lines"][0]
    ai = diffexplain_pure.validate_ai(
        {
            "summary": ["a"] * 9,
            "risks": [
                {
                    "severity": "bogus",
                    "title": "t",
                    "explanation": "e",
                    "lines": [line_no, 1, 99999, "x"],
                }
            ],
            "confidence": "huge",
        },
        diff,
    )
    assert len(ai["summary"]) == 5 and ai["confidence"] == "low"
    assert ai["risks"][0]["lines"] == [line_no] and ai["risks"][0]["severity"] == "info"


# ---------------------------------------------------------------- digest
def test_digest_signature_vectors() -> None:
    s1, n1 = digest_pure.signature("Interface GigabitEthernet1/0/12 changed state to down")
    s2, _ = digest_pure.signature("Interface GigabitEthernet1/0/7 changed state to down")
    assert s1 == s2 and len(s1) == 16
    assert n1 == "interface gigabitethernet<n>/<n>/<n> changed state to down"
    assert digest_pure.normalize("Login failed for user 'bob' from 10.1.1.5") == (
        "login failed for user <str> from <ip>"
    )
    assert digest_pure.normalize("MAC 00:11:22:aa:bb:cc via fe80::1") == "mac <mac> via <ip>"
    assert len(digest_pure.normalize("x " * 500)) <= 160


def test_digest_compare_vectors_and_rows() -> None:
    assert digest_pure.compare(10, 35) == (True, 3.5)
    assert digest_pure.compare(10, 25) == (False, 2.5)
    assert digest_pure.compare(0, 20, severe=True)[0]
    assert digest_pure.compare(0, 1, severe=True)[0]
    assert not digest_pure.compare(0, 5)[0]
    assert digest_pure.compare(None, 20)[0]  # new key with ≥ 20 messages
    base = [{"sshd": 10}, {"sshd": 10}, {"sshd": 10}]
    rows = digest_pure.rows({"sshd": 35, "cron": 3}, base)
    assert rows[0]["key"] == "sshd" and rows[0]["unusual"] and rows[0]["baseline"] == 10
    learning = digest_pure.rows({"sshd": 35}, base[:2])
    assert learning[0]["baseline"] is None and not learning[0]["unusual"]
    stats = {"by_program": rows, "by_severity": [], "by_device": [], "new_types": [], "day": "x"}
    assert digest_pure.unusual_count(stats) == 1
    assert "sshd" in digest_pure.stats_table_text(stats)


# ---------------------------------------------------------------- templates
def test_template_forbidden_subtrees_and_presets() -> None:
    for oid in (
        "1.3.6.1.6.3.15",
        "1.3.6.1.6.3.15.1.2",
        "1.3.6.1.2.1.4.21",
        "1.3.6.1.2.1.17.4.3",
        "1.3.6.1.2.1.4.22.1",
        "1.3.6.1.2.1.4.35",
        "1.3.6.1.6.3.18",
        "1.3.6.1.6.3.16",
        "1.3.6.1.2.1.17.7",
        "1.3.6.1.2.1.4",
    ):
        assert templates_pure.forbidden_reason(oid) == "never_walked", oid
    assert templates_pure.forbidden_reason("1.3.6.1.4.1.25506.2.6") is None
    assert templates_pure.forbidden_reason("not-an-oid") == "invalid_oid"
    subs, errs = templates_pure.resolve_subtrees(
        ["system", "enterprise", "if_sample"],
        ["1.3.6.1.4.1.9.9.1", "1.3.6.1.6.3.15"],
        "1.3.6.1.4.1.25506.11.1.1",
    )
    assert (
        subs[0] == "1.3.6.1.2.1.1" and "1.3.6.1.4.1.25506" in subs and "1.3.6.1.4.1.9.9.1" in subs
    )
    assert errs == [{"oid": "1.3.6.1.6.3.15", "reason": "never_walked"}]
    assert templates_pure.is_forbidden_varbind("1.3.6.1.2.1.17.4.3.1.1.0.1.2.3.4.5")
    assert templates_pure.row_cap_key("1.3.6.1.2.1.2.2", "1.3.6.1.2.1.2.2.1.2.17") == "17"


def test_compact_walk_tables_and_size() -> None:
    vbs = [
        ["1.3.6.1.2.1.1.1.0", "OctetString", "HPE 5130 password=Hunter2 " + "x" * 200],
        ["1.3.6.1.2.1.1.2.0", "ObjectIdentifier", "1.3.6.1.4.1.25506.11.1.1"],
    ]
    for i in range(1, 11):
        vbs.append([f"1.3.6.1.2.1.2.2.1.2.{i}", "OctetString", f"Gi1/0/{i}"])
        vbs.append([f"1.3.6.1.2.1.2.2.1.5.{i}", "Gauge32", "1000000000"])
    vbs.append(["1.3.6.1.6.3.15.1.2.2.1.3.1", "OctetString", "secret-user"])  # never kept
    out = templates_pure.compact_walk(
        vbs,
        {"1.3.6.1.2.1.1.1.0": "sysDescr.0"},
        redact=lambda s: s.replace("Hunter2", "<secret removed>"),
    )
    assert out["scalars"][0]["name"] == "sysDescr.0" and len(out["scalars"][0]["value"]) <= 64
    assert "Hunter2" not in str(out) and "secret-user" not in str(out)
    t = out["tables"][0]
    assert t["entry"] == "1.3.6.1.2.1.2.2.1" and t["row_count"] == 10 and len(t["rows"]) == 3
    big = [[f"1.3.6.1.4.1.9.{i}.0", "OctetString", "v" * 60] for i in range(2000)]
    small = templates_pure.compact_walk(big, max_bytes=4096)
    assert small["truncated"] and len(str(small)) < 8000


def test_validate_yaml_marks_unverified_and_warns() -> None:
    yaml_text = """
schema: 1
id: acme_x100
name: {en: Acme X100, he: Acme X100}
priority: 40
vendor: acme
match:
  - sys_object_id_prefixes: ["1.3.6.1.4.1.99999"]
entries:
  - kind: sensor
    type: snmp_custom
    name: {en: Temp, he: Temp}
    config: {oid: "1.3.6.1.4.1.99999.1.1.0", value_type: gauge, unit: C}
    confidence: unverified
  - kind: sensor
    type: snmp_custom
    name: {en: Other, he: Other}
    config: {oid: "1.3.6.1.4.1.12345.9.0", value_type: gauge, unit: ""}
    confidence: high
"""
    out, valid, errors, warnings = templates.validate_yaml(
        yaml_text, None, ["1.3.6.1.4.1.99999.1.1.0"], mark=True
    )
    assert valid, errors
    assert "confidence: low" in out and "unverified" in out
    assert warnings == ["OID 1.3.6.1.4.1.12345.9.0 does not appear in the walk"]
    _, valid, errors, _ = templates.validate_yaml("schema: 1\nid: Bad!\n", None, [])
    assert not valid and errors[0]["error"]
    _, valid, errors, _ = templates.validate_yaml("a: [", None, [])
    assert not valid and "YAML" in errors[0]["error"]


# ---------------------------------------------------------------- report summary
def test_report_summary_percentages() -> None:
    data = {
        "availability": {
            "overall": 99.95,
            "devices": [{"availability": 98.5}, {"availability": 0.5}],
        }
    }
    assert reportsummary.verify_percentages("Availability was 99.95% overall; worst 98.5 %.", data)
    assert reportsummary.verify_percentages("No numbers here.", data)
    assert reportsummary.verify_percentages("Ratio 50% of x.", data)  # 0.5 as a fraction
    assert not reportsummary.verify_percentages("Availability was 99.9%.", data)
    assert not reportsummary.verify_percentages("It was 97 %.", data)
    trimmed = reportsummary.trim_data({"top_alerts": list(range(50)), "ai_summary": "x"})
    assert len(trimmed["top_alerts"]) == 20 and "ai_summary" not in trimmed
    long = " ".join(["word"] * 200) + " [link](https://x)"
    assert len(reportsummary.clean(long).split()) <= 121 and "https" not in reportsummary.clean(
        "[a](https://x)"
    )


# ---------------------------------------------------------------- /ask rendering
def test_parse_ask() -> None:
    assert chatask.parse_ask("/ask which devices are down?", "NetBot") == "which devices are down?"
    assert chatask.parse_ask("/ask@NetBot  hi there", "NetBot") == "hi there"
    assert chatask.parse_ask("/ask@OtherBot hi", "NetBot") is None
    assert chatask.parse_ask("/ask", None) == ""
    assert chatask.parse_ask("/link NM-ABC", None) is None


def test_to_telegram_html() -> None:
    md = (
        "# Summary\n**core-sw** is down since 11:58 <script>\n- see [core-sw](nm:device/12) and "
        "[evil](https://evil.example/x)\n- `show ip int` _now_\n```\nline <1>\n```"
    )
    out = chatask.to_telegram_html(md, "https://nm.example")
    assert "<b>Summary</b>" in out and "<b>core-sw</b>" in out
    assert "&lt;script&gt;" in out and "<script>" not in out
    assert '<a href="https://nm.example/devices/12">core-sw</a>' in out
    assert "evil.example" not in out and "• see" in out
    assert "<code>show ip int</code>" in out and "<i>now</i>" in out
    assert "<pre>line &lt;1&gt;</pre>" in out
    no_base = chatask.to_telegram_html("[core](nm:device/1)", None)
    assert no_base == "core"
    long = chatask.to_telegram_html("\n".join(["line " * 20] * 100), None, limit=500)
    assert len(long) <= 510 and long.endswith("…")


# ---------------------------------------------------------------- alert-note builders
def test_note_builders() -> None:
    assert notes.clean_note("**bold** `x`\n" + "y" * 400).startswith("bold x")
    assert len(notes.clean_note("y" * 400)) == 300
    edited = notes.telegram_edit_text("<b>DOWN</b> core", "a < b", "HTML")
    assert edited == "<b>DOWN</b> core\n\n<i>AI:</i> a &lt; b"
    assert notes.telegram_edit_text("x" * 4090, "note", "HTML") == ""
    assert notes.telegram_reply_text("n", None, "he") == "הערת AI: n"
    card = notes.teams_card("DOWN core-sw / Ping", "the note", "https://x/sensors/1", "en")
    content = card["attachments"][0]["content"]
    assert content["body"][0]["text"] == "AI note: DOWN core-sw / Ping"
    assert content["actions"][0]["url"] == "https://x/sensors/1"
    subj, txt, html_, headers = notes.email_reply(
        {"message_id": "<1@nm>", "subject": "[NetworkMonitor] DOWN"}, "n", "en"
    )
    assert subj == "Re: [NetworkMonitor] DOWN" and headers == {
        "In-Reply-To": "<1@nm>",
        "References": "<1@nm>",
    }
    assert "AI-generated" in txt and "AI-generated" in html_


# ---------------------------------------------------------------- rule drafts
def test_propose_tool_schema_matches_models() -> None:
    from netmon.schemas import AlertRuleCreate
    from netmon.schemas.ingest import EventRuleIn

    t = drafts.propose_tool("alert_rule")
    props = t["inputSchema"]["json"]["properties"]
    assert t["name"] == "propose_alert_rule"
    assert set(props) == set(AlertRuleCreate.model_fields) | {"explanation", "assumptions"}
    assert "$ref" not in str(t) and t["inputSchema"]["json"]["additionalProperties"] is False
    e = drafts.propose_tool("event_rule")
    assert set(e["inputSchema"]["json"]["properties"]) == set(EventRuleIn.model_fields) | {
        "explanation",
        "assumptions",
    }
    draft, expl, assumptions = drafts.split_draft(
        "alert_rule", {"name": "x", "bogus": 1, "explanation": "E", "assumptions": ["a", ""]}
    )
    assert draft == {"name": "x"} and expl == "E" and assumptions == ["a"]


# ---------------------------------------------------------------- explain plan
def test_explain_gather_plan() -> None:
    t0 = T(12, 0)
    full = explain_gather.gather_plan(t0, t0 + timedelta(minutes=5), None, {"syslog": True})
    assert full.window_start == t0 - timedelta(minutes=60) and full.syslog == 50 and full.traps == 0
    assert full.max_bytes == 24 * 1024 and full.neighbours and not full.config_diff
    red = explain_gather.gather_plan(
        t0, t0, t0 + timedelta(minutes=2), {"traps": True}, reduced=True
    )
    assert red.window_start == t0 - timedelta(minutes=30) and red.traps == 10
    assert red.window_end == t0 + timedelta(minutes=2) and not red.config_changes
    data = {"a": [{"x": "y" * 100}] * 400, "b": 1}
    out = explain_gather.shrink(data, 2048)
    assert "a" in out["truncated"] and len(str(out)) < 4096
    assert explain_gather.refs_of(
        {"s": {"ref": "sensor/1"}, "l": [{"ref": "event/2"}, {"ref": "sensor/1"}]}
    ) == ["sensor/1", "event/2"]
