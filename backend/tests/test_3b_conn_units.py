"""[3b] Connection layer, pure parts and in-memory I/O (no DB): SigV4 / Roles Anywhere vectors
(cross-checked with botocore), event-stream codec, AWS error mapping, pricing and caps
arithmetic, profile classification, settings validation, certificates / CSR, credential cache,
Bedrock client retries and fallbacks against an in-memory transport."""

from __future__ import annotations

import asyncio
import json
import os
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from netmon.assistant import (
    certs,
    config,
    eventstream,
    gates,
    pricing,
    profiles,
    sigv4,
    usage,
)
from netmon.assistant.bedrock import BedrockClient, StreamAssembler, strip_cache_points
from netmon.assistant.credentials import CachingProvider, Credentials, DefaultChainProvider
from netmon.assistant.errors import AssistantError, map_error, map_stream_exception

AK, SK = "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"


# ---------------------------------------------------------------- SigV4
def test_sigv4_get_vanilla() -> None:
    r = sigv4.sign_v4(
        "GET",
        "https://example.amazonaws.com/",
        {},
        b"",
        access_key=AK,
        secret_key=SK,
        region="us-east-1",
        service="service",
        amzdate="20150830T123600Z",
    )
    assert r.signature == "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    assert r.headers["Authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, Signature=5fa0"
    )


BODY2 = (
    b'{"messages":[{"role":"user","content":[{"text":"ping"}]}],"inferenceConfig":{"maxTokens":5}}'
)


def test_sigv4_bedrock_vector() -> None:
    assert sigv4.sha256_hex(BODY2) == (
        "6df0851f0b28f3a611972fc70e4b1125520ef1133a54940f39609fb54f780a69"
    )
    path = sigv4.model_path("eu.anthropic.claude-test-v1:0", "converse")
    assert path == "/model/eu.anthropic.claude-test-v1%3A0/converse"
    r = sigv4.sign_v4(
        "POST",
        f"https://bedrock-runtime.eu-central-1.amazonaws.com{path}",
        {"Content-Type": "application/json"},
        BODY2,
        access_key=AK,
        secret_key=SK,
        region="eu-central-1",
        service="bedrock",
        amzdate="20261109T120000Z",
        session_token="FQoGZXIvYXdzEXAMPLETOKEN",
    )
    assert r.canonical_request.split("\n")[1] == "/model/eu.anthropic.claude-test-v1%253A0/converse"
    assert r.signed_headers == "content-type;host;x-amz-date;x-amz-security-token"
    assert sigv4.sha256_hex(r.canonical_request.encode()) == (
        "52ce1d34b2b4abf31938695167c6d8c532ce88217496117380e25836634cf708"
    )
    assert r.signature == "2584eff908463e2cb9f972d9e432585ee8fb20adc50f7e26ffa5a24b2065a3e8"
    assert r.headers["X-Amz-Security-Token"] == "FQoGZXIvYXdzEXAMPLETOKEN"


def test_sigv4_matches_botocore() -> None:
    botocore = pytest.importorskip("botocore")
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials as BotoCreds

    assert botocore
    rnd = random.Random(7)
    ids = [
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "global.anthropic.claude-x:1",
        "arn:aws:bedrock:us-east-1:111122223333:inference-profile/us.anthropic.claude-y-v1:0",
        "anthropic.claude-3-haiku-20240307-v1:0",
    ]
    for i in range(20):
        mid = ids[i % len(ids)]
        url = f"https://bedrock-runtime.us-east-1.amazonaws.com{sigv4.model_path(mid, 'converse')}"
        if i % 5 == 0:
            url = "https://bedrock.us-east-1.amazonaws.com/foundation-models?byProvider=Anthropic&a=b%20c"
        body = json.dumps({"x": rnd.random(), "t": "שלום"}, ensure_ascii=False).encode()
        amz = f"202611{10 + i % 9:02d}T1{i % 10}0000Z"
        token = "TOKEN" + str(i) if i % 2 else None
        ours = sigv4.sign_v4(
            "POST",
            url,
            {"Content-Type": "application/json"},
            body,
            access_key=AK,
            secret_key=SK,
            region="us-east-1",
            service="bedrock",
            amzdate=amz,
            session_token=token,
        )
        hdrs = {k: v for k, v in ours.headers.items() if k != "Authorization"}
        req = AWSRequest(method="POST", url=url, headers=hdrs, data=body)
        req.context["timestamp"] = amz
        a = SigV4Auth(BotoCreds(AK, SK, token), "bedrock", "us-east-1")
        sig = a.signature(a.string_to_sign(req, a.canonical_request(req)), req)
        assert sig == ours.signature, (url, i)


BODY3 = (
    b'{"durationSeconds":3600,"profileArn":"arn:aws:rolesanywhere:eu-central-1:111122223333:'
    b'profile/p-1","roleArn":"arn:aws:iam::111122223333:role/netmon-bedrock","trustAnchorArn":'
    b'"arn:aws:rolesanywhere:eu-central-1:111122223333:trust-anchor/ta-1"}'
)
STS3 = (
    "AWS4-X509-RSA-SHA256\n20261109T120000Z\n20261109/eu-central-1/rolesanywhere/aws4_request\n"
    "5fb1c33641630cafe7c989350f015c1e6226cfc92d1ccf977798482982629f68"
)


