import { api } from './client';
import type {
  DependencyBulkItem,
  DependencyInput,
  DependencySuggestions,
  DeviceIdentity,
  LayoutPosition,
  MaintenanceActive,
  MaintenanceWindow,
  MaintenanceWindowInput,
  Occurrence,
  TopologyGraph,
  TopologyLink,
  TopologyLinkInput,
  TopologyRefresh,
  AlertRule,
  ApplyTemplateResult,
  DeviceTemplate,
  DeviceTemplatesResponse,
  DevicePorts,
  DiscoveryProfile,
  InventoryMode,
  MacSearchResult,
  PortDetail,
  PortKind,
  TemplateLibraryStatus,
  DiscoveryResult,
  DiscoveryScan,
  ImportRequest,
  ImportResponse,
  ScanOptionsInput,
  SnmpStorage,
  TelegramChannelConfigInput,
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
  // [2a]
  snmpStorage: (deviceId: number) => ['snmpStorage', deviceId] as const,
  deviceTemplates: ['deviceTemplates'] as const,
  discoveryProfiles: ['discoveryProfiles'] as const,
  discoveryScans: (params?: object) => ['discoveryScans', params ?? {}] as const,
  discoveryScan: (id: number) => ['discoveryScan', id] as const,
  /** All results of a scan (unfiltered; filters are applied client-side so WS rows merge in). */
  discoveryResults: (scanId: number) => ['discoveryResults', scanId] as const,
  // [2b-1]
  maintenanceWindows: (params?: object) => ['maintenanceWindows', params ?? {}] as const,
  maintenanceWindow: (id: number) => ['maintenanceWindow', id] as const,
  occurrences: (id: number, days: number) => ['occurrences', id, days] as const,
  maintenanceActive: ['maintenanceActive'] as const,
  topology: (params?: object) => ['topology', params ?? {}] as const,
  topologyLinks: (params?: object) => ['topologyLinks', params ?? {}] as const,
  dependencySuggestions: (root?: number | null) => ['dependencySuggestions', root ?? null] as const,
  // [2b-2]
  devicePorts: (deviceId: number) => ['device-ports', deviceId] as const,
  portDetail: (deviceId: number, ifIndex: number, includeUplinkMacs = false) =>
    ['port-detail', deviceId, ifIndex, includeUplinkMacs] as const,
  macSearch: (q: MacSearchQuery) => ['mac-search', q] as const,
};

/** [2b-2] exactly one of q / ip */
export type MacSearchQuery = { q: string } | { ip: string };

