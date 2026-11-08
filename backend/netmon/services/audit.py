"""[2b-3] Audit log (architecture § 12.8, data-model § audit_log).

Interface used by every router::

    from netmon.services import audit
    ctx = audit_ctx              # dependency `AuditCtxDep` (netmon.api.deps) or AuditCtx.system(rt)
    await session.flush()        # creates: the entity needs its id first
    await audit.record(session, ctx, "device.create", audit.entity("device", d.id, d.name,
                       device_id=d.id), audit.created({...}))
    await session.commit()       # the record is committed with the change (same transaction)

`record()` adds the row to the caller's session; it never commits and is a no-op while the
`audit` feature is OFF (`ctx.enabled`, snapshotted when the context is built). `changes` is
always passed through `redact()`: secret values never reach the table.

Helpers to build `changes` (`{"field": [old, new]}`): `diff(old, new)`, `created(values)`,
`deleted(values)`. Nested dicts are flattened to dotted keys (`config.url`, `ldap.servers`).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.audit")

MASK = "***"
PURGE_BATCH = 50000
EXPORT_MAX_ROWS = 100000
BOM = "\ufeff"  # UTF-8 BOM so Excel opens the CSV as UTF-8

# Leaf names whose values are secrets wherever they appear (any nesting level).
SECRET_NAMES = frozenset(
    {
        "password",
        "password_hash",
        "current_password",
        "new_password",
        "bind_password",
        "bind_password_enc",
        "community",
        "communities",
        "trap_communities",
        "v3_auth_key",
        "v3_priv_key",
        "auth_key",
        "priv_key",
        "bot_token",
        "token",
        "token_hash",
        "secret",
        "headers",  # webhook headers commonly carry Authorization values
    }
)

ACTOR_TYPES = ("user", "token", "system", "anonymous")


# ---------------------------------------------------------------- context
@dataclass(frozen=True)
class AuditCtx:
    """Who performs the change. Build it with the router dependency (`AuditCtxDep`) or
    `AuditCtx.system(rt)`; `enabled` is the `audit` feature at build time."""

    actor_type: str = "system"
    actor_user_id: int | None = None
    actor_name: str = ""
    token_id: int | None = None
    ip: str | None = None
    enabled: bool = False
    source: str | None = None  # login source for auth.* actions (local|ldap|token)

    @staticmethod
    def system(rt: Runtime | None, name: str = "system") -> AuditCtx:
        return AuditCtx(actor_type="system", actor_name=name, enabled=audit_enabled(rt))

    @staticmethod
    def anonymous(rt: Runtime | None, name: str, ip: str | None) -> AuditCtx:
        return AuditCtx(
            actor_type="anonymous", actor_name=(name or "")[:256], ip=ip, enabled=audit_enabled(rt)
        )

    def with_enabled(self, enabled: bool) -> AuditCtx:
        return replace(self, enabled=enabled)

    def enabled_or_toggling(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
        """Settings: also audit the change that turns the audit log on or off."""
        key = "feature_audit_enabled"
        return self.enabled or bool(before.get(key)) != bool(after.get(key))

    def as_user(self, user_id: int, name: str) -> AuditCtx:
        return replace(self, actor_type="user", actor_user_id=user_id, actor_name=name[:256])


def audit_enabled(rt: Any) -> bool:
    features = getattr(rt, "features", None) if rt is not None else None
    if features is None:
        return False
    try:
        return bool(features.enabled("audit"))
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class Entity:
    type: str | None = None
    id: int | None = None
    name: str | None = None
    device_id: int | None = None


def entity(
    type_: str | None,
    id_: int | None = None,
    name: str | None = None,
    *,
    device_id: int | None = None,
) -> Entity:
    return Entity(type_, id_, (name[:256] if isinstance(name, str) else name), device_id)


# ---------------------------------------------------------------- pure: changes / redaction
def _is_secret(path: str, extra: frozenset[str]) -> bool:
    leaf = path.rsplit(".", 1)[-1]
    return leaf in SECRET_NAMES or path in extra or leaf in extra


def _scrub(value: Any, extra: frozenset[str], prefix: str = "") -> Any:
    """Replace secret leaves inside nested values (lists of dicts, e.g. trap_v3_users)."""
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out[k] = (
                (MASK if v not in (None, "") else v)
                if _is_secret(p, extra)
                else _scrub(v, extra, p)
            )
        return out
    if isinstance(value, list | tuple):
        return [_scrub(v, extra, prefix) for v in value]
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        from netmon.services.serialize import iso

        return iso(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        items = [_jsonable(v) for v in value]
        return sorted(items, key=str) if isinstance(value, set | frozenset) else items
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def flatten(values: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Nested dicts → dotted keys; lists and scalars are leaves."""
    out: dict[str, Any] = {}
    for k, v in values.items():
        p = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, Mapping) and v and not _is_secret(p, frozenset()):
            out.update(flatten(v, p))
        else:
            out[p] = v
    return out


