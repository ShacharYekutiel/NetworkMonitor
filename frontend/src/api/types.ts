// Hand-written mirror of docs/api.md. Field names are exact.
import type {
  BackupMode,
  BackupStatus,
  ConfigVersion,
  FlowExporter,
  KnownMac,
  ReportRun,
  Settings3,
  SshCredentials,
} from './types3';
export * from './types3';

export type Status = 'up' | 'warning' | 'down' | 'paused' | 'unknown' | 'unreachable'; // [2b-1] unreachable
export type ChannelStatus = 'up' | 'warning' | 'down' | 'unknown';
export type Role = 'admin' | 'viewer';
export type Language = 'en' | 'he';

export type StatusCounts = {
  up: number;
  warning: number;
  down: number;
  paused: number;
  unknown: number;
  /** [2b-1] */
  unreachable: number;
  total: number;
};

export type Page<T> = { items: T[]; total: number; page: number; page_size: number };

export type ApiErrorDetail = {
  loc?: (string | number)[];
  msg?: string;
  field?: string;
  /** [2a] scan_running */
  scan_id?: number;
  /** [2a] too_many_hosts */
  count?: number;
  max?: number;
  /** [2b-1] dependency_cycle: the cycle path, e.g. [3, 7, 3] */
  device_ids?: number[];
  /** [2b-1] topology_refresh_running */
  refresh_id?: number;
  /** [2b-2] ports_disabled */
  port_inventory?: string;
  arp_collect?: string;
  effective_class?: string;
  /** [2b-2] ports_refresh_running */
  device_id?: number;
  /** [2b-3] feature_disabled */
  feature?: string;
  /** [3] config backup not eligible (`no_credentials`, …) */
  reason?: string;
  /** [3] ssh_error */
  error?: string;
  step?: string;
};
export type ApiErrorEnvelope = {
  error: { code: string; message: string; details: ApiErrorDetail[] | null };
};

export type User = {
  id: number;
  username: string;
  email: string | null;
  role: Role;
  language: Language;
  is_active: boolean;
  last_login_at: string | null;
  created_at: string;
  /** [2b-3] (absent on older servers → local) */
  source?: UserSource;
  display_name?: string | null;
  ldap_synced_at?: string | null;
};
/** [2b-3] */
export type UserSource = 'local' | 'ldap' | 'oidc'; // [3] oidc

export type SnmpVersion = 'v1' | 'v2c' | 'v3';
export type SnmpSecurityLevel = 'noAuthNoPriv' | 'authNoPriv' | 'authPriv';
export type SnmpAuthProtocol = 'MD5' | 'SHA' | 'SHA224' | 'SHA256' | 'SHA384' | 'SHA512';
export type SnmpPrivProtocol = 'DES' | 'AES128' | 'AES192' | 'AES256';

export type SnmpCredentials = {
  version: SnmpVersion;
  port: number;
  timeout_s: number;
  retries: number;
  community: null; // write-only (see Secret fields)
  community_set: boolean;
  v3_username: string | null;
  v3_security_level: SnmpSecurityLevel | null;
  v3_auth_protocol: SnmpAuthProtocol | null;
  v3_auth_key: null;
  v3_auth_key_set: boolean;
  v3_priv_protocol: SnmpPrivProtocol | null;
  v3_priv_key: null;
  v3_priv_key_set: boolean;
};

/** Request form of SNMP credentials: secrets may be strings; *_set keys are not sent. */
export type SnmpCredentialsInput = Partial<
  Omit<
    SnmpCredentials,
    | 'community'
    | 'v3_auth_key'
    | 'v3_priv_key'
    | 'community_set'
    | 'v3_auth_key_set'
    | 'v3_priv_key_set'
  >
> & { community?: string | null; v3_auth_key?: string | null; v3_priv_key?: string | null };

export type Group = {
  id: number;
  parent_id: number | null;
  name: string;
  description: string;
  snmp_credentials: SnmpCredentials | null;
  sort_order: number;
  status: Status;
  status_counts: StatusCounts;
  /** [2b-1] `inherit` at a root group means none. */
  dependency: Dependency;
  /** [3] */
  ssh_credentials?: SshCredentials | null;
  created_at: string;
  updated_at: string;
};

export type Device = {
  id: number;
  group_id: number;
  name: string;
  host: string;
  description: string;
  enabled: boolean;
  snmp_credentials: SnmpCredentials | null;
  effective_snmp_source: 'device' | 'group' | 'default';
  effective_snmp_group_id: number | null;
  sort_order: number;
  status: Status;
  status_counts: StatusCounts;
  /** [2b-1] */
  dependency: Dependency;
  effective_dependency: EffectiveDependency | null;
  in_maintenance: boolean;
  maintenance: MaintenanceRef | null;
  identity: DeviceIdentity | null;
  /** [2b-2] */
  port_inventory: InventoryMode;
  arp_collect: InventoryMode;
  ports_effective: boolean;
  arp_effective: boolean;
  effective_class: DeviceClass;
  /** [2b-3] additional sender IPs (≤ 16) */
  source_addresses?: string[];
  /** [2b-3] read-only */
  source_address_conflicts?: { address: string; device_id: number; device_name: string }[];
  /** [2b-3] read-only */
  event_sensor_optout?: EventSensorType[];
  /** [3] config backup */
  ssh_credentials?: SshCredentials | null;
  effective_ssh_source?: 'device' | 'group' | 'none';
  effective_ssh_group_id?: number | null;
  config_backup?: BackupMode;
  config_backup_driver?: string | null;
  /** [3] new-device alerts: ifIndexes excluded from detection */
  newdev_excluded_ports?: number[];
  created_at: string;
  updated_at: string;
};

/** [2b-2] */
export type InventoryMode = 'auto' | 'on' | 'off';
export const INVENTORY_MODES: InventoryMode[] = ['auto', 'on', 'off'];

export type SensorType =
  | 'ping'
  | 'snmp_system'
  | 'snmp_cpu'
  | 'snmp_memory'
  | 'snmp_interface'
  | 'http'
  | 'tcp_port'
  | 'dns'
  | 'snmp_custom' // [2a]
  | 'snmp_disk' // [2a]
  | 'snmp_walk_count' // [2b-2]
  | 'trap' // [2b-3] passive event sensors
  | 'syslog'
  | 'port_flap'
  | 'config_backup' // [3]
  | 'new_mac';
/** [2b-3] */
export type EventSensorType = 'trap' | 'syslog' | 'port_flap';
export const EVENT_SENSOR_TYPES: EventSensorType[] = ['trap', 'syslog', 'port_flap'];

export const SENSOR_TYPES: SensorType[] = [
  'ping',
  'snmp_system',
  'snmp_cpu',
  'snmp_memory',
  'snmp_interface',
  'http',
  'tcp_port',
  'dns',
  'snmp_custom',
  'snmp_disk',
  'snmp_walk_count',
  'trap',
  'syslog',
  'port_flap',
  'config_backup',
  'new_mac',
];

export type Channel = {
  key: string;
  unit: string;
  warning_min: number | null;
  warning_max: number | null;
  down_min: number | null;
  down_max: number | null;
  last_value: number | null;
  status: ChannelStatus;
};

export type ChannelThresholdInput = {
  key: string;
  warning_min?: number | null;
  warning_max?: number | null;
  down_min?: number | null;
  down_max?: number | null;
};

