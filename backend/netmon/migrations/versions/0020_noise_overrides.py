"""[if-noise] Per-sensor / per-device / per-group overrides of the sensor event noise protections.

On `groups`, `devices` and `sensors` (the same four columns on each):
- `noise_debounce` / `noise_flap` (text NOT NULL DEFAULT 'inherit', CHECK IN ('inherit', 'on',
  'off')): the debounce / flapping collapse of the event-log rows of polled sensors.
  `inherit` = the next level decides (sensor -> device -> group -> parent groups -> the global
  settings key).
- `noise_debounce_polls` (integer NULL, 1-10) / `noise_debounce_min_s` (integer NULL, 0-3600): the
  debounce values; NULL = inherit.

None of the three tables is a hypertable (TimescaleDB mode or not); a constant default and NULL
columns are catalog-only (no table rewrite). Idempotent. `downgrade()` drops the columns (every
sensor follows the global settings again).

Revision ID: 0020 (data-model "[if-noise] Overrides").
Revises: 0019
Create Date: 2026-11-25
"""

from __future__ import annotations

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

TABLES = ("groups", "devices", "sensors")


def _upgrade(table: str) -> list[str]:
    out = []
    for col in ("noise_debounce", "noise_flap"):
        out += [
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} text NOT NULL DEFAULT 'inherit'",
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_{col}_check",
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_{col}_check "
            f"CHECK ({col} IN ('inherit','on','off'))",
        ]
    for col, lo, hi in (("noise_debounce_polls", 1, 10), ("noise_debounce_min_s", 0, 3600)):
        out += [
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} integer NULL",
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_{col}_check",
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_{col}_check "
            f"CHECK ({col} IS NULL OR {col} BETWEEN {lo} AND {hi})",
        ]
    return out


UPGRADE = [stmt for t in TABLES for stmt in _upgrade(t)]
DOWNGRADE = [
    f"ALTER TABLE {t} DROP COLUMN IF EXISTS {c}"
    for t in TABLES
    for c in ("noise_debounce", "noise_flap", "noise_debounce_polls", "noise_debounce_min_s")
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