def redact(changes: Mapping[str, Any], secret_keys: Iterable[str] = ()) -> dict[str, list[Any]]:
    """Pure: make `changes` safe to store. A secret field keeps only whether it was set
    (`"***"` or null on each side); secret leaves nested in values are masked; values are
    made JSON-safe. Keys whose last segment is in SECRET_NAMES (or listed in `secret_keys`,
    as a dotted path or a leaf name) are secrets."""
    extra = frozenset(secret_keys)
    out: dict[str, list[Any]] = {}
    for key, pair in changes.items():
        if not isinstance(pair, list | tuple) or len(pair) != 2:
            pair = [None, pair]
        old, new = pair
        if _is_secret(str(key), extra):
            out[str(key)] = [
                MASK if old not in (None, "", [], {}) else None,
                MASK if new not in (None, "", [], {}) else None,
            ]
        else:
            out[str(key)] = [
                _jsonable(_scrub(old, extra, str(key))),
                _jsonable(_scrub(new, extra, str(key))),
            ]
    return out


def diff(
    old: Mapping[str, Any], new: Mapping[str, Any], secret_keys: Iterable[str] = ()
) -> dict[str, list[Any]]:
    """`{"field": [old, new]}` for every changed (flattened) field; redacted."""
    extra = frozenset(secret_keys)
    fo, fn = flatten(old), flatten(new)
    raw: dict[str, list[Any]] = {}
    for key in list(dict.fromkeys([*fo, *fn])):
        a, b = fo.get(key), fn.get(key)
        if _jsonable(a) == _jsonable(b):
            continue
        raw[key] = [a, b]
    return redact(raw, extra)


def created(values: Mapping[str, Any], secret_keys: Iterable[str] = ()) -> dict[str, list[Any]]:
    """Create: the new values (`[null, new]`); secrets as `[null, "***"]` when set."""
    flat = flatten(values)
    return redact({k: [None, v] for k, v in flat.items()}, secret_keys)


def deleted(values: Mapping[str, Any], secret_keys: Iterable[str] = ()) -> dict[str, list[Any]]:
    """Delete: the old values (`[old, null]`); secrets omitted."""
    extra = frozenset(secret_keys)
    flat = flatten(values)
    return redact({k: [v, None] for k, v in flat.items() if not _is_secret(k, extra)}, extra)


# ---------------------------------------------------------------- write
_INSERT = text(
    """
    INSERT INTO audit_log (time, actor_type, actor_user_id, actor_name, token_id, ip, action,
                           entity_type, entity_id, entity_name, device_id, source, reason, changes)
    VALUES (coalesce(:time, now()), :actor_type, :actor_user_id, :actor_name, :token_id, :ip,
            :action, :entity_type, :entity_id, :entity_name, :device_id, :source, :reason,
            CAST(:changes AS jsonb))
    """
)


async def record(
    session: AsyncSession,
    ctx: AuditCtx | None,
    action: str,
    entity: Entity | None = None,
    changes: Mapping[str, Any] | None = None,
    *,
    source: str | None = None,
    reason: str | None = None,
    secret_keys: Iterable[str] = (),
    time: datetime | None = None,
) -> None:
    """Add one audit row to `session` (same transaction as the change; the caller commits).
    No-op when `ctx` is None or the audit feature was OFF when `ctx` was built."""
    if ctx is None or not ctx.enabled:
        return
    if ctx.actor_type not in ACTOR_TYPES:
        raise ValueError(f"invalid actor_type {ctx.actor_type!r}")
    ent = entity or Entity()
    safe = redact(changes or {}, secret_keys)
    src = source if source is not None else ctx.source
    await session.execute(
        _INSERT,
        {
            "time": time,
            "actor_type": ctx.actor_type,
            "actor_user_id": ctx.actor_user_id,
            "actor_name": (ctx.actor_name or "")[:256],
            "token_id": ctx.token_id,
            "ip": (ctx.ip or None) and ctx.ip[:45],
            "action": action[:64],
            "entity_type": ent.type[:32] if ent.type else None,
            "entity_id": ent.id,
            "entity_name": ent.name[:256] if isinstance(ent.name, str) else ent.name,
            "device_id": ent.device_id,
            "source": src,
            "reason": reason[:64] if reason else None,
            "changes": json.dumps(safe, ensure_ascii=False, separators=(",", ":")),
        },
    )