export type GroupInput = {
  name?: string;
  parent_id?: number | null;
  description?: string;
  snmp_credentials?: SnmpCredentialsInput | null;
  sort_order?: number;
  /** [2b-1] */
  dependency?: DependencyInput;
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
  /** [2b-1] */
  dependency?: DependencyInput;
  /** [2b-2] */
  port_inventory?: InventoryMode;
  arp_collect?: InventoryMode;
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
  /** [2b-1] */
  in_maintenance?: boolean;
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

export type ChannelInput = Partial<Pick<NotificationChannel, 'name' | 'type' | 'enabled'>> & {
  config?: NotificationChannel['config'] | TelegramChannelConfigInput;
};

export type DiscoveryProfileInput = {
  name?: string;
  targets?: string[];
  group_id?: number;
  options?: ScanOptionsInput;
  schedule_enabled?: boolean;
  schedule_every_hours?: number;
  notify_new_hosts?: boolean;
  notify_channel_ids?: number[];
};

export type ScanRequest = { targets: string[]; group_id: number; options?: ScanOptionsInput };

export type ResultsQuery = {
  only_new?: boolean;
  hide_existing?: boolean;
  device_class?: string;
  q?: string;
  page?: number;
  page_size?: number;
};

const RESULTS_PAGE = 1000;

/** Fetches every result page of a scan (≤ 4096 hosts, so at most a few pages). */
async function allResults(scanId: number, q: ResultsQuery = {}): Promise<DiscoveryResult[]> {
  const out: DiscoveryResult[] = [];
  for (let page = 1; page <= 10; page++) {
    const r = await api.get<Page<DiscoveryResult>>(`/discovery/scans/${scanId}/results`, {
      ...q,
      page,
      page_size: RESULTS_PAGE,
    });
    out.push(...r.items);
    if (out.length >= r.total || r.items.length < RESULTS_PAGE) break;
  }
  return out;
}
export type AlertRuleInput = Partial<
  Pick<
    AlertRule,
    | 'name'
    | 'sensor_id'
    | 'on_statuses'
    | 'delay_s'
    | 'enabled'
    | 'channel_ids'
    | 'escalation_after_s'
    | 'escalation_channel_ids'
    | 'repeat_every_s'
  >
>;

export type MaintenanceQuery = {
  scope?: string;
  group_id?: number;
  device_id?: number;
  active?: boolean;
  include_past?: boolean;
};

export type TopologyQuery = { group_id?: number; include_stale?: boolean };

export const endpoints = {
  // setup / auth
  setupStatus: () =>
    api.get<{ setup_required: boolean }>('/setup/status', undefined, { skipAuthRedirect: true }),
  setup: (body: {
    username: string;
    password: string;
    email: string | null;
    language: Language;
    /** [2b-1] optional IANA zone (the browser's) */
    timezone?: string;
  }) => api.post<{ user: User }>('/setup', body, { skipAuthRedirect: true }),
  publicSettings: () =>
    api.get<{ default_language: Language; timezone?: string }>('/settings/public', undefined, {
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
    acknowledged?: boolean;
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

  // [2a] snmp storage, device templates
  snmpStorage: (id: number) => api.get<{ items: SnmpStorage[] }>(`/devices/${id}/snmp/storage`),
  deviceTemplates: () => api.get<DeviceTemplatesResponse>('/device-templates'),
  /** [2b-2] admin */
  reloadTemplates: () => api.post<TemplateLibraryStatus>('/device-templates/reload'),
  deviceTemplate: (id: string) =>
    api.get<DeviceTemplate>(`/device-templates/${encodeURIComponent(id)}`),
  applyTemplate: (deviceId: number, template_id: string) =>
    api.post<ApplyTemplateResult>(`/devices/${deviceId}/apply-template`, { template_id }),

  // [2a] discovery
  discoveryProfiles: () =>
    api.get<Page<DiscoveryProfile>>('/discovery/profiles', { page_size: 1000 }),
  discoveryProfile: (id: number) => api.get<DiscoveryProfile>(`/discovery/profiles/${id}`),
  createDiscoveryProfile: (body: DiscoveryProfileInput) =>
    api.post<DiscoveryProfile>('/discovery/profiles', body),
  updateDiscoveryProfile: (id: number, body: DiscoveryProfileInput) =>
    api.patch<DiscoveryProfile>(`/discovery/profiles/${id}`, body),
  deleteDiscoveryProfile: (id: number) => api.del(`/discovery/profiles/${id}`),
  runDiscoveryProfile: (id: number) => api.post<DiscoveryScan>(`/discovery/profiles/${id}/run`),
  startScan: (body: ScanRequest) => api.post<DiscoveryScan>('/discovery/scans', body),
  discoveryScans: (params?: { profile_id?: number; page?: number; page_size?: number }) =>
    api.get<Page<DiscoveryScan>>('/discovery/scans', { page_size: 100, ...params }),
  discoveryScan: (id: number) => api.get<DiscoveryScan>(`/discovery/scans/${id}`),
  cancelScan: (id: number) => api.post<DiscoveryScan>(`/discovery/scans/${id}/cancel`),
  deleteScan: (id: number) => api.del(`/discovery/scans/${id}`),
  discoveryResults: allResults,
  importResults: (scanId: number, body: ImportRequest) =>
    api.post<ImportResponse>(`/discovery/scans/${scanId}/import`, body),

  // settings
  settings: () => api.get<Settings>('/settings'),
  updateSettings: (body: SettingsInput) => api.put<Settings>('/settings', body),
  testSmtp: (to: string) => api.post<TestResult>('/settings/smtp/test', { to }),

  // [2b-1] dependencies
  bulkDependencies: (items: DependencyBulkItem[]) =>
    api.post<{ updated: number[] }>('/devices/dependencies', { items }),
  identifyDevice: (id: number) => api.post<DeviceIdentity>(`/devices/${id}/identify`),

  // [2b-1] acknowledge
  acknowledge: (id: number, comment: string) =>
    api.post<Sensor>(`/sensors/${id}/acknowledge`, comment ? { comment } : {}),
  unacknowledge: (id: number) => api.del<Sensor>(`/sensors/${id}/acknowledge`),

  // [2b-1] maintenance windows
  maintenanceWindows: (q: MaintenanceQuery = {}) =>
    api.get<Page<MaintenanceWindow>>('/maintenance-windows', { page_size: 1000, ...q }),
  maintenanceWindow: (id: number) => api.get<MaintenanceWindow>(`/maintenance-windows/${id}`),
  createMaintenanceWindow: (body: MaintenanceWindowInput) =>
    api.post<MaintenanceWindow>('/maintenance-windows', body),
  updateMaintenanceWindow: (id: number, body: MaintenanceWindowInput) =>
    api.patch<MaintenanceWindow>(`/maintenance-windows/${id}`, body),
  deleteMaintenanceWindow: (id: number) => api.del(`/maintenance-windows/${id}`),
  occurrences: (id: number, days = 14) =>
    api.get<{ items: Occurrence[] }>(`/maintenance-windows/${id}/occurrences`, { days }),
  maintenanceActive: () => api.get<MaintenanceActive>('/maintenance/active'),

  // [2b-1] topology
  topology: (q: TopologyQuery = {}) => api.get<TopologyGraph>('/topology', q),
  refreshTopology: () => api.post<TopologyRefresh>('/topology/refresh'),
  topologyLinks: (q: { device_id?: number; source?: string; stale?: boolean } = {}) =>
    api.get<Page<TopologyLink>>('/topology/links', { page_size: 1000, ...q }),
  createTopologyLink: (body: TopologyLinkInput) => api.post<TopologyLink>('/topology/links', body),
  updateTopologyLink: (id: number, body: Partial<TopologyLinkInput>) =>
    api.patch<TopologyLink>(`/topology/links/${id}`, body),
  deleteTopologyLink: (id: number) => api.del(`/topology/links/${id}`),
  deleteStaleLinks: () => api.del<{ deleted: number }>('/topology/links', { stale: true }),
  saveLayout: (positions: LayoutPosition[]) => api.put<void>('/topology/layout', { positions }),
  resetLayout: () => api.del('/topology/layout'),
  // [2b-2] ports and MAC/IP search
  devicePorts: (id: number, kinds?: PortKind[]) =>
    api.get<DevicePorts>(`/devices/${id}/ports`, kinds?.length ? { kind: kinds } : undefined),
  portDetail: (id: number, ifIndex: number, includeUplinkMacs = false) =>
    api.get<PortDetail>(
      `/devices/${id}/ports/${ifIndex}`,
      includeUplinkMacs ? { include_uplink_macs: true } : undefined,
    ),
  refreshPorts: (id: number) =>
    api.post<{ device_id: number; queued: boolean }>(`/devices/${id}/ports/refresh`),
  searchMac: (q: MacSearchQuery) => api.get<MacSearchResult>('/search/mac', q),

  dependencySuggestions: (root_device_id?: number | null) =>
    api.get<DependencySuggestions>(
      '/topology/dependency-suggestions',
      root_device_id ? { root_device_id } : undefined,
    ),
};
