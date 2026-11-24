"""[if-noise] Interface sensor event noise.

- `events.noise` (text NULL: NULL = shown in the event log, `pending` = a status change still
  inside its debounce hold, `debounced` = a change that reverted within the hold, `flapping` = a
  change collapsed into a `sensor_flapping` row) and `events.noise_until` (timestamptz NULL: when
  a `pending` row is shown). The rows are always written (alerts, reports and availability still
  see every change); the event log hides the non-NULL ones by default.
- `events.flap_count` / `flap_last` / `flap_end` and the event kind `sensor_flapping`: one row
  per flapping episode of a polled sensor (count of changes, last change, settle time).
- `port_importance_overrides`: a user's explicit "important" / "not important" choice for a
  switch port (wins over the criteria; never sent to the device).
- Settings: an EXISTING install (the `settings` table already has rows) gets
  `important_port_criteria` written with the pre-upgrade meaning (`critical_sensor` included,
  so no link-down alert silently stops); a fresh install keeps the code default (without it).

`events` is a plain table in both modes (not a hypertable): plain DDL, constant/NULL defaults
are catalog-only. Idempotent. `downgrade()` deletes the `sensor_flapping` rows, drops the new
columns (hidden rows become visible to an older release), the overrides table and the settings
row `important_port_criteria` (a re-upgrade decides again).

Revision ID: 0019 (data-model "[if-noise] Migration").
Revises: 0018
Create Date: 2026-11-24
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

OLD_EVENT_KINDS = (
    "'status_change','notification','system','discovery','acknowledge','maintenance',"
    "'notification_sent','port_change','config_change','new_device'"
)
NEW_EVENT_KINDS = OLD_EVENT_KINDS + ",'sensor_flapping'"
LEGACY_CRITERIA = '["uplink", "lag", "trunk", "irf", "critical_sensor"]'

UPGRADE = [
    "ALTER TABLE events DROP CONSTRAINT IF EXISTS events_kind_check",
    f"ALTER TABLE events ADD CONSTRAINT events_kind_check CHECK (kind IN ({NEW_EVENT_KINDS}))",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS noise text NULL",
    "ALTER TABLE events DROP CONSTRAINT IF EXISTS events_noise_check",
    "ALTER TABLE events ADD CONSTRAINT events_noise_check "
    "CHECK (noise IN ('pending','debounced','flapping'))",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS noise_until timestamptz NULL",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS flap_count integer NULL",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS flap_last timestamptz NULL",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS flap_end timestamptz NULL",
    "CREATE INDEX IF NOT EXISTS ix_events_noise_pending ON events (noise_until) "
    "WHERE noise = 'pending'",
    "CREATE INDEX IF NOT EXISTS ix_events_flapping_open ON events (sensor_id) "
    "WHERE kind = 'sensor_flapping' AND flap_end IS NULL",
    """
    CREATE TABLE IF NOT EXISTS port_importance_overrides (
        device_id integer NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        if_index integer NOT NULL,
        important boolean NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        updated_by integer NULL,
        PRIMARY KEY (device_id, if_index)
    )""",
    # an existing install keeps the pre-upgrade meaning of "important ports only"
    f"""
    INSERT INTO settings (key, value)
    SELECT 'important_port_criteria', '{LEGACY_CRITERIA}'::jsonb
    WHERE EXISTS (SELECT 1 FROM settings)
    ON CONFLICT (key) DO NOTHING""",
]

DOWNGRADE = [
    "DELETE FROM settings WHERE key = 'important_port_criteria'",
    "DROP TABLE IF EXISTS port_importance_overrides",
    "DROP INDEX IF EXISTS ix_events_flapping_open",
    "DROP INDEX IF EXISTS ix_events_noise_pending",
    "DELETE FROM events WHERE kind = 'sensor_flapping'",
    "ALTER TABLE events DROP CONSTRAINT IF EXISTS events_noise_check",
    "ALTER TABLE events DROP COLUMN IF EXISTS flap_end",
    "ALTER TABLE events DROP COLUMN IF EXISTS flap_last",
    "ALTER TABLE events DROP COLUMN IF EXISTS flap_count",
    "ALTER TABLE events DROP COLUMN IF EXISTS noise_until",
    "ALTER TABLE events DROP COLUMN IF EXISTS noise",
    "ALTER TABLE events DROP CONSTRAINT IF EXISTS events_kind_check",
    f"ALTER TABLE events ADD CONSTRAINT events_kind_check CHECK (kind IN ({OLD_EVENT_KINDS}))",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
