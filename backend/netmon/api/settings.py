from __future__ import annotations

from fastapi import APIRouter

from netmon.alerts import templates
from netmon.alerts.email import SmtpError, send_email
from netmon.api.common import merge_secret
from netmon.api.deps import DB, RT, Admin, Audit, feature_on
from netmon.api.errors import ApiError
from netmon.db.timescale import reapply_retention
from netmon.flows import settings as flow_settings
from netmon.schemas import SettingsPut, SmtpTestIn
from netmon.services import audit
from netmon.services import settings as settings_svc

router = APIRouter()


@router.get("/api/settings/public")
async def public_settings(rt: RT, session: DB) -> dict:
    out = {
        "default_language": await settings_svc.get(session, "default_language"),
        "timezone": await settings_svc.get(session, "timezone"),
    }
    # [2b-3] the login page shows the AD domain hint (null while AD login is off)
    ldap_public = None
    if feature_on(rt, "ldap"):
        stored = await settings_svc.get(session, "ldap")
        ldap_public = {
            "domain": stored.get("domain") or "",
            "netbios_domain": stored.get("netbios_domain") or "",
        }
    out["ldap"] = ldap_public
    out["features"] = rt.features.snapshot()  # [2b-3]
    from netmon.api.sso import sso_public

    out["sso"] = await sso_public(rt, session)  # [3]
    return out


@router.get("/api/settings")
async def get_settings(_: Admin, rt: RT, session: DB) -> dict:
    return settings_svc.settings_out(await settings_svc.get_all(session), rt.secret_key)


