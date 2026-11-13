"""[4] The IAM policies a connection (and one hop) needs, rendered from the stored values
(`GET /api/aws/connections/{id}/policies`, architecture § 15.1.6; the same JSON as
`deploy/production/aws/render-policies.py` of PR 4-0b). Pure."""

from __future__ import annotations

from typing import Any

from netmon.aws.sts import role_arn_of_session

VERSION = "2012-10-17"


def base_trust_policy(
    trust_anchor_arn: str, cert_cn: str | None, issuer_cn: str | None
) -> dict[str, Any]:
    """Trust policy of the Roles Anywhere (base) role: `aws:SourceArn` = the trust anchor ARN
    (once), the certificate's subject CN and issuer CN as principal tags."""
    cond: dict[str, Any] = {"ArnEquals": {"aws:SourceArn": trust_anchor_arn}}
    tags = {}
    if cert_cn:
        tags["aws:PrincipalTag/x509Subject/CN"] = cert_cn
    if issuer_cn:
        tags["aws:PrincipalTag/x509Issuer/CN"] = issuer_cn
    if tags:
        cond["StringEquals"] = tags
    return {
        "Version": VERSION,
        "Statement": [
            {
                "Sid": "NetMonitorRolesAnywhere",
                "Effect": "Allow",
                "Principal": {"Service": "rolesanywhere.amazonaws.com"},
                "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"],
                "Condition": cond,
            }
        ],
    }


def hop_caller_policy(role_arn: str, *, roles_anywhere_base: bool) -> dict[str, Any]:
    """Permissions the base identity needs for the hop (`sts:SetSourceIdentity` too when Roles
    Anywhere set a source identity)."""
    actions = ["sts:AssumeRole"] + (["sts:SetSourceIdentity"] if roles_anywhere_base else [])
    return {
        "Version": VERSION,
        "Statement": [
            {"Sid": "NetMonitorHop", "Effect": "Allow", "Action": actions, "Resource": role_arn}
        ],
    }


def hop_trust_policy(
    base_principal: str, external_id: str | None, source_identity: str | None
) -> dict[str, Any]:
    """Trust policy of the target role: principal = the base role (not its session), the
    ExternalId and the inherited source identity. No `sts:TagSession` (§ 15.1.2 fact 5)."""
    actions = ["sts:AssumeRole"] + (["sts:SetSourceIdentity"] if source_identity else [])
    cond: dict[str, Any] = {}
    eq: dict[str, Any] = {}
    if external_id:
        eq["sts:ExternalId"] = external_id
    if source_identity:
        eq["aws:SourceIdentity"] = source_identity
    if eq:
        cond["StringEquals"] = eq
    stmt: dict[str, Any] = {
        "Sid": "NetMonitorFromBase",
        "Effect": "Allow",
        "Principal": {"AWS": base_principal},
        "Action": actions,
    }
    if cond:
        stmt["Condition"] = cond
    return {"Version": VERSION, "Statement": [stmt]}


def render(
    v: dict[str, Any],
    cert: dict[str, str | None],
    *,
    role_arn: str | None = None,
    external_id: str | None = None,
) -> dict[str, Any]:
    """`{"base_trust_policy", "hop_caller_policy", "hop_trust_policy", "notes"}` for the
    connection view `v`; `cert` = `connections.cert_values` (cert_cn, issuer_cn,
    source_identity). Parts whose values are missing are null and `notes` says why."""
    notes: list[str] = []
    ra = v.get("auth_mode") == "roles_anywhere"
    base_trust = None
    if ra:
        ta = v.get("ra_trust_anchor_arn")
        if not ta:
            notes.append("base_trust_policy: the trust anchor ARN is not set")
        else:
            if not cert.get("cert_cn"):
                notes.append(
                    "base_trust_policy: no certificate uploaded (the CN condition is left out)"
                )
            base_trust = base_trust_policy(ta, cert.get("cert_cn"), cert.get("issuer_cn"))
    else:
        notes.append("base_trust_policy: only for a Roles Anywhere connection")
    caller = trust = None
    if role_arn:
        if ra:
            principal = v.get("ra_role_arn")
        else:
            last = v.get("_last_test") or {}
            principal = role_arn_of_session(last.get("arn"))
        caller = hop_caller_policy(role_arn, roles_anywhere_base=ra)
        if not principal:
            notes.append(
                "hop_trust_policy: the base identity's ARN is unknown; "
                "run the connection Test first"
            )
        else:
            src = cert.get("source_identity") if ra else None
            if ra and not src:
                notes.append("hop_trust_policy: no certificate, so the source identity is unknown")
            trust = hop_trust_policy(principal, external_id, src)
        if not external_id:
            notes.append(
                "hop_trust_policy: no ExternalId (recommended against the confused deputy)"
            )
    return {
        "base_trust_policy": base_trust,
        "hop_caller_policy": caller,
        "hop_trust_policy": trust,
        "notes": notes,
    }
