// [3] NOC mode / kiosk (docs/architecture.md §13.7, §13.11 spec 59; api.md "[3] NOC kiosk").
// Screen allowed from 127.0.0.1/32 -> /noc#token shows KPIs and down devices; a sensor going down appears
// without a reload (kiosk WS refresh); a screen allowing only 10.9.9.9/32 -> "not available"; regenerating the
// token stops the old URL. The kiosk client (tests/sim/kiosk_client.py) checks the guard from other source
// addresses, the WS auth (5 s, 4401) and that the kiosk header authorises nothing else.
import { expect, test } from '@playwright/test';
import { join } from 'node:path';
import { BASE_URL, apiAs, markDocument, expectSameDocument, type Api } from '../lib/helpers';
import { SIM_DIR, raw } from '../lib/lab';
import { PHASE3, kioskClient, problemDevice, setFeatures, shot3, tx } from '../lib/lab3';

test.describe.configure({ mode: 'serial' });
test.skip(!PHASE3, 'phase-3 simulators not running');

let api: Api;
let screen: any;
let token = '';
let prob: Awaited<ReturnType<typeof problemDevice>>;
const kc = (args: string[], t?: number) => kioskClient(['--base', BASE_URL, ...args], t);

test.beforeAll(async () => {
  api = await apiAs();
  await setFeatures(api, { noc: true });
  prob = await problemDevice(api, '127.0.0.132', 'qa3-noc-web');
  await prob.up();
});
test.afterAll(async () => {
  await prob?.ctl('ok');
  await prob?.stop();
});

test('create a screen: token shown once (nmk_ + 43), stored hashed; allowed sources required', async () => {
  const bad = await raw(api, 'POST', '/api/kiosk-screens', { name: 'qa3-wall-bad', allowed_sources: [], widgets: ['summary'], language: 'en', refresh_s: 10 });
  expect(bad.status).toBe(422);
  const r = await api.post('/api/kiosk-screens', {
    name: 'qa3-wall', enabled: true, allowed_sources: ['127.0.0.1/32'], scope_group_id: null,
    widgets: ['summary', 'down_devices', 'problems', 'recent_alerts'], language: 'en', show_addresses: false, refresh_s: 10,
  });
  token = r.token;
  screen = r.item;
  expect(token).toMatch(/^nmk_[A-Za-z0-9_-]{43}$/);
  expect(screen.token_prefix).toBe(token.slice(0, 12));
  const list = await api.get('/api/kiosk-screens');
  expect(JSON.stringify(list)).not.toContain(token);
});

test('kiosk API guard: view from 127.0.0.1 ok (no IPs), other source 404, wrong token 404, header useless elsewhere', async () => {
  const [ok] = kc(['--token', token, 'view']);
  expect(ok.status).toBe(200);
  expect(ok.cache_control).toMatch(/no-store/);
  expect(ok.set_cookie, 'no cookies for kiosks').toBeNull();
  expect(ok.body.screen.name).toBe('qa3-wall');
  expect(ok.body.summary.device_counts.total).toBeGreaterThan(0);
  expect(ok.body.map, 'widget not configured -> null').toBeNull();
  expect(JSON.stringify(ok.body), 'no addresses without show_addresses').not.toContain('127.0.0.132');
  const [other] = kc(['--token', token, '--source', '127.0.0.77', 'view']);
  expect(other.status, 'source outside allowed_sources').toBe(404);
  const [wrong] = kc(['--token', 'nmk_' + 'z'.repeat(43), 'view']);
  expect(wrong.status).toBe(404);
  expect(wrong.body).toEqual(other.body);
  const [probe] = kc(['--token', token, 'probe', '--path', '/api/devices']);
  expect(probe.status, 'the kiosk header is not a session').toBe(401);
  const [probe2] = kc(['--token', token, 'probe', '--path', '/api/settings']);
  expect(probe2.status).toBe(401);
});

