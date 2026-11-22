"""[severity] Pure parts: resolution order, the default mapping rows, an edited mapping,
messages (feature OFF byte-identical to the code before the feature; ON adds a severity row /
fact / webhook key), the assistant tool schemas and the event-rule validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from netmon.alerts import teams, templates
from netmon.api.errors import ApiError
from netmon.services import severity as sev
from tests import severity_golden

GOLDEN = Path(__file__).parent / "fixtures" / "severity_off_golden.json"


# ---------------------------------------------------------------- levels and mapping
def test_levels_are_ordered() -> None:
    assert sev.LEVELS == ("critical", "major", "minor", "warning", "info")
    assert [sev.rank(x) for x in sev.LEVELS] == [5, 4, 3, 2, 1]
    assert sev.rank(None) == 0 and sev.rank("bogus") == 0
    assert sev.highest(["info", None, "minor", "major", "warning"]) == "major"
    assert sev.highest([None, "x"]) is None


@pytest.mark.parametrize(
    ("status", "impact", "expected"),
    [
        ("down", "critical", "critical"),
        ("down", "warning", "major"),
        ("down", "none", "minor"),
        ("warning", "critical", "warning"),
        ("warning", "warning", "warning"),
        ("warning", "none", "warning"),
        ("up", "critical", "info"),  # recovery
        ("paused", "critical", "info"),  # status changes that are not problems
        ("unreachable", "critical", "info"),
        ("unknown", "none", "info"),
        ("down", None, "critical"),  # a missing impact counts as critical (the column default)
    ],
)
def test_default_mapping_rows(status: str, impact: str | None, expected: str) -> None:
    assert sev.resolve(status, impact, sev.DEFAULT_MAPPING) == expected


def test_every_mapping_row_is_editable() -> None:
    # each row moved to another level changes exactly that resolution
    cases = {
        "down_critical": ("down", "critical"),
        "down_warning": ("down", "warning"),
        "down_none": ("down", "none"),
        "warning_critical": ("warning", "critical"),
        "warning_warning": ("warning", "warning"),
        "warning_none": ("warning", "none"),
        "info": ("up", "critical"),
    }
    assert set(cases) == set(sev.MAPPING_KEYS)
    for key, (status, impact) in cases.items():
        moved = "minor" if sev.DEFAULT_MAPPING[key] != "minor" else "major"
        edited = {**sev.DEFAULT_MAPPING, key: moved}
        assert sev.resolve(status, impact, edited) == edited[key]
        for other, (st2, im2) in cases.items():
            if other != key:
                assert sev.resolve(st2, im2, edited) == sev.DEFAULT_MAPPING[other]


def test_resolution_order_override_rule_mapping() -> None:
    m = sev.DEFAULT_MAPPING
    r = sev.resolve
    # override > rule > mapping
    assert r("down", "critical", m, override_down="minor", rule_severity="major") == "minor"
    assert sev.resolve("down", "critical", m, rule_severity="major") == "major"
    assert sev.resolve("down", "critical", m) == "critical"
    # the override of the other state does not apply
    assert sev.resolve("down", "critical", m, override_warning="info") == "critical"
    assert sev.resolve("warning", "critical", m, override_down="info") == "warning"
    assert sev.resolve("warning", "none", m, override_warning="critical") == "critical"
    assert sev.resolve("warning", "none", m, rule_severity="major") == "major"
    # non-problem statuses: always the info row (no override, no rule)
    assert r("up", "critical", m, override_down="critical", rule_severity="major") == "info"
    # invalid values are ignored
    assert r("down", "critical", m, override_down="bogus", rule_severity="nah") == "critical"


def test_normalize_drops_unknown_and_invalid() -> None:
    assert sev.normalize(None) == sev.DEFAULT_MAPPING
    got = sev.normalize({"down_none": "critical", "x": "major", "info": "bogus"})
    assert got == {**sev.DEFAULT_MAPPING, "down_none": "critical"}
    assert sev.mapping_key("down", "weird") == "down_critical"


def test_count_problems() -> None:
    sensors = [
        {"status": "down", "severity": "critical", "acknowledged": False},
        {"status": "down", "severity": "critical", "acknowledged": True},
        {"status": "warning", "severity": "warning", "acknowledged": False},
        {"status": "up", "severity": "critical", "acknowledged": False},  # not a problem
        {"status": "down", "severity": None, "acknowledged": False},  # unresolved
    ]
    assert sev.count_problems(sensors) == {
        "critical": 2,
        "major": 0,
        "minor": 0,
        "warning": 1,
        "info": 0,
        "total": 3,
    }
    assert sev.count_problems(sensors, unacknowledged=True)["critical"] == 1


def test_parse_levels() -> None:
    assert sev.parse_levels(None, ["q"]) is None
    assert sev.parse_levels(["major", "critical", "major"], ["q"]) == ["major", "critical"]
    with pytest.raises(ApiError) as exc:
        sev.parse_levels(["high"], ["query", "severity"])
    assert exc.value.status == 422


# ---------------------------------------------------------------- messages
def test_feature_off_messages_are_byte_identical_to_before() -> None:
    """Rendered with the code before alert severity (fixtures/severity_off_golden.json)."""
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert severity_golden.render_all() == expected


def test_feature_on_adds_severity_everywhere_and_changes_nothing_else() -> None:
    off = severity_golden.payloads()
    on = severity_golden.payloads(severity="major")
    for name in off:
        if name == "test":
            assert "severity" not in on[name]
            continue
        # the webhook body: one new key at the end, every existing key untouched
        assert list(on[name]) == [*off[name], "severity"]
        assert {k: v for k, v in on[name].items() if k != "severity"} == off[name]
        assert on[name]["severity"] == "major"
    p = on["problem"]
    assert "Severity: Major\n" in templates.text_body(p, "en")
    assert "חומרה: חמורה" in templates.text_body(p, "he")
    assert ">Severity</th>" in templates.html_body(p, "en")
    assert "<b>Severity:</b> Major" in templates.telegram_text(p, "en", "HTML")
    assert "Severity: Major" in templates.telegram_text(p, "en", None)
    card = json.dumps(teams.build_card(p, "en"))
    assert '"title": "Severity", "value": "Major"' in card
    # the subject is unchanged (mail filters keep working)
    assert templates.subject(p, "en") == templates.subject(off["problem"], "en")
    # a null severity (e.g. a problem opened before the feature was on) renders no row
    p_null = severity_golden.payloads(severity=None)["problem"]
    assert templates.text_body(p_null, "en") == templates.text_body(off["problem"], "en")
    assert json.loads(json.dumps(p_null))["severity"] is None


# ---------------------------------------------------------------- assistant tool schemas
def test_tool_severity_argument_offered_only_while_on() -> None:
    from netmon.assistant import tools

    tools.ensure_loaded()
    for name in ("list_problems", "list_events"):
        t = tools.REGISTRY[name]
        off = t.spec(tools.GateInfo(features={}))["toolSpec"]["inputSchema"]["json"]
        on = t.spec(tools.GateInfo(features={"alert_severity": True}))["toolSpec"]
        on_schema = on["inputSchema"]["json"]
        assert "severities" not in off["properties"]
        assert on_schema["properties"]["severities"]["items"]["enum"] == list(sev.LEVELS)
        # nothing else differs; the default spec (no gate) is the OFF one
        rest = {k: v for k, v in on_schema["properties"].items() if k != "severities"}
        assert rest == off["properties"]
        assert t.spec()["toolSpec"]["inputSchema"]["json"] == off


# ---------------------------------------------------------------- event rules
def _rule(**kw: object) -> dict:
    base = {
        "name": "r",
        "kind": "syslog",
        "scope": "global",
        "match": {"text": "LINK"},
        "action": "down",
        "clear_after_min": 10,
    }
    return {**base, **kw}


def test_event_rule_severity_validation() -> None:
    from netmon.ingest import rules

    assert rules.validate_rule(_rule())["severity"] is None
    assert rules.validate_rule(_rule(severity="major"))["severity"] == "major"
    assert rules.validate_rule(_rule(action="warning", severity="info"))["severity"] == "info"
    with pytest.raises(ApiError) as exc:
        rules.validate_rule(_rule(severity="high"))
    assert exc.value.details[0]["loc"] == ["body", "severity"]
    with pytest.raises(ApiError):
        rules.validate_rule(_rule(action="ignore", clear_after_min=None, severity="major"))
