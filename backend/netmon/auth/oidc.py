"""[3] OpenID Connect with Microsoft Entra ID (architecture § 13.5.2).

Authorization code flow + PKCE (S256), confidential client. Outbound HTTPS (discovery, JWKS,
token) goes through the corporate proxy (`trust_env=True`, corporate CA via `tlsutil`).

Pure parts (unit-tested with the data-model vectors): `validate_claims`, `map_role`,
`safe_next`, `pkce_challenge`, `oidc_config_errors`. The `netmon_oidc` cookie carries state,
nonce, PKCE verifier and `next`, AES-GCM encrypted (AAD `oidc.cookie`), 10 minutes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

from netmon import __version__
from netmon.services import crypto
from netmon.tlsutil import httpx_verify

DEFAULT_AUTHORITY = "https://login.microsoftonline.com"
AAD_CLIENT_SECRET = b"oidc.client_secret"
AAD_COOKIE = b"oidc.cookie"
COOKIE_NAME = "netmon_oidc"
COOKIE_PATH = "/api/auth/oidc"
COOKIE_TTL_S = 600
LEEWAY_S = 300
JWKS_TTL_S = 24 * 3600
JWKS_REFETCH_MIN_S = 300
# An unknown kid that has not itself caused a refetch in the last 5 min may refetch once the
# cached JWKS is older than this (a bogus kid shortly before a key rotation must not block the
# new key for 5 min).
JWKS_UNKNOWN_KID_MIN_S = 60
HTTP_TIMEOUT_S = 15.0
SCOPES = "openid profile email"
DEFAULT_BUTTON = "Sign in with Microsoft"
GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Entra `error` values → our codes (never echoed raw)
ERROR_MAP = {
    "access_denied": "sso_failed",
    "consent_required": "sso_failed",
    "interaction_required": "sso_failed",
    "login_required": "sso_failed",
    "invalid_request": "oidc_error",
    "unauthorized_client": "oidc_error",
    "invalid_client": "oidc_error",
    "server_error": "oidc_error",
    "temporarily_unavailable": "oidc_error",
}


def authority() -> str:
    return (os.environ.get("NETMON_OIDC_AUTHORITY") or DEFAULT_AUTHORITY).rstrip("/")


def issuer_for(tenant_id: str, auth: str | None = None) -> str:
    return f"{(auth or authority())}/{tenant_id}/v2.0"


def is_guid(v: Any) -> bool:
    return isinstance(v, str) and bool(GUID_RE.match(v))


# ---------------------------------------------------------------- pure
def pkce_verifier() -> str:
    return secrets.token_urlsafe(48)[:64]


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def safe_next(value: str | None) -> str:
    """Open-redirect guard: a same-origin relative path, else `/`."""
    if not value or not isinstance(value, str) or len(value) > 2000:
        return "/"
    if not value.startswith("/") or value.startswith("//") or value.startswith("/\\"):
        return "/"
    if any(ord(c) < 32 or c == "\\" for c in value):
        return "/"
    return value


def map_role(claims: dict[str, Any], cfg: dict[str, Any]) -> tuple[str | None, str | None]:
    """(role, error): error ∈ `not_in_group` | `oidc_group_overage`."""
    source = cfg.get("role_source") or "app_roles"
    admin_vals = {str(v) for v in cfg.get("admin_values") or []}
    viewer_vals = {str(v) for v in cfg.get("viewer_values") or []}
    if source == "groups":
        names = claims.get("_claim_names")
        if isinstance(names, dict) and "groups" in names:
            return None, "oidc_group_overage"
        values = claims.get("groups")
        admin_vals = {v.lower() for v in admin_vals}
        viewer_vals = {v.lower() for v in viewer_vals}
        have = {str(v).lower() for v in values} if isinstance(values, list) else set()
    else:
        values = claims.get("roles")
        have = {str(v) for v in values} if isinstance(values, list) else set()
    if have & admin_vals:
        return "admin", None
    if have & viewer_vals:
        return "viewer", None
    return None, "not_in_group"


@dataclass
class ClaimsResult:
    ok: bool
    error: str | None = None  # sso_failed | not_in_group | oidc_group_overage
    detail: str = ""
    role: str | None = None


def validate_claims(
    claims: dict[str, Any],
    cfg: dict[str, Any],
    nonce: str | None,
    now: float,
    auth: str | None = None,
) -> ClaimsResult:
    """id_token claim checks after the signature verification (data-model vectors)."""
    tenant = str(cfg.get("tenant_id") or "").lower()
    client = str(cfg.get("client_id") or "").lower()

    def fail(detail: str) -> ClaimsResult:
        return ClaimsResult(False, "sso_failed", detail)

    if claims.get("iss") != issuer_for(tenant, auth):
        return fail("issuer")
    aud = claims.get("aud")
    auds = [aud] if isinstance(aud, str) else list(aud) if isinstance(aud, list) else []
    if client not in {str(a).lower() for a in auds}:
        return fail("audience")
    if str(claims.get("tid") or "").lower() != tenant:
        return fail("tenant")
    try:
        exp = float(claims["exp"])
    except (KeyError, TypeError, ValueError):
        return fail("exp missing")
    if now > exp + LEEWAY_S:
        return fail("expired")
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and now + LEEWAY_S < float(nbf):
        return fail("not yet valid")
    iat = claims.get("iat")
    if isinstance(iat, (int, float)) and now + LEEWAY_S < float(iat):
        return fail("issued in the future")
    if not nonce or not isinstance(claims.get("nonce"), str):
        return fail("nonce missing")
    if not secrets.compare_digest(str(claims["nonce"]), nonce):
        return fail("nonce mismatch")
    role, err = map_role(claims, cfg)
    if err:
        return ClaimsResult(False, err, err)
    return ClaimsResult(True, None, "", role)


def oidc_config_errors(stored: dict[str, Any], secret_key: str) -> list[dict[str, Any]]:
    """Turning `sso_oidc` ON (422 details)."""
    errors: list[dict[str, Any]] = []
    if not is_guid(stored.get("tenant_id")):
        errors.append({"loc": ["body", "oidc", "tenant_id"], "msg": "a tenant id (GUID)"})
    if not is_guid(stored.get("client_id")):
        errors.append({"loc": ["body", "oidc", "client_id"], "msg": "a client id (GUID)"})
    if decrypt_secret(stored, secret_key) is None:
        errors.append({"loc": ["body", "oidc", "client_secret"], "msg": "a client secret"})
    if not (stored.get("admin_values") or stored.get("viewer_values")):
        errors.append({"loc": ["body", "oidc", "admin_values"], "msg": "at least one role value"})
    return errors


def decrypt_secret(stored: dict[str, Any], secret_key: str) -> str | None:
    enc = stored.get("client_secret_enc")
    if not enc:
        return None
    try:
        return crypto.decrypt(secret_key, enc, AAD_CLIENT_SECRET)
    except crypto.DecryptError:
        return None


def encrypt_secret(value: str, secret_key: str) -> str:
    return crypto.encrypt(secret_key, value, AAD_CLIENT_SECRET)


def seal_cookie(secret_key: str, data: dict[str, Any]) -> str:
    return crypto.encrypt(secret_key, json.dumps(data, separators=(",", ":")), AAD_COOKIE)


def open_cookie(secret_key: str, value: str | None, now: float) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        data = json.loads(crypto.decrypt(secret_key, value, AAD_COOKIE))
    except (crypto.DecryptError, ValueError):
        return None
    if not isinstance(data, dict) or float(data.get("exp") or 0) < now:
        return None
    return data


# ---------------------------------------------------------------- network
class OidcError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass
class _Cache:
    discovery: dict[str, Any] | None = None
    discovery_at: float = 0.0
    jwks: dict[str, Any] = field(default_factory=dict)  # kid -> jwk
    jwks_at: float = 0.0
    tenant: str = ""
    kid_refetch_at: dict[str, float] = field(default_factory=dict)  # unknown kid -> refetch time


class OidcClient:
    """Process-wide discovery/JWKS cache (rt.oidc)."""

    def __init__(self) -> None:
        self._cache = _Cache()
        self._lock = asyncio.Lock()
        self.transport: httpx.AsyncBaseTransport | None = None  # tests

    def reset(self) -> None:
        self._cache = _Cache()

    def _client(self) -> httpx.AsyncClient:
        kw: dict[str, Any] = {
            "timeout": HTTP_TIMEOUT_S,
            "headers": {"User-Agent": f"NetworkMonitor/{__version__}"},
        }
        if self.transport is not None:
            kw["transport"] = self.transport
        else:
            kw["verify"] = httpx_verify(True)
            kw["trust_env"] = True
        return httpx.AsyncClient(**kw)

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            async with self._client() as c:
                r = await c.get(url)
        except httpx.HTTPError as exc:
            raise OidcError("oidc_error", f"{type(exc).__name__}: {exc}"[:300]) from None
        if r.status_code != 200:
            raise OidcError("oidc_error", f"HTTP {r.status_code} from {httpx.URL(url).host}")
        try:
            data = r.json()
        except ValueError:
            raise OidcError("oidc_error", "invalid JSON") from None
        if not isinstance(data, dict):
            raise OidcError("oidc_error", "invalid JSON")
        return data

    async def discovery(self, tenant: str, force: bool = False) -> dict[str, Any]:
        c = self._cache
        if c.tenant != tenant:
            self._cache = c = _Cache(tenant=tenant)
        if not force and c.discovery and time.monotonic() - c.discovery_at < JWKS_TTL_S:
            return c.discovery
        data = await self._get_json(f"{authority()}/{tenant}/v2.0/.well-known/openid-configuration")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(data.get(key), str):
                raise OidcError("oidc_error", f"discovery document lacks {key}")
        c.discovery, c.discovery_at = data, time.monotonic()
        return data

    async def jwks(
        self, tenant: str, force: bool = False, kid: str | None = None
    ) -> dict[str, Any]:
        disc = await self.discovery(tenant)
        c = self._cache
        now = time.monotonic()
        age = now - c.jwks_at
        fresh = c.jwks and age < JWKS_TTL_S
        if fresh and not force:
            return c.jwks
        if force and c.jwks and age < JWKS_REFETCH_MIN_S:
            # unknown kid: at most one refetch per 5 min, except that a kid which has not
            # itself caused a refetch within 5 min may refetch once the JWKS is > 60 s old
            c.kid_refetch_at = {
                k: t for k, t in c.kid_refetch_at.items() if now - t < JWKS_REFETCH_MIN_S
            }
            if kid is None or kid in c.kid_refetch_at or age < JWKS_UNKNOWN_KID_MIN_S:
                return c.jwks
        if force and kid is not None:
            c.kid_refetch_at[kid] = now
        data = await self._get_json(disc["jwks_uri"])
        keys = {
            str(k.get("kid")): k
            for k in data.get("keys") or []
            if isinstance(k, dict) and k.get("kid")
        }
        c.jwks, c.jwks_at = keys, time.monotonic()
        return keys

    async def key_for(self, tenant: str, kid: str) -> Any:
        async with self._lock:
            keys = await self.jwks(tenant)
            if kid not in keys:
                keys = await self.jwks(tenant, force=True, kid=kid)
        jwk = keys.get(kid)
        if jwk is None:
            raise OidcError("sso_failed", "unknown signing key")
        try:
            return jwt.PyJWK(jwk, algorithm="RS256").key
        except (jwt.PyJWKError, jwt.InvalidKeyError, ValueError) as exc:
            raise OidcError("sso_failed", f"bad signing key: {exc}") from None

    async def exchange_code(
        self,
        tenant: str,
        client_id: str,
        client_secret: str,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        disc = await self.discovery(tenant)
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": verifier,
            "scope": SCOPES,
        }
        try:
            async with self._client() as c:
                r = await c.post(disc["token_endpoint"], data=form)
        except httpx.HTTPError as exc:
            raise OidcError("oidc_error", f"{type(exc).__name__}"[:300]) from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200 or not isinstance(data, dict) or "id_token" not in data:
            err = data.get("error") if isinstance(data, dict) else None
            raise OidcError("sso_failed", f"token endpoint: HTTP {r.status_code} {err or ''}")
        return data

    async def verify_id_token(self, tenant: str, id_token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError:
            raise OidcError("sso_failed", "malformed id_token") from None
        if header.get("alg") != "RS256":
            raise OidcError("sso_failed", "unexpected algorithm")
        key = await self.key_for(tenant, str(header.get("kid") or ""))
        try:
            return jwt.decode(
                id_token,
                key,
                algorithms=["RS256"],
                options={
                    "verify_aud": False,
                    "verify_iss": False,
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                },
            )
        except jwt.PyJWTError as exc:
            raise OidcError("sso_failed", f"signature: {type(exc).__name__}") from None