export type Sensor = {
  id: number;
  device_id: number;
  device_name: string;
  group_id: number;
  name: string;
  type: SensorType;
  config: Record<string, unknown>;
  interval_s: number;
  enabled: boolean;
  status: Status;
  status_message: string;
  last_check_at: string | null;
  last_status_change_at: string | null;
  primary_channel: string;
  channels: Channel[];
  sort_order: number;
  /** [2b-1] */
  acknowledgement: Acknowledgement | null;
  in_maintenance: boolean;
  /** [1.3] how the sensor's status rolls up into the device status (absent on older servers = critical) */
  impact?: SensorImpact;
  created_at: string;
  updated_at: string;
};

/** [1.3] critical: the device follows the sensor; warning: at most warning; none: ignored. */
export type SensorImpact = 'critical' | 'warning' | 'none';
export const SENSOR_IMPACTS: SensorImpact[] = ['critical', 'warning', 'none'];

export type EventKind =
  | 'status_change'
  | 'notification'
  | 'system'
  | 'discovery'
  | 'acknowledge' // [2b-1]
  | 'maintenance' // [2b-1]
  | 'notification_sent' // [2b-3]
  | 'port_change' // [2b-3]
  | 'config_change' // [3]
  | 'new_device';
export const EVENT_KINDS: EventKind[] = [
  'status_change',
  'notification',
  'system',
  'discovery',
  'acknowledge',
  'maintenance',
  'notification_sent',
  'port_change',
  'config_change',
  'new_device',
];

export type NmEvent = {
  id: number;
  time: string;
  kind: EventKind;
  sensor_id: number | null;
  device_id: number | null;
  group_id: number | null;
  sensor_name: string | null;
  device_name: string | null;
  old_status: Status | null;
  new_status: Status | null;
  message: string;
  /** [2b-1] */
  in_maintenance: boolean;
};

export type CheckResult = {
  ok: boolean;
  status: Status;
  message: string;
  duration_ms: number;
  checked_at: string;
  channels: { key: string; unit: string; value: number | null; status: ChannelStatus }[];
};

export type Health = {
  status: 'ok' | 'degraded';
  version: string;
  database: 'ok' | 'error';
  timescale: boolean;
  poller: 'running' | 'stopped';
  sensors_scheduled: number;
  uptime_s: number;
  /** [2b-3] */
  trgm?: boolean;
  ingest?: {
    trap: ReceiverState;
    syslog_udp: ReceiverState;
    syslog_tcp: ReceiverState;
    /** [3] */
    netflow?: ReceiverState;
    ipfix?: ReceiverState;
    sflow?: ReceiverState;
  };
  /** [3] */
  flows_rollup?: 'cagg' | 'table' | null;
  kerberos?: 'ready' | 'unavailable' | 'disabled' | 'error';
};

// Tree
export type TreeSensor = {
  id: number;
  name: string;
  type: SensorType;
  enabled: boolean;
  status: Status;
  status_message: string;
  primary_channel: string;
  primary_value: number | null;
  primary_unit: string;
  last_check_at: string | null;
  /** [2b-1] */
  acknowledged: boolean;
};
export type TreeDevice = {
  id: number;
  name: string;
  host: string;
  enabled: boolean;
  status: Status;
  status_counts: StatusCounts;
  /** [2b-1] */
  in_maintenance: boolean;
  /** [2b-1] effective parent device */
  parent_device_id: number | null;
  sensors: TreeSensor[];
};
export type TreeGroup = {
  id: number;
  name: string;
  parent_id: number | null;
  status: Status;
  status_counts: StatusCounts;
  children: TreeGroup[];
  devices: TreeDevice[];
};
export type Tree = { groups: TreeGroup[] };

// Sensor type definitions
export type ConfigFieldType =
  | 'int'
  | 'str'
  | 'bool'
  | 'enum'
  | 'int_list'
  | 'str_nullable'
  | 'int_nullable'
  | 'headers'
  | 'float' // [2a]
  | 'value_map'; // [2b-1]

export type ConfigField = {
  key: string;
  type: ConfigFieldType;
  required: boolean;
  default: unknown;
  min: number | null;
  max: number | null;
  options: string[] | null;
};

export type ChannelDef = {
  key: string;
  unit: string;
  warning_min: number | null;
  warning_max: number | null;
  down_min: number | null;
  down_max: number | null;
};

/** [1.3.1] a named enum value map (GET /api/sensor-types `value_maps`); texts per language */
export type NamedValueMap = {
  title: { en: string; he: string };
  codes: {
    value: string;
    status: 'up' | 'warning' | 'down';
    mib: string;
    text: { en: string; he: string };
  }[];
  /** text of a code the table does not know; `{code}` = the value */
  unknown: { en: string; he: string };
};

export type SensorTypeDef = {
  type: SensorType;
  requires_snmp: boolean;
  primary_channel: string;
  config_fields: ConfigField[];
  channels: ChannelDef[];
  /** [2b-3] event sensors: not polled, no run/test */
  passive?: boolean;
};

export type SnmpInterface = {
  if_index: number;
  name: string;
  descr: string;
  alias: string;
  type: number;
  speed_bps: number | null;
  admin_status: string;
  oper_status: string;
  mac: string | null;
  existing_sensor_id: number | null;
};

export type HistoryRange = '1h' | '24h' | '7d' | '30d';
export type HistoryPoint = { t: string; avg: number; min: number; max: number };
export type History = {
  sensor_id: number;
  from: string;
  to: string;
  bucket_s: number;
  channels: { key: string; unit: string; points: HistoryPoint[] }[];
};

export type Dashboard = {
  sensor_counts: StatusCounts;
  device_counts: StatusCounts;
  group_count: number;
  recent_events: NmEvent[];
  /** down/warning, not acknowledged */
  problem_sensors: Sensor[];
  /** [2b-1] */
  acknowledged_sensors: Sensor[];
  acknowledged_count: number;
  unreachable_devices: UnreachableDevice[];
  maintenance: { active_window_ids: number[]; device_count: number };
};

/** [2b-1] */
export type UnreachableDevice = {
  id: number;
  name: string;
  host: string;
  parent_device_id: number | null;
  parent_device_name: string | null;
};

export type EmailChannelConfig = { to: string[]; /** [3] */ ack_link?: boolean };
export type WebhookChannelConfig = {
  url: string;
  headers: Record<string, string>;
  verify_tls: boolean;
  timeout_s: number;
};
/** [2a] */
export type TelegramChannelConfig = {
  bot_token: null;
  bot_token_set: boolean;
  chat_id: string;
  parse_mode: 'HTML' | null;
  disable_notification: boolean;
  /** [3] chat ack */
  ack_buttons?: boolean;
  ack_link?: boolean;
};
/** Request form: bot_token is a real string (or omitted/null = keep). */
export type TelegramChannelConfigInput = {
  bot_token?: string | null;
  chat_id: string;
  parse_mode: 'HTML' | null;
  disable_notification: boolean;
  ack_buttons?: boolean;
  ack_link?: boolean;
};
/** [2b-3] url is a write-only secret */
export type TeamsChannelConfig = {
  url: null;
  url_set: boolean;
  url_host: string | null;
  legacy: boolean;
  /** [3] */
  ack_link?: boolean;
};
export type TeamsChannelConfigInput = { url?: string | null; ack_link?: boolean };
export type NotificationType = 'email' | 'webhook' | 'telegram' | 'teams';
export const NOTIFICATION_TYPES: NotificationType[] = ['email', 'webhook', 'telegram', 'teams'];
export type NotificationChannel = {
  id: number;
  name: string;
  type: NotificationType;
  enabled: boolean;
  config: EmailChannelConfig | WebhookChannelConfig | TelegramChannelConfig | TeamsChannelConfig;
  created_at: string;
  updated_at: string;
};

