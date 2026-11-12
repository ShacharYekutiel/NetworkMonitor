"""[1.3.3] ReDoS fix: event-rule and config-backup regexes compile under RE2 (linear time),
reject features RE2 cannot run safely, and never backtrack. Pure units (no DB)."""

from __future__ import annotations

import logging
import time

import pytest

from netmon.ingest import rules
from netmon.ingest.event_sensors import EventSensors
from netmon.util.saferegex import SafePatternError, check_safe, compile_safe

# Patterns that make Python's backtracking `re` explode on this input; RE2 stays linear.
CATASTROPHIC = ("(a+)+$", "(a|a)*$", "(a+)+b", "(.*a){30}$")
EVIL_INPUT = "a" * 40 + "!"
# A tight budget: RE2 matches EVIL_INPUT in well under a millisecond, `re` would take seconds.
BUDGET_S = 0.2


def _rule() -> rules.Rule:
    return rules.Rule(1, "x", "syslog", 1, "global", None, None, {}, "down")


@pytest.mark.parametrize("pattern", CATASTROPHIC)
def test_catastrophic_pattern_is_linear_via_saferegex(pattern: str) -> None:
    rx = compile_safe(pattern)
    assert rx is not None
    start = time.perf_counter()
    rx.search(EVIL_INPUT)  # must not hang
    assert time.perf_counter() - start < BUDGET_S


@pytest.mark.parametrize("pattern", CATASTROPHIC)
def test_catastrophic_pattern_is_linear_via_rule_regex(pattern: str) -> None:
    rx = _rule().regex(pattern)
    assert rx is not None
    start = time.perf_counter()
    rx.search(EVIL_INPUT)
    assert time.perf_counter() - start < BUDGET_S


def test_saferegex_ignorecase_and_multiline() -> None:
    assert compile_safe("hello").search("XHELLOX") is not None  # ignorecase default
    assert compile_safe("hello", ignorecase=False).search("XHELLOX") is None
    # ^/$ anchor to line boundaries only with multiline (like re.MULTILINE)
    assert compile_safe("^bar$", multiline=True).search("foo\nbar\nbaz") is not None
    assert compile_safe("^bar$").search("foo\nbar\nbaz") is None


def test_saferegex_caps_input_length() -> None:
    rx = compile_safe("x", max_input=3)
    assert rx is not None
    assert rx.search("aaax") is None  # the 'x' at index 3 is clipped away
    assert rx.search("xaaa") is not None


def test_compile_safe_returns_none_for_unsupported() -> None:
    assert compile_safe(None) is None
    assert compile_safe("") is None
    assert compile_safe(r"(a)\1") is None  # backreference: RE2 cannot compile
    assert compile_safe("(?=x)") is None  # lookahead
    assert compile_safe("(") is None  # syntax error


@pytest.mark.parametrize("pattern", [r"(a)\1", "(?=x)", "(?<=x)y", "(?!x)", "("])
def test_check_safe_rejects_unsafe(pattern: str) -> None:
    with pytest.raises(SafePatternError):
        check_safe(pattern)


def test_check_safe_length_limit() -> None:
    with pytest.raises(SafePatternError, match="at most 10 characters"):
        check_safe("a" * 11, max_len=10)
    check_safe("a" * 10, max_len=10)  # at the limit: ok


def test_check_safe_accepts_normal_patterns() -> None:
    for ok in (r"interface (\S+)", "^ vlan 20$", "fan [0-9]+", *CATASTROPHIC):
        check_safe(ok)  # catastrophic-for-re patterns are perfectly safe under RE2


def test_check_regex_rejects_unsafe_with_422_detail() -> None:
    errors: list[dict] = []
    rules.check_regex(r"(a)\1", ["text"], errors)
    assert errors and "loc" in errors[0] and "msg" in errors[0]
    errors.clear()
    rules.check_regex("fan ([0-9]+)", ["text"], errors)
    assert errors == []  # valid pattern passes


def test_unsafe_rule_patterns_detects_stored_backreference() -> None:
    # A pre-1.3.3 rule whose stored key_regex RE2 cannot compile.
    r = rules.Rule(
        7, "legacy", "syslog", 1, "global", None, None,
        {"text": "fan", "text_regex": False}, "down",
        key_regex=r"(a)\1", clear_after_min=5,
    )  # fmt: skip
    assert rules.unsafe_rule_patterns(r) == [r"(a)\1"]
    # A rule with only RE2-safe patterns reports nothing.
    ok = rules.Rule(
        8, "ok", "syslog", 1, "global", None, None,
        {"text": r"fan (\d+)", "text_regex": True}, "down",
        key_regex=r"fan (\d+)", clear_after_min=5,
    )  # fmt: skip
    assert rules.unsafe_rule_patterns(ok) == []


def test_startup_scan_warns_for_unsafe_pattern(caplog: pytest.LogCaptureFixture) -> None:
    bad = rules.Rule(
        42, "legacy", "syslog", 1, "global", None, None,
        {"text": "fan", "text_regex": False}, "down",
        key_regex=r"(a)\1", clear_after_min=5,
    )  # fmt: skip
    good = rules.Rule(1, "g", "trap", 1, "global", None, None, {"trap_oids": []}, "warning")
    with caplog.at_level(logging.WARNING, logger="netmon.ingest.event_sensors"):
        # _warn_unsafe_patterns does not touch instance state, so a bare instance is enough.
        EventSensors._warn_unsafe_patterns(object.__new__(EventSensors), [bad, good])
    assert "rule 42" in caplog.text and "RE2" in caplog.text
    assert "rule 1" not in caplog.text
