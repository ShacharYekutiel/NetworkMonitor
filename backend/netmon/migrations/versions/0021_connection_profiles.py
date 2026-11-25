"""[profiles] Connection profiles for SNMP and SSH credentials (release 1.7.0).

- `connection_profiles`: named, globally defined credential sets of one kind (`snmp`, `ssh`;
  the kind is checked by format here and by the code registry `conn_profiles.KINDS`).
- `connection_profile_assignments`: one row per (group or device, kind) in mode `profile`.
  The composite FK `(profile_id, kind) -> connection_profiles (id, kind)` makes a kind
  mismatch impossible and (RESTRICT) keeps a referenced profile from being deleted.
- `discovery_results.snmp_profile_id` (no FK: a snapshot; the profile may be deleted later).
- Opt-in SSH login check of discovery (owner decision Q3): `discovery_results.ssh_attempted`,
  `ssh_ok`, `ssh_credential`, `ssh_profile_id` and `discovery_scans.ssh_candidates`.

**No data is changed** by the upgrade: every existing group / device stays `manual`
(credentials set) or `inherit` (NULL), so every effective credential is byte-identical.

Plain DDL, identical in TimescaleDB and plain PostgreSQL mode (no hypertable is touched),
idempotent (`IF NOT EXISTS`, constraints dropped and re-added).

`downgrade()` materialises every profile reference into the node's own column (SNMP re-sealed
for the node owner in the current secrets form, SSH copied as is): the profile's data is the
node's effective value, so it overwrites dormant manual data kept behind the reference. The
node keeps working credentials as `manual` in the older release. Then it removes
`snmp_profile_ids`, `ssh_profile_ids` and `ssh_credentials` from the discovery options (the 1.6
options model rejects unknown keys) and drops the new columns and both
tables. A profile secret that cannot be decrypted stops the downgrade with an error and
nothing is changed. `secret.key` is needed only when an assignment exists.

Revision ID: 0021 (data-model "[profiles] Migration (`0021`)").
Revises: 0018 -- MERGE NOTE: when release 1.6.0 (0019, 0020) lands on main this becomes "0020".
Create Date: 2026-11-25
"""

from __future__ import annotations

import json
import logging
from typing import Any

from alembic import op

revision = "0021"
down_revision = "0018"
branch_labels = None
depends_on = None

log = logging.getLogger("netmon.migrations")