export type AlertRule = {
  id: number;
  name: string;
  sensor_id: number | null;
  sensor_name: string | null;
  on_statuses: ('down' | 'warning' | 'up')[];
  delay_s: number;
  enabled: boolean;
  channel_ids: number[];
  /** [2b-1] */
  escalation_after_s: number | null;
  escalation_channel_ids: number[];
  repeat_every_s: number | null;
  created_at: string;
  updated_at: string;
};

export type SmtpSecurity = 'none' | 'starttls' | 'ssl';
export type SmtpSettings = {
  host: string;
  port: number;
  security: SmtpSecurity;
  username: string | null;
  password: null;
  password_set: boolean;
  from_address: string;
  from_name: string;
};
export type Settings = {
  smtp: SmtpSettings;
  retention_days: number;
  event_retention_days: number;
  default_interval_s: number;
  default_language: Language;
  public_base_url: string | null;
  /** [2b-1] IANA zone */
  timezone: string;
  /** [2b-1] 0 (off) or 5–1440 */
  topology_refresh_min: number;
  /** [2b-2] 0 (off) or 5–1440 */
  port_inventory_min: number;
  /** [2b-2] 1–1000 */
  uplink_mac_threshold: number;
  /** [2b-2] 1–365 */
  port_mac_retention_days: number;
  /** [2b-2] */
  reverse_dns: boolean;
} & Partial<Settings2b3> &
  Partial<Settings3>;
export type SettingsInput = Partial<
  Omit<Settings, 'smtp' | 'trap_communities' | 'trap_v3_users' | 'ldap' | 'trap_communities_count'>
> & {
  smtp?: Partial<Omit<SmtpSettings, 'password' | 'password_set'>> & { password?: string | null };
  /** [2b-3] omitted/null keep, a list (also []) replaces */
  trap_communities?: string[] | null;
  trap_v3_users?: TrapV3UserInput[];
  ldap?: LdapSettingsInput;
};

export type TestResult = { ok: boolean; message: string };

// WebSocket
export type WsMessage =
  | { type: 'hello'; ts: string; data: { user: User; version: string; timescale: boolean } }
  | {
      type: 'sensor.update';
      ts: string;
      data: {
        id: number;
        device_id: number;
        status: Status;
        status_message: string;
        last_check_at: string | null;
        status_changed: boolean;
        channels: { key: string; value: number | null; status: ChannelStatus }[];
        /** [2b-1] */
        acknowledged?: boolean;
      };
    }
  | { type: 'event.created'; ts: string; data: NmEvent }
  | {
      type: 'device.status';
      ts: string;
      data: { id: number; status: Status; status_counts: StatusCounts };
    }
  | {
      type: 'group.status';
      ts: string;
      data: { id: number; status: Status; status_counts: StatusCounts };
    }
  | {
      type: 'config.changed';
      ts: string;
      data: {
        entity:
          | 'group'
          | 'device'
          | 'sensor'
          | 'alert_rule'
          | 'notification_channel'
          | 'discovery_profile'
          | 'maintenance_window' // [2b-1]
          | 'topology_link' // [2b-1]
          | 'event_rule' // [2b-3]
          | 'api_token' // [2b-3]
          | 'report' // [3]
          | 'kiosk_screen'
          | 'flow_exporter'
          | 'mac_allowlist'
          | 'config_backup';
        id: number;
        action: 'created' | 'updated' | 'deleted';
      };
    }
  | { type: 'discovery.progress'; ts: string; data: DiscoveryProgress }
  | { type: 'discovery.result'; ts: string; data: { scan_id: number; result: DiscoveryResult } }
  | { type: 'discovery.finished'; ts: string; data: DiscoveryFinished }
  | {
      type: 'sensor.ack';
      ts: string;
      data: { id: number; device_id: number; acknowledgement: Acknowledgement | null };
    }
  | {
      type: 'maintenance.changed';
      ts: string;
      data: {
        active_window_ids: number[];
        entered_device_ids: number[];
        left_device_ids: number[];
      };
    }
  | {
      type: 'topology.updated';
      ts: string;
      data: { reason: 'refresh' | 'link' | 'layout'; refresh_id: number | null };
    }
  | { type: 'topology.refresh'; ts: string; data: { refresh: TopologyRefresh } }
  | {
      type: 'ports.updated';
      ts: string;
      data: {
        device_id: number;
        ok: boolean;
        collected_at: string;
        /** [2b-2] the device collects ARP (absent on older servers) */
        arp_source?: boolean;
      };
    }
  | {
      type: 'templates.reloaded';
      ts: string;
      data: { loaded_at: string; count: number; error_count: number };
    }
  | { type: 'pong'; ts: string; data: Record<string, never> }
  // [2b-3]
  | { type: 'subscribed'; ts: string; data: { topics: string[] } }
  | { type: 'syslog.batch'; ts: string; data: { items: SyslogMessage[]; skipped: number } }
  | { type: 'trap.batch'; ts: string; data: { items: Trap[]; skipped: number } }
  | { type: 'features.changed'; ts: string; data: { features: Features } }
  | { type: 'ingest.receiver'; ts: string; data: { receiver: ReceiverStatus } }
  // [3]
  | { type: 'config_backup.update'; ts: string; data: { device_id: number; status: BackupStatus } }
  | {
      type: 'config_backup.changed';
      ts: string;
      data: { device_id: number; version: ConfigVersion };
    }
  | { type: 'flows.exporter'; ts: string; data: { exporter: FlowExporter } }
  | {
      type: 'new_device.detected';
      ts: string;
      data: { items: KnownMac[]; summary_count: number };
    }
  | { type: 'report.run'; ts: string; data: { run: ReportRun } }
  | {
      type: 'chat_ack.poller';
      ts: string;
      data: { bot_username: string; state: 'polling' | 'error'; last_error: string | null };
    };

// ---------------------------------------------------------------------------
// [2a] Discovery, device templates, snmp storage

export type DeviceClass =
  'router' | 'switch' | 'access_point' | 'printer' | 'nas' | 'server' | 'workstation' | 'unknown';
export const DEVICE_CLASSES: DeviceClass[] = [
  'router',
  'switch',
  'access_point',
  'printer',
  'nas',
  'server',
  'workstation',
  'unknown',
];
export type ScanStatus = 'running' | 'completed' | 'cancelled' | 'failed';
export type L10n = { en: string; he: string };

export type ExtraV3Out = {
  v3_username: string;
  v3_security_level: SnmpSecurityLevel;
  v3_auth_protocol: SnmpAuthProtocol | null;
  v3_auth_key: null;
  v3_auth_key_set: boolean;
  v3_priv_protocol: SnmpPrivProtocol | null;
  v3_priv_key: null;
  v3_priv_key_set: boolean;
};

export type ScanOptions = {
  ping: boolean;
  ping_timeout_ms: number;
  snmp: boolean;
  snmp_port: number | null;
  snmp_timeout_s: number;
  extra_communities: null; // write-only list
  extra_communities_count: number;
  extra_v3: ExtraV3Out[];
  extra_v3_count: number;
  tcp: boolean;
  tcp_ports: number[];
  tcp_timeout_ms: number;
  dns: boolean;
  /** [2b-1] default true */
  fingerprint: boolean;
  /** [2b-1] 500–5000, default 2000 */
  fingerprint_timeout_ms: number;
};

