"""Phase 3b: the "Ask the network" assistant (Amazon Bedrock) and the AI features.

Conversations, messages, tool calls, usage counters, AI artifacts, incidents, alert-note
targets, syslog signatures, SNMP walks; `alert_rules.ai_note`, `reports.ai_summary`,
`report_runs.ai_summary_status`; audit source `telegram`. Plain PostgreSQL DDL only
(data-model "[3b] Phase 3b migration (0008)").

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-10
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

PURPOSES = (
    "'ask','explain','incident','alert_note','diff_explain','digest','rule_draft',"
    "'template_draft','chat_ask','report_summary','test'"
)
CHANNELS = "'web','telegram'"
CONVERSATION_KINDS = "'chat','explain','rule_draft','template_draft'"
MESSAGE_ROLES = "'user','assistant'"
MESSAGE_STATUSES = "'running','complete','cancelled','error','limit_reached'"
ARTIFACT_KINDS = (
    "'explain','incident_summary','alert_note','diff_explain','digest','rule_draft',"
    "'template_draft','report_summary'"
)
ARTIFACT_STATUSES = "'ok','failed','skipped','running'"
INCIDENT_STATES = "'open','resolved','stale'"
WALK_STATUSES = "'running','ok','partial','failed','cancelled'"
NOTE_CHANNEL_TYPES = "'telegram','teams','email'"
AI_SUMMARY_STATUSES = "'ok','failed','skipped_cap','timeout','unverified_numbers','disabled'"
OLD_AUDIT_SOURCES = "'local','ldap','token','oidc'"
NEW_AUDIT_SOURCES = OLD_AUDIT_SOURCES + ",'telegram'"

UPGRADE = [
    # 1. conversations, messages, tool calls
    f"""
    CREATE TABLE assistant_conversations (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind text NOT NULL DEFAULT 'chat' CONSTRAINT assistant_conversations_kind_check
            CHECK (kind IN ({CONVERSATION_KINDS})),
        channel text NOT NULL DEFAULT 'web' CONSTRAINT assistant_conversations_channel_check
            CHECK (channel IN ({CHANNELS})),
        title varchar(200) NOT NULL,
        language varchar(2) NOT NULL DEFAULT 'en',
        ref_sensor_id integer NULL REFERENCES sensors(id) ON DELETE SET NULL,
        ref_event_id bigint NULL,
        redaction_map jsonb NOT NULL DEFAULT '{{}}',
        known_refs text[] NOT NULL DEFAULT '{{}}',
        message_count integer NOT NULL DEFAULT 0,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        last_message_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX ix_assistant_conversations_user ON assistant_conversations "
    "(user_id, last_message_at DESC)",
    "CREATE INDEX ix_assistant_conversations_last ON assistant_conversations (last_message_at)",
    "CREATE INDEX ix_assistant_conversations_explain ON assistant_conversations "
    "(ref_sensor_id, ref_event_id) WHERE kind = 'explain'",
    f"""
    CREATE TABLE assistant_messages (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        conversation_id bigint NOT NULL REFERENCES assistant_conversations(id) ON DELETE CASCADE,
        seq integer NOT NULL,
        role text NOT NULL CONSTRAINT assistant_messages_role_check
            CHECK (role IN ({MESSAGE_ROLES})),
        purpose text NOT NULL CONSTRAINT assistant_messages_purpose_check
            CHECK (purpose IN ({PURPOSES})),
        content text NOT NULL DEFAULT '',
        notes jsonb NOT NULL DEFAULT '[]',
        sources jsonb NOT NULL DEFAULT '[]',
        converse_turns jsonb NULL,
        status text NOT NULL DEFAULT 'complete' CONSTRAINT assistant_messages_status_check
            CHECK (status IN ({MESSAGE_STATUSES})),
        error_code varchar(64) NULL,
        notices text[] NOT NULL DEFAULT '{{}}',
        stop_reason varchar(32) NULL,
        model_id varchar(256) NULL,
        model_calls integer NOT NULL DEFAULT 0,
        tool_calls integer NOT NULL DEFAULT 0,
        input_tokens integer NOT NULL DEFAULT 0,
        output_tokens integer NOT NULL DEFAULT 0,
        cache_read_tokens integer NOT NULL DEFAULT 0,
        cache_write_tokens integer NOT NULL DEFAULT 0,
        cost_estimate numeric(12,6) NULL,
        duration_ms integer NULL,
        feedback smallint NULL CONSTRAINT assistant_messages_feedback_check
            CHECK (feedback IN (-1, 1)),
        feedback_comment varchar(1000) NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz NULL,
        CONSTRAINT assistant_messages_conversation_seq_key UNIQUE (conversation_id, seq)
    )""",
    """
    CREATE TABLE assistant_tool_calls (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        message_id bigint NOT NULL REFERENCES assistant_messages(id) ON DELETE CASCADE,
        seq integer NOT NULL,
        round smallint NOT NULL,
        tool_use_id varchar(64) NOT NULL,
        tool varchar(64) NOT NULL,
        arguments jsonb NOT NULL,
        ok boolean NOT NULL,
        error_code varchar(64) NULL,
        rows integer NULL,
        result_bytes integer NOT NULL DEFAULT 0,
        truncated boolean NOT NULL DEFAULT false,
        duration_ms integer NOT NULL,
        started_at timestamptz NOT NULL
    )""",
    "CREATE INDEX ix_assistant_tool_calls_message ON assistant_tool_calls (message_id, seq)",
    # 2. usage counters (user_id 0 = system; no FK so the rows survive user deletion)
    f"""
    CREATE TABLE assistant_usage (
        day date NOT NULL,
        user_id integer NOT NULL,
        purpose text NOT NULL CONSTRAINT assistant_usage_purpose_check
            CHECK (purpose IN ({PURPOSES})),
        questions integer NOT NULL DEFAULT 0,
        model_calls integer NOT NULL DEFAULT 0,
        tool_calls integer NOT NULL DEFAULT 0,
        errors integer NOT NULL DEFAULT 0,
        throttles integer NOT NULL DEFAULT 0,
        input_tokens bigint NOT NULL DEFAULT 0,
        output_tokens bigint NOT NULL DEFAULT 0,
        cache_read_tokens bigint NOT NULL DEFAULT 0,
        cache_write_tokens bigint NOT NULL DEFAULT 0,
        cost_estimate numeric(14,6) NOT NULL DEFAULT 0,
        PRIMARY KEY (day, user_id, purpose)
    )""",
    # 3. artifacts, incidents
    f"""
    CREATE TABLE assistant_artifacts (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        kind text NOT NULL CONSTRAINT assistant_artifacts_kind_check
            CHECK (kind IN ({ARTIFACT_KINDS})),
        key varchar(200) NOT NULL,
        status text NOT NULL CONSTRAINT assistant_artifacts_status_check
            CHECK (status IN ({ARTIFACT_STATUSES})),
        skip_reason varchar(64) NULL,
        content text NOT NULL DEFAULT '',
        data jsonb NOT NULL DEFAULT '{{}}',
        conversation_id bigint NULL REFERENCES assistant_conversations(id) ON DELETE SET NULL,
        created_by integer NULL,
        model_id varchar(256) NULL,
        input_tokens integer NOT NULL DEFAULT 0,
        output_tokens integer NOT NULL DEFAULT 0,
        cost_estimate numeric(12,6) NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT assistant_artifacts_kind_key_key UNIQUE (kind, key)
    )""",
    "CREATE INDEX ix_assistant_artifacts_created ON assistant_artifacts (created_at)",
    f"""
    CREATE TABLE assistant_incidents (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        state text NOT NULL CONSTRAINT assistant_incidents_state_check
            CHECK (state IN ({INCIDENT_STATES})),
        opened_at timestamptz NOT NULL,
        last_change_at timestamptz NOT NULL,
        resolved_at timestamptz NULL,
        title varchar(200) NOT NULL,
        title_fields jsonb NOT NULL,
        root_device_id integer NULL REFERENCES devices(id) ON DELETE SET NULL,
        root_candidates integer[] NOT NULL DEFAULT '{{}}',
        ai_title varchar(200) NULL,
        ai_summary text NULL,
        ai_root_guess jsonb NULL,
        ai_summaries smallint NOT NULL DEFAULT 0 CONSTRAINT assistant_incidents_ai_summaries_check
            CHECK (ai_summaries BETWEEN 0 AND 5),
        member_count integer NOT NULL DEFAULT 0,
        note_sent boolean NOT NULL DEFAULT false
    )""",
    "CREATE INDEX ix_assistant_incidents_state ON assistant_incidents (state, last_change_at DESC)",
    "CREATE INDEX ix_assistant_incidents_opened ON assistant_incidents (opened_at DESC)",
    """
    CREATE TABLE assistant_incident_members (
        incident_id bigint NOT NULL REFERENCES assistant_incidents(id) ON DELETE CASCADE,
        event_id bigint NOT NULL,
        sensor_id integer NULL,
        device_id integer NULL,
        status text NULL,
        time timestamptz NULL,
        recovered_at timestamptz NULL,
        PRIMARY KEY (incident_id, event_id)
    )""",
    "CREATE INDEX ix_assistant_incident_members_sensor ON assistant_incident_members "
    "(sensor_id, time DESC)",
    # 4. note targets, syslog signatures, SNMP walks
    f"""
    CREATE TABLE assistant_note_targets (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        event_id bigint NOT NULL,
        rule_id integer NULL REFERENCES alert_rules(id) ON DELETE SET NULL,
        channel_id integer NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
        channel_type text NOT NULL CONSTRAINT assistant_note_targets_channel_type_check
            CHECK (channel_type IN ({NOTE_CHANNEL_TYPES})),
        ref jsonb NOT NULL,
        sent_at timestamptz NOT NULL,
        note_status text NULL
    )""",
    "CREATE INDEX ix_assistant_note_targets_event ON assistant_note_targets (event_id)",
    """
    CREATE TABLE assistant_syslog_signatures (
        signature char(16) PRIMARY KEY,
        sample varchar(300) NOT NULL,
        first_seen date NOT NULL,
        last_seen date NOT NULL,
        days_seen integer NOT NULL DEFAULT 1,
        count bigint NOT NULL DEFAULT 0
    )""",
    f"""
    CREATE TABLE assistant_snmp_walks (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        device_id integer NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        requested_by integer NULL,
        status text NOT NULL CONSTRAINT assistant_snmp_walks_status_check
            CHECK (status IN ({WALK_STATUSES})),
        subtrees text[] NOT NULL,
        varbinds jsonb NOT NULL DEFAULT '[]',
        counts jsonb NOT NULL DEFAULT '{{}}',
        error varchar(300) NULL,
        started_at timestamptz NOT NULL,
        finished_at timestamptz NULL
    )""",
    "CREATE INDEX ix_assistant_snmp_walks_device ON assistant_snmp_walks "
    "(device_id, started_at DESC)",
    # 5. column additions
    "ALTER TABLE alert_rules ADD COLUMN ai_note boolean NOT NULL DEFAULT false",
    "ALTER TABLE reports ADD COLUMN ai_summary boolean NOT NULL DEFAULT false",
    "ALTER TABLE report_runs ADD COLUMN ai_summary_status text NULL "
    "CONSTRAINT report_runs_ai_summary_status_check "
    f"CHECK (ai_summary_status IN ({AI_SUMMARY_STATUSES}))",
    # 6. audit source 'telegram' (Telegram /ask questions)
    "ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_source_check",
    "ALTER TABLE audit_log ADD CONSTRAINT audit_log_source_check "
    f"CHECK (source IN ({NEW_AUDIT_SOURCES}))",
]

