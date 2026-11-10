"""[3b] AWS credential providers (architecture § 14.2.3).

- `RolesAnywhereProvider`: X.509-signed `POST /sessions` → temporary credentials;
- `StaticProvider`: access key id + secret (write-only setting);
- `DefaultChainProvider`: environment → container credentials → EC2 IMDSv2 (never via a proxy);
- `CachingProvider`: in-memory cache, refreshed 5 min before expiration, single-flight refresh,
  `get(force=True)` after an `aws_auth_failed` answer.

Credentials are never logged."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from netmon.assistant import sigv4
from netmon.assistant import transport as tp
from netmon.assistant.errors import AssistantError, make, map_exception, map_rolesanywhere

REFRESH_BEFORE = timedelta(minutes=5)
IMDS_DEFAULT = "http://169.254.169.254"
CONTAINER_HOST = "http://169.254.170.2"


@dataclass
class Credentials:
    access_key_id: str
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)
    expiration: datetime | None = None
    source: str = "static"
    detail: dict[str, Any] = field(default_factory=dict)

    def needs_refresh(self, now: datetime) -> bool:
        return self.expiration is not None and now >= self.expiration - REFRESH_BEFORE


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


Now = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class StaticProvider:
    source = "access_key"

    def __init__(self, access_key_id: str, secret_access_key: str) -> None:
        self._creds = Credentials(access_key_id, secret_access_key, source="access_key")

    async def fetch(self) -> Credentials:
        return self._creds

    async def aclose(self) -> None:
        return None


@dataclass
class RaMaterial:
    cert_der: bytes
    serial: int
    chain_der: list[bytes]
    key: Any  # private key object


class RolesAnywhereProvider:
    source = "roles_anywhere"

    def __init__(
        self,
        cfg: dict[str, Any],
        material: Callable[[], RaMaterial],
        client: httpx.AsyncClient,
        now: Now = _utcnow,
    ) -> None:
        from netmon.assistant import config

        self.cfg = cfg
        self.material = material
        self.client = client
        self.now = now
        self.region = config.ra_region(cfg)
        self.endpoint = config.ra_endpoint(cfg)

    def body(self) -> bytes:
        payload = {
            "durationSeconds": int(self.cfg.get("ra_session_seconds") or 3600),
            "profileArn": self.cfg["ra_profile_arn"],
            "roleArn": self.cfg["ra_role_arn"],
            "trustAnchorArn": self.cfg["ra_trust_anchor_arn"],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    async def fetch(self) -> Credentials:
        try:
            mat = self.material()
        except AssistantError:
            raise
        except Exception as exc:  # noqa: BLE001 - bad host files, undecryptable key
            raise make("rolesanywhere_failed", aws=f"certificate/key unusable: {exc}") from None
        url = f"{self.endpoint}/sessions"
        body = self.body()
        signed = sigv4.sign_x509(
            "POST",
            url,
            {"Content-Type": "application/json"},
            body,
            cert_der=mat.cert_der,
            serial=mat.serial,
            chain_der=mat.chain_der,
            signer=sigv4.X509Signer(mat.key),
            region=self.region,
            amzdate=sigv4.amz_date(self.now()),
        )
        try:
            resp = await self.client.post(url, content=body, headers=signed.headers)
        except httpx.HTTPError as exc:
            raise map_exception(exc, host=tp.host_of(url), via=tp.via(url, self.cfg)) from None
        if resp.status_code != 201 and resp.status_code != 200:
            raise map_rolesanywhere(resp.status_code, dict(resp.headers), resp.content)
        try:
            data = resp.json()
            cs = data["credentialSet"][0]
            c = cs["credentials"]
            return Credentials(
                c["accessKeyId"],
                c["secretAccessKey"],
                c.get("sessionToken"),
                parse_time(c.get("expiration")),
                "roles_anywhere",
                {"subject_arn": data.get("subjectArn"), "role_arn": (cs.get("assumedRoleUser")
                 or {}).get("arn")},
            )  # fmt: skip
        except (KeyError, IndexError, TypeError, ValueError):
            raise make("rolesanywhere_failed", aws="unexpected CreateSession response") from None

    async def aclose(self) -> None:
        return None


class DefaultChainProvider:
    """Environment → container credentials → EC2 IMDSv2. `tried` lists the sources looked at
    (reported by Test)."""

    source = "default_chain"

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = env if env is not None else os.environ
        self.tried: list[str] = []

    async def fetch(self) -> Credentials:
        self.tried = []
        env = self.env
        self.tried.append("environment")
        if env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"):
            return Credentials(
                env["AWS_ACCESS_KEY_ID"],
                env["AWS_SECRET_ACCESS_KEY"],
                env.get("AWS_SESSION_TOKEN") or None,
                None,
                "environment",
            )
        async with tp.metadata_client() as client:
            full = env.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
            rel = env.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
            if full or rel:
                self.tried.append("container")
                return await self._container(client, full or f"{CONTAINER_HOST}{rel}")
            if str(env.get("AWS_EC2_METADATA_DISABLED", "")).lower() != "true":
                self.tried.append("imds")
                creds = await self._imds(client)
                if creds is not None:
                    return creds
        raise AssistantError(
            "aws_auth_failed",
            "No AWS credentials found (tried: " + ", ".join(self.tried) + ").",
            [{"tried": list(self.tried)}],
        )

    async def _container(self, client: httpx.AsyncClient, url: str) -> Credentials:
        headers = {}
        token = self.env.get("AWS_CONTAINER_AUTHORIZATION_TOKEN")
        token_file = self.env.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE")
        if token_file:
            try:
                token = (await asyncio.to_thread(_read_text, token_file)).strip()
            except OSError:
                token = None
        if token:
            headers["Authorization"] = token
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            d = resp.json()
            return Credentials(
                d["AccessKeyId"], d["SecretAccessKey"], d.get("Token"),
                parse_time(d.get("Expiration")), "container",
            )  # fmt: skip
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise AssistantError(
                "aws_auth_failed",
                f"Container credentials unavailable: {type(exc).__name__}",
                [{"tried": list(self.tried)}],
            ) from None

    async def _imds(self, client: httpx.AsyncClient) -> Credentials | None:
        base = (self.env.get("AWS_EC2_METADATA_SERVICE_ENDPOINT") or IMDS_DEFAULT).rstrip("/")
        try:
            tok = await client.put(
                f"{base}/latest/api/token",
                headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
            )
            if tok.status_code != 200:
                return None
            h = {"X-aws-ec2-metadata-token": tok.text.strip()}
            path = f"{base}/latest/meta-data/iam/security-credentials/"
            r = await client.get(path, headers=h)
            if r.status_code != 200 or not r.text.strip():
                return None
            role = r.text.strip().splitlines()[0].strip()
            r = await client.get(path + role, headers=h)
            if r.status_code != 200:
                return None
            d = r.json()
            if d.get("Code", "Success") != "Success":
                return None
            return Credentials(
                d["AccessKeyId"], d["SecretAccessKey"], d.get("Token"),
                parse_time(d.get("Expiration")), "imds", {"role": role},
            )  # fmt: skip
        except (httpx.HTTPError, KeyError, ValueError):
            return None

    async def aclose(self) -> None:
        return None


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class CachingProvider:
    """Cache + single-flight refresh around a provider."""

    def __init__(
        self,
        provider: Any,
        now: Now = _utcnow,
        on_refresh: Callable[[Credentials], Awaitable[None] | None] | None = None,
    ) -> None:
        self.provider = provider
        self.now = now
        self.on_refresh = on_refresh
        self._creds: Credentials | None = None
        self._lock = asyncio.Lock()
        self.refreshes = 0

    @property
    def cached(self) -> Credentials | None:
        return self._creds

    async def get(self, force: bool = False) -> Credentials:
        creds = self._creds
        if creds is not None and not force and not creds.needs_refresh(self.now()):
            return creds
        generation = self.refreshes
        async with self._lock:
            creds = self._creds
            if (
                self.refreshes != generation
                and creds is not None
                and not creds.needs_refresh(self.now())
            ):
                return creds  # another task refreshed while this one waited (single flight)
            new = await self.provider.fetch()
            self._creds = new
            self.refreshes += 1
            if self.on_refresh is not None:
                res = self.on_refresh(new)
                if asyncio.iscoroutine(res):
                    await res
            return new

    def clear(self) -> None:
        self._creds = None

    async def aclose(self) -> None:
        self._creds = None
        close = getattr(self.provider, "aclose", None)
        if close is not None:
            await close()
