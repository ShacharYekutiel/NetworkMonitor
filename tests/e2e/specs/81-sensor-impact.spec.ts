import { expect, test } from '@playwright/test';
import { Api, VIEWER, apiAs, loginViaApi, statusDot, treeDevice } from '../lib/helpers';
import { ensureViewer, raw } from '../lib/lab';
import { IP, T } from '../lib/lab2b2';
import { freshSwitch, shot13, waitDeviceStatus } from '../lib/lab13';

// [1.3] 64 — sensor impact drives the device status (api.md § "[1.3] Sensor impact", architecture.md § 14.1).
// Own device on the ArubaOS port sim 127.0.0.81: snmp_system (up) + an interface sensor on access port 6
// (oper down, not important -> impact "warning" by default) => the device is amber (warning), not red; impact
// critical => red (down); impact none => green. Bulk "Set impact…" from the device's sensor list.
test.describe.configure({ mode: 'serial' });

let admin: Api;
let dev: any;
let if6: any;
let sys: any;

test.beforeAll(async () => {
  admin = await apiAs();
  dev = await freshSwitch(admin, IP.aos, 'qa13-impact');
  sys = (await admin.get(`/api/sensors?device_id=${dev.id}&page_size=1000`)).items.find((s: any) => s.type === 'snmp_system');
  expect(sys, 'snmp_system sensor').toBeTruthy();
});

test.afterAll(async () => {
  if (dev) await raw(admin, 'DELETE', `/api/devices/${dev.id}`);
});

test('down interface sensor on a non-important port: impact warning by default -> device amber, not red', async ({ page }) => {
  if6 = await admin.post('/api/sensors', { device_id: dev.id, type: 'snmp_interface', name: 'qa13 port 6', interval_s: 10, config: { if_index: 6, if_name: '6' } });
  expect(if6.impact, 'snmp_interface on an access port -> warning').toBe('warning');
  // an explicit impact wins over the port importance
  const ex = await admin.post('/api/sensors', { device_id: dev.id, type: 'snmp_interface', name: 'qa13 port 8', interval_s: 10, impact: 'none', config: { if_index: 8, if_name: '8' } });
  expect(ex.impact).toBe('none');
  await raw(admin, 'DELETE', `/api/sensors/${ex.id}`);
  await expect.poll(async () => (await admin.get(`/api/sensors/${if6.id}`)).status, { timeout: 60_000, message: 'port 6 sensor down' }).toBe('down');
  await expect.poll(async () => (await admin.get(`/api/sensors/${sys.id}`)).status, { timeout: 60_000, message: 'system sensor up' }).toBe('up');
  const d = await waitDeviceStatus(admin, dev.id, 'warning');
  // sensor counters keep counting raw sensor statuses
  expect.soft(JSON.stringify(d.sensor_counts ?? d.status_counts ?? {}), 'raw counts keep the down sensor').toMatch(/"down":1/);
  const tree = await admin.get('/api/tree');
  expect(JSON.stringify(tree)).toContain('"impact":"warning"');

  await loginViaApi(page, undefined, `/devices/${dev.id}?tab=sensors`);
  await expect(statusDot(page.getByRole('heading', { level: 1 }), 'Warning')).toBeVisible({ timeout: 30_000 });
  const sensorRow = page.getByRole('row').filter({ has: page.getByRole('link', { name: 'qa13 port 6' }) });
  await expect(sensorRow.getByTestId('impact-tag')).toHaveText(T('impact.tag.warning'));
  await expect(sensorRow.getByRole('cell', { name: 'Down', exact: true })).toBeVisible(); // the sensor itself stays down
  await expect(statusDot(treeDevice(page, dev.id), 'Warning')).toBeVisible();
  await shot13(page, '64-impact-warning-device-amber');
});

test('same sensor with impact critical (sensor form) -> device red (down)', async ({ page }) => {
  await loginViaApi(page, undefined, `/sensors/${if6.id}`);
  await page.getByRole('button', { name: T('common.edit'), exact: true }).first().click();
  const field = page.getByTestId('impact-field');
  await expect(field).toBeVisible();
  await expect(field.getByRole('radio', { name: T('impact.option.warning') })).toBeChecked();
  await field.getByRole('radio', { name: T('impact.option.critical') }).check();
  await page.getByRole('dialog').getByRole('button', { name: T('common.save'), exact: true }).click();
  await expect.poll(async () => (await admin.get(`/api/sensors/${if6.id}`)).impact, { timeout: 10_000 }).toBe('critical');
  await waitDeviceStatus(admin, dev.id, 'down');
  await page.goto(`/devices/${dev.id}?tab=sensors`);
  await expect(statusDot(page.getByRole('heading', { level: 1 }), 'Down')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole('row').filter({ has: page.getByRole('link', { name: 'qa13 port 6' }) }).getByTestId('impact-tag')).toHaveCount(0);
  await shot13(page, '64-impact-critical-device-red');
});

