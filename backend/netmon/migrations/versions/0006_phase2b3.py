"""Phase 2b-3: traps, syslog, event rules/conditions, passive event sensors, LAG membership,
Active Directory users, audit log, API tokens, new event kinds and the Teams channel type.

Plain PostgreSQL DDL (+ one plpgsql trigger function); Timescale hypertables and the pg_trgm
indexes are created by the bootstrap (netmon.db.timescale / netmon.db.extensions).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

OLD_SENSOR_TYPES = (
    "'ping','snmp_system','snmp_cpu','snmp_memory','snmp_interface','http','tcp_port','dns',"
    "'snmp_custom','snmp_disk','snmp_walk_count'"
)
NEW_SENSOR_TYPES = OLD_SENSOR_TYPES + ",'trap','syslog','port_flap'"
OLD_CHANNEL_TYPES = "'email','webhook','telegram'"
NEW_CHANNEL_TYPES = OLD_CHANNEL_TYPES + ",'teams'"
OLD_EVENT_KINDS = "'status_change','notification','system','discovery','acknowledge','maintenance'"
NEW_EVENT_KINDS = OLD_EVENT_KINDS + ",'notification_sent','port_change'"

# 0001: local user names. LDAP users store lower(sAMAccountName): 1-64 printable characters
# except "/\[]:;|=,+*?<>@ (data-model "Users, Active Directory").
LOCAL_USERNAME = "username ~ '^[A-Za-z0-9._-]{3,64}$'"
LDAP_USERNAME = (
    "char_length(username) BETWEEN 1 AND 64 AND username !~ '[\"/\\\\\\[\\]\\:;|=,+*?<>@[:cntrl:]]'"
)


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


TRAP_MATCH_LINK_DOWN = (
    '{"trap_oids":["1.3.6.1.6.3.1.1.5.3"],'
    '"varbinds":[{"oid":"1.3.6.1.2.1.2.2.1.7","op":"ne","value":"2"}],"ports":"important"}'
)
TRAP_MATCH_LINK_UP = '{"trap_oids":["1.3.6.1.6.3.1.1.5.4"],"varbinds":[],"ports":"any"}'
TRAP_MATCH_AUTH = '{"trap_oids":["1.3.6.1.6.3.1.1.5.5"],"varbinds":[],"ports":"any"}'
TRAP_MATCH_RESTART = (
    '{"trap_oids":["1.3.6.1.6.3.1.1.5.1","1.3.6.1.6.3.1.1.5.2"],"varbinds":[],"ports":"any"}'
)
SYSLOG_MATCH_ERROR = (
    '{"severity_max":3,"facilities":[],"programs":[],"text":null,"text_regex":false}'
)

# the built-in rules (data-model "Built-in rules"); the JSON goes in as bound parameters
# (a literal `"x":3` would be read as a bind parameter by text())
BUILTIN_RULES = [
    {
        "name": "Link down",
        "kind": "trap",
        "builtin_key": "trap_link_down",
        "priority": 1000,
        "match": TRAP_MATCH_LINK_DOWN,
        "key_varbind_oid": "1.3.6.1.2.1.2.2.1.1",
        "clear_match": TRAP_MATCH_LINK_UP,
        "clear_after_min": 1440,
    },
    {
        "name": "SNMP authentication failure",
        "kind": "trap",
        "builtin_key": "trap_auth_failure",
        "priority": 1010,
        "match": TRAP_MATCH_AUTH,
        "key_varbind_oid": None,
        "clear_match": None,
        "clear_after_min": 60,
    },
    {
        "name": "Device restarted",
        "kind": "trap",
        "builtin_key": "trap_restart",
        "priority": 1020,
        "match": TRAP_MATCH_RESTART,
        "key_varbind_oid": None,
        "clear_match": None,
        "clear_after_min": 30,
    },
    {
        "name": "Syslog error or worse",
        "kind": "syslog",
        "builtin_key": "syslog_error",
        "priority": 1030,
        "match": SYSLOG_MATCH_ERROR,
        "key_varbind_oid": None,
        "clear_match": None,
        "clear_after_min": 30,
    },
]
SEED_BUILTIN_RULES = "__seed_builtin_rules__"
INSERT_RULE = (
    "INSERT INTO event_rules (name, kind, enabled, builtin_key, priority, scope, match, action, "
    "key_varbind_oid, key_regex, clear_match, clear_after_min) VALUES (:name, :kind, false, "
    ":builtin_key, :priority, 'global', CAST(:match AS jsonb), 'warning', :key_varbind_oid, "
    "NULL, CAST(:clear_match AS jsonb), :clear_after_min)"
)

UPGRADE = [
    # 1-2. enums: sensor types (passive event sensors), Teams channels, event kinds
    *_checks(NEW_SENSOR_TYPES, NEW_CHANNEL_TYPES, NEW_EVENT_KINDS),
    "CREATE UNIQUE INDEX ux_sensors_event_type ON sensors (device_id, type) "
    "WHERE type IN ('trap','syslog','port_flap')",
    "CREATE INDEX ix_events_maintenance ON events (time DESC) WHERE kind = 'maintenance'",
    # 3. users: Active Directory
    "ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL",
    "ALTER TABLE users ADD COLUMN source text NOT NULL DEFAULT 'local' "
    "CONSTRAINT users_source_check CHECK (source IN ('local','ldap'))",
    "ALTER TABLE users ADD COLUMN external_id varchar(64) NULL",
    "CREATE UNIQUE INDEX ux_users_external_id ON users (external_id) WHERE external_id IS NOT NULL",
    "ALTER TABLE users ADD COLUMN display_name varchar(256) NULL",
    "ALTER TABLE users ADD COLUMN ldap_dn text NULL",
    "ALTER TABLE users ADD COLUMN ldap_synced_at timestamptz NULL",
    "ALTER TABLE users ADD CONSTRAINT users_source_password_check "
    "CHECK ((source = 'local') = (password_hash IS NOT NULL))",
    # the 0001 inline username check only fits local users
    "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_username_check",
    "ALTER TABLE users ADD CONSTRAINT users_username_check CHECK ("
    f"(source = 'local' AND {LOCAL_USERNAME}) OR (source = 'ldap' AND {LDAP_USERNAME}))",
    # 4. devices: additional source addresses, deleted event sensors
    "ALTER TABLE devices ADD COLUMN source_addresses inet[] NOT NULL DEFAULT '{}'",
    "ALTER TABLE devices ADD COLUMN event_sensor_optout text[] NOT NULL DEFAULT '{}'",
    # 5. switch ports: LAG membership
    "ALTER TABLE switch_ports ADD COLUMN lag_if_index integer NULL",
    "ALTER TABLE switch_ports ADD COLUMN lag_attached boolean NULL",
    # 6. alert state: flapping suppression
    "ALTER TABLE alert_state ADD COLUMN flap_suppressed boolean NOT NULL DEFAULT false",
    # 7. traps and syslog (hypertables in Timescale mode, see netmon.db.timescale)
    """
    CREATE TABLE traps (
        id bigint GENERATED ALWAYS AS IDENTITY,
        time timestamptz NOT NULL,
        source_ip inet NOT NULL,
        agent_addr inet NULL,
        device_id integer NULL,
        version text NOT NULL CONSTRAINT traps_version_check CHECK (version IN ('v1','v2c','v3')),
        pdu text NOT NULL CONSTRAINT traps_pdu_check CHECK (pdu IN ('trap','inform')),
        security_name varchar(64) NULL,
        trap_oid varchar(255) NOT NULL,
        trap_name varchar(128) NULL,
        uptime_ticks bigint NULL,
        varbinds jsonb NOT NULL DEFAULT '[]',
        summary text NOT NULL,
        matched_rule_ids integer[] NOT NULL DEFAULT '{}',
        PRIMARY KEY (id, time)
    )""",
    "CREATE INDEX ix_traps_time ON traps (time DESC)",
    "CREATE INDEX ix_traps_device_time ON traps (device_id, time DESC)",
    "CREATE INDEX ix_traps_unknown ON traps (source_ip, time DESC) WHERE device_id IS NULL",
    "CREATE INDEX ix_traps_oid ON traps (trap_oid varchar_pattern_ops)",
    """
    CREATE TABLE syslog_messages (
        id bigint GENERATED ALWAYS AS IDENTITY,
        time timestamptz NOT NULL,
        device_time timestamptz NULL,
        source_ip inet NOT NULL,
        device_id integer NULL,
        transport text NOT NULL CONSTRAINT syslog_messages_transport_check
            CHECK (transport IN ('udp','tcp')),
        format text NOT NULL CONSTRAINT syslog_messages_format_check
            CHECK (format IN ('rfc5424','rfc3164','raw')),
        facility smallint NOT NULL CONSTRAINT syslog_messages_facility_check
            CHECK (facility BETWEEN 0 AND 23),
        severity smallint NOT NULL CONSTRAINT syslog_messages_severity_check
            CHECK (severity BETWEEN 0 AND 7),
        hostname varchar(255) NOT NULL DEFAULT '',
        program varchar(128) NOT NULL DEFAULT '',
        pid varchar(128) NOT NULL DEFAULT '',
        msgid varchar(64) NOT NULL DEFAULT '',
        structured_data text NULL,
        message text NOT NULL,
        truncated boolean NOT NULL DEFAULT false,
        matched_rule_ids integer[] NOT NULL DEFAULT '{}',
        PRIMARY KEY (id, time)
    )""",
    "CREATE INDEX ix_syslog_time ON syslog_messages (time DESC)",
    "CREATE INDEX ix_syslog_device_time ON syslog_messages (device_id, time DESC)",
    "CREATE INDEX ix_syslog_unknown ON syslog_messages (source_ip, time DESC) "
    "WHERE device_id IS NULL",
    # 8. event rules and conditions
    """
    CREATE TABLE event_rules (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name varchar(128) NOT NULL,
        kind text NOT NULL CONSTRAINT event_rules_kind_check CHECK (kind IN ('trap','syslog')),
        enabled boolean NOT NULL DEFAULT true,
        builtin_key varchar(64) NULL CONSTRAINT event_rules_builtin_key_key UNIQUE,
        priority integer NOT NULL DEFAULT 100 CONSTRAINT event_rules_priority_check
            CHECK (priority BETWEEN 0 AND 100000),
        scope text NOT NULL DEFAULT 'global' CONSTRAINT event_rules_scope_check
            CHECK (scope IN ('global','group','device')),
        group_id integer NULL REFERENCES groups(id) ON DELETE CASCADE,
        device_id integer NULL REFERENCES devices(id) ON DELETE CASCADE,
        match jsonb NOT NULL,
        action text NOT NULL CONSTRAINT event_rules_action_check
            CHECK (action IN ('warning','down','ignore')),
        key_varbind_oid varchar(255) NULL,
        key_regex varchar(200) NULL,
        clear_match jsonb NULL,
        clear_after_min integer NULL CONSTRAINT event_rules_clear_after_min_check
            CHECK (clear_after_min BETWEEN 1 AND 10080),
        last_matched_at timestamptz NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT event_rules_scope_ids_check CHECK (
            (scope = 'group') = (group_id IS NOT NULL)
            AND (scope = 'device') = (device_id IS NOT NULL))
    )""",
    "CREATE INDEX ix_event_rules_group_id ON event_rules (group_id)",
    "CREATE INDEX ix_event_rules_device_id ON event_rules (device_id)",
    SEED_BUILTIN_RULES,
    """
    CREATE TABLE event_conditions (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        sensor_id integer NOT NULL REFERENCES sensors(id) ON DELETE CASCADE,
        source text NOT NULL CONSTRAINT event_conditions_source_check
            CHECK (source IN ('rule','flap')),
        rule_id integer NULL REFERENCES event_rules(id) ON DELETE CASCADE,
        key varchar(128) NOT NULL DEFAULT '',
        status text NOT NULL CONSTRAINT event_conditions_status_check
            CHECK (status IN ('warning','down')),
        first_at timestamptz NOT NULL,
        last_at timestamptz NOT NULL,
        count integer NOT NULL DEFAULT 1,
        message text NOT NULL,
        last_kind text NULL CONSTRAINT event_conditions_last_kind_check
            CHECK (last_kind IN ('trap','syslog')),
        last_ref_id bigint NULL,
        expires_at timestamptz NULL,
        CONSTRAINT event_conditions_rule_check CHECK ((source = 'rule') = (rule_id IS NOT NULL))
    )""",
    "CREATE UNIQUE INDEX ux_event_conditions_key ON event_conditions "
    "(sensor_id, source, (COALESCE(rule_id, 0)), key)",
    "CREATE INDEX ix_event_conditions_expires_at ON event_conditions (expires_at) "
    "WHERE expires_at IS NOT NULL",
    "CREATE INDEX ix_event_conditions_rule_id ON event_conditions (rule_id)",
    # 9. audit log (append-only)
    """
    CREATE TABLE audit_log (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        time timestamptz NOT NULL DEFAULT now(),
        actor_type text NOT NULL CONSTRAINT audit_log_actor_type_check
            CHECK (actor_type IN ('user','token','system','anonymous')),
        actor_user_id integer NULL,
        actor_name varchar(256) NOT NULL DEFAULT '',
        token_id integer NULL,
        ip varchar(45) NULL,
        action varchar(64) NOT NULL,
        entity_type varchar(32) NULL,
        entity_id bigint NULL,
        entity_name varchar(256) NULL,
        device_id integer NULL,
        source text NULL CONSTRAINT audit_log_source_check
            CHECK (source IN ('local','ldap','token')),
        reason varchar(64) NULL,
        changes jsonb NOT NULL DEFAULT '{}'
    )""",
    "CREATE INDEX ix_audit_log_time ON audit_log (time DESC)",
    "CREATE INDEX ix_audit_log_actor ON audit_log (actor_user_id, time DESC)",
    "CREATE INDEX ix_audit_log_entity ON audit_log (entity_type, entity_id, time DESC)",
    "CREATE INDEX ix_audit_log_device ON audit_log (device_id, time DESC) "
    "WHERE device_id IS NOT NULL",
    "CREATE INDEX ix_audit_log_action ON audit_log (action, time DESC)",
    """
    CREATE FUNCTION netmon_audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE'
           AND coalesce(current_setting('netmon.audit_purge', true), '') = 'on' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'audit_log is append-only (% rejected)', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END
    $$""",
    "CREATE TRIGGER audit_log_immutable BEFORE UPDATE OR DELETE ON audit_log "
    "FOR EACH ROW EXECUTE FUNCTION netmon_audit_immutable()",
    # 10. API tokens
    """
    CREATE TABLE api_tokens (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name varchar(128) NOT NULL,
        prefix varchar(12) NOT NULL,
        token_hash char(64) NOT NULL CONSTRAINT api_tokens_token_hash_key UNIQUE,
        role text NOT NULL CONSTRAINT api_tokens_role_check CHECK (role IN ('admin','viewer')),
        created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL,
        last_used_at timestamptz NULL,
        last_used_ip varchar(45) NULL,
        revoked_at timestamptz NULL
    )""",
    "CREATE UNIQUE INDEX ux_api_tokens_user_name ON api_tokens (user_id, lower(name))",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        if stmt == SEED_BUILTIN_RULES:
            for rule in BUILTIN_RULES:
                op.execute(sa.text(INSERT_RULE).bindparams(**rule))
        else:
            op.execute(stmt)


DOWNGRADE = [
    "DROP TABLE IF EXISTS api_tokens",
    "DROP TRIGGER IF EXISTS audit_log_immutable ON audit_log",
    "DROP FUNCTION IF EXISTS netmon_audit_immutable()",
    "DROP TABLE IF EXISTS audit_log",
    "DROP TABLE IF EXISTS event_conditions",
    "DROP TABLE IF EXISTS event_rules",
    "DROP TABLE IF EXISTS syslog_messages",
    "DROP TABLE IF EXISTS traps",
    "ALTER TABLE alert_state DROP COLUMN IF EXISTS flap_suppressed",
    "ALTER TABLE switch_ports DROP COLUMN IF EXISTS lag_attached, "
    "DROP COLUMN IF EXISTS lag_if_index",
    "ALTER TABLE devices DROP COLUMN IF EXISTS event_sensor_optout, "
    "DROP COLUMN IF EXISTS source_addresses",
    # users created by AD logins (their FKs are SET NULL / CASCADE as today)
    "DELETE FROM users WHERE source = 'ldap'",
    "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_username_check",
    f"ALTER TABLE users ADD CONSTRAINT users_username_check CHECK ({LOCAL_USERNAME})",
    "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_source_password_check",
    "DROP INDEX IF EXISTS ux_users_external_id",
    "ALTER TABLE users DROP COLUMN IF EXISTS ldap_synced_at, DROP COLUMN IF EXISTS ldap_dn, "
    "DROP COLUMN IF EXISTS display_name, DROP COLUMN IF EXISTS external_id, "
    "DROP COLUMN IF EXISTS source",
    "ALTER TABLE users ALTER COLUMN password_hash SET NOT NULL",
    "DELETE FROM events WHERE kind IN ('notification_sent','port_change')",
    # rule links cascade with the channel
    "DELETE FROM notification_channels WHERE type = 'teams'",
    "DROP INDEX IF EXISTS ix_events_maintenance",
    "DROP INDEX IF EXISTS ux_sensors_event_type",
    # channels, conditions and alert state cascade with the sensor
    "DELETE FROM sensors WHERE type IN ('trap','syslog','port_flap')",
    *_checks(OLD_SENSOR_TYPES, OLD_CHANNEL_TYPES, OLD_EVENT_KINDS),
]


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
