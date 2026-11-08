// Phase 2b-1 helpers: test clock, backend restart, single lab components, toggle targets, webhook
// filtering and fixture builders (docs/architecture.md §10.4, docs/api.md [2b-1]).
import { expect, type Page } from '@playwright/test';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Api, MIN_INTERVAL, WEBHOOK_URL, uid, waitForSensor, webhookRecords } from './helpers';
import { PY, SIM_DIR, raw } from './lab';

export const LAB_SH = process.env.LAB_SH ?? join(SIM_DIR, 'lab.sh');
export const LAB_DIR = process.env.LAB_DIR ?? '/tmp/netmon-lab';
export const BACKEND_RESTART = process.env.BACKEND_RESTART ?? '';
export const SHOT_DIR = process.env.QA2B1_SHOT_DIR ?? join(__dirname, '..', 'test-results', 'screenshots', 'qa-2b1');

// ---------------------------------------------------------------- test clock (NETMON_TEST_MODE=1)
/** PUT /api/test/clock: absolute offset in seconds (0 resets). Returns the backend's shifted "now". */
export async function setClock(api: Api, offset_s: number): Promise<{ now: string; offset_s: number }> {
  return api.put('/api/test/clock', { offset_s });
}
export async function resetClock(api: Api): Promise<void> {
  await raw(api, 'PUT', '/api/test/clock', { offset_s: 0 });
}

// ---------------------------------------------------------------- backend restart (run.sh)
export function canRestartBackend(): boolean {
  return !!BACKEND_RESTART && existsSync(BACKEND_RESTART);
}
/** Graceful stop + start of the e2e backend; returns once /api/health answers. */
export function restartBackend(): void {
  const r = spawnSync('bash', [BACKEND_RESTART], { encoding: 'utf8', timeout: 90_000 });
  if (r.status !== 0) throw new Error(`backend restart failed: ${r.status} ${r.stdout} ${r.stderr}`);
}

// ---------------------------------------------------------------- lab components (tests/sim/lab.sh)
function labCmd(args: string[]): string {
  const env = { ...process.env, LAB_DIR, LAB_GENERIC: '0', PY: existsSync(PY) ? PY : 'python3' };
  const r = spawnSync('bash', [LAB_SH, ...args], { encoding: 'utf8', env, timeout: 60_000 });
  if (r.status !== 0) throw new Error(`lab.sh ${args.join(' ')} -> ${r.status}: ${r.stdout} ${r.stderr}`);
  return r.stdout;
}
/** Stop one lab component (simulated outage: connection refused / SNMP timeout). */
export const labKill = (name: string) => labCmd(['kill', name]);
export const labRevive = (name: string) => labCmd(['revive', name]);
/** Restart one SNMP component with another data dir (relative to tests/sim/hpe or absolute). */
export const labSwap = (name: string, dataDir?: string) =>
  labCmd(['swap', name, ...(dataDir ? [dataDir.startsWith('/') ? dataDir : join(SIM_DIR, 'hpe', dataDir)] : [])]);
export function labLog(name: string): string {
  const p = join(LAB_DIR, 'logs', `${name}.log`);
  return existsSync(p) ? readFileSync(p, 'utf8') : '';
}

/** Toggle HTTP targets 127.0.0.31-36:8080 (tests/sim/http_target.py). */
export async function target(ip: string, action: 'ok' | 'fail'): Promise<void> {
  const r = await fetch(`http://${ip}:8080/${action}`, { method: 'POST' });
  expect(r.status).toBe(200);
}

// ---------------------------------------------------------------- webhooks
/** Records received on WEBHOOK_URL + path (each spec uses its own path). */
export async function hooks(path: string): Promise<any[]> {
  return (await webhookRecords()).filter((r) => r.path === path);
}
/** Every sensor notification (any channel/path) about the given sensor ids. */
export async function hooksForSensors(ids: number[]): Promise<any[]> {
  return (await webhookRecords()).filter((r) => r.body?.sensor && ids.includes(r.body.sensor.id));
}
export async function createWebhook(api: Api, path: string, name = uid('qa2b-hook')): Promise<number> {
  return (await api.post('/api/notification-channels', { name, type: 'webhook', enabled: true, config: { url: `${WEBHOOK_URL}${path}` } })).id;
}

