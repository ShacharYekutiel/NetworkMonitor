// [security] GET /api/health is public and answers only {"status": "ok"}: an anonymous caller learns
// whether the service is up, nothing else. The runtime details (version, uptime, scheduler,
// receivers, Kerberos, assistant, test mode) are at GET /api/health/details for any signed-in user
// or API token; the Settings page subtitle still shows them; the login page needs none of them.
import { randomUUID } from 'node:crypto';
import { expect, request as pwRequest, test } from '@playwright/test';
import { Api, BASE_URL, apiAs, loginViaApi, uid } from '../lib/helpers';

test.describe.configure({ mode: 'serial' });

const DETAIL_KEYS = [
  'assistant',
  'database',
  'flows_rollup',
  'ingest',
  'kerberos',
  'poller',
  'sensors_scheduled',
  'status',
  'test_mode',
  'timescale',
  'trgm',
  'uptime_s',
  'version',
];

let admin: Api;
const viewer = { username: `hv${randomUUID().replace(/-/g, '').slice(0, 12)}`, password: 'Viewer-pass-92' };
let viewerId = 0;
let tokenId = 0;

test.beforeAll(async () => {
  admin = await apiAs();
  viewerId = (await admin.post('/api/users', { ...viewer, role: 'viewer' })).id;
});

test.afterAll(async () => {
  if (tokenId) await admin.del(`/api/tokens/${tokenId}`).catch(() => undefined);
  if (viewerId) await admin.del(`/api/users/${viewerId}`).catch(() => undefined);
});

test('anonymous /api/health: exactly {"status": "ok"}; /api/health/details: 401', async () => {
  const anon = await pwRequest.newContext({ baseURL: BASE_URL });
  const r = await anon.get('/api/health');
  expect(r.status()).toBe(200);
  expect(await r.json()).toEqual({ status: 'ok' });
  const version = (await admin.get('/api/health/details')).version as string;
  expect(version).toBeTruthy();
  expect(await r.text()).not.toContain(version);
  const d = await anon.get('/api/health/details');
  expect(d.status()).toBe(401);
  expect((await d.json()).error.code).toBe('unauthenticated');
  expect(await d.text()).not.toContain(version);
  // a bogus bearer token is refused too
  const b = await anon.get('/api/health/details', { headers: { Authorization: 'Bearer nm_bogus' } });
  expect(b.status()).toBe(401);
  await anon.dispose();
});

test('signed-in admin and viewer, and a viewer API token, get the details', async () => {
  const v = await apiAs(viewer);
  for (const [who, api] of [
    ['admin', admin],
    ['viewer', v],
  ] as const) {
    const h = await api.get('/api/health/details');
    expect(Object.keys(h).sort(), who).toEqual(DETAIL_KEYS);
    expect(h.status, who).toBe('ok');
    expect(h.database, who).toBe('ok');
    expect(typeof h.sensors_scheduled, who).toBe('number');
    expect(Object.keys(h.ingest), who).toEqual(expect.arrayContaining(['trap', 'syslog_udp', 'syslog_tcp']));
    // the public answer stays minimal for a signed-in caller as well
    expect(await api.get('/api/health'), who).toEqual({ status: 'ok' });
  }
  const tok = await v.post('/api/tokens', { name: uid('health'), role: 'viewer' });
  tokenId = tok.item.id;
  const bearer = await pwRequest.newContext({ baseURL: BASE_URL, extraHTTPHeaders: { Authorization: `Bearer ${tok.token}` } });
  const r = await bearer.get('/api/health/details');
  expect(r.status()).toBe(200);
  expect(Object.keys(await r.json()).sort()).toEqual(DETAIL_KEYS);
  await bearer.dispose();
  await v.ctx.dispose();
});

test('Settings shows the version line; the login page works without health details', async ({ page, browser }) => {
  const version = (await admin.get('/api/health/details')).version as string;
  await loginViaApi(page, undefined, '/settings');
  await expect(page.getByText(`Version ${version}`, { exact: false })).toBeVisible();

  const ctx = await browser.newContext({ baseURL: BASE_URL });
  const p = await ctx.newPage();
  const health: { url: string; status: number }[] = [];
  p.on('response', (res) => {
    if (res.url().includes('/api/health')) health.push({ url: res.url(), status: res.status() });
  });
  await p.goto('/login');
  await expect(p.locator('input[type="password"]')).toBeVisible();
  await p.waitForLoadState('networkidle');
  // the pre-login UI never asks for the details (and so never sees a 401 for them)
  expect(health.filter((h) => h.url.includes('/api/health/details'))).toEqual([]);
  await expect(p.getByText(version)).toHaveCount(0);
  await ctx.close();
});
