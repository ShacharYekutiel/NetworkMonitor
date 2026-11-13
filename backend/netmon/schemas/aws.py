"""[4] Request models of the shared AWS connections (api.md § [4] "Shared AWS connections").

Field syntax (ARNs, regions, URLs) is checked by `netmon.aws.connections.field_errors`, so every
error carries its `loc` and the specific message (e.g. "the ARN appears twice — paste it
once"); the models only bound sizes and types."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_TOKEN = re.compile(r"^[A-Za-z0-9+/=_-]+$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AwsAccessIn(_Strict):
    """Consumer access (data-model "Consumer access")."""

    mode: Literal["direct", "assume_role", "ra_role"] = "direct"
    role_arn: str | None = Field(default=None, max_length=600)
    external_id: str | None = Field(default=None, max_length=1224)

    @field_validator("role_arn", "external_id")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return v.strip() or None


class EndpointOverrideIn(_Strict):
    service: str = Field(min_length=1, max_length=32)
    region: str = Field(min_length=1, max_length=32)
    url: str = Field(min_length=1, max_length=512)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return v.strip().rstrip("/")


class AwsConnectionFields(_Strict):
    name: str | None = Field(default=None, max_length=64)
    auth_mode: Literal["roles_anywhere", "access_key", "default_chain"] | None = None
    region: str | None = Field(default=None, max_length=32)
    proxy_mode: Literal["env", "direct", "custom"] | None = None
    proxy_url: str | None = Field(default=None, max_length=512)
    endpoint_overrides: list[EndpointOverrideIn] | None = Field(default=None, max_length=40)
    access_key_id: str | None = Field(default=None, max_length=32)
    access_key_created_at: date | None = None
    ra_region: str | None = Field(default=None, max_length=32)
    ra_trust_anchor_arn: str | None = Field(default=None, max_length=1024)
    ra_profile_arn: str | None = Field(default=None, max_length=1024)
    ra_role_arn: str | None = Field(default=None, max_length=1024)
    ra_session_seconds: int | None = Field(default=None, ge=900, le=43200)
    secret_access_key: str | None = Field(default=None, max_length=256)  # write-only
    session_token: str | None = Field(default=None, max_length=4096)  # write-only

    @field_validator(
        "name", "region", "access_key_id", "ra_region", "ra_trust_anchor_arn", "ra_profile_arn",
        "ra_role_arn",
    )  # fmt: skip
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return v.strip()

    @field_validator("proxy_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return v.strip().rstrip("/") or None

    @field_validator("session_token")
    @classmethod
    def _token(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = "".join(v.split())
        if v and not _TOKEN.match(v):
            raise ValueError("invalid session token (base64 characters expected)")
        return v

    def changes(self) -> dict:
        out: dict = {}
        for k in self.model_fields_set:
            v = getattr(self, k)
            if k == "endpoint_overrides":
                out[k] = [o.model_dump() for o in v] if v else []
            elif k == "access_key_created_at":
                out[k] = v.isoformat() if v is not None else None
            elif k in ("name", "auth_mode", "region", "proxy_mode", "ra_session_seconds") and (
                v is None
            ):
                continue  # required values cannot be cleared
            elif k in ("ra_region", "ra_trust_anchor_arn", "ra_profile_arn", "ra_role_arn",
                       "access_key_id"):  # fmt: skip
                out[k] = v or None
            else:
                out[k] = v
        return out


class AwsConnectionPatch(AwsConnectionFields):
    test_id: str | None = Field(default=None, max_length=64)


class AwsTestIn(_Strict):
    connection_id: int | None = Field(default=None, ge=1)
    connection: AwsConnectionFields | None = None
    access: AwsAccessIn | None = None


class CertCopyIn(_Strict):
    from_connection_id: int = Field(ge=1)


class DefaultIn(_Strict):
    """`POST /api/aws/connections/{id}/default`: the `test_id` of a successful Test of that
    connection, needed while an ON consumer uses the default implicitly."""

    test_id: str | None = Field(default=None, max_length=64)
