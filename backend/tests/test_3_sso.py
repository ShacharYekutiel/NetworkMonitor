"""[3] SSO against the database: Kerberos HTTP negotiation with a mocked GSSAPI acceptor and
the in-process fake AD (role from the AD groups), keytab upload/encryption, NTLM refusal,
feature gating; OIDC code flow + PKCE against a mocked Entra (httpx MockTransport, RS256)."""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncpg
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from netmon.auth import kerberos_pure as kp
from netmon.auth import oidc
from tests.conftest import _plain, requires_db
from tests.fixtures_2b3_auth import ldap_settings
from tests.test_3_sso_units import keytab_entry, make_keytab

pytestmark = requires_db

KEYTAB = make_keytab(keytab_entry("HTTP/netmonitor.mnidom.net@MNIDOM.NET", 3, 18))


def neg(principal: str) -> dict[str, str]:
    inner = b"\x60\x10PRINCIPAL:" + principal.encode()
    tok = kp.spnego_init([kp.OID_MS_KRB5, kp.OID_KRB5], inner)
    return {"Authorization": "Negotiate " + base64.b64encode(tok).decode()}


def fake_acceptor(path: str, token: bytes) -> tuple[str, bytes | None]:
    from netmon.auth.kerberos import KerberosFailure

    assert os.path.isfile(path)
    assert (os.stat(path).st_mode & 0o777) == 0o600 or os.name == "nt"
    marker = b"PRINCIPAL:"
    i = token.find(marker)
    if i < 0 or b"BAD" in token:
        raise KerberosFailure("sso_failed", "decrypt integrity check failed")
    return token[i + len(marker) :].decode(), b"mutual-token"


async def _upload(c: httpx.AsyncClient, data: bytes) -> httpx.Response:
    return await c.post(
        "/api/sso/kerberos/keytab",
        files={"file": ("netmon-http.keytab", data, "application/octet-stream")},
    )


async def _settings_row(rt: Any, key: str) -> Any:
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        return await conn.fetchval("SELECT value FROM settings WHERE key = $1", key)
    finally:
        await conn.close()


async def _audit(rt: Any, action: str) -> list[Any]:
    conn = await asyncpg.connect(_plain(rt.settings.database_url))
    try:
        return await conn.fetch("SELECT * FROM audit_log WHERE action = $1 ORDER BY id", action)
    finally:
        await conn.close()


@pytest.fixture
async def krb(rt: Any, admin: httpx.AsyncClient, feats: Any) -> AsyncIterator[Any]:
    from netmon.auth.kerberos import KerberosManager, gss_accept

    if rt.kerberos is None:
        rt.kerberos = KerberosManager(rt)
    rt.kerberos.acceptor = fake_acceptor
    yield rt.kerberos
    await rt.kerberos.stop()
    rt.kerberos.acceptor = gss_accept


async def test_kerberos_gated_and_admin_endpoints_work_while_off(
    admin: httpx.AsyncClient, krb: Any, rt: Any
) -> None:
    r = await admin.get("/api/auth/sso/kerberos")
    assert r.status_code == 409 and r.json()["error"]["code"] == "feature_disabled"
    r = await admin.get("/api/sso/kerberos")
    assert r.status_code == 200
    body = r.json()
    assert body["keytab_set"] is False and body["state"] == "disabled"
    # turning the feature on without a keytab and without AD login → 422
    r = await admin.put("/api/settings", json={"feature_sso_kerberos_enabled": True})
    assert r.status_code == 422, r.text
    locs = {d["loc"][-1] for d in r.json()["error"]["details"]}
    assert {"keytab", "feature_ldap_enabled"} <= locs


async def test_keytab_upload_encrypted_and_listed(
    admin: httpx.AsyncClient, krb: Any, rt: Any, feats: Any
) -> None:
    r = await _upload(admin, b"\x05\x01junk")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_keytab"
    r = await _upload(admin, b"x" * (70 * 1024))
    assert r.status_code == 422
    r = await _upload(admin, KEYTAB)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["keytab_set"] is True
    assert out["keytab_entries"] == [
        {
            "principal": "HTTP/netmonitor.mnidom.net@MNIDOM.NET",
            "kvno": 3,
            "enctype": 18,
            "enctype_name": "aes256-cts-hmac-sha1-96",
        }
    ]
    assert out["warnings"] == []
    stored = json.loads(await _settings_row(rt, "kerberos"))
    assert stored["keytab_enc"].startswith("v1:")
    assert base64.b64encode(KEYTAB).decode() not in json.dumps(stored)
    r = await admin.put("/api/sso/kerberos", json={"realms": ["mnidom.net"], "auto_login": False})
    assert r.status_code == 200 and r.json()["realms"] == ["MNIDOM.NET"]
    assert r.json()["auto_login"] is False
    # delete is refused while ON
    feats(sso_kerberos=True)
    r = await admin.delete("/api/sso/kerberos/keytab")
    assert r.status_code == 409
    feats(sso_kerberos=False)
    r = await admin.delete("/api/sso/kerberos/keytab")
    assert r.status_code == 200 and r.json()["keytab_set"] is False


