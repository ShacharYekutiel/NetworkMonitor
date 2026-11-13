"""[4] PR 4-0a pure units: the data-model vectors (chain key, session name, source identity,
`map_sts`, `hints_for`), ARN / access / override validation (doubled ARN), the XML guard, the
endpoint builder, the STS query body, the rendered policies and the 1.4.x shims."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from netmon.aws import awsxml, certs, endpoints, policies, sigv4, sts
from netmon.aws import connections as conns
from netmon.aws.credcache import chain_key
from netmon.aws.errors import AssistantError, hints_for, make, map_sts

BASE = "arn:aws:sts::111111111111:assumed-role/netmon-base/1f71c5"
TARGET = "arn:aws:iam::222222222222:role/netmon-bedrock"
TA = (
    "arn:aws:rolesanywhere:il-central-1:111111111111:trust-anchor/"
    "0f0e0d0c-1111-2222-3333-444455556666"
)
PROFILE = (
    "arn:aws:rolesanywhere:il-central-1:111111111111:profile/aaaa1111-2222-3333-4444-555566667777"
)
BASE_ROLE = "arn:aws:iam::111111111111:role/netmon-base"


def _err_xml(code: str, message: str) -> bytes:
    return (
        '<ErrorResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><Error><Type>Sender'
        f"</Type><Code>{code}</Code><Message>{message}</Message></Error><RequestId>x</RequestId>"
        "</ErrorResponse>"
    ).encode()


# ---------------------------------------------------------------- vectors (data-model § [4])
def test_chain_key_vector() -> None:
    assert chain_key(1, "h1", {"mode": "direct"}) == (1, "h1", "direct", None, None)
    k = chain_key(1, "h1", {"mode": "assume_role", "role_arn": TARGET, "external_id": "ext"})
    assert k == (1, "h1", "assume_role", TARGET, hashlib.sha256(b"ext").hexdigest())
    # a save of connection 1 (hash h2) changes both keys
    assert chain_key(1, "h2", {"mode": "direct"}) != chain_key(1, "h1", {"mode": "direct"})
    assert (
        chain_key(1, "h2", {"mode": "assume_role", "role_arn": TARGET, "external_id": "ext"}) != k
    )
    # direct drops a stray role / ExternalId
    assert chain_key(1, "h", {"mode": "direct", "role_arn": TARGET})[3] is None


def test_session_name_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NETMON_NODE_NAME", raising=False)
    assert sts.session_name("assistant") == "netmon-assistant"
    monkeypatch.setenv("NETMON_NODE_NAME", "office 1")
    assert sts.session_name("assistant") == "netmon-assistant-office-1"
    long = sts.session_name("assistant", node="n" * 60)
    assert len(long) == 64 and long.startswith("netmon-assistant-nnn")


def _cert(cn: str | None, serial: int) -> x509.Certificate:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, cn)]
        if cn
        else [x509.NameAttribute(NameOID.ORGANIZATION_NAME, "QA")]
    )
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )


def test_source_identity_vector() -> None:
    assert certs.source_identity(_cert("netmonitor.example.net", 5)) == "CN=netmonitor.example.net"
    cn62 = "a" * 50 + ".example.net"
    assert len(cn62) == 62 and certs.source_identity(_cert(cn62, 5)) == cn62
    assert certs.source_identity(_cert(None, 291)) == "ID=0123"
    c = _cert("NetMonitor Server", 7)
    assert certs.source_identity(c) == "CN=NetMonitor Server"
    assert certs.cn_warnings("NetMonitor Server") == ["cn_not_source_identity_safe"]
    assert certs.cn_warnings("netmonitor.example.net") == []
    info = certs.cert_info(c, [], source="uploaded", uploaded_at=None, key_unreadable=False)
    assert info["subject_cn"] == "NetMonitor Server" and info["issuer_cn"] == "NetMonitor Server"
    assert info["warnings"] == ["cn_not_source_identity_safe"]


def test_map_sts_vector() -> None:
    msg = f"User: {BASE} is not authorized to perform: sts:SetSourceIdentity on resource: {TARGET}"
    err = map_sts(403, _err_xml("AccessDenied", msg))
    assert err.code == "aws_assume_role_denied" and err.status == 502
    assert err.details[0] == {
        "hop": 1,
        "from_arn": BASE,
        "role_arn": TARGET,
        "action": "sts:SetSourceIdentity",
    }
    plain = map_sts(403, _err_xml("AccessDenied", "Access denied"), from_arn="F", role_arn="R")
    assert plain.details[0] == {"hop": 1, "from_arn": "F", "role_arn": "R", "action": None}
    assert map_sts(400, _err_xml("ValidationError", "bad")).code == "aws_assume_role_invalid"
    rd = map_sts(403, _err_xml("RegionDisabledException", "STS is not activated"))
    assert (
        rd.code == "aws_assume_role_invalid"
        and rd.details[0]["aws_code"] == "RegionDisabledException"
    )
    assert map_sts(403, _err_xml("ExpiredToken", "expired")).code == "aws_token_expired"
    assert map_sts(403, _err_xml("InvalidClientTokenId", "x")).code == "aws_auth_failed"
    assert map_sts(403, _err_xml("SignatureDoesNotMatch", "Signature expired: 2020")).code == (
        "aws_clock_skew"
    )
    assert map_sts(400, _err_xml("Throttling", "Rate exceeded")).code == "aws_throttled"
    assert map_sts(403, b"<!DOCTYPE x [<!ENTITY a 'b'>]><x/>").code == "aws_network"
    # GetCallerIdentity is never "assume role denied"
    assert (
        map_sts(403, _err_xml("AccessDenied", "x"), op="GetCallerIdentity").code
        == "aws_auth_failed"
    )


def _codes(hints: list[dict[str, Any]]) -> list[str]:
    return [h["code"] for h in hints]


def test_hints_for_vectors() -> None:
    def denied(action: str | None) -> AssistantError:
        return make(
            "aws_assume_role_denied",
            aws="x",
            details=[{"hop": 1, "from_arn": BASE, "role_arn": TARGET, "action": action}],
        )

    ra_ctx = {
        "base_kind": "roles_anywhere",
        "source_identity": "CN=netmonitor.example.net",
        "base_role_arn": BASE_ROLE,
    }
    h = hints_for("hop", denied("sts:SetSourceIdentity"), ra_ctx)
    assert _codes(h) == ["set_source_identity"]
    assert h[0]["values"] == {
        "source_identity": "CN=netmonitor.example.net",
        "from_arn": BASE,
        "role_arn": TARGET,
    }
    assert TARGET in h[0]["text"] and "two" in h[0]["text"]
    assert _codes(hints_for("hop", denied("sts:TagSession"), ra_ctx)) == ["tag_session"]
    assert _codes(hints_for("hop", denied("sts:AssumeRole"), ra_ctx)) == [
        "caller_policy",
        "trust_policy",
        "external_id",
        "set_source_identity",
        "scp",
    ]
    ak = hints_for("hop", denied("sts:AssumeRole"), {"base_kind": "access_key"})
    assert _codes(ak) == ["caller_policy", "trust_policy", "external_id", "scp"]
    assert _codes(hints_for("hop", denied(None), {"base_kind": "access_key"}))[0] == "caller_policy"
    rd = make("aws_assume_role_invalid", aws="x", details=[{"aws_code": "RegionDisabledException"}])
    h = hints_for("hop", rd, {"region": "il-central-1", "account": "111111111111"})
    assert _codes(h) == ["sts_region"] and h[0]["values"]["region"] == "il-central-1"
    assert _codes(hints_for("hop", make("aws_assume_role_invalid", aws="RoleArn invalid"), {})) == [
        "role_arn_invalid"
    ]
    dur = make("aws_assume_role_invalid", aws="The requested DurationSeconds exceeds the limit")
    assert _codes(hints_for("hop", dur, {})) == ["max_session"]
    ctx = {
        "trust_anchor_arn": TA,
        "cert_cn": "netmonitor.example.net",
        "issuer_cn": "Corp-CA",
        "profile_arn": PROFILE,
        "role_arn": BASE_ROLE,
    }
    unable = make(
        "rolesanywhere_failed",
        aws=f"AccessDeniedException: Unable to assume role for arn: {BASE_ROLE}",
    )
    h = hints_for("credentials", unable, ctx)
    assert _codes(h) == ["ra_trust_policy", "ra_subject_cn", "ra_issuer_cn", "ra_profile_role"]
    merged: dict[str, str] = {}
    for x in h:
        merged.update(x["values"])
    assert merged == {
        "trust_anchor_arn": TA,
        "cert_cn": "netmonitor.example.net",
        "issuer_cn": "Corp-CA",
        "profile_arn": PROFILE,
        "role_arn": BASE_ROLE,
    }
    assert "trust-anchor/arn:aws:rolesanywhere" in h[0]["text"]  # the doubled-ARN mistake named
    untrusted = make("rolesanywhere_failed", aws="Untrusted signing certificate")
    assert _codes(hints_for("credentials", untrusted, ctx)) == ["ra_untrusted_cert"]
    assert _codes(
        hints_for("credentials", make("rolesanywhere_failed", aws="Profile is disabled"), ctx)
    ) == ["ra_disabled"]
    assert _codes(
        hints_for("credentials", make("rolesanywhere_failed", aws="cert revoked"), ctx)
    ) == ["ra_revoked"]
    net = make("aws_network", host="sts.il-central-1.amazonaws.com", aws="ConnectError")
    net.details = [{"host": "sts.il-central-1.amazonaws.com", "via": "direct"}]
    h = hints_for("identity", net, {"region": "il-central-1"})
    assert _codes(h) == ["vpc_endpoint_or_proxy"]
    assert h[0]["values"]["host"] == "sts.il-central-1.amazonaws.com"
    access = make("bedrock_model_access", region="us-east-1")
    assert _codes(hints_for("converse", access, {"bedrock_region": "us-east-1"})) == [
        "model_access"
    ]


# ---------------------------------------------------------------- validation
def _view(**kw: Any) -> dict[str, Any]:
    row = conns.new_row(
        {"name": "c", "auth_mode": "roles_anywhere", "region": "il-central-1", **kw}, "sk"
    )
    return conns.view(row)


def test_doubled_and_invalid_arns() -> None:
    doubled = "arn:aws:rolesanywhere:il-central-1:111111111111:trust-anchor/" + TA
    errs = conns.field_errors(_view(ra_trust_anchor_arn=doubled), test_mode=False)
    assert errs == [{"loc": ["body", "ra_trust_anchor_arn"], "msg": conns.DOUBLED_ARN}]
    errs = conns.field_errors(
        _view(ra_role_arn="arn:aws-us-gov:iam::111111111111:role/x"), test_mode=False
    )
    assert (
        errs[0]["loc"][-1] == "ra_role_arn" and errs[0]["msg"] == "invalid ARN"
    )  # partition aws only
    ok = _view(ra_trust_anchor_arn=TA, ra_profile_arn=PROFILE, ra_role_arn=BASE_ROLE)
    assert conns.field_errors(ok, test_mode=False) == []
    assert conns.arn_account_warning({**ok, "ra_role_arn": TARGET})
    # access rules
    a = conns.normalize_access({"mode": "assume_role", "role_arn": doubled})
    assert conns.access_errors(a, ok)[0]["msg"] == conns.DOUBLED_ARN
    a = conns.normalize_access({"mode": "assume_role", "role_arn": TARGET, "external_id": "a b"})
    assert conns.access_errors(a, ok)[0]["loc"][-1] == "external_id"
    a = conns.normalize_access({"mode": "ra_role", "role_arn": TARGET})
    assert conns.access_errors(a, ok)[0]["msg"] == "must be in the profile's account"
    same = conns.normalize_access(
        {"mode": "ra_role", "role_arn": "arn:aws:iam::111111111111:role/r2"}
    )
    assert conns.access_errors(same, ok) == []
    ak = _view(auth_mode="access_key")
    assert conns.access_errors(same, ak)[0]["msg"] == "only with a Roles Anywhere connection"
    assert conns.normalize_access({"mode": "direct", "role_arn": TARGET, "external_id": "x"}) == {
        "mode": "direct",
        "role_arn": None,
        "external_id": None,
    }
    # name rules
    assert conns.field_errors({**ok, "_name": " "}, test_mode=False)[0]["loc"][-1] == "name"


def test_endpoint_overrides_and_builder() -> None:
    good = [
        {"service": "sts", "region": "il-central-1", "url": "https://10.0.1.5"},
        {"service": "rolesanywhere", "region": "il-central-1", "url": "https://10.0.1.6"},
    ]
    assert endpoints.override_errors(good, test_mode=False) == []
    bad = [
        {"service": "ec2", "region": "il-central-1", "url": "https://10.9.9.9"},
        {"service": "sts", "region": "Israel", "url": "https://10.9.9.9"},
        {"service": "sts", "region": "il-central-1", "url": "https://169.254.169.254"},
        {"service": "sts", "region": "il-central-1", "url": "https://10.9.9.9/path"},
        {"service": "sts", "region": "il-central-1", "url": "https://10.9.9.9"},
    ]
    errs = endpoints.override_errors(bad, test_mode=False)
    assert len(errs) == 6 and "duplicate" in errs[-1] and "path" in errs[3]
    assert endpoints.endpoint("sts", "il-central-1", good) == "https://10.0.1.5"
    assert endpoints.endpoint("sts", "us-east-1", good) == "https://sts.us-east-1.amazonaws.com"
    with pytest.raises(ValueError):
        endpoints.endpoint("sts", "us-east-1/../x")
    with pytest.raises(ValueError):
        endpoints.endpoint("sts.evil.com#", "us-east-1")
    v = conns.view(
        conns.new_row(
            {
                "name": "c",
                "auth_mode": "access_key",
                "region": "il-central-1",
                "endpoint_overrides": good,
            },
            "sk",
        )
    )
    assert endpoints.sts_endpoint(v) == "https://10.0.1.5"
    assert endpoints.ra_endpoint(v) == "https://10.0.1.6"


def test_awsxml_guard_and_parse() -> None:
    ok = (
        '<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleResult>'
        "<SourceIdentity>CN=x</SourceIdentity><AssumedRoleUser><Arn>arn:aws:sts::2:assumed-role/r/s"
        "</Arn></AssumedRoleUser><Credentials><AccessKeyId>ASIAX</AccessKeyId><SecretAccessKey>s"
        "</SecretAccessKey><SessionToken>t</SessionToken><Expiration>2026-11-13T12:00:00Z"
        "</Expiration></Credentials></AssumeRoleResult></AssumeRoleResponse>"
    )
    d = awsxml.parse_assume_role(ok)
    assert d["access_key_id"] == "ASIAX" and d["source_identity"] == "CN=x"
    for hostile in (
        '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>',
        "<!ENTITY a 'b'><x/>",
        "<x>" + "a" * (awsxml.MAX_BYTES + 1) + "</x>",
        "<x><unclosed></x>",
    ):
        with pytest.raises(awsxml.XmlError):
            awsxml.parse(hostile)
    assert awsxml.parse_error(_err_xml("AccessDenied", "m")) == ("AccessDenied", "m")


def test_sts_query_body_and_signing_service() -> None:
    body = sigv4.query_body(
        "AssumeRole", {"RoleArn": TARGET, "ExternalId": None, "RoleSessionName": "netmon-assistant"}
    )
    assert body == (
        b"Action=AssumeRole&RoleArn=arn%3Aaws%3Aiam%3A%3A222222222222%3Arole%2Fnetmon-bedrock"
        b"&RoleSessionName=netmon-assistant&Version=2011-06-15"
    )
    assert sts.account_of(TARGET) == "222222222222" and sts.account_of("x") is None
    assert sts.role_arn_of_session(BASE) == "arn:aws:iam::111111111111:role/netmon-base"


# ---------------------------------------------------------------- policies
def test_policies_render() -> None:
    v = _view(ra_trust_anchor_arn=TA, ra_profile_arn=PROFILE, ra_role_arn=BASE_ROLE)
    cert = {
        "cert_cn": "netmonitor.example.net",
        "issuer_cn": "Corp-CA",
        "source_identity": "CN=netmonitor.example.net",
    }
    out = policies.render(v, cert, role_arn=TARGET, external_id="ExtId123")
    base = out["base_trust_policy"]["Statement"][0]
    assert base["Condition"]["ArnEquals"]["aws:SourceArn"] == TA
    assert str(out["base_trust_policy"]).count("arn:aws:rolesanywhere") == 1  # once
    assert base["Condition"]["StringEquals"] == {
        "aws:PrincipalTag/x509Subject/CN": "netmonitor.example.net",
        "aws:PrincipalTag/x509Issuer/CN": "Corp-CA",
    }
    assert set(base["Action"]) == {"sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"}
    caller = out["hop_caller_policy"]["Statement"][0]
    assert caller["Resource"] == TARGET and "sts:SetSourceIdentity" in caller["Action"]
    trust = out["hop_trust_policy"]["Statement"][0]
    assert trust["Principal"] == {"AWS": BASE_ROLE}
    assert "sts:TagSession" not in trust["Action"]  # fact 5
    assert trust["Condition"]["StringEquals"] == {
        "sts:ExternalId": "ExtId123",
        "aws:SourceIdentity": "CN=netmonitor.example.net",
    }
    ak = conns.view(
        {
            **conns.new_row({"name": "k", "auth_mode": "access_key", "region": "us-east-1"}, "sk"),
            "last_test": {"arn": "arn:aws:iam::3:user/u"},
        }
    )
    out = policies.render(ak, {}, role_arn=TARGET)
    assert out["base_trust_policy"] is None and out["notes"]
    assert out["hop_trust_policy"]["Statement"][0]["Principal"] == {"AWS": "arn:aws:iam::3:user/u"}
    assert "Condition" not in out["hop_trust_policy"]["Statement"][0]


# ---------------------------------------------------------------- shims / secrets
def test_shims_are_the_moved_modules() -> None:
    from netmon.assistant import certs as a_certs
    from netmon.assistant import credentials as a_cred
    from netmon.assistant import errors as a_err
    from netmon.assistant import sigv4 as a_sig
    from netmon.assistant import transport as a_tp
    from netmon.assistant.testconn import TestRegistry
    from netmon.aws import credentials, errors, transport
    from netmon.services import testids

    assert a_certs is certs and a_cred is credentials and a_err is errors and a_sig is sigv4
    assert a_tp is transport and TestRegistry is testids.TestRegistry
    assert errors.AwsError is errors.AssistantError


def test_aad_scope_and_legacy_identity() -> None:
    from netmon.assistant import config
    from netmon.services import crypto

    # the legacy row's AADs are byte-identical to 1.4.x
    assert conns.aad("assistant", "secret_access_key") == config.AAD_SECRET_ACCESS_KEY
    assert conns.aad("assistant", "ra_csr_key") == config.AAD_RA_CSR_KEY
    row = conns.new_row(
        {
            "name": "n",
            "auth_mode": "access_key",
            "region": "us-east-1",
            "secret_access_key": "s3cr3t",
        },
        "sk",
    )
    assert row["aad_scope"].startswith("aws.conn.") and len(row["aad_scope"]) == 25
    v = conns.view(row)
    assert conns.decrypt_field(v, "secret_access_key_enc", "sk") == "s3cr3t"
    with pytest.raises(crypto.DecryptError):  # another row's AAD does not open it
        crypto.decrypt("sk", v["secret_access_key_enc"], config.AAD_SECRET_ACCESS_KEY)
    # the connection hash binds secrets by digest, overrides order-independent
    h = conns.connection_hash(v, "sk")
    v2 = conns.view(conns.merge(row, {"secret_access_key": "other"}, "sk"))
    assert conns.connection_hash(v2, "sk") != h
    assert conns.connection_hash(conns.view(conns.merge(row, {"name": "renamed"}, "sk")), "sk") == h
