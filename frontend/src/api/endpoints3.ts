// [3] Phase 3 endpoints (docs/api.md "Phase 3"). Field names and paths are exact.
import { api, ApiError, buildUrl, parseError, type Query } from './client';
import type { Page, Sensor } from './types';
import type {
  AllowEntry,
  BackupRunnerStatus,
  BackupStatus,
  ChatAckResolve,
  ChatLink,
  ChatLinkCode,
  ConfigDiff,
  ConfigSearchResult,
  ConfigVersion,
  FlowExporter,
  FlowInterface,
  FlowQuery,
  FlowSeries,
  FlowStatus,
  FlowTop,
  HostKey,
  KerberosSettings,
  KioskScreen,
  KioskScreenInput,
  KioskTokenResponse,
  KioskView,
  KnownMac,
  KnownMacState,
  NewDeviceStatus,
  OidcSettings,
  OidcSettingsInput,
  OidcTestResult,
  Report,
  ReportData,
  ReportInput,
  ReportRun,
  SshCredentialsInput,
  SshTestResult,
  TelegramPoller,
} from './types3';

export const qk3 = {
  backups: (q: object) => ['config-backups', q] as const,
  backupRunner: ['config-backups-status'] as const,
  backupSearch: (q: object) => ['config-backups-search', q] as const,
  deviceConfig: (id: number) => ['device-config', id] as const,
  configVersions: (id: number, page: number) => ['config-versions', id, page] as const,
  configVersion: (id: number, vid: number) => ['config-version', id, vid] as const,
  configDiff: (id: number, from: number, to: number | null) =>
    ['config-diff', id, from, to] as const,
  flowStatus: ['flows-status'] as const,
  flowExporters: ['flows-exporters'] as const,
  flowTop: (kind: string, q: object) => ['flows-top', kind, q] as const,
  flowInterfaces: (q: object) => ['flows-interfaces', q] as const,
  flowSeries: (q: object) => ['flows-series', q] as const,
  newDeviceStatus: ['new-devices-status'] as const,
  newDevices: (q: object) => ['new-devices', q] as const,
  allowlist: ['new-devices-allowlist'] as const,
  kerberos: ['sso-kerberos'] as const,
  oidc: ['sso-oidc'] as const,
  reports: ['reports'] as const,
  report: (id: number) => ['report', id] as const,
  reportRuns: (q: object) => ['report-runs', q] as const,
  reportRun: (id: number) => ['report-run', id] as const,
  kioskScreens: ['kiosk-screens'] as const,
  chatLinks: (all: boolean) => ['chat-links', all] as const,
  telegramStatus: ['chat-ack-telegram-status'] as const,
};

export type BackupsQuery = {
  group_id?: number;
  result?: string;
  q?: string;
  page?: number;
  page_size?: number;
};
export type ConfigSearchQuery = {
  q: string;
  regex?: boolean;
  group_id?: number;
  all_versions?: boolean;
  since?: string;
  until?: string;
};
export type NewDevicesQuery = {
  state?: KnownMacState[];
  device_id?: number;
  group_id?: number;
  q?: string;
  since?: string;
  page?: number;
  page_size?: number;
};
export type RunReportBody = { period_start?: string; period_end?: string; send?: boolean };

/** multipart/form-data upload (keytab); same error envelope as JSON requests. */
async function upload<T>(path: string, file: File): Promise<T> {
  const fd = new FormData();
  fd.append('file', file);
  let res: Response;
  try {
    res = await fetch(buildUrl(path), {
      method: 'POST',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      body: fd,
    });
  } catch (e) {
    throw new ApiError(0, 'network_error', e instanceof Error ? e.message : 'Network error');
  }
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as T;
}

