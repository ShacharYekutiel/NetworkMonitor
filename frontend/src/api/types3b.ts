// [3b] Hand-written mirror of docs/api.md "[3b] Phase 3b contract". Field names are exact.

export const AI_FEATURES = [
  'ai_explain_alert',
  'ai_correlation',
  'ai_alert_notes',
  'ai_config_diff',
  'ai_syslog_digest',
  'ai_rule_drafts',
  'ai_template_drafts',
  'ai_knowledge',
  'ai_chat_ask',
  'ai_report_summary',
] as const;
export type AiFeatureName = (typeof AI_FEATURES)[number];
export type AssistantFeatureName = 'assistant' | AiFeatureName;

export type AssistantState = 'disabled' | 'unconfigured' | 'ready' | 'degraded' | 'error';
export const ASSISTANT_STATES: AssistantState[] = [
  'disabled',
  'unconfigured',
  'ready',
  'degraded',
  'error',
];
export type AuthMode = 'roles_anywhere' | 'access_key' | 'default_chain';
export const AUTH_MODES: AuthMode[] = ['roles_anywhere', 'access_key', 'default_chain'];
export type ProxyMode = 'env' | 'direct' | 'custom';
export const PROXY_MODES: ProxyMode[] = ['env', 'direct', 'custom'];

export const ASSISTANT_WARNINGS = [
  'cert_expiring',
  'vpce_via_proxy',
  'stream_buffered',
  'cache_unsupported',
  'access_key_old',
  'key_file_mode',
  'temporary_credentials',
] as const;
export type AssistantWarning = (typeof ASSISTANT_WARNINGS)[number];

export type CertInfo = {
  source: 'uploaded' | 'files';
  subject: string;
  issuer: string;
  serial_hex: string;
  serial_decimal: string;
  not_before: string;
  not_after: string;
  key_type: 'rsa' | 'ec';
  key_bits: number;
  sha256: string;
  chain: { subject: string; not_after: string }[];
  uploaded_at: string | null;
  key_unreadable: boolean;
};

export type AssistantPrices = {
  currency: string;
  input_per_mtok: number | null;
  output_per_mtok: number | null;
  cache_read_per_mtok: number | null;
  cache_write_per_mtok: number | null;
};

export type AssistantSettings = {
  enabled: boolean;
  state: AssistantState;
  state_error: { code: string; message: string; at: string } | null;
  auth_mode: AuthMode;
  region: string;
  model_id: string;
  model_id_light: string | null;
  endpoint_url: string | null;
  control_endpoint_url: string | null;
  proxy_mode: ProxyMode;
  proxy_url: string | null;
  prompt_caching: boolean;
  access_key_id: string | null;
  secret_access_key: null;
  secret_access_key_set: boolean;
  secret_access_key_unreadable: boolean;
  /** [3b] optional session token of temporary (ASIA…) keys — write-only like the secret */
  session_token?: null;
  session_token_set?: boolean;
  session_token_unreadable?: boolean;
  access_key_created_at: string | null;
  ra_region: string | null;
  ra_endpoint_url: string | null;
  ra_trust_anchor_arn: string | null;
  ra_profile_arn: string | null;
  ra_role_arn: string | null;
  ra_session_seconds: number;
  certificate: CertInfo | null;
  send_config_excerpts: boolean;
  redact_internal_ips: boolean;
  internal_cidrs: string[];
  chat_redact_ips: boolean;
  prices: AssistantPrices;
  /** [3b owner] models offered in the chat ([] = main + light); not a connection field */
  chat_models?: ChatModel[];
  /** the allowed chat models: main model first (the default), then the configured ones */
  chat_models_effective?: ChatModel[];
  last_test: { ok: boolean; at: string; by: string; error_code: string | null } | null;
  warnings: AssistantWarning[];
  /** [3b owner decision] pending CSR (key generated on the server) */
  csr?: CsrResult | null;
  certificate_host_files?: boolean;
};

/** Writable connection fields (hashed for the `test_id`; data-model § [3b] Settings). */
export type AssistantConnectionInput = {
  auth_mode: AuthMode;
  region: string;
  model_id: string;
  model_id_light: string | null;
  endpoint_url: string | null;
  control_endpoint_url: string | null;
  proxy_mode: ProxyMode;
  proxy_url: string | null;
  prompt_caching: boolean;
  access_key_id: string | null;
  /** omitted/null keeps, "" clears, a string replaces */
  secret_access_key?: string | null;
  /** omitted/null keeps, "" clears, a string replaces (temporary ASIA… keys) */
  session_token?: string | null;
  access_key_created_at: string | null;
  ra_region: string | null;
  ra_endpoint_url: string | null;
  ra_trust_anchor_arn: string | null;
  ra_profile_arn: string | null;
  ra_role_arn: string | null;
  ra_session_seconds: number;
};

