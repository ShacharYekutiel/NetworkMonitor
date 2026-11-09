// Phase 2b-3 helpers (docs/architecture.md §12, docs/api.md [2b-3], docs/data-model.md [2b-3]):
// feature toggles, the ingest senders (tests/sim/ingest), SIEM sink, Teams mock and fake AD controls, LDAP
// config for the fake AD, polling helpers for traps/syslog/conditions.
//
// Ports and URLs come from tests/e2e/run.sh. Senders bind 127.0.0.x source addresses (Linux routes 127/8 to
// lo), so 127.0.0.81 is "port-aos" of the 2b-2 ports lab.
import { expect, request as pwRequest, type Page } from '@playwright/test';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Api, BASE_URL, uid } from './helpers';
import { PY, SIM_DIR, raw } from './lab';
import { LAB_DIR } from './lab2b';

export const TRAP_PORT = Number(process.env.TRAP_PORT ?? 1162);
export const SYSLOG_PORT = Number(process.env.SYSLOG_PORT ?? 1514);
export const CERTS_DIR = process.env.CERTS_DIR ?? '';
export const LDAPS_PORT = Number(process.env.LDAP_LDAPS_PORT ?? 16360);
export const LDAP_PLAIN_PORT = Number(process.env.LDAP_PLAIN_PORT ?? 13890);
export const LDAP_WRONG_PORT = Number(process.env.LDAP_WRONG_PORT ?? 16361);
export const LDAP_DEAD_PORT = Number(process.env.LDAP_DEAD_PORT ?? 16362);
export const LDAP_CONTROL_URL = process.env.LDAP_CONTROL_URL ?? 'http://127.0.0.1:18389';
export const SIEM_UDP_PORT = Number(process.env.SIEM_UDP_PORT ?? 15514);
export const SIEM_TCP_PORT = Number(process.env.SIEM_TCP_PORT ?? 15515);
export const SIEM_TLS_PORT = Number(process.env.SIEM_TLS_PORT ?? 16514);
export const SIEM_CONTROL_URL = process.env.SIEM_CONTROL_URL ?? 'http://127.0.0.1:18514';
export const TEAMS_PORT = Number(process.env.TEAMS_PORT ?? 18443);
export const TEAMS_CONTROL_URL = process.env.TEAMS_CONTROL_URL ?? 'http://127.0.0.1:18444';
export const SHOT_DIR_2B3 = process.env.QA2B3_SHOT_DIR ?? join(__dirname, '..', 'test-results', 'screenshots', 'qa-2b3');
export const VECTORS = join(SIM_DIR, 'ingest', 'vectors.txt');

export const FEATURES = [
  // owner decision: Teams channels and API tokens are always available (not toggles)
  'traps', 'syslog', 'event_alerts', 'lag', 'ldap', 'audit', 'timeline', 'flapping', 'siem_forwarding',
] as const;
export type Feature = (typeof FEATURES)[number];

/** Sender addresses (127.0.0.81 = port-aos of the 2b-2 lab; .87/.88 = the LAG sims; .99 = unknown). */
export const SRC = { aos: '127.0.0.81', cw: '127.0.0.82', lagCw: '127.0.0.87', lagAos: '127.0.0.88', v3dev: '127.0.0.89', unknown: '127.0.0.99' } as const;

export const OID = {
  coldStart: '1.3.6.1.6.3.1.1.5.1',
  warmStart: '1.3.6.1.6.3.1.1.5.2',
  linkDown: '1.3.6.1.6.3.1.1.5.3',
  linkUp: '1.3.6.1.6.3.1.1.5.4',
  authFailure: '1.3.6.1.6.3.1.1.5.5',
  ifIndex: '1.3.6.1.2.1.2.2.1.1',
  ifAdminStatus: '1.3.6.1.2.1.2.2.1.7',
  ifOperStatus: '1.3.6.1.2.1.2.2.1.8',
  snmpTrapAddress: '1.3.6.1.6.3.18.1.3.0',
} as const;

/** linkDown/linkUp varbinds for an ifIndex (ifIndex, ifAdminStatus, ifOperStatus). */
export function linkVarbinds(ifIndex: number, up: boolean, adminUp = true): string[] {
  return [
    `${OID.ifIndex}.${ifIndex}=i:${ifIndex}`,
    `${OID.ifAdminStatus}.${ifIndex}=i:${adminUp ? 1 : 2}`,
    `${OID.ifOperStatus}.${ifIndex}=i:${up ? 1 : 2}`,
  ];
}

