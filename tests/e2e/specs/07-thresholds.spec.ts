import { expect, test } from '@playwright/test';
import {
  Api,
  MIN_INTERVAL,
  apiAs,
  card,
  defaultGroupId,
  expandTreeDevice,
  expectSameDocument,
  loginViaApi,
  markDocument,
  statusDot,
  treeSensor,
  uid,
  waitForSensor,
} from '../lib/helpers';

// Scenario (g): a warning threshold on ping latency set very low (warning_max 0.001 ms) turns the
// sensor to "warning"; restoring it turns it back to "up".
test.describe.configure({ mode: 'serial' });

let api: Api;
let deviceId = 0;
let sensorId = 0;
const sensorName = uid('Ping thr');

test.beforeAll(async () => {
  api = await apiAs();
  const d = await api.post('/api/devices', {
    group_id: await defaultGroupId(api),
    name: uid('thr-device'),
    host: '127.0.0.1',
    auto_sensors: [],
  });
  deviceId = d.id;
  const s = await api.post('/api/sensors', {
    device_id: deviceId,
    name: sensorName,
    type: 'ping',
    config: { count: 2 },
    interval_s: MIN_INTERVAL,
  });
  sensorId = s.id;
  await waitForSensor(api, sensorId, (x) => x.status === 'up', 45_000, 'ping up');
});

test('warning_max 0.001 on latency -> warning (live)', async ({ page }) => {
  await loginViaApi(page, undefined, `/sensors/${sensorId}`);
  await expandTreeDevice(page, deviceId);
  const header = page.getByRole('heading', { level: 1 });
  await expect(statusDot(header, 'Up')).toBeVisible();
  await markDocument(page);

  const thresholds = card(page, 'Thresholds');
  const wmax = thresholds.getByLabel('latency_ms Warning above');
  await expect(wmax).toHaveValue('100');
  await wmax.fill('0.001');
  await thresholds.getByRole('button', { name: 'Save thresholds' }).click();
  await expect(page.getByText('Saved', { exact: true })).toBeVisible();

  const s = await api.get(`/api/sensors/${sensorId}`);
  const lat = s.channels.find((c: any) => c.key === 'latency_ms');
  expect(lat).toMatchObject({ warning_max: 0.001, down_max: 500, warning_min: null, down_min: null });
  // Untouched channel keeps its defaults.
  expect(s.channels.find((c: any) => c.key === 'packet_loss_pct')).toMatchObject({ warning_max: 10, down_max: 50 });

  // Next scheduled check evaluates the new threshold.
  await expect(statusDot(header, 'Warning')).toBeVisible({ timeout: 40_000 });
  await expect(statusDot(treeSensor(page, sensorId), 'Warning')).toBeVisible();
  await expect(page.getByRole('button', { name: /Latency/ }).locator('[aria-label="Warning"]')).toBeVisible();
  await expectSameDocument(page);

  const w = await waitForSensor(api, sensorId, (x) => x.status === 'warning', 5_000);
  expect(w.channels.find((c: any) => c.key === 'latency_ms').status).toBe('warning');
  expect(w.channels.find((c: any) => c.key === 'packet_loss_pct').status).toBe('up');

  const ev = await api.get(`/api/events?sensor_id=${sensorId}&status=warning`);
  expect(ev.items[0]).toMatchObject({ kind: 'status_change', old_status: 'up', new_status: 'warning' });

  // Warning shows on the dashboard problem list.
  await page.getByRole('complementary').getByRole('link', { name: 'Dashboard' }).click();
  const problems = card(page, 'Down and warning sensors');
  await expect(problems.getByRole('row').filter({ hasText: sensorName })).toContainText('Warning');
});

test('restoring the default threshold -> up again', async ({ page }) => {
  await loginViaApi(page, undefined, `/sensors/${sensorId}`);
  const thresholds = card(page, 'Thresholds');
  const wmax = thresholds.getByLabel('latency_ms Warning above');
  await expect(wmax).toHaveValue('0.001');
  await wmax.fill('100');
  await thresholds.getByRole('button', { name: 'Save thresholds' }).click();
  await expect(page.getByText('Saved', { exact: true })).toBeVisible();
  await expect(statusDot(page.getByRole('heading', { level: 1 }), 'Up')).toBeVisible({ timeout: 40_000 });
});

test('invalid threshold input is rejected client-side', async ({ page }) => {
  await loginViaApi(page, undefined, `/sensors/${sensorId}`);
  const thresholds = card(page, 'Thresholds');
  await thresholds.getByLabel('latency_ms Down above').fill('abc');
  await thresholds.getByRole('button', { name: 'Save thresholds' }).click();
  await expect(thresholds.getByRole('alert')).toContainText('Enter a valid number.');
  await thresholds.getByRole('button', { name: 'Reset' }).click();
  await expect(thresholds.getByLabel('latency_ms Down above')).toHaveValue('500');
});