export type AssistantSettingsInput = Partial<AssistantConnectionInput> & {
  send_config_excerpts?: boolean;
  redact_internal_ips?: boolean;
  internal_cidrs?: string[];
  chat_redact_ips?: boolean;
  prices?: AssistantPrices;
  chat_models?: ChatModel[];
  enabled?: boolean;
  test_id?: string;
};

export type AssistantTestStepName =
  'config' | 'credentials' | 'endpoint' | 'converse' | 'knowledge';
export const ASSISTANT_TEST_STEPS: AssistantTestStepName[] = [
  'config',
  'credentials',
  'endpoint',
  'converse',
  'knowledge',
];
export type AssistantTestStep = {
  step: AssistantTestStepName;
  ok: boolean;
  detail: string;
  ms: number;
  error_code: string | null;
};
export type AssistantTestResult = {
  ok: boolean;
  test_id: string | null;
  expires_at: string | null;
  steps: AssistantTestStep[];
  via: string;
  endpoint_host: string;
  address_class: 'private' | 'public' | null;
  model_id: string;
  time_to_first_event_ms: number | null;
  usage: { input_tokens: number; output_tokens: number } | null;
  warnings: string[];
  error: { code: string; message: string } | null;
};

export type ModelListItem = {
  id: string;
  name: string;
  kind: 'inference_profile' | 'foundation_model';
  streaming: boolean;
  status: string;
};
export type ModelList = { items: ModelListItem[]; error: { code: string; message: string } | null };

/**
 * [3b owner decision, not in api.md yet; shape of the backend implementation] "Generate CSR":
 * `POST /api/assistant/cert/csr` creates the key pair on the server (kept encrypted, pending) and
 * returns the CSR; the issued certificate is then uploaded through `POST /api/assistant/cert`
 * without a key. `GET` downloads the pending CSR, `DELETE` discards it.
 */
export type CsrRequest = {
  common_name: string;
  key_type: 'rsa' | 'ec';
  key_size?: number | null;
  organization?: string | null;
  organizational_unit?: string | null;
};
export type CsrResult = {
  subject: string | null;
  created_at: string | null;
  pem: string | null;
  key_unreadable?: boolean;
};

export type AssistantToolStatus = { name: string; available: boolean; reason: string | null };
export type AssistantFeatureStatus = {
  name: AiFeatureName;
  enabled: boolean;
  idle_reason: string | null;
  status_line: string | null;
};
export type UsageMonthSummary = {
  month: string;
  tokens: number;
  token_cap: number;
  cost_estimate: number | null;
  cost_cap: number | null;
  currency: string | null;
  pct_of_cap: number;
  projection_pct: number;
  background_stopped: boolean;
};
export type AssistantStatus = {
  state: AssistantState;
  state_error: { code: string; message: string; at: string } | null;
  tools: AssistantToolStatus[];
  features: AssistantFeatureStatus[];
  me: { questions_today: number; daily_limit: number; running_message_id: number | null };
  month: UsageMonthSummary;
  /** [3b owner] models the chat composer offers (main model first = default) */
  chat_models?: ChatModel[];
  default_model_id?: string | null;
};

/** [3b owner] a model / inference profile offered in the chat (optional display label). */
export type ChatModel = { id: string; label: string | null };

// Ask, stream, conversations -------------------------------------------------

export type ConversationKind = 'chat' | 'explain' | 'rule_draft' | 'template_draft';
export type AssistantConversation = {
  id: number;
  kind: ConversationKind;
  channel: 'web' | 'telegram';
  title: string;
  language: 'en' | 'he';
  message_count: number;
  ref: { sensor_id: number | null; event_id: number | null } | null;
  created_at: string;
  last_message_at: string;
};
export type AssistantStep = {
  call_id: string;
  round: number;
  tool: string;
  label_key: string;
  args: Record<string, unknown>;
  state: 'running' | 'done';
  ok: boolean | null;
  rows: number | null;
  truncated: boolean;
  ms: number | null;
  error: string | null;
  /** only on the SSE `step` done event */
  refs?: string[];
};
export type MessageStatus = 'running' | 'complete' | 'cancelled' | 'error' | 'limit_reached';
export type AssistantUsage = {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
};
export type AssistantSource = { ref: string; label: string; url: string | null };
export type AssistantMessage = {
  id: number;
  conversation_id: number;
  seq: number;
  role: 'user' | 'assistant';
  purpose: string;
  content: string;
  notes: { round: number; text: string }[];
  steps: AssistantStep[];
  sources: AssistantSource[];
  status: MessageStatus;
  error: { code: string; message: string } | null;
  notices: string[];
  model_id: string | null;
  model_calls: number;
  tool_calls: number;
  usage: AssistantUsage;
  cost_estimate: number | null;
  currency: string | null;
  duration_ms: number | null;
  feedback: 1 | -1 | null;
  feedback_comment: string | null;
  created_at: string;
  finished_at: string | null;
};
export type ConversationDetail = AssistantConversation & { messages: AssistantMessage[] };