/** Request form: every key optional; secrets are real values. */
export type ExtraV3Input = {
  v3_username: string;
  v3_security_level: SnmpSecurityLevel;
  v3_auth_protocol: SnmpAuthProtocol | null;
  v3_auth_key: string | null;
  v3_priv_protocol: SnmpPrivProtocol | null;
  v3_priv_key: string | null;
};
export type ScanOptionsInput = Partial<{
  ping: boolean;
  ping_timeout_ms: number;
  snmp: boolean;
  snmp_port: number | null;
  snmp_timeout_s: number;
  extra_communities: string[] | null;
  extra_v3: ExtraV3Input[] | null;
  tcp: boolean;
  tcp_ports: number[];
  tcp_timeout_ms: number;
  dns: boolean;
  fingerprint: boolean;
  fingerprint_timeout_ms: number;
}>;

export type DiscoveryProfile = {
  id: number;
  name: string;
  targets: string[];
  group_id: number;
  options: ScanOptions;
  schedule_enabled: boolean;
  schedule_every_hours: number;
  notify_new_hosts: boolean;
  notify_channel_ids: number[];
  last_run_at: string | null;
  next_run_at: string | null;
  last_scan_id: number | null;
  created_at: string;
  updated_at: string;
};

export type DiscoveryScan = {
  id: number;
  profile_id: number | null;
  profile_name: string | null;
  trigger: 'manual' | 'scheduled';
  status: ScanStatus;
  group_id: number | null;
  targets: string[];
  options: ScanOptions;
  total_hosts: number;
  scanned_hosts: number;
  found_hosts: number;
  new_hosts: number;
  error: string | null;
  created_by: string | null;
  started_at: string;
  finished_at: string | null;
};

export type DiscoveryResult = {
  id: number;
  scan_id: number;
  ip: string;
  hostname: string | null;
  mac: string | null;
  ping_ok: boolean;
  ping_rtt_ms: number | null;
  snmp_ok: boolean;
  snmp_version: SnmpVersion | null;
  snmp_credential: 'group' | 'extra' | null;
  sys_name: string | null;
  sys_descr: string | null;
  sys_object_id: string | null;
  open_ports: number[];
  device_class: DeviceClass;
  vendor: string | null;
  suggested_template: string;
  existing_device_id: number | null;
  is_new: boolean;
  imported_device_id: number | null;
  /** [2b-1] */
  fingerprint: Fingerprint | null;
  model: string | null;
  classified_by: ClassifiedBy;
  confidence: Confidence;
};

export type DiscoveryProgress = {
  scan_id: number;
  status: ScanStatus;
  total_hosts: number;
  scanned_hosts: number;
  found_hosts: number;
  new_hosts: number;
};
export type DiscoveryFinished = DiscoveryProgress & { error: string | null };

export type ChannelOverride = {
  key: string;
  warning_min?: number | null;
  warning_max?: number | null;
  down_min?: number | null;
  down_max?: number | null;
};

export type TemplateEntry =
  | {
      kind: 'sensor';
      type: SensorType;
      name: L10n;
      config: Record<string, unknown>;
      channels: ChannelOverride[];
      interval_s: number | null;
      probe: boolean;
      /** [2b-2] */
      when?: TemplateFilter[];
      confidence?: Confidence | null;
      notes?: string;
    }
  | {
      kind: 'snmp_interfaces';
      oper_up_only: boolean;
      exclude_if_types: number[];
      include_name_regex: string | null;
      exclude_name_regex: string | null;
      max: number;
      channels: ChannelOverride[];
      /** [2b-2] */
      confidence?: Confidence | null;
      notes?: string;
    }
  | {
      kind: 'snmp_table';
      walk_oid: string;
      label_oid: string | null;
      label2_oid: string | null;
      percent_of_column: string | null;
      name: L10n;
      config: Record<string, unknown>;
      channels: ChannelOverride[];
      max: number;
      /** [2b-1] */
      skip_zero?: boolean;
      filters?: TemplateFilter[];
      interval_s?: number | null;
      /** [2b-2] */
      confidence?: Confidence | null;
      notes?: string;
    };

/** [2b-2] */
export type TemplateFilter = { oid: string; in: number[] | null; not_in: number[] | null };

export type TemplateMatchRule = {
  device_classes: DeviceClass[] | null;
  vendors: string[] | null;
  requires_snmp: boolean;
  any_open_ports: number[] | null;
  /** [2b-1] sysObjectID enterprise numbers (null = any) */
  enterprises?: number[] | null;
  /** [2b-2] */
  sys_object_id_prefixes?: string[] | null;
  sys_descr_regex?: string | null;
};
/** [2b-2] api.md name */
export type TemplateRule = TemplateMatchRule;

export type DeviceTemplate = {
  id: string;
  name: L10n;
  description: L10n;
  requires_snmp: boolean;
  priority: number;
  match: TemplateMatchRule[];
  entries: TemplateEntry[];
  /** [2b-2] (optional only so 2b-1 servers still render) */
  version?: number;
  verified?: boolean;
  verified_note?: string;
  vendor?: string | null;
  source?: TemplateSource;
  overrides_builtin?: boolean;
  file?: string;
};

/** [2b-2] */
export type TemplateSource = 'builtin' | 'user';
export type TemplateLoadError = {
  source: TemplateSource;
  file: string;
  template_id: string | null;
  path: string;
  message: string;
};
export type TemplateLibraryStatus = {
  loaded_at: string;
  builtin_count: number;
  user_count: number;
  /** ids of built-ins replaced by user files, sorted */
  overridden: string[];
  /** admins only, else "" */
  user_dir: string;
  /** admins only, else [] */
  errors: TemplateLoadError[];
};
export type DeviceTemplatesResponse = { items: DeviceTemplate[]; library?: TemplateLibraryStatus };

export type SkipReason =
  'exists' | 'probe_failed' | 'snmp_error' | 'max_reached' | 'no_rows' | 'not_applicable'; // [2b-2]
/** [2b-2] api.md name */
export type TemplateSkipReason = SkipReason;
export const SKIP_REASONS: SkipReason[] = [
  'exists',
  'probe_failed',
  'snmp_error',
  'max_reached',
  'no_rows',
  'not_applicable',
];
export type SkippedSensor = { name: string; type: SensorType; reason: SkipReason; message: string };

export type ApplyTemplateResult = {
  device_id: number;
  template_id: string;
  created: { sensor_id: number; name: string; type: SensorType }[];
  skipped: SkippedSensor[];
  /** [1.3] interface sensors removed (remove_unselected_interfaces) */
  removed?: { sensor_id: number; name: string }[];
};

/** [1.3] which interfaces an `snmp_interfaces` entry creates sensors for */
export type ApplyInterfacesMode = 'important' | 'none' | 'all';
export type ApplyTemplateRequest = {
  template_id: string;
  interfaces?: ApplyInterfacesMode | number[];
  remove_unselected_interfaces?: boolean;
};
/** [1.3.1] irf: an IRF physical port of a Comware IRF stack */
export type ApplyInterfaceReason = 'uplink' | 'lag' | 'trunk' | 'irf';
export const APPLY_INTERFACE_REASONS: ApplyInterfaceReason[] = ['uplink', 'lag', 'trunk'];
export type ApplyInterface = {
  if_index: number;
  name: string;
  alias: string;
  oper_status: IfStatus | string;
  speed_bps: number | null;
  kind: PortKind | string;
  important: boolean;
  reasons: ApplyInterfaceReason[];
  has_sensor: boolean;
  sensor_id: number | null;
};
export type ApplyInterfacesResponse = { items: ApplyInterface[]; inventory: 'ok' | 'missing' };

