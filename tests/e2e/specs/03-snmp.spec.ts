import { expect, test, type Page } from '@playwright/test';
import {
  Api,
  MIN_INTERVAL,
  SNMP_PORT,
  apiAs,
  card,
  defaultGroupId,
  loginViaApi,
  shot,
  uid,
  waitForSensor,
} from '../lib/helpers';

// Scenario (c): SNMP device pointing at the simulator; enumerate interfaces via the picker; add
// interface + CPU + memory sensors; values appear.
test.describe.configure({ mode: 'serial' });

let api: Api;
const deviceName = uid('snmp-sim');
let deviceId = 0;

test.beforeAll(async () => {
  api = await apiAs();
});

async function openAddSensor(page: Page) {
  await page.getByRole('button', { name: 'Add sensor' }).click();
  const dlg = page.getByRole('dialog');
  await expect(dlg.getByLabel('Type')).toBeVisible();
  return dlg;
}

test('add an SNMP v2c device with device-level credentials', async ({ page }) => {
  const groupId = await defaultGroupId(api);
  await loginViaApi(page, undefined, `/groups/${groupId}`);
  await page.getByRole('button', { name: 'Add device' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel(/^Name/).fill(deviceName);
  await dlg.getByLabel(/^Host/).fill('127.0.0.1');
  await dlg.getByRole('checkbox', { name: 'Inherit from the group' }).uncheck();
  await expect(dlg.getByLabel('Version')).toHaveValue('v2c');
  await dlg.getByLabel('Port', { exact: true }).fill(String(SNMP_PORT));
  await dlg.getByLabel('Timeout (s)').fill('1');
  await dlg.getByLabel('Retries').fill('1');
  await dlg.getByLabel('Community').fill('public');
  // No ping for this device; snmp_system as an auto sensor.
  await dlg.getByRole('checkbox', { name: 'Ping' }).uncheck();
  await dlg.getByRole('checkbox', { name: 'SNMP System' }).check();
  await dlg.getByRole('button', { name: 'Save' }).click();

  await expect(page).toHaveURL(/\/devices\/\d+$/);
  deviceId = Number(page.url().split('/').pop());
  await expect(page.getByText(/SNMP: set on the device \(v2c\)/)).toBeVisible();
  const d = await api.get(`/api/devices/${deviceId}`);
  expect(d.effective_snmp_source).toBe('device');
  expect(d.snmp_credentials).toMatchObject({ version: 'v2c', port: SNMP_PORT, community: 'public' });
});

test('add SNMP CPU and memory sensors (with a test run first)', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${deviceId}`);

  let dlg = await openAddSensor(page);
  await dlg.getByLabel('Type').selectOption('snmp_cpu');
  await expect(dlg.getByText(/This sensor uses SNMP\. Credentials: set on the device/)).toBeVisible();
  await expect(dlg.getByLabel(/^Name/)).toHaveValue('SNMP CPU');
  await dlg.getByLabel(/^Interval/).fill(String(MIN_INTERVAL));
  await dlg.getByRole('button', { name: 'Test' }).click();
  await expect(dlg.getByText('Check succeeded')).toBeVisible();
  await expect(dlg).toContainText('30%'); // avg of hrProcessorLoad 20 and 40
  await dlg.getByRole('button', { name: 'Create sensor' }).click();
  await expect(dlg).toBeHidden();
  await expect(page.getByText('Sensor "SNMP CPU" created')).toBeVisible();

  dlg = await openAddSensor(page);
  await dlg.getByLabel('Type').selectOption('snmp_memory');
  await expect(dlg.getByLabel(/^Name/)).toHaveValue('SNMP Memory');
  await dlg.getByLabel(/^Interval/).fill(String(MIN_INTERVAL));
  await dlg.getByRole('button', { name: 'Create sensor' }).click();
  await expect(dlg).toBeHidden();

  const sensors = card(page, 'Sensors');
  await expect(sensors.getByRole('link', { name: 'SNMP CPU' })).toBeVisible();
  await expect(sensors.getByRole('link', { name: 'SNMP Memory' })).toBeVisible();
});

test('enumerate interfaces in the picker and add eth0 + eth1', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${deviceId}`);
  const dlg = await openAddSensor(page);
  await dlg.getByLabel('Type').selectOption('snmp_interface');

  const table = dlg.getByRole('table');
  await expect(table.getByRole('row')).toHaveCount(4); // header + lo, eth0, eth1
  const eth0 = table.getByRole('row').filter({ hasText: 'eth0' });
  await expect(eth0).toContainText('uplink');
  await expect(eth0).toContainText('1 Gbit/s');
  await expect(eth0).toContainText('up/up');
  const eth1 = table.getByRole('row').filter({ hasText: 'eth1' });
  await expect(eth1).toContainText('backup link');
  await expect(eth1).toContainText('up/down');

  // The filter narrows the list.
  await dlg.getByPlaceholder('Filter…').fill('link');
  await expect(table.getByRole('row')).toHaveCount(3);
  await dlg.getByPlaceholder('Filter…').fill('');

  await dlg.getByRole('checkbox', { name: 'eth0' }).check();
  await dlg.getByRole('checkbox', { name: 'eth1' }).check();
  await expect(dlg.getByText('Selected: 2')).toBeVisible();
  await shot(page, 'snmp-interface-picker');
  await dlg.getByRole('button', { name: 'Add selected (2)' }).click();
  await expect(dlg).toBeHidden();
  await expect(page.getByText('Interface sensors added: 2, failed: 0')).toBeVisible();

  const list = await api.get(`/api/sensors?device_id=${deviceId}&type=snmp_interface`);
  const byName = Object.fromEntries(list.items.map((s: any) => [s.name, s]));
  expect(Object.keys(byName).sort()).toEqual(['eth0', 'eth1']);
  expect(byName.eth0.config).toMatchObject({ if_index: 2, if_name: 'eth0' });
  expect(byName.eth1.config).toMatchObject({ if_index: 3, if_name: 'eth1' });

  // Re-opening the picker marks them as already monitored.
  const dlg2 = await openAddSensor(page);
  await dlg2.getByLabel('Type').selectOption('snmp_interface');
  await expect(dlg2.getByRole('row').filter({ hasText: 'eth0' })).toContainText('already monitored');
  await expect(dlg2.getByRole('checkbox', { name: 'eth0' })).toBeDisabled();
  await dlg2.getByRole('button', { name: 'Cancel' }).click();
});