// ---------------------------------------------------------------- feature toggles
export async function publicFeatures(api: Api): Promise<Record<Feature, boolean>> {
  return (await api.get('/api/settings/public')).features;
}
/** PUT /api/settings with feature_<name>_enabled for each entry. */
export async function setFeatures(api: Api, f: Partial<Record<Feature, boolean>>): Promise<any> {
  const body: Record<string, boolean> = {};
  for (const [k, v] of Object.entries(f)) body[`feature_${k}_enabled`] = !!v;
  const r = await raw(api, 'PUT', '/api/settings', body);
  expect(r.status, `PUT features ${JSON.stringify(body)}: ${r.text}`).toBe(200);
  return r.body;
}
export async function featureOn(api: Api, ...names: Feature[]): Promise<void> {
  await setFeatures(api, Object.fromEntries(names.map((n) => [n, true])));
}
export async function featureOff(api: Api, ...names: Feature[]): Promise<void> {
  await setFeatures(api, Object.fromEntries(names.map((n) => [n, false])));
}
/** 409 feature_disabled with details [{feature}] (api.md [2b-3] Errors). */
export async function expectFeatureDisabled(api: Api, method: string, path: string, feature: Feature, body?: unknown): Promise<void> {
  const r = await raw(api, method, path, body);
  expect.soft(r.status, `${method} ${path} while ${feature} OFF: ${r.text.slice(0, 300)}`).toBe(409);
  if (r.status === 409) {
    expect.soft(r.body?.error?.code, `${method} ${path}`).toBe('feature_disabled');
    expect.soft(r.body?.error?.details, `${method} ${path} details`).toEqual(expect.arrayContaining([expect.objectContaining({ feature })]));
  }
}

/** Waits until the given ingest receivers report `state` (GET /api/ingest/status). */
export async function waitReceivers(api: Api, kinds: string[], state: 'listening' | 'disabled' | 'error', timeout = 15_000): Promise<any> {
  let last: any = null;
  await expect
    .poll(
      async () => {
        const r = await raw(api, 'GET', '/api/ingest/status');
        last = r.body;
        if (r.status !== 200) return `HTTP ${r.status}`;
        return kinds.every((k) => r.body.receivers?.find((x: any) => x.kind === k)?.state === state);
      },
      { timeout, intervals: [250, 500, 1000], message: `receivers ${kinds} -> ${state}` },
    )
    .toBe(true);
  return last;
}

// ---------------------------------------------------------------- senders (python, tests/sim/ingest)
function py(args: string[], timeout = 60_000): { status: number | null; stdout: string; stderr: string } {
  const r = spawnSync(existsSync(PY) ? PY : 'python3', args, { encoding: 'utf8', timeout });
  return { status: r.status, stdout: r.stdout ?? '', stderr: r.stderr ?? '' };
}

export type TrapOpts = {
  from?: string; version?: 'v1' | 'v2c' | 'v3'; oid: string; varbinds?: string[]; inform?: boolean; community?: string;
  agentAddr?: string; enterprise?: string; user?: string; auth?: string; authKey?: string; priv?: string; privKey?: string;
  engineId?: string; repeat?: number; interval?: number; port?: number; uptime?: number;
};
/** tests/sim/ingest/send_trap.py; returns the JSON lines it printed (one per PDU) and the exit code. */
export function sendTrap(o: TrapOpts): { rc: number | null; out: any[]; stderr: string } {
  const a = [join(SIM_DIR, 'ingest', 'send_trap.py'), '--to', `127.0.0.1:${o.port ?? TRAP_PORT}`, '--from', o.from ?? SRC.aos,
    '--version', o.version ?? 'v2c', '--oid', o.oid];
  for (const vb of o.varbinds ?? []) a.push('--varbind', vb);
  if (o.inform) a.push('--inform');
  if (o.community) a.push('--community', o.community);
  if (o.agentAddr) a.push('--agent-addr', o.agentAddr);
  if (o.enterprise) a.push('--enterprise', o.enterprise);
  if (o.user) a.push('--user', o.user);
  if (o.auth) a.push('--auth', o.auth);
  if (o.authKey) a.push('--auth-key', o.authKey);
  if (o.priv) a.push('--priv', o.priv);
  if (o.privKey) a.push('--priv-key', o.privKey);
  if (o.engineId) a.push('--engine-id', o.engineId);
  if (o.repeat) a.push('--repeat', String(o.repeat));
  if (o.interval) a.push('--interval', String(o.interval));
  if (o.uptime !== undefined) a.push('--uptime', String(o.uptime));
  const r = py(a);
  if (r.status !== 0 && r.status !== 3) throw new Error(`send_trap ${a.join(' ')} -> ${r.status}: ${r.stdout} ${r.stderr}`);
  return { rc: r.status, out: r.stdout.trim().split('\n').filter(Boolean).map((l) => JSON.parse(l)), stderr: r.stderr };
}

