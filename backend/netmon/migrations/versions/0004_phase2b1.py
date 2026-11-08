"""Phase 2b-1: dependencies, unreachable, maintenance, acknowledge, escalation (alert_state),
fingerprinting, topology.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

OLD_STATUS = "'up','warning','down','paused','unknown'"
NEW_STATUS = OLD_STATUS + ",'unreachable'"
OLD_EVENT_KINDS = "'status_change','notification','system','discovery'"
NEW_EVENT_KINDS = OLD_EVENT_KINDS + ",'acknowledge','maintenance'"


def _checks(status: str, kinds: str) -> list[str]:
    return [
        "ALTER TABLE sensors DROP CONSTRAINT IF EXISTS sensors_status_check",
        f"ALTER TABLE sensors ADD CONSTRAINT sensors_status_check CHECK (status IN ({status}))",
        "ALTER TABLE events DROP CONSTRAINT IF EXISTS events_old_status_check",
        "ALTER TABLE events ADD CONSTRAINT events_old_status_check "
        f"CHECK (old_status IN ({status}))",
        "ALTER TABLE events DROP CONSTRAINT IF EXISTS events_new_status_check",
        "ALTER TABLE events ADD CONSTRAINT events_new_status_check "
        f"CHECK (new_status IN ({status}))",
        "ALTER TABLE events DROP CONSTRAINT IF EXISTS events_kind_check",
        f"ALTER TABLE events ADD CONSTRAINT events_kind_check CHECK (kind IN ({kinds}))",
    ]


def _dependency_columns(table: str) -> list[str]:
    return [
        f"ALTER TABLE {table} ADD COLUMN dependency_mode text NOT NULL DEFAULT 'inherit' "
        f"CONSTRAINT {table}_dependency_mode_check "
        "CHECK (dependency_mode IN ('inherit','none','device'))",
        f"ALTER TABLE {table} ADD COLUMN dependency_device_id integer NULL "
        "REFERENCES devices(id) ON DELETE SET NULL",
        f"ALTER TABLE {table} ADD COLUMN dependency_sensor_id integer NULL "
        "REFERENCES sensors(id) ON DELETE SET NULL",
    ]


UPGRADE = [
    *_checks(NEW_STATUS, NEW_EVENT_KINDS),
    # 2. groups
    *_dependency_columns("groups"),
    # 3. devices
    *_dependency_columns("devices"),
    "ALTER TABLE devices ADD COLUMN identity jsonb NULL",
    "ALTER TABLE devices ADD COLUMN lldp_chassis_id varchar(255) NULL",
    "ALTER TABLE devices ADD COLUMN lldp_sys_name varchar(255) NULL",
    "CREATE INDEX ix_devices_dependency_device_id ON devices (dependency_device_id)",
    # 4. sensors: acknowledgement
    "ALTER TABLE sensors ADD COLUMN ack_at timestamptz NULL",
    "ALTER TABLE sensors ADD COLUMN ack_user_id integer NULL "
    "REFERENCES users(id) ON DELETE SET NULL",
    "ALTER TABLE sensors ADD COLUMN ack_by varchar(64) NULL",
    "ALTER TABLE sensors ADD COLUMN ack_comment text NOT NULL DEFAULT ''",
    "ALTER TABLE sensors ADD COLUMN ack_status text NULL "
    "CONSTRAINT sensors_ack_status_check CHECK (ack_status IN ('down','warning'))",
    # 5. events
    "ALTER TABLE events ADD COLUMN in_maintenance boolean NOT NULL DEFAULT false",
    # 6. alert rules: escalation
    "ALTER TABLE alert_rules ADD COLUMN escalation_after_s integer NULL "
    "CONSTRAINT alert_rules_escalation_after_s_check "
    "CHECK (escalation_after_s >= 60 AND escalation_after_s <= 604800)",
    "ALTER TABLE alert_rules ADD COLUMN escalation_channel_ids integer[] NOT NULL DEFAULT '{}'",
    "ALTER TABLE alert_rules ADD COLUMN repeat_every_s integer NULL "
    "CONSTRAINT alert_rules_repeat_every_s_check "
    "CHECK (repeat_every_s >= 300 AND repeat_every_s <= 604800)",
    # 7. alert_state replaces alert_notified
    """
    CREATE TABLE alert_state (
        rule_id integer NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
        sensor_id integer NOT NULL REFERENCES sensors(id) ON DELETE CASCADE,
        status text NOT NULL CHECK (status IN ('down','warning','up','suspended')),
        event_id bigint NULL,
        problem_since timestamptz NOT NULL,
        due_at timestamptz NULL,
        primary_sent_at timestamptz NULL,
        escalation_due_at timestamptz NULL,
        escalated_at timestamptz NULL,
        next_reminder_at timestamptz NULL,
        reminders_sent integer NOT NULL DEFAULT 0,
        maintenance_suppressed boolean NOT NULL DEFAULT false,
        notified_channel_ids integer[] NOT NULL DEFAULT '{}',
        sending_since timestamptz NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (rule_id, sensor_id)
    )""",
    "CREATE INDEX ix_alert_state_sensor_id ON alert_state (sensor_id)",
    """
    INSERT INTO alert_state (rule_id, sensor_id, status, problem_since, primary_sent_at,
                             notified_channel_ids)
    SELECT n.rule_id, n.sensor_id, n.status, n.time, n.time,
           COALESCE((SELECT array_agg(c.channel_id ORDER BY c.channel_id)
                     FROM alert_rule_channels c WHERE c.rule_id = n.rule_id), '{}')
    FROM alert_notified n""",
    "DROP TABLE alert_notified",
    # 8. maintenance windows
    """
    CREATE TABLE maintenance_windows (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name varchar(128) NOT NULL,
        enabled boolean NOT NULL DEFAULT true,
        scope text NOT NULL CHECK (scope IN ('global','group','device')),
        group_id integer NULL REFERENCES groups(id) ON DELETE CASCADE,
        device_id integer NULL REFERENCES devices(id) ON DELETE CASCADE,
        kind text NOT NULL CHECK (kind IN ('once','weekly')),
        starts_at timestamptz NULL,
        ends_at timestamptz NULL,
        weekdays smallint[] NULL,
        start_time time NULL,
        duration_min integer NULL,
        comment text NOT NULL DEFAULT '',
        created_by integer NULL REFERENCES users(id) ON DELETE SET NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT maintenance_windows_scope_ids_check CHECK (
            (scope = 'group') = (group_id IS NOT NULL)
            AND (scope = 'device') = (device_id IS NOT NULL)),
        CONSTRAINT maintenance_windows_kind_fields_check CHECK (
            (kind = 'once' AND starts_at IS NOT NULL AND ends_at IS NOT NULL
             AND ends_at > starts_at AND weekdays IS NULL AND start_time IS NULL
             AND duration_min IS NULL)
            OR (kind = 'weekly' AND starts_at IS NULL AND ends_at IS NULL
                AND weekdays IS NOT NULL AND start_time IS NOT NULL
                AND duration_min BETWEEN 1 AND 10080))
    )""",
    "CREATE INDEX ix_maintenance_windows_group_id ON maintenance_windows (group_id)",
    "CREATE INDEX ix_maintenance_windows_device_id ON maintenance_windows (device_id)",
    # 9. discovery results: fingerprinting
    "ALTER TABLE discovery_results ADD COLUMN fingerprint jsonb NULL",
    "ALTER TABLE discovery_results ADD COLUMN model varchar(64) NULL",
    "ALTER TABLE discovery_results ADD COLUMN classified_by text NOT NULL DEFAULT 'ports' "
    "CONSTRAINT discovery_results_classified_by_check "
    "CHECK (classified_by IN ('snmp','fingerprint','ports','none'))",
    "ALTER TABLE discovery_results ADD COLUMN confidence text NOT NULL DEFAULT 'low' "
    "CONSTRAINT discovery_results_confidence_check "
    "CHECK (confidence IN ('high','medium','low'))",
    "UPDATE discovery_results SET classified_by = 'snmp', confidence = 'medium' WHERE snmp_ok",
    # 10. topology
    """
    CREATE TABLE topology_neighbors (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        chassis_id varchar(255) NOT NULL UNIQUE,
        sys_name varchar(255) NULL,
        sys_descr text NULL,
        mgmt_address varchar(64) NULL,
        first_seen timestamptz NOT NULL DEFAULT now(),
        last_seen timestamptz NOT NULL DEFAULT now()
    )""",
    """
    CREATE TABLE topology_links (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        source text NOT NULL CHECK (source IN ('lldp','cdp','manual')),
        a_device_id integer NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        a_if_index integer NULL,
        a_port varchar(255) NOT NULL DEFAULT '',
        b_device_id integer NULL REFERENCES devices(id) ON DELETE CASCADE,
        b_neighbor_id integer NULL REFERENCES topology_neighbors(id) ON DELETE CASCADE,
        b_if_index integer NULL,
        b_port varchar(255) NOT NULL DEFAULT '',
        first_seen timestamptz NOT NULL DEFAULT now(),
        last_seen timestamptz NOT NULL DEFAULT now(),
        missed_refreshes integer NOT NULL DEFAULT 0,
        stale boolean NOT NULL DEFAULT false,
        created_by integer NULL REFERENCES users(id) ON DELETE SET NULL,
        CONSTRAINT topology_links_one_b_check
            CHECK ((b_device_id IS NULL) <> (b_neighbor_id IS NULL)),
        CONSTRAINT topology_links_not_self_check
            CHECK (b_device_id IS NULL OR b_device_id <> a_device_id)
    )""",
    "CREATE UNIQUE INDEX ux_topology_links_ends ON topology_links (source, a_device_id, a_port, "
    "COALESCE(b_device_id, 0), COALESCE(b_neighbor_id, 0), b_port)",
    "CREATE INDEX ix_topology_links_b_device_id ON topology_links (b_device_id)",
    "CREATE INDEX ix_topology_links_b_neighbor_id ON topology_links (b_neighbor_id)",
    """
    CREATE TABLE topology_layout (
        node_key varchar(32) PRIMARY KEY,
        x double precision NOT NULL,
        y double precision NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    """
    CREATE TABLE topology_refreshes (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        trigger text NOT NULL CHECK (trigger IN ('manual','scheduled')),
        status text NOT NULL CHECK (status IN ('running','completed','failed')),
        started_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz NULL,
        devices_total integer NOT NULL DEFAULT 0,
        devices_ok integer NOT NULL DEFAULT 0,
        devices_failed integer NOT NULL DEFAULT 0,
        links_found integer NOT NULL DEFAULT 0,
        error text NULL,
        created_by integer NULL REFERENCES users(id) ON DELETE SET NULL
    )""",
    "CREATE UNIQUE INDEX ux_topology_refreshes_running ON topology_refreshes ((true)) "
    "WHERE status = 'running'",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


DOWNGRADE = [
    "DROP TABLE IF EXISTS topology_refreshes",
    "DROP TABLE IF EXISTS topology_layout",
    "DROP TABLE IF EXISTS topology_links",
    "DROP TABLE IF EXISTS topology_neighbors",
    "DROP TABLE IF EXISTS maintenance_windows",
    """
    CREATE TABLE alert_notified (
        rule_id integer NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
        sensor_id integer NOT NULL REFERENCES sensors(id) ON DELETE CASCADE,
        status text NOT NULL CHECK (status IN ('down','warning')),
        time timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (rule_id, sensor_id)
    )""",
    """
    INSERT INTO alert_notified (rule_id, sensor_id, status, time)
    SELECT rule_id, sensor_id,
           CASE WHEN status IN ('down','warning') THEN status ELSE 'down' END,
           COALESCE(primary_sent_at, problem_since)
    FROM alert_state
    WHERE cardinality(notified_channel_ids) > 0
       OR (sending_since IS NOT NULL AND status IN ('down','warning'))""",
    "DROP TABLE alert_state",
    "ALTER TABLE discovery_results DROP COLUMN IF EXISTS fingerprint",
    "ALTER TABLE discovery_results DROP COLUMN IF EXISTS model",
    "ALTER TABLE discovery_results DROP COLUMN IF EXISTS classified_by",
    "ALTER TABLE discovery_results DROP COLUMN IF EXISTS confidence",
    "ALTER TABLE alert_rules DROP COLUMN IF EXISTS escalation_after_s",
    "ALTER TABLE alert_rules DROP COLUMN IF EXISTS escalation_channel_ids",
    "ALTER TABLE alert_rules DROP COLUMN IF EXISTS repeat_every_s",
    "ALTER TABLE events DROP COLUMN IF EXISTS in_maintenance",
    "ALTER TABLE sensors DROP COLUMN IF EXISTS ack_at",
    "ALTER TABLE sensors DROP COLUMN IF EXISTS ack_user_id",
    "ALTER TABLE sensors DROP COLUMN IF EXISTS ack_by",
    "ALTER TABLE sensors DROP COLUMN IF EXISTS ack_comment",
    "ALTER TABLE sensors DROP COLUMN IF EXISTS ack_status",
    "DROP INDEX IF EXISTS ix_devices_dependency_device_id",
    "ALTER TABLE devices DROP COLUMN IF EXISTS identity",
    "ALTER TABLE devices DROP COLUMN IF EXISTS lldp_chassis_id",
    "ALTER TABLE devices DROP COLUMN IF EXISTS lldp_sys_name",
    *[
        f"ALTER TABLE {t} DROP COLUMN IF EXISTS {c}"
        for t in ("devices", "groups")
        for c in ("dependency_mode", "dependency_device_id", "dependency_sensor_id")
    ],
    "UPDATE sensors SET status = 'down' WHERE status = 'unreachable'",
    "UPDATE events SET old_status = 'down' WHERE old_status = 'unreachable'",
    "UPDATE events SET new_status = 'down' WHERE new_status = 'unreachable'",
    "DELETE FROM events WHERE kind IN ('acknowledge','maintenance')",
    *_checks(OLD_STATUS, OLD_EVENT_KINDS),
]


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
