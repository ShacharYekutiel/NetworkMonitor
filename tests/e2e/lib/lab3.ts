// Phase 3 helpers: feature toggles, SSH device lab, flow senders, SMTP sink, OIDC mock, Teams/Telegram mocks,
// fake AD + KDC, kiosk client (docs/architecture.md §13.11, docs/api.md "[3] Phase 3 contract").
// Simulators are started by tests/e2e/run.sh (E2E_PHASE3=1); every helper reads its URL from the environment.
import { expect, type Page } from '@playwright/test';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Api, uid } from './helpers';
import { PY, SIM_DIR, TELEGRAM_MOCK_URL, raw } from './lab';

export const SSH_SIM_URL = process.env.SSH_SIM_URL ?? 'http://127.0.0.1:18622';
export const SMTP_SINK_URL = process.env.SMTP_SINK_URL ?? 'http://127.0.0.1:18026';
export const SMTP_PORT = Number(process.env.SMTP_PORT ?? 18025);
export const OIDC_MOCK_URL = process.env.OIDC_MOCK_URL ?? 'http://127.0.0.1:18900';
export const TEAMS_CONTROL_URL = process.env.TEAMS_CONTROL_URL ?? 'http://127.0.0.1:18444';
export const TEAMS_MOCK_PORT = Number(process.env.TEAMS_MOCK_PORT ?? 18443);
export const FAKE_AD_URL = process.env.FAKE_AD_URL ?? 'http://127.0.0.1:18389';
export const LDAPS_PORT = Number(process.env.LDAPS_PORT ?? 16360);
export const SYSLOG_PORT = Number(process.env.SYSLOG_PORT ?? 514);
export const NETFLOW_PORT = Number(process.env.NETFLOW_PORT ?? 2055);
export const IPFIX_PORT = Number(process.env.IPFIX_PORT ?? 4739);
export const SFLOW_PORT = Number(process.env.SFLOW_PORT ?? 6343);
export const KDC_DIR = process.env.KDC_DIR ?? '';
export const SHOT_DIR_3 = process.env.QA3_SHOT_DIR ?? join(__dirname, '..', 'test-results', 'screenshots', 'qa-3');
export const PHASE3 = !!process.env.SSH_SIM_URL;

export const P3_FEATURES = [
  'config_backup',
  'flows',
  'new_device_alerts',
  'sso_kerberos',
  'sso_oidc',
  'reports',
  'noc',
  'chat_ack',
] as const;
export type P3Feature = (typeof P3_FEATURES)[number];

// ---------------------------------------------------------------- feature toggles (§13.0)
export async function features(api: Api): Promise<Record<string, boolean>> {
  return (await api.get('/api/settings/public')).features;
}
/** PUT /api/settings feature_<name>_enabled; waits until /api/settings/public reflects it (<= 5 s rule). */
export async function setFeatures(api: Api, map: Record<string, boolean>): Promise<void> {
  const body: Record<string, boolean> = {};
  for (const [k, v] of Object.entries(map)) body[`feature_${k}_enabled`] = v;
  const r = await raw(api, 'PUT', '/api/settings', body);
  expect(r.status, `PUT features ${JSON.stringify(body)}: ${r.text}`).toBe(200);
  await expect
    .poll(async () => {
      const f = await features(api);
      return Object.entries(map).every(([k, v]) => f[k] === v);
    }, { timeout: 10_000, message: `features applied ${JSON.stringify(map)}` })
    .toBe(true);
}

/** First feature-gated endpoint per phase-3 feature (409 feature_disabled while OFF; kiosk: 404). */
export const GATED: Record<P3Feature, { method: string; path: string; off: number }> = {
  config_backup: { method: 'GET', path: '/api/config-backups', off: 409 },
  flows: { method: 'GET', path: '/api/flows/status', off: 409 },
  new_device_alerts: { method: 'GET', path: '/api/new-devices/status', off: 409 },
  sso_kerberos: { method: 'GET', path: '/api/auth/sso/kerberos', off: 409 },
  sso_oidc: { method: 'GET', path: '/api/auth/oidc/start?next=/', off: 409 },
  reports: { method: 'GET', path: '/api/reports', off: 409 },
  noc: { method: 'GET', path: '/api/kiosk-screens', off: 409 },
  chat_ack: { method: 'GET', path: '/api/chat-links', off: 409 },
};

