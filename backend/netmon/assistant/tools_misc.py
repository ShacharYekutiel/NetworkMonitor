"""[3b] Read-only tools: new devices, masked configuration backups, notification channels (rule
drafts only) and the Knowledge Base search (api.md § [3b] Tools).

Configuration tools are offered only while `config_backup` is ON, the assistant data setting
`send_config_excerpts` is ON **and** `config_backup_mask_secrets` is ON; a version stored
unmasked (before masking was turned on) is never read; the text passes the secret redaction
again in `run_tool`."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import Field
from sqlalchemy import text

from netmon.assistant.tools import (
    Args,
    ToolCtx,
    ToolDef,
    ToolError,
    ToolOutput,
    cap,
    config_gate,
    feature_gate,
    iso,
    parse_time,
    purpose_gate,
    register,
    require_device,
)

DIFF_MAX_LINES = 400
DIFF_MAX_BYTES = 16 * 1024


# --------------------------------------------------------------------------- list_new_devices
class NewDevicesArgs(Args):
    state: list[Literal["new", "known", "allowed", "baseline"]] | None = None
    device_id: int | None = None
    since: str | None = None
    limit: int = Field(20, ge=1, le=100)


async def list_new_devices(ctx: ToolCtx, a: NewDevicesArgs) -> ToolOutput:
    from netmon.newdev.service import known_macs_out

    conds = ["true"]
    p: dict[str, Any] = {}
    states = list(a.state or ["new"])
    conds.append("k.state = ANY(:st)")
    p["st"] = states
    if a.device_id is not None:
        await require_device(ctx.session, a.device_id)
        conds.append("(k.last_device_id = :d OR k.first_device_id = :d)")
        p["d"] = a.device_id
    since = parse_time(a.since, "since")
    if since is not None:
        conds.append("coalesce(k.detected_at, k.first_seen) >= :s")
        p["s"] = since
    rows = (
        (
            await ctx.session.execute(
                text(
                    f"SELECT * FROM known_macs k WHERE {' AND '.join(conds)} "  # noqa: S608
                    "ORDER BY coalesce(k.detected_at, k.first_seen) DESC, k.mac LIMIT :lim"
                ),
                {**p, "lim": a.limit + 1},
            )
        )
        .mappings()
        .all()
    )
    out = await known_macs_out(ctx.session, rows=rows[: a.limit])
    items = []
    for m in out:
        did = m.get("last_device_id") or m.get("first_device_id")
        items.append(
            {
                "mac": m.get("mac"),
                "vendor": cap(m.get("vendor"), 64),
                "ip": m.get("ip") or m.get("first_ip"),
                "hostname": cap(m.get("hostname") or m.get("first_hostname"), 200),
                "switch_name": cap(m.get("last_device_name") or m.get("first_device_name")),
                "port_name": cap(m.get("last_port_name") or m.get("first_port_name")),
                "vlan": m.get("last_vlan")
                if m.get("last_vlan") is not None
                else m.get("first_vlan"),
                "state": m.get("state"),
                "detected_at": m.get("detected_at") or m.get("first_seen"),
                "ref": f"device/{did}" if did else None,
            }
        )
    return ToolOutput({"items": items}, len(items), truncated=len(rows) > a.limit)


# --------------------------------------------------------------------------- config tools
class ConfigVersionsArgs(Args):
    device_id: int
    limit: int = Field(10, ge=1, le=20)


async def config_versions(ctx: ToolCtx, a: ConfigVersionsArgs) -> ToolOutput:
    from netmon.configbackup.service import version_numbers

    await require_device(ctx.session, a.device_id)
    nums = await version_numbers(ctx.session, a.device_id)
    rows = (
        (
            await ctx.session.execute(
                text(
                    "SELECT id, collected_at, trigger, driver, masked, size_bytes, lines, added, "
                    "removed, initial, is_latest FROM config_versions WHERE device_id = :d "
                    "ORDER BY collected_at DESC, id DESC LIMIT :lim"
                ),
                {"d": a.device_id, "lim": a.limit},
            )
        )
        .mappings()
        .all()
    )
    items = [
        {
            "id": int(r["id"]),
            "ref": f"config/{a.device_id}/{r['id']}",
            "version_no": nums.get(int(r["id"]), 0),
            "collected_at": iso(r["collected_at"]),
            "trigger": r["trigger"],
            "driver": r["driver"],
            "masked": bool(r["masked"]),
            "lines": int(r["lines"]),
            "added": int(r["added"]),
            "removed": int(r["removed"]),
            "initial": bool(r["initial"]),
            "is_latest": bool(r["is_latest"]),
        }
        for r in rows
    ]
    return ToolOutput({"device_ref": f"device/{a.device_id}", "items": items}, len(items))


class ConfigDiffArgs(Args):
    device_id: int
    from_version_id: int | None = None
    to_version_id: int | None = None


async def _version(ctx: ToolCtx, device_id: int, version_id: int) -> Any:
    row = (
        (
            await ctx.session.execute(
                text(
                    "SELECT id, content, masked FROM config_versions "
                    "WHERE id = :v AND device_id = :d"
                ),
                {"v": version_id, "d": device_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ToolError("not_found", f"version {version_id} of device {device_id} not found")
    if not row["masked"]:
        raise ToolError("not_found", f"version {version_id} was stored unmasked; not readable")
    return row


async def config_diff(ctx: ToolCtx, a: ConfigDiffArgs) -> ToolOutput:
    from netmon.configbackup import normalize

    await require_device(ctx.session, a.device_id)
    to_id = a.to_version_id
    if to_id is None:
        to_id = (
            await ctx.session.execute(
                text("SELECT id FROM config_versions WHERE device_id = :d AND is_latest"),
                {"d": a.device_id},
            )
        ).scalar()
        if to_id is None:
            raise ToolError("not_found", "the device has no configuration backup")
    from_id = a.from_version_id
    if from_id is None:
        from_id = (
            await ctx.session.execute(
                text(
                    "SELECT id FROM config_versions WHERE device_id = :d AND (collected_at, id) "
                    "< (SELECT collected_at, id FROM config_versions WHERE id = :t) "
                    "ORDER BY collected_at DESC, id DESC LIMIT 1"
                ),
                {"d": a.device_id, "t": to_id},
            )
        ).scalar()
        if from_id is None:
            raise ToolError("not_found", "there is no earlier version to compare with")
    va = await _version(ctx, a.device_id, int(from_id))
    vb = await _version(ctx, a.device_id, int(to_id))
    diff_text, added, removed, truncated = await asyncio.to_thread(
        normalize.diff,
        va["content"],
        vb["content"],
        from_name=f"version {from_id}",
        to_name=f"version {to_id}",
    )
    lines = diff_text.splitlines()
    if len(lines) > DIFF_MAX_LINES:
        lines = lines[:DIFF_MAX_LINES]
        truncated = True
    out_text = "\n".join(lines)
    if len(out_text.encode()) > DIFF_MAX_BYTES:
        out_text = out_text.encode()[:DIFF_MAX_BYTES].decode(errors="ignore")
        truncated = True
    data = {
        "from": int(from_id),
        "to": int(to_id),
        "added": int(added),
        "removed": int(removed),
        "diff": out_text,
        "truncated": bool(truncated),
        "ref": f"config-diff/{a.device_id}/{from_id}/{to_id}",
        "device_ref": f"device/{a.device_id}",
    }
    return ToolOutput(data, len(lines), truncated=bool(truncated))


class ConfigSearchArgs(Args):
    text: str = Field(min_length=2, max_length=100, description="literal text (no regex)")


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def config_search(ctx: ToolCtx, a: ConfigSearchArgs) -> ToolOutput:
    rows = (
        (
            await ctx.session.execute(
                text(
                    "SELECT v.id, v.device_id, d.name AS device_name, v.content "
                    "FROM config_versions v "
                    "JOIN devices d ON d.id = v.device_id WHERE v.is_latest AND v.masked "
                    "AND v.content ILIKE :q ESCAPE '\\' ORDER BY lower(d.name), d.id LIMIT 21"
                ),
                {"q": f"%{_like(a.text)}%"},
            )
        )
        .mappings()
        .all()
    )
    from netmon.assistant.redact import redact_secrets

    needle = a.text.lower()
    items = []
    for r in rows[:20]:
        hits = []
        for i, line in enumerate(r["content"].splitlines(), start=1):
            # match on the redacted line: a search for a secret value must not reveal which
            # devices contain it (no oracle)
            safe = redact_secrets(line)
            if needle in safe.lower():
                hits.append({"line": i, "text": cap(safe, 300)})
                if len(hits) >= 5:
                    break
        if not hits:
            continue
        items.append(
            {
                "device_ref": f"device/{r['device_id']}",
                "device_name": cap(r["device_name"]),
                "version_ref": f"config/{r['device_id']}/{r['id']}",
                "lines": hits,
            }
        )
    return ToolOutput({"items": items}, len(items), truncated=len(rows) > 20)


# --------------------------------------------------------------------------- notification channels
class NoArgs(Args):
    pass


async def list_notification_channels(ctx: ToolCtx, a: NoArgs) -> ToolOutput:
    rows = (
        await ctx.session.execute(
            text(
                "SELECT id, name, type, enabled FROM notification_channels ORDER BY lower(name), "
                "id LIMIT 101"
            )
        )
    ).all()
    items = [
        {"id": int(r.id), "name": cap(r.name), "type": r.type, "enabled": bool(r.enabled)}
        for r in rows[:100]
    ]
    return ToolOutput({"items": items}, len(items), truncated=len(rows) > 100)


# --------------------------------------------------------------------------- knowledge base
class KnowledgeArgs(Args):
    query: str = Field(min_length=3, max_length=300)


def _kb_client(ctx: ToolCtx) -> Any:
    svc = getattr(ctx.rt, "assistant", None)
    client = getattr(svc, "client", None)
    if client is None or not hasattr(client, "retrieve"):
        raise ToolError("feature_disabled", "the Knowledge Base is not available")
    return client


async def search_knowledge(ctx: ToolCtx, a: KnowledgeArgs) -> ToolOutput:
    """Bedrock Knowledge Base ``Retrieve`` through the connection layer (`rt.assistant.client
    .retrieve(query, kb)` → ``[{"text", "score", "title", "source_url"}]``)."""
    from netmon.assistant.features.common import setting

    client = _kb_client(ctx)
    kb = await setting(ctx.session, "assistant_kb", {}) or {}
    if not isinstance(kb, dict) or not kb.get("knowledge_base_id"):
        raise ToolError("feature_disabled", "no Knowledge Base is configured")
    try:
        results = await client.retrieve(a.query, kb)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the model gets an error envelope
        code = getattr(exc, "code", None)
        raise ToolError(
            "kb_unavailable", f"knowledge base unavailable ({code or type(exc).__name__})"
        ) from None
    items = []
    for r in list(results or [])[:10]:
        if not isinstance(r, dict):
            continue
        n = len(ctx.kb_sources) + 1
        ref = f"kb/{n}"
        url = r.get("url") or r.get("source_url")
        if isinstance(url, str) and url.startswith("https://"):
            ctx.kb_sources[ref] = url
        else:
            url = None
            ctx.kb_sources[ref] = ""
        items.append(
            {
                "ref": ref,
                "title": cap(r.get("title") or "", 200),
                "source_url": url,
                "score": r.get("score"),
                "text": cap(r.get("text") or "", 1500),
            }
        )
    return ToolOutput({"items": items}, len(items))


register(
    ToolDef(
        "list_new_devices",
        "MAC addresses detected as new on access ports (or known/allowed), with switch, port, "
        "VLAN, IP and hostname.",
        NewDevicesArgs,
        list_new_devices,
        gate=feature_gate("new_device_alerts"),
        untrusted=("hostname",),
        source_key="new_device_alerts",
    )
)
register(
    ToolDef(
        "config_versions",
        "Configuration backup versions of a device (metadata only: time, trigger, +/- lines).",
        ConfigVersionsArgs,
        config_versions,
        gate=config_gate,
        source_key="config",
    )
)
register(
    ToolDef(
        "config_diff",
        "Masked unified diff between two configuration versions of a device (default: previous → "
        "latest), ≤ 400 lines.",
        ConfigDiffArgs,
        config_diff,
        gate=config_gate,
        untrusted=("diff",),
        source_key="config",
    )
)
register(
    ToolDef(
        "config_search",
        "Literal (not regex) search in the latest masked configuration of every device; ≤ 20 "
        "devices × 5 lines.",
        ConfigSearchArgs,
        config_search,
        gate=config_gate,
        untrusted=("text",),
        source_key="config",
    )
)
register(
    ToolDef(
        "list_notification_channels",
        "Notification channels (id, name, type, enabled) for alert-rule drafts.",
        NoArgs,
        list_notification_channels,
        gate=purpose_gate("rule_draft"),
        untrusted=("name",),
    )
)
register(
    ToolDef(
        "search_knowledge",
        "Search the organisation's Knowledge Base (runbooks, documentation); returns passages with "
        "their source.",
        KnowledgeArgs,
        search_knowledge,
        gate=feature_gate("assistant", "ai_knowledge"),
        untrusted=("text", "title"),
        source_key="knowledge",
    )
)
