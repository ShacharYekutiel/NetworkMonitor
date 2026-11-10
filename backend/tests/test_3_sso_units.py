"""[3] SSO pure parts: NTLM detection, principals, keytab parser, OIDC claim validation,
PKCE, open-redirect guard, cookie sealing (data-model § Users, SSO vectors)."""

from __future__ import annotations

import base64
import hashlib
import struct
import time

import pytest

from netmon.auth import kerberos_pure as kp
from netmon.auth import oidc

# ---------------------------------------------------------------- NTLM / mech detection
NTLM_RAW = b"NTLMSSP\x00\x01\x00\x00\x00" + b"\x07\x82\x08\xa2" + b"\x00" * 16


def test_raw_ntlm_is_ntlm() -> None:
    assert kp.token_mech(NTLM_RAW) == "ntlm"


def test_spnego_kerberos_mechs() -> None:
    tok = kp.spnego_init([kp.OID_MS_KRB5, kp.OID_KRB5], b"\x60\x05kerb!")
    assert kp.token_mech(tok) == "kerberos"


def test_spnego_ntlm_only() -> None:
    assert kp.token_mech(kp.spnego_init([kp.OID_NTLM], NTLM_RAW)) == "ntlm"
    assert kp.token_mech(kp.spnego_init([kp.OID_NTLM])) == "ntlm"


def test_spnego_kerberos_mechs_but_ntlm_token() -> None:
    assert kp.token_mech(kp.spnego_init([kp.OID_MS_KRB5, kp.OID_KRB5], NTLM_RAW)) == "ntlm"


def test_spnego_ntlm_preferred_first() -> None:
    assert kp.token_mech(kp.spnego_init([kp.OID_NTLM, kp.OID_KRB5])) == "ntlm"


def test_raw_krb5_gss_token() -> None:
    tok = kp.der(0x60, kp.encode_oid(kp.OID_KRB5) + b"\x01\x00" + b"\x6e\x00")
    assert kp.token_mech(tok) == "kerberos"


@pytest.mark.parametrize(
    "tok",
    [
        b"",
        b"garbage",
        b"\x60\x80",
        b"\x60\x05\x06\x03\x2b\x06",  # truncated
        b"\xa1\x03\x30\x01\x00",  # NegTokenResp as first leg
        kp.der(0x60, kp.encode_oid("1.2.3.4") + b"\x00"),
        kp.spnego_init(["1.2.3.4"]),
        b"\x00" * (70 * 1024),
    ],
)
def test_garbage_is_invalid(tok: bytes) -> None:
    assert kp.token_mech(tok) == "invalid"


def test_oid_roundtrip() -> None:
    for oid in (kp.OID_SPNEGO, kp.OID_KRB5, kp.OID_MS_KRB5, kp.OID_NTLM):
        enc = kp.encode_oid(oid)
        assert kp.decode_oid(enc[2:]) == oid
    assert kp.encode_oid(kp.OID_SPNEGO) == bytes.fromhex("06062b0601050502")
    assert kp.encode_oid(kp.OID_KRB5) == bytes.fromhex("06092a864886f712010202")


def test_negotiate_header() -> None:
    assert kp.negotiate_token(None) is None
    assert kp.negotiate_token("Basic abc") is None
    assert kp.negotiate_token("Negotiate") == b""
    assert kp.negotiate_token("Negotiate !!!") == b""
    assert kp.negotiate_token("negotiate " + base64.b64encode(b"xyz").decode()) == b"xyz"


# ---------------------------------------------------------------- principals
def test_split_principal() -> None:
    assert kp.split_principal("jdoe@MNIDOM.NET") == ("jdoe", "MNIDOM.NET")
    assert kp.split_principal("JDoe@mnidom.net") == ("JDoe", "MNIDOM.NET")
    assert kp.split_principal("HTTP/x@MNIDOM.NET") is None
    assert kp.split_principal("a@b@C") is None
    assert kp.split_principal("@R") is None
    assert kp.split_principal("jdoe") is None
    assert kp.default_realms("mnidom.net") == ["MNIDOM.NET"]
    assert kp.default_realms("") == []


# ---------------------------------------------------------------- keytab
def _counted(b: bytes) -> bytes:
    return struct.pack(">H", len(b)) + b


def keytab_entry(principal: str, kvno: int, enctype: int, kvno32: int | None = None) -> bytes:
    name, realm = principal.rsplit("@", 1)
    comps = name.split("/")
    body = struct.pack(">H", len(comps)) + _counted(realm.encode())
    body += b"".join(_counted(c.encode()) for c in comps)
    body += struct.pack(">IIB", 1, 1700000000, kvno & 0xFF)
    body += struct.pack(">H", enctype) + _counted(b"K" * (32 if enctype != 23 else 16))
    if kvno32 is not None:
        body += struct.pack(">I", kvno32)
    return struct.pack(">i", len(body)) + body