// ---------------------------------------------------------------- SSH device lab (tests/sim/ssh)
export const SSH_USER = 'netmon-backup';
export const SSH_PASS = 'Backup-pass-1';
export const SSH_ENABLE = 'Enable-pass-1';
export const SSH_PORT = 8022;
export const SSH = {
  cw: { name: 'sw-cw1', ip: '127.0.0.111', driver: 'hpe_comware' },
  aos: { name: 'sw-aos1', ip: '127.0.0.112', driver: 'hpe_arubaos_switch' },
  cx: { name: 'sw-cx1', ip: '127.0.0.113', driver: 'aruba_cx' },
  cp: { name: 'gw-hq', ip: '127.0.0.114', driver: 'checkpoint_gaia' },
  cpBash: { name: 'gw-bash', ip: '127.0.0.115', driver: 'checkpoint_gaia' },
  ios: { name: 'r1', ip: '127.0.0.116', driver: 'cisco_ios' },
  fgt: { name: 'fgt1', ip: '127.0.0.117', driver: 'fortigate' },
  rb: { name: 'rb1', ip: '127.0.0.118', driver: 'mikrotik_routeros' },
  legacy: { name: 'sw-legacy', ip: '127.0.0.119', driver: 'hpe_arubaos_switch' },
  hang: { name: 'sw-hang', ip: '127.0.0.120', driver: 'hpe_comware' },
  huge: { name: 'rb-huge', ip: '127.0.0.121', driver: 'mikrotik_routeros' },
  legacy1: { name: 'sw-legacy1', ip: '127.0.0.122', driver: 'hpe_arubaos_switch' },
} as const;
export type SshDev = (typeof SSH)[keyof typeof SSH];

export function sshCreds(extra: Record<string, unknown> = {}) {
  return { username: SSH_USER, port: SSH_PORT, password: SSH_PASS, enable_password: SSH_ENABLE, legacy_algorithms: false, ...extra };
}
async function j(url: string, method = 'GET', body?: unknown): Promise<any> {
  const r = await fetch(url, { method, body: body === undefined ? undefined : JSON.stringify(body), headers: body === undefined ? undefined : { 'Content-Type': 'application/json' } });
  const t = await r.text();
  if (!r.ok) throw new Error(`${method} ${url} -> ${r.status} ${t}`);
  return t ? JSON.parse(t) : null;
}
export const sshSim = (path: string, method = 'GET', body?: unknown) => j(SSH_SIM_URL + path, method, body);
export const sshDevice = (name: string) => sshSim(`/devices/${name}`);
export const sshState = (name: string, patch: Record<string, unknown>) => sshSim(`/devices/${name}/state`, 'POST', patch);
export const rotateHostKey = (name: string) => sshSim(`/devices/${name}/rotate-host-key`, 'POST');
export const restoreHostKey = (name: string) => sshSim(`/devices/${name}/restore-host-key`, 'POST');
export const sshLog = (device?: string): Promise<any[]> => sshSim(`/log${device ? `?device=${device}` : ''}`);
export const clearSshLog = () => sshSim('/log', 'DELETE');
export const sshClientKey = (): Promise<{ private_key: string; public_key: string }> => sshSim('/client-key');
export async function sshAllowedCommands(): Promise<string[]> {
  return sshSim('/allowed-commands');
}
/** The read-only guarantee (§13.12): every command/exec the devices received is allow-listed. */
export async function expectOnlyAllowedCommands(): Promise<void> {
  const allowed = new Set(await sshAllowedCommands());
  const bad = (await sshLog()).filter((e) => (e.event === 'command' || e.event === 'exec') && !allowed.has(e.command));
  expect(bad, 'SSH sim command log contains non-allow-listed commands').toEqual([]);
}
/** Normalised expected config (masking on) of a lab device: tests/sim/ssh/expected/<device>/<version>.txt */
export function expectedConfig(device: string, version: 'v1' | 'v2' = 'v1', masked = true): string {
  return readFileSync(join(SIM_DIR, 'ssh', 'expected', device, `${version}${masked ? '' : '.unmasked'}.txt`), 'utf8');
}