@pytest.mark.parametrize("kind", ["rsa", "ec"])
def test_roles_anywhere_signing(kind: str) -> None:
    assert sigv4.sha256_hex(BODY3) == (
        "b156a6b2d763ba2b892b764f394878e1e0283bcd6625c4f5016b0d177b9996c2"
    )
    key: Any = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if kind == "rsa"
        else ec.generate_private_key(ec.SECP256R1())
    )
    signer = sigv4.X509Signer(key)
    headers = {
        "Content-Type": "application/json",
        "Host": "rolesanywhere.eu-central-1.amazonaws.com",
        "X-Amz-Date": "20261109T120000Z",
        "X-Amz-X509": "MIIBTESTCERT",
    }
    r = sigv4.sign_x509_headers(
        "POST",
        "https://rolesanywhere.eu-central-1.amazonaws.com/sessions",
        headers,
        BODY3,
        serial=0x1234,
        signer=signer,
        region="eu-central-1",
        amzdate="20261109T120000Z",
    )
    assert r.signed_headers == "content-type;host;x-amz-date;x-amz-x509"
    expected_sts = STS3 if kind == "rsa" else STS3.replace("X509-RSA", "X509-ECDSA")
    assert r.string_to_sign == expected_sts
    auth = r.headers["Authorization"]
    assert auth.startswith(
        f"{signer.algorithm} Credential=4660/20261109/eu-central-1/rolesanywhere/"
    )
    sig = bytes.fromhex(r.signature)
    if kind == "rsa":
        key.public_key().verify(sig, expected_sts.encode(), padding.PKCS1v15(), hashes.SHA256())
    else:
        key.public_key().verify(sig, expected_sts.encode(), ec.ECDSA(hashes.SHA256()))


def test_roles_anywhere_chain_signed() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    r = sigv4.sign_x509(
        "POST",
        "https://rolesanywhere.us-east-1.amazonaws.com/sessions",
        {"Content-Type": "application/json"},
        b"{}",
        cert_der=b"leaf",
        serial=5,
        chain_der=[b"ca1", b"ca2"],
        signer=sigv4.X509Signer(key),
        region="us-east-1",
        amzdate="20261109T120000Z",
    )
    assert r.signed_headers == "content-type;host;x-amz-date;x-amz-x509;x-amz-x509-chain"
    assert r.headers["X-Amz-X509-Chain"] == "Y2Ex,Y2Ey"


# ---------------------------------------------------------------- event stream
VECTOR = bytes.fromhex(
    "0000009f00000057c37babff0b3a6576656e742d74797065070011636f6e74656e74426c6f636b44656c74610d3a"
    "636f6e74656e742d747970650700106170706c69636174696f6e2f6a736f6e0d3a6d6573736167652d74797065"
    "0700056576656e747b22636f6e74656e74426c6f636b496e646578223a302c2264656c7461223a7b2274657874"
    "223a224869227d2c2270223a2261626364227df2ace5ff"
)


def test_eventstream_vector() -> None:
    assert len(VECTOR) == 159
    msgs, rest = eventstream.decode_frames(VECTOR)
    assert rest == b"" and len(msgs) == 1
    m = msgs[0]
    assert m.event_type == "contentBlockDelta" and m.message_type == "event"
    assert m.json() == {"contentBlockIndex": 0, "delta": {"text": "Hi"}, "p": "abcd"}
    enc = eventstream.encode_event(
        "contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "Hi"}, "p": "abcd"}
    )
    assert enc == VECTOR


def test_eventstream_split_everywhere() -> None:
    stream = VECTOR + eventstream.encode_event("messageStop", {"stopReason": "end_turn"})
    for cut in range(len(stream) + 1):
        d = eventstream.Decoder()
        got = d.feed(stream[:cut]) + d.feed(stream[cut:])
        assert [m.event_type for m in got] == ["contentBlockDelta", "messageStop"], cut
    d = eventstream.Decoder()
    got = [m for b in stream for m in d.feed(bytes([b]))]
    assert len(got) == 2 and d.pending == 0


def test_eventstream_corruption() -> None:
    for i in range(12):
        bad = bytearray(VECTOR)
        bad[i % 8] ^= 0x01
        with pytest.raises(eventstream.EventStreamError) as e:
            eventstream.decode_frames(bytes(bad))
        assert e.value.code == "bad_prelude_crc"
    bad = bytearray(VECTOR)
    bad[120] ^= 0xFF
    with pytest.raises(eventstream.EventStreamError) as e:
        eventstream.decode_frames(bytes(bad))
    assert e.value.code == "bad_message_crc"
    import struct
    import zlib

    prelude = struct.pack(">II", 2 * 1024 * 1024, 0)
    big = prelude + struct.pack(">I", zlib.crc32(prelude))
    with pytest.raises(eventstream.EventStreamError) as e:
        eventstream.decode_frames(big)
    assert e.value.code == "frame_too_large"
    exc = eventstream.encode_exception("throttlingException", "slow down")
    (m,), _ = eventstream.decode_frames(exc)
    assert m.message_type == "exception" and m.exception_type == "throttlingException"
    assert map_stream_exception(m.exception_type, m.json()).code == "bedrock_throttled"


