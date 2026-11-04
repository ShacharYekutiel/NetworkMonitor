import { api } from './client';
import type {
  AlertRule,
  ChannelThresholdInput,
  CheckResult,
  Dashboard,
  Device,
  Group,
  History,
  HistoryRange,
  Language,
  NmEvent,
  NotificationChannel,
  Page,
  Role,
  Sensor,
  SensorType,
  SensorTypeDef,
  Settings,
  SettingsInput,
  SnmpCredentialsInput,
  SnmpInterface,
  TestResult,
  Tree,
  User,
} from './types';

export const qk = {
  setupStatus: ['setupStatus'] as const,
  publicSettings: ['publicSettings'] as const,
  me: ['me'] as const,
  tree: ['tree'] as const,
  dashboard: ['dashboard'] as const,
  sensorTypes: ['sensorTypes'] as const,
  group: (id: number) => ['group', id] as const,
  groups: (params?: object) => ['groups', params ?? {}] as const,
  device: (id: number) => ['device', id] as const,
  devices: (params?: object) => ['devices', params ?? {}] as const,
  sensor: (id: number) => ['sensor', id] as const,
  sensors: (params?: object) => ['sensors', params ?? {}] as const,
  history: (id: number, range: HistoryRange) => ['history', id, range] as const,
  events: (params?: object) => ['events', params ?? {}] as const,
  snmpInterfaces: (deviceId: number) => ['snmpInterfaces', deviceId] as const,
  channels: ['notificationChannels'] as const,
  alertRules: (params?: object) => ['alertRules', params ?? {}] as const,
  settings: ['settings'] as const,
  users: ['users'] as const,
};

export type GroupInput = {
  name?: string;
  parent_id?: number | null;
  description?: string;
  snmp_credentials?: SnmpCredentialsInput | null;
  sort_order?: number;
};

export type DeviceInput = {
  group_id?: number;
  name?: string;
  host?: string;
  description?: string;
  enabled?: boolean;
  snmp_credentials?: SnmpCredentialsInput | null;
  sort_order?: number;
  auto_sensors?: SensorType[];
};

export type SensorInput = {
  device_id?: number;
  name?: string;
  type?: SensorType;
  config?: Record<string, unknown>;
  interval_s?: number;
  enabled?: boolean;
  sort_order?: number;
  channels?: ChannelThresholdInput[];
};

export type EventQuery = {
  sensor_id?: number;
  device_id?: number;
  group_id?: number;
  kind?: string;
  status?: string;
  since?: string;
  until?: string;
  page?: number;
  page_size?: number;
};

export type UserInput = {
  username?: string;
  password?: string;
  email?: string | null;
  role?: Role;
  language?: Language;
  is_active?: boolean;
};

export type ChannelInput = Partial<
  Pick<NotificationChannel, 'name' | 'type' | 'enabled' | 'config'>
>;
export type AlertRuleInput = Partial<
  Pick<AlertRule, 'name' | 'sensor_id' | 'on_statuses' | 'delay_s' | 'enabled' | 'channel_ids'>
>;