export async function backupStatus(api: Api, deviceId: number): Promise<any> {
  return api.get(`/api/devices/${deviceId}/config`);
}
/** Waits until the device's BackupStatus satisfies pred (and no attempt is running). */
export async function waitBackup(api: Api, deviceId: number, pred: (s: any) => boolean, timeout = 90_000, msg = 'backup status'): Promise<any> {
  let last: any = null;
  await expect
    .poll(async () => {
      last = await backupStatus(api, deviceId);
      return !last.running && pred(last);
    }, { timeout, intervals: [500, 1000, 2000], message: `${msg} of device ${deviceId}` })
    .toBe(true);
  return last;
}
export async function versions(api: Api, deviceId: number): Promise<any[]> {
  return (await api.get(`/api/devices/${deviceId}/config/versions?page_size=100`)).items;
}

// ---------------------------------------------------------------- python simulators run from specs
export function py(args: string[], timeoutMs = 60_000): { status: number | null; stdout: string; stderr: string } {
  const r = spawnSync(PY, args, { encoding: 'utf8', timeout: timeoutMs });
  return { status: r.status, stdout: r.stdout ?? '', stderr: r.stderr ?? '' };
}
/** tests/sim/flows/send_flows.py; returns its JSON (expected aggregates + "sent"). */
export function sendFlows(proto: 'v5' | 'v9' | 'ipfix' | 'sflow', scenario: string, from: string, port: number, extra: string[] = []): any {
  const r = py([join(SIM_DIR, 'flows', 'send_flows.py'), '--proto', proto, '--scenario', scenario, '--from', from, '--to', `127.0.0.1:${port}`, ...extra]);
  if (r.status !== 0) throw new Error(`send_flows failed: ${r.stderr}`);
  return JSON.parse(r.stdout.trim().split('\n').pop()!);
}
export function expectedFlows(proto: 'v5' | 'v9' | 'ipfix' | 'sflow', scenario: string, from: string): any {
  const r = py([join(SIM_DIR, 'flows', 'send_flows.py'), '--proto', proto, '--scenario', scenario, '--from', from, '--expect']);
  if (r.status !== 0) throw new Error(`send_flows --expect failed: ${r.stderr}`);
  return JSON.parse(r.stdout);
}
export function sendSyslog(from: string, messages: string[]): void {
  const r = py([join(SIM_DIR, 'ingest', 'send_syslog.py'), '--to', `127.0.0.1:${SYSLOG_PORT}`, '--from', from, '--udp', ...messages]);
  if (r.status !== 0) throw new Error(`send_syslog failed: ${r.stderr}`);
}
export function kioskClient(args: string[], timeoutMs = 60_000): any[] {
  const r = py([join(SIM_DIR, 'kiosk_client.py'), ...args], timeoutMs);
  if (r.status !== 0) throw new Error(`kiosk_client failed: ${r.stderr}`);
  return r.stdout.trim().split('\n').filter(Boolean).map((l) => JSON.parse(l));
}