# ---------------------------------------------------------------- error mapping
def _h(t: str) -> dict[str, str]:
    return {"x-amzn-ErrorType": f"{t}:http://internal.amazon.com/coral/com.amazon.bedrock/"}


@pytest.mark.parametrize(
    ("status", "etype", "msg", "code"),
    [
        (429, "ThrottlingException", "Too many requests", "bedrock_throttled"),
        (400, "ServiceQuotaExceededException", "quota", "bedrock_quota"),
        (
            403,
            "AccessDeniedException",
            "You don't have access to the model with the specified model ID.",
            "bedrock_model_access",
        ),  # noqa: E501
        (
            403,
            "AccessDeniedException",
            "User x is not authorized to perform: bedrock:InvokeModel",
            "bedrock_access_denied",
        ),  # noqa: E501
        (404, "ResourceNotFoundException", "Model not found", "bedrock_model_invalid"),
        (
            400,
            "ValidationException",
            "The provided model identifier is invalid.",
            "bedrock_model_invalid",
        ),  # noqa: E501
        (
            400,
            "ValidationException",
            "Invocation of model ID x with on-demand throughput isn’t supported.",
            "bedrock_model_invalid",
        ),  # noqa: E501
        (
            403,
            "UnrecognizedClientException",
            "The security token included in the request is invalid.",
            "aws_auth_failed",
        ),  # noqa: E501
        (
            403,
            "ExpiredTokenException",
            "The security token included in the request is expired",
            "aws_token_expired",
        ),  # noqa: E501
        (
            403,
            "InvalidSignatureException",
            "Signature expired: 20261109T120000Z is now earlier than 20261109T121000Z",
            "aws_clock_skew",
        ),  # noqa: E501
        (424, "ModelErrorException", "x", "bedrock_unavailable"),
        (429, "ModelNotReadyException", "Model is not ready", "bedrock_unavailable"),
        (429, "ThrottlingException", "Too many requests", "bedrock_throttled"),
        (503, "ServiceUnavailableException", "x", "bedrock_unavailable"),
        (500, "InternalServerException", "x", "bedrock_unavailable"),
        (
            400,
            "ValidationException",
            "Malformed input request: extraneous key",
            "bedrock_validation",
        ),
    ],
)
def test_map_error_table(status: int, etype: str, msg: str, code: str) -> None:
    err = map_error(status, _h(etype), json.dumps({"message": msg}).encode(), region="us-east-1")
    assert err.code == code
    if code == "bedrock_model_access":
        assert "us-east-1" in err.message
    if code == "bedrock_validation":
        assert "extraneous" in err.message


def test_map_error_body_type_and_status() -> None:
    err = map_error(400, {}, b'{"__type":"com.amazon#ThrottlingException","message":"x"}')
    assert err.code == "bedrock_throttled"
    assert map_error(502, {}, b"<html>bad gateway</html>").code == "bedrock_unavailable"
    assert map_error(401, {}, b"").code == "aws_auth_failed"
    long = map_error(400, _h("ValidationException"), json.dumps({"message": "x" * 900}).encode())
    assert len(long.message) <= 300
    assert long.status == 502 and long.to_api().status == 502
    assert AssistantError("assistant_timeout").status == 504
    assert AssistantError("aws_network").is_error_state
    assert not AssistantError("bedrock_throttled").is_error_state


# ---------------------------------------------------------------- pricing / caps
def test_pricing_vector() -> None:
    u = {
        "input_tokens": 6000,
        "output_tokens": 600,
        "cache_read_tokens": 6000,
        "cache_write_tokens": 0,
    }
    prices = {
        "input_per_mtok": 3,
        "output_per_mtok": 15,
        "cache_read_per_mtok": None,
        "cache_write_per_mtok": None,
    }
    assert pricing.estimate(u, prices) == Decimal("0.028800")
    assert pricing.effective_prices(prices)["cache_write"] == Decimal("3.75")
    assert pricing.estimate(u, {"input_per_mtok": 3}) is None
    assert usage.cap_tokens(u) == 7200
    assert usage.cap_tokens({"cache_read_tokens": 1}) == 1


def test_cap_check() -> None:
    kw = {"token_cap": 1000, "cost_used": None, "cost_cap": None, "stop_pct": 80}
    assert usage.cap_check(interactive=True, tokens_used=999, **kw) is None
    assert usage.cap_check(interactive=True, tokens_used=1000, **kw)["cap"] == "monthly_tokens"
    assert usage.cap_check(interactive=False, tokens_used=799, **kw) is None
    blocked = usage.cap_check(interactive=False, tokens_used=800, **kw)
    assert blocked["cap"] == "monthly_tokens" and blocked["background_stop"] is True
    kw2 = {"token_cap": 10**9, "cost_used": Decimal("8.0"), "cost_cap": 10.0, "stop_pct": 80}
    assert usage.cap_check(interactive=True, tokens_used=0, **kw2) is None
    assert usage.cap_check(interactive=False, tokens_used=0, **kw2)["cap"] == "monthly_cost"
    from datetime import date

    assert usage.projection_pct(100, 1000, date(2026, 11, 10)) == 30.0
    assert usage.month_bounds(date(2026, 12, 31)) == (date(2026, 12, 1), date(2027, 1, 1))