export type SyslogOpts = {
  from?: string; tcp?: boolean; framing?: 'octet' | 'lf'; ids?: string[]; messages?: string[]; count?: number; rate?: number;
  rawHex?: string; port?: number; file?: string;
};
export function sendSyslog(o: SyslogOpts): any {
  const a = [join(SIM_DIR, 'ingest', 'send_syslog.py'), '--to', `127.0.0.1:${o.port ?? SYSLOG_PORT}`, '--from', o.from ?? SRC.aos];
  if (o.tcp) a.push('--tcp', '--framing', o.framing ?? 'octet');
  if (o.ids) a.push('--file', o.file ?? VECTORS, '--ids', o.ids.join(','));
  if (o.count) a.push('--count', String(o.count));
  if (o.rate !== undefined) a.push('--rate', String(o.rate));
  if (o.rawHex) a.push('--raw-hex', o.rawHex);
  a.push(...(o.messages ?? []));
  const r = py(a);
  if (r.status !== 0) throw new Error(`send_syslog ${a.join(' ')} -> ${r.status}: ${r.stdout} ${r.stderr}`);
  return JSON.parse(r.stdout.trim().split('\n').pop() as string);
}

// ---------------------------------------------------------------- stored records
function qs(q: Record<string, string | number | boolean | (string | number)[] | undefined>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(q)) {
    if (v === undefined) continue;
    for (const x of Array.isArray(v) ? v : [v]) parts.push(`${k}=${encodeURIComponent(String(x))}`);
  }
  return parts.length ? `?${parts.join('&')}` : '';
}
export async function syslogPage(api: Api, q: Record<string, any> = {}): Promise<any> {
  return api.get(`/api/syslog${qs(q)}`);
}
export async function trapPage(api: Api, q: Record<string, any> = {}): Promise<any> {
  return api.get(`/api/traps${qs(q)}`);
}
/** Polls GET /api/syslog (or traps) with `q` until `pred(items)`; returns the items. */
export async function waitItems(
  api: Api,
  kind: 'syslog' | 'traps',
  q: Record<string, any>,
  pred: (items: any[]) => boolean,
  timeout = 20_000,
  message = `${kind} records`,
): Promise<any[]> {
  let items: any[] = [];
  await expect
    .poll(
      async () => {
        const r = await raw(api, 'GET', `/api/${kind}${qs({ limit: 500, ...q })}`);
        if (r.status !== 200) return `HTTP ${r.status} ${r.text.slice(0, 200)}`;
        items = r.body.items;
        return pred(items);
      },
      { timeout, intervals: [300, 500, 1000], message },
    )
    .toBe(true);
  return items;
}
/** A unique marker to find this test's own messages with `q`. */
export function marker(prefix = 'qa'): string {
  return uid(prefix).replace(/[^A-Za-z0-9-]/g, '');
}

// ---------------------------------------------------------------- devices / sensors
export async function deviceByHost(api: Api, host: string): Promise<any | null> {
  const p = await api.get(`/api/devices?q=${encodeURIComponent(host)}&page_size=1000`);
  return p.items.find((d: any) => d.host === host) ?? null;
}
export async function ensureDevice(api: Api, groupId: number, host: string, name: string, extra: Record<string, unknown> = {}): Promise<any> {
  let d = await deviceByHost(api, host);
  if (!d) d = await api.post('/api/devices', { group_id: groupId, name, host, auto_sensors: [], ...extra });
  return api.get(`/api/devices/${d.id}`);
}
export async function sensorsOf(api: Api, deviceId: number): Promise<any[]> {
  return (await api.get(`/api/sensors?device_id=${deviceId}&page_size=1000`)).items;
}
export async function eventSensor(api: Api, deviceId: number, type: 'trap' | 'syslog' | 'port_flap'): Promise<any | null> {
  return (await sensorsOf(api, deviceId)).find((s) => s.type === type) ?? null;
}
export async function waitEventSensor(api: Api, deviceId: number, type: 'trap' | 'syslog' | 'port_flap', pred: (s: any) => boolean = () => true, timeout = 20_000): Promise<any> {
  let s: any = null;
  await expect
    .poll(async () => {
      s = await eventSensor(api, deviceId, type);
      return !!s && pred(s);
    }, { timeout, intervals: [300, 500, 1000], message: `device ${deviceId}: ${type} event sensor` })
    .toBe(true);
  return s;
}
export async function conditions(api: Api, sensorId: number): Promise<any[]> {
  return (await api.get(`/api/sensors/${sensorId}/conditions`)).items;
}
export async function builtinRule(api: Api, key: string): Promise<any> {
  const items = (await api.get('/api/event-rules')).items;
  const r = items.find((x: any) => x.builtin === key);
  expect(r, `built-in rule ${key} in ${JSON.stringify(items.map((x: any) => x.builtin))}`).toBeTruthy();
  return r;
}