export type NoticeCode = 'limit_reached' | 'truncated_context' | 'retrying' | 'kb_unavailable';

/** Parsed SSE events of `POST /api/assistant/ask`, `…/explain` and `…/messages/{id}/stream`. */
export type StreamEvent =
  | {
      event: 'start';
      id: number | null;
      data: {
        conversation_id: number;
        message_id: number;
        user_message_id?: number;
        language: 'en' | 'he';
        redaction: boolean;
      };
    }
  | { event: 'step'; id: number | null; data: AssistantStep }
  | { event: 'delta'; id: number | null; data: { text: string } }
  | { event: 'round_end'; id: number | null; data: { round: number; reason: string } }
  | { event: 'notice'; id: number | null; data: { code: string; message: string } }
  | { event: 'done'; id: number | null; data: { message: AssistantMessage } }
  | {
      event: 'error';
      id: number | null;
      data: { code: string; message: string; details?: Record<string, unknown>[] };
    };

// AI features ------------------------------------------------------------------

export type IncidentState = 'open' | 'resolved' | 'stale';
export const INCIDENT_STATES: IncidentState[] = ['open', 'resolved', 'stale'];
export type Confidence3 = 'low' | 'medium' | 'high';
export type Incident = {
  id: number;
  state: IncidentState;
  opened_at: string;
  last_change_at: string;
  resolved_at: string | null;
  title: string;
  title_fields: {
    root: string;
    root_status: string;
    count: number;
    unit: 'devices' | 'sensors';
    status: string;
  };
  root_device: { id: number; name: string } | null;
  root_candidates: { id: number; name: string }[];
  ai: {
    title: string;
    summary: string;
    root_guess: { device_id: number; confidence: Confidence3 } | null;
    generated_at: string;
  } | null;
  member_count: number;
  down: number;
  unreachable: number;
  warning: number;
  recovered: number;
};
export type IncidentMember = {
  event_id: number;
  sensor_id: number | null;
  sensor_name: string | null;
  device_id: number | null;
  device_name: string | null;
  status: string;
  time: string;
  recovered_at: string | null;
  is_root: boolean;
};
export type IncidentDetail = Incident & { members: IncidentMember[] };

export type RiskSeverity = 'high' | 'medium' | 'info';
export type DiffFlag = {
  rule_id: string;
  severity: RiskSeverity;
  title_key: string;
  side: 'added' | 'removed';
  lines: number[];
};
export type DiffExplanation = {
  device_id: number;
  from: number;
  to: number;
  flags: DiffFlag[];
  ai: {
    summary: string[];
    risks: { severity: RiskSeverity; title: string; explanation: string; lines: number[] }[];
    confidence: Confidence3;
    truncated: boolean;
  } | null;
  status: 'ok' | 'failed' | 'skipped';
  error: { code: string; message: string } | null;
  generated_at: string;
  usage: { input_tokens: number; output_tokens: number };
  cost_estimate: number | null;
};

export type DigestStatus = 'ok' | 'failed' | 'skipped' | 'running';
export type DigestSummary = {
  day: string;
  status: DigestStatus;
  unusual_count: number;
  new_types: number;
  total_messages: number;
  generated_at: string;
};
export type DigestRow = {
  key: string;
  ref: string | null;
  count: number;
  baseline: number | null;
  ratio: number | null;
  unusual: boolean;
};
export type Digest = DigestSummary & {
  language: 'en' | 'he';
  baseline_days: number;
  stats: {
    by_severity: DigestRow[];
    by_program: DigestRow[];
    by_device: DigestRow[];
    by_hour: [number, number][];
    new_types: {
      signature: string;
      sample: string;
      count: number;
      example: string;
      example_ref: string;
    }[];
    sampled: boolean;
  };
  ai_markdown: string | null;
  deliveries: { target: string; ok: boolean; error: string | null; at: string }[];
};

export type RuleDraftKind = 'alert_rule' | 'event_rule';
/** [3b owner] No draft possible: the model's own answer, or the server's placeholder backstop. */
export type CannotDraft = { reason: string; source: 'model' | 'placeholder' };
export type RuleDraft = {
  kind: RuleDraftKind;
  /** null together with `cannot_draft`. */
  draft: Record<string, unknown> | null;
  cannot_draft?: CannotDraft | null;
  /** Language of name/explanation/assumptions/reason (from the description, else the UI). */
  language?: 'en' | 'he';
  explanation: string;
  assumptions: string[];
  validation: { ok: boolean; errors: { loc: (string | number)[]; msg: string }[] };
  conversation_id: number;
  usage: { input_tokens: number; output_tokens: number };
};

