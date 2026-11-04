import { expect, test } from '@playwright/test';
import {
  Api,
  HTTP_TARGET_URL,
  MIN_INTERVAL,
  WEBHOOK_URL,
  apiAs,
  card,
  checkboxByText,
  clearWebhookRecords,
  defaultGroupId,
  expandTreeDevice,
  expectSameDocument,
  httpTarget,
  loginViaApi,
  markDocument,
  statusDot,
  treeDevice,
  treeSensor,
  uid,
  waitForSensor,
  webhookRecords,
} from '../lib/helpers';

// Scenario (d): HTTP sensor on the toggle server; webhook channel + alert rule; failure -> sensor
// goes down live (WebSocket, no reload), event appears, webhook received; recovery -> up + webhook.
test.describe.configure({ mode: 'serial' });

let api: Api;
const deviceName = uid('web-target');
const sensorName = uid('Web E2E');
const channelName = uid('QA hook');
const ruleName = uid('Web down/up');
let deviceId = 0;
let sensorId = 0;
const targetPort = new URL(HTTP_TARGET_URL).port;

test.beforeAll(async () => {
  api = await apiAs();
  await httpTarget('ok');
  const d = await api.post('/api/devices', {
    group_id: await defaultGroupId(api),
    name: deviceName,
    host: '127.0.0.1',
    auto_sensors: [],
  });
  deviceId = d.id;
});

test.afterAll(async () => {
  await httpTarget('ok');
});

test('create an HTTP sensor against the toggle server', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${deviceId}`);
  await page.getByRole('button', { name: 'Add sensor' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel('Type').selectOption('http');
  await dlg.getByLabel(/^Name/).fill(sensorName);
  await dlg.getByLabel(/^Interval/).fill(String(MIN_INTERVAL));
  await dlg.getByRole('textbox', { name: /^URL/ }).fill(`http://{host}:${targetPort}/`);
  await dlg.getByRole('textbox', { name: /^Keyword/ }).fill('sim OK');
  await dlg.getByRole('button', { name: 'Test' }).click();
  await expect(dlg.getByText('Check succeeded')).toBeVisible();
  await dlg.getByRole('button', { name: 'Create sensor' }).click();
  await expect(dlg).toBeHidden();

  const list = await api.get(`/api/sensors?device_id=${deviceId}`);
  const s = list.items.find((x: any) => x.name === sensorName);
  expect(s).toBeTruthy();
  sensorId = s.id;
  expect(s.interval_s).toBe(MIN_INTERVAL);
  expect(s.config).toMatchObject({ url: `http://{host}:${targetPort}/`, keyword: 'sim OK', method: 'GET' });
  await waitForSensor(api, sensorId, (x) => x.status === 'up', 45_000, 'http sensor up');
});