// ---------------------------------------------------------------- SIEM sink / Teams mock / fake AD
async function ctl(base: string, method: string, path: string): Promise<any> {
  const r = await fetch(`${base}${path}`, { method });
  return r.json();
}
export const siemRecords = (transport?: 'udp' | 'tcp' | 'tls'): Promise<any[]> => ctl(SIEM_CONTROL_URL, 'GET', `/records${transport ? `?transport=${transport}` : ''}`);
export const clearSiem = () => ctl(SIEM_CONTROL_URL, 'DELETE', '/records');
export const siemDown = () => ctl(SIEM_CONTROL_URL, 'POST', '/down');
export const siemUp = () => ctl(SIEM_CONTROL_URL, 'POST', '/up');
export const teamsRecords = (): Promise<any[]> => ctl(TEAMS_CONTROL_URL, 'GET', '/records');
export const clearTeams = () => ctl(TEAMS_CONTROL_URL, 'DELETE', '/records');
export const teamsStatus = (code: number, body = '') => ctl(TEAMS_CONTROL_URL, 'POST', `/status?code=${code}&body=${encodeURIComponent(body)}`);
export const TEAMS_URL = `https://localhost:${TEAMS_PORT}/workflows/qa2b3/triggers/manual/paths/invoke?api-version=2016-06-01&sig=QA-SECRET-SIG`;
export const adBinds = (): Promise<any[]> => ctl(LDAP_CONTROL_URL, 'GET', '/binds');
export const clearAdBinds = () => ctl(LDAP_CONTROL_URL, 'DELETE', '/binds');
export const adHang = (on: boolean) => ctl(LDAP_CONTROL_URL, 'POST', on ? '/hang/on' : '/hang/off');
export const adStop = () => ctl(LDAP_CONTROL_URL, 'POST', '/stop');
export const adStart = () => ctl(LDAP_CONTROL_URL, 'POST', '/start');

export const AD = {
  password: 'Passw0rd!',
  adminGroup: 'CN=NetMon-Admins,OU=Groups,DC=mnidom,DC=net',
  viewerGroup: 'CN=NetMon-Viewers,OU=Groups,DC=mnidom,DC=net',
};
/** LDAP settings for the fake AD (data-model § LDAP settings). Server names must match the cert: localhost. */
export function ldapConfig(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    servers: [`localhost:${LDAPS_PORT}`],
    security: 'ldaps',
    tls_verify: true,
    domain: 'mnidom.net',
    netbios_domain: 'MNIDOM',
    base_dn: 'DC=mnidom,DC=net',
    bind_dn: 'svc-netmon@mnidom.net',
    bind_password: AD.password,
    admin_group_dn: AD.adminGroup,
    viewer_group_dn: AD.viewerGroup,
    timeout_s: 3,
    ...over,
  };
}

/** Login with a fresh cookie jar; returns status + body (never throws). */
export async function tryLogin(username: string, password: string): Promise<{ status: number; body: any; ms: number; api: Api }> {
  const ctx = await pwRequest.newContext({ baseURL: BASE_URL });
  const a = new Api(ctx);
  const t0 = Date.now();
  const r = await raw(a, 'POST', '/api/auth/login', { username, password });
  return { status: r.status, body: r.body, ms: Date.now() - t0, api: a };
}

// ---------------------------------------------------------------- misc
export async function shot2b3(page: Page, name: string): Promise<string> {
  mkdirSync(SHOT_DIR_2B3, { recursive: true });
  const p = join(SHOT_DIR_2B3, `${name}.png`);
  await page.screenshot({ path: p, fullPage: false });
  return p;
}
export function labLogText(name: string): string {
  const p = join(LAB_DIR, 'logs', `${name}.log`);
  return existsSync(p) ? readFileSync(p, 'utf8') : '';
}
/** Request lines snmpsim logged after a given offset (to assert which OIDs were / were not walked). */
export function snmpsimRequestsSince(name: string, offset: number): string {
  return labLogText(name).slice(offset);
}
export async function sleep(ms: number): Promise<void> {
  await new Promise((r) => setTimeout(r, ms));
}
/** Optional i18n lookup: the en text for a key, or the given fallback when the key does not exist yet. */
export function tOr(key: string, fallback: string, lang: 'en' | 'he' = 'en'): string {
  try {
    const root = JSON.parse(readFileSync(join(__dirname, '..', '..', '..', 'frontend', 'src', 'i18n', 'locales', `${lang}.json`), 'utf8'));
    const v = key.split('.').reduce((o: any, p) => (o == null ? undefined : o[p]), root);
    return typeof v === 'string' ? v : fallback;
  } catch {
    return fallback;
  }
}
export { raw, uid };
