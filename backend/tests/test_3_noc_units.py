"""[3] NOC kiosk pure parts: guard vectors, source normalisation, tokens, failure limiter."""

from __future__ import annotations

import pytest

from netmon import kiosk


def test_guard_vectors() -> None:
    assert kiosk.guard("10.1.2.3", ["10.1.2.0/24"])
    assert not kiosk.guard("10.1.3.1", ["10.1.2.0/24"])
    assert kiosk.guard("::ffff:10.1.2.3", ["10.1.2.0/24"])
    assert not kiosk.guard("10.1.2.3", [])
    assert kiosk.guard("10.9.9.9", ["192.168.0.1", "10.9.9.9"])
    assert kiosk.guard("fd00::5", ["fd00::/64"])
    assert not kiosk.guard("fd00:1::5", ["fd00::/64"])
    assert not kiosk.guard("garbage", ["0.0.0.0/0"])
    assert not kiosk.guard("10.1.2.3", ["fd00::/8"])


def test_normalize_source() -> None:
    assert kiosk.normalize_source(" 10.1.2.7/24 ") == "10.1.2.0/24"
    assert kiosk.normalize_source("10.1.2.7") == "10.1.2.7"
    assert kiosk.normalize_source("10.1.2.7/32") == "10.1.2.7"
    assert kiosk.normalize_source("::ffff:10.0.0.0/120") == "10.0.0.0/24"
    for bad in ("", "10.1.2", "host.example", "10.0.0.0/33"):
        with pytest.raises(ValueError):
            kiosk.normalize_source(bad)


def test_tokens() -> None:
    t = kiosk.new_token()
    assert t.startswith("nmk_") and len(t) == 47
    assert kiosk.plausible_token(t)
    assert kiosk.token_prefix(t) == t[:12] and len(kiosk.token_prefix(t)) == 12
    assert kiosk.hash_matches(t, kiosk.token_hash(t))
    assert not kiosk.hash_matches(t + "x", kiosk.token_hash(t))
    for bad in (None, "", "nmk_short", "xyz_" + "a" * 43, "nmk_" + "a" * 40 + "!!!"):
        assert not kiosk.plausible_token(bad)


def test_failure_limiter() -> None:
    lim = kiosk.FailureLimiter(max_failures=3, window_s=60)
    for _ in range(3):
        assert not lim.blocked("1.2.3.4")
        lim.fail("1.2.3.4", "bad_token")
    assert lim.blocked("1.2.3.4")
    assert not lim.blocked("1.2.3.5")
    assert lim.reasons["bad_token"] == 3
    lim.clear()
    assert not lim.blocked("1.2.3.4")
