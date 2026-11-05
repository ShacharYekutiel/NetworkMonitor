// Phase 2a helpers: the discovery lab (tests/sim/lab.sh), the Telegram mock and discovery API waits.
import { expect, type Page } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Api, apiAs, uid, VIEWER } from './helpers';

export const TELEGRAM_MOCK_URL = process.env.TELEGRAM_MOCK_URL ?? 'http://127.0.0.1:18081';
export const DISCOVERY_SNMP_PORT = Number(process.env.DISCOVERY_SNMP_PORT ?? 1161);
export const SIM_DIR = process.env.SIM_DIR ?? join(__dirname, '..', '..', 'sim');
export const PY = process.env.PY ?? join(__dirname, '..', '..', '..', 'backend', '.venv', 'bin', 'python');
export const BACKEND_LOG = process.env.BACKEND_LOG ?? '';

export const TG_VALID_TOKEN = '123456:TEST_TOKEN_valid_0123456789abcdef';
/** Syntactically valid (^\d{3,20}:[A-Za-z0-9_-]{20,100}$) but unknown to the mock -> 401 Unauthorized. */
export const TG_BAD_TOKEN = '654321:TEST_TOKEN_wrong_0123456789abcdef';
export const TG_CHAT_OK = '-1001';

/** The lab range and what docs/architecture.md §9.6 expects from a scan of it. */
export const LAB_RANGE = '127.0.0.20-30';
export const LAB_SCAN_OPTIONS = {
  ping: false,
  snmp: true,
  snmp_port: DISCOVERY_SNMP_PORT,
  snmp_timeout_s: 1,
  extra_communities: ['printers'],
  tcp: true,
  tcp_ports: [8080, 9100],
  dns: false,
};
export const LAB_EXPECTED: Record<
  string,
  { device_class: string; vendor: string | null; suggested_template: string; snmp_credential: string | null; sys_name?: string; open_ports: number[] }
> = {
  '127.0.0.21': { device_class: 'router', vendor: 'opnsense', suggested_template: 'opnsense', snmp_credential: 'group', sys_name: 'OPNsense.localdomain', open_ports: [] },
  '127.0.0.22': { device_class: 'access_point', vendor: 'ubiquiti', suggested_template: 'unifi_ap', snmp_credential: 'group', sys_name: 'U6-Lite-Office', open_ports: [] },
  '127.0.0.23': { device_class: 'printer', vendor: 'brother', suggested_template: 'printer', snmp_credential: 'extra', sys_name: 'BRN3C2AF4', open_ports: [9100] },
  '127.0.0.24': { device_class: 'switch', vendor: 'cisco', suggested_template: 'generic_snmp', snmp_credential: 'group', sys_name: 'sw-core-2960x', open_ports: [] },
  '127.0.0.25': { device_class: 'unknown', vendor: null, suggested_template: 'http_server', snmp_credential: null, open_ports: [8080] },
};

export function snmpCreds(community = 'public', port = DISCOVERY_SNMP_PORT) {
  return { version: 'v2c', port, timeout_s: 1, retries: 1, community };
}

// ---------------------------------------------------------------- Telegram mock
export type TgRecord = { time: number; token: string; method_name: string; body: any; status: number; response: any };
export async function telegramMessages(): Promise<TgRecord[]> {
  const r = await fetch(`${TELEGRAM_MOCK_URL}/_messages`);
  return ((await r.json()) as TgRecord[]).filter((m) => !['-1009', 'slow'].includes(String(m.body?.chat_id)));
}
export async function clearTelegram(): Promise<void> {
  await fetch(`${TELEGRAM_MOCK_URL}/_messages`, { method: 'DELETE' });
}
/** Successfully delivered sendMessage calls. */
export async function telegramDelivered(): Promise<TgRecord[]> {
  // Slow-chat requests ("-1009"/"slow") land late (after the client timed out); they are never deliveries.
  return (await telegramMessages()).filter(
    (m) => m.method_name === 'sendMessage' && m.status === 200 && !['-1009', 'slow'].includes(String(m.body?.chat_id)),
  );
}