export const WALK_PRESETS = [
  'system',
  'entity',
  'host_resources',
  'enterprise',
  'if_sample',
  'entity_sensor',
] as const;
export type WalkPreset = (typeof WALK_PRESETS)[number];
/** Checked by default (architecture § 14.8.7). */
export const DEFAULT_WALK_PRESETS: WalkPreset[] = [
  'system',
  'entity',
  'host_resources',
  'enterprise',
];
export type WalkStatus = 'running' | 'ok' | 'partial' | 'failed' | 'cancelled';
export type SnmpWalk = {
  id: number;
  device_id: number;
  status: WalkStatus;
  subtrees: string[];
  counts: Record<string, number>;
  total: number;
  error: string | null;
  started_at: string;
  finished_at: string | null;
  varbinds?: [string, string, string][];
};
export type TemplateDraft = {
  walk_id: number;
  yaml: string;
  valid: boolean;
  errors: { path: string; error: string }[];
  warnings: string[];
  notes: string;
  /** yaml is "" when set. */
  cannot_draft?: CannotDraft | null;
  language?: 'en' | 'he';
  repair_rounds: number;
  conversation_id: number;
  usage: { input_tokens: number; output_tokens: number };
};

export type KnowledgeSettings = {
  enabled: boolean;
  knowledge_base_id: string;
  region: string | null;
  endpoint_url: string | null;
  results: number;
  min_score: number;
  description: string;
  last_test: { ok: boolean; at: string; by?: string; error_code: string | null } | null;
};
export type KnowledgeInput = {
  knowledge_base_id: string;
  region: string | null;
  endpoint_url: string | null;
  results: number;
  min_score: number;
  description: string;
};
export type KnowledgeTestResult = {
  ok: boolean;
  test_id: string | null;
  expires_at: string | null;
  results: number;
  first_source: { title: string; url: string | null } | null;
  ms: number;
  error: { code: string; message: string } | null;
};

export type UsageReport = UsageMonthSummary & {
  by_day: { day: string; questions: number; tokens: number; cost_estimate: number | null }[];
  by_user: {
    user_id: number;
    username: string;
    questions: number;
    tokens: number;
    cost_estimate: number | null;
  }[];
  by_purpose: { purpose: string; calls: number; tokens: number; cost_estimate: number | null }[];
  /** [3b owner] per model / inference profile, tokens desc; model_id "" = unknown */
  by_model?: ModelUsage[];
  top_tools: { tool: string; calls: number; errors: number; avg_ms: number }[];
  errors: { code: string; count: number }[];
  feedback: { up: number; down: number };
  last_12_months: { month: string; tokens: number; cost_estimate: number | null }[];
};

export type ModelUsage = {
  model_id: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  tokens: number;
  cost_estimate: number | null;
};

export type ReportAiSummaryStatus =
  'ok' | 'failed' | 'skipped_cap' | 'timeout' | 'unverified_numbers' | 'disabled';
export const REPORT_AI_STATUSES: ReportAiSummaryStatus[] = [
  'ok',
  'failed',
  'skipped_cap',
  'timeout',
  'unverified_numbers',
  'disabled',
];

/** Scalar settings keys (data-model § [3b] Scalar keys). */
export type Settings3b = {
  feature_assistant_enabled: boolean;
  feature_ai_explain_alert_enabled: boolean;
  feature_ai_correlation_enabled: boolean;
  feature_ai_alert_notes_enabled: boolean;
  feature_ai_config_diff_enabled: boolean;
  feature_ai_syslog_digest_enabled: boolean;
  feature_ai_rule_drafts_enabled: boolean;
  feature_ai_template_drafts_enabled: boolean;
  feature_ai_knowledge_enabled: boolean;
  feature_ai_chat_ask_enabled: boolean;
  feature_ai_report_summary_enabled: boolean;
  assistant_daily_questions_per_user: number;
  assistant_monthly_token_cap: number;
  assistant_monthly_cost_cap: number | null;
  assistant_background_stop_pct: number;
  assistant_max_model_calls: number;
  assistant_max_tool_calls: number;
  assistant_max_output_tokens: number;
  assistant_question_timeout_s: number;
  assistant_max_concurrent: number;
  assistant_context_budget_tokens: number;
  assistant_retention_days: number;
  assistant_artifact_retention_days: number;
  correlation_window_min: number;
  correlation_min_alerts: number;
  correlation_attach_to_alert: boolean;
  correlation_daily_summaries: number;
  alert_note_timeout_s: number;
  alert_notes_daily_cap: number;
  config_diff_auto: boolean;
  config_diff_daily_cap: number;
  digest_time: string;
  digest_language: 'en' | 'he' | null;
  digest_email_to: string[];
  digest_channel_ids: number[];
  chat_ask_per_user_hour: number;
};