// ---------------------------------------------------------------- fixtures
/** Flattens GET /api/tree (groups are nested: children[] / devices[] / sensors[]). */
export function flattenTree(tree: any): { groups: any[]; devices: any[] } {
  const groups: any[] = [];
  const devices: any[] = [];
  const walk = (g: any) => {
    groups.push(g);
    for (const d of g.devices ?? []) devices.push(d);
    for (const c of g.children ?? []) walk(c);
  };
  for (const g of tree.groups ?? []) walk(g);
  return { groups, devices };
}
export async function ensureChildGroup(api: Api, name: string, parentId: number | null, extra: Record<string, unknown> = {}): Promise<number> {
  const g = flattenTree(await api.get('/api/tree')).groups.find((x) => x.name === name && x.parent_id === parentId);
  if (g) {
    if (Object.keys(extra).length) await api.patch(`/api/groups/${g.id}`, extra);
    return g.id;
  }
  return (await api.post('/api/groups', { name, parent_id: parentId, ...extra })).id;
}

/** A device on a toggle target with one HTTP sensor (interval 10 s) and no ping sensor. */
export async function httpDevice(
  api: Api,
  opts: { name: string; ip: string; group_id: number; dependency?: Record<string, unknown> },
): Promise<{ device: any; sensor: any }> {
  const device = await api.post('/api/devices', {
    group_id: opts.group_id,
    name: opts.name,
    host: opts.ip,
    auto_sensors: [],
    ...(opts.dependency ? { dependency: opts.dependency } : {}),
  });
  const sensor = await api.post('/api/sensors', {
    device_id: device.id,
    name: 'Web',
    type: 'http',
    interval_s: MIN_INTERVAL,
    config: { url: 'http://{host}:8080/', keyword: 'sim OK', timeout_s: 3 },
  });
  return { device, sensor };
}

export async function rule(api: Api, body: Record<string, unknown>): Promise<any> {
  return api.post('/api/alert-rules', { name: uid('qa2b-rule'), on_statuses: ['down', 'up'], delay_s: 0, enabled: true, ...body });
}

export async function waitStatus(api: Api, sensorId: number, status: string, timeout = 60_000): Promise<any> {
  return waitForSensor(api, sensorId, (s) => s.status === status, timeout, `sensor ${sensorId} -> ${status}`);
}

/** Polls until fn() is stable-true; for "nothing arrives" checks use quiet(). */
export async function quiet(ms: number): Promise<void> {
  await new Promise((r) => setTimeout(r, ms));
}

export async function shot2b(page: Page, name: string): Promise<string> {
  mkdirSync(SHOT_DIR, { recursive: true });
  const p = join(SHOT_DIR, `${name}.png`);
  await page.screenshot({ path: p, fullPage: false });
  return p;
}

export async function findDevice(api: Api, host: string): Promise<any | null> {
  const p = await api.get(`/api/devices?q=${encodeURIComponent(host)}&page_size=1000`);
  return p.items.find((d: any) => d.host === host) ?? null;
}

export async function waitTopologyRefresh(api: Api, timeout = 90_000): Promise<any> {
  let last: any = null;
  await expect
    .poll(
      async () => {
        last = await api.get('/api/topology');
        return last.refresh_running === false && !!last.last_refresh && last.last_refresh.status !== 'running';
      },
      { timeout, intervals: [500, 1000, 2000], message: 'topology refresh finished' },
    )
    .toBe(true);
  return last;
}

/** Runs one manual topology refresh to completion (waits for a running one first). */
export async function refreshTopology(api: Api): Promise<any> {
  await waitTopologyRefresh(api).catch(() => undefined);
  const r = await raw(api, 'POST', '/api/topology/refresh');
  expect(r.status, r.text).toBe(202);
  expect(r.body.status).toBe('running');
  let last: any = null;
  await expect
    .poll(
      async () => {
        last = await api.get('/api/topology');
        return last.last_refresh?.id === r.body.id && last.last_refresh.status !== 'running';
      },
      { timeout: 90_000, intervals: [500, 1000, 2000], message: `topology refresh ${r.body.id}` },
    )
    .toBe(true);
  expect(last.last_refresh.status, JSON.stringify(last.last_refresh)).toBe('completed');
  return last;
}