export type ImportItem = { result_id: number; template_id: string; name?: string | null };
export type ImportRequest = { group_id?: number | null; items: ImportItem[] };
export type ImportErrorCode = 'not_found' | 'device_exists' | 'internal_error';
export type ImportResponse = {
  created: {
    result_id: number;
    device_id: number;
    device_name: string;
    template_id: string;
    sensors_created: number;
    sensors_skipped: SkippedSensor[];
  }[];
  errors: { result_id: number; code: ImportErrorCode; message: string; device_id: number | null }[];
};

export type StorageType =
  'ram' | 'virtual_memory' | 'fixed_disk' | 'removable_disk' | 'network_disk' | 'flash' | 'other';
export type SnmpStorage = {
  index: number;
  descr: string;
  type: StorageType;
  size_bytes: number | null;
  used_bytes: number | null;
  existing_sensor_id: number | null;
};

// ---------------------------------------------------------------------------
// [2b-1] Dependencies, maintenance, acknowledge, fingerprinting, topology

export type Acknowledgement = {
  /** username snapshot */
  by: string | null;
  at: string;
  comment: string;
  status: 'down' | 'warning';
};

export type DependencyMode = 'inherit' | 'none' | 'device';
export const DEPENDENCY_MODES: DependencyMode[] = ['inherit', 'none', 'device'];
export type Dependency = {
  mode: DependencyMode;
  device_id: number | null;
  device_name: string | null;
  sensor_id: number | null;
  sensor_name: string | null;
};
/** Request form of a dependency. */
export type DependencyInput = {
  mode: DependencyMode;
  device_id: number | null;
  sensor_id: number | null;
};
export type EffectiveDependency = {
  device_id: number;
  device_name: string;
  /** the resolved dependency sensor (null = whole device) */
  sensor_id: number | null;
  sensor_name: string | null;
  source: 'device' | 'group';
  /** set when source = group */
  group_id: number | null;
  parent_down: boolean;
};
export type MaintenanceRef = { window_id: number; name: string; ends_at: string };

export type Confidence = 'high' | 'medium' | 'low';
export const CONFIDENCES: Confidence[] = ['high', 'medium', 'low'];
export type ClassifiedBy = 'snmp' | 'fingerprint' | 'ports' | 'none';
export const CLASSIFIED_BY: ClassifiedBy[] = ['snmp', 'fingerprint', 'ports', 'none'];

export type FingerprintHttp = {
  port: number;
  scheme: 'http' | 'https';
  status: number | null;
  title: string | null;
  server: string | null;
  realm: string | null;
  markers: string[];
  redirects: number;
  error: string | null;
};
export type Fingerprint = {
  http: FingerprintHttp[];
  ssh: { port: number; banner: string | null } | null;
  telnet: { port: number; banner: string | null } | null;
};
export type DeviceIdentity = {
  device_class: DeviceClass;
  vendor: string | null;
  model: string | null;
  confidence: Confidence;
  classified_by: ClassifiedBy;
  suggested_template: string;
  snmp_ok: boolean;
  sys_object_id: string | null;
  sys_descr: string | null;
  open_ports: number[];
  fingerprint: Fingerprint | null;
  identified_at: string;
};

export type MaintenanceScope = 'global' | 'group' | 'device';
export const MAINTENANCE_SCOPES: MaintenanceScope[] = ['global', 'group', 'device'];
export type MaintenanceKind = 'once' | 'weekly';
export type MaintenanceWindow = {
  id: number;
  name: string;
  enabled: boolean;
  scope: MaintenanceScope;
  group_id: number | null;
  group_name: string | null;
  device_id: number | null;
  device_name: string | null;
  kind: MaintenanceKind;
  /** once */
  starts_at: string | null;
  ends_at: string | null;
  /** weekly: ISO 1=Mon..7=Sun */
  weekdays: number[] | null;
  /** weekly: "HH:MM" local (settings.timezone) */
  start_time: string | null;
  /** weekly */
  duration_min: number | null;
  /** settings.timezone, informational */
  timezone: string;
  comment: string;
  active_now: boolean;
  /** current occurrence if active, else next; null = none left */
  occurrence_start: string | null;
  occurrence_end: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
};
export type MaintenanceWindowInput = {
  name?: string;
  enabled?: boolean;
  scope?: MaintenanceScope;
  group_id?: number | null;
  device_id?: number | null;
  kind?: MaintenanceKind;
  starts_at?: string | null;
  ends_at?: string | null;
  weekdays?: number[] | null;
  start_time?: string | null;
  duration_min?: number | null;
  comment?: string;
};
export type Occurrence = { start: string; end: string };
export type MaintenanceActive = {
  now: string;
  windows: MaintenanceWindow[];
  /** devices currently covered */
  device_ids: number[];
};

export type TopologyDeviceNode = {
  key: string;
  kind: 'device';
  device_id: number;
  name: string;
  host: string;
  group_id: number;
  status: Status;
  in_maintenance: boolean;
  /** from identity */
  device_class: DeviceClass | null;
  x: number | null;
  y: number | null;
  in_filter: boolean;
};
export type TopologyNeighborNode = {
  key: string;
  kind: 'neighbor';
  neighbor_id: number;
  /** sys_name, else chassis id */
  name: string;
  chassis_id: string;
  mgmt_address: string | null;
  sys_descr: string | null;
  x: number | null;
  y: number | null;
  in_filter: true;
};
export type TopologyNode = TopologyDeviceNode | TopologyNeighborNode;
export type LinkEnd = {
  node: string;
  device_id: number | null;
  neighbor_id: number | null;
  if_index: number | null;
  port: string;
  /** snmp_interface sensor of that device with this if_index */
  sensor_id: number | null;
  sensor_status: Status | null;
  /** last oper_status channel value (1 = up) */
  oper_status: number | null;
  in_bps: number | null;
  out_bps: number | null;
  in_util_pct: number | null;
  out_util_pct: number | null;
};
export type LinkSource = 'lldp' | 'cdp' | 'manual';
export const LINK_SOURCES: LinkSource[] = ['lldp', 'cdp', 'manual'];
export type TopologyLink = {
  id: number;
  source: LinkSource;
  a: LinkEnd;
  b: LinkEnd;
  stale: boolean;
  missed_refreshes: number;
  first_seen: string;
  last_seen: string;
};
export type TopologyRefreshStatus = 'running' | 'completed' | 'failed';
export type TopologyRefresh = {
  id: number;
  trigger: 'manual' | 'scheduled';
  status: TopologyRefreshStatus;
  started_at: string;
  finished_at: string | null;
  devices_total: number;
  devices_ok: number;
  devices_failed: number;
  links_found: number;
  error: string | null;
};
export type TopologyGraph = {
  nodes: TopologyNode[];
  links: TopologyLink[];
  last_refresh: TopologyRefresh | null;
  refresh_running: boolean;
};
export type TopologyLinkInput = {
  a_device_id: number;
  a_if_index: number | null;
  a_port?: string;
  b_device_id: number | null;
  b_neighbor_id: number | null;
  b_if_index: number | null;
  b_port?: string;
};
export type LayoutPosition = { node: string; x: number; y: number };
export type DependencySuggestion = {
  device_id: number;
  device_name: string;
  suggested_parent_id: number | null;
  suggested_parent_name: string | null;
  current: Dependency;
  effective_parent_id: number | null;
  hops: number;
};
export type DependencySuggestions = {
  root_device_id: number | null;
  items: DependencySuggestion[];
};
export type DependencyBulkItem = { device_id: number; dependency: DependencyInput };

// ---------------------------------------------------------------------------
// [2b-2] Switch ports view, MAC/IP search