test('create a webhook channel and send a test notification', async ({ page }) => {
  await clearWebhookRecords();
  await loginViaApi(page, undefined, '/notifications');
  await page.getByRole('button', { name: 'Add channel' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel(/^Name/).fill(channelName);
  await dlg.getByLabel('Type').selectOption('webhook');
  await dlg.getByLabel(/^URL/).fill(`${WEBHOOK_URL}/hook`);
  await dlg.getByLabel(/^Headers/).fill('X-Netmon-Test: e2e');
  await dlg.getByRole('button', { name: 'Save' }).click();
  await expect(dlg).toBeHidden();

  const row = card(page, 'Notification channels').getByRole('row').filter({ hasText: channelName });
  await expect(row).toBeVisible();
  await row.getByRole('button', { name: 'Send test' }).click();
  await expect(page.getByText(`${channelName}: test sent`)).toBeVisible();

  await expect.poll(async () => (await webhookRecords()).length, { timeout: 15_000 }).toBeGreaterThan(0);
  const rec = (await webhookRecords())[0];
  expect(rec.body.event).toBe('test');
  expect(rec.headers['Content-Type'] ?? rec.headers['content-type']).toContain('application/json');
  expect(rec.headers['User-Agent'] ?? rec.headers['user-agent']).toMatch(/^NetworkMonitor\//);
  expect(rec.headers['X-Netmon-Test'] ?? rec.headers['x-netmon-test']).toBe('e2e');
});

test('create an alert rule for the HTTP sensor (down + up)', async ({ page }) => {
  await loginViaApi(page, undefined, '/notifications');
  await page.getByRole('button', { name: 'Add rule' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel(/^Name/).fill(ruleName);
  const scope = dlg.getByLabel('Applies to');
  await expect(scope.locator('option', { hasText: sensorName })).toHaveCount(1);
  await scope.selectOption({ label: `${deviceName} / ${sensorName}` });

  // Defaults: Down + Up. Clicking one status checkbox must toggle only that one.
  const down = checkboxByText(dlg, 'Down');
  const warning = checkboxByText(dlg, 'Warning');
  const up = checkboxByText(dlg, 'Up');
  await expect(down).toBeChecked();
  await expect(up).toBeChecked();
  await expect(warning).not.toBeChecked();
  await warning.click();
  await expect(warning).toBeChecked();
  await expect(down).toBeChecked();
  await expect(up).toBeChecked();
  await warning.click();
  await expect(warning).not.toBeChecked();
  await expect(down).toBeChecked();

  await dlg.getByLabel(/^Delay/).fill('0');
  const ch = checkboxByText(dlg, `${channelName} (Webhook)`);
  await ch.click();
  await expect(ch).toBeChecked();
  await expect(down).toBeChecked();
  await dlg.getByRole('button', { name: 'Save' }).click();
  await expect(dlg).toBeHidden();

  const rules = await api.get(`/api/alert-rules?sensor_id=${sensorId}`);
  expect(rules.items).toHaveLength(1);
  expect(rules.items[0]).toMatchObject({
    name: ruleName,
    sensor_id: sensorId,
    sensor_name: sensorName,
    delay_s: 0,
    enabled: true,
  });
  expect([...rules.items[0].on_statuses].sort()).toEqual(['down', 'up']);
  expect(rules.items[0].channel_ids).toHaveLength(1);
  await expect(card(page, 'Alert rules').getByRole('row').filter({ hasText: ruleName })).toBeVisible();
});

test('target fails -> sensor goes down live, event + webhook; recovers -> up + webhook', async ({ page }) => {
  await clearWebhookRecords();
  await loginViaApi(page, undefined, `/sensors/${sensorId}`);
  await expandTreeDevice(page, deviceId);
  const header = page.getByRole('heading', { level: 1 });
  await expect(header).toContainText(sensorName);
  await expect(statusDot(header, 'Up')).toBeVisible();
  await expect(page.getByTestId('ws-indicator')).toContainText('Live');
  await markDocument(page);

  // --- failure
  await httpTarget('fail');
  await expect(statusDot(header, 'Down')).toBeVisible({ timeout: 40_000 });
  await expect(statusDot(treeSensor(page, sensorId), 'Down')).toBeVisible();
  await expect(statusDot(treeDevice(page, deviceId), 'Down')).toBeVisible(); // device.status rollup
  await expect(page.locator('main').getByText('Unexpected status code 503').first()).toBeVisible();
  const events = card(page, 'Events');
  await expect(events.getByRole('row').filter({ hasText: 'Down' }).first()).toBeVisible();
  await expectSameDocument(page);

  await expect
    .poll(async () => (await webhookRecords()).filter((r) => r.body?.new_status === 'down').length, {
      timeout: 20_000,
      message: 'webhook for down',
    })
    .toBe(1);
  const downHook = (await webhookRecords()).find((r) => r.body.new_status === 'down');
  expect(downHook.body).toMatchObject({
    event: 'sensor.status_changed',
    old_status: 'up',
    new_status: 'down',
    sensor: { id: sensorId, name: sensorName, type: 'http' },
    device: { id: deviceId, name: deviceName, host: '127.0.0.1' },
    rule: { name: ruleName },
  });
  expect(downHook.body.event_id).toEqual(expect.any(Number));
  expect(downHook.body.message).toContain('503');

  // The status_change event exists in the API as well.
  const ev = await api.get(`/api/events?sensor_id=${sensorId}&kind=status_change&status=down`);
  expect(ev.items.length).toBeGreaterThanOrEqual(1);
  expect(ev.items[0]).toMatchObject({ old_status: 'up', new_status: 'down', sensor_name: sensorName });

  // Dashboard problem list (client-side navigation, the WS keeps it fresh).
  await page.getByRole('complementary').getByRole('link', { name: 'Dashboard' }).click();
  await expect(card(page, 'Down and warning sensors').getByRole('link', { name: sensorName })).toBeVisible();
  await expectSameDocument(page);

  // --- recovery
  await httpTarget('ok');
  await expect(card(page, 'Down and warning sensors').getByRole('link', { name: sensorName })).toHaveCount(0, {
    timeout: 40_000,
  });
  await expect(statusDot(treeSensor(page, sensorId), 'Up')).toBeVisible();
  await expect(
    card(page, 'Recent events').getByRole('row').filter({ hasText: sensorName }).first(),
  ).toContainText('Up');
  await expectSameDocument(page);

  await expect
    .poll(async () => (await webhookRecords()).filter((r) => r.body?.new_status === 'up').length, {
      timeout: 20_000,
      message: 'webhook for recovery',
    })
    .toBe(1);
  const upHook = (await webhookRecords()).find((r) => r.body.new_status === 'up');
  expect(upHook.body).toMatchObject({ old_status: 'down', new_status: 'up', sensor: { id: sensorId } });
  // Exactly one notification per transition (one rule, one channel).
  expect((await webhookRecords()).length).toBe(2);
});
