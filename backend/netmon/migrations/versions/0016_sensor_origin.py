"""[template-switch] Where a sensor came from.

- `sensors.origin` (text, NULL): `manual` (created in the sensor form / `POST /api/sensors`),
  `auto` (the default sensors of `POST /api/devices`, automatic event sensors) or
  `template:<id>` (created by applying that template, also by the discovery import). NULL =
  created before this migration (origin unknown). Read by the apply dialog to offer the
  sensors a newly applied template replaces (`GET /api/devices/{id}/apply-template/replaces`).

Plain DDL, no hypertable: identical in TimescaleDB and plain PostgreSQL mode. Idempotent.
`downgrade()` drops the column (nothing else reads it).

Revision ID: 0016 (data-model "[template-switch] Migration").
Revises: 0015
Create Date: 2026-11-22
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

UPGRADE = ["ALTER TABLE sensors ADD COLUMN IF NOT EXISTS origin text NULL"]

DOWNGRADE = ["ALTER TABLE sensors DROP COLUMN IF EXISTS origin"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
