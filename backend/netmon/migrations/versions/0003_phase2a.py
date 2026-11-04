"""Phase 2a: discovery tables, new sensor types, telegram channels, discovery events.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-04
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

OLD_SENSOR_TYPES = (
    "'ping','snmp_system','snmp_cpu','snmp_memory','snmp_interface','http','tcp_port','dns'"
)
NEW_SENSOR_TYPES = OLD_SENSOR_TYPES + ",'snmp_custom','snmp_disk'"
OLD_CHANNEL_TYPES = "'email','webhook'"
NEW_CHANNEL_TYPES = OLD_CHANNEL_TYPES + ",'telegram'"
OLD_EVENT_KINDS = "'status_change','notification','system'"
NEW_EVENT_KINDS = OLD_EVENT_KINDS + ",'discovery'"

DEVICE_CLASSES = "'router','switch','access_point','printer','nas','server','workstation','unknown'"


def _checks(sensor_types: str, channel_types: str, event_kinds: str) -> list[str]:
    return [
        "ALTER TABLE sensors DROP CONSTRAINT IF EXISTS sensors_type_check",
        f"ALTER TABLE sensors ADD CONSTRAINT sensors_type_check CHECK (type IN ({sensor_types}))",
        "ALTER TABLE notification_channels "
        "DROP CONSTRAINT IF EXISTS notification_channels_type_check",
        "ALTER TABLE notification_channels ADD CONSTRAINT notification_channels_type_check "
        f"CHECK (type IN ({channel_types}))",
        "ALTER TABLE events DROP CONSTRAINT IF EXISTS events_kind_check",
        f"ALTER TABLE events ADD CONSTRAINT events_kind_check CHECK (kind IN ({event_kinds}))",
    ]


TABLES = [
    """
    CREATE TABLE discovery_profiles (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name varchar(128) NOT NULL,
        targets text[] NOT NULL CHECK (cardinality(targets) BETWEEN 1 AND 64),
        group_id integer NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        options jsonb NOT NULL DEFAULT '{}',
        schedule_enabled boolean NOT NULL DEFAULT false,
        schedule_every_hours integer NOT NULL DEFAULT 24
            CHECK (schedule_every_hours >= 1 AND schedule_every_hours <= 720),
        notify_new_hosts boolean NOT NULL DEFAULT false,
        notify_channel_ids integer[] NOT NULL DEFAULT '{}',
        last_run_at timestamptz NULL,
        next_run_at timestamptz NULL,
        last_scan_id integer NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE UNIQUE INDEX ux_discovery_profiles_name_lower ON discovery_profiles (lower(name))",
    """
    CREATE TABLE discovery_scans (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        profile_id integer NULL REFERENCES discovery_profiles(id) ON DELETE SET NULL,
        profile_name varchar(128) NULL,
        trigger text NOT NULL CHECK (trigger IN ('manual','scheduled')),
        status text NOT NULL CHECK (status IN ('running','completed','cancelled','failed')),
        group_id integer NULL REFERENCES groups(id) ON DELETE SET NULL,
        targets text[] NOT NULL,
        options jsonb NOT NULL DEFAULT '{}',
        credentials jsonb NOT NULL DEFAULT '[]',
        total_hosts integer NOT NULL,
        scanned_hosts integer NOT NULL DEFAULT 0,
        found_hosts integer NOT NULL DEFAULT 0,
        new_hosts integer NOT NULL DEFAULT 0,
        error text NULL,
        created_by integer NULL REFERENCES users(id) ON DELETE SET NULL,
        started_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz NULL
    )""",
    "CREATE INDEX ix_discovery_scans_started_at ON discovery_scans (started_at DESC)",
    "CREATE UNIQUE INDEX ux_discovery_scans_running ON discovery_scans ((true)) "
    "WHERE status = 'running'",
    f"""
    CREATE TABLE discovery_results (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        scan_id integer NOT NULL REFERENCES discovery_scans(id) ON DELETE CASCADE,
        ip inet NOT NULL,
        hostname varchar(255) NULL,
        mac varchar(17) NULL,
        ping_ok boolean NOT NULL,
        ping_rtt_ms double precision NULL,
        snmp_ok boolean NOT NULL,
        snmp_version text NULL CHECK (snmp_version IN ('v1','v2c','v3')),
        snmp_credential_index integer NULL,
        sys_name varchar(255) NULL,
        sys_descr text NULL,
        sys_object_id varchar(255) NULL,
        open_ports integer[] NOT NULL DEFAULT '{{}}',
        device_class text NOT NULL CHECK (device_class IN ({DEVICE_CLASSES})),
        vendor varchar(32) NULL,
        suggested_template varchar(32) NOT NULL,
        is_new boolean NOT NULL,
        imported_device_id integer NULL REFERENCES devices(id) ON DELETE SET NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_discovery_results_scan_ip UNIQUE (scan_id, ip)
    )""",
    "CREATE INDEX ix_discovery_results_scan_id ON discovery_results (scan_id)",
    """
    CREATE TABLE discovery_known_hosts (
        ip inet PRIMARY KEY,
        first_seen_at timestamptz NOT NULL DEFAULT now(),
        last_seen_at timestamptz NOT NULL DEFAULT now(),
        last_scan_id integer NULL
    )""",
]


def upgrade() -> None:
    for stmt in _checks(NEW_SENSOR_TYPES, NEW_CHANNEL_TYPES, NEW_EVENT_KINDS):
        op.execute(stmt)
    for stmt in TABLES:
        op.execute(stmt)


def downgrade() -> None:
    for table in (
        "discovery_results",
        "discovery_known_hosts",
        "discovery_scans",
        "discovery_profiles",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DELETE FROM sensors WHERE type IN ('snmp_custom','snmp_disk')")
    op.execute("DELETE FROM notification_channels WHERE type = 'telegram'")
    op.execute("DELETE FROM events WHERE kind = 'discovery'")
    for stmt in _checks(OLD_SENSOR_TYPES, OLD_CHANNEL_TYPES, OLD_EVENT_KINDS):
        op.execute(stmt)