async def record_now(
    rt: Runtime,
    ctx: AuditCtx | None,
    action: str,
    entity: Entity | None = None,
    changes: Mapping[str, Any] | None = None,
    **kw: Any,
) -> None:
    """Own transaction (logins, failed attempts: nothing else to commit). Never raises."""
    if ctx is None or not ctx.enabled:
        return
    try:
        async with rt.sessionmaker() as session:
            await record(session, ctx, action, entity, changes, **kw)
            await session.commit()
    except Exception:  # noqa: BLE001 - an audit failure must not break a login response
        log.exception("Writing the audit record %s failed", action)


# ---------------------------------------------------------------- read
COLUMNS = (
    "id, time, actor_type, actor_user_id, actor_name, token_id, ip, action, entity_type, "
    "entity_id, entity_name, device_id, source, reason, changes"
)


@dataclass
class AuditFilter:
    since: datetime | None = None
    until: datetime | None = None
    actor: str | None = None
    actor_user_id: int | None = None
    action: str | None = None
    entity_type: str | None = None
    entity_id: int | None = None
    device_id: int | None = None
    ip: str | None = None

    def where(self) -> tuple[str, dict[str, Any]]:
        conds: list[str] = []
        p: dict[str, Any] = {}
        if self.since is not None:
            conds.append("time >= :since")
            p["since"] = self.since
        if self.until is not None:
            conds.append("time <= :until")
            p["until"] = self.until
        if self.actor:
            conds.append("actor_name ILIKE :actor ESCAPE '\\'")
            p["actor"] = "%" + like_escape(self.actor) + "%"
        if self.actor_user_id is not None:
            conds.append("actor_user_id = :auid")
            p["auid"] = self.actor_user_id
        if self.action:
            if self.action.endswith("."):
                conds.append("action LIKE :action ESCAPE '\\'")
                p["action"] = like_escape(self.action) + "%"
            else:
                conds.append("action = :action")
                p["action"] = self.action
        if self.entity_type:
            conds.append("entity_type = :etype")
            p["etype"] = self.entity_type
        if self.entity_id is not None:
            conds.append("entity_id = :eid")
            p["eid"] = self.entity_id
        if self.device_id is not None:
            conds.append("device_id = :did")
            p["did"] = self.device_id
        if self.ip:
            conds.append("ip = :ip")
            p["ip"] = self.ip
        return (" WHERE " + " AND ".join(conds)) if conds else "", p


def like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def entry_out(r: Any) -> dict[str, Any]:
    from netmon.services.serialize import iso

    changes = r.changes
    if isinstance(changes, str):
        changes = json.loads(changes)
    return {
        "id": int(r.id),
        "time": iso(r.time),
        "actor_type": r.actor_type,
        "actor_user_id": r.actor_user_id,
        "actor_name": r.actor_name,
        "token_id": r.token_id,
        "ip": r.ip,
        "action": r.action,
        "entity_type": r.entity_type,
        "entity_id": None if r.entity_id is None else int(r.entity_id),
        "entity_name": r.entity_name,
        "device_id": r.device_id,
        "source": r.source,
        "reason": r.reason,
        "changes": changes or {},
    }


async def list_page(
    session: AsyncSession, flt: AuditFilter, offset: int, limit: int
) -> tuple[list[dict[str, Any]], int]:
    where, p = flt.where()
    total = (await session.execute(text(f"SELECT count(*) FROM audit_log{where}"), p)).scalar_one()
    rows = (
        await session.execute(
            text(
                f"SELECT {COLUMNS} FROM audit_log{where} ORDER BY time DESC, id DESC "
                "OFFSET :off LIMIT :lim"
            ),
            {**p, "off": offset, "lim": limit},
        )
    ).all()
    return [entry_out(r) for r in rows], int(total)


