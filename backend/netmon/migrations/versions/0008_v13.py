"""[1.3] Sensor impact on the device status and the LAG-degraded event condition source.

- `sensors.impact` text NOT NULL DEFAULT 'critical' CHECK in ('critical','warning','none')
  (existing rows become 'critical' = unchanged behaviour; the one-time startup backfill
  `netmon.services.impact.backfill` lowers interface sensors of unimportant ports to 'warning').
- `event_conditions.source` gains 'lag_degraded' (LAG degraded, built-in link-down rule).

Plain PostgreSQL DDL only (data-model "[1.3] Migration 0008").

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-11
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

OLD_COND_SOURCES = "'rule','flap','backup_failed','host_key','config_changed','new_mac'"
NEW_COND_SOURCES = OLD_COND_SOURCES + ",'lag_degraded'"


def _cond_check(sources: str) -> list[str]:
    return [
        "ALTER TABLE event_conditions DROP CONSTRAINT IF EXISTS event_conditions_source_check",
        "ALTER TABLE event_conditions ADD CONSTRAINT event_conditions_source_check "
        f"CHECK (source IN ({sources}))",
    ]


UPGRADE = [
    "ALTER TABLE sensors ADD COLUMN IF NOT EXISTS impact text NOT NULL DEFAULT 'critical'",
    "ALTER TABLE sensors DROP CONSTRAINT IF EXISTS sensors_impact_check",
    "ALTER TABLE sensors ADD CONSTRAINT sensors_impact_check "
    "CHECK (impact IN ('critical','warning','none'))",
    *_cond_check(NEW_COND_SOURCES),
]

DOWNGRADE = [
    "DELETE FROM event_conditions WHERE source = 'lag_degraded'",
    *_cond_check(OLD_COND_SOURCES),
    "ALTER TABLE sensors DROP CONSTRAINT IF EXISTS sensors_impact_check",
    "ALTER TABLE sensors DROP COLUMN IF EXISTS impact",
    # the one-time impact backfill runs again after a re-upgrade
    "DELETE FROM settings WHERE key = 'impact_backfill_version'",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