// ---------------------------------------------------------------- SMTP sink, OIDC mock, Teams, Telegram
export const smtpMessages = (): Promise<any[]> => j(`${SMTP_SINK_URL}/messages`);
export const clearSmtp = () => j(`${SMTP_SINK_URL}/messages`, 'DELETE');
export async function configureSmtp(api: Api): Promise<void> {
  await api.put('/api/settings', { smtp: { host: '127.0.0.1', port: SMTP_PORT, security: 'none', username: '', password: '', from_address: 'netmon@mnidom.test', from_name: 'NetworkMonitor QA' } });
}
export const oidcState = () => j(`${OIDC_MOCK_URL}/_control/state`);
export const oidcMode = (mode: string, once = false) => j(`${OIDC_MOCK_URL}/_control/mode`, 'POST', { mode, once });
export const oidcDefaultUser = (login_hint: string | null) => j(`${OIDC_MOCK_URL}/_control/default-user`, 'POST', { login_hint });
export const oidcRequests = (): Promise<any[]> => j(`${OIDC_MOCK_URL}/_control/requests`);
export const teamsRecords = (): Promise<any[]> => j(`${TEAMS_CONTROL_URL}/records`);
export const clearTeams = () => j(`${TEAMS_CONTROL_URL}/records`, 'DELETE');
export const TEAMS_URL = () => `https://localhost:${TEAMS_MOCK_PORT}/workflows/qa3/triggers/manual/paths/invoke?api-version=2016-06-01&sig=QA3SECRET`;
export const tgInject = (body: Record<string, unknown>) => j(`${TELEGRAM_MOCK_URL}/_inject`, 'POST', body);
export const tgControl = (body: Record<string, unknown>) => j(`${TELEGRAM_MOCK_URL}/_control`, 'POST', body);
export const tgUpdates = (): Promise<any[]> => j(`${TELEGRAM_MOCK_URL}/_updates`);
export const tgPolls = (): Promise<any[]> => j(`${TELEGRAM_MOCK_URL}/_polls`);
export async function tgCalls(method: string): Promise<any[]> {
  return ((await j(`${TELEGRAM_MOCK_URL}/_messages`)) as any[]).filter((m) => m.method_name === method);
}

/** 2b-3 LDAP settings pointing at the fake AD (tests/sim/ldap/fake_ad.py on $LDAPS_PORT, CA trusted by the backend). */
export async function configureFakeAd(api: Api): Promise<void> {
  await api.put('/api/settings', {
    ldap: {
      servers: [`localhost:${LDAPS_PORT}`],
      security: 'ldaps',
      tls_verify: true,
      domain: 'mnidom.net',
      netbios_domain: 'MNIDOM',
      base_dn: 'DC=mnidom,DC=net',
      bind_dn: 'svc-netmon@mnidom.net',
      bind_password: 'Passw0rd!',
      admin_group_dn: 'CN=NetMon-Admins,OU=Groups,DC=mnidom,DC=net',
      viewer_group_dn: 'CN=NetMon-Viewers,OU=Groups,DC=mnidom,DC=net',
      timeout_s: 5,
    },
  });
}

// ---------------------------------------------------------------- misc
export async function shot3(page: Page, name: string): Promise<string> {
  mkdirSync(SHOT_DIR_3, { recursive: true });
  const p = join(SHOT_DIR_3, `${name}.png`);
  await page.screenshot({ path: p, fullPage: false });
  return p;
}
export async function ensureGroupNamed(api: Api, name: string, extra: Record<string, unknown> = {}): Promise<any> {
  const page = await api.get('/api/groups?page_size=1000');
  const g = (page.items ?? page).find((x: any) => x.name === name);
  if (g) return Object.keys(extra).length ? api.patch(`/api/groups/${g.id}`, extra) : g;
  return api.post('/api/groups', { name, ...extra });
}
export async function ensureDeviceAt(api: Api, groupId: number, host: string, name: string, extra: Record<string, unknown> = {}): Promise<any> {
  const list = await api.get(`/api/devices?q=${encodeURIComponent(host)}&page_size=200`).catch(() => null);
  const items: any[] = list?.items ?? (Array.isArray(list) ? list : []);
  const d = items.find((x) => x.host === host);
  if (d) return Object.keys(extra).length ? api.patch(`/api/devices/${d.id}`, extra) : d;
  return api.post('/api/devices', { group_id: groupId, name, host, auto_sensors: [], ...extra });
}
export function fileExists(p: string): boolean {
  return existsSync(p);
}
export { raw, uid };

