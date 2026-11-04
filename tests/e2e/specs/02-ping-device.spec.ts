import { expect, test } from '@playwright/test';
import {
  Api,
  MIN_INTERVAL,
  apiAs,
  card,
  expandTreeDevice,
  loginViaApi,
  markDocument,
  expectSameDocument,
  statusDot,
  treeDevice,
  treeSensor,
  uid,
  waitForSensor,
} from '../lib/helpers';

// Scenario (b): group -> device 127.0.0.1 with a ping sensor -> data arrives -> "up" in tree and
// dashboard -> history chart renders.
test.describe.configure({ mode: 'serial' });

let api: Api;
const groupName = uid('E2E Group');
const deviceName = uid('localhost');
let groupId = 0;
let deviceId = 0;
let sensorId = 0;

test.beforeAll(async () => {
  api = await apiAs();
});

test('create a group from the dashboard', async ({ page }) => {
  await loginViaApi(page);
  await page.getByRole('button', { name: 'Add group' }).click();
  const dlg = page.getByRole('dialog');
  await expect(dlg.getByRole('heading', { name: 'Add group' })).toBeVisible();
  await dlg.getByLabel(/^Name/).fill(groupName);
  await dlg.getByLabel(/Description/).fill('Created by the E2E suite');
  await dlg.getByRole('button', { name: 'Save' }).click();
  await expect(dlg).toBeHidden();

  const link = page.locator('aside a[href^="/groups/"]', { hasText: groupName });
  await expect(link).toBeVisible();
  groupId = Number((await link.getAttribute('href'))!.split('/').pop());
  expect(groupId).toBeGreaterThan(0);
  const g = await api.get(`/api/groups/${groupId}`);
  expect(g).toMatchObject({ name: groupName, parent_id: null, description: 'Created by the E2E suite' });
});

test('add device 127.0.0.1 with an auto ping sensor', async ({ page }) => {
  await loginViaApi(page, undefined, `/groups/${groupId}`);
  await expect(page.getByRole('heading', { name: groupName })).toBeVisible();
  await page.getByRole('button', { name: 'Add device' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel(/^Name/).fill(deviceName);
  await dlg.getByLabel(/^Host/).fill('127.0.0.1');
  // The group defaults to the page's group.
  await expect(dlg.getByLabel(/^Group/)).toHaveValue(String(groupId));
  // "Ping" is pre-selected in the auto sensors.
  await expect(dlg.getByRole('checkbox', { name: 'Ping' })).toBeChecked();
  await dlg.getByRole('button', { name: 'Save' }).click();

  await expect(page).toHaveURL(/\/devices\/\d+$/);
  deviceId = Number(page.url().split('/').pop());
  await expect(page.getByRole('heading', { name: deviceName })).toBeVisible();
  await expect(treeDevice(page, deviceId)).toBeVisible();

  const sensors = await api.get(`/api/sensors?device_id=${deviceId}`);
  expect(sensors.items).toHaveLength(1);
  expect(sensors.items[0].type).toBe('ping');
  sensorId = sensors.items[0].id;
  // Note (UX, not a contract violation): api.md lets the backend fall back to the type key for
  // auto_sensors, so the UI shows the raw key "ping" as the sensor name. Recorded, not asserted.
});

test('ping sensor gets data and shows up in tree and dashboard (live)', async ({ page }) => {
  // Shorten the interval (the minimum the API allows) so the scheduler runs it quickly.
  await api.patch(`/api/sensors/${sensorId}`, { interval_s: MIN_INTERVAL });

  await loginViaApi(page, undefined, '/');
  await markDocument(page);
  const row = treeSensor(page, sensorId);
  // Expand the device in the tree if the sensor row is not visible yet.
  await expandTreeDevice(page, deviceId);
  await expect(row).toBeVisible();

  // Wait for the scheduled check (first run has jitter < interval).
  await expect(statusDot(row, 'Up')).toBeVisible({ timeout: 45_000 });
  await expect(row).toContainText(/ms/);
  await expectSameDocument(page); // updated through the WebSocket, no reload

  const s = await waitForSensor(api, sensorId, (x) => x.status === 'up' && x.last_check_at !== null);
  expect(s.channels.find((c: any) => c.key === 'latency_ms').last_value).not.toBeNull();

  // The dashboard tiles count it as up.
  const d = await api.get('/api/dashboard');
  expect(d.sensor_counts.up).toBeGreaterThanOrEqual(1);
  const sensorsSection = page.locator('section').filter({ has: page.getByRole('heading', { name: 'Sensors', exact: true }) });
  // Tile = value + label; the "Up" tile must count at least this sensor.
  const upTile = sensorsSection.locator('div.rounded-lg').filter({ hasText: /^\d+Up$/ });
  await expect(upTile).toHaveText(/^[1-9]\d*Up$/);
  // The recent events list shows the unknown -> up transition of this sensor.
  const recent = card(page, 'Recent events');
  await expect(recent.getByText(deviceName).first()).toBeVisible();
});

test('device and group roll up to up', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${deviceId}`);
  await expect(page.getByRole('heading', { name: deviceName })).toBeVisible();
  await expect(page.locator('main').getByText('Up', { exact: true }).first()).toBeVisible();
  await expect(statusDot(treeDevice(page, deviceId), 'Up')).toBeVisible();
  const g = await api.get(`/api/groups/${groupId}`);
  expect(g.status).toBe('up');
});

test('sensor page renders a history chart from real measurements', async ({ page }) => {
  // Make sure at least two measurements exist (one more run on demand).
  await api.post(`/api/sensors/${sensorId}/run`);
  await loginViaApi(page, undefined, `/sensors/${sensorId}`);
  await expect(page.getByRole('heading', { level: 1, name: /ping/i })).toBeVisible();
  await page.getByRole('button', { name: '1 hour' }).click();
  const history = card(page, /^History: Latency/);
  await expect(history.locator('canvas').first()).toBeVisible();
  await expect(history.getByText('No data in this time range.')).toHaveCount(0);
  const box = await history.locator('canvas').first().boundingBox();
  expect(box!.width).toBeGreaterThan(200);
  expect(box!.height).toBeGreaterThan(100);

  const h = await api.get(`/api/sensors/${sensorId}/history?range=1h`);
  expect(h.bucket_s).toBe(60);
  const lat = h.channels.find((c: any) => c.key === 'latency_ms');
  expect(lat.points.length).toBeGreaterThanOrEqual(1);
  expect(lat.points[0]).toEqual(expect.objectContaining({ t: expect.stringMatching(/Z$/), avg: expect.any(Number) }));

  // Channel tiles show the latest values.
  await expect(page.getByRole('button', { name: /Latency/ })).toContainText('ms');
  await expect(page.getByRole('button', { name: /Packet loss/ })).toContainText('%');
});