CSV_HEADER = [
    "time",
    "actor_type",
    "actor_name",
    "ip",
    "action",
    "entity_type",
    "entity_id",
    "entity_name",
    "source",
    "reason",
    "changes",
]


def csv_line(values: list[Any]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerow(
        ["" if v is None else _csv_safe(v) for v in values]
    )
    return buf.getvalue()


def _csv_safe(v: Any) -> Any:
    """Spreadsheet formula injection guard (=, +, -, @ at the start of a text cell)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


async def export_rows(
    rt: Runtime, flt: AuditFilter, limit: int = EXPORT_MAX_ROWS, chunk: int = 2000
) -> AsyncIterator[str]:
    """CSV lines (header first), newest first, at most `limit` rows, streamed with a server
    cursor (own session; the request's session is not held while the client reads)."""
    from netmon.services.serialize import iso

    where, p = flt.where()
    yield BOM + csv_line(CSV_HEADER)
    sql = text(f"SELECT {COLUMNS} FROM audit_log{where} ORDER BY time DESC, id DESC LIMIT :lim")
    async with rt.sessionmaker() as session:
        result = await session.stream(sql, {**p, "lim": limit})
        async for part in result.partitions(chunk):
            lines = []
            for r in part:
                changes = r.changes if not isinstance(r.changes, str) else json.loads(r.changes)
                lines.append(
                    csv_line(
                        [
                            iso(r.time),
                            r.actor_type,
                            r.actor_name,
                            r.ip,
                            r.action,
                            r.entity_type,
                            r.entity_id,
                            r.entity_name,
                            r.source,
                            r.reason,
                            json.dumps(changes or {}, ensure_ascii=False, separators=(",", ":")),
                        ]
                    )
                )
            yield "".join(lines)


async def count_matching(session: AsyncSession, flt: AuditFilter, cap: int) -> int:
    where, p = flt.where()
    return int(
        (
            await session.execute(
                text(f"SELECT count(*) FROM (SELECT 1 FROM audit_log{where} LIMIT :cap) s"),
                {**p, "cap": cap},
            )
        ).scalar_one()
    )


# ---------------------------------------------------------------- retention
async def purge_old(rt: Runtime, days: int, batch: int = PURGE_BATCH) -> int:
    """Delete rows older than `days` (batches). Runs whether or not the feature is on."""
    total = 0
    while True:
        async with rt.sessionmaker() as session:
            await session.execute(text("SET LOCAL netmon.audit_purge = 'on'"))
            res = await session.execute(
                text(
                    "DELETE FROM audit_log WHERE id IN (SELECT id FROM audit_log "
                    "WHERE time < now() - make_interval(days => :d) LIMIT :n)"
                ),
                {"d": int(days), "n": batch},
            )
            await session.commit()
        n = res.rowcount or 0
        total += n
        if n < batch:
            return total
        await asyncio.sleep(0)


# ---------------------------------------------------------------- settings
FEATURE_PREFIX = "feature_"
FEATURE_SUFFIX = "_enabled"


async def record_settings(
    session: AsyncSession, ctx: AuditCtx | None, before: Mapping[str, Any], after: Mapping[str, Any]
) -> None:
    """`PUT /api/settings`: one `feature.enable|disable` per toggled feature (entity_name =
    feature) and one `settings.update` (entity null) with the changed keys (objects such as
    `smtp` / `ldap` diffed per field; secrets only as "***")."""
    if ctx is None or not ctx.enabled_or_toggling(before, after):
        return
    others_old: dict[str, Any] = {}
    others_new: dict[str, Any] = {}
    for key in dict.fromkeys([*before, *after]):
        old, new = before.get(key), after.get(key)
        if _jsonable(old) == _jsonable(new):
            continue
        if key.startswith(FEATURE_PREFIX) and key.endswith(FEATURE_SUFFIX):
            feature = key[len(FEATURE_PREFIX) : -len(FEATURE_SUFFIX)]
            await record(
                session,
                ctx.with_enabled(True) if feature == "audit" else ctx,
                "feature.enable" if new else "feature.disable",
                entity("feature", None, feature),
                {key: [bool(old), bool(new)]},
            )
            continue
        others_old[key] = old
        others_new[key] = new
    if others_old or others_new:
        changes = diff(others_old, others_new)
        if changes:
            await record(session, ctx, "settings.update", None, changes)
