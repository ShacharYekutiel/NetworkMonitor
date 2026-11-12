"""[1.3.1] IRF ports are important ports; the Comware hardware fault-code table.

- `switch_ports.irf_port` smallint NULL: the IRF port (1/2) an HPE Comware IRF physical port is
  bound to (HH3C-STACK-MIB hh3cStackPhyPortInfoBelongtoPort), NULL = not an IRF port. Filled by
  the next port inventory run; existing rows start NULL.
- `snmp_custom` hardware-status sensors created by the `hpe_comware` template
  (hh3cEntityExtErrorStatus, value map still exactly the template's `{"2": normal}` with default
  `down`, i.e. not edited by a user) switch to the shared fault-code table:
  `config.value_map_ref = 'hh3c_error_status'`, `config.value_map = {}`
  (texts in netmon.poller.value_maps, localized).

Plain PostgreSQL only (data-model "[1.3.1] Migration 0009").

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-12
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

ERROR_STATUS_OID = "1.3.6.1.4.1.25506.2.6.1.1.1.1.19"
OLD_MAP = '{"2": {"status": "up", "text": "normal"}}'

UPGRADE = [
    "ALTER TABLE switch_ports ADD COLUMN IF NOT EXISTS irf_port smallint NULL",
    "UPDATE sensors SET config = config || jsonb_build_object("
    "'value_map_ref', 'hh3c_error_status', 'value_map', '{}'::jsonb) "
    "WHERE type = 'snmp_custom' AND config->>'value_type' = 'enum' "
    f"AND config->>'oid' LIKE '{ERROR_STATUS_OID}.%' "
    f"AND config->'value_map' = '{OLD_MAP}'::jsonb "
    "AND config->>'value_map_default' = 'down' AND config->'value_map_ref' IS NULL",
]

DOWNGRADE = [
    # 1.3.0 knows no named maps: an empty map gets the template's map back
    "UPDATE sensors SET config = (config - 'value_map_ref') || CASE WHEN "
    "config->'value_map' = '{}'::jsonb OR config->'value_map' IS NULL "
    f"THEN jsonb_build_object('value_map', '{OLD_MAP}'::jsonb) ELSE '{{}}'::jsonb END "
    "WHERE type = 'snmp_custom' AND config->'value_map_ref' IS NOT NULL",
    "ALTER TABLE switch_ports DROP COLUMN IF EXISTS irf_port",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