# ---------------------------------------------------------------- profiles
def test_profile_classification() -> None:
    assert profiles.classify("us.anthropic.claude-x-v1:0")["scope"] == "geo"
    assert profiles.classify("us.anthropic.claude-x-v1:0")["geography"] == "us"
    g = profiles.classify("global.anthropic.claude-x-v1:0")
    assert g["scope"] == "global" and g["warning"] == "global_inference_profile"
    assert "worldwide" in g["explanation"]["en"]
    assert profiles.classify("anthropic.claude-3-haiku")["scope"] == "in_region"
    arn = "arn:aws:bedrock:il-central-1:111122223333:inference-profile/global.anthropic.claude-x"
    assert profiles.classify(arn)["scope"] == "global"
    assert profiles.classify("arn:aws:bedrock:us-east-1:1:provisioned-model/abc")["scope"] == "arn"
    assert profiles.classify(None)["scope"] is None
    assert profiles.warnings_for(["eu.anthropic.x", "global.anthropic.y"]) == [
        "global_inference_profile"
    ]
    assert {s["scope"] for s in profiles.SCOPES} == {"global", "geo", "in_region", "arn"}


# ---------------------------------------------------------------- settings validation
def test_settings_validation_and_hash() -> None:
    cfg = dict(config.DEFAULT_ASSISTANT)
    assert cfg["region"] == "us-east-1" and cfg["model_id"] == ""
    assert config.field_errors(cfg, test_mode=False) == []
    bad = {
        **cfg,
        "region": "Europe",
        "endpoint_url": "http://x.example",
        "proxy_mode": "custom",
        "proxy_url": "http://u:p@proxy:8080",
        "ra_role_arn": "arn:aws:iam::1:role/x",
    }
    fields = {e["loc"][-1] for e in config.field_errors(bad, test_mode=False)}
    assert fields == {"region", "endpoint_url", "proxy_url", "ra_role_arn"}
    assert config.endpoint_error("http://127.0.0.1:9", test_mode=True) is None
    assert (
        config.endpoint_error(
            "https://vpce-1.bedrock-runtime.us-east-1.vpce.amazonaws.com/x", test_mode=False
        )
        == "must not contain a path"
    )
    assert config.REGION_RE.match("il-central-1") and config.REGION_RE.match("us-gov-west-1")
    req = config.required_errors(
        {**cfg, "auth_mode": "access_key"}, "k", has_cert=False, has_key=False
    )
    assert {e["loc"][-1] for e in req} == {"model_id", "access_key_id", "secret_access_key"}
    req = config.required_errors({**cfg, "model_id": "m"}, "k", has_cert=True, has_key=False)
    assert {e["loc"][-1] for e in req} == {
        "ra_trust_anchor_arn",
        "ra_profile_arn",
        "ra_role_arn",
        "certificate",
    }
    h = config.connection_hash(cfg, secret_plain="a", cert_pem=None, chain_pem=None, key_pem=None)
    assert h != config.connection_hash(
        cfg, secret_plain="b", cert_pem=None, chain_pem=None, key_pem=None
    )
    assert h != config.connection_hash(
        {**cfg, "region": "il-central-1"},
        secret_plain="a",
        cert_pem=None,
        chain_pem=None,
        key_pem=None,
    )
    # non-connection fields do not change the hash
    assert h == config.connection_hash(
        {**cfg, "redact_internal_ips": True, "prices": {"x": 1}},
        secret_plain="a",
        cert_pem=None,
        chain_pem=None,
        key_pem=None,
    )
    merged = config.merge(
        cfg, {"secret_access_key": "s3cr3t", "prices": {"input_per_mtok": 3}}, "secret"
    )
    assert merged["secret_access_key_enc"].startswith("v1:")
    assert config.secret_access_key(merged, "secret") == "s3cr3t"
    assert merged["prices"]["input_per_mtok"] == 3 and merged["prices"]["currency"] == "USD"
    assert config.merge(merged, {"secret_access_key": None}, "secret")["secret_access_key_enc"]
    assert (
        config.merge(merged, {"secret_access_key": ""}, "secret")["secret_access_key_enc"] is None
    )
    with pytest.raises(config.Unreadable):
        config.secret_access_key(merged, "other-secret")
    assert config.runtime_endpoint({"region": "il-central-1"}) == (
        "https://bedrock-runtime.il-central-1.amazonaws.com"
    )
    assert config.arn_account_warning(
        {
            "ra_trust_anchor_arn": "arn:aws:rolesanywhere:r:111:x",
            "ra_role_arn": "arn:aws:iam::222:role/y",
        }
    )


def test_gates_idle_reasons() -> None:
    snap = dict.fromkeys(gates.ALL, False)
    assert gates.idle_reasons("assistant", snap, {}) == ["audit"]
    snap.update(assistant=True, audit=True)
    assert gates.idle_reasons("ai_explain_alert", snap, {}) == []
    assert gates.idle_reasons("ai_config_diff", snap, {}) == [
        "config_backup",
        "send_config_excerpts",
    ]
    snap["config_backup"] = True
    assert gates.idle_reasons("ai_config_diff", snap, {"send_config_excerpts": True}) == []
    assert gates.idle_reasons("ai_chat_ask", {**snap, "assistant": False}, {}) == [
        "assistant",
        "chat_ack",
    ]