UPGRADE = [
    """
    CREATE TABLE IF NOT EXISTS connection_profiles (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        kind text NOT NULL,
        name varchar(128) NOT NULL,
        description text NOT NULL DEFAULT '',
        data jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    """
    CREATE TABLE IF NOT EXISTS connection_profile_assignments (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        kind text NOT NULL,
        profile_id integer NOT NULL,
        group_id integer NULL,
        device_id integer NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    # constraints dropped (dependants first) and re-added: idempotent
    "ALTER TABLE connection_profile_assignments DROP CONSTRAINT IF EXISTS fk_cpa_profile_kind",
    "ALTER TABLE connection_profile_assignments DROP CONSTRAINT IF EXISTS fk_cpa_group",
    "ALTER TABLE connection_profile_assignments DROP CONSTRAINT IF EXISTS fk_cpa_device",
    "ALTER TABLE connection_profile_assignments DROP CONSTRAINT IF EXISTS ck_cpa_one_owner",
    "ALTER TABLE connection_profile_assignments DROP CONSTRAINT IF EXISTS ux_cpa_group",
    "ALTER TABLE connection_profile_assignments DROP CONSTRAINT IF EXISTS ux_cpa_device",
    "ALTER TABLE connection_profiles DROP CONSTRAINT IF EXISTS uq_connection_profiles_id_kind",
    "ALTER TABLE connection_profiles DROP CONSTRAINT IF EXISTS ck_connection_profiles_kind",
    "ALTER TABLE connection_profiles ADD CONSTRAINT ck_connection_profiles_kind "
    "CHECK (kind ~ '^[a-z][a-z0-9_]{1,31}$')",
    "ALTER TABLE connection_profiles ADD CONSTRAINT uq_connection_profiles_id_kind "
    "UNIQUE (id, kind)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_connection_profiles_name "
    "ON connection_profiles (kind, lower(name))",
    "ALTER TABLE connection_profile_assignments ADD CONSTRAINT ck_cpa_one_owner "
    "CHECK (num_nonnulls(group_id, device_id) = 1)",
    "ALTER TABLE connection_profile_assignments ADD CONSTRAINT fk_cpa_profile_kind "
    "FOREIGN KEY (profile_id, kind) REFERENCES connection_profiles (id, kind) "
    "ON DELETE RESTRICT",
    "ALTER TABLE connection_profile_assignments ADD CONSTRAINT fk_cpa_group "
    "FOREIGN KEY (group_id) REFERENCES groups (id) ON DELETE CASCADE",
    "ALTER TABLE connection_profile_assignments ADD CONSTRAINT fk_cpa_device "
    "FOREIGN KEY (device_id) REFERENCES devices (id) ON DELETE CASCADE",
    "ALTER TABLE connection_profile_assignments ADD CONSTRAINT ux_cpa_group "
    "UNIQUE (group_id, kind)",
    "ALTER TABLE connection_profile_assignments ADD CONSTRAINT ux_cpa_device "
    "UNIQUE (device_id, kind)",
    "CREATE INDEX IF NOT EXISTS ix_cpa_profile ON connection_profile_assignments (profile_id)",
    "ALTER TABLE discovery_results ADD COLUMN IF NOT EXISTS snmp_profile_id integer NULL",
    # [profiles] opt-in SSH login check of discovery (owner decision Q3)
    "ALTER TABLE discovery_results ADD COLUMN IF NOT EXISTS ssh_attempted boolean NOT NULL "
    "DEFAULT false",
    "ALTER TABLE discovery_results ADD COLUMN IF NOT EXISTS ssh_ok boolean NOT NULL DEFAULT false",
    "ALTER TABLE discovery_results ADD COLUMN IF NOT EXISTS ssh_credential text NULL",
    "ALTER TABLE discovery_results ADD COLUMN IF NOT EXISTS ssh_profile_id integer NULL",
    "ALTER TABLE discovery_scans ADD COLUMN IF NOT EXISTS ssh_candidates jsonb NOT NULL "
    "DEFAULT '[]'",
]

DROP = [
    "ALTER TABLE discovery_scans DROP COLUMN IF EXISTS ssh_candidates",
    "ALTER TABLE discovery_results DROP COLUMN IF EXISTS ssh_profile_id",
    "ALTER TABLE discovery_results DROP COLUMN IF EXISTS ssh_credential",
    "ALTER TABLE discovery_results DROP COLUMN IF EXISTS ssh_ok",
    "ALTER TABLE discovery_results DROP COLUMN IF EXISTS ssh_attempted",
    "ALTER TABLE discovery_results DROP COLUMN IF EXISTS snmp_profile_id",
    "DROP TABLE IF EXISTS connection_profile_assignments",
    "DROP TABLE IF EXISTS connection_profiles",
]

# kind -> the node column of its manual mode (frozen for this migration)
NODE_COLUMNS = {"snmp": "snmp_credentials", "ssh": "ssh_credentials"}
SNMP_FIELDS = ("community", "v3_auth_key", "v3_priv_key")
SSH_FIELDS = ("password", "private_key", "passphrase", "enable_password")


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def _secret_key() -> str:
    config = op.get_context().config
    key = config.attributes.get("secret_key") if config is not None else None
    if key:
        return str(key)
    from netmon.config import load_settings

    path = load_settings().data_dir / "secret.key"
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    raise RuntimeError(f"migration 0021 downgrade needs the server's secret.key ({path} not found)")


def materialise(conn: Any, key_source: Any) -> int:
    """Copy every profile reference into the node's own column. The profile decides at a
    node that has one, so its data replaces dormant manual data kept in the column. All
    profile data is checked first, so an unreadable secret raises before anything is
    written. Returns the rows written."""
    from sqlalchemy import text

    from netmon.services import crypto, secretbox

    rows = conn.execute(
        text(
            "SELECT a.kind, a.group_id, a.device_id, p.id AS profile_id, p.data "
            "FROM connection_profile_assignments a "
            "JOIN connection_profiles p ON p.id = a.profile_id ORDER BY a.id"
        )
    ).all()
    if not rows:
        return 0
    key = key_source() if callable(key_source) else str(key_source)
    removed = conn.execute(
        text("SELECT value FROM settings WHERE key = :k"), {"k": secretbox.STATE_KEY}
    ).scalar()
    keep_plaintext = removed is not True
    planned: list[tuple[str, str, int, Any]] = []
    for r in rows:
        column = NODE_COLUMNS.get(r.kind)
        if column is None:
            raise RuntimeError(f"connection profile {r.profile_id}: unknown kind {r.kind!r}")
        table, node_id, owner_kind = (
            ("groups", r.group_id, "group")
            if r.group_id is not None
            else ("devices", r.device_id, "device")
        )
        data = r.data if isinstance(r.data, dict) else {}
        if r.kind == "snmp":
            plain, unreadable = secretbox.open_with(
                key, data, SNMP_FIELDS, f"conn_profile:{r.profile_id}"
            )
            if unreadable:
                raise RuntimeError(
                    f"connection profile {r.profile_id}: a secret cannot be decrypted with this "
                    "secret.key; nothing was changed"
                )
            value = secretbox.seal_with(
                key, plain, SNMP_FIELDS, f"{owner_kind}:{node_id}", keep_plaintext
            )
        else:
            for f in SSH_FIELDS:
                enc = data.get(f"{f}_enc")
                if enc:
                    try:
                        crypto.decrypt(key, enc, f"ssh.{f}".encode())
                    except crypto.DecryptError:
                        raise RuntimeError(
                            f"connection profile {r.profile_id}: a secret cannot be decrypted "
                            "with this secret.key; nothing was changed"
                        ) from None
            value = dict(data)
        planned.append((table, column, int(node_id), value))
    n = 0
    for table, column, node_id, value in planned:
        res = conn.execute(
            text(
                f"UPDATE {table} SET {column} = CAST(:v AS jsonb) WHERE id = :i"  # noqa: S608
            ),
            {"v": json.dumps(value), "i": node_id},
        )
        n += res.rowcount or 0
    return n


def downgrade() -> None:
    from sqlalchemy import text

    bind = op.get_bind()
    exists = bind.execute(
        text("SELECT to_regclass('public.connection_profile_assignments') IS NOT NULL")
    ).scalar()
    if exists:
        n = materialise(bind, _secret_key)
        log.info("Migration 0021 downgrade: %d profile reference(s) copied into nodes", n)
    for table in ("discovery_profiles", "discovery_scans"):
        op.execute(
            f"UPDATE {table} SET options = options - "  # noqa: S608
            "ARRAY['snmp_profile_ids', 'ssh_profile_ids', 'ssh_credentials'] "
            "WHERE options ?| ARRAY['snmp_profile_ids', 'ssh_profile_ids', 'ssh_credentials']"
        )
    for stmt in DROP:
        op.execute(stmt)