def make_keytab(*entries: bytes, version: int = 0x02) -> bytes:
    return bytes([0x05, version]) + b"".join(entries)


def test_keytab_parse() -> None:
    data = make_keytab(
        keytab_entry("HTTP/netmonitor.mnidom.net@MNIDOM.NET", 3, 18),
        struct.pack(">i", -12) + b"\x00" * 12,  # deleted hole
        keytab_entry("HTTP/netmonitor.mnidom.net@MNIDOM.NET", 3, 17, kvno32=300),
    )
    entries = kp.parse_keytab(data)
    assert [e.out() for e in entries] == [
        {
            "principal": "HTTP/netmonitor.mnidom.net@MNIDOM.NET",
            "kvno": 3,
            "enctype": 18,
            "enctype_name": "aes256-cts-hmac-sha1-96",
        },
        {
            "principal": "HTTP/netmonitor.mnidom.net@MNIDOM.NET",
            "kvno": 300,
            "enctype": 17,
            "enctype_name": "aes128-cts-hmac-sha1-96",
        },
    ]
    assert kp.keytab_warnings(entries) == []
    assert b"KKKK" not in repr([e.out() for e in entries]).encode()


def test_keytab_warnings() -> None:
    rc4 = kp.parse_keytab(make_keytab(keytab_entry("HTTP/x.y@R", 1, 23)))
    assert kp.keytab_warnings(rc4) == ["rc4_only"]
    nohttp = kp.parse_keytab(make_keytab(keytab_entry("host/x.y@R", 1, 18)))
    assert kp.keytab_warnings(nohttp) == ["no_http_principal"]


@pytest.mark.parametrize(
    "data",
    [
        make_keytab(keytab_entry("HTTP/x@R", 1, 18), version=0x01),  # 0x0501
        make_keytab(keytab_entry("HTTP/x@R", 1, 18))[:-5],  # truncated
        b"\x05\x02",  # no entries
        b"\x04\x02abc",
        b"",
        b"\x05\x02" + b"\x00" * (65 * 1024),
    ],
)
def test_keytab_invalid(data: bytes) -> None:
    with pytest.raises(kp.KeytabError):
        kp.parse_keytab(data)


def test_krb5_conf() -> None:
    conf = kp.krb5_conf("MNIDOM.NET")
    assert "default_realm = MNIDOM.NET" in conf
    assert "ignore_acceptor_hostname = true" in conf
    assert "rdns = false" in conf and "dns_canonicalize_hostname = false" in conf


# ---------------------------------------------------------------- OIDC
TENANT = "00000000-0000-0000-0000-0000000000aa"
CLIENT = "11111111-1111-1111-1111-111111111111"
CFG = {
    "tenant_id": TENANT,
    "client_id": CLIENT,
    "role_source": "app_roles",
    "admin_values": ["NetMon.Admin"],
    "viewer_values": ["NetMon.Viewer"],
}
NOW = 1_800_000_000.0


def claims(**kw: object) -> dict:
    base = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": CLIENT,
        "tid": TENANT,
        "exp": NOW + 3600,
        "nbf": NOW - 10,
        "iat": NOW - 10,
        "nonce": "n0nce",
        "roles": ["NetMon.Viewer"],
        "oid": "22222222-2222-2222-2222-222222222222",
        "preferred_username": "JDoe@mnidom.net",
    }
    base.update(kw)
    return base


def v(c: dict, cfg: dict | None = None, nonce: str = "n0nce") -> oidc.ClaimsResult:
    return oidc.validate_claims(c, cfg or CFG, nonce, NOW, "https://login.microsoftonline.com")


def test_oidc_valid_and_roles() -> None:
    r = v(claims())
    assert r.ok and r.role == "viewer"
    assert v(claims(roles=["NetMon.Admin", "NetMon.Viewer"])).role == "admin"
    r = v(claims(roles=[]))
    assert not r.ok and r.error == "not_in_group"


@pytest.mark.parametrize(
    "bad",
    [
        {"iss": "https://login.microsoftonline.com/99999999-0000-0000-0000-000000000000/v2.0"},
        {"aud": "33333333-3333-3333-3333-333333333333"},
        {"tid": "99999999-0000-0000-0000-000000000000"},
        {"exp": NOW - 301},
        {"nonce": "other"},
        {"nbf": NOW + 400},
        {"iat": NOW + 400},
    ],
)
def test_oidc_failures(bad: dict) -> None:
    r = v(claims(**bad))
    assert not r.ok and r.error == "sso_failed"


