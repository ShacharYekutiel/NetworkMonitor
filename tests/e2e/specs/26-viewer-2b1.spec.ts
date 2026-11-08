import { expect, test } from '@playwright/test';
import { Api, VIEWER, apiAs, defaultGroupId, loginViaApi, uid } from '../lib/helpers';
import { ensureViewer, raw } from '../lib/lab';
import { findDevice, httpDevice, shot2b, target, waitStatus } from '../lib/lab2b';

// [2b-1] (h) Viewer: can view the map and maintenance windows; cannot acknowledge, edit maintenance,
// refresh topology, edit links/layout, apply dependencies, re-identify or move the test clock
// (403 forbidden + hidden buttons).
test.describe.configure({ mode: 'serial' });

let admin: Api;
let viewer: Api;
let dev: { device: any; sensor: any };
let winId = 0;
const IP = '127.0.0.35';

test.beforeAll(async () => {
  admin = await apiAs();
  await ensureViewer(admin);
  viewer = await apiAs(VIEWER);
  await target(IP, 'fail');
  dev = await httpDevice(admin, { name: uid('viewer-2b-dev'), ip: IP, group_id: await defaultGroupId(admin) });
  await waitStatus(admin, dev.sensor.id, 'down', 45_000);
  winId = (
    await admin.post('/api/maintenance-windows', { name: uid('Viewer window'), scope: 'device', device_id: dev.device.id, kind: 'weekly', weekdays: [7], start_time: '02:00', duration_min: 60 })
  ).id;
});

test.afterAll(async () => {
  await target(IP, 'ok');
  if (winId) await raw(admin, 'DELETE', `/api/maintenance-windows/${winId}`);
  if (dev) await raw(admin, 'DELETE', `/api/devices/${dev.device.id}`);
});

test('viewer reads: topology, maintenance windows/occurrences/active, dashboard, settings/public timezone', async () => {
  for (const p of ['/api/topology', '/api/maintenance-windows', `/api/maintenance-windows/${winId}`, `/api/maintenance-windows/${winId}/occurrences`, '/api/maintenance/active', '/api/dashboard', '/api/settings/public']) {
    const r = await raw(viewer, 'GET', p);
    expect.soft(r.status, p).toBe(200);
  }
  expect((await raw(viewer, 'GET', '/api/settings/public')).body.timezone).toBeTruthy();
});

test('viewer writes -> 403 forbidden', async () => {
  const core = await findDevice(admin, '127.0.0.51');
  const did = core?.id ?? dev.device.id;
  const checks: [string, string, unknown?][] = [
    ['POST', `/api/sensors/${dev.sensor.id}/acknowledge`, { comment: 'x' }],
    ['DELETE', `/api/sensors/${dev.sensor.id}/acknowledge`],
    ['POST', '/api/maintenance-windows', { name: 'x', scope: 'global', kind: 'weekly', weekdays: [1], start_time: '01:00', duration_min: 5 }],
    ['PATCH', `/api/maintenance-windows/${winId}`, { enabled: false }],
    ['DELETE', `/api/maintenance-windows/${winId}`],
    ['POST', '/api/topology/refresh'],
    ['GET', '/api/topology/links'],
    ['POST', '/api/topology/links', { a_device_id: did, b_device_id: dev.device.id, b_port: 'x' }],
    ['DELETE', '/api/topology/links?stale=true'],
    ['PUT', '/api/topology/layout', { positions: [{ node: `d:${did}`, x: 1, y: 1 }] }],
    ['DELETE', '/api/topology/layout'],
    ['GET', '/api/topology/dependency-suggestions'],
    ['POST', '/api/devices/dependencies', { items: [{ device_id: dev.device.id, dependency: { mode: 'none', device_id: null, sensor_id: null } }] }],
    ['POST', `/api/devices/${dev.device.id}/identify`],
    ['PUT', '/api/test/clock', { offset_s: 10 }],
  ];
  const wrong: string[] = [];
  for (const [m, p, b] of checks) {
    const r = await raw(viewer, m, p, b);
    if (r.status !== 403 || r.body?.error?.code !== 'forbidden') wrong.push(`${m} ${p} -> ${r.status} ${r.text.slice(0, 120)}`);
  }
  expect(wrong).toEqual([]);
  // Nothing changed.
  expect((await admin.get(`/api/sensors/${dev.sensor.id}`)).acknowledgement).toBeNull();
  expect((await admin.get(`/api/maintenance-windows/${winId}`)).enabled).toBe(true);
});

test('viewer UI: map visible without admin toolbar; maintenance read-only; no Acknowledge button', async ({ page }) => {
  await loginViaApi(page, VIEWER, '/topology');
  await expect(page.getByRole('navigation').getByRole('link', { name: 'Map' })).toBeVisible();
  await expect(page.locator('main canvas').first()).toBeVisible();
  for (const b of [/^Refresh topology/, /^Add link/, /^Suggest dependencies/, /^Reset layout/, /^Delete \d+ stale/]) {
    await expect(page.getByRole('button', { name: b })).toHaveCount(0);
  }
  await expect(page.getByText(/Include stale/)).toBeVisible();
  await shot2b(page, 'viewer-topology-en');

  await page.getByRole('navigation').getByRole('link', { name: 'Maintenance' }).click();
  await expect(page).toHaveURL(/\/maintenance/);
  await expect(page.getByText(/Viewer window/).first()).toBeVisible();
  await expect(page.getByRole('button', { name: /Add maintenance window/ })).toHaveCount(0);
  await expect(page.getByRole('switch').or(page.getByRole('checkbox', { name: /Enabled/ }))).toHaveCount(0);
  await shot2b(page, 'viewer-maintenance-en');

  await page.goto(`/sensors/${dev.sensor.id}`);
  await expect(page.getByText(/^Down$/).first()).toBeVisible();
  await expect(page.getByRole('button', { name: 'Acknowledge' })).toHaveCount(0);
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Acknowledge' })).toHaveCount(0);
  await page.goto(`/devices/${dev.device.id}`);
  await expect(page.getByRole('button', { name: 'Re-identify' })).toHaveCount(0);
  await expect(page.getByRole('main').getByRole('button', { name: /^(Add maintenance window|Maintenance)$/ })).toHaveCount(0);
});
