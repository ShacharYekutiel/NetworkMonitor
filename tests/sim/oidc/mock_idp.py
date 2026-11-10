#!/usr/bin/env python3
"""Mock Microsoft Entra ID (OIDC) identity provider for the phase-3 SSO tests (architecture.md §13.5.2, §13.11).

    backend/.venv/bin/python tests/sim/oidc/mock_idp.py --port 18900
    # backend: NETMON_OIDC_AUTHORITY=http://127.0.0.1:18900 ; settings oidc.tenant_id = TENANT, client_id = CLIENT,
    #          client_secret = SECRET (defaults below)

ASGI (Starlette, served by uvicorn); RS256 keys generated at start. Entra-shaped endpoints under the authority:
  GET  /<tenant>/v2.0/.well-known/openid-configuration
  GET  /<tenant>/oauth2/v2.0/authorize   response_type=code, S256 PKCE required, response_mode=query; the user is
       chosen by `login_hint` (or the control default); without one a tiny HTML form lists the users
  POST /<tenant>/oauth2/v2.0/token       authorization_code + code_verifier + client_secret (form or basic auth)
  GET  /<tenant>/discovery/v2.0/keys     JWKS
The issuer is `<authority>/<tenant>/v2.0` (with --issuer-base https://login.microsoftonline.com the real Entra
issuer string is used instead while the endpoints stay local).

Users (login_hint = preferred_username; roles = app roles):
  dana.admin@mnidom.net   NetMon.Admin            vic.viewer@mnidom.net  NetMon.Viewer
  both@mnidom.net         NetMon.Admin + Viewer   nora.norole@mnidom.net (no roles -> not_in_group)
  grace.groups@mnidom.net groups [GROUP_ADMIN]    olga.overage@mnidom.net (_claim_names.groups -> overage)
  admin@mnidom.net        NetMon.Admin, preferred_username "admin" (clashes with the local admin -> sso_conflict)

Modes (POST /_control/mode {"mode": "...", "once": false}; also per request `&mock_mode=` on authorize):
  ok, wrong_iss, wrong_aud, wrong_tid, expired (exp = now-301 s), expired_leeway (exp = now-299 s: still valid),
  bad_nonce, no_nonce, unknown_kid (signed with a key never published), rotate (a new key, published at once: a
  backend that caches the JWKS must refetch on the unknown kid), bad_signature, error (authorize redirects back
  with error=access_denied&error_description=...), token_error (token endpoint 400 invalid_grant).
Control: GET /health, GET /_control/requests, DELETE /_control/requests, GET/POST /_control/mode,
POST /_control/default-user {"login_hint": "..."}, GET /_control/state.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import secrets
import time
import uuid
from urllib.parse import parse_qsl, urlencode

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

TENANT = "8f0e2a4c-1b3d-4e5f-9a7b-6c5d4e3f2a1b"
CLIENT_ID = "3a9d7c1e-5b2f-4d6a-8e0c-1f2a3b4c5d6e"
CLIENT_SECRET = "qa-oidc-client-secret-0123456789"
GROUP_ADMIN = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
GROUP_VIEWER = "1c2d3e4f-5061-7283-94a5-b6c7d8e9f0a1"

USERS = {
    "dana.admin@mnidom.net": {"name": "Dana Admin", "roles": ["NetMon.Admin"]},
    "vic.viewer@mnidom.net": {"name": "Vic Viewer", "roles": ["NetMon.Viewer"]},
    "both@mnidom.net": {"name": "Both Roles", "roles": ["NetMon.Admin", "NetMon.Viewer"]},
    "nora.norole@mnidom.net": {"name": "Nora NoRole", "roles": []},
    "grace.groups@mnidom.net": {"name": "Grace Groups", "roles": [], "groups": [GROUP_ADMIN]},
    "olga.overage@mnidom.net": {"name": "Olga Overage", "roles": [], "overage": True},
    "admin@mnidom.net": {"name": "Clashing Admin", "roles": ["NetMon.Admin"], "preferred_username": "admin"},
}
for _u, _v in USERS.items():
    _v["oid"] = str(uuid.uuid5(uuid.NAMESPACE_DNS, _u))


def b64u(n: int) -> str:
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Key:
    def __init__(self) -> None:
        self.priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = secrets.token_hex(8)

    def jwk(self) -> dict:
        n = self.priv.public_key().public_numbers()
        return {"kty": "RSA", "use": "sig", "kid": self.kid, "alg": "RS256", "n": b64u(n.n), "e": b64u(n.e)}


class State:
    def __init__(self, a: argparse.Namespace) -> None:
        self.base = f"http://{a.host}:{a.port}"
        self.issuer_base = (a.issuer_base or self.base).rstrip("/")
        self.tenant = a.tenant
        self.client_id = a.client_id
        self.client_secret = a.client_secret
        self.keys = [Key()]
        self.signing = self.keys[0]
        self.mode = "ok"
        self.once = False
        self.default_user: str | None = None
        self.codes: dict[str, dict] = {}
        self.requests: list[dict] = []

    def issuer(self, tenant: str | None = None) -> str:
        return f"{self.issuer_base}/{tenant or self.tenant}/v2.0"

    def take_mode(self, override: str | None) -> str:
        m = override or self.mode
        if self.once and not override:
            self.mode, self.once = "ok", False
        return m


S: State


def record(req: Request, extra: dict | None = None) -> None:
    S.requests.append({"time": time.time(), "method": req.method, "path": req.url.path,
                       "query": dict(req.query_params), **(extra or {})})


async def discovery(req: Request) -> Response:
    record(req)
    t = req.path_params["tenant"]
    if t != S.tenant:
        return JSONResponse({"error": "invalid_tenant", "error_description": f"AADSTS90002: Tenant '{t}' not found."},
                            400)
    b = f"{S.base}/{t}"
    return JSONResponse({
        "issuer": S.issuer(t), "authorization_endpoint": f"{b}/oauth2/v2.0/authorize",
        "token_endpoint": f"{b}/oauth2/v2.0/token", "jwks_uri": f"{b}/discovery/v2.0/keys",
        "response_types_supported": ["code", "id_token", "code id_token", "id_token token"],
        "response_modes_supported": ["query", "fragment", "form_post"], "subject_types_supported": ["pairwise"],
        "id_token_signing_alg_values_supported": ["RS256"], "scopes_supported": ["openid", "profile", "email"],
        "code_challenge_methods_supported": ["plain", "S256"], "tenant_region_scope": "EU",
        "claims_supported": ["sub", "iss", "aud", "exp", "iat", "nonce", "tid", "oid", "name", "preferred_username",
                             "email", "roles", "groups"]})


async def jwks(req: Request) -> Response:
    record(req)
    return JSONResponse({"keys": [k.jwk() for k in S.keys]})


def _redirect(uri: str, params: dict) -> RedirectResponse:
    sep = "&" if "?" in uri else "?"
    return RedirectResponse(uri + sep + urlencode(params), status_code=302)


async def authorize(req: Request) -> Response:
    q = dict(req.query_params)
    record(req)
    redirect_uri = q.get("redirect_uri", "")
    state = q.get("state", "")
    mode = S.take_mode(q.get("mock_mode"))
    if q.get("client_id") != S.client_id:
        return HTMLResponse(f"AADSTS700016: Application with identifier '{q.get('client_id')}' was not found.", 400)
    if not redirect_uri:
        return HTMLResponse("AADSTS900144: The request body must contain the following parameter: 'redirect_uri'.",
                            400)
    if mode == "error":
        return _redirect(redirect_uri, {"error": "access_denied", "error_description":
                                        "AADSTS65004: User declined to consent <script>alert(1)</script>",
                                        "state": state})
    problems = []
    if q.get("response_type") != "code":
        problems.append("response_type must be code")
    if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
        problems.append("PKCE S256 required (QA mock)")
    if "openid" not in q.get("scope", "").split():
        problems.append("scope must contain openid")
    if q.get("response_mode", "query") != "query":
        problems.append("response_mode must be query")
    if problems:
        return _redirect(redirect_uri, {"error": "invalid_request", "error_description": "; ".join(problems),
                                        "state": state})
    hint = (q.get("login_hint") or S.default_user or "").lower()
    if hint not in USERS:
        if "user" in q and q["user"] in USERS:
            hint = q["user"]
        else:
            rows = "".join(f'<li><a href="?{urlencode({**q, "user": u})}">{u}</a></li>' for u in USERS)
            return HTMLResponse(f"<!doctype html><title>Mock Entra sign-in</title><h1>Pick an account</h1><ul>{rows}</ul>")
    code = secrets.token_urlsafe(24)
    S.codes[code] = {"user": hint, "nonce": q.get("nonce"), "challenge": q.get("code_challenge"),
                     "redirect_uri": redirect_uri, "mode": mode, "time": time.time()}
    return _redirect(redirect_uri, {"code": code, "state": state, "session_state": str(uuid.uuid4())})


def make_id_token(user: str, nonce: str | None, mode: str) -> str:
    u = USERS[user]
    now = int(time.time())
    claims: dict = {
        "aud": S.client_id, "iss": S.issuer(), "iat": now, "nbf": now, "exp": now + 3600,
        "tid": S.tenant, "oid": u["oid"], "sub": hashlib.sha256(u["oid"].encode()).hexdigest()[:43],
        "preferred_username": u.get("preferred_username", user), "name": u["name"], "email": user,
        "ver": "2.0", "uti": secrets.token_urlsafe(16), "rh": "0.QA",
    }
    if nonce is not None:
        claims["nonce"] = nonce
    if u.get("roles"):
        claims["roles"] = u["roles"]
    if u.get("groups"):
        claims["groups"] = u["groups"]
    if u.get("overage"):
        claims["_claim_names"] = {"groups": "src1"}
        claims["_claim_sources"] = {"src1": {"endpoint": "https://graph.microsoft.com/v1.0/users/x/getMemberObjects"}}
    key = S.signing
    if mode == "wrong_iss":
        claims["iss"] = S.issuer("00000000-dead-beef-0000-000000000000")
    elif mode == "wrong_aud":
        claims["aud"] = "99999999-9999-9999-9999-999999999999"
    elif mode == "wrong_tid":
        claims["tid"] = "00000000-dead-beef-0000-000000000000"
    elif mode == "expired":
        claims["exp"] = now - 301
        claims["iat"] = claims["nbf"] = now - 3901
    elif mode == "expired_leeway":
        claims["exp"] = now - 299
        claims["iat"] = claims["nbf"] = now - 3899
    elif mode == "bad_nonce":
        claims["nonce"] = "not-the-nonce-you-sent"
    elif mode == "no_nonce":
        claims.pop("nonce", None)
    elif mode == "unknown_kid":
        key = Key()  # never published
    elif mode == "rotate":
        k = Key()
        S.keys.append(k)
        S.signing = key = k
    tok = jwt.encode(claims, key.priv, algorithm="RS256", headers={"kid": key.kid, "typ": "JWT"})
    if mode == "bad_signature":
        h, p, s = tok.split(".")
        s = ("A" if s[0] != "A" else "B") + s[1:]
        tok = f"{h}.{p}.{s}"
    return tok


async def token(req: Request) -> Response:
    form = dict(parse_qsl((await req.body()).decode()))  # no python-multipart dependency
    auth = req.headers.get("authorization", "")
    cid, csec = form.get("client_id"), form.get("client_secret")
    if auth.lower().startswith("basic "):
        cid, _, csec = base64.b64decode(auth[6:]).decode().partition(":")
    safe = {k: (v if k not in ("client_secret", "code_verifier", "code") else "***") for k, v in form.items()}
    record(req, {"form": safe, "basic_auth": auth.lower().startswith("basic ")})
    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, 400)
    if cid != S.client_id or csec != S.client_secret:
        return JSONResponse({"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret"},
                            401)
    c = S.codes.pop(form.get("code", ""), None)
    if c is None or time.time() - c["time"] > 600:
        return JSONResponse({"error": "invalid_grant", "error_description": "AADSTS70008: code expired or used"}, 400)
    if c["mode"] == "token_error":
        return JSONResponse({"error": "invalid_grant", "error_description": "AADSTS54005: mock token_error"}, 400)
    if form.get("redirect_uri") != c["redirect_uri"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "AADSTS50148: redirect_uri mismatch"}, 400)
    verifier = form.get("code_verifier", "")
    chal = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    if chal != c["challenge"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "AADSTS501481: code_verifier mismatch"},
                            400)
    return JSONResponse({"token_type": "Bearer", "scope": "openid profile email", "expires_in": 3600,
                         "access_token": secrets.token_urlsafe(32),
                         "id_token": make_id_token(c["user"], c["nonce"], c["mode"])})


async def health(req: Request) -> Response:
    return JSONResponse({"ok": True})


async def ctl_requests(req: Request) -> Response:
    if req.method == "DELETE":
        S.requests.clear()
        return JSONResponse({"ok": True})
    return JSONResponse(S.requests)


async def ctl_mode(req: Request) -> Response:
    if req.method == "POST":
        b = await req.json()
        S.mode = b.get("mode", "ok")
        S.once = bool(b.get("once", False))
        if S.mode == "ok":
            S.keys = S.keys[:1]
            S.signing = S.keys[0]
    return JSONResponse({"mode": S.mode, "once": S.once})


async def ctl_default_user(req: Request) -> Response:
    b = await req.json()
    S.default_user = b.get("login_hint")
    return JSONResponse({"default_user": S.default_user})


async def ctl_state(req: Request) -> Response:
    return JSONResponse({"tenant": S.tenant, "client_id": S.client_id, "client_secret": S.client_secret,
                         "issuer": S.issuer(), "authority": S.base, "mode": S.mode, "keys": [k.kid for k in S.keys],
                         "users": {u: {"oid": v["oid"], "roles": v.get("roles", []), "groups": v.get("groups", [])}
                                   for u, v in USERS.items()},
                         "group_admin": GROUP_ADMIN, "group_viewer": GROUP_VIEWER})


def app() -> Starlette:
    return Starlette(routes=[
        Route("/health", health),
        Route("/_control/requests", ctl_requests, methods=["GET", "DELETE"]),
        Route("/_control/mode", ctl_mode, methods=["GET", "POST"]),
        Route("/_control/default-user", ctl_default_user, methods=["POST"]),
        Route("/_control/state", ctl_state),
        Route("/{tenant}/v2.0/.well-known/openid-configuration", discovery),
        Route("/{tenant}/oauth2/v2.0/authorize", authorize),
        Route("/{tenant}/oauth2/v2.0/token", token, methods=["POST"]),
        Route("/{tenant}/discovery/v2.0/keys", jwks),
    ])


def main() -> None:
    global S
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18900)
    ap.add_argument("--tenant", default=TENANT)
    ap.add_argument("--client-id", default=CLIENT_ID)
    ap.add_argument("--client-secret", default=CLIENT_SECRET)
    ap.add_argument("--issuer-base", default="")
    a = ap.parse_args()
    S = State(a)
    print(f"READY oidc mock on {S.base} tenant={S.tenant} client_id={S.client_id} issuer={S.issuer()}", flush=True)
    uvicorn.run(app(), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