@router.put("/api/settings")
async def put_settings(body: SettingsPut, _: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    current = await settings_svc.get_all(session)
    fields = body.model_fields_set
    await _put_3b(body, fields, current, session)  # [3b] before any feature toggle is written
    await _put_2b3_auth(body, fields, current, rt, session)
    await _put_2b3_ingest(body, fields, current, session)
    await _put_3_plain(body, fields, current, session)
    await _put_3c(body, fields, current, rt, session)  # [3] SSO toggles, reports, chat ack
    await flow_settings.put(body, fields, session)  # [3] flows
    retention_changed = False
    if "smtp" in fields and body.smtp is not None:
        smtp = dict(current["smtp"])
        incoming = body.smtp
        for key in incoming.model_fields_set:
            if key == "password":
                smtp["password"] = merge_secret(incoming.password, smtp.get("password"))
                continue
            value = getattr(incoming, key)
            if value is None and key in ("host", "port", "security", "from_address", "from_name"):
                continue
            smtp[key] = value if value != "" or key != "username" else None
        await settings_svc.put(session, "smtp", smtp)
    for key in ("retention_days", "event_retention_days", "default_interval_s", "default_language"):
        if key in fields and getattr(body, key) is not None:
            if key == "retention_days" and body.retention_days != current["retention_days"]:
                retention_changed = True
            await settings_svc.put(session, key, getattr(body, key))
    if "public_base_url" in fields:
        await settings_svc.put(session, "public_base_url", body.public_base_url)
    tz_changed = topo_changed = False
    if "timezone" in fields and body.timezone is not None:
        tz_changed = body.timezone != current["timezone"]
        await settings_svc.put(session, "timezone", body.timezone)
    if "topology_refresh_min" in fields and body.topology_refresh_min is not None:
        topo_changed = body.topology_refresh_min != current["topology_refresh_min"]
        await settings_svc.put(session, "topology_refresh_min", body.topology_refresh_min)
    for key in (  # [2b-2]; the port scheduler reads them on every pass
        "port_inventory_min",
        "uplink_mac_threshold",
        "port_mac_retention_days",
        "reverse_dns",
    ):
        if key in fields and getattr(body, key) is not None:
            await settings_svc.put(session, key, getattr(body, key))
    # [2b-3] audit (same transaction): feature toggles and changed keys, secrets redacted
    await audit.record_settings(session, ctx, current, await settings_svc.get_all(session))
    await session.commit()
    values = await settings_svc.get_all(session)
    if retention_changed and rt.timescale_enabled and rt.engine is not None:
        rt.retention_policy_ok = await reapply_retention(rt.engine, int(values["retention_days"]))
    if tz_changed and rt.maintenance is not None:
        await rt.maintenance.reload()  # weekly windows move with the zone
    if topo_changed and rt.topology_scheduler is not None:
        await rt.topology_scheduler.reschedule()
    await _apply_2b3_ingest(rt, current, values)
    await _apply_3(rt, current, values)
    await flow_settings.after_put(rt, current, values)  # [3] flows
    return settings_svc.settings_out(values, rt.secret_key)


async def _put_2b3_auth(
    body: SettingsPut, fields: set[str], current: dict, rt: RT, session: DB
) -> None:
    """[2b-3] `ldap` (field-by-field merge, write-only bind password, AES-GCM at rest; 422 with
    the missing fields when it is saved while AD login is on or AD login is turned on) and
    `audit_retention_days`."""
    from netmon.auth import ldap_config

    new_ldap = current["ldap"]
    if "ldap" in fields and body.ldap is not None:
        new_ldap = ldap_config.merge(current["ldap"], body.ldap, rt.secret_key)
        await settings_svc.put(session, "ldap", new_ldap)
    toggled = (
        getattr(body, "feature_ldap_enabled", None) if "feature_ldap_enabled" in fields else None
    )
    ldap_on = bool(current.get("feature_ldap_enabled")) if toggled is None else bool(toggled)
    if ldap_on and ("ldap" in fields or (toggled and not current.get("feature_ldap_enabled"))):
        errors = ldap_config.validation_errors(new_ldap, rt.secret_key)
        if errors:
            raise ApiError(
                422, "validation_error", "The Active Directory settings are incomplete", errors
            )
    if "audit_retention_days" in fields and body.audit_retention_days is not None:
        await settings_svc.put(session, "audit_retention_days", body.audit_retention_days)


INGEST_PLAIN_KEYS = (
    "trap_collapse_enabled",
    "trap_collapse_oids",
    "trap_collapse_keep",
    "trap_retention_days",
    "syslog_retention_days",
    "trap_community_check",
    "ingest_unknown_sources",
    "ingest_ignored_sources",
    "flap_threshold",
    "flap_window_min",
    "flap_quiet_min",
    "event_linkdown_scope",
    "trunk_min_tagged_vlans",  # [1.3]
    "event_linkdown_include_trunks",
)


async def _put_2b3_ingest(body: SettingsPut, fields: set[str], current: dict, session: DB) -> None:
    """[2b-3] feature toggles and the ingestion keys (secret lists write-only)."""
    from netmon.ingest.forward import new_target_id
    from netmon.services.features import FEATURES, setting_key

    for name in FEATURES:
        key = setting_key(name)
        if key in fields and getattr(body, key) is not None:
            await settings_svc.put(session, key, bool(getattr(body, key)))
    for key in INGEST_PLAIN_KEYS:
        if key in fields and getattr(body, key) is not None:
            await settings_svc.put(session, key, getattr(body, key))
    # trap_communities: omitted/null keep, a list (also []) replaces
    if "trap_communities" in fields and body.trap_communities is not None:
        await settings_svc.put(session, "trap_communities", list(body.trap_communities))
    # trap_v3_users: the list replaces; a key given as null/omitted keeps the stored key of the
    # entry with the same user name, "" clears, a string replaces
    if "trap_v3_users" in fields and body.trap_v3_users is not None:
        stored = {u.get("username"): u for u in current.get("trap_v3_users") or []}
        users = []
        for u in body.trap_v3_users:
            old = stored.get(u.username) or {}
            item = u.model_dump()
            for k in ("auth_key", "priv_key"):
                item[k] = merge_secret(item.get(k), old.get(k))
            users.append(item)
        await settings_svc.put(session, "trap_v3_users", users)
    if "siem_targets" in fields and body.siem_targets is not None:
        known = {t.get("id") for t in current.get("siem_targets") or []}
        targets = []
        for t in body.siem_targets:
            item = t.model_dump()
            if not item.get("id") or item["id"] not in known:
                item["id"] = item.get("id") or new_target_id()
            targets.append(item)
        ids = [t["id"] for t in targets]
        if len(set(ids)) != len(ids):
            raise ApiError(
                422,
                "validation_error",
                "Duplicate SIEM target id",
                [{"loc": ["body", "siem_targets"], "msg": "ids must be unique"}],
            )
        await settings_svc.put(session, "siem_targets", targets)


async def _put_3c(body: SettingsPut, fields: set[str], current: dict, rt: RT, session: DB) -> None:
    """[3] report / chat-ack keys; turning `sso_kerberos` ON needs a keytab and AD login,
    `sso_oidc` ON needs complete OIDC settings (422 with the missing parts)."""
    from netmon.api.sso import kerberos_enable_errors
    from netmon.auth import oidc
    from netmon.schemas.phase3c import PLAIN_KEYS
    from netmon.services import settings_p3c

    for key in PLAIN_KEYS:
        if key in fields and getattr(body, key) is not None:
            await settings_svc.put(session, key, getattr(body, key))

    def turning_on(name: str) -> bool:
        key = f"feature_{name}_enabled"
        return bool(key in fields and getattr(body, key, None) and not current.get(key))

    errors: list[dict] = []
    if turning_on("sso_kerberos"):
        ldap_key = "feature_ldap_enabled"
        ldap_on = (
            bool(getattr(body, ldap_key))
            if ldap_key in fields and getattr(body, ldap_key) is not None
            else bool(current.get(ldap_key))
        )
        errors += await kerberos_enable_errors(rt, session, ldap_on)
    if turning_on("sso_oidc"):
        errors += oidc.oidc_config_errors(await settings_p3c.get(session, "oidc"), rt.secret_key)
    if errors:
        raise ApiError(422, "validation_error", "Single sign-on is not configured", errors)


async def _put_3b(body: SettingsPut, fields: set[str], current: dict, session: DB) -> None:
    """[3b] `assistant` / `ai_knowledge` can only be turned ON through their configure-first
    endpoints (422 assistant_test_required); `assistant` needs `audit` (both directions,
    422 feature_dependency); the scalar keys are written as they come."""
    from netmon.schemas.assistant import NULLABLE_KEYS, SCALAR_KEYS

    def wanted(name: str) -> bool | None:
        key = f"feature_{name}_enabled"
        return getattr(body, key) if key in fields and getattr(body, key) is not None else None

    for name in ("assistant", "ai_knowledge"):
        if wanted(name) is True and not current.get(f"feature_{name}_enabled"):
            raise ApiError(
                422,
                "assistant_test_required",
                "Run a successful Test connection first (Settings → Assistant)",
                [{"feature": name}],
            )
    assistant_on = (
        bool(current.get("feature_assistant_enabled")) and wanted("assistant") is not False
    )
    if assistant_on and wanted("audit") is False:
        raise ApiError(
            422,
            "feature_dependency",
            "Turn the assistant off before the audit log",
            [{"feature": "assistant", "requires": "audit"}],
        )
    for key in SCALAR_KEYS:
        if key in fields and (getattr(body, key) is not None or key in NULLABLE_KEYS):
            await settings_svc.put(session, key, getattr(body, key))


async def _put_3_plain(body: SettingsPut, fields: set[str], current: dict, session: DB) -> None:
    """[3] config backup and new-device keys (the feature toggles are written with the 2b-3
    ones: FEATURES covers all 17)."""
    keys = (*settings_svc.CONFIG_BACKUP_KEYS, *settings_svc.NEWDEV_KEYS)
    new = {k: getattr(body, k) for k in keys if k in fields and getattr(body, k) is not None}
    lo = new.get("config_backup_min_versions", current["config_backup_min_versions"])
    hi = new.get("config_backup_max_versions", current["config_backup_max_versions"])
    if lo > hi:
        raise ApiError(
            422,
            "validation_error",
            "config_backup_min_versions must not exceed config_backup_max_versions",
            [{"loc": ["body", "config_backup_min_versions"], "msg": "must be <= max_versions"}],
        )
    for key, value in new.items():
        await settings_svc.put(session, key, value)


async def _apply_3(rt: RT, before: dict, after: dict) -> None:
    """[3] after the commit: the config-backup runner and the new-device detector re-read
    their settings."""
    runner = getattr(rt, "configbackup", None)
    if runner is not None and any(
        before.get(k) != after.get(k) for k in (*settings_svc.CONFIG_BACKUP_KEYS, "timezone")
    ):
        await runner.settings_changed()
    newdev = getattr(rt, "newdev", None)
    if newdev is not None and any(before.get(k) != after.get(k) for k in settings_svc.NEWDEV_KEYS):
        await newdev.settings_changed()


async def _apply_2b3_ingest(rt: RT, before: dict, after: dict) -> None:
    """[2b-3] after the commit: runtime toggles, retention policies, ingest configuration."""
    from netmon.db.timescale import reapply_ingest_retention
    from netmon.services.features import FEATURES, setting_key

    changed_features = {
        n: bool(after[setting_key(n)])
        for n in FEATURES
        if bool(before.get(setting_key(n))) != bool(after.get(setting_key(n)))
    }
    ingest = getattr(rt, "ingest", None)
    if ingest is not None:
        cfg_keys = (
            "trap_community_check",
            "trap_communities",
            "trap_v3_users",
            "ingest_unknown_sources",
            "ingest_ignored_sources",
            "trap_collapse_enabled",
            "trap_collapse_oids",
            "trap_collapse_keep",
            "event_linkdown_scope",
            "trunk_min_tagged_vlans",  # [1.3]
            "event_linkdown_include_trunks",
            "flap_threshold",
            "flap_window_min",
            "flap_quiet_min",
            "timezone",
        )
        if any(before.get(k) != after.get(k) for k in cfg_keys):
            await ingest.reload_config()
            if any(before.get(k) != after.get(k) for k in ("trap_communities", "trap_v3_users")):
                await ingest.rebuild_sources()
        if (
            before.get("siem_targets") != after.get("siem_targets")
            and ingest.forwarding_on
            and "siem_forwarding" not in changed_features
        ):
            await ingest.set_forwarding(True)
    if (
        rt.timescale_enabled
        and rt.engine is not None
        and any(
            before.get(k) != after.get(k) for k in ("trap_retention_days", "syslog_retention_days")
        )
    ):
        rt.ingest_retention_policy_ok = await reapply_ingest_retention(
            rt.engine, int(after["trap_retention_days"]), int(after["syslog_retention_days"])
        )
    if changed_features:
        await rt.features.apply(changed_features)


@router.post("/api/settings/smtp/test")
async def smtp_test(body: SmtpTestIn, _: Admin, session: DB) -> dict:
    cfg = await settings_svc.get_all(session)
    await session.close()
    lang = cfg["default_language"]
    payload = {"event": "test", "time": None}
    try:
        await send_email(
            cfg["smtp"],
            [body.to],
            templates.subject(payload, lang),
            templates.text_body(payload, lang),
            templates.html_body(payload, lang),
        )
    except SmtpError as exc:
        raise ApiError(502, "smtp_error", str(exc)) from exc
    return {"ok": True, "message": "sent"}