test('SNMP values appear (system, CPU, memory, interface traffic)', async ({ page }) => {
  const list = await api.get(`/api/sensors?device_id=${deviceId}`);
  const by = (type: string, name?: string) =>
    list.items.find((s: any) => s.type === type && (!name || s.name === name));
  const sys = by('snmp_system');
  const cpu = by('snmp_cpu');
  const mem = by('snmp_memory');
  const eth0 = by('snmp_interface', 'eth0');
  const eth1 = by('snmp_interface', 'eth1');
  for (const s of [sys, eth0, eth1]) {
    // The picker has no interval field, so speed these up through the API (precondition only).
    await api.patch(`/api/sensors/${s.id}`, { interval_s: MIN_INTERVAL });
  }

  const v = (s: any, k: string) => s.channels.find((c: any) => c.key === k)?.last_value;
  const c = await waitForSensor(api, cpu.id, (x) => x.status === 'up', 45_000, 'cpu up');
  expect(v(c, 'cpu_load_pct')).toBeCloseTo(30, 5);
  const m = await waitForSensor(api, mem.id, (x) => x.status === 'up', 45_000, 'memory up');
  expect(v(m, 'mem_used_pct')).toBeCloseTo(62.5, 5);
  expect(v(m, 'mem_total_bytes')).toBe(8000000 * 1024);
  expect(m.config.storage_descr).toBe('Physical memory');
  const sy = await waitForSensor(api, sys.id, (x) => x.status === 'up', 45_000, 'system up');
  expect(sy.status_message).toContain('netmon-sim');
  expect(v(sy, 'uptime_s')).toBeGreaterThan(12000);

  // Interface rates need two polls (the first one stores the counter baseline).
  const e0 = await waitForSensor(api, eth0.id, (x) => (v(x, 'in_bps') ?? 0) > 0, 60_000, 'eth0 in_bps');
  // Simulator: in +1.25 MB/s (10 Mbit/s), out +125 kB/s (1 Mbit/s), 1 error/s, speed 1 Gbit/s.
  expect(v(e0, 'in_bps')).toBeGreaterThan(8e6);
  expect(v(e0, 'in_bps')).toBeLessThan(12e6);
  expect(v(e0, 'out_bps')).toBeGreaterThan(0.8e6);
  expect(v(e0, 'out_bps')).toBeLessThan(1.2e6);
  expect(v(e0, 'in_util_pct')).toBeGreaterThan(0.8);
  expect(v(e0, 'in_util_pct')).toBeLessThan(1.2);
  expect(v(e0, 'oper_status')).toBe(1);
  expect(e0.status).toBe('up');
  // eth1 is oper down -> oper_status = 2 > down_max 1 -> the sensor is down.
  const e1 = await waitForSensor(api, eth1.id, (x) => x.status === 'down', 45_000, 'eth1 down');
  expect(v(e1, 'oper_status')).toBe(2);

  // The UI shows the values.
  await loginViaApi(page, undefined, `/devices/${deviceId}`);
  const sensors = card(page, 'Sensors');
  await expect(sensors.getByRole('row').filter({ hasText: 'SNMP CPU' })).toContainText('30%');
  await expect(sensors.getByRole('row').filter({ hasText: 'SNMP Memory' })).toContainText('62.5%');
  await expect(sensors.getByRole('row').filter({ hasText: 'eth0' })).toContainText(/\d+(\.\d+)? Mbit\/s/);
  await expect(sensors.getByRole('row').filter({ hasText: 'eth1' })).toContainText('Down');

  await page.getByRole('link', { name: 'eth0' }).first().click();
  await expect(page.getByRole('heading', { level: 1, name: /eth0/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /Traffic in/ })).toContainText('Mbit/s');
  await expect(page.getByRole('button', { name: /Operational status/ })).toContainText('1');
  await expect(page.getByText('if_index').or(page.getByText('Interface index:'))).toBeVisible();
});