# ---------------------------------------------------------------- certificates
def _ca() -> tuple[x509.Certificate, Any]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Issuing CA")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def _issue(
    csr_or_key: Any,
    ca: x509.Certificate,
    ca_key: Any,
    *,
    days: int = 365,
    ca_flag: bool = False,
    digsig: bool = True,
    serial: int = 0x1234,
) -> x509.Certificate:
    if isinstance(csr_or_key, x509.CertificateSigningRequest):
        pub, subject = csr_or_key.public_key(), csr_or_key.subject
    else:
        pub = csr_or_key.public_key()
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "netmonitor-bedrock.test")])
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca.subject)
        .public_key(pub)
        .serial_number(serial)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca_flag, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(digsig, False, not digsig, False, False, False, False, False, False),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )


def _pem(c: x509.Certificate) -> bytes:
    return c.public_bytes(serialization.Encoding.PEM)


def test_cert_upload_variants() -> None:
    ca, ca_key = _ca()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = _issue(key, ca, ca_key)
    key_pem = certs.key_to_pkcs8_pem(key).encode()
    mat = certs.parse_upload(_pem(leaf) + _pem(ca), key_pem)
    assert mat.cert.serial_number == 0x1234 and len(mat.chain) == 1
    info = certs.cert_info(
        mat.cert, mat.chain, source="uploaded", uploaded_at=None, key_unreadable=False
    )
    assert (
        info["serial_decimal"] == "4660" and info["key_type"] == "rsa" and info["key_bits"] == 2048
    )
    # PKCS#1 encrypted key with password
    enc = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"pw"),
    )
    assert certs.parse_upload(_pem(leaf), enc, "pw").key is not None
    with pytest.raises(certs.CertError) as e:
        certs.parse_upload(_pem(leaf), enc, "wrong")
    assert e.value.code == "invalid_private_key"
    # PKCS#12
    pfx = pkcs12.serialize_key_and_certificates(
        b"x", key, leaf, [ca], serialization.BestAvailableEncryption(b"Pfx-pass")
    )
    mat = certs.parse_upload(pfx, None, "Pfx-pass")
    assert mat.cert.serial_number == 0x1234 and mat.chain[0].subject == ca.subject
    with pytest.raises(certs.CertError) as e:
        certs.parse_upload(pfx, None, "nope")
    assert e.value.code == "invalid_private_key"  # wrong .pfx password (api.md)
    with pytest.raises(certs.CertError) as e:
        certs.parse_upload(b"\x00garbage-not-a-cert" * 4, None, None)
    assert e.value.code == "invalid_certificate"
    # mismatching key, CA:TRUE, expired, no digital signature
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(certs.CertError) as e:
        certs.parse_upload(_pem(leaf), certs.key_to_pkcs8_pem(other).encode())
    assert e.value.code == "key_mismatch"
    with pytest.raises(certs.CertError) as e:
        certs.parse_upload(_pem(_issue(key, ca, ca_key, ca_flag=True)), key_pem)
    assert e.value.code == "invalid_certificate"
    with pytest.raises(certs.CertError) as e:
        certs.parse_upload(_pem(_issue(key, ca, ca_key, days=-1)), key_pem)
    assert "expired" in e.value.reason
    with pytest.raises(certs.CertError) as e:
        certs.parse_upload(_pem(_issue(key, ca, ca_key, digsig=False)), key_pem)
    assert "Digital Signature" in e.value.reason
    assert certs.expiring(_issue(key, ca, ca_key, days=10), datetime.now(UTC))


@pytest.mark.parametrize(("kt", "size"), [("rsa", 2048), ("ec", 256), ("ec", 384)])
def test_csr_flow(kt: str, size: int) -> None:
    key_pem, csr_pem = certs.generate_csr("netmonitor-bedrock.mnidom.net", kt, size, "Mnidom")
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    assert csr.is_signature_valid
    assert csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == (
        "netmonitor-bedrock.mnidom.net"
    )
    ku = csr.extensions.get_extension_for_class(x509.KeyUsage).value
    assert ku.digital_signature
    ca, ca_key = _ca()
    cert = _issue(csr, ca, ca_key)
    mat = certs.parse_upload(_pem(cert), None, None, pending_key_pem=key_pem)  # no key uploaded
    assert certs.key_matches(mat.cert, mat.key)
    other_pem, _ = certs.generate_csr("x", "ec", 256)
    with pytest.raises(certs.CertError) as e:
        certs.parse_upload(_pem(cert), None, None, pending_key_pem=other_pem)
    assert e.value.code == "key_mismatch"
    assert certs.validate_cn("bad/name") is not None
    with pytest.raises(ValueError):
        certs.generate_csr("x", "rsa", 1024)


# ---------------------------------------------------------------- credentials
class _Counting:
    def __init__(self, lifetime: timedelta) -> None:
        self.calls = 0
        self.lifetime = lifetime

    async def fetch(self) -> Credentials:
        self.calls += 1
        await asyncio.sleep(0.01)
        return Credentials(f"ASIA{self.calls}", "s", "t", datetime.now(UTC) + self.lifetime, "x")