async def test_kerberos_sign_in_flow(
    app: Any, admin: httpx.AsyncClient, krb: Any, rt: Any, feats: Any, fake_ad: Any
) -> None:
    feats(audit=True)
    r = await admin.put("/api/settings", json={"ldap": ldap_settings(fake_ad)})
    assert r.status_code == 200, r.text
    assert (await _upload(admin, KEYTAB)).status_code == 200
    feats(ldap=True, sso_kerberos=True, audit=True)
    await krb.start()
    assert krb.state == "ready" and krb.keytab_path.is_file()
    assert os.environ["KRB5_CONFIG"].endswith("krb5.conf")
    pub = (await admin.get("/api/settings/public")).json()
    assert pub["sso"]["kerberos"] == {"auto_login": True} and pub["sso"]["oidc"] is None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.get("/api/auth/sso/kerberos")
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Negotiate"
        assert r.json()["error"]["code"] == "sso_challenge"
        # NTLM: refused, no new challenge (no credential dialog)
        ntlm = kp.spnego_init([kp.OID_NTLM], b"NTLMSSP\x00\x01\x00\x00\x00")
        r = await c.get(
            "/api/auth/sso/kerberos",
            headers={"Authorization": "Negotiate " + base64.b64encode(ntlm).decode()},
        )
        assert r.status_code == 401 and r.json()["error"]["code"] == "sso_ntlm"
        assert "www-authenticate" not in r.headers
        # bad ticket
        r = await c.get("/api/auth/sso/kerberos", headers=neg("BAD@MNIDOM.NET"))
        assert r.status_code == 401 and r.json()["error"]["code"] == "sso_failed"
        assert "www-authenticate" not in r.headers
        # foreign realm
        r = await c.get("/api/auth/sso/kerberos", headers=neg("jdoe@OTHER.REALM"))
        assert r.status_code == 401 and r.json()["error"]["code"] == "sso_failed"
        # not in a NetMon group
        r = await c.get("/api/auth/sso/kerberos", headers=neg("nogroup@MNIDOM.NET"))
        assert r.status_code == 401
        # disabled account
        r = await c.get("/api/auth/sso/kerberos", headers=neg("olduser@MNIDOM.NET"))
        assert r.status_code == 401
        # viewer via group, admin via nested group
        r = await c.get("/api/auth/sso/kerberos", headers=neg("jdoe@mnidom.net"))
        assert r.status_code == 200, r.text
        assert (
            r.headers["www-authenticate"]
            == "Negotiate " + base64.b64encode(b"mutual-token").decode()
        )
        user = r.json()["user"]
        assert user["username"] == "jdoe" and user["source"] == "ldap"
        assert user["role"] == "viewer"
        me = await c.get("/api/auth/me")
        assert me.status_code == 200 and me.json()["user"]["username"] == "jdoe"
        cookie = c.cookies.get("netmon_session")
        claims = jwt.decode(cookie, options={"verify_signature": False})
        assert claims["am"] == "kerberos" and claims["src"] == "ldap"
        assert claims["exp"] - claims["at"] <= 12 * 3600 + 5
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.get("/api/auth/sso/kerberos", headers=neg("asmith@MNIDOM.NET"))
        assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    logins = await _audit(rt, "auth.login")
    methods = [json.loads(r_["changes"]).get("method") for r_ in logins]
    assert [None, "kerberos"] in methods
    failed = await _audit(rt, "auth.login_failed")
    reasons = {r_["reason"] for r_ in failed}
    assert {"sso_ntlm", "sso_failed", "sso_realm", "not_in_group"} <= reasons
    # feature OFF: the keytab file is deleted on stop
    await krb.stop()
    assert not krb.keytab_path.exists()
    h = (await admin.get("/api/health")).json()
    assert h["kerberos"] == "disabled"


