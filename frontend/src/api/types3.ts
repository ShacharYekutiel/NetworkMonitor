// [3] Hand-written mirror of docs/api.md "Phase 3". Field names are exact.
import type { Acknowledgement, Language, Sensor, Status, StatusCounts } from './types';
import type { ReceiverStatus } from './types';

// ---------------------------------------------------------------------------
// SSH credentials (groups/devices)

export type SshCredentials = {
  username: string;
  port: number;
  password: null;
  password_set: boolean;
  private_key: null;
  private_key_set: boolean;
  passphrase: null;
  passphrase_set: boolean;
  enable_password: null;
  enable_password_set: boolean;
  legacy_algorithms: boolean;
  credentials_unreadable: boolean;
};

/** Request form: secrets omitted/null keep, "" clears, a string replaces. */
export type SshCredentialsInput = {
  username?: string;
  port?: number;
  password?: string | null;
  private_key?: string | null;
  passphrase?: string | null;
  enable_password?: string | null;
  legacy_algorithms?: boolean;
};

export type BackupMode = 'auto' | 'on' | 'off';
export const BACKUP_MODES: BackupMode[] = ['auto', 'on', 'off'];

// ---------------------------------------------------------------------------
// Config backup

export const BACKUP_REASONS = [
  'ok',
  'disabled_device',
  'mode_off',
  'no_credentials',
  'no_driver',
  'host_key_changed',
] as const;
export type BackupReason = (typeof BACKUP_REASONS)[number];
export const BACKUP_RESULTS = ['ok', 'unchanged', 'failed', 'cancelled'] as const;
export type BackupResult = (typeof BACKUP_RESULTS)[number];
export const BACKUP_TRIGGERS = ['schedule', 'manual', 'event', 'retry'] as const;
export type BackupTrigger = (typeof BACKUP_TRIGGERS)[number];

export type HostKey = {
  algorithm: string;
  fingerprint: string;
  state: 'pinned' | 'changed';
  pinned_at: string;
  presented_algorithm: string | null;
  presented_fingerprint: string | null;
  changed_at: string | null;
  accepted_by: string | null;
};

export type BackupStatus = {
  device_id: number;
  device_name: string;
  group_id: number;
  eligible: boolean;
  reason: BackupReason;
  mode: BackupMode;
  driver: string | null;
  driver_source: 'override' | 'template' | 'sys_object_id' | null;
  last_attempt_at: string | null;
  last_result: BackupResult | null;
  last_error: string | null;
  last_message: string | null;
  last_trigger: BackupTrigger | null;
  last_duration_ms: number | null;
  last_success_at: string | null;
  last_change_at: string | null;
  consecutive_failures: number;
  next_due_at: string | null;
  running: boolean;
  versions: number;
  host_key: HostKey | null;
};

export type ConfigVersion = {
  id: number;
  device_id: number;
  version_no: number;
  collected_at: string;
  trigger: BackupTrigger;
  driver: string;
  sha256: string;
  masked: boolean;
  size_bytes: number;
  lines: number;
  added: number;
  removed: number;
  initial: boolean;
  is_latest: boolean;
};

export type ConfigDriver = {
  id: string;
  name: string;
  version: number;
  confidence: 'verified' | 'unverified';
  mode: 'shell' | 'exec';
  commands: string[];
  match_templates: string[];
  notes: string;
};

export type BackupRunnerStatus = {
  running: number;
  queued: number;
  concurrency: number;
  next_schedule_at: string | null;
  last_schedule_at: string | null;
  eligible_devices: number;
  drivers: ConfigDriver[];
  driver_errors: { file: string; error: string }[];
};

export type ConfigSearchResult = {
  items: {
    device_id: number;
    device_name: string;
    version_id: number;
    collected_at: string;
    matches: { line: number; text: string }[];
    more: boolean;
  }[];
  truncated: boolean;
};

export type ConfigDiff = {
  from: ConfigVersion;
  to: ConfigVersion;
  diff: string;
  added: number;
  removed: number;
  truncated: boolean;
};

export const SSH_STEPS = ['connect', 'host_key', 'auth', 'prompt', 'setup', 'command'] as const;
export type SshStep = (typeof SSH_STEPS)[number];
export type SshTestResult = {
  ok: boolean;
  steps: { step: SshStep; ok: boolean; detail: string; ms: number }[];
  driver: string | null;
  output_preview: string;
};

// ---------------------------------------------------------------------------
// Flows