async def test_caching_provider_single_flight_and_refresh() -> None:
    p = _Counting(timedelta(hours=1))
    c = CachingProvider(p)
    got = await asyncio.gather(*(c.get() for _ in range(10)))
    assert p.calls == 1 and {g.access_key_id for g in got} == {"ASIA1"}
    assert (await c.get()).access_key_id == "ASIA1"
    assert (await c.get(force=True)).access_key_id == "ASIA2"
    p2 = _Counting(timedelta(minutes=4))  # inside the 5-minute refresh window
    c2 = CachingProvider(p2)
    await c2.get()
    await c2.get()
    assert p2.calls == 2


async def test_default_chain_environment_and_none(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in [k for k in os.environ if k.startswith("AWS_")]:
        monkeypatch.delenv(k, raising=False)  # explicit env dicts only; nothing from the host
    assert DefaultChainProvider().env is os.environ  # default: the process environment
    env = {"AWS_ACCESS_KEY_ID": "AKIAENV", "AWS_SECRET_ACCESS_KEY": "x", "AWS_SESSION_TOKEN": "t"}
    c = await DefaultChainProvider(env).fetch()
    assert c.source == "environment" and c.session_token == "t"
    p = DefaultChainProvider({"AWS_EC2_METADATA_DISABLED": "true"})
    with pytest.raises(AssistantError) as e:
        await p.fetch()
    assert e.value.code == "aws_auth_failed" and p.tried == ["environment"]


# ---------------------------------------------------------------- Bedrock client (in-memory)
def _stream_body(events: list[tuple[str, dict]]) -> bytes:
    return b"".join(eventstream.encode_event(t, d) for t, d in events)


TEXT_EVENTS = [
    ("messageStart", {"role": "assistant"}),
    ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "Hel"}}),
    ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "lo"}}),
    ("contentBlockStop", {"contentBlockIndex": 0}),
    (
        "contentBlockStart",
        {
            "contentBlockIndex": 1,
            "start": {"toolUse": {"toolUseId": "t1", "name": "list_problems"}},
        },
    ),
    ("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"toolUse": {"input": '{"lim'}}}),
    ("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"toolUse": {"input": 'it": 5}'}}}),
    ("contentBlockStop", {"contentBlockIndex": 1}),
    ("messageStop", {"stopReason": "tool_use"}),
    ("metadata", {"usage": {"inputTokens": 10, "outputTokens": 3, "cacheReadInputTokens": 4}}),
]


class _Script:
    """httpx transport answering from a list of (status, headers, body) per request."""

    def __init__(self, replies: list[tuple[int, dict[str, str], bytes]]) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, headers, body = self.replies.pop(0)
        return httpx.Response(status, headers=headers, content=body)


class _Static:
    async def fetch(self) -> Credentials:
        return Credentials("AKIAX", "secret", None, None, "access_key")


def _client(script: _Script, **cfg: Any) -> BedrockClient:
    base = {
        "region": "us-east-1",
        "model_id": "us.anthropic.claude-test-v1:0",
        "endpoint_url": "https://bedrock.test",
        "proxy_mode": "direct",
        "prompt_caching": True,
    }
    base.update(cfg)
    http = httpx.AsyncClient(transport=httpx.MockTransport(script))

    async def no_sleep(_: float) -> None:
        return None

    return BedrockClient(base, CachingProvider(_Static()), http, sleep=no_sleep)


def _err(t: str, msg: str, status: int = 400) -> tuple[int, dict[str, str], bytes]:
    return status, _h(t), json.dumps({"message": msg}).encode()


REQ = {
    "system": [{"text": "sys"}, {"cachePoint": {"type": "default"}}],
    "messages": [{"role": "user", "content": [{"text": "q"}]}],
    "toolConfig": {
        "tools": [{"toolSpec": {"name": "x"}}, {"cachePoint": {"type": "default"}}],
        "toolChoice": {"tool": {"name": "x"}},
    },
    "inferenceConfig": {"maxTokens": 100, "temperature": 0.2},
}


async def test_converse_stream_assembles_and_signs() -> None:
    s = _Script([(200, {}, _stream_body(TEXT_EVENTS))])
    c = _client(s)
    asm = StreamAssembler()
    deltas = []
    end = None
    async for ev in c.converse_stream(REQ):
        if ev["type"] == "stream_end":
            end = ev["data"]
            continue
        d = asm.feed(ev)
        if d:
            deltas.append(d)
    assert deltas == ["Hel", "lo"] and asm.text == "Hello"
    assert asm.stop_reason == "tool_use"
    assert asm.tool_uses == [{"toolUseId": "t1", "name": "list_problems", "input": {"limit": 5}}]
    assert asm.usage == {
        "input_tokens": 10,
        "output_tokens": 3,
        "cache_read_tokens": 4,
        "cache_write_tokens": 0,
    }
    assert end is not None and end["model_id"] == "us.anthropic.claude-test-v1:0"
    req = s.requests[0]
    assert req.url.raw_path.decode() == "/model/us.anthropic.claude-test-v1%3A0/converse-stream"
    assert req.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAX/")
    assert "modelId" not in json.loads(req.content)