async def test_kerberos_throttle(app: Any, admin: httpx.AsyncClient, krb: Any, feats: Any) -> None:
    assert (await _upload(admin, KEYTAB)).status_code == 200
    feats(sso_kerberos=True)
    await krb.start()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        for _ in range(10):
            r = await c.get("/api/auth/sso/kerberos", headers=neg("BAD@MNIDOM.NET"))
            assert r.status_code == 401
        r = await c.get("/api/auth/sso/kerberos", headers=neg("jdoe@MNIDOM.NET"))
        assert r.status_code == 429


async def test_kerberos_unavailable_without_gssapi(admin: httpx.AsyncClient, rt: Any) -> None:
    from netmon.auth import kerberos

    ok, why = kerberos.gssapi_available()
    if ok:
        pytest.skip("python-gssapi is installed here")
    assert "gssapi" in (why or "")


# ---------------------------------------------------------------- OIDC with a mocked Entra
TENANT = "00000000-0000-0000-0000-0000000000aa"
CLIENT = "11111111-1111-1111-1111-111111111111"
AUTHORITY = "https://login.example.test"


class FakeEntra:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.claims: dict[str, Any] = {}
        self.token_requests: list[dict[str, list[str]]] = []
        self.jwks_fetches = 0

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": f"{AUTHORITY}/{TENANT}/v2.0",
                    "authorization_endpoint": f"{AUTHORITY}/{TENANT}/oauth2/v2.0/authorize",
                    "token_endpoint": f"{AUTHORITY}/{TENANT}/oauth2/v2.0/token",
                    "jwks_uri": f"{AUTHORITY}/{TENANT}/discovery/v2.0/keys",
                },
            )
        if path.endswith("/keys"):
            self.jwks_fetches += 1
            return httpx.Response(200, json=self.jwks())
        if path.endswith("/token"):
            form = parse_qs(request.content.decode())
            self.token_requests.append(form)
            if form.get("code") != ["good-code"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            token = jwt.encode(self.claims, self.key, algorithm="RS256", headers={"kid": self.kid})
            return httpx.Response(200, json={"id_token": token, "token_type": "Bearer"})
        return httpx.Response(404)


@pytest.fixture
async def entra(rt: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeEntra]:
    from netmon.auth.oidc import OidcClient

    monkeypatch.setenv("NETMON_OIDC_AUTHORITY", AUTHORITY)
    fake = FakeEntra()
    if rt.oidc is None:
        rt.oidc = OidcClient()
    rt.oidc.reset()
    rt.oidc.transport = httpx.MockTransport(fake.handler)
    yield fake
    rt.oidc.transport = None
    rt.oidc.reset()


async def _configure_oidc(admin: httpx.AsyncClient, **kw: Any) -> None:
    body = {
        "tenant_id": TENANT,
        "client_id": CLIENT,
        "client_secret": "s3cret-value",
        "role_source": "app_roles",
        "admin_values": ["NetMon.Admin"],
        "viewer_values": ["NetMon.Viewer"],
        **kw,
    }
    r = await admin.put("/api/sso/oidc", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["client_secret"] is None and out["client_secret_set"] is True
    assert out["redirect_uri"].endswith("/api/auth/oidc/callback")


async def _flow(app: Any, fake: FakeEntra, claims_kw: dict[str, Any], code: str = "good-code"):
    transport = httpx.ASGITransport(app=app)
    c = httpx.AsyncClient(transport=transport, base_url="https://testserver")
    r = await c.get("/api/auth/oidc/start", params={"next": "/devices/3"})
    assert r.status_code == 302, r.text
    loc = urlsplit(r.headers["location"])
    q = parse_qs(loc.query)
    assert loc.netloc == "login.example.test"
    assert q["code_challenge_method"] == ["S256"] and q["response_mode"] == ["query"]
    assert q["scope"] == ["openid profile email"] and q["client_id"] == [CLIENT]
    set_cookie = r.headers["set-cookie"]
    assert "netmon_oidc=" in set_cookie and "samesite=lax" in set_cookie.lower()
    assert "path=/api/auth/oidc" in set_cookie.lower() and "httponly" in set_cookie.lower()
    now = time.time()
    fake.claims = {
        "iss": f"{AUTHORITY}/{TENANT}/v2.0",
        "aud": CLIENT,
        "tid": TENANT,
        "exp": now + 3600,
        "iat": now,
        "nbf": now,
        "nonce": q["nonce"][0],
        "oid": "22222222-2222-2222-2222-22222222aaaa",
        "preferred_username": "Carol@mnidom.net",
        "name": "Carol",
        "email": "carol@mnidom.net",
        "roles": ["NetMon.Viewer"],
        **claims_kw,
    }
    r = await c.get("/api/auth/oidc/callback", params={"code": code, "state": q["state"][0]})
    return c, r, q


async def test_oidc_flow(app: Any, admin: httpx.AsyncClient, entra: FakeEntra, feats: Any) -> None:
    r = await admin.get("/api/auth/oidc/start")
    assert r.status_code == 409
    r = await admin.put("/api/settings", json={"feature_sso_oidc_enabled": True})
    assert r.status_code == 422  # not configured
    await _configure_oidc(admin)
    feats(sso_oidc=True, audit=True)
    r = await admin.get("/api/sso/oidc")
    assert r.json()["client_secret_set"] is True
    r = await admin.post("/api/sso/oidc/test")
    assert r.status_code == 200 and r.json()["jwks_keys"] == 1
    pub = (await admin.get("/api/settings/public")).json()
    assert pub["sso"]["oidc"] == {"button_label": "Sign in with Microsoft"}
    c, r, q = await _flow(app, entra, {})
    try:
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/html")
        assert '"/devices/3"' in r.text
        form = entra.token_requests[-1]
        assert form["grant_type"] == ["authorization_code"] and form["code_verifier"]
        assert oidc.pkce_challenge(form["code_verifier"][0]) == q["code_challenge"][0]
        me = await c.get("/api/auth/me")
        assert me.status_code == 200, me.text
        u = me.json()["user"]
        assert (u["username"], u["source"], u["role"]) == ("carol@mnidom.net", "oidc", "viewer")
        claims = jwt.decode(c.cookies.get("netmon_session"), options={"verify_signature": False})
        assert claims["am"] == "oidc" and claims["src"] == "oidc"
        # LDAP-managed rules apply
        r = await c.post(
            "/api/auth/change-password",
            json={"current_password": "x" * 10, "new_password": "y" * 12},
        )
        assert r.status_code == 409 and r.json()["error"]["code"] == "ldap_managed"
        # feature OFF: OIDC sessions are refused
        feats(sso_oidc=False)
        assert (await c.get("/api/auth/me")).status_code == 401
        r = await c.get("/api/auth/oidc/callback", params={"code": "x", "state": "y"})
        assert r.status_code == 302 and "sso_error=feature_disabled" in r.headers["location"]
    finally:
        await c.aclose()
    feats(sso_oidc=True)
    # role change at the next login (admin) bumps the token version
    c, r, _ = await _flow(app, entra, {"roles": ["NetMon.Admin"]})
    await c.aclose()
    assert r.status_code == 200
    users = (await admin.get("/api/users")).json()["items"]
    carol = next(x for x in users if x["username"] == "carol@mnidom.net")
    assert carol["role"] == "admin" and carol["source"] == "oidc"
    logins = [r_ for r_ in await _audit(admin_rt(admin), "auth.login") if r_["source"] == "oidc"]
    assert logins and json.loads(logins[-1]["changes"])["method"] == [None, "oidc"]
    assert await _audit(admin_rt(admin), "user.oidc_role_change")


def admin_rt(c: httpx.AsyncClient) -> Any:
    return c._transport.app.state.rt  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("claims_kw", "code", "expect"),
    [
        ({"roles": []}, "good-code", "not_in_group"),
        ({"aud": "33333333-3333-3333-3333-333333333333"}, "good-code", "sso_failed"),
        ({}, "bad-code", "sso_failed"),
        ({"preferred_username": "admin"}, "good-code", "sso_conflict"),
    ],
)
async def test_oidc_failures(
    app: Any,
    admin: httpx.AsyncClient,
    entra: FakeEntra,
    feats: Any,
    claims_kw: dict[str, Any],
    code: str,
    expect: str,
) -> None:
    await _configure_oidc(admin)
    feats(sso_oidc=True)
    c, r, _ = await _flow(app, entra, claims_kw, code)
    await c.aclose()
    assert r.status_code == 302
    assert r.headers["location"] == f"/login?sso_error={expect}"


async def test_oidc_state_mismatch_and_entra_error(
    app: Any, admin: httpx.AsyncClient, entra: FakeEntra, feats: Any
) -> None:
    await _configure_oidc(admin)
    feats(sso_oidc=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        r = await c.get("/api/auth/oidc/start")
        assert r.status_code == 302
        r = await c.get("/api/auth/oidc/callback", params={"code": "good-code", "state": "x"})
        assert r.headers["location"] == "/login?sso_error=sso_failed"
        r = await c.get(
            "/api/auth/oidc/callback",
            params={"error": "access_denied", "error_description": "<script>"},
        )
        assert r.headers["location"] == "/login?sso_error=sso_failed"
        assert "script" not in r.headers["location"]