def test_oidc_leeway_and_missing_nonce() -> None:
    assert v(claims(exp=NOW - 299)).ok
    assert v(claims(), nonce="").error == "sso_failed"
    c = claims()
    del c["nonce"]
    assert v(c).error == "sso_failed"
    assert v(claims(aud=[CLIENT, "x"])).ok


def test_oidc_groups_source_and_overage() -> None:
    cfg = {**CFG, "role_source": "groups", "admin_values": ["AAAA-1"], "viewer_values": ["b"]}
    assert v(claims(groups=["aaaa-1"]), cfg).role == "admin"
    assert v(claims(groups=["B"]), cfg).role == "viewer"
    r = v(claims(_claim_names={"groups": "src1"}), cfg)
    assert not r.ok and r.error == "oidc_group_overage"


def test_pkce_and_next() -> None:
    ver = oidc.pkce_verifier()
    assert 43 <= len(ver) <= 128
    expect = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).rstrip(b"=")
    assert oidc.pkce_challenge(ver) == expect.decode()
    # RFC 7636 appendix B
    assert (
        oidc.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )
    assert oidc.safe_next("/devices/3?x=1") == "/devices/3?x=1"
    for bad in ("//evil.com", "/\\evil.com", "https://evil.com", "", None, "devices", "/a\nb"):
        assert oidc.safe_next(bad) == "/"


def test_cookie_seal_open() -> None:
    sealed = oidc.seal_cookie("k" * 32, {"state": "s", "exp": time.time() + 60})
    assert oidc.open_cookie("k" * 32, sealed, time.time())["state"] == "s"
    assert oidc.open_cookie("other-key", sealed, time.time()) is None
    assert oidc.open_cookie("k" * 32, sealed, time.time() + 120) is None
    assert oidc.open_cookie("k" * 32, "garbage", time.time()) is None


def test_oidc_config_errors() -> None:
    sk = "s" * 32
    good = {**CFG, "client_secret_enc": oidc.encrypt_secret("sec", sk)}
    assert oidc.oidc_config_errors(good, sk) == []
    errs = oidc.oidc_config_errors({"tenant_id": "x", "admin_values": []}, sk)
    fields = {e["loc"][-1] for e in errs}
    assert fields == {"tenant_id", "client_id", "client_secret", "admin_values"}
    assert oidc.decrypt_secret(good, "other") is None


# ---------------------------------------------------------------- QA phase 3: JWKS rotation
async def test_jwks_unknown_kid_refetch_not_blocked_by_bogus_kid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bogus kid refetches at t=0; the tenant rotates its key at t=30 s; a token with the new
    kid at t=90 s must refetch (cache > 60 s old, kid not yet refetched) instead of waiting
    5 min. The same unknown kid still refetches at most once per 5 min."""
    import httpx
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    monkeypatch.setenv("NETMON_OIDC_AUTHORITY", "https://login.example.test")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = __import__("json").loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    state = {"kids": ["k1"], "fetches": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            base = "https://login.example.test/t"
            return httpx.Response(200, json={"authorization_endpoint": f"{base}/a",
                                             "token_endpoint": f"{base}/t",
                                             "jwks_uri": f"{base}/keys"})  # fmt: skip
        state["fetches"] += 1
        return httpx.Response(200, json={"keys": [{**jwk, "kid": k} for k in state["kids"]]})

    now = [1000.0]
    monkeypatch.setattr(oidc.time, "monotonic", lambda: now[0])
    c = oidc.OidcClient()
    c.transport = httpx.MockTransport(handler)
    assert await c.key_for("t", "k1") is not None and state["fetches"] == 1
    now[0] += 1
    with pytest.raises(oidc.OidcError):
        await c.key_for("t", "bogus")
    assert state["fetches"] == 1  # cache < 60 s old: no refetch
    now[0] += 60
    with pytest.raises(oidc.OidcError):
        await c.key_for("t", "bogus")
    assert state["fetches"] == 2  # bogus refetched once
    now[0] += 30
    state["kids"] = ["k2"]  # rotation
    now[0] += 30
    with pytest.raises(oidc.OidcError):
        await c.key_for("t", "bogus")
    assert state["fetches"] == 2  # the same unknown kid: at most once per 5 min
    now[0] += 31
    assert await c.key_for("t", "k2") is not None  # new kid: not blocked for 5 min
    assert state["fetches"] == 3
    now[0] += 300
    with pytest.raises(oidc.OidcError):
        await c.key_for("t", "bogus")
    assert state["fetches"] == 4  # 5 min later the bogus kid may refetch again