export type PortKind = 'physical' | 'lag' | 'vlan' | 'mgmt' | 'other';
export const PORT_KINDS: PortKind[] = ['physical', 'lag', 'vlan', 'mgmt', 'other'];
export type PortState = 'up' | 'down' | 'admin_down' | 'absent';
export const PORT_STATES: PortState[] = ['up', 'down', 'admin_down', 'absent'];
export type ConnectedKind = 'none' | 'client' | 'ap' | 'switch';
export const CONNECTED_KINDS: ConnectedKind[] = ['none', 'client', 'ap', 'switch'];
export type IfStatus =
  'up' | 'down' | 'testing' | 'unknown' | 'dormant' | 'notPresent' | 'lowerLayerDown';
export const IF_STATUSES: IfStatus[] = [
  'up',
  'down',
  'testing',
  'unknown',
  'dormant',
  'notPresent',
  'lowerLayerDown',
];
export type SourceState = 'ok' | 'empty' | 'error' | 'skipped' | 'unmapped';
export const SOURCE_STATES: SourceState[] = ['ok', 'empty', 'error', 'skipped', 'unmapped'];
export type PoeDetection =
  'disabled' | 'searching' | 'delivering' | 'fault' | 'test' | 'other_fault';
export const POE_DETECTIONS: PoeDetection[] = [
  'disabled',
  'searching',
  'delivering',
  'fault',
  'test',
  'other_fault',
];
export type AlertReason = 'sensor' | 'errors' | 'poe_fault';
export const ALERT_REASONS: AlertReason[] = ['sensor', 'errors', 'poe_fault'];

export type PortPoe = {
  admin_enabled: boolean | null;
  detection: PoeDetection | null;
  /** 0–4 */
  power_class: number | null;
  /** null on every device in 2b-2 (no vendor column yet) */
  power_w: number | null;
};

export type PortMac = {
  /** "aa:bb:cc:dd:ee:ff" */
  mac: string;
  vlan: number | null;
  vendor: string | null;
  locally_administered: boolean;
  /** newest (may be a last-known, non-present one; then ips = []) */
  ip: string | null;
  ips: string[];
  /** reverse DNS of ip */
  hostname: string | null;
  /** a monitored device whose host equals ip */
  device_id: number | null;
  device_name: string | null;
  first_seen: string;
  last_seen: string;
  /** [2b-3] learned on the port's LAG */
  via_lag?: boolean;
};

export type PortNeighbor = {
  link_id: number;
  source: 'lldp' | 'cdp' | 'manual';
  kind: 'device' | 'neighbor';
  device_id: number | null;
  neighbor_id: number | null;
  /** device name, else neighbour sys_name, else chassis id */
  name: string;
  /** the remote port text */
  port: string;
  device_class: DeviceClass | null;
  status: Status | null;
  mgmt_address: string | null;
};

export type Port = {
  if_index: number;
  name: string;
  descr: string;
  alias: string;
  kind: PortKind;
  if_type: number | null;
  admin_status: IfStatus;
  oper_status: IfStatus;
  state: PortState;
  alert: boolean;
  alert_reasons: AlertReason[];
  speed_bps: number | null;
  duplex: 'full' | 'half' | 'unknown' | null;
  mac: string | null;
  last_change_at: string | null;
  pvid: number | null;
  mode: 'access' | 'trunk' | null;
  untagged_vlans: number[];
  tagged_vlans: number[];
  /** null = no PoE row for this port */
  poe: PortPoe | null;
  in_bps: number | null;
  out_bps: number | null;
  in_errors_delta: number | null;
  out_errors_delta: number | null;
  is_uplink: boolean;
  uplink_reason: 'neighbor' | 'mac_count' | null;
  connected_kind: ConnectedKind;
  trunk: boolean;
  /** primary neighbour */
  neighbor: PortNeighbor | null;
  mac_count: number;
  /** null for uplinks */
  first_mac: PortMac | null;
  /** all present MACs; [] for uplinks (count only) */
  macs: PortMac[];
  sensor_id: number | null;
  sensor_status: Status | null;
  collected_at: string;
  /** [2b-3] LAG membership (null/[]/false while the `lag` feature is off) */
  lag?: { if_index: number; name: string; attached: boolean | null } | null;
  lag_members?: number[];
  vlans_from_lag?: boolean;
  /** [1.3.1] IRF port (1/2) of an IRF physical port, else null */
  irf_port?: number | null;
  /** [1.3] connected end devices (first = the most likely one) */
  connected?: { count: number; first: PortConnectedHost | null };
  /** [1.3] LLDP/CDP neighbour name and management address (null = none) */
  neighbor_name?: string | null;
  neighbor_mgmt_address?: string | null;
};

/** [1.3] */
export type PortConnectedHost = {
  mac: string;
  ip: string | null;
  hostname: string | null;
  vendor: string | null;
  device_id: number | null;
  device_name: string | null;
};

export type PortDetail = Port & {
  /** all non-stale neighbours of the port */
  neighbors: PortNeighbor[];
  /** true when is_uplink and include_uplink_macs was not set */
  macs_hidden: boolean;
  /** the full linked sensor (channels with last values) */
  sensor: Sensor | null;
};

export type HealthCategory = 'cpu' | 'memory' | 'temperature' | 'poe' | 'psu' | 'fan';
export const HEALTH_CATEGORIES: HealthCategory[] = [
  'cpu',
  'memory',
  'temperature',
  'poe',
  'psu',
  'fan',
];
export type HealthItem = {
  category: HealthCategory;
  status: Status;
  value: number | null;
  unit: string;
  text: string;
  sensor_ids: number[];
  primary_sensor_id: number;
};

export type PortInventoryStatus = {
  last_run_at: string | null;
  last_ok_at: string | null;
  duration_ms: number | null;
  error: string | null;
  running: boolean;
  next_run_at: string | null;
  sources: {
    if_xtable: SourceState;
    duplex: SourceState;
    bridge: SourceState;
    vlans: SourceState;
    fdb: SourceState;
    poe: SourceState;
    arp: SourceState;
    /** [2b-3] */
    lag?: SourceState;
    /** [1.3.1] IRF physical ports (HH3C-STACK-MIB) */
    irf?: SourceState;
  };
  /** [2b-3] */
  lag_mib?: 'ieee8023' | 'if_stack' | 'both' | null;
  fdb_mib: 'q_bridge' | 'bridge' | null;
  vlan_mib: 'static' | 'current' | null;
  ports: number;
  macs: number;
  arp_entries: number;
  /** devices currently collecting ARP (global; 0 → no IPs anywhere) */
  arp_sources: number;
};

export type DevicePorts = {
  device_id: number;
  ports_effective: boolean;
  arp_effective: boolean;
  port_inventory: InventoryMode;
  arp_collect: InventoryMode;
  effective_class: DeviceClass;
  status: PortInventoryStatus;
  settings: { interval_min: number; uplink_mac_threshold: number };
  health: HealthItem[];
  ports: Port[];
};

export type MacLocation = {
  mac: string;
  vendor: string | null;
  locally_administered: boolean;
  ip: string | null;
  ips: string[];
  hostname: string | null;
  device_id: number;
  device_name: string;
  device_host: string;
  if_index: number;
  port: string;
  port_alias: string;
  vlan: number | null;
  is_uplink: boolean;
  present: boolean;
  first_seen: string;
  last_seen: string;
  /** the most likely access port of this MAC (≤ 1 per MAC) */
  best: boolean;
  /** [2b-3] member port names when `port` is a LAG */
  lag_members?: string[];
};
export type ArpHit = {
  ip: string;
  mac: string;
  hostname: string | null;
  source_device_id: number;
  source_device_name: string;
  present: boolean;
  first_seen: string;
  last_seen: string;
};
export type MacSearchResult = {
  query: { mac: string | null; mac_prefix: string | null; ip: string | null };
  /** ≤ 200 */
  items: MacLocation[];
  /** ≤ 50 */
  arp: ArpHit[];
};

