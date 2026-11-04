import { expect, test } from '@playwright/test';
import {
  Api,
  VIEWER,
  apiAs,
  card,
  defaultGroupId,
  loginViaApi,
  loginViaUi,
  uid,
} from '../lib/helpers';

// Scenario (f): a viewer created by the admin logs in, sees no admin pages or write buttons, and the
// API answers 403 forbidden for writes.
test.describe.configure({ mode: 'serial' });

let admin: Api;
let deviceId = 0;
let sensorId = 0;

test.beforeAll(async () => {
  admin = await apiAs();
  // Precondition: a device with a sensor to look at.
  const d = await admin.post('/api/devices', {
    group_id: await defaultGroupId(admin),
    name: uid('viewer-target'),
    host: '127.0.0.1',
    auto_sensors: ['ping'],
  });
  deviceId = d.id;
  sensorId = (await admin.get(`/api/sensors?device_id=${deviceId}`)).items[0].id;
});

test('admin creates a viewer on the Users page', async ({ page }) => {
  await loginViaApi(page, undefined, '/users');
  await page.getByRole('button', { name: 'Add user' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel(/^Username/).fill(VIEWER.username);
  await dlg.getByLabel(/^Email/).fill('viewer1@example.com');
  await expect(dlg.getByLabel('Role')).toHaveValue('viewer');
  await dlg.getByLabel(/^Password/).fill(VIEWER.password);
  await dlg.getByRole('button', { name: 'Save' }).click();
  await expect(dlg).toBeHidden();
  const row = page.getByRole('row').filter({ hasText: VIEWER.username });
  await expect(row).toContainText('Viewer');
  await expect(row).toContainText('viewer1@example.com');
});

test('viewer sees no admin navigation or write buttons', async ({ page }) => {
  await loginViaUi(page, VIEWER);
  await expect(page.getByRole('heading', { level: 1, name: 'Dashboard' })).toBeVisible();
  await expect(page.getByRole('button', { name: /viewer1/ })).toContainText('Viewer');
  const nav = page.getByRole('complementary');
  await expect(nav.getByRole('link', { name: 'Dashboard' })).toBeVisible();
  await expect(nav.getByRole('link', { name: 'Events' })).toBeVisible();
  for (const name of ['Notifications', 'Settings', 'Users']) {
    await expect(nav.getByRole('link', { name })).toHaveCount(0);
  }
  await expect(page.getByRole('button', { name: 'Add device' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Add group' })).toHaveCount(0);
  await expect(page.getByTestId('ws-indicator')).toContainText('Live');

  // Group page
  await page.goto(`/groups/${await defaultGroupId(admin)}`);
  await expect(page.getByRole('heading', { level: 1, name: /Default/ })).toBeVisible();
  for (const name of ['Add device', 'Add subgroup', 'Edit', 'Delete']) {
    await expect(page.getByRole('button', { name, exact: true })).toHaveCount(0);
  }

  // Device page
  await page.goto(`/devices/${deviceId}`);
  await expect(card(page, 'Sensors')).toBeVisible();
  for (const name of ['Add sensor', 'Pause device', 'Edit', 'Delete']) {
    await expect(page.getByRole('button', { name, exact: true })).toHaveCount(0);
  }
  // No per-row run/pause actions either.
  await expect(card(page, 'Sensors').getByRole('button')).toHaveCount(0);

  // Sensor page: read-only thresholds, no actions.
  await page.goto(`/sensors/${sensorId}`);
  await expect(card(page, 'Thresholds')).toBeVisible();
  for (const name of ['Run now', 'Pause', 'Edit', 'Delete', 'Save thresholds']) {
    await expect(page.getByRole('button', { name, exact: true })).toHaveCount(0);
  }
  const inputs = card(page, 'Thresholds').getByRole('textbox');
  expect(await inputs.count()).toBeGreaterThan(0);
  for (const i of await inputs.all()) await expect(i).toBeDisabled();

  // Admin pages typed into the address bar show the forbidden message.
  for (const path of ['/settings', '/users', '/notifications']) {
    await page.goto(path);
    await expect(page.getByRole('alert')).toContainText('You do not have permission for this action.');
  }
});

test('viewer gets 403 forbidden from write and admin endpoints', async ({ page }) => {
  await loginViaApi(page, VIEWER);
  const r = page.request;
  const json = { 'Content-Type': 'application/json' };
  const checks: [string, () => ReturnType<typeof r.get>][] = [
    ['POST /api/groups', () => r.post('/api/groups', { data: { name: 'nope' }, headers: json })],
    ['POST /api/devices', () => r.post('/api/devices', { data: { group_id: 1, name: 'x', host: '1.2.3.4' }, headers: json })],
    ['PATCH /api/devices/{id}', () => r.patch(`/api/devices/${deviceId}`, { data: { name: 'x' }, headers: json })],
    ['DELETE /api/devices/{id}', () => r.delete(`/api/devices/${deviceId}`)],
    ['POST /api/sensors', () => r.post('/api/sensors', { data: { device_id: deviceId, type: 'ping' }, headers: json })],
    ['PATCH /api/sensors/{id}', () => r.patch(`/api/sensors/${sensorId}`, { data: { enabled: false }, headers: json })],
    ['POST /api/sensors/{id}/run', () => r.post(`/api/sensors/${sensorId}/run`)],
    ['POST /api/sensors/test', () => r.post('/api/sensors/test', { data: { device_id: deviceId, type: 'ping', config: {} }, headers: json })],
    ['GET /api/devices/{id}/snmp/interfaces', () => r.get(`/api/devices/${deviceId}/snmp/interfaces`)],
    ['GET /api/users', () => r.get('/api/users')],
    ['POST /api/users', () => r.post('/api/users', { data: { username: 'x123', password: 'x1234567', role: 'admin' }, headers: json })],
    ['GET /api/settings', () => r.get('/api/settings')],
    ['PUT /api/settings', () => r.put('/api/settings', { data: { retention_days: 5 }, headers: json })],
    ['GET /api/notification-channels', () => r.get('/api/notification-channels')],
    ['GET /api/alert-rules', () => r.get('/api/alert-rules')],
  ];
  const wrong: string[] = [];
  for (const [label, fn] of checks) {
    const res = await fn();
    const body = await res.json().catch(() => null);
    if (res.status() !== 403 || body?.error?.code !== 'forbidden') wrong.push(`${label} -> ${res.status()} ${JSON.stringify(body)}`);
  }
  expect(wrong, 'endpoints that did not answer 403 forbidden').toEqual([]);

  // Reads are allowed for a viewer.
  for (const p of ['/api/tree', '/api/dashboard', `/api/sensors/${sensorId}`, `/api/sensors/${sensorId}/history?range=1h`, '/api/events', '/api/sensor-types']) {
    expect((await r.get(p)).status(), p).toBe(200);
  }
  // A viewer may still change their own profile language.
  expect((await r.patch('/api/auth/me', { data: { language: 'en' }, headers: json })).status()).toBe(200);
  // Nothing changed.
  const s = await admin.get(`/api/sensors/${sensorId}`);
  expect(s.enabled).toBe(true);
});