async def test_converse_retries_and_fallbacks() -> None:
    ok = (
        200,
        {},
        json.dumps(
            {
                "output": {"message": {"role": "assistant", "content": []}},
                "stopReason": "end_turn",
                "usage": {},
            }
        ).encode(),
    )
    s = _Script(
        [
            _err("ThrottlingException", "slow", 429),
            _err("ThrottlingException", "slow", 429),
            _err("ValidationException", "This model does not support prompt caching (cachePoint)."),
            _err("ValidationException", "temperature is not supported for this model"),
            _err("ValidationException", "tool_choice type tool is not supported for this model"),
            ok,
        ]
    )
    c = _client(s)
    out = await c.converse(REQ)
    assert out["_meta"]["retries"] == [
        "bedrock_throttled",
        "bedrock_throttled",
        "cache_unsupported",
        "sampling_unsupported",
        "forced_tool_unsupported",
    ]
    last = json.loads(s.requests[-1].content)
    assert "cachePoint" not in json.dumps(last)
    assert "temperature" not in last["inferenceConfig"]
    assert last["toolConfig"]["toolChoice"] == {"auto": {}}
    assert c.caching_supported is False
    # a third throttle is not retried
    s2 = _Script([_err("ThrottlingException", "x", 429)] * 3)
    with pytest.raises(AssistantError) as e:
        await _client(s2).converse(REQ)
    assert e.value.code == "bedrock_throttled" and len(s2.requests) == 3


async def test_stream_no_retry_after_text_and_auth_refresh() -> None:
    body = _stream_body(TEXT_EVENTS[:2]) + eventstream.encode_exception("throttlingException", "x")
    s = _Script([(200, {}, body)])
    c = _client(s)
    got = []
    with pytest.raises(AssistantError) as e:
        async for ev in c.converse_stream(REQ):
            got.append(ev["type"])
    assert e.value.code == "bedrock_throttled" and len(s.requests) == 1
    # before any text: retried with a `retrying` event
    s = _Script(
        [
            (200, {}, eventstream.encode_exception("throttlingException", "x")),
            (200, {}, _stream_body(TEXT_EVENTS)),
        ]
    )
    types = [ev["type"] async for ev in _client(s).converse_stream(REQ)]
    assert types[0] == "retrying" and types[-1] == "stream_end"
    # an auth failure forces a credential refresh and one retry
    s = _Script(
        [_err("ExpiredTokenException", "expired", 403), (200, {}, _stream_body(TEXT_EVENTS))]
    )
    c = _client(s)
    calls = []
    orig = c.credentials.get

    async def spy(force: bool = False) -> Credentials:
        calls.append(force)
        return await orig(force=force)

    c.credentials.get = spy  # type: ignore[method-assign]
    types = [ev["type"] async for ev in c.converse_stream(REQ)]
    assert calls == [False, True] and types[-1] == "stream_end"


async def test_stream_errors_reported() -> None:
    seen: list[str] = []
    s = _Script([_err("AccessDeniedException", "You don't have access to the model", 403)])
    c = _client(s)
    c.on_error = lambda err: seen.append(err.code)
    with pytest.raises(AssistantError):
        async for _ in c.converse_stream(REQ):
            pass
    assert seen == ["bedrock_model_access"]
    s = _Script([(200, {}, VECTOR[:50] + b"\x00" * 200)] * 2)  # retried once (unavailable)
    with pytest.raises(AssistantError) as e:
        async for _ in _client(s).converse_stream(REQ):
            pass
    assert e.value.code == "bedrock_unavailable"


def test_strip_cache_points() -> None:
    out = strip_cache_points(REQ)
    assert "cachePoint" not in json.dumps(out) and "cachePoint" in json.dumps(REQ)


def test_retrieve_parse() -> None:
    from netmon.assistant.bedrock import parse_retrieve

    data = {
        "retrievalResults": [
            {
                "content": {"text": "a"},
                "score": 0.9,
                "location": {"type": "S3", "s3Location": {"uri": "s3://b/x/runbook.pdf"}},
            },
            {
                "content": {"text": "b"},
                "score": 0.2,
                "location": {"type": "WEB", "webLocation": {"url": "https://w/x"}},
            },
        ]
    }
    out = parse_retrieve(data, 0.5, 5)
    assert out == [
        {
            "text": "a",
            "score": 0.9,
            "title": "runbook.pdf",
            "source_url": "s3://b/x/runbook.pdf",
            "location_type": "S3",
        }
    ]


def test_auth_failure_keeps_the_aws_type_and_message() -> None:
    """Owner's office test: "AWS rejected the credentials" alone did not say whether the key
    id is unknown or the signature (secret / a TLS-inspecting proxy) is wrong."""
    from netmon.assistant.errors import classify

    e = classify(
        "UnrecognizedClientException", "The security token included in the request is invalid.", 403
    )
    assert e.code == "aws_auth_failed"
    assert "UnrecognizedClientException" in e.message and "security token" in e.message
    e = classify(
        "InvalidSignatureException", "The request signature we calculated does not match", 403
    )
    assert "InvalidSignatureException" in e.message


# ---------------------------------------------------------------- session token (temporary keys)
ASIA = "ASIA" + "B" * 16
TOKEN = "IQoJb3JpZ2luX2VjEJr//////////wEaCXVzLWVhc3QtMSJHMEUCIQ=="