// ---------------------------------------------------------------------------
// [2b-3] Feature toggles, traps, syslog, event rules, ingest, LDAP, audit, timeline, tokens

export type FeatureName =
  | 'traps'
  | 'syslog'
  | 'event_alerts'
  | 'lag'
  | 'ldap'
  | 'audit'
  | 'timeline'
  | 'flapping'
  | 'siem_forwarding'
  // [3]
  | 'config_backup'
  | 'flows'
  | 'new_device_alerts'
  | 'sso_kerberos'
  | 'sso_oidc'
  | 'reports'
  | 'noc'
  | 'chat_ack';
export const FEATURE_NAMES: FeatureName[] = [
  'traps',
  'syslog',
  'event_alerts',
  'flapping',
  'siem_forwarding',
  'lag',
  'ldap',
  'audit',
  'timeline',
  'config_backup',
  'flows',
  'new_device_alerts',
  'sso_kerberos',
  'sso_oidc',
  'reports',
  'noc',
  'chat_ack',
];
export type Features = Record<FeatureName, boolean>;

export type PublicSettings = {
  default_language: Language;
  timezone?: string;
  /** [2b-3] */
  features?: Partial<Features>;
  ldap?: { domain: string; netbios_domain: string } | null;
  /** [3] null while the feature is OFF */
  sso?: {
    kerberos: { auto_login: boolean } | null;
    oidc: { button_label: string } | null;
  };
};

export type ReceiverState = 'listening' | 'disabled' | 'error';

/** Cursor page (traps, syslog, timeline). */
export type CursorPage<T> = { items: T[]; next_cursor: string | null; limit: number };

export type TrapVarbind = {
  oid: string;
  name: string | null;
  type: string;
  value: string;
  display: string;
};
export type TrapVersion = 'v1' | 'v2c' | 'v3';
export type Trap = {
  id: number;
  time: string;
  source_ip: string;
  agent_addr: string | null;
  device_id: number | null;
  device_name: string | null;
  version: TrapVersion;
  pdu: 'trap' | 'inform';
  security_name: string | null;
  trap_oid: string;
  trap_name: string | null;
  uptime_ticks: number | null;
  summary: string;
  varbinds: TrapVarbind[];
  matched_rule_ids: number[];
};
export type TopSource = {
  device_id: number | null;
  device_name: string | null;
  source_ip: string;
  count: number;
};
export type TrapStats = {
  since: string;
  until: string;
  bucket_s: number;
  buckets: { t: string; count: number }[];
  total: number;
  top_sources: TopSource[];
  top_traps: { trap_oid: string; trap_name: string | null; count: number }[];
};

export type SyslogMessage = {
  id: number;
  time: string;
  device_time: string | null;
  source_ip: string;
  device_id: number | null;
  device_name: string | null;
  transport: 'udp' | 'tcp';
  format: 'rfc5424' | 'rfc3164' | 'raw';
  facility: number;
  severity: number;
  hostname: string;
  program: string;
  pid: string;
  msgid: string;
  structured_data: string | null;
  message: string;
  truncated: boolean;
  matched_rule_ids: number[];
};
export type SyslogStats = {
  since: string;
  until: string;
  bucket_s: number;
  /** counts: 8 entries, index = severity */
  buckets: { t: string; counts: number[] }[];
  total: number;
  by_severity: number[];
  top_sources: TopSource[];
  top_programs: { program: string; count: number }[];
};

export type IngestReceiverKind = 'trap' | 'syslog_udp' | 'syslog_tcp';
export type ReceiverStatus = {
  kind: IngestReceiverKind;
  port: number;
  enabled: boolean;
  state: ReceiverState;
  error: string | null;
  since: string | null;
};
export const DROP_REASONS = [
  'rate_limited',
  'queue_full',
  'too_large',
  'parse_error',
  'ignored_source',
  'unknown_source',
  'bad_community',
  'v3_auth',
  'unsupported',
  'db_error',
] as const;
export type DropReason = (typeof DROP_REASONS)[number];
export type IngestCounters = {
  received: number;
  stored: number;
  forwarded: number;
  dropped: Record<DropReason, number>;
};
export type ForwarderState = 'connected' | 'connecting' | 'idle' | 'error' | 'disabled';
export type ForwarderStatus = {
  id: string;
  name: string;
  state: ForwarderState;
  sent: number;
  dropped: number;
  errors: number;
  queue: number;
  last_error: string | null;
};
export type IngestStatus = {
  receivers: ReceiverStatus[];
  counters_since: string;
  counters: { trap: IngestCounters; syslog: IngestCounters };
  rate_per_min: { trap: number; syslog: number };
  queue: { trap: number; syslog: number; capacity: number };
  tcp_connections: number;
  nat_suspected: boolean;
  nat_address: string | null;
  v3_failures: {
    time: string;
    source_ip: string;
    engine_id: string;
    username: string;
    reason: string;
  }[];
  forwarders: ForwarderStatus[];
  trgm: boolean;
  timescale: boolean;
};
export type UnknownSource = {
  source_ip: string;
  syslog_count: number;
  trap_count: number;
  last_seen: string;
  last_hostname: string;
  ignored: boolean;
};

export type EventRuleKind = 'trap' | 'syslog';
export type EventRuleAction = 'warning' | 'down' | 'ignore' | 'config_backup'; // [3] config_backup
export const EVENT_RULE_ACTIONS: EventRuleAction[] = ['warning', 'down', 'ignore', 'config_backup'];
/** [3] actions without key/clear fields (they never raise a condition) */
export function isPassiveAction(a: EventRuleAction): boolean {
  return a === 'ignore' || a === 'config_backup';
}
export type RuleScope = 'global' | 'group' | 'device';
export type VarbindOp = 'eq' | 'ne' | 'contains' | 'regex' | 'exists' | 'absent';
export const VARBIND_OPS: VarbindOp[] = ['eq', 'ne', 'contains', 'regex', 'exists', 'absent'];
export type TrapMatch = {
  trap_oids: string[];
  varbinds: { oid: string; op: VarbindOp; value: string | null }[];
  ports: 'any' | 'important';
};
export type SyslogMatch = {
  severity_max: number | null;
  facilities: number[];
  programs: string[];
  text: string | null;
  text_regex: boolean;
};
export type EventRule = {
  id: number;
  name: string;
  kind: EventRuleKind;
  enabled: boolean;
  builtin: string | null;
  priority: number;
  scope: RuleScope;
  group_id: number | null;
  device_id: number | null;
  scope_name: string | null;
  match: TrapMatch | SyslogMatch;
  action: EventRuleAction;
  key_varbind_oid: string | null;
  key_regex: string | null;
  clear_match: TrapMatch | SyslogMatch | null;
  clear_after_min: number | null;
  active_conditions: number;
  last_matched_at: string | null;
  created_at: string;
  updated_at: string;
};
export type EventRuleInput = {
  name?: string;
  kind?: EventRuleKind;
  enabled?: boolean;
  priority?: number;
  scope?: RuleScope;
  group_id?: number | null;
  device_id?: number | null;
  match?: TrapMatch | SyslogMatch;
  action?: EventRuleAction;
  key_varbind_oid?: string | null;
  key_regex?: string | null;
  clear_match?: TrapMatch | SyslogMatch | null;
  clear_after_min?: number | null;
};
export type RuleTestItem = {
  kind: EventRuleKind;
  id: number;
  time: string;
  device_name: string | null;
  text: string;
  result: 'raise' | 'clear' | 'ignore';
  key: string;
};
export type RuleTestResult = {
  scanned: number;
  matched: number;
  cleared: number;
  items: RuleTestItem[];
};

