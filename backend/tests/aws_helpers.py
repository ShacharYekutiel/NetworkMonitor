"""[4] Test helpers for the shared AWS connections (PR 4-0a): split a 1.4.x-shaped assistant
settings dict into the connection part and the assistant part, and store a connection
directly (bypassing the API, as the 3b `configure` helper bypasses the configure-first rule)."""

from __future__ import annotations

from typing import Any

CONN_KEYS = {
    "auth_mode",
    "access_key_id",
    "secret_access_key",
    "session_token",
    "access_key_created_at",
    "ra_region",
    "ra_trust_anchor_arn",
    "ra_profile_arn",
    "ra_role_arn",
    "ra_session_seconds",
    "proxy_mode",
    "proxy_url",
}


def split(fields: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(connection fields incl. `region` / `endpoint_overrides`, assistant fields)."""
    conn = {k: v for k, v in fields.items() if k in CONN_KEYS}
    asst = {k: v for k, v in fields.items() if k not in CONN_KEYS and k != "ra_endpoint_url"}
    region = fields.get("ra_region") or fields.get("region") or "us-east-1"
    conn["region"] = region
    conn.pop("ra_region", None)
    overrides = []
    if fields.get("ra_endpoint_url"):
        overrides.append(
            {"service": "rolesanywhere", "region": region, "url": fields["ra_endpoint_url"]}
        )
    if fields.get("sts_endpoint_url"):
        overrides.append({"service": "sts", "region": region, "url": fields["sts_endpoint_url"]})
        asst.pop("sts_endpoint_url", None)
    conn["endpoint_overrides"] = overrides
    return conn, asst


async def store_connection(rt: Any, fields: dict[str, Any], name: str = "Default") -> int:
    """Create or update the connection `name` with `fields` (API input names; secrets
    encrypted) and return its id."""
    from netmon.aws import connections as conns

    async with rt.sessionmaker() as session:
        rows = [r for r in await conns.list_rows(session) if r["name"] == name]
        if rows:
            row = conns.merge(rows[0], fields, rt.secret_key)
            await conns.update(session, row)
            cid = int(rows[0]["id"])
        else:
            row = conns.new_row({**fields, "name": name}, rt.secret_key)
            cid = int((await conns.insert(session, row))["id"])
        await conns.sync_legacy(session)
        await session.commit()
    rt.aws.invalidate(cid)
    return cid
