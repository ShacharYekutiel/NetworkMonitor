"""[severity] Alert severity (data-model "[severity] Migration 0015").

- `sensors.severity`: the current problem's resolved severity (NULL = no problem, or the
  feature is OFF); `sensors.severity_down` / `severity_warning`: the optional per-sensor
  override for the down / warning state.
- `events.severity`: resolved when the status_change event is written (history, never
  recomputed); also carried by `notification_sent` events.
- `event_rules.severity`: optional severity of a trap / syslog rule (warning / down actions).
- `alert_rules.severities`: the severities a notification rule applies to (`{}` = all, the
  behaviour before 0015).
- `alert_state.severity`: the notified problem's severity (payloads of escalation, reminder,
  recovery).

Existing rows get NULL / `{}` (no backfill guessing). Active problems are resolved once by the
application when the feature is turned on. Plain DDL, no hypertable (`events` is a plain table
in both modes): identical in TimescaleDB and plain PostgreSQL mode. Idempotent.
`downgrade()` drops the columns (the stored severities and rule filters are lost).

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-22
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

LEVELS = ("critical", "major", "minor", "warning", "info")
_IN = ", ".join(f"'{lvl}'" for lvl in LEVELS)
_ARRAY = f"ARRAY[{_IN}]::text[]"


def _column(table: str, column: str) -> list[str]:
    return [
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} text NULL",
        f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_{column}_check",
        f"ALTER TABLE {table} ADD CONSTRAINT {table}_{column}_check CHECK ({column} IN ({_IN}))",
    ]


UPGRADE = [
    *_column("sensors", "severity"),
    *_column("sensors", "severity_down"),
    *_column("sensors", "severity_warning"),
    *_column("events", "severity"),
    *_column("event_rules", "severity"),
    *_column("alert_state", "severity"),
    "ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS severities text[] NOT NULL DEFAULT '{}'",
    "ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_severities_check",
    f"ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_severities_check "
    f"CHECK (severities <@ {_ARRAY})",
    "CREATE INDEX IF NOT EXISTS ix_events_severity_time ON events (severity, time DESC) "
    "WHERE severity IS NOT NULL",
]

DOWNGRADE = [
    "DROP INDEX IF EXISTS ix_events_severity_time",
    "ALTER TABLE alert_rules DROP COLUMN IF EXISTS severities",
    "ALTER TABLE alert_state DROP COLUMN IF EXISTS severity",
    "ALTER TABLE event_rules DROP COLUMN IF EXISTS severity",
    "ALTER TABLE events DROP COLUMN IF EXISTS severity",
    "ALTER TABLE sensors DROP COLUMN IF EXISTS severity_warning",
    "ALTER TABLE sensors DROP COLUMN IF EXISTS severity_down",
    "ALTER TABLE sensors DROP COLUMN IF EXISTS severity",
    "DELETE FROM settings WHERE key = 'severity_mapping'",
    "DELETE FROM settings WHERE key = 'severity_in_subject'",
    "DELETE FROM settings WHERE key = 'feature_alert_severity_enabled'",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