/** Kiosk requests: no session, token in a header; every failure is a plain error (no login redirect). */
async function kioskGet<T>(path: string, token: string, signal?: AbortSignal): Promise<T> {
  let res: Response;
  try {
    res = await fetch(buildUrl(path), {
      method: 'GET',
      credentials: 'omit',
      cache: 'no-store',
      headers: { Accept: 'application/json', 'X-Netmon-Kiosk': token },
      signal,
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    throw new ApiError(0, 'network_error', e instanceof Error ? e.message : 'Network error');
  }
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as T;
}

const enc = encodeURIComponent;

export const endpoints3 = {
  // config backup
  backups: (q: BackupsQuery) =>
    api.get<Page<BackupStatus>>('/config-backups', { page_size: 100, ...q } as Query),
  backupRunner: () => api.get<BackupRunnerStatus>('/config-backups/status'),
  backupAll: (device_ids?: number[]) =>
    api.post<{ queued: number }>('/config-backups/run', device_ids ? { device_ids } : {}),
  searchConfigs: (q: ConfigSearchQuery) =>
    api.get<ConfigSearchResult>('/config-backups/search', q as Query),
  exportZipUrl: (group_id?: number) =>
    buildUrl('/config-backups/export.zip', group_id ? { group_id } : undefined),
  deviceConfig: (id: number) => api.get<BackupStatus>(`/devices/${id}/config`),
  backupNow: (id: number) => api.post<{ queued: number }>(`/devices/${id}/config/backup`),
  configVersions: (id: number, page = 1, page_size = 50) =>
    api.get<Page<ConfigVersion>>(`/devices/${id}/config/versions`, { page, page_size }),
  configVersion: (id: number, vid: number) =>
    api.get<ConfigVersion & { content: string }>(`/devices/${id}/config/versions/${vid}`),
  configDownloadUrl: (id: number, vid: number) =>
    buildUrl(`/devices/${id}/config/versions/${vid}/download`),
  configDiff: (id: number, from: number, to?: number | null) =>
    api.get<ConfigDiff>(`/devices/${id}/config/diff`, { from, to: to ?? undefined }),
  acceptHostKey: (id: number, fingerprint: string) =>
    api.post<HostKey>(`/devices/${id}/config/host-key/accept`, { fingerprint }),
  resetHostKey: (id: number) => api.del(`/devices/${id}/config/host-key`),
  sshTest: (id: number, body: { ssh_credentials?: SshCredentialsInput; driver?: string | null }) =>
    api.post<SshTestResult>(`/devices/${id}/ssh/test`, body),

  // flows
  flowStatus: () => api.get<FlowStatus>('/flows/status'),
  flowExporters: (state?: string) =>
    api.get<{ items: FlowExporter[] }>('/flows/exporters', state ? { state } : undefined),
  updateExporter: (
    id: number,
    body: {
      state?: 'accepted' | 'ignored';
      device_id?: number | null;
      sampling_override?: number | null;
    },
  ) => api.patch<FlowExporter>(`/flows/exporters/${id}`, body),
  deleteExporter: (id: number) => api.del(`/flows/exporters/${id}`),
  topTalkers: (q: FlowQuery & { side?: 'src' | 'dst' | 'both' }) =>
    api.get<FlowTop>('/flows/top/talkers', q as Query),
  topApplications: (q: FlowQuery) => api.get<FlowTop>('/flows/top/applications', q as Query),
  topConversations: (q: FlowQuery) => api.get<FlowTop>('/flows/top/conversations', q as Query),
  flowInterfaces: (q: { exporter_id?: number; since?: string; until?: string }) =>
    api.get<{ items: FlowInterface[] }>('/flows/interfaces', q as Query),
  flowSeries: (q: FlowQuery & { split?: 'none' | 'app' | 'host' }) =>
    api.get<FlowSeries>('/flows/series', q as Query),

  // new devices
  newDeviceStatus: () => api.get<NewDeviceStatus>('/new-devices/status'),
  newDevices: (q: NewDevicesQuery) =>
    api.get<Page<KnownMac>>('/new-devices', { page_size: 100, ...q } as Query),
  acknowledgeMacs: (macs: string[], comment: string) =>
    api.post<{ acknowledged: number }>('/new-devices/acknowledge', { macs, comment }),
  forgetMac: (mac: string) => api.del(`/new-devices/${enc(mac)}`),
  restartLearning: () => api.post<NewDeviceStatus>('/new-devices/restart-learning'),
  allowlist: () => api.get<{ items: AllowEntry[] }>('/new-devices/allowlist'),
  addAllow: (prefix: string, comment: string) =>
    api.post<AllowEntry>('/new-devices/allowlist', { prefix, comment }),
  deleteAllow: (id: number) => api.del(`/new-devices/allowlist/${id}`),

  // SSO
  kerberosSso: () =>
    fetch('/api/auth/sso/kerberos', {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    }),
  oidcStartUrl: (next?: string) => buildUrl('/auth/oidc/start', next ? { next } : undefined),
  kerberos: () => api.get<KerberosSettings>('/sso/kerberos'),
  updateKerberos: (body: { realms?: string[]; auto_login?: boolean }) =>
    api.put<KerberosSettings>('/sso/kerberos', body),
  uploadKeytab: (file: File) => upload<KerberosSettings>('/sso/kerberos/keytab', file),
  deleteKeytab: () => api.del<KerberosSettings>('/sso/kerberos/keytab'),
  oidc: () => api.get<OidcSettings>('/sso/oidc'),
  updateOidc: (body: OidcSettingsInput) => api.put<OidcSettings>('/sso/oidc', body),
  testOidc: () => api.post<OidcTestResult>('/sso/oidc/test'),

  // reports
  reports: () => api.get<{ items: Report[] }>('/reports'),
  report: (id: number) => api.get<Report>(`/reports/${id}`),
  createReport: (body: ReportInput) => api.post<Report>('/reports', body),
  updateReport: (id: number, body: ReportInput) => api.patch<Report>(`/reports/${id}`, body),
  deleteReport: (id: number) => api.del(`/reports/${id}`),
  runReport: (id: number, body: RunReportBody = {}) =>
    api.post<ReportRun>(`/reports/${id}/run`, body),
  previewReport: (report: ReportInput, period_start?: string, period_end?: string) =>
    api.post<ReportRun>('/reports/preview', { report, period_start, period_end }),
  reportRuns: (q: { report_id?: number; page?: number; page_size?: number }) =>
    api.get<Page<ReportRun>>('/reports/runs', { page_size: 50, ...q }),
  reportRun: (id: number) => api.get<ReportRun & { data: ReportData }>(`/reports/runs/${id}`),
  reportHtmlUrl: (id: number) => buildUrl(`/reports/runs/${id}/html`),
  resendReport: (id: number, targets?: string[]) =>
    api.post<ReportRun>(`/reports/runs/${id}/resend`, targets ? { targets } : {}),

  // NOC kiosk (admin management)
  kioskScreens: () => api.get<{ items: KioskScreen[] }>('/kiosk-screens'),
  createKioskScreen: (body: KioskScreenInput) =>
    api.post<KioskTokenResponse>('/kiosk-screens', body),
  updateKioskScreen: (id: number, body: KioskScreenInput) =>
    api.patch<KioskScreen>(`/kiosk-screens/${id}`, body),
  regenerateKiosk: (id: number) => api.post<KioskTokenResponse>(`/kiosk-screens/${id}/regenerate`),
  deleteKioskScreen: (id: number) => api.del(`/kiosk-screens/${id}`),
  // kiosk access (no session)
  kioskView: (token: string, signal?: AbortSignal) =>
    kioskGet<KioskView>('/kiosk/view', token, signal),

  // chat acknowledge
  resolveAck: (token: string) => api.post<ChatAckResolve>('/chat-ack/resolve', { token }),
  acknowledgeAck: (token: string, comment: string) =>
    api.post<Sensor>('/chat-ack/acknowledge', { token, comment }),
  chatLinks: (all = false) =>
    api.get<{ items: ChatLink[] }>('/chat-links', all ? { all: true } : undefined),
  createLinkCode: () => api.post<ChatLinkCode>('/chat-links/codes', { provider: 'telegram' }),
  deleteChatLink: (id: number) => api.del(`/chat-links/${id}`),
  telegramStatus: () => api.get<{ pollers: TelegramPoller[] }>('/chat-ack/telegram/status'),
  takeOverBot: (channelId: number) =>
    api.post<{ ok: boolean }>(`/notification-channels/${channelId}/telegram/take-over`),
};
