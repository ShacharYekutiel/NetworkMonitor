"""[premium] Connected device kind per switch port.

- `port_kind_overrides`: the kind a user chose for what is connected to a switch port
  (`PUT /api/devices/{id}/ports/{if_index}/connected-kind`), keyed by device and ifIndex.
- `topology_neighbors.capabilities`: the LLDP / CDP system capabilities of an unmanaged
  neighbour (parsed from the lldpRemTable / cdpCacheTable walks the topology refresh already
  does; no new SNMP request).

Plain DDL, no hypertable: identical in TimescaleDB and plain PostgreSQL mode. Idempotent.
`downgrade()` drops both (the overrides are lost; 1.4.2 has no use for them).

Revision ID: 0013 (data-model "[premium] Migration"). Renumbered at merge time if another
0013 lands first: the id lives here only (tests read the head from the script directory).
Revises: 0012
Create Date: 2026-11-16
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

KINDS = ("computer", "phone", "ap", "printer", "camera", "server", "switch", "router", "unknown")

UPGRADE = [
    f"""
    CREATE TABLE IF NOT EXISTS port_kind_overrides (
        device_id integer NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        if_index integer NOT NULL,
        kind text NOT NULL CHECK (kind IN ({", ".join(f"'{k}'" for k in KINDS)})),
        updated_at timestamptz NOT NULL DEFAULT now(),
        updated_by integer NULL,
        PRIMARY KEY (device_id, if_index)
    )""",
    "ALTER TABLE topology_neighbors ADD COLUMN IF NOT EXISTS capabilities text[] NULL",
]

DOWNGRADE = [
    "ALTER TABLE topology_neighbors DROP COLUMN IF EXISTS capabilities",
    "DROP TABLE IF EXISTS port_kind_overrides",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