test('kiosk WS: kiosk_auth within 5 s -> ready; no auth -> 4401; refresh on a status change', async () => {
  test.setTimeout(120_000);
  const noAuth = kc(['ws', '--no-auth', '--seconds', '9'], 30_000);
  const close = noAuth.find((e) => e.event === 'close');
  expect(close?.code).toBe(4401);
  expect(close.t).toBeGreaterThanOrEqual(4);
  expect(close.t).toBeLessThan(8);
  const late = kc(['--token', token, 'ws', '--auth-delay', '6', '--seconds', '8'], 30_000);
  expect(late.find((e) => e.event === 'close')?.code).toBe(4401);
  const bad = kc(['ws', '--bad-token', '--seconds', '4'], 30_000);
  expect(bad.find((e) => e.event === 'close')?.code).toBe(4401);
  // a good session receives kiosk.ready, then kiosk.refresh after a status change (debounced 5 s)
  const { spawn } = await import('node:child_process');
  const child = spawn(process.env.PY ?? 'python3', [join(SIM_DIR, 'kiosk_client.py'), '--base', BASE_URL, '--token', token, 'ws', '--seconds', '45']);
  let out = '';
  child.stdout.on('data', (d) => (out += d));
  await new Promise((r) => setTimeout(r, 3000));
  await prob.down();
  await new Promise((r) => child.on('exit', r));
  const msgs = out.trim().split('\n').filter(Boolean).map((l) => JSON.parse(l));
  expect(msgs.find((m) => m.data?.type === 'kiosk.ready'), 'kiosk.ready').toBeTruthy();
  expect(msgs.find((m) => m.data?.type === 'kiosk.refresh'), 'kiosk.refresh after the sensor went down').toBeTruthy();
  await prob.up();
});

test('/noc#token in a session-less browser: KPIs, a device going down appears without reload', async ({ browser }) => {
  test.setTimeout(150_000);
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await ctx.newPage();
  await page.goto(`/noc#${token}`);
  await expect(page.getByText(tx('noc.devicesUp', /devices up|up/i)).first()).toBeVisible({ timeout: 20_000 });
  expect(page.url(), 'token removed from the address bar').not.toContain(token);
  expect(await page.evaluate(() => sessionStorage.length)).toBeGreaterThan(0);
  expect((await ctx.cookies()).length, 'no cookies').toBe(0);
  await markDocument(page);
  await prob.down();
  await expect(page.getByText('qa3-noc-web').first()).toBeVisible({ timeout: 30_000 });
  await expectSameDocument(page);
  await shot3(page, '59-noc-down');
  await prob.up();
  // recovered: gone from down_devices/problems (recent alerts keep listing it; other specs' devices may be down)
  await expect.poll(() => {
    const v = kc(['--token', token, 'view'])[0].body;
    return [...(v.down_devices ?? []), ...(v.problems ?? [])].some((d: any) => (d.name ?? d.device_name) === 'qa3-noc-web');
  }, { timeout: 60_000 }).toBe(false);
  await ctx.close();
});

test('screen allowing only 10.9.9.9/32 -> "not available"; regenerate -> old token 404 and old page stops', async ({ browser }) => {
  test.setTimeout(90_000);
  const other = await api.post('/api/kiosk-screens', { name: 'qa3-wall-remote', allowed_sources: ['10.9.9.9/32'], widgets: ['summary'], language: 'he', refresh_s: 10 });
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.goto(`/noc#${other.token}`);
  await expect(page.getByText(tx('noc.unavailable', /not available|unavailable/i))).toBeVisible({ timeout: 20_000 });
  await shot3(page, '59-noc-unavailable');
  // regenerate qa3-wall
  const p2 = await ctx.newPage();
  await p2.goto(`/noc#${token}`);
  await expect(p2.getByText(tx('noc.devicesUp', /devices up|up/i)).first()).toBeVisible({ timeout: 20_000 });
  const re = await api.post(`/api/kiosk-screens/${screen.id}/regenerate`);
  expect(re.token).not.toBe(token);
  const [old] = kc(['--token', token, 'view']);
  expect(old.status).toBe(404);
  const [neu] = kc(['--token', re.token, 'view']);
  expect(neu.status).toBe(200);
  await expect(p2.getByText(tx('noc.unavailable', /not available|unavailable/i))).toBeVisible({ timeout: 40_000 });
  // disabling the feature: every kiosk request 404, management 409
  await setFeatures(api, { noc: false });
  expect(kc(['--token', re.token, 'view'])[0].status).toBe(404);
  expect((await raw(api, 'GET', '/api/kiosk-screens')).status).toBe(409);
  await setFeatures(api, { noc: true });
  expect(kc(['--token', re.token, 'view'])[0].status).toBe(200);
  // 30 failures per IP per 5 min -> 429
  const [h] = kc(['--source', '127.0.0.78', 'hammer', '--count', '32']);
  expect(h.statuses.slice(0, 30).every((s: number) => s === 404)).toBe(true);
  expect(h.statuses.slice(30)).toContain(429);
  await ctx.close();
});