export type FlowProtocol = 'netflow5' | 'netflow9' | 'ipfix' | 'sflow5';
export type FlowExporterState = 'accepted' | 'pending' | 'ignored';
export type FlowExporter = {
  id: number;
  address: string;
  device_id: number | null;
  device_name: string | null;
  state: FlowExporterState;
  protocols: FlowProtocol[];
  sampling_override: number | null;
  sampling_seen: number | null;
  first_seen: string;
  last_seen: string;
  records_per_s: number;
  both_directions_warning: boolean;
  unsampled_warning: boolean;
};
export const FLOW_DROP_REASONS = [
  'rate_limited',
  'queue_full',
  'parse_error',
  'no_template',
  'unknown_exporter',
  'ignored_exporter',
  'over_budget',
  'unsupported',
  'exporter_limit',
  'aggregation_overflow',
  'db_error',
] as const;
export type FlowDropReason = (typeof FLOW_DROP_REASONS)[number];
export type FlowCounters = {
  received_datagrams: number;
  records: number;
  stored_rows: number;
  dropped: Record<FlowDropReason, number>;
};
/** 2b-3 receiver shape with the flow kinds. */
export type FlowReceiverStatus = Omit<ReceiverStatus, 'kind'> & {
  kind: 'netflow' | 'ipfix' | 'sflow';
};
export type FlowStatus = {
  receivers: FlowReceiverStatus[];
  counters_since: string;
  counters: FlowCounters;
  records_per_s: number;
  budget_per_s: number;
  queue: number;
  queue_capacity: number;
  templates: number;
  rollup: 'cagg' | 'table' | null;
  storage_bytes: number | null;
};
export type FlowQuery = {
  exporter_id?: number;
  device_id?: number;
  if_index?: number;
  direction?: 'in' | 'out';
  since?: string;
  until?: string;
  limit?: number;
};
export type FlowTopItem = {
  key: string;
  label: string;
  ip?: string;
  hostname?: string | null;
  device_id?: number | null;
  protocol?: number;
  port?: number;
  app?: string | null;
  src_ip?: string;
  dst_ip?: string;
  src_port?: number;
  dst_port?: number;
  in_if?: number;
  out_if?: number;
  bytes: number;
  packets: number;
  flows: number;
  share: number;
};
export type FlowTop = {
  since: string;
  until: string;
  source: 'minute' | 'hour';
  total_bytes: number;
  items: FlowTopItem[];
  other_bytes: number;
};
export type FlowSeries = {
  since: string;
  until: string;
  bucket_s: number;
  source: 'minute' | 'hour';
  interface: {
    exporter_id: number;
    if_index: number;
    name: string;
    speed_bps: number | null;
  } | null;
  series: { key: string; label: string; direction: 'in' | 'out'; points: [string, number][] }[];
};
export type FlowInterface = {
  exporter_id: number;
  if_index: number;
  name: string;
  in_bytes: number;
  out_bytes: number;
  speed_bps: number | null;
};

// ---------------------------------------------------------------------------
// New-device alerts

export const KNOWN_MAC_STATES = ['baseline', 'new', 'known', 'allowed'] as const;
export type KnownMacState = (typeof KNOWN_MAC_STATES)[number];
export type KnownMac = {
  mac: string;
  state: KnownMacState;
  vendor: string | null;
  locally_administered: boolean;
  first_seen: string;
  first_device_id: number | null;
  first_device_name: string | null;
  first_if_index: number | null;
  first_port_name: string | null;
  first_vlan: number | null;
  first_ip: string | null;
  first_hostname: string | null;
  last_seen: string;
  last_device_id: number | null;
  last_device_name: string | null;
  last_port_name: string | null;
  last_vlan: number | null;
  ip: string | null;
  hostname: string | null;
  detected_at: string | null;
  ack_at: string | null;
  ack_by: string | null;
  ack_comment: string;
};
export type AllowEntry = {
  id: number;
  prefix: string;
  bits: 24 | 28 | 36 | 48;
  vendor: string | null;
  comment: string;
  created_by: string | null;
  created_at: string;
};
export type NewDeviceStatus = {
  learning: boolean;
  learning_until: string | null;
  scope_group_ids: number[];
  switches_in_scope: number;
  switches_baselined: number;
  known: number;
  new_unacknowledged: number;
  last_detection_at: string | null;
};

// ---------------------------------------------------------------------------
// SSO

export type KerberosState = 'ready' | 'unavailable' | 'disabled' | 'error';
export type KerberosWarning = 'rc4_only' | 'no_http_principal';
export type KerberosSettings = {
  realms: string[];
  auto_login: boolean;
  keytab_set: boolean;
  keytab_uploaded_at: string | null;
  keytab_entries: { principal: string; kvno: number; enctype: number; enctype_name: string }[];
  warnings: KerberosWarning[];
  platform_supported: boolean;
  state: KerberosState;
  error: string | null;
};
export type OidcRoleSource = 'app_roles' | 'groups';
export type OidcSettings = {
  tenant_id: string;
  client_id: string;
  client_secret: null;
  client_secret_set: boolean;
  client_secret_expires_at: string | null;
  role_source: OidcRoleSource;
  admin_values: string[];
  viewer_values: string[];
  button_label: string | null;
  redirect_uri: string;
};
export type OidcSettingsInput = Partial<
  Omit<OidcSettings, 'client_secret' | 'client_secret_set' | 'redirect_uri'>
