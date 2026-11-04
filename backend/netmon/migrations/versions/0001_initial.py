"""Initial schema (plain PostgreSQL DDL only; Timescale is handled in netmon.db.timescale).

Revision ID: 0001
Revises:
Create Date: 2026-11-04
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATUS = "('up','warning','down','paused','unknown')"

STATEMENTS = [
    """
    CREATE TABLE users (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        username varchar(64) NOT NULL CHECK (username ~ '^[A-Za-z0-9._-]{3,64}$'),
        email varchar(255) NULL,
        password_hash text NOT NULL,
        role text NOT NULL DEFAULT 'viewer' CHECK (role IN ('admin','viewer')),
        language text NOT NULL DEFAULT 'en' CHECK (language IN ('en','he')),
        is_active boolean NOT NULL DEFAULT true,
        token_version integer NOT NULL DEFAULT 0,
        last_login_at timestamptz NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE UNIQUE INDEX ux_users_username_lower ON users (lower(username))",
    """
    CREATE TABLE groups (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        parent_id integer NULL REFERENCES groups(id) ON DELETE CASCADE,
        name varchar(128) NOT NULL,
        description text NOT NULL DEFAULT '',
        snmp_credentials jsonb NULL,
        sort_order integer NOT NULL DEFAULT 0,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE UNIQUE INDEX ux_groups_parent_name ON groups (COALESCE(parent_id, 0), lower(name))",
    """
    CREATE TABLE devices (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        group_id integer NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        name varchar(128) NOT NULL,
        host varchar(255) NOT NULL,
        description text NOT NULL DEFAULT '',
        enabled boolean NOT NULL DEFAULT true,
        snmp_credentials jsonb NULL,
        sort_order integer NOT NULL DEFAULT 0,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX ix_devices_group_id ON devices (group_id)",
    f"""
    CREATE TABLE sensors (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        device_id integer NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        name varchar(128) NOT NULL,
        type text NOT NULL CHECK (type IN ('ping','snmp_system','snmp_cpu','snmp_memory',
            'snmp_interface','http','tcp_port','dns')),
        config jsonb NOT NULL,
        interval_s integer NOT NULL DEFAULT 60 CHECK (interval_s >= 10 AND interval_s <= 86400),
        enabled boolean NOT NULL DEFAULT true,
        status text NOT NULL DEFAULT 'unknown' CHECK (status IN {STATUS}),
        status_message text NOT NULL DEFAULT '',
        last_check_at timestamptz NULL,
        last_status_change_at timestamptz NULL,
        state jsonb NOT NULL DEFAULT '{{}}',
        sort_order integer NOT NULL DEFAULT 0,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX ix_sensors_device_id ON sensors (device_id)",
    "CREATE INDEX ix_sensors_status ON sensors (status)",
    """
    CREATE TABLE sensor_channels (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        sensor_id integer NOT NULL REFERENCES sensors(id) ON DELETE CASCADE,
        key varchar(64) NOT NULL,
        unit varchar(16) NOT NULL,
        warning_min double precision NULL,
        warning_max double precision NULL,
        down_min double precision NULL,
        down_max double precision NULL,
        last_value double precision NULL,
        status text NOT NULL DEFAULT 'unknown' CHECK (status IN ('up','warning','down','unknown')),
        CONSTRAINT uq_sensor_channels_sensor_key UNIQUE (sensor_id, key)
    )""",
    """
    CREATE TABLE measurements (
        time timestamptz NOT NULL,
        sensor_id integer NOT NULL,
        channel varchar(64) NOT NULL,
        value double precision NOT NULL
    )""",
    "CREATE INDEX ix_measurements_sensor_channel_time "
    "ON measurements (sensor_id, channel, time DESC)",
    f"""
    CREATE TABLE events (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        time timestamptz NOT NULL DEFAULT now(),
        kind text NOT NULL CHECK (kind IN ('status_change','notification','system')),
        sensor_id integer NULL REFERENCES sensors(id) ON DELETE SET NULL,
        device_id integer NULL REFERENCES devices(id) ON DELETE SET NULL,
        group_id integer NULL REFERENCES groups(id) ON DELETE SET NULL,
        sensor_name varchar(128) NULL,
        device_name varchar(128) NULL,
        old_status text NULL CHECK (old_status IN {STATUS}),
        new_status text NULL CHECK (new_status IN {STATUS}),
        message text NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX ix_events_time ON events (time DESC)",
    "CREATE INDEX ix_events_sensor_time ON events (sensor_id, time DESC)",
    "CREATE INDEX ix_events_device_time ON events (device_id, time DESC)",
    """
    CREATE TABLE notification_channels (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name varchar(128) NOT NULL UNIQUE,
        type text NOT NULL CHECK (type IN ('email','webhook')),
        config jsonb NOT NULL,
        enabled boolean NOT NULL DEFAULT true,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    """
    CREATE TABLE alert_rules (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name varchar(128) NOT NULL,
        sensor_id integer NULL REFERENCES sensors(id) ON DELETE CASCADE,
        on_statuses text[] NOT NULL CHECK (cardinality(on_statuses) > 0
            AND on_statuses <@ ARRAY['down','warning','up']::text[]),
        delay_s integer NOT NULL DEFAULT 0 CHECK (delay_s >= 0 AND delay_s <= 86400),
        enabled boolean NOT NULL DEFAULT true,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    """
    CREATE TABLE alert_rule_channels (
        rule_id integer NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
        channel_id integer NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
        PRIMARY KEY (rule_id, channel_id)
    )""",
    """
    CREATE TABLE settings (
        key varchar(64) PRIMARY KEY,
        value jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
]

TABLES = [
    "settings",
    "alert_rule_channels",
    "alert_rules",
    "notification_channels",
    "events",
    "measurements",
    "sensor_channels",
    "sensors",
    "devices",
    "groups",
    "users",
]


def upgrade() -> None:
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
