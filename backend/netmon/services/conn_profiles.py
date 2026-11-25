"""[profiles] Connection profiles (data-model "[profiles] Connection profiles").

A connection profile is a named, globally defined credential set of one **kind** (`snmp`,
`ssh`). Every group and device chooses per kind one of three modes:

- `profile` - a `connection_profile_assignments` row references a profile (the node column
              may still hold the node's earlier manual data: kept **dormant**, unused, and
              restored when the node leaves the profile for manual),
- `manual`  - no assignment and the node column `<kind>_credentials` is NOT NULL,
- `inherit` - neither: the parent's value applies.

`resolve` (pure) is the single credential inheritance walk of the whole server; every reader
of SNMP / SSH credentials goes through it. `KINDS` is the registry of kinds: a future kind is
one more entry (plus its node column), the two tables do not change.

Secrets: SNMP profile data uses `secretbox` with owner `conn_profile:<id>`, encrypted only;
SSH profile data uses the [3] form (`configbackup.credentials`). Nothing here logs a secret.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.services import secretbox

if TYPE_CHECKING:
    from pydantic import BaseModel

log = logging.getLogger("netmon.conn_profiles")

MAX_GROUP_DEPTH = 100  # the group chain is walked at most 100 deep (as before 1.7.0)
USAGE_LIST_LIMIT = 1000
MAX_DISCOVERY_PROFILE_IDS = 8
MODES = ("inherit", "profile", "manual")


# ====================================================================== pure data
@dataclass(frozen=True)
class ChainNode:
    """One element of a resolution chain: the device (optional) then its groups up to the
    root. `creds` is the node's own `<kind>_credentials` in stored form (None = none);
    `profile_id` its assignment for this kind (None = no reference). With both, the
    profile decides and `creds` is the dormant manual data."""

    type: str  # "device" | "group"
    id: int
    creds: Any = None
    profile_id: int | None = None
    name: str | None = None


@dataclass(frozen=True)
class Profile:
    id: int
    kind: str
    name: str
    data: Any = None
    description: str = ""


@dataclass(frozen=True)
class Resolution:
    """What decides one kind for a node: `node` (None = the kind's fallback), its `mode`
    (`manual` / `profile`, None for the fallback), the profile and the stored data."""

    kind: str
    mode: str | None
    node: ChainNode | None
    profile_id: int | None = None
    profile: Profile | None = None
    stored: Any = None

    @property
    def source(self) -> str:
        """`device` | `group` | the kind's fallback source (`default` / `none`)."""
        if self.node is None:
            return KINDS[self.kind].fallback
        return self.node.type

    @property
    def group_id(self) -> int | None:
        return self.node.id if self.node is not None and self.node.type == "group" else None


# ====================================================================== kind registry
@dataclass(frozen=True)
class KindSpec:
    kind: str
    column: str  # groups.<column> / devices.<column> (the manual mode)
    fallback: str  # source when nothing in the chain decides
    input_model: Callable[[], type[BaseModel]]
    # incoming API object + stored data -> pending data (validated; SNMP plaintext)
    prepare: Callable[[Any, Any, int | None, str, tuple[str, ...]], Any]
    # pending data -> stored form for this profile id (SNMP: sealed, encrypted only)
    store: Callable[[Any, int], Any]
    # pending data without secrets (insert before the id, which is in the AAD, is known)
    strip: Callable[[Any], Any]
    out: Callable[[Any, int], dict[str, Any] | None]
    audit_view: Callable[[Any, int], dict[str, Any] | None]
    effective: Callable[[Resolution], Any]
    # profile data -> the node column value of a manual copy (re-encrypted for the node)
    to_node: Callable[[Any, int, str, int], Any]
    unreadable: Callable[[Any, int, str], bool]


# ---------------------------------------------------------------- snmp
SNMP_SECRETS = secretbox.SNMP_FIELDS


def _default_snmp() -> dict[str, Any]:
    from netmon.poller.snmp_client import DEFAULT_SNMP

    return dict(DEFAULT_SNMP)


def _snmp_model() -> type[BaseModel]:
    from netmon.schemas import SnmpIn

    return SnmpIn


def _snmp_prepare(
    incoming: Any, stored: Any, profile_id: int | None, secret_key: str, loc: tuple[str, ...]
) -> dict[str, Any]:
    """Field-by-field merge of the given keys over the stored profile (secrets: omitted /
    null keep, "" clears, a string replaces), validated like manual SNMP credentials."""
    from netmon.api.common import merge_snmp
    from netmon.schemas import SnmpIn

    plain = secretbox.open_conn_profile_snmp(stored, profile_id) if stored else None
    if isinstance(plain, dict) and plain:
        base = {
            k: v for k, v in plain.items() if k in SnmpIn.model_fields and k not in SNMP_SECRETS
        }
        given = {k: getattr(incoming, k) for k in incoming.model_fields_set}
        incoming = SnmpIn.model_validate({**base, **given})
        return merge_snmp(incoming, plain, loc)
    return merge_snmp(incoming, None, loc)


def _snmp_store(pending: Any, profile_id: int) -> Any:
    return secretbox.seal_conn_profile_snmp(pending, profile_id)


def _snmp_strip(pending: Any) -> Any:
    return secretbox.strip_fields(pending, SNMP_SECRETS)


def _snmp_out(stored: Any, profile_id: int) -> dict[str, Any] | None:
    from netmon.services.serialize import snmp_out

    return snmp_out(stored, secretbox.CONN_PROFILE, profile_id)


def _snmp_audit(stored: Any, profile_id: int) -> dict[str, Any] | None:
    # plaintext for the comparison; the audit masks community / v3 keys as "***"
    out = secretbox.open_conn_profile_snmp(stored, profile_id)
    return dict(out) if isinstance(out, dict) else None


def _snmp_effective(res: Resolution) -> dict[str, Any]:
    if res.mode == "manual" and res.node is not None:
        return {
            **_default_snmp(),
            **secretbox.open_snmp(res.stored, res.node.type, res.node.id),
        }
    if res.mode == "profile":
        if not isinstance(res.stored, dict):
            # a missing / unusable profile: never fall back to the default community
            return {**_default_snmp(), "community": None}
        return {**_default_snmp(), **secretbox.open_conn_profile_snmp(res.stored, res.profile_id)}
    return _default_snmp()


def _snmp_to_node(data: Any, profile_id: int, node_type: str, node_id: int) -> Any:
    plain = secretbox.open_conn_profile_snmp(data, profile_id)
    return secretbox.seal_snmp(dict(plain), node_type, node_id)


def _snmp_unreadable(stored: Any, profile_id: int, secret_key: str) -> bool:
    return secretbox.snmp_unreadable(stored, secretbox.CONN_PROFILE, profile_id)


# ---------------------------------------------------------------- ssh
def _ssh_model() -> type[BaseModel]:
    from netmon.configbackup.credentials import SshCredentialsIn

    return SshCredentialsIn


def _ssh_prepare(
    incoming: Any, stored: Any, profile_id: int | None, secret_key: str, loc: tuple[str, ...]
) -> dict[str, Any]:
    from netmon.configbackup.credentials import merge

    return merge(incoming, stored, secret_key, loc=loc)


def _ssh_out(stored: Any, profile_id: int) -> dict[str, Any] | None:
    from netmon.configbackup.credentials import ssh_out

    return ssh_out(stored)


def _ssh_audit(stored: Any, profile_id: int) -> dict[str, Any] | None:
    from netmon.configbackup.credentials import audit_view

    return audit_view(stored)


def _ssh_effective(res: Resolution) -> Any:
    return res.stored if res.mode is not None else None


def _ssh_to_node(data: Any, profile_id: int, node_type: str, node_id: int) -> Any:
    return dict(data)  # the [3] form has no row identity in its AAD


def _ssh_unreadable(stored: Any, profile_id: int, secret_key: str) -> bool:
    from netmon.configbackup.credentials import unreadable

    return unreadable(stored, secret_key)


KINDS: dict[str, KindSpec] = {
    "snmp": KindSpec(
        kind="snmp",
        column="snmp_credentials",
        fallback="default",
        input_model=_snmp_model,
        prepare=_snmp_prepare,
        store=_snmp_store,
        strip=_snmp_strip,
        out=_snmp_out,
        audit_view=_snmp_audit,
        effective=_snmp_effective,
        to_node=_snmp_to_node,
        unreadable=_snmp_unreadable,
    ),
    "ssh": KindSpec(
        kind="ssh",
        column="ssh_credentials",
        fallback="none",
        input_model=_ssh_model,
        prepare=_ssh_prepare,
        store=lambda pending, _pid: pending,
        strip=lambda pending: pending,
        out=_ssh_out,
        audit_view=_ssh_audit,
        effective=_ssh_effective,
        to_node=_ssh_to_node,
        unreadable=_ssh_unreadable,
    ),
}


def spec(kind: str) -> KindSpec:
    return KINDS[kind]


# ====================================================================== pure: resolution
def resolve(kind: str, chain: Iterable[ChainNode], profiles: Mapping[int, Profile]) -> Resolution:
    """Walk `chain` (the device, then its group, ... up to the root; a group's own value
    starts at the group). At each node: an assignment uses the profile's current data (the
    node's own column is then dormant), else manual data, else go to the parent. Nothing
    found -> the kind's fallback. A repeated node (a cycle) or more than 1 + MAX_GROUP_DEPTH
    nodes end the walk."""
    if kind not in KINDS:
        raise KeyError(kind)
    seen: set[tuple[str, int]] = set()
    for i, node in enumerate(chain):
        if i > MAX_GROUP_DEPTH:
            break
        key = (node.type, node.id)
        if key in seen:
            break
        seen.add(key)
        if node.profile_id is not None:
            p = profiles.get(node.profile_id)
            data = p.data if p is not None and p.kind == kind else None
            return Resolution(kind, "profile", node, node.profile_id, p, data)
        if node.creds is not None:
            return Resolution(kind, "manual", node, stored=node.creds)
    return Resolution(kind, None, None)


def effective(res: Resolution) -> Any:
    """The value consumers use: SNMP a plaintext credential object (built-in default as the
    fallback), SSH the stored [3] object or None."""
    return KINDS[res.kind].effective(res)


def origin(kind: str, chain: list[ChainNode], profiles: Mapping[int, Profile]) -> dict[str, Any]:
    """api.md `CredentialOrigin` of the first node of `chain`."""
    res = resolve(kind, chain, profiles)
    return origin_of(res, chain[0] if chain else None)


def origin_of(res: Resolution, first: ChainNode | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "source": KINDS[res.kind].fallback,
        "group_id": None,
        "group_name": None,
        "mode": res.mode,
        "profile_id": None,
        "profile_name": None,
    }
    if res.node is not None:
        if first is not None and (res.node.type, res.node.id) == (first.type, first.id):
            out["source"] = "self"
        else:
            out["source"] = "group"
            out["group_id"] = res.node.id
            out["group_name"] = res.node.name
    if res.mode == "profile":
        out["profile_id"] = res.profile_id
        out["profile_name"] = res.profile.name if res.profile is not None else None
    return out


def node_mode(creds: Any, profile_id: int | None) -> str:
    """A node's own mode: an assignment makes it `profile` (its column, if any, is dormant
    manual data), else `manual` when the column is set, else `inherit`."""
    if profile_id is not None:
        return "profile"
    if creds is not None:
        return "manual"
    return "inherit"


def has_saved_manual(creds: Any, profile_id: int | None) -> bool:
    """API `<kind>_has_saved_manual`: dormant manual data behind a profile reference."""
    return profile_id is not None and creds is not None


class ModeRequestError(ValueError):
    pass


DECISIONS = ("unchanged", "inherit", "profile", "profile_clear", "manual", "manual_restore")


def decide_mode(
    fields: Iterable[str],
    kind: str,
    credentials: Any,
    profile_id: int | None,
    current: str,
    create: bool,
    has_saved: bool = False,
) -> str:
    """Pure: the request table of api.md "[profiles] Groups and devices" for kind K.
    `fields` = the keys present in the request body, `current` the node's mode, `has_saved`
    its dormant manual data. Returns one of DECISIONS:

    - `profile`        K_profile_id: <id> (K_credentials absent): reference set, a node's
                       manual data is kept dormant;
    - `profile_clear`  K_profile_id: <id> with K_credentials: null: reference set and the
                       node's manual / dormant data discarded;
    - `manual`         K_credentials: {...}: manual (merged over the stored manual data when
                       the node is manual; a new object from profile / inherit);
    - `inherit`        K_credentials: null: no reference, no manual / dormant data;
    - `manual_restore` K_profile_id: null alone on a profile node with dormant data;
                       on a profile node without it -> `inherit`; else `unchanged`;
    - `unchanged`      neither field on PATCH (POST: `inherit`).

    Raises ModeRequestError for K_profile_id: <id> with a non-null K_credentials."""
    fields = set(fields)
    has_creds = f"{kind}_credentials" in fields
    has_pid = f"{kind}_profile_id" in fields
    if has_pid and profile_id is not None:
        if has_creds and credentials is not None:
            raise ModeRequestError(
                f"{kind}_profile_id and a non-null {kind}_credentials contradict each other"
            )
        return "profile_clear" if has_creds else "profile"
    if has_creds:
        return "manual" if credentials is not None else "inherit"
    if has_pid:  # K_profile_id: null only = leave the profile
        if current != "profile":
            return "unchanged"
        return "manual_restore" if has_saved else "inherit"
    return "inherit" if create else "unchanged"


# ====================================================================== DB: loaders
def _profile(row: Any) -> Profile:
    return Profile(
        id=int(row.id),
        kind=str(row.kind),
        name=str(row.name),
        data=row.data,
        description=str(getattr(row, "description", "") or ""),
    )


async def load_profiles(
    session: AsyncSession, ids: Iterable[int] | None = None, kind: str | None = None
) -> dict[int, Profile]:
    """One query. `ids` None = all profiles."""
    where: list[str] = []
    params: dict[str, Any] = {}
    if ids is not None:
        id_list = sorted({int(i) for i in ids})
        if not id_list:
            return {}
        where.append("id = ANY(:ids)")
        params["ids"] = id_list
    if kind is not None:
        where.append("kind = :kind")
        params["kind"] = kind
    sql = "SELECT id, kind, name, description, data FROM connection_profiles"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return {int(r.id): _profile(r) for r in await session.execute(text(sql), params)}


async def load_assignments(session: AsyncSession) -> dict[tuple[str, str, int], int]:
    """One query: (kind, "group" | "device", node id) -> profile id."""
    out: dict[tuple[str, str, int], int] = {}
    for r in await session.execute(
        text("SELECT kind, group_id, device_id, profile_id FROM connection_profile_assignments")
    ):
        if r.group_id is not None:
            out[(r.kind, "group", int(r.group_id))] = int(r.profile_id)
        else:
            out[(r.kind, "device", int(r.device_id))] = int(r.profile_id)
    return out


async def db_chain(
    session: AsyncSession,
    kind: str,
    group_id: int | None,
    device: tuple[int, Any] | None = None,
) -> tuple[list[ChainNode], dict[int, Profile]]:
    """The chain from the database: the device (id, its stored column value) when given,
    then `group_id` and its ancestors (at most MAX_GROUP_DEPTH), each with its assignment.
    Two queries at most (the chain, the referenced profiles)."""
    col = KINDS[kind].column
    nodes: list[ChainNode] = []
    params: dict[str, Any] = {"gid": group_id, "kind": kind, "did": device[0] if device else None}
    rows = (
        await session.execute(
            text(
                f"""
                WITH RECURSIVE chain(id, parent_id, name, creds, depth) AS (
                    SELECT id, parent_id, name, {col}, 0 FROM groups WHERE id = :gid
                    UNION ALL
                    SELECT g.id, g.parent_id, g.name, g.{col}, c.depth + 1
                    FROM groups g JOIN chain c ON g.id = c.parent_id
                    WHERE c.depth < {MAX_GROUP_DEPTH - 1}
                )
                SELECT 'device' AS type, CAST(:did AS integer) AS id, NULL AS name,
                       NULL::jsonb AS creds, a.profile_id, -1 AS depth
                FROM (SELECT 1) one
                LEFT JOIN connection_profile_assignments a
                       ON a.device_id = CAST(:did AS integer) AND a.kind = :kind
                WHERE CAST(:did AS integer) IS NOT NULL
                UNION ALL
                SELECT 'group', c.id, c.name, c.creds, a.profile_id, c.depth FROM chain c
                LEFT JOIN connection_profile_assignments a
                       ON a.group_id = c.id AND a.kind = :kind
                ORDER BY depth
                """  # noqa: S608 - the column comes from the registry
            ),
            params,
        )
    ).all()
    for r in rows:
        if r.type == "device":
            assert device is not None
            nodes.append(ChainNode("device", int(device[0]), device[1], r.profile_id))
        else:
            nodes.append(ChainNode("group", int(r.id), r.creds, r.profile_id, r.name))
    pids = {n.profile_id for n in nodes if n.profile_id is not None}
    profiles = await load_profiles(session, pids) if pids else {}
    return nodes, profiles


async def resolve_device(session: AsyncSession, kind: str, device: Any) -> Resolution:
    """`device`: an ORM Device (or any object with id, group_id and the kind's column)."""
    col = KINDS[kind].column
    chain, profiles = await db_chain(
        session, kind, device.group_id, (int(device.id), getattr(device, col, None))
    )
    return resolve(kind, chain, profiles)


async def resolve_group(session: AsyncSession, kind: str, group_id: int | None) -> Resolution:
    if group_id is None:
        return Resolution(kind, None, None)
    chain, profiles = await db_chain(session, kind, group_id)
    return resolve(kind, chain, profiles)


async def node_state(session: AsyncSession, node_type: str, node_id: int) -> dict[str, Any]:
    """{kind: (profile_id, profile_name)} of one node's own references (one query)."""
    col = "group_id" if node_type == "group" else "device_id"
    out: dict[str, Any] = {}
    for r in await session.execute(
        text(
            "SELECT a.kind, a.profile_id, p.name FROM connection_profile_assignments a "
            f"JOIN connection_profiles p ON p.id = a.profile_id WHERE a.{col} = :i"  # noqa: S608
        ),
        {"i": node_id},
    ):
        out[str(r.kind)] = (int(r.profile_id), str(r.name))
    return out


def audit_modes(node: Any, state: Mapping[str, Any]) -> dict[str, Any]:
    """Audit keys `<kind>_mode` / `<kind>_profile` (profile by name, null = none)."""
    out: dict[str, Any] = {}
    for kind, sp in KINDS.items():
        pid, pname = state.get(kind, (None, None))
        creds = getattr(node, sp.column, None)
        mode = node_mode(creds, pid)
        out[f"{kind}_mode"] = mode
        out[f"{kind}_profile"] = pname if mode == "profile" else None
    return out


# ====================================================================== DB: assignments
async def lock_profile_for_use(
    session: AsyncSession, kind: str, profile_id: int, loc: list[str | int]
) -> Profile:
    """Validate a profile reference (exists, kind) and take `FOR SHARE` on its row, so a
    concurrent DELETE (FOR UPDATE + count) cannot slip between the check and the insert."""
    from netmon.api.errors import ApiError

    row = (
        await session.execute(
            text(
                "SELECT id, kind, name, description, data FROM connection_profiles "
                "WHERE id = :i FOR SHARE"
            ),
            {"i": profile_id},
        )
    ).first()
    if row is None:
        raise ApiError(
            422, "validation_error", "unknown profile", [{"loc": loc, "msg": "unknown profile"}]
        )
    if row.kind != kind:
        raise ApiError(
            422,
            "profile_kind_mismatch",
            f"The profile is of kind {row.kind}, expected {kind}",
            [{"loc": loc, "expected": kind, "actual": row.kind}],
        )
    return _profile(row)


async def assign(
    session: AsyncSession, kind: str, node_type: str, node_id: int, profile_id: int
) -> None:
    """Set the node's reference for `kind` (the caller validated it with
    `lock_profile_for_use` and clears the node column)."""
    col = "group_id" if node_type == "group" else "device_id"
    constraint = "ux_cpa_group" if node_type == "group" else "ux_cpa_device"
    await session.execute(
        text(
            f"INSERT INTO connection_profile_assignments (kind, profile_id, {col}) "  # noqa: S608
            f"VALUES (:k, :p, :n) ON CONFLICT ON CONSTRAINT {constraint} "
            "DO UPDATE SET profile_id = EXCLUDED.profile_id, created_at = now()"
        ),
        {"k": kind, "p": profile_id, "n": node_id},
    )


async def unassign(session: AsyncSession, kind: str, node_type: str, node_id: int) -> None:
    col = "group_id" if node_type == "group" else "device_id"
    await session.execute(
        text(
            f"DELETE FROM connection_profile_assignments WHERE {col} = :n AND kind = :k"  # noqa: S608
        ),
        {"k": kind, "n": node_id},
    )


async def apply_mode(
    session: AsyncSession,
    *,
    kind: str,
    node: Any,
    node_type: str,
    decision: str,
    profile_id: int | None,
    current_profile_id: int | None,
    manual_value: Callable[[], Any],
) -> bool:
    """Carry out a `decide_mode` decision on an ORM group / device (same transaction).
    `manual_value()` builds the new stored column value for `manual` (it may raise 422).
    Returns True when the effective configuration of the node may have changed."""
    col = KINDS[kind].column
    if decision == "unchanged":
        return False
    if decision in ("profile", "profile_clear"):
        assert profile_id is not None
        await lock_profile_for_use(session, kind, profile_id, ["body", f"{kind}_profile_id"])
        changed = current_profile_id != profile_id
        if decision == "profile_clear" and getattr(node, col) is not None:
            setattr(node, col, None)  # discard the manual / dormant data (audited)
        await assign(session, kind, node_type, int(node.id), profile_id)
        return changed
    if decision == "manual":
        new = manual_value()
        changed = current_profile_id is not None or new != getattr(node, col)
        if current_profile_id is not None:
            await unassign(session, kind, node_type, int(node.id))
        if new != getattr(node, col):
            setattr(node, col, new)
        return changed
    if decision == "manual_restore":  # the dormant manual data becomes active again
        if current_profile_id is not None:
            await unassign(session, kind, node_type, int(node.id))
        return current_profile_id is not None
    # inherit
    changed = current_profile_id is not None or getattr(node, col) is not None
    if current_profile_id is not None:
        await unassign(session, kind, node_type, int(node.id))
    if getattr(node, col) is not None:
        setattr(node, col, None)
    return changed


# ====================================================================== DB: usage / delete
DISCOVERY_ID_KEYS = ("snmp_profile_ids", "ssh_profile_ids")
_DP_WITH_IDS = (
    "jsonb_typeof(options -> 'snmp_profile_ids') = 'array' "
    "OR jsonb_typeof(options -> 'ssh_profile_ids') = 'array'"
)


def discovery_profile_ids(options: Any) -> list[int]:
    """Connection profile ids a saved discovery profile's options list (SNMP and SSH)."""
    if not isinstance(options, dict):
        return []
    out = []
    for key in DISCOVERY_ID_KEYS:
        ids = options.get(key)
        if not isinstance(ids, list):
            continue
        for i in ids:
            try:
                out.append(int(i))
            except (TypeError, ValueError):
                continue
    return out


async def direct_counts(session: AsyncSession) -> dict[int, dict[str, int]]:
    """profile id -> {groups, devices, discovery_profiles} (direct references)."""
    out: dict[int, dict[str, int]] = {}

    def slot(pid: int) -> dict[str, int]:
        return out.setdefault(pid, {"groups": 0, "devices": 0, "discovery_profiles": 0})

    for r in await session.execute(
        text(
            "SELECT profile_id, count(group_id) AS g, count(device_id) AS d "
            "FROM connection_profile_assignments GROUP BY profile_id"
        )
    ):
        s = slot(int(r.profile_id))
        s["groups"] = int(r.g)
        s["devices"] = int(r.d)
    for r in await session.execute(
        text(f"SELECT options FROM discovery_profiles WHERE {_DP_WITH_IDS}")  # noqa: S608
    ):
        for pid in set(discovery_profile_ids(r.options)):
            slot(pid)["discovery_profiles"] += 1
    return out


def effective_device_counts(inv: Any) -> dict[int, int]:
    """profile id -> devices whose effective credentials come from it (direct or inherited),
    from a preloaded `services.status.Inventory`."""
    out: dict[int, int] = {}
    for did in inv.devices:
        for kind in KINDS:
            res = inv.resolution(kind, device_id=did)
            if res.mode == "profile" and res.profile_id is not None:
                out[res.profile_id] = out.get(res.profile_id, 0) + 1
    return out


def usage_out(
    profile_id: int, direct: Mapping[int, Mapping[str, int]], eff: Mapping[int, int]
) -> dict[str, int]:
    d = direct.get(profile_id, {})
    return {
        "groups": int(d.get("groups", 0)),
        "devices": int(d.get("devices", 0)),
        "discovery_profiles": int(d.get("discovery_profiles", 0)),
        "devices_effective": int(eff.get(profile_id, 0)),
    }


async def usage(session: AsyncSession, profile_id: int) -> dict[str, Any]:
    """api.md `ProfileUsage` ("used by")."""
    from netmon.services.status import load_inventory

    inv = await load_inventory(session)
    groups_out: list[dict[str, Any]] = []
    devices_out: list[dict[str, Any]] = []
    truncated = False
    group_ids = sorted(
        nid for (k, t, nid), pid in inv.assignments.items() if pid == profile_id and t == "group"
    )
    device_ids = sorted(
        nid for (k, t, nid), pid in inv.assignments.items() if pid == profile_id and t == "device"
    )
    for gid in group_ids:
        if len(groups_out) >= USAGE_LIST_LIMIT:
            truncated = True
            break
        if gid not in inv.groups:
            continue
        path = [str(inv.groups[a]["name"]) for a in reversed(inv.ancestors(gid))]
        groups_out.append({"id": gid, "name": inv.groups[gid]["name"], "path": path})
    for did in device_ids:
        if len(devices_out) >= USAGE_LIST_LIMIT:
            truncated = True
            break
        d = inv.devices.get(did)
        if d is None:
            continue
        g = inv.groups.get(d["group_id"], {})
        devices_out.append(
            {
                "id": did,
                "name": d["name"],
                "host": d["host"],
                "group_id": d["group_id"],
                "group_name": g.get("name"),
            }
        )
    dps: list[dict[str, Any]] = []
    for r in await session.execute(
        text(
            f"SELECT id, name, options FROM discovery_profiles WHERE {_DP_WITH_IDS} "  # noqa: S608
            "ORDER BY lower(name), id"
        )
    ):
        if profile_id in discovery_profile_ids(r.options):
            if len(dps) >= USAGE_LIST_LIMIT:
                truncated = True
                break
            dps.append({"id": int(r.id), "name": r.name})
    return {
        "groups": groups_out,
        "devices": devices_out,
        "discovery_profiles": dps,
        "devices_effective": effective_device_counts(inv).get(profile_id, 0),
        "truncated": truncated,
    }


async def lock_for_delete(session: AsyncSession, profile_id: int) -> tuple[Any, dict[str, int]]:
    """`SELECT ... FOR UPDATE` on the profile, then count its uses. Returns (row, counts);
    row None = not found."""
    row = (
        await session.execute(
            text(
                "SELECT id, kind, name, description, data FROM connection_profiles "
                "WHERE id = :i FOR UPDATE"
            ),
            {"i": profile_id},
        )
    ).first()
    if row is None:
        return None, {}
    a = (
        await session.execute(
            text(
                "SELECT count(group_id) AS g, count(device_id) AS d "
                "FROM connection_profile_assignments WHERE profile_id = :i"
            ),
            {"i": profile_id},
        )
    ).one()
    dp = (
        await session.execute(
            text(
                "SELECT count(*) FROM discovery_profiles "
                "WHERE options -> 'snmp_profile_ids' @> CAST(:j AS jsonb) "
                "OR options -> 'ssh_profile_ids' @> CAST(:j AS jsonb)"
            ),
            {"j": json.dumps([profile_id])},
        )
    ).scalar_one()
    return row, {"groups": int(a.g), "devices": int(a.d), "discovery_profiles": int(dp)}


async def lock_discovery_ids(
    session: AsyncSession, ids: list[int], base_loc: list[Any], kind: str = "snmp"
) -> None:
    """Discovery options `snmp_profile_ids` / `ssh_profile_ids`: each id exists and is of
    `kind`; `FOR SHARE` on every row (a saved discovery profile adds uses)."""
    from netmon.api.errors import ApiError

    if not ids:
        return
    rows = {
        int(r.id): r.kind
        for r in await session.execute(
            text("SELECT id, kind FROM connection_profiles WHERE id = ANY(:ids) FOR SHARE"),
            {"ids": list(ids)},
        )
    }
    for i, pid in enumerate(ids):
        actual = rows.get(pid)
        if actual is None:
            raise ApiError(
                422,
                "validation_error",
                "unknown profile",
                [{"loc": [*base_loc, i], "msg": "unknown profile"}],
            )
        if actual != kind:
            raise ApiError(
                422,
                "profile_kind_mismatch",
                f"The profile is of kind {actual}, expected {kind}",
                [{"loc": [*base_loc, i], "expected": kind, "actual": actual}],
            )


# ====================================================================== copy into manual
def copy_value(kind: str, profile: Profile, node_type: str, node_id: int, secret_key: str) -> Any:
    """The node column value of a manual copy of `profile` (secrets re-encrypted for the
    node). Raises ValueError when a secret of the profile cannot be decrypted."""
    sp = KINDS[kind]
    if sp.unreadable(profile.data, profile.id, secret_key):
        raise ValueError("unreadable")
    return sp.to_node(profile.data, profile.id, node_type, node_id)


# ====================================================================== API shapes
def profile_out(p: Any, usage_counts: Mapping[str, int] | None = None) -> dict[str, Any]:
    """api.md `ConnectionProfile` (secrets never returned)."""
    from netmon.services.serialize import iso

    sp = KINDS.get(p.kind)
    return {
        "id": p.id,
        "kind": p.kind,
        "name": p.name,
        "description": p.description or "",
        "credentials": sp.out(p.data, p.id) if sp is not None else None,
        "usage": dict(
            usage_counts
            or {"groups": 0, "devices": 0, "discovery_profiles": 0, "devices_effective": 0}
        ),
        "created_at": iso(getattr(p, "created_at", None)),
        "updated_at": iso(getattr(p, "updated_at", None)),
    }


def validate_credentials(kind: str, raw: Any, loc: tuple[str, ...]) -> Any:
    """The API object of `kind` from a request dict -> its input model; pydantic errors are
    422 `validation_error` at `loc`."""
    from pydantic import ValidationError

    from netmon.api.errors import ApiError

    model = KINDS[kind].input_model()
    if not isinstance(raw, dict):
        raise ApiError(
            422,
            "validation_error",
            "Request validation failed",
            [{"loc": list(loc), "msg": "must be an object"}],
        )
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        details = []
        for err in exc.errors():
            msg = str(err.get("msg", "invalid"))
            for prefix in ("Value error, ", "Assertion failed, "):
                if msg.startswith(prefix):
                    msg = msg[len(prefix) :]
            details.append({"loc": [*loc, *err.get("loc", ())], "msg": msg})
        raise ApiError(422, "validation_error", "Request validation failed", details) from None
