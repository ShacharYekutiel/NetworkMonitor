"""Phase 3: config backup (SSH credentials, host keys, status, versions, config-change trigger
rules), flow collector (exporters, 1-minute tables), new-device alerts (known MACs, allow-list),
OIDC users, port flapping episodes, reports, NOC kiosk screens and chat acknowledge.

Plain PostgreSQL DDL only: hypertables, continuous aggregates, the hourly flow fallback tables
and the trigram index on config_versions are created by the bootstrap (data-model "Phase 3
migration (0007)").

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

OLD_SENSOR_TYPES = (
    "'ping','snmp_system','snmp_cpu','snmp_memory','snmp_interface','http','tcp_port','dns',"
    "'snmp_custom','snmp_disk','snmp_walk_count','trap','syslog','port_flap'"
)
NEW_SENSOR_TYPES = OLD_SENSOR_TYPES + ",'config_backup','new_mac'"
OLD_PASSIVE = "'trap','syslog','port_flap'"
NEW_PASSIVE = OLD_PASSIVE + ",'config_backup','new_mac'"
OLD_EVENT_KINDS = (
    "'status_change','notification','system','discovery','acknowledge','maintenance',"
    "'notification_sent','port_change'"
)
NEW_EVENT_KINDS = OLD_EVENT_KINDS + ",'config_change','new_device'"
OLD_USER_SOURCES = "'local','ldap'"
NEW_USER_SOURCES = OLD_USER_SOURCES + ",'oidc'"
OLD_COND_SOURCES = "'rule','flap'"
NEW_COND_SOURCES = OLD_COND_SOURCES + ",'backup_failed','host_key','config_changed','new_mac'"
OLD_RULE_ACTIONS = "'warning','down','ignore'"
NEW_RULE_ACTIONS = OLD_RULE_ACTIONS + ",'config_backup'"

# 0006 username rules; [3] OIDC users: lower(preferred_username), 1-64 characters, no
# control characters or white space (UPNs contain '@')
LOCAL_USERNAME = "username ~ '^[A-Za-z0-9._-]{3,64}$'"
LDAP_USERNAME = (
    "char_length(username) BETWEEN 1 AND 64 AND username !~ '[\"/\\\\\\[\\]\\:;|=,+*?<>@[:cntrl:]]'"
)
OIDC_USERNAME = "char_length(username) BETWEEN 1 AND 64 AND username !~ '[[:cntrl:][:space:]]'"
OLD_USERNAME_CHECK = (
    f"(source = 'local' AND {LOCAL_USERNAME}) OR (source = 'ldap' AND {LDAP_USERNAME})"
)
NEW_USERNAME_CHECK = f"{OLD_USERNAME_CHECK} OR (source = 'oidc' AND {OIDC_USERNAME})"

BACKUP_MODES = "'auto','on','off'"
BACKUP_RESULTS = "'ok','unchanged','failed','cancelled'"
BACKUP_TRIGGERS = "'schedule','manual','event','retry'"
BACKUP_ERRORS = (
    "'connect_timeout','connect_refused','auth_failed','host_key_changed','prompt_timeout',"
    "'command_error','too_large','no_driver','no_credentials','cancelled','internal'"
)
FLOW_PROTOCOLS = "'netflow5','netflow9','ipfix','sflow5'"
KNOWN_MAC_STATES = "'baseline','new','known','allowed'"
REPORT_SECTIONS = (
    "'availability','top_alerts','flapping_ports','port_errors','config_changes','new_devices',"
    "'top_talkers'"
)
KIOSK_WIDGETS = "'summary','down_devices','problems','recent_alerts','map','group_tiles'"
LANGUAGES = "'en','he'"


def _checks(
    sensor_types: str,
    passive: str,
    event_kinds: str,
    user_sources: str,
    username_check: str,
    cond_sources: str,
    rule_actions: str,
) -> list[str]:
    return [
        "ALTER TABLE sensors DROP CONSTRAINT IF EXISTS sensors_type_check",
        f"ALTER TABLE sensors ADD CONSTRAINT sensors_type_check CHECK (type IN ({sensor_types}))",
        "DROP INDEX IF EXISTS ux_sensors_event_type",
        f"CREATE UNIQUE INDEX ux_sensors_event_type ON sensors (device_id, type) "
        f"WHERE type IN ({passive})",
        "ALTER TABLE events DROP CONSTRAINT IF EXISTS events_kind_check",
        f"ALTER TABLE events ADD CONSTRAINT events_kind_check CHECK (kind IN ({event_kinds}))",
        "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_source_check",
        f"ALTER TABLE users ADD CONSTRAINT users_source_check CHECK (source IN ({user_sources}))",
        "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_username_check",
        f"ALTER TABLE users ADD CONSTRAINT users_username_check CHECK ({username_check})",
        "ALTER TABLE event_conditions DROP CONSTRAINT IF EXISTS event_conditions_source_check",
        "ALTER TABLE event_conditions ADD CONSTRAINT event_conditions_source_check "
        f"CHECK (source IN ({cond_sources}))",
        "ALTER TABLE event_rules DROP CONSTRAINT IF EXISTS event_rules_action_check",
        "ALTER TABLE event_rules ADD CONSTRAINT event_rules_action_check "
        f"CHECK (action IN ({rule_actions}))",
    ]


# the built-in config-change trigger rules (data-model "Built-in config-change rules"); the
# JSON goes in as bound parameters
BUILTIN_BACKUP_RULES = [
    {
        "name": "Comware configuration changed",
        "builtin_key": "syslog_comware_cfg_changed",
        "priority": 2000,
        "match": '{"severity_max":null,"facilities":[],"programs":["CFGMAN"],"text":null,'
        '"text_regex":false}',
    },
    {
        "name": "ArubaOS-Switch configuration changed",
        "builtin_key": "syslog_arubaos_cfg_changed",
        "priority": 2010,
        "match": '{"severity_max":null,"facilities":[],"programs":["mgr"],'
        '"text":"(running|startup) config(uration)? (file )?changed","text_regex":true}',
    },
    {
        "name": "Cisco IOS configured",
        "builtin_key": "syslog_cisco_config_i",
        "priority": 2020,
        "match": '{"severity_max":null,"facilities":[],"programs":[],"text":"%SYS-5-CONFIG_I",'
        '"text_regex":false}',
    },
]
SEED_BUILTIN_RULES = "__seed_builtin_backup_rules__"
INSERT_RULE = (
    "INSERT INTO event_rules (name, kind, enabled, builtin_key, priority, scope, match, action) "
    "VALUES (:name, 'syslog', false, :builtin_key, :priority, 'global', CAST(:match AS jsonb), "
    "'config_backup') ON CONFLICT (builtin_key) DO NOTHING"
)

UPGRADE = [
    # 1-4. enums
    *_checks(
        NEW_SENSOR_TYPES,
        NEW_PASSIVE,
        NEW_EVENT_KINDS,
        NEW_USER_SOURCES,
        NEW_USERNAME_CHECK,
        NEW_COND_SOURCES,
        NEW_RULE_ACTIONS,
    ),
    "ALTER TABLE event_rules ADD CONSTRAINT event_rules_backup_action_check CHECK ("
    "action <> 'config_backup' OR (key_varbind_oid IS NULL AND key_regex IS NULL "
    "AND clear_match IS NULL AND clear_after_min IS NULL))",
    SEED_BUILTIN_RULES,
    # 4b. audit log: OIDC sign-ins record source 'oidc'
    "ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_source_check",
    "ALTER TABLE audit_log ADD CONSTRAINT audit_log_source_check "
    "CHECK (source IN ('local','ldap','token','oidc'))",
    # 5. SSH credentials, config backup mode / driver override, new-device excluded ports
    "ALTER TABLE groups ADD COLUMN ssh_credentials jsonb NULL",
    "ALTER TABLE devices ADD COLUMN ssh_credentials jsonb NULL",
    "ALTER TABLE devices ADD COLUMN config_backup text NOT NULL DEFAULT 'auto' "
    f"CONSTRAINT devices_config_backup_check CHECK (config_backup IN ({BACKUP_MODES}))",
    "ALTER TABLE devices ADD COLUMN config_backup_driver varchar(64) NULL",
    "ALTER TABLE devices ADD COLUMN newdev_excluded_ports integer[] NOT NULL DEFAULT '{}' "
    "CONSTRAINT devices_newdev_excluded_ports_check "
    "CHECK (cardinality(newdev_excluded_ports) <= 256)",
    # 6. per-switch "first FDB read since the new-device feature was turned on"
    "ALTER TABLE port_inventory_status ADD COLUMN newdev_baselined_at timestamptz NULL",
    # 7. config backup
    """
    CREATE TABLE ssh_host_keys (
        device_id integer PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
        algorithm varchar(64) NOT NULL,
        fingerprint varchar(64) NOT NULL,
        public_key text NOT NULL,
        state text NOT NULL DEFAULT 'pinned' CONSTRAINT ssh_host_keys_state_check
            CHECK (state IN ('pinned','changed')),
        presented_algorithm varchar(64) NULL,
        presented_fingerprint varchar(64) NULL,
        presented_public_key text NULL,
        pinned_at timestamptz NOT NULL,
        changed_at timestamptz NULL,
        accepted_by varchar(64) NULL
    )""",
    f"""
    CREATE TABLE config_backup_status (
        device_id integer PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
        driver varchar(64) NULL,
        last_attempt_at timestamptz NULL,
        last_result text NULL CONSTRAINT config_backup_status_last_result_check
            CHECK (last_result IN ({BACKUP_RESULTS})),
        last_error text NULL CONSTRAINT config_backup_status_last_error_check
            CHECK (last_error IN ({BACKUP_ERRORS})),
        last_message varchar(500) NULL,
        last_trigger text NULL CONSTRAINT config_backup_status_last_trigger_check
            CHECK (last_trigger IN ({BACKUP_TRIGGERS})),
        last_duration_ms integer NULL,
        last_success_at timestamptz NULL,
        last_change_at timestamptz NULL,
        last_unchanged_at timestamptz NULL,
        consecutive_failures integer NOT NULL DEFAULT 0,
        next_due_at timestamptz NULL,
        pending_trigger boolean NOT NULL DEFAULT false
    )""",
    f"""
    CREATE TABLE config_versions (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        device_id integer NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        collected_at timestamptz NOT NULL,
        trigger text NOT NULL CONSTRAINT config_versions_trigger_check
            CHECK (trigger IN ({BACKUP_TRIGGERS})),
        driver varchar(64) NOT NULL,
        sha256 char(64) NOT NULL,
        content text NOT NULL,
        masked boolean NOT NULL,
        size_bytes integer NOT NULL,
        lines integer NOT NULL,
        added integer NOT NULL DEFAULT 0,
        removed integer NOT NULL DEFAULT 0,
        initial boolean NOT NULL,
        is_latest boolean NOT NULL
    )""",
    "CREATE INDEX ix_config_versions_device ON config_versions (device_id, collected_at DESC)",
    "CREATE UNIQUE INDEX ux_config_versions_latest ON config_versions (device_id) WHERE is_latest",
    # 8. flow collector (hypertables / hourly objects: bootstrap)
    f"""
    CREATE TABLE flow_exporters (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        address inet NOT NULL CONSTRAINT flow_exporters_address_key UNIQUE,
        device_id integer NULL REFERENCES devices(id) ON DELETE SET NULL,
        state text NOT NULL CONSTRAINT flow_exporters_state_check
            CHECK (state IN ('accepted','pending','ignored')),
        protocols text[] NOT NULL DEFAULT '{{}}' CONSTRAINT flow_exporters_protocols_check
            CHECK (protocols <@ ARRAY[{FLOW_PROTOCOLS}]::text[]),
        sampling_override integer NULL CONSTRAINT flow_exporters_sampling_override_check
            CHECK (sampling_override BETWEEN 1 AND 1000000),
        sampling_seen integer NULL,
        first_seen timestamptz NOT NULL,
        last_seen timestamptz NOT NULL,
        records_total bigint NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX ix_flow_exporters_device_id ON flow_exporters (device_id)",
    """
    CREATE TABLE flow_if_1m (
        time timestamptz NOT NULL,
        exporter_id integer NOT NULL,
        if_index integer NOT NULL,
        direction text NOT NULL CONSTRAINT flow_if_1m_direction_check
            CHECK (direction IN ('in','out')),
        bytes bigint NOT NULL,
        packets bigint NOT NULL,
        flows integer NOT NULL
    )""",
    "CREATE UNIQUE INDEX ux_flow_if_1m_key ON flow_if_1m (exporter_id, if_index, direction, time)",
    "CREATE INDEX ix_flow_if_1m_time ON flow_if_1m (time DESC)",
    """
    CREATE TABLE flow_app_1m (
        time timestamptz NOT NULL,
        exporter_id integer NOT NULL,
        protocol smallint NOT NULL,
        port integer NOT NULL,
        bytes bigint NOT NULL,
        packets bigint NOT NULL,
        flows integer NOT NULL
    )""",
    "CREATE UNIQUE INDEX ux_flow_app_1m_key ON flow_app_1m (exporter_id, protocol, port, time)",
    "CREATE INDEX ix_flow_app_1m_time ON flow_app_1m (time DESC)",
    """
    CREATE TABLE flow_host_1m (
        time timestamptz NOT NULL,
        exporter_id integer NOT NULL,
        ip inet NOT NULL,
        direction text NOT NULL CONSTRAINT flow_host_1m_direction_check
            CHECK (direction IN ('src','dst')),
        other boolean NOT NULL DEFAULT false,
        bytes bigint NOT NULL,
        packets bigint NOT NULL,
        flows integer NOT NULL
    )""",
    "CREATE UNIQUE INDEX ux_flow_host_1m_key ON flow_host_1m "
    "(exporter_id, ip, direction, other, time)",
    "CREATE INDEX ix_flow_host_1m_time ON flow_host_1m (time DESC)",
    "CREATE INDEX ix_flow_host_1m_ip ON flow_host_1m (ip, time DESC)",
    """
    CREATE TABLE flow_conv_1m (
        time timestamptz NOT NULL,
        exporter_id integer NOT NULL,
        in_if integer NOT NULL,
        out_if integer NOT NULL,
        src_ip inet NOT NULL,
        dst_ip inet NOT NULL,
        protocol smallint NOT NULL,
        src_port integer NOT NULL,
        dst_port integer NOT NULL,
        other boolean NOT NULL DEFAULT false,
        bytes bigint NOT NULL,
        packets bigint NOT NULL,
        flows integer NOT NULL
    )""",
    "CREATE INDEX ix_flow_conv_1m_exporter_time ON flow_conv_1m (exporter_id, time DESC)",
    # 9. new-device alerts
    f"""
    CREATE TABLE known_macs (
        mac varchar(17) PRIMARY KEY,
        state text NOT NULL CONSTRAINT known_macs_state_check
            CHECK (state IN ({KNOWN_MAC_STATES})),
        vendor varchar(64) NULL,
        first_seen timestamptz NOT NULL,
        first_device_id integer NULL REFERENCES devices(id) ON DELETE SET NULL,
        first_if_index integer NULL,
        first_port_name varchar(255) NULL,
        first_vlan integer NULL,
        first_ip varchar(45) NULL,
        first_hostname varchar(255) NULL,
        last_seen timestamptz NOT NULL,
        last_device_id integer NULL REFERENCES devices(id) ON DELETE SET NULL,
        last_if_index integer NULL,
        last_vlan integer NULL,
        detected_at timestamptz NULL,
        ack_at timestamptz NULL,
        ack_by varchar(64) NULL,
        ack_comment varchar(500) NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX ix_known_macs_state ON known_macs (state, detected_at DESC)",
    "CREATE INDEX ix_known_macs_first_seen ON known_macs (first_seen DESC)",
    "CREATE INDEX ix_known_macs_last_device_id ON known_macs (last_device_id)",
    "CREATE INDEX ix_known_macs_first_device_id ON known_macs (first_device_id)",
    """
    CREATE TABLE mac_allowlist (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        prefix varchar(17) NOT NULL CONSTRAINT mac_allowlist_prefix_key UNIQUE,
        bits smallint NOT NULL CONSTRAINT mac_allowlist_bits_check
            CHECK (bits IN (24, 28, 36, 48)),
        comment varchar(200) NOT NULL DEFAULT '',
        created_by varchar(64) NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    # 10. port flapping episodes (reports)
    """
    CREATE TABLE port_flap_episodes (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        device_id integer NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        if_index integer NOT NULL,
        port_name varchar(255) NOT NULL DEFAULT '',
        started_at timestamptz NOT NULL,
        ended_at timestamptz NULL,
        transitions integer NOT NULL
    )""",
    "CREATE INDEX ix_port_flap_episodes_started ON port_flap_episodes (started_at DESC)",
    "CREATE INDEX ix_port_flap_episodes_device ON port_flap_episodes (device_id, started_at DESC)",
    # 11. reports
    f"""
    CREATE TABLE reports (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name varchar(128) NOT NULL,
        enabled boolean NOT NULL DEFAULT true,
        language text NOT NULL CONSTRAINT reports_language_check CHECK (language IN ({LANGUAGES})),
        scope_group_ids integer[] NOT NULL DEFAULT '{{}}',
        scope_device_ids integer[] NOT NULL DEFAULT '{{}}',
        sections text[] NOT NULL CONSTRAINT reports_sections_check
            CHECK (cardinality(sections) >= 1 AND sections <@ ARRAY[{REPORT_SECTIONS}]::text[]),
        schedule text NOT NULL CONSTRAINT reports_schedule_check
            CHECK (schedule IN ('weekly','monthly','none')),
        weekday smallint NULL CONSTRAINT reports_weekday_check CHECK (weekday BETWEEN 1 AND 7),
        month_day smallint NULL CONSTRAINT reports_month_day_check
            CHECK (month_day BETWEEN 1 AND 28),
        send_time varchar(5) NULL CONSTRAINT reports_send_time_check
            CHECK (send_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
        email_to text[] NOT NULL DEFAULT '{{}}' CONSTRAINT reports_email_to_check
            CHECK (cardinality(email_to) <= 50),
        channel_ids integer[] NOT NULL DEFAULT '{{}}',
        next_run_at timestamptz NULL,
        last_run_id bigint NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT reports_schedule_fields_check CHECK (
            (schedule = 'weekly') = (weekday IS NOT NULL)
            AND (schedule = 'monthly') = (month_day IS NOT NULL)
            AND (schedule IN ('weekly','monthly')) = (send_time IS NOT NULL))
    )""",
    "CREATE UNIQUE INDEX ux_reports_name ON reports (lower(name))",
    f"""
    CREATE TABLE report_runs (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        report_id integer NULL REFERENCES reports(id) ON DELETE SET NULL,
        report_name varchar(128) NOT NULL,
        trigger text NOT NULL CONSTRAINT report_runs_trigger_check
            CHECK (trigger IN ('schedule','manual')),
        period_start timestamptz NOT NULL,
        period_end timestamptz NOT NULL,
        started_at timestamptz NOT NULL,
        finished_at timestamptz NULL,
        status text NOT NULL CONSTRAINT report_runs_status_check
            CHECK (status IN ('running','ok','partial','failed')),
        language text NOT NULL CONSTRAINT report_runs_language_check
            CHECK (language IN ({LANGUAGES})),
        data jsonb NOT NULL,
        html text NULL,
        deliveries jsonb NOT NULL DEFAULT '[]',
        created_by varchar(64) NULL
    )""",
    "CREATE INDEX ix_report_runs_report ON report_runs (report_id, started_at DESC)",
    "CREATE INDEX ix_report_runs_started ON report_runs (started_at DESC)",
    # 12. NOC kiosk screens
    f"""
    CREATE TABLE kiosk_screens (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name varchar(128) NOT NULL CONSTRAINT kiosk_screens_name_key UNIQUE,
        enabled boolean NOT NULL DEFAULT true,
        token_hash char(64) NOT NULL CONSTRAINT kiosk_screens_token_hash_key UNIQUE,
        token_prefix varchar(12) NOT NULL,
        allowed_sources text[] NOT NULL CONSTRAINT kiosk_screens_allowed_sources_check
            CHECK (cardinality(allowed_sources) BETWEEN 1 AND 64),
        scope_group_id integer NULL REFERENCES groups(id) ON DELETE SET NULL,
        widgets text[] NOT NULL CONSTRAINT kiosk_screens_widgets_check
            CHECK (cardinality(widgets) >= 1 AND widgets <@ ARRAY[{KIOSK_WIDGETS}]::text[]),
        language text NOT NULL CONSTRAINT kiosk_screens_language_check
            CHECK (language IN ({LANGUAGES})),
        show_addresses boolean NOT NULL DEFAULT false,
        refresh_s integer NOT NULL DEFAULT 30 CONSTRAINT kiosk_screens_refresh_s_check
            CHECK (refresh_s BETWEEN 10 AND 300),
        last_seen_at timestamptz NULL,
        last_seen_ip varchar(45) NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX ix_kiosk_screens_scope_group_id ON kiosk_screens (scope_group_id)",
    # 13. chat acknowledge
    """
    CREATE TABLE chat_identities (
        id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        provider text NOT NULL CONSTRAINT chat_identities_provider_check
            CHECK (provider IN ('telegram')),
        external_id varchar(64) NOT NULL,
        display varchar(128) NOT NULL DEFAULT '',
        linked_at timestamptz NOT NULL,
        last_used_at timestamptz NULL,
        CONSTRAINT chat_identities_provider_external_id_key UNIQUE (provider, external_id)
    )""",
    "CREATE INDEX ix_chat_identities_user_id ON chat_identities (user_id)",
    """
    CREATE TABLE chat_link_codes (
        code_hash char(64) PRIMARY KEY,
        user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        provider text NOT NULL CONSTRAINT chat_link_codes_provider_check
            CHECK (provider IN ('telegram')),
        expires_at timestamptz NOT NULL,
        used_at timestamptz NULL
    )""",
    "CREATE INDEX ix_chat_link_codes_user_id ON chat_link_codes (user_id)",
    """
    CREATE TABLE telegram_poll_state (
        bot_key char(64) PRIMARY KEY,
        bot_username varchar(64) NULL,
        next_offset bigint NOT NULL DEFAULT 0,
        state text NOT NULL DEFAULT 'idle' CONSTRAINT telegram_poll_state_state_check
            CHECK (state IN ('idle','polling','error')),
        last_error varchar(500) NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        if stmt == SEED_BUILTIN_RULES:
            for rule in BUILTIN_BACKUP_RULES:
                op.execute(sa.text(INSERT_RULE).bindparams(**rule))
        else:
            op.execute(stmt)


# hourly flow objects: continuous aggregates (Timescale) or plain tables (fallback), whichever
# the bootstrap created
DROP_HOURLY_FLOWS = """
DO $$
DECLARE
    n text;
BEGIN
    FOREACH n IN ARRAY ARRAY['flow_if_1h', 'flow_app_1h', 'flow_host_1h'] LOOP
        BEGIN
            EXECUTE format('DROP MATERIALIZED VIEW IF EXISTS %I CASCADE', n);
        EXCEPTION
            WHEN wrong_object_type OR feature_not_supported
                OR object_not_in_prerequisite_state THEN NULL;
        END;
        BEGIN
            EXECUTE format('DROP TABLE IF EXISTS %I CASCADE', n);
        EXCEPTION WHEN wrong_object_type THEN NULL;
        END;
        BEGIN
            EXECUTE format('DROP VIEW IF EXISTS %I CASCADE', n);
        EXCEPTION WHEN wrong_object_type THEN NULL;
        END;
    END LOOP;
END
$$
"""

DOWNGRADE = [
    DROP_HOURLY_FLOWS,
    # 13 -> 7
    "DROP TABLE IF EXISTS telegram_poll_state",
    "DROP TABLE IF EXISTS chat_link_codes",
    "DROP TABLE IF EXISTS chat_identities",
    "DROP TABLE IF EXISTS kiosk_screens",
    "DROP TABLE IF EXISTS report_runs",
    "DROP TABLE IF EXISTS reports",
    "DROP TABLE IF EXISTS port_flap_episodes",
    "DROP TABLE IF EXISTS mac_allowlist",
    "DROP TABLE IF EXISTS known_macs",
    "DROP TABLE IF EXISTS flow_conv_1m",
    "DROP TABLE IF EXISTS flow_host_1m",
    "DROP TABLE IF EXISTS flow_app_1m",
    "DROP TABLE IF EXISTS flow_if_1m",
    "DROP TABLE IF EXISTS flow_exporters",
    "DROP TABLE IF EXISTS config_versions",
    "DROP TABLE IF EXISTS config_backup_status",
    "DROP TABLE IF EXISTS ssh_host_keys",
    # 6, 5
    "ALTER TABLE port_inventory_status DROP COLUMN IF EXISTS newdev_baselined_at",
    "ALTER TABLE devices DROP COLUMN IF EXISTS newdev_excluded_ports, "
    "DROP COLUMN IF EXISTS config_backup_driver, DROP COLUMN IF EXISTS config_backup, "
    "DROP COLUMN IF EXISTS ssh_credentials",
    "ALTER TABLE groups DROP COLUMN IF EXISTS ssh_credentials",
    # 4. rules and conditions
    "DELETE FROM event_rules WHERE action = 'config_backup'",
    "ALTER TABLE event_rules DROP CONSTRAINT IF EXISTS event_rules_backup_action_check",
    "DELETE FROM event_conditions WHERE source IN "
    "('backup_failed','host_key','config_changed','new_mac')",
    # 4b. audit sources (the append-only trigger is disabled for this one statement)
    "ALTER TABLE audit_log DISABLE TRIGGER audit_log_immutable",
    "UPDATE audit_log SET source = NULL WHERE source = 'oidc'",
    "ALTER TABLE audit_log ENABLE TRIGGER audit_log_immutable",
    "ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_source_check",
    "ALTER TABLE audit_log ADD CONSTRAINT audit_log_source_check "
    "CHECK (source IN ('local','ldap','token'))",
    # 3. OIDC users (their FKs are SET NULL / CASCADE as today)
    "DELETE FROM users WHERE source = 'oidc'",
    # 2. events
    "DELETE FROM events WHERE kind IN ('config_change','new_device')",
    # 1. sensors (channels, conditions and alert state cascade)
    "DELETE FROM sensors WHERE type IN ('config_backup','new_mac')",
    *_checks(
        OLD_SENSOR_TYPES,
        OLD_PASSIVE,
        OLD_EVENT_KINDS,
        OLD_USER_SOURCES,
        OLD_USERNAME_CHECK,
        OLD_COND_SOURCES,
        OLD_RULE_ACTIONS,
    ),
]


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
