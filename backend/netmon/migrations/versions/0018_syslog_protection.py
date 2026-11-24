"""[syslog-protection] Syslog flood protection.

- `syslog_filters`: the ordered syslog filter rules (first match wins) applied at ingest:
  `drop` or `keep` (only the newest `keep_n` per source). Empty after the upgrade, so nothing
  changes until an operator adds a rule.
- `syslog_messages.repeat_count` (int, NOT NULL DEFAULT 1) and `last_seen` (timestamptz NULL):
  identical messages within the dedup window are stored as one row (`time` = first seen).
- `syslog_messages.filter_rule_id` (int NULL, no FK: rows outlive their rule; the id is an
  identity and never reused): the `keep` rule a row was stored under, so the per-write prune
  of "keep newest N" only touches that rule's rows (partial index, empty for other rows).

TimescaleDB: `syslog_messages` is a hypertable WITHOUT compression (netmon.db.timescale:
rows are updated by "assign" / device delete, and now by the dedup repeat count), so
ADD/DROP COLUMN and the index propagate to every chunk; constant defaults are catalog-only
(no table rewrite) in both modes. Plain PostgreSQL (NETMON_DISABLE_TIMESCALE=1): the same DDL.
Idempotent. `downgrade()` drops the table, the index and the three columns (the repeat
counts are lost; every row stays). An older release on the upgraded database keeps working:
its INSERT leaves the new columns at their defaults.

Revision ID: 0018 (data-model "[syslog-protection] Migration"). No 0017 exists: the
branch expected to add it needed no migration, so this follows 0016 directly.
Revises: 0016
Create Date: 2026-11-24
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0016"
branch_labels = None
depends_on = None

UPGRADE = [
    """
    CREATE TABLE IF NOT EXISTS syslog_filters (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name varchar(100) NOT NULL,
        enabled boolean NOT NULL DEFAULT true,
        position integer NOT NULL DEFAULT 0,
        sources text[] NOT NULL DEFAULT '{}',
        device_ids integer[] NOT NULL DEFAULT '{}',
        program varchar(128) NOT NULL DEFAULT '',
        program_match text NOT NULL DEFAULT 'exact'
            CONSTRAINT syslog_filters_program_match_check
            CHECK (program_match IN ('exact','prefix')),
        msgid varchar(64) NOT NULL DEFAULT '',
        severity_min smallint NULL
            CONSTRAINT syslog_filters_severity_check CHECK (severity_min BETWEEN 0 AND 7),
        message_contains varchar(200) NOT NULL DEFAULT '',
        action text NOT NULL DEFAULT 'drop'
            CONSTRAINT syslog_filters_action_check CHECK (action IN ('drop','keep')),
        keep_n integer NOT NULL DEFAULT 100
            CONSTRAINT syslog_filters_keep_check CHECK (keep_n BETWEEN 1 AND 100000),
        hits bigint NOT NULL DEFAULT 0,
        last_hit_at timestamptz NULL,
        hits_since timestamptz NOT NULL DEFAULT now(),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    "ALTER TABLE syslog_messages ADD COLUMN IF NOT EXISTS repeat_count integer NOT NULL DEFAULT 1",
    "ALTER TABLE syslog_messages ADD COLUMN IF NOT EXISTS last_seen timestamptz NULL",
    "ALTER TABLE syslog_messages ADD COLUMN IF NOT EXISTS filter_rule_id integer NULL",
    "CREATE INDEX IF NOT EXISTS ix_syslog_filter_rule ON syslog_messages "
    "(filter_rule_id, source_ip, time DESC) WHERE filter_rule_id IS NOT NULL",
]

DOWNGRADE = [
    "DROP INDEX IF EXISTS ix_syslog_filter_rule",
    "ALTER TABLE syslog_messages DROP COLUMN IF EXISTS filter_rule_id",
    "ALTER TABLE syslog_messages DROP COLUMN IF EXISTS last_seen",
    "ALTER TABLE syslog_messages DROP COLUMN IF EXISTS repeat_count",
    "DROP TABLE IF EXISTS syslog_filters",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
