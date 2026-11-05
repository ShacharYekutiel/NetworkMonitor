// Phase 2b-2 helpers: port-inventory lab setup, refresh waits, search, templates user dir, i18n lookup
// (docs/architecture.md §11.6, docs/api.md [2b-2], docs/data-model.md [2b-2]).
//
// Lab addresses (tests/sim/lab.sh, LAB_2B2=1). The contract places the port sims on 127.0.0.61-.65; those
// collide with the 2b-1 fingerprint hosts that spec 23 scans with SNMP on, so the lab uses .81-.85 with the
// contract's data unchanged (QA defect report). Contract ".61" == AOS here, ".62" == CW, and so on.
import { expect, type Page } from '@playwright/test';
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { Api, uid } from './helpers';
import { SIM_DIR, ensureGroup, raw, snmpCreds } from './lab';
import { findDevice, refreshTopology } from './lab2b';

export const IP = {
  aos: '127.0.0.81', // contract .61 ArubaOS-Switch
  cw: '127.0.0.82', // contract .62 Comware
  cx: '127.0.0.83', // contract .63 Aruba CX
  arp: '127.0.0.84', // contract .64 ARP router (ipNetToPhysical)
  legacy: '127.0.0.85', // contract .65 legacy ARP (ipNetToMedia)
  aos52: '127.0.0.86', // QA extra: 52-port ArubaOS
  cp: '127.0.0.71',
  cpCluster: '127.0.0.72',
  instant: '127.0.0.73',
  ap: '127.0.0.54', // 2b-1 UniFi LLDP sim: LLDP neighbour of CW port 2 (AP tile)
} as const;

export const SYNTH: Record<string, string> = {
  aruba_cx: '127.0.0.101',
  aruba_wifi: '127.0.0.102',
  checkpoint_gaia: '127.0.0.103',
  fortinet_fortigate: '127.0.0.104',
  cisco_ios: '127.0.0.105',
  paloalto_panos: '127.0.0.106',
  mikrotik_routeros: '127.0.0.107',
  apc_ups: '127.0.0.108',
  hpe_ilo: '127.0.0.109',
  dell_idrac: '127.0.0.110',
};

export const PORTS_GROUP = 'QA Ports 2b-2';
export const SHOT_DIR_2B2 = process.env.QA2B2_SHOT_DIR ?? join(__dirname, '..', 'test-results', 'screenshots', 'qa-2b2');
export const E2E_DATA_DIR = process.env.E2E_DATA_DIR ?? '';
export const TEMPLATE_FIXTURES = join(SIM_DIR, 'templates', 'user');