test('bulk "Set impact…" on the device sensor list -> none -> device up; audited per changed sensor', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${dev.id}?tab=sensors`);
  const rowOf = (name: string) => page.getByRole('row').filter({ has: page.getByRole('link', { name }) });
  await expect(page.getByTestId('sensor-bulk-bar')).toHaveCount(0);
  // (the phase-3 "New devices" sensor may also be on the switch when that feature is ON: count the list)
  const n = (await admin.get(`/api/sensors?device_id=${dev.id}&page_size=1000`)).items.length;
  await page.getByTestId('sensor-select-all').check();
  const bar = page.getByTestId('sensor-bulk-bar');
  await expect(bar).toContainText(T('impact.selected', { count: n }));
  await bar.getByRole('button', { name: T('impact.clearSelection') }).click();
  await expect(bar).toHaveCount(0);
  await expect(page.getByTestId('sensor-select-all')).not.toBeChecked();
  // only the interface sensor
  await rowOf('qa13 port 6').getByTestId('sensor-select').check();
  await expect(bar).toContainText(T('impact.selected', { count: 1 }));
  await bar.getByRole('button', { name: T('impact.setImpact') }).click();
  const modal = page.getByTestId('bulk-impact');
  await expect(modal).toBeVisible();
  await expect(modal).toContainText(T('impact.bulkTitle', { count: 1 }));
  await modal.getByRole('radio', { name: T('impact.option.none') }).check();
  await shot13(page, '64-bulk-impact-dialog');
  await modal.getByRole('button', { name: T('impact.apply') }).click();
  await expect(page.getByText(T('impact.bulkDone', { count: 1 }))).toBeVisible();
  await expect(rowOf('qa13 port 6').getByTestId('impact-tag')).toHaveText(T('impact.tag.none'));
  await expect(page.getByTestId('sensor-bulk-bar')).toHaveCount(0);
  expect((await admin.get(`/api/sensors/${if6.id}`)).impact).toBe('none');
  expect((await admin.get(`/api/sensors/${sys.id}`)).impact).toBe('critical');
  await waitDeviceStatus(admin, dev.id, 'up');
  await expect(statusDot(page.getByRole('heading', { level: 1 }), 'Up')).toBeVisible({ timeout: 30_000 });
  await shot13(page, '64-bulk-impact-none-device-up');
});

test('API: bulk-impact counts only real changes; unknown id -> 422 and nothing changed; viewer forbidden', async () => {
  const same = await admin.post('/api/sensors/bulk-impact', { sensor_ids: [if6.id, sys.id], impact: 'critical' });
  expect(same).toEqual({ updated: 1 }); // sys already critical
  const bad = await raw(admin, 'POST', '/api/sensors/bulk-impact', { sensor_ids: [if6.id, 99999999], impact: 'warning' });
  expect(bad.status).toBe(422);
  expect((await admin.get(`/api/sensors/${if6.id}`)).impact).toBe('critical');
  expect((await raw(admin, 'POST', '/api/sensors/bulk-impact', { sensor_ids: [if6.id], impact: 'major' })).status).toBe(422);
  expect((await raw(admin, 'POST', '/api/sensors/bulk-impact', { sensor_ids: [], impact: 'none' })).status).toBe(422);
  await ensureViewer(admin);
  const viewer = await apiAs(VIEWER);
  expect((await raw(viewer, 'POST', '/api/sensors/bulk-impact', { sensor_ids: [if6.id], impact: 'none' })).status).toBe(403);
  await waitDeviceStatus(admin, dev.id, 'down');
  const audit = await raw(admin, 'GET', `/api/audit?action=sensor.update&entity_id=${if6.id}&page_size=5`);
  if (audit.status === 200) expect.soft(audit.body.items[0]?.changes?.impact).toEqual(['none', 'critical']);
});