def test_session_token_merge_encrypt_clear() -> None:
    cfg = dict(config.DEFAULT_ASSISTANT, auth_mode="access_key", access_key_id=ASIA)
    merged = config.merge(cfg, {"session_token": TOKEN, "secret_access_key": "s3cr3t"}, "k")
    enc = merged["session_token_enc"]
    assert enc.startswith("v1:") and TOKEN not in json.dumps(merged)
    assert config.session_token(merged, "k") == TOKEN
    # its own AAD: the ciphertext does not decrypt as the secret access key (no field swap)
    with pytest.raises(config.Unreadable):
        config.decrypt_opt("k", enc, config.AAD_SECRET_ACCESS_KEY)
    with pytest.raises(config.Unreadable):
        config.session_token(merged, "other-key")
    assert config.secret_unreadable(
        merged, "other-key", "session_token_enc", config.AAD_SESSION_TOKEN
    )
    # None keeps, "" clears
    assert config.merge(merged, {"session_token": None}, "k")["session_token_enc"] == enc
    assert config.merge(merged, {"session_token": ""}, "k")["session_token_enc"] is None
    assert config.temporary_credentials(merged)
    assert not config.temporary_credentials({**merged, "auth_mode": "roles_anywhere"})
    assert not config.temporary_credentials({**merged, "session_token_enc": None})
    # the token is part of the connection hash (a new token needs a new test)
    kw = {"secret_plain": "s3cr3t", "cert_pem": None, "chain_pem": None, "key_pem": None}
    h0 = config.connection_hash(merged, **kw)
    assert h0 == config.connection_hash(merged, **kw, token_plain=None)
    h1 = config.connection_hash(merged, **kw, token_plain=TOKEN)
    h2 = config.connection_hash(merged, **kw, token_plain=TOKEN + "A")
    assert len({h0, h1, h2}) == 3
    # an unreadable stored token is reported by required_errors (enable / test)
    errs = config.required_errors(merged, "other-key", has_cert=False, has_key=False)
    assert any(e["loc"][-1] == "session_token" for e in errs)
    ok = config.required_errors(merged, "k", has_cert=False, has_key=False)
    assert not any(e["loc"][-1] == "session_token" for e in ok)


def test_session_token_schema_and_key_ids() -> None:
    from pydantic import ValidationError

    from netmon.schemas.assistant import AssistantFields

    assert config.ACCESS_KEY_RE.match(ASIA) and config.ACCESS_KEY_RE.match("AKIA" + "C" * 16)
    assert config.is_temporary_key(ASIA) and not config.is_temporary_key("AKIA" + "C" * 16)
    assert not config.is_temporary_key(None)
    f = AssistantFields(access_key_id=ASIA, session_token=TOKEN[:20] + "\n " + TOKEN[20:])
    assert f.session_token == TOKEN and f.changes()["session_token"] == TOKEN
    assert AssistantFields(session_token="").changes()["session_token"] == ""
    assert "session_token" not in AssistantFields(region="us-east-1").changes()
    for bad in ("abc\r\nX-Evil: 1", "tok<en>", "x" * 4097):
        with pytest.raises(ValidationError):
            AssistantFields(session_token=bad)
    assert len(AssistantFields(session_token="A" * 4096).session_token or "") == 4096


async def test_static_provider_session_token_signed() -> None:
    from netmon.assistant.credentials import StaticProvider

    creds = await StaticProvider(ASIA, SK, TOKEN).fetch()
    assert creds.session_token == TOKEN and creds.source == "access_key"
    assert TOKEN not in repr(creds)
    assert (await StaticProvider(AK, SK).fetch()).session_token is None
    assert (await StaticProvider(AK, SK, "").fetch()).session_token is None
    signed = sigv4.sign_v4(
        "POST",
        "https://bedrock-runtime.us-east-1.amazonaws.com/model/x/converse",
        {"Content-Type": "application/json"},
        b"{}",
        access_key=creds.access_key_id,
        secret_key=creds.secret_access_key,
        session_token=creds.session_token,
        region="us-east-1",
        service="bedrock",
        amzdate="20261111T120000Z",
    ).headers
    assert signed["X-Amz-Security-Token"] == TOKEN
    assert "x-amz-security-token" in signed["Authorization"]


def test_expired_token_mapping() -> None:
    for etype in ("ExpiredTokenException", "ExpiredToken"):
        err = map_error(
            403,
            {"x-amzn-ErrorType": f"{etype}:http://internal.amazon.com/coral/"},
            b'{"message": "The security token included in the request is expired"}',
        )
        assert err.code == "aws_token_expired", etype
        assert "session token has expired" in err.message and err.status == 502
        assert err.retry_policy == "auth" and err.is_error_state
    err = map_stream_exception("ExpiredTokenException", {"message": "expired"})
    assert err.code == "aws_token_expired"
    # other credential errors keep aws_auth_failed
    err = map_error(403, {"x-amzn-ErrorType": "UnrecognizedClientException"}, b"{}")
    assert err.code == "aws_auth_failed"
    from netmon.assistant.errors import make

    req = make("aws_session_token_required")
    assert "ASIA" in req.message and "session token" in req.message and req.is_error_state


def test_model_errors_keep_the_aws_message() -> None:
    from netmon.assistant.errors import classify

    e = classify(
        "ResourceNotFoundException",
        "Model use case details have not been submitted for this account.",
        404,
        region="us-east-1",
    )
    assert e.code == "bedrock_model_access" and "use case details" in e.message
    e = classify(
        "ValidationException", "The provided model identifier is invalid.", 400, region="us-east-1"
    )
    assert e.code == "bedrock_model_invalid" and "provided model identifier is invalid" in e.message