// ---------------------------------------------------------------- i18n (the real locale files)
const LOCALES = join(__dirname, '..', '..', '..', 'frontend', 'src', 'i18n', 'locales');
const cache: Record<string, any> = {};
function locale(lang: 'en' | 'he'): any {
  if (!cache[lang]) cache[lang] = JSON.parse(readFileSync(join(LOCALES, `${lang}.json`), 'utf8'));
  return cache[lang];
}
/** i18next-style lookup with {{var}} interpolation and _one/_other plurals; throws on a missing key. */
export function T(key: string, vars: Record<string, unknown> = {}, lang: 'en' | 'he' = 'en'): string {
  const root = locale(lang);
  const get = (k: string) => k.split('.').reduce((o: any, p) => (o == null ? undefined : o[p]), root);
  let v = get(key);
  if (v === undefined && 'count' in vars) v = get(`${key}_${Number(vars.count) === 1 ? 'one' : 'other'}`) ?? get(`${key}_other`);
  if (typeof v !== 'string') throw new Error(`i18n key ${lang}:${key} missing`);
  return v.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, n) => String(vars[n] ?? ''));
}
export function hasKey(key: string, lang: 'en' | 'he' = 'en'): boolean {
  try {
    T(key, {}, lang);
    return true;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------- ports API
export type PortLab = {
  groupId: number;
  aos: any; cw: any; cx: any; arp: any; legacy: any; aos52: any; ap: any;
  ifSensorId: number; // snmp_interface sensor on AOS port 1
  linkId: number; // manual link CW if 49 <-> AOS if 10
};

export async function devicePorts(api: Api, id: number, kinds: string[] = []): Promise<any> {
  const q = kinds.map((k) => `kind=${encodeURIComponent(k)}`).join('&');
  return api.get(`/api/devices/${id}/ports${q ? `?${q}` : ''}`);
}
export function portOf(dp: any, ifIndex: number): any {
  const p = dp.ports.find((x: any) => x.if_index === ifIndex);
  if (!p) throw new Error(`device ${dp.device_id}: no port ${ifIndex} (${dp.ports.map((x: any) => x.if_index).join(',')})`);
  return p;
}

/** POST …/ports/refresh (202) and wait until a newer run finished (status.running false, last_run_at newer). */
export async function refreshPorts(api: Api, id: number, timeout = 100_000): Promise<any> {
  const before = (await devicePorts(api, id)).status;
  await expect
    .poll(async () => (await devicePorts(api, id)).status.running, { timeout: 100_000, message: `device ${id} idle before refresh` })
    .toBe(false);
  const r = await raw(api, 'POST', `/api/devices/${id}/ports/refresh`);
  expect(r.status, `refresh ${id}: ${r.text}`).toBe(202);
  expect(r.body).toEqual({ device_id: id, queued: true });
  return waitRun(api, id, before.last_run_at, timeout);
}
export async function waitRun(api: Api, id: number, prevRunAt: string | null, timeout = 100_000): Promise<any> {
  let dp: any = null;
  await expect
    .poll(
      async () => {
        dp = await devicePorts(api, id);
        const s = dp.status;
        return !s.running && !!s.last_run_at && (prevRunAt === null || Date.parse(s.last_run_at) > Date.parse(prevRunAt));
      },
      { timeout, intervals: [300, 500, 1000, 2000], message: `ports run of device ${id} finished` },
    )
    .toBe(true);
  return dp;
}

async function ensureDevice(api: Api, groupId: number, host: string, name: string, extra: Record<string, unknown> = {}, sensors = ['ping', 'snmp_system']): Promise<any> {
  let d = await findDevice(api, host);
  if (!d) d = await api.post('/api/devices', { group_id: groupId, name, host, snmp_credentials: snmpCreds(), auto_sensors: sensors, ...extra });
  else if (Object.keys(extra).length) d = await api.patch(`/api/devices/${d.id}`, extra);
  return api.get(`/api/devices/${d.id}`);
}

/**
 * The §11.6 test setup, idempotent (every 2b-2 spec calls it; the first call does the work):
 * devices for AOS/CW/CX/ARP/legacy (+ 52-port) with ping + snmp_system (identity null -> class detection),
 * arp_collect on for legacy, the UniFi AP device .54 (identified -> access_point) as the LLDP neighbour
 * of CW port 2, a manual link CW if 49 <-> AOS if 10, an snmp_interface sensor on AOS port 1, a topology
 * refresh (LLDP link CW 2 -> AP) and port refreshes: ARP, legacy, then AOS, CW, CX (and AOS52).
 */
export async function ensurePortLab(api: Api, opts: { force?: boolean } = {}): Promise<PortLab> {
  const groupId = await ensureGroup(api, PORTS_GROUP);
  const aos = await ensureDevice(api, groupId, IP.aos, 'port-aos');
  const cw = await ensureDevice(api, groupId, IP.cw, 'port-cw');
  const cx = await ensureDevice(api, groupId, IP.cx, 'port-cx');
  const arp = await ensureDevice(api, groupId, IP.arp, 'fw-arp');
  const legacy = await ensureDevice(api, groupId, IP.legacy, 'gw2', { arp_collect: 'on' });
  const aos52 = await ensureDevice(api, groupId, IP.aos52, 'port-aos52');
  // AP device (the 2b-1 UniFi sim .54, also created by spec 25): identified -> access_point (rule 3, "U6-Lite").
  // The contract's Aruba Instant sim (.73) cannot serve: rule 10's switch regex contains `arubaos`.
  let ap = await findDevice(api, IP.ap);
  if (!ap) ap = await api.post('/api/devices', { group_id: groupId, name: 'U6-Lite-Office', host: IP.ap, snmp_credentials: snmpCreds(), auto_sensors: [] });
  if (!ap.identity) {
    const r = await raw(api, 'POST', `/api/devices/${ap.id}/identify`);
    expect(r.status, r.text).toBe(200);
  }
  ap = await api.get(`/api/devices/${ap.id}`);
  expect(ap.effective_class ?? ap.identity?.device_class, 'AP neighbour class').toBe('access_point');

  // snmp_interface sensor on AOS port 1 (contract: port 1 sensor_id set)
  const sensors = (await api.get(`/api/sensors?device_id=${aos.id}&page_size=1000`)).items;
  let ifSensor = sensors.find((s: any) => s.type === 'snmp_interface' && s.config?.if_index === 1);
  if (!ifSensor) ifSensor = await api.post('/api/sensors', { device_id: aos.id, type: 'snmp_interface', name: '1', interval_s: 10, config: { if_index: 1, if_name: '1' } });

  // manual link CW if 49 <-> AOS if 10
  const links = (await api.get(`/api/topology/links?device_id=${cw.id}&page_size=1000`)).items;
  const ends = (l: any) => [l.a?.device_id ?? l.a_device_id, l.b?.device_id ?? l.b_device_id].sort().join(',');
  let link = links.find((l: any) => ends(l) === [cw.id, aos.id].sort().join(','));
  if (!link) {
    const r = await raw(api, 'POST', '/api/topology/links', { a_device_id: cw.id, a_if_index: 49, b_device_id: aos.id, b_neighbor_id: null, b_if_index: 10, b_port: '10' });
    if (r.status === 201) link = r.body;
    else {
      // 409: it exists, but the filtered listing did not return it -> look through every link
      const all = (await api.get('/api/topology/links?page_size=1000')).items;
      link = all.find((l: any) => ends(l) === [cw.id, aos.id].sort().join(','));
      expect(link, `manual link CW 49 <-> AOS 10: POST ${r.status} ${r.text}; filtered listing ${JSON.stringify(links)}`).toBeTruthy();
    }
  }

  const lab: PortLab = { groupId, aos, cw, cx, arp, legacy, aos52, ap, ifSensorId: ifSensor.id, linkId: link.id };
  const fresh = !(await devicePorts(api, aos.id)).status.last_ok_at;
  if (fresh || opts.force) {
    // LLDP: CW port 2 -> U6-Lite-Office (mgmt 127.0.0.54)
    const lldp = (await api.get(`/api/topology/links?device_id=${cw.id}&page_size=1000`)).items.find((l: any) => l.source === 'lldp');
    if (!lldp) await refreshTopology(api);
    for (const d of [arp, legacy, aos, cw, cx, aos52]) await refreshPorts(api, d.id);
  }
  return lab;
}

export async function search(api: Api, q: Record<string, string>): Promise<{ status: number; body: any; text: string }> {
  const qs = Object.entries(q).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&');
  return raw(api, 'GET', `/api/search/mac?${qs}`);
}

// ---------------------------------------------------------------- UI helpers
export async function openPorts(page: Page, deviceId: number, view: 'faceplate' | 'list' = 'faceplate', port?: number): Promise<void> {
  await page.goto(`/devices/${deviceId}?tab=ports&view=${view}${port !== undefined ? `&port=${port}` : ''}`);
  await expect(page.getByTestId('ports-tab')).toBeVisible();
  if (view === 'faceplate') await expect(page.getByTestId('faceplate')).toBeVisible();
  else await expect(page.getByTestId('port-list')).toBeVisible();
}
export const tile = (page: Page, ifIndex: number) => page.locator(`[data-testid="faceplate"] button[data-if-index="${ifIndex}"]`);
export async function box(page: Page, ifIndex: number): Promise<{ x: number; y: number; width: number; height: number }> {
  const b = await tile(page, ifIndex).boundingBox();
  if (!b) throw new Error(`tile ${ifIndex} not rendered`);
  return b;
}

export async function shot2b2(page: Page, name: string): Promise<string> {
  mkdirSync(SHOT_DIR_2B2, { recursive: true });
  const p = join(SHOT_DIR_2B2, `${name}.png`);
  await page.screenshot({ path: p, fullPage: false });
  return p;
}

export async function setLang(page: Page, lang: 'en' | 'he'): Promise<void> {
  await page.request.patch('/api/auth/me', { data: { language: lang } });
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('dir', lang === 'he' ? 'rtl' : 'ltr');
}

// ---------------------------------------------------------------- user template directory
export function userTemplateDir(): string {
  if (!E2E_DATA_DIR) throw new Error('E2E_DATA_DIR not set (run through tests/e2e/run.sh)');
  return join(E2E_DATA_DIR, 'templates');
}
function walkFiles(root: string): string[] {
  const out: string[] = [];
  for (const n of readdirSync(root)) {
    const p = join(root, n);
    if (statSync(p).isDirectory()) out.push(...walkFiles(p));
    else out.push(p);
  }
  return out;
}
/** Copies tests/sim/templates/user/** into ${NETMON_DATA_DIR}/templates (returns the relative paths). */
export function installUserTemplates(): string[] {
  const dst = userTemplateDir();
  if (!dst || !dst.endsWith('/templates')) throw new Error(`refusing to write to ${dst}`);
  const rels: string[] = [];
  for (const f of walkFiles(TEMPLATE_FIXTURES)) {
    const rel = relative(TEMPLATE_FIXTURES, f);
    mkdirSync(dirname(join(dst, rel)), { recursive: true });
    copyFileSync(f, join(dst, rel));
    rels.push(rel);
  }
  return rels.sort();
}
export function removeUserTemplate(rel: string): void {
  const p = join(userTemplateDir(), rel);
  if (existsSync(p)) rmSync(p);
}
export function clearUserTemplates(): void {
  const dst = userTemplateDir();
  if (!dst.endsWith('/templates') || !existsSync(dst)) return;
  for (const n of readdirSync(dst)) rmSync(join(dst, n), { recursive: true, force: true });
}

export { uid };