export const endpoints = {
  // setup / auth
  setupStatus: () =>
    api.get<{ setup_required: boolean }>('/setup/status', undefined, { skipAuthRedirect: true }),
  setup: (body: { username: string; password: string; email: string | null; language: Language }) =>
    api.post<{ user: User }>('/setup', body, { skipAuthRedirect: true }),
  publicSettings: () =>
    api.get<{ default_language: Language }>('/settings/public', undefined, {
      skipAuthRedirect: true,
    }),
  me: () => api.get<{ user: User }>('/auth/me', undefined, { skipAuthRedirect: true }),
  updateMe: (body: { email?: string | null; language?: Language }) =>
    api.patch<{ user: User }>('/auth/me', body),
  login: (username: string, password: string) =>
    api.post<{ user: User }>('/auth/login', { username, password }, { skipAuthRedirect: true }),
  logout: () => api.post<void>('/auth/logout', undefined, { skipAuthRedirect: true }),
  changePassword: (current_password: string, new_password: string) =>
    api.post<void>('/auth/change-password', { current_password, new_password }),

  // users
  users: () => api.get<Page<User>>('/users', { page_size: 1000 }),
  createUser: (body: UserInput) => api.post<User>('/users', body),
  updateUser: (id: number, body: UserInput) => api.patch<User>(`/users/${id}`, body),
  deleteUser: (id: number) => api.del(`/users/${id}`),

  // tree / dashboard
  tree: () => api.get<Tree>('/tree'),
  dashboard: () => api.get<Dashboard>('/dashboard'),
  health: () => api.get<import('./types').Health>('/health'),

  // groups
  groups: (params?: { parent_id?: number | 'root' }) =>
    api.get<Page<Group>>('/groups', { page_size: 1000, ...params }),
  group: (id: number) => api.get<Group>(`/groups/${id}`),
  createGroup: (body: GroupInput) => api.post<Group>('/groups', body),
  updateGroup: (id: number, body: GroupInput) => api.patch<Group>(`/groups/${id}`, body),
  deleteGroup: (id: number) => api.del(`/groups/${id}`),

  // devices
  devices: (params?: { group_id?: number; status?: string; q?: string }) =>
    api.get<Page<Device>>('/devices', { page_size: 1000, ...params }),
  device: (id: number) => api.get<Device>(`/devices/${id}`),
  createDevice: (body: DeviceInput) => api.post<Device>('/devices', body),
  updateDevice: (id: number, body: DeviceInput) => api.patch<Device>(`/devices/${id}`, body),
  deleteDevice: (id: number) => api.del(`/devices/${id}`),
  snmpInterfaces: (id: number) =>
    api.get<{ items: SnmpInterface[] }>(`/devices/${id}/snmp/interfaces`),

  // sensors
  sensorTypes: () => api.get<{ items: SensorTypeDef[] }>('/sensor-types'),
  sensors: (params?: {
    device_id?: number;
    group_id?: number;
    status?: string;
    type?: string;
    q?: string;
  }) => api.get<Page<Sensor>>('/sensors', { page_size: 1000, ...params }),
  sensor: (id: number) => api.get<Sensor>(`/sensors/${id}`),
  createSensor: (body: SensorInput) => api.post<Sensor>('/sensors', body),
  updateSensor: (id: number, body: SensorInput) => api.patch<Sensor>(`/sensors/${id}`, body),
  deleteSensor: (id: number) => api.del(`/sensors/${id}`),
  runSensor: (id: number) => api.post<CheckResult>(`/sensors/${id}/run`),
  testSensor: (body: { device_id: number; type: SensorType; config: Record<string, unknown> }) =>
    api.post<CheckResult>('/sensors/test', body),
  history: (id: number, range: HistoryRange) =>
    api.get<History>(`/sensors/${id}/history`, { range }),

  // events
  events: (q: EventQuery) => api.get<Page<NmEvent>>('/events', q),

  // notifications
  channels: () => api.get<Page<NotificationChannel>>('/notification-channels', { page_size: 1000 }),
  createChannel: (body: ChannelInput) =>
    api.post<NotificationChannel>('/notification-channels', body),
  updateChannel: (id: number, body: ChannelInput) =>
    api.patch<NotificationChannel>(`/notification-channels/${id}`, body),
  deleteChannel: (id: number) => api.del(`/notification-channels/${id}`),
  testChannel: (id: number) => api.post<TestResult>(`/notification-channels/${id}/test`),
  alertRules: (params?: { sensor_id?: number | 'global' }) =>
    api.get<Page<AlertRule>>('/alert-rules', { page_size: 1000, ...params }),
  createAlertRule: (body: AlertRuleInput) => api.post<AlertRule>('/alert-rules', body),
  updateAlertRule: (id: number, body: AlertRuleInput) =>
    api.patch<AlertRule>(`/alert-rules/${id}`, body),
  deleteAlertRule: (id: number) => api.del(`/alert-rules/${id}`),

  // settings
  settings: () => api.get<Settings>('/settings'),
  updateSettings: (body: SettingsInput) => api.put<Settings>('/settings', body),
  testSmtp: (to: string) => api.post<TestResult>('/settings/smtp/test', { to }),
};
