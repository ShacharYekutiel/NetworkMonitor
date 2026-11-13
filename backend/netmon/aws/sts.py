"""[4] Minimal AWS STS client (Query protocol, SigV4 service `sts`; architecture § 15.1.3).

Only `GetCallerIdentity` and `AssumeRole`. The request carries no `Tags`, `TransitiveTagKeys`,
`SourceIdentity`, `Policy` or `PolicyArns` (the source identity set by Roles Anywhere is
inherited; a different value would be refused). Always the regional endpoint of the
connection (or its `(sts, region)` override), never the global one."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from netmon.aws import awsxml, sigv4
from netmon.aws import transport as tp
from netmon.aws.credentials import Credentials, parse_time
from netmon.aws.errors import AssistantError, make, map_exception, map_sts

ROLE_SESSION_SECONDS = 3600  # role chaining: at most 1 hour (§ 15.1.2 fact 6)
SESSION_NAME_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+=,.@_-")
ENV_NODE_NAME = "NETMON_NODE_NAME"


def session_name(consumer: str, node: str | None = None) -> str:
    """`netmon-<consumer>` + `-<NETMON_NODE_NAME>` when set; other characters → `-`; ≤ 64."""
    node = os.environ.get(ENV_NODE_NAME) if node is None else node
    raw = f"netmon-{consumer}" + (f"-{node}" if node else "")
    return "".join(ch if ch in SESSION_NAME_CHARS else "-" for ch in raw)[:64]


def account_of(arn: str | None) -> str | None:
    """The account id field of an ARN (`arn:aws:<svc>:<region>:<account>:…`)."""
    if not arn:
        return None
    parts = arn.split(":")
    if len(parts) > 4 and parts[4].isdigit() and len(parts[4]) == 12:
        return parts[4]
    return None


def role_arn_of_session(arn: str | None) -> str | None:
    """`arn:aws:sts::A:assumed-role/NAME/SESSION` → `arn:aws:iam::A:role/NAME` (a trust policy
    names the role, not the session). Other ARNs are returned unchanged."""
    if not arn or ":assumed-role/" not in arn:
        return arn
    head, _, rest = arn.partition(":assumed-role/")
    acct = head.rsplit(":", 1)[-1]
    name = rest.split("/", 1)[0]
    return f"arn:aws:iam::{acct}:role/{name}"


class StsClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        region: str,
        endpoint: str,
        *,
        via: str = "direct",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.http = http
        self.region = region
        self.endpoint = endpoint.rstrip("/")
        self.via = via
        self.now = now or (lambda: datetime.now(UTC))

    async def _call(
        self, creds: Credentials, action: str, params: dict[str, Any], **map_kw: Any
    ) -> bytes:
        url = f"{self.endpoint}/"
        body = sigv4.query_body(action, params)
        signed = sigv4.sign_v4(
            "POST",
            url,
            {"Content-Type": sigv4.FORM_CONTENT_TYPE},
            body,
            access_key=creds.access_key_id,
            secret_key=creds.secret_access_key,
            session_token=creds.session_token,
            region=self.region,
            service="sts",
            amzdate=sigv4.amz_date(self.now()),
        )
        try:
            resp = await self.http.post(url, content=body, headers=signed.headers)
        except httpx.HTTPError as exc:
            err = map_exception(exc, host=tp.host_of(url), via=self.via)
            if err.code == "assistant_timeout":
                err = make("aws_network", host=tp.host_of(url), aws="connection timed out")
                err.details = [{"host": tp.host_of(url), "via": self.via}]
            raise err from None
        if resp.status_code != 200:
            raise map_sts(resp.status_code, resp.content, op=action, **map_kw)
        return resp.content

    async def get_caller_identity(self, creds: Credentials) -> dict[str, str | None]:
        raw = await self._call(creds, "GetCallerIdentity", {})
        try:
            return awsxml.parse_caller_identity(raw)
        except awsxml.XmlError as exc:
            raise make(
                "aws_network", host=tp.host_of(self.endpoint), aws=f"invalid response ({exc})"
            ) from None

    async def assume_role(
        self,
        creds: Credentials,
        role_arn: str,
        session_name: str,
        external_id: str | None = None,
        duration: int = ROLE_SESSION_SECONDS,
        *,
        from_arn: str | None = None,
    ) -> Credentials:
        params: dict[str, Any] = {
            "RoleArn": role_arn,
            "RoleSessionName": session_name,
            "DurationSeconds": duration,
            "ExternalId": external_id or None,
        }
        raw = await self._call(creds, "AssumeRole", params, from_arn=from_arn, role_arn=role_arn)
        try:
            d = awsxml.parse_assume_role(raw)
        except awsxml.XmlError as exc:
            raise make(
                "aws_network", host=tp.host_of(self.endpoint), aws=f"invalid response ({exc})"
            ) from None
        return Credentials(
            str(d["access_key_id"]),
            str(d["secret_access_key"]),
            d.get("session_token"),
            parse_time(d.get("expiration")),
            "assume_role",
            {
                "role_arn": role_arn,
                "assumed_role_arn": d.get("assumed_role_arn"),
                "source_identity": d.get("source_identity"),
            },
        )


__all__ = ["AssistantError", "StsClient", "account_of", "role_arn_of_session", "session_name"]