> & { client_secret?: string | null };
export type OidcTestResult = { ok: boolean; issuer: string; jwks_keys: number; latency_ms: number };
export const SSO_ERRORS = [
  'sso_failed',
  'sso_conflict',
  'not_in_group',
  'oidc_group_overage',
  'oidc_error',
  'feature_disabled',
  'inactive',
  'sso_ntlm',
] as const;

// ---------------------------------------------------------------------------
// Reports

export const REPORT_SECTIONS = [
  'availability',
  'top_alerts',
  'flapping_ports',
  'port_errors',
  'config_changes',
  'new_devices',
  'top_talkers',
] as const;
export type ReportSection = (typeof REPORT_SECTIONS)[number];
export type ReportRunStatus = 'running' | 'ok' | 'partial' | 'failed';
export type ReportSchedule = 'weekly' | 'monthly' | 'none';
export const REPORT_SCHEDULES: ReportSchedule[] = ['weekly', 'monthly', 'none'];
export type Report = {
  id: number;
  name: string;
  enabled: boolean;
  language: Language;
  scope_group_ids: number[];
  scope_device_ids: number[];
  sections: ReportSection[];
  schedule: ReportSchedule;
  weekday: number | null;
  month_day: number | null;
  send_time: string | null;
  email_to: string[];
  channel_ids: number[];
  next_run_at: string | null;
  last_run: { id: number; status: ReportRunStatus; finished_at: string | null } | null;
  /** [3b] AI narrative summary (feature ai_report_summary) */
  ai_summary?: boolean;
  created_at: string;
  updated_at: string;
};
export type ReportInput = Partial<
  Omit<Report, 'id' | 'next_run_at' | 'last_run' | 'created_at' | 'updated_at'>
>;
export type ReportDelivery = { target: string; ok: boolean; error: string | null; at: string };
export type ReportRun = {
  id: number;
  report_id: number | null;
  report_name: string;
  trigger: 'schedule' | 'manual';
  period_start: string;
  period_end: string;
  started_at: string;
  finished_at: string | null;
  status: ReportRunStatus;
  language: Language;
  created_by: string | null;
  deliveries: ReportDelivery[];
  /** [3b] */
  ai_summary_status?:
    'ok' | 'failed' | 'skipped_cap' | 'timeout' | 'unverified_numbers' | 'disabled' | null;
};
export type AvailabilityRow = {
  device_id: number;
  device_name: string;
  group_name: string;
  sensor_id: number | null;
  availability: number | null;
  measured_s: number;
  downtime_s: number;
  outages: number;
  longest_outage_s: number;
};
export type ReportData = {
  /** [3b] AI-generated summary paragraph */
  ai_summary?: string | null;
  generated_at: string;
  period_start: string;
  period_end: string;
  timezone: string;
  scope_label: string;
  sections: ReportSection[];
  notes: { section: ReportSection; note: 'feature_off' | 'no_data' }[];
  availability?: {
    overall: number | null;
    devices: AvailabilityRow[];
    not_measured: { device_id: number; device_name: string }[];
    groups: {
      group_id: number;
      group_name: string;
      availability: number | null;
      devices: number;
    }[];
  };
  top_alerts?: {
    sensor_id: number;
    sensor_name: string;
    device_name: string;
    notifications: number;
    problem_s: number;
  }[];
  flapping_ports?: {
    device_id: number;
    device_name: string;
    if_index: number;
    port_name: string;
    episodes: number;
    transitions: number;
  }[];
  port_errors?: {
    device_id: number;
    device_name: string;
    if_index: number | null;
    port_name: string;
    errors: number;
    source: 'sensor' | 'snapshot';
  }[];
  config_changes?: {
    device_id: number;
    device_name: string;
    changes: number;
    last_change_at: string;
    added: number;
    removed: number;
  }[];
  new_devices?: {
    mac: string;
    vendor: string | null;
    ip: string | null;
    hostname: string | null;
    switch_name: string | null;
    port_name: string | null;
    detected_at: string;
    acknowledged: boolean;
  }[];
  top_talkers?: { hosts: FlowTopItem[]; applications: FlowTopItem[] };
};

// ---------------------------------------------------------------------------
// NOC kiosk