DOWNGRADE = [
    # 6. audit sources (the append-only trigger is disabled for this one statement)
    "ALTER TABLE audit_log DISABLE TRIGGER audit_log_immutable",
    "UPDATE audit_log SET source = NULL WHERE source = 'telegram'",
    "ALTER TABLE audit_log ENABLE TRIGGER audit_log_immutable",
    "ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_source_check",
    "ALTER TABLE audit_log ADD CONSTRAINT audit_log_source_check "
    f"CHECK (source IN ({OLD_AUDIT_SOURCES}))",
    # 5.
    "ALTER TABLE report_runs DROP COLUMN IF EXISTS ai_summary_status",
    "ALTER TABLE reports DROP COLUMN IF EXISTS ai_summary",
    "ALTER TABLE alert_rules DROP COLUMN IF EXISTS ai_note",
    # 4 -> 1
    "DROP TABLE IF EXISTS assistant_snmp_walks",
    "DROP TABLE IF EXISTS assistant_syslog_signatures",
    "DROP TABLE IF EXISTS assistant_note_targets",
    "DROP TABLE IF EXISTS assistant_incident_members",
    "DROP TABLE IF EXISTS assistant_incidents",
    "DROP TABLE IF EXISTS assistant_artifacts",
    "DROP TABLE IF EXISTS assistant_usage",
    "DROP TABLE IF EXISTS assistant_tool_calls",
    "DROP TABLE IF EXISTS assistant_messages",
    "DROP TABLE IF EXISTS assistant_conversations",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