export type EventCondition = {
  id: number;
  source:
    | 'rule'
    | 'flap'
    | 'backup_failed'
    | 'host_key'
    | 'config_changed'
    | 'new_mac' // [3]
    | 'lag_degraded'; // [1.3]
  rule_id: number | null;
  rule_name: string | null;
  key: string;
  status: 'warning' | 'down';
  first_at: string;
  last_at: string;
  count: number;
  message: string;
  last_kind: EventRuleKind | null;
  last_ref_id: number | null;
  expires_at: string | null;
};

export type SiemProtocol = 'udp' | 'tcp' | 'tls';
export type SiemTarget = {
  /** server-generated; absent on a new entry */
  id?: string;
  name: string;
  enabled: boolean;
  host: string;
  port: number;
  protocol: SiemProtocol;
  tls_verify: boolean;
  kinds: EventRuleKind[];
  min_severity: number;
  sources: string[];
  include_unknown: boolean;
};

export type TrapV3User = {
  username: string;
  security_level: SnmpSecurityLevel;
  auth_protocol: SnmpAuthProtocol | null;
  auth_key: null;
  auth_key_set: boolean;
  priv_protocol: SnmpPrivProtocol | null;
  priv_key: null;
  priv_key_set: boolean;
};
export type TrapV3UserInput = {
  username: string;
  security_level: SnmpSecurityLevel;
  auth_protocol: SnmpAuthProtocol | null;
  /** null/omitted keep (same username), "" clears, a string replaces */
  auth_key?: string | null;
  priv_protocol: SnmpPrivProtocol | null;
  priv_key?: string | null;
};

export type LdapSecurity = 'ldaps' | 'starttls';
export type LdapSettings = {
  servers: string[];
  security: LdapSecurity;
  tls_verify: boolean;
  domain: string;
  netbios_domain: string;
  base_dn: string;
  bind_dn: string;
  bind_password: null;
  bind_password_set: boolean;
  bind_password_unreadable: boolean;
  admin_group_dn: string;
  viewer_group_dn: string;
  timeout_s: number;
};
export type LdapSettingsInput = Partial<
  Omit<LdapSettings, 'bind_password' | 'bind_password_set' | 'bind_password_unreadable'>
> & { bind_password?: string | null };

export type Settings2b3 = {
  feature_traps_enabled: boolean;
  feature_syslog_enabled: boolean;
  feature_event_alerts_enabled: boolean;
  feature_lag_enabled: boolean;
  feature_ldap_enabled: boolean;
  feature_audit_enabled: boolean;
  feature_timeline_enabled: boolean;
  feature_flapping_enabled: boolean;
  feature_siem_forwarding_enabled: boolean;
  trap_retention_days: number;
  syslog_retention_days: number;
  audit_retention_days: number;
  trap_community_check: 'any' | 'known';
  trap_communities: null;
  trap_communities_count: number;
  trap_v3_users: TrapV3User[];
  ingest_unknown_sources: 'store' | 'drop';
  ingest_ignored_sources: string[];
  /** [1.2.1] repetitive traps: keep the newest `trap_collapse_keep` per source and OID */
  trap_collapse_enabled: boolean;
  trap_collapse_oids: string[];
  trap_collapse_keep: number;
  flap_threshold: number;
  flap_window_min: number;
  flap_quiet_min: number;
  /** [2b-3] owner decision: link-down/up and flapping alerts for important ports only, or all */
  event_linkdown_scope: 'important' | 'all';
  /** [1.3] trunk = a port with at least this many tagged VLANs (1–100, default 2) */
  trunk_min_tagged_vlans?: number;
  /** [1.3] link-down/flapping alerts also for every trunk port (default true) */
  event_linkdown_include_trunks?: boolean;
  siem_targets: SiemTarget[];
  ldap: LdapSettings;
};

export type LdapServerResult = {
  server: string;
  ok: boolean;
  security: LdapSecurity;
  latency_ms: number | null;
  tls: { subject: string; issuer: string; not_after: string; verified: boolean } | null;
  active_directory: boolean;
  error: string | null;
};
export type LdapTestResult = {
  ok: boolean;
  discovered_via_dns: boolean;
  servers: LdapServerResult[];
  service_bind: { ok: boolean; error: string | null };
  base_dn: { ok: boolean; error: string | null };
  admin_group: { dn: string; found: boolean } | null;
  viewer_group: { dn: string; found: boolean } | null;
  message: string;
};
export type LdapStep =
  'connect' | 'service_bind' | 'search_user' | 'account_state' | 'user_bind' | 'groups';
export const LDAP_STEPS: LdapStep[] = [
  'connect',
  'service_bind',
  'search_user',
  'account_state',
  'user_bind',
  'groups',
];
export type LdapUserTestResult = {
  ok: boolean;
  steps: { step: LdapStep; ok: boolean; detail: string; server: string | null }[];
  user: {
    dn: string;
    sam_account_name: string;
    user_principal_name: string | null;
    display_name: string | null;
    mail: string | null;
    disabled: boolean;
    locked: boolean;
  } | null;
  role: Role | null;
  reason: string | null;
};
export const LOGIN_FAILURE_REASONS = [
  'bad_password',
  'unknown_user',
  'inactive',
  'empty_password',
  'not_in_group',
  'account_disabled',
  'account_locked',
  'account_expired',
  'password_expired',
  'password_must_change',
  'not_permitted',
  'ambiguous_user',
  'ldap_disabled',
  'directory_unavailable',
  'rate_limited',
  'token_invalid',
] as const;

export type ActorType = 'user' | 'token' | 'system' | 'anonymous';
export type AuditEntry = {
  id: number;
  time: string;
  actor_type: ActorType;
  actor_user_id: number | null;
  actor_name: string;
  token_id: number | null;
  ip: string | null;
  action: string;
  entity_type: string | null;
  entity_id: number | null;
  entity_name: string | null;
  device_id: number | null;
  source: 'local' | 'ldap' | 'token' | null;
  reason: string | null;
  changes: Record<string, [unknown, unknown]>;
};

export type TimelineKind =
  | 'status'
  | 'notification'
  | 'ack'
  | 'maintenance'
  | 'trap'
  | 'syslog'
  | 'port'
  | 'audit'
  | 'config' // [3]
  | 'new_mac';
export const TIMELINE_KINDS: TimelineKind[] = [
  'status',
  'notification',
  'ack',
  'maintenance',
  'trap',
  'syslog',
  'port',
  'audit',
  'config',
  'new_mac',
];
export type TimelineItem = {
  kind: TimelineKind;
  id: number;
  time: string;
  title: string;
  status: Status | null;
  old_status: Status | null;
  severity: number | null;
  sensor_id: number | null;
  sensor_name: string | null;
  message: string;
  ref: { type: 'event' | 'trap' | 'syslog' | 'audit'; id: number };
  actor: string | null;
};

export type ApiTokenState = 'active' | 'expired' | 'revoked';
export type ApiToken = {
  id: number;
  name: string;
  prefix: string;
  role: Role;
  user_id: number;
  username: string;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  last_used_ip: string | null;
  revoked_at: string | null;
  state: ApiTokenState;
};
