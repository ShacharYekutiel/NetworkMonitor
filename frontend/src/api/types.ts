// Hand-written mirror of docs/api.md. Field names are exact.

export type Status = 'up' | 'warning' | 'down' | 'paused' | 'unknown';
export type ChannelStatus = 'up' | 'warning' | 'down' | 'unknown';
export type Role = 'admin' | 'viewer';
export type Language = 'en' | 'he';

export type StatusCounts = {
  up: number;
  warning: number;
  down: number;
  paused: number;
  unknown: number;
  total: number;
};

export type Page<T> = { items: T[]; total: number; page: number; page_size: number };

export type ApiErrorDetail = { loc?: (string | number)[]; msg?: string; field?: string };
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
};

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
  created_at: string;
  updated_at: string;
};

export type SensorType =
  | 'ping'
  | 'snmp_system'
  | 'snmp_cpu'
  | 'snmp_memory'
  | 'snmp_interface'
  | 'http'
  | 'tcp_port'
  | 'dns';

export const SENSOR_TYPES: SensorType[] = [
  'ping',
  'snmp_system',
  'snmp_cpu',
  'snmp_memory',
  'snmp_interface',
  'http',
  'tcp_port',
  'dns',
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
  created_at: string;
  updated_at: string;
};

export type EventKind = 'status_change' | 'notification' | 'system';

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
};
export type TreeDevice = {
  id: number;
  name: string;
  host: string;
  enabled: boolean;
  status: Status;
  status_counts: StatusCounts;
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
  'int' | 'str' | 'bool' | 'enum' | 'int_list' | 'str_nullable' | 'int_nullable' | 'headers';

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

export type SensorTypeDef = {
  type: SensorType;
  requires_snmp: boolean;
  primary_channel: string;
  config_fields: ConfigField[];
  channels: ChannelDef[];
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
  problem_sensors: Sensor[];
};

export type EmailChannelConfig = { to: string[] };
export type WebhookChannelConfig = {
  url: string;
  headers: Record<string, string>;
  verify_tls: boolean;
  timeout_s: number;
};
export type NotificationChannel = {
  id: number;
  name: string;
  type: 'email' | 'webhook';
  enabled: boolean;
  config: EmailChannelConfig | WebhookChannelConfig;
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
};
export type SettingsInput = Partial<Omit<Settings, 'smtp'>> & {
  smtp?: Partial<Omit<SmtpSettings, 'password' | 'password_set'>> & { password?: string | null };
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
        entity: 'group' | 'device' | 'sensor' | 'alert_rule' | 'notification_channel';
        id: number;
        action: 'created' | 'updated' | 'deleted';
      };
    }
  | { type: 'pong'; ts: string; data: Record<string, never> };
