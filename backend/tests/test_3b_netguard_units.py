"""[3b] Pure unit tests for the SSRF endpoint guard (netmon.util.netguard), its wiring
into the assistant endpoint/proxy/KB validators, and the pattern-based secret redaction
added to netmon.assistant.redact (Findings A and C of the phase-3b pentest)."""

from __future__ import annotations

import socket
from typing import Any

import pytest

from netmon.assistant import config, redact
from netmon.util import netguard

B = netguard.BLOCKED_MSG


# --------------------------------------------------------------------------- netguard
@pytest.mark.parametrize(
    ("host", "allow_loopback", "want"),
    [
        # loopback: blocked in production, allowed in test mode
        ("127.0.0.1", False, B),
        ("127.0.0.1", True, None),
        ("127.5.6.7", False, B),
        ("::1", False, B),
        ("::1", True, None),
        ("localhost", False, B),
        ("localhost", True, None),
        # link-local / cloud metadata: blocked ALWAYS (even in test mode)
        ("169.254.169.254", False, B),
        ("169.254.169.254", True, B),
        ("169.254.1.1", True, B),
        ("fe80::1", True, B),
        ("metadata.google.internal", True, B),
        ("metadata", True, B),
        ("100.100.100.200", True, B),  # Alibaba metadata alias (CGNAT range)
        # unspecified / multicast: blocked always
        ("0.0.0.0", True, B),
        ("::", True, B),
        ("224.0.0.1", True, B),
        ("ff02::1", True, B),
        # normal public and PRIVATE VPC-endpoint addresses: allowed
        ("8.8.8.8", False, None),
        ("10.0.0.5", False, None),
        ("172.16.3.4", False, None),
        ("192.168.1.1", False, None),
    ],
)
def test_blocked_endpoint_host_literals(host: str, allow_loopback: bool, want: str | None) -> None:
    assert netguard.blocked_endpoint_host(host, allow_loopback=allow_loopback) == want


def test_blocked_endpoint_host_resolution() -> None:
    def to(addr: str):
        def resolver(_host: str) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (addr, 0))]

        return resolver

    # a hostname that resolves to a blocked IP is blocked ...
    assert netguard.blocked_endpoint_host("evil.example.com", resolver=to("169.254.169.254")) == B
    assert netguard.blocked_endpoint_host("evil.example.com", resolver=to("127.0.0.1")) == B
    # ... but allowed when it resolves to a normal (public or private VPC) address
    assert netguard.blocked_endpoint_host("vpce.example.com", resolver=to("10.0.0.5")) is None
    assert netguard.blocked_endpoint_host("api.example.com", resolver=to("203.0.113.9")) is None


def test_blocked_endpoint_host_resolution_failure_is_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_k: Any) -> list[Any]:
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert netguard.blocked_endpoint_host("does-not-resolve.invalid") == netguard.UNRESOLVED_MSG


def test_validator_wiring() -> None:
    # endpoint_error: blocked in production, loopback relaxed in test mode, metadata always
    assert config.endpoint_error("https://127.0.0.1", test_mode=False) == B
    assert config.endpoint_error("https://127.0.0.1:8443", test_mode=True) is None
    assert config.endpoint_error("https://169.254.169.254", test_mode=True) == B
    assert config.endpoint_error("https://10.0.0.5", test_mode=False) is None
    # proxy_error (same guard)
    assert config.proxy_error("http://169.254.169.254:80", test_mode=True) == B
    assert config.proxy_error("http://127.0.0.1:3128", test_mode=True) is None
    assert config.proxy_error("http://127.0.0.1:3128", test_mode=False) == B
    # kb_field_errors routes its endpoint through the same guard
    errs = config.kb_field_errors({"endpoint_url": "https://169.254.169.254"}, test_mode=True)
    assert any(e["loc"][-1] == "endpoint_url" and e["msg"] == B for e in errs)
    assert config.kb_field_errors({"endpoint_url": "https://10.0.0.5"}, test_mode=False) == []


# --------------------------------------------------------------------- redact patterns
def test_pattern_redaction_catches_non_keyworded_secrets() -> None:
    akia = "AKIAIOSFODNN7EXAMPLE"
    asia = "ASIAIOSFODNN7EXAMPLE"
    assert redact.redact_secrets(f"id {akia} end") == "id <secret removed> end"
    assert redact.redact_secrets(f"id {asia} end") == "id <secret removed> end"
    # a bare 40-char base64 AWS secret (no keyword in front) is caught by the blob rule
    secret = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
    assert redact.redact_secrets(f"val {secret} here") == "val <secret removed> here"
    pem = "-----BEGIN PRIVATE KEY-----\nMIIBVQ\n-----END PRIVATE KEY-----"
    assert redact.redact_secrets(f"key {pem}") == "key <pem removed>"


def test_pattern_redaction_leaves_ordinary_monitoring_text() -> None:
    samples = [
        "Interface Gi1/0/1 is down on core-sw01",
        "host 10.0.0.77 unreachable at 12:00:30",
        "mac aa:bb:cc:dd:ee:ff oid 1.3.6.1.4.1.9.2.1",
        "uuid 550e8400-e29b-41d4-a716-446655440000",
        # a 64-char lowercase sha256 fingerprint is single-case hex -> not redacted
        "sha256 " + "a1b2c3d4e5f6" * 5 + "abcd",
    ]
    for s in samples:
        out = redact.redact_secrets(s)
        assert out == s, out
        assert "secret removed" not in out