/** The translated text of an i18n key when the locale has it, else the fallback regex (keys land with the UI). */
import { hasKey, T } from './lab2b2';
export function tx(key: string, fallback: RegExp, vars: Record<string, unknown> = {}, lang: 'en' | 'he' = 'en'): string | RegExp {
  if (hasKey(key, lang)) return T(key, vars, lang);
  // the locale does not have the key yet: accept the English fallback OR the raw key that i18next renders
  const raw = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`${fallback.source}|^${raw}$`, fallback.flags.includes('i') ? 'i' : '');
}

// ---------------------------------------------------------------- toggleable HTTP devices (problems on demand)
// Own http_target.py per spec on 127.0.0.131-.139:8080 (POST /fail, /ok), so phase-3 specs never disturb the
// shared targets of earlier specs.
import { startHttpHost, stopHost } from './lab';
import { httpDevice, waitStatus } from './lab2b';
export async function problemDevice(api: Api, ip: string, name: string, groupName = 'QA Problems 3') {
  const child = await startHttpHost(ip, 8080);
  const g = await ensureGroupNamed(api, groupName);
  const existing = (await api.get(`/api/devices?q=${encodeURIComponent(ip)}&page_size=50`)).items.find((d: any) => d.host === ip);
  let device: any;
  let sensor: any;
  if (existing) {
    device = existing;
    sensor = (await api.get(`/api/sensors?device_id=${existing.id}&page_size=50`)).items.find((s: any) => s.type === 'http');
  } else {
    ({ device, sensor } = await httpDevice(api, { name, ip, group_id: g.id }));
  }
  const ctl = async (a: 'fail' | 'ok') => { await fetch(`http://${ip}:8080/${a}`, { method: 'POST' }); };
  return {
    device, sensor, child, ctl,
    down: async () => { await ctl('fail'); return waitStatus(api, sensor.id, 'down', 90_000); },
    up: async () => { await ctl('ok'); return waitStatus(api, sensor.id, 'up', 90_000); },
    // pause the device first so a stopped target does not leave a "down" device behind for later specs
    stop: async () => { await api.patch(`/api/devices/${device.id}`, { enabled: false }).catch(() => undefined); stopHost(child); },
  };
}

/** A device measured by an snmp_system sensor against its own snmpsim agent (ip:1161): kill() makes it down. */
import { spawn as spawnChild, type ChildProcess } from 'node:child_process';
function killAgent(ip: string): void {
  // snmp_agent.py runs snmpsim as a child that drops to nobody: kill both by their command lines
  spawnSync('pkill', ['-f', `${ip}:1161`]);
  spawnSync('pkill', ['-f', `snmp_agent.py --host ${ip} `]);
}
export async function snmpMeasuredDevice(api: Api, ip: string, name: string, groupName = 'QA Problems 3') {
  let child: ChildProcess | null = null;
  killAgent(ip); // leftovers of an aborted run
  const start = async () => {
    child = spawnChild(PY, [join(SIM_DIR, 'snmp_agent.py'), '--host', ip, '--port', '1161'], { stdio: ['ignore', 'pipe', 'ignore'], detached: true });
    let out = '';
    child.stdout!.on('data', (d) => (out += d));
    await expect.poll(() => out.includes('READY'), { timeout: 30_000, message: `snmp agent ${ip}` }).toBe(true);
  };
  await start();
  const g = await ensureGroupNamed(api, groupName);
  const device = await api.post('/api/devices', {
    group_id: g.id, name, host: ip, auto_sensors: ['snmp_system'],
    snmp_credentials: { version: 'v2c', port: 1161, timeout_s: 1, retries: 0, community: 'public' },
  });
  const sensor = (await api.get(`/api/sensors?device_id=${device.id}&page_size=50`)).items.find((s: any) => s.type === 'snmp_system');
  await api.patch(`/api/sensors/${sensor.id}`, { interval_s: 10 });
  return {
    device, sensor,
    down: async () => { killAgent(ip); child = null; return waitStatus(api, sensor.id, 'down', 90_000); },
    up: async () => { await start(); return waitStatus(api, sensor.id, 'up', 90_000); },
    stop: async () => { await api.patch(`/api/devices/${device.id}`, { enabled: false }).catch(() => undefined); killAgent(ip); },
  };
}