// ---------------------------------------------------------------- discovery API
export async function waitForScan(api: Api, id: number, timeout = 90_000, statuses = ['completed', 'cancelled', 'failed']) {
  let last: any = null;
  await expect
    .poll(
      async () => {
        last = await api.get(`/api/discovery/scans/${id}`);
        return statuses.includes(last.status);
      },
      { timeout, intervals: [500, 1000, 2000], message: `scan ${id} to finish` },
    )
    .toBe(true);
  return last;
}

/** Waits for any running scan to end (the system allows only one), so a spec can start its own. */
export async function waitNoRunningScan(api: Api, timeout = 120_000): Promise<void> {
  await expect
    .poll(
      async () => {
        const p = await api.get('/api/discovery/scans');
        return p.items.filter((s: any) => s.status === 'running').length;
      },
      { timeout, intervals: [1000, 2000], message: 'no running discovery scan' },
    )
    .toBe(0);
}

export async function startScan(api: Api, body: Record<string, unknown>): Promise<any> {
  await waitNoRunningScan(api);
  return api.post('/api/discovery/scans', body);
}

export async function scanResults(api: Api, scanId: number, query = ''): Promise<any[]> {
  const p = await api.get(`/api/discovery/scans/${scanId}/results?page_size=1000${query ? `&${query}` : ''}`);
  return p.items;
}

/** Raw request returning status + parsed body (for asserting error envelopes). */
export async function raw(api: Api, method: string, path: string, body?: unknown): Promise<{ status: number; body: any; text: string }> {
  const r = await api.ctx.fetch(path, {
    method,
    data: body === undefined ? undefined : body,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
  });
  const text = await r.text();
  let parsed: any = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = null;
  }
  return { status: r.status(), body: parsed, text };
}

export async function findDeviceByHost(api: Api, host: string): Promise<any | null> {
  const p = await api.get(`/api/devices?q=${encodeURIComponent(host)}&page_size=1000`);
  return p.items.find((d: any) => d.host === host) ?? null;
}

export async function ensureGroup(api: Api, name: string): Promise<number> {
  const tree = await api.get<{ groups: { id: number; name: string }[] }>('/api/tree');
  const g = tree.groups.find((x) => x.name === name);
  if (g) return g.id;
  return (await api.post('/api/groups', { name, parent_id: null })).id;
}

export async function ensureViewer(admin: Api): Promise<void> {
  const users = await admin.get('/api/users?page_size=1000');
  if (!users.items.some((u: any) => u.username === VIEWER.username)) {
    await admin.post('/api/users', { ...VIEWER, email: null, role: 'viewer', language: 'en' });
  }
}

// ---------------------------------------------------------------- extra simulated hosts
/** Starts an HTTP-only host (tests/sim/http_target.py) on host:port; resolves when it answers. */
export async function startHttpHost(host: string, port = 8080): Promise<ChildProcess> {
  const py = existsSync(PY) ? PY : 'python3';
  const child = spawn(py, [join(SIM_DIR, 'http_target.py'), '--host', host, '--port', String(port)], {
    stdio: 'ignore',
  });
  await expect
    .poll(
      async () => {
        try {
          return (await fetch(`http://${host}:${port}/health`)).status;
        } catch {
          return 0;
        }
      },
      { timeout: 15_000, message: `http host ${host}:${port}` },
    )
    .toBe(200);
  return child;
}
export function stopHost(child: ChildProcess | null | undefined): void {
  if (child && child.exitCode === null) child.kill('SIGTERM');
}

/** The backend log written by run.sh (empty string when not available). */
export function backendLog(): string {
  if (!BACKEND_LOG || !existsSync(BACKEND_LOG)) return '';
  return readFileSync(BACKEND_LOG, 'utf8');
}

/** Collects the WS frames a page receives (call before navigation). */
export function collectWsFrames(page: Page): { types: string[]; frames: any[] } {
  const out = { types: [] as string[], frames: [] as any[] };
  page.on('websocket', (ws) => {
    ws.on('framereceived', (f) => {
      try {
        const m = JSON.parse(String(f.payload));
        out.types.push(m.type);
        out.frames.push(m);
      } catch {
        /* binary / non-JSON */
      }
    });
  });
  return out;
}

/** No horizontal page overflow (mobile check). Returns the offending widths for the message. */
export async function horizontalOverflow(page: Page): Promise<{ scrollWidth: number; clientWidth: number }> {
  return page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
}

export { apiAs, uid };