export const KIOSK_WIDGETS = [
  'summary',
  'down_devices',
  'problems',
  'recent_alerts',
  'map',
  'group_tiles',
] as const;
export type KioskWidget = (typeof KIOSK_WIDGETS)[number];
export type KioskScreen = {
  id: number;
  name: string;
  enabled: boolean;
  token_prefix: string;
  allowed_sources: string[];
  scope_group_id: number | null;
  scope_group_name: string | null;
  widgets: KioskWidget[];
  language: Language;
  show_addresses: boolean;
  refresh_s: number;
  last_seen_at: string | null;
  last_seen_ip: string | null;
  created_at: string;
  updated_at: string;
};
export type KioskScreenInput = Partial<
  Pick<
    KioskScreen,
    | 'name'
    | 'enabled'
    | 'allowed_sources'
    | 'scope_group_id'
    | 'widgets'
    | 'language'
    | 'show_addresses'
    | 'refresh_s'
  >
>;
export type KioskTokenResponse = { token: string; url: string | null; item: KioskScreen };
export type KioskView = {
  screen: { name: string; language: Language; widgets: KioskWidget[]; refresh_s: number };
  generated_at: string;
  timezone: string;
  summary: {
    device_counts: StatusCounts;
    sensor_counts: StatusCounts;
    acknowledged: number;
    in_maintenance: number;
  } | null;
  down_devices:
    | {
        id: number;
        name: string;
        group: string;
        host: string | null;
        status: 'down' | 'unreachable';
        since: string | null;
        acknowledged: boolean;
      }[]
    | null;
  problems:
    | {
        sensor_id: number;
        sensor_name: string;
        device_name: string;
        status: Status;
        message: string;
        since: string | null;
        acknowledged: boolean;
      }[]
    | null;
  recent_alerts:
    | {
        time: string;
        device_name: string;
        sensor_name: string;
        old_status: Status | null;
        new_status: Status | null;
        message: string;
      }[]
    | null;
  group_tiles: { id: number; name: string; status: Status; counts: StatusCounts }[] | null;
  map: {
    nodes: {
      id: number;
      name: string;
      status: Status;
      x: number | null;
      y: number | null;
      kind: string;
    }[];
    links: { a: number; b: number; status: 'up' | 'down' | 'unknown' }[];
  } | null;
};

// ---------------------------------------------------------------------------
// Chat acknowledge

export type ChatAckResolve = {
  sensor: Sensor;
  device_name: string;
  problem_since: string | null;
  event_id: number;
  active: boolean;
  acknowledgement: Acknowledgement | null;
  can_acknowledge: boolean;
};
export type ChatLink = {
  id: number;
  user_id: number;
  username: string;
  provider: 'telegram';
  display: string;
  linked_at: string;
  last_used_at: string | null;
};
export type ChatLinkCode = {
  code: string;
  expires_at: string;
  bot_usernames: string[];
  instructions: string;
};
export type TelegramPoller = {
  bot_username: string;
  channel_ids: number[];
  state: 'idle' | 'polling' | 'error';
  last_error: string | null;
  updated_at: string;
};

// ---------------------------------------------------------------------------
// Settings keys (data-model § [3] Settings keys)

export type Settings3 = {
  feature_config_backup_enabled: boolean;
  feature_flows_enabled: boolean;
  feature_new_device_alerts_enabled: boolean;
  feature_sso_kerberos_enabled: boolean;
  feature_sso_oidc_enabled: boolean;
  feature_reports_enabled: boolean;
  feature_noc_enabled: boolean;
  feature_chat_ack_enabled: boolean;
  config_backup_time: string;
  config_backup_window_min: number;
  config_backup_concurrency: number;
  config_backup_timeout_s: number;
  config_backup_mask_secrets: boolean;
  config_backup_max_versions: number;
  config_backup_min_versions: number;
  config_backup_retention_days: number;
  config_backup_fail_threshold: number;
  config_change_alert: boolean;
  config_change_clear_min: number;
  flow_max_records_per_s: number;
  flow_top_conversations: number;
  flow_top_hosts: number;
  flow_top_apps: number;
  flow_raw_retention_hours: number;
  flow_minute_retention_hours: number;
  flow_rollup_retention_days: number;
  flow_app_overrides: { protocol: number; port: number; name: string }[];
  newdev_learning_days: number;
  newdev_learning_until: string | null;
  newdev_scope_group_ids: number[];
  newdev_ignore_random_macs: boolean;
  newdev_condition_clear_min: number;
  newdev_forget_after_days: number;
  report_retention_days: number;
  chat_ack_ttl_h: number;
  telegram_poll_timeout_s: number;
};
