import { expect, test, type Page } from '@playwright/test';
import { Api, MIN_INTERVAL, apiAs, card, defaultGroupId, loginViaApi, uid } from '../lib/helpers';
import { raw, snmpCreds } from '../lib/lab';

// Phase 2a (d): manual snmp_custom sensors (gauge + string) and snmp_disk through the storage picker, against
// the OPNsense sim (127.0.0.21:1161). data-model.md § snmp_custom / snmp_disk.
test.describe.configure({ mode: 'serial' });

let api: Api;
let deviceId = 0;
const deviceName = uid('custom-oid');

test.beforeAll(async () => {
  api = await apiAs();
  deviceId = (
    await api.post('/api/devices', {
      group_id: await defaultGroupId(api),
      name: deviceName,
      host: '127.0.0.21',
      snmp_credentials: snmpCreds(),
      auto_sensors: [],
    })
  ).id;
});

async function openAddSensor(page: Page, type: string) {
  await page.getByRole('button', { name: 'Add sensor' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByRole('combobox', { name: 'Type', exact: true }).selectOption(type);
  return dlg;
}
async function sensorByName(name: string) {
  const list = (await api.get(`/api/sensors?device_id=${deviceId}&page_size=1000`)).items;
  return list.find((s: any) => s.name === name);
}

test('sensor types list contains snmp_custom and snmp_disk', async () => {
  const types = (await api.get('/api/sensor-types')).items;
  const custom = types.find((t: any) => t.type === 'snmp_custom');
  const disk = types.find((t: any) => t.type === 'snmp_disk');
  expect(custom).toMatchObject({ requires_snmp: true, primary_channel: 'value' });
  expect(custom.config_fields.find((f: any) => f.key === 'multiplier')).toMatchObject({ type: 'float', default: 1 });
  expect(custom.config_fields.find((f: any) => f.key === 'value_type').options).toEqual(['gauge', 'counter', 'string', 'enum']); // [2b-1] enum
  expect(disk).toMatchObject({ requires_snmp: true, primary_channel: 'disk_used_pct' });
  expect(disk.channels.map((c: any) => c.key)).toEqual(['disk_used_pct', 'disk_used_bytes', 'disk_free_bytes', 'disk_total_bytes']);
});

test('custom OID, gauge: hrProcessorLoad.196609 = 30 %', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${deviceId}`);
  const dlg = await openAddSensor(page, 'snmp_custom');
  await expect(dlg.getByRole('combobox', { name: 'Type', exact: true }).locator('option:checked')).toHaveText('Custom SNMP OID');
  await dlg.getByLabel(/^Name/).fill('Core 1 load');
  await dlg.getByLabel(/^Interval/).fill(String(MIN_INTERVAL));
  await dlg.getByLabel(/^OID/).fill('.1.3.6.1.2.1.25.3.3.1.2.196609');
  await expect(dlg.getByLabel(/^Value type/)).toHaveValue('gauge');
  // Gauge shows "percent of OID", hides expected/match.
  await expect(dlg.getByLabel(/^Percent of OID/)).toBeVisible();
  await expect(dlg.getByLabel(/^Expected/)).toHaveCount(0);
  await expect(dlg.getByLabel(/^Multiplier/)).toHaveAttribute('step', 'any');
  await dlg.getByLabel(/^Unit/).fill('%');
  await dlg.getByLabel(/^Label/).fill('Core 1');
  await dlg.getByRole('button', { name: 'Test' }).click();
  await expect(dlg.getByText('Check succeeded')).toBeVisible();
  await expect(dlg).toContainText('30');
  await dlg.getByRole('button', { name: 'Create sensor' }).click();
  await expect(dlg).toBeHidden();

  const s = await sensorByName('Core 1 load');
  expect(s).toMatchObject({ type: 'snmp_custom', interval_s: MIN_INTERVAL });
  expect(s.config).toMatchObject({ oid: '1.3.6.1.2.1.25.3.3.1.2.196609', value_type: 'gauge', unit: '%', label: 'Core 1', multiplier: 1 });
  expect(s.channels[0]).toMatchObject({ key: 'value', unit: '%' });
  const r = await api.post(`/api/sensors/${s.id}/run`);
  expect(r).toMatchObject({ ok: true, status: 'up' });
  expect(r.channels[0].value).toBe(30);
  expect(r.message).toMatch(/^Core 1: 30/);

  // A unit change through PATCH updates the channel unit; multiplier is applied to gauges.
  const p = await api.patch(`/api/sensors/${s.id}`, { config: { unit: 'pct', multiplier: 0.5 } });
  expect(p.channels[0].unit).toBe('pct');
  expect((await api.post(`/api/sensors/${s.id}/run`)).channels[0].value).toBe(15);
});

test('custom OID, string: sysName contains "OPNsense"', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${deviceId}`);
  const dlg = await openAddSensor(page, 'snmp_custom');
  await dlg.getByLabel(/^Name/).fill('Hostname check');
  await dlg.getByLabel(/^OID/).fill('1.3.6.1.2.1.1.5.0');
  await dlg.getByLabel(/^Value type/).selectOption('string');
  await expect(dlg.getByLabel(/^Percent of OID/)).toHaveCount(0);
  await dlg.getByLabel(/^Expected/).fill('OPNsense');
  await dlg.getByLabel(/^Match/).selectOption('contains');
  await dlg.getByLabel(/^Label/).fill('Hostname');
  await dlg.getByRole('button', { name: 'Test' }).click();
  await expect(dlg.getByText('Check succeeded')).toBeVisible();
  await expect(dlg).toContainText('OPNsense.localdomain');
  await dlg.getByRole('button', { name: 'Create sensor' }).click();
  await expect(dlg).toBeHidden();
  await expect(card(page, 'Sensors').getByRole('link', { name: 'Hostname check' })).toBeVisible();

  const s = await sensorByName('Hostname check');
  expect(s.config).toMatchObject({ oid: '1.3.6.1.2.1.1.5.0', value_type: 'string', expected: 'OPNsense', match: 'contains' });
  const r = await api.post(`/api/sensors/${s.id}/run`);
  expect(r).toMatchObject({ ok: true, status: 'up', message: 'Hostname: OPNsense.localdomain' });
  expect(r.channels[0].value).toBe(1);

  // Mismatch with "equals" -> down with the documented message.
  const p = await api.patch(`/api/sensors/${s.id}`, { config: { expected: 'OPNsense', match: 'equals' } });
  expect(p.config.match).toBe('equals');
  const down = await api.post(`/api/sensors/${s.id}/run`);
  expect(down).toMatchObject({ ok: false, status: 'down' });
  expect(down.message).toBe("Hostname: 'OPNsense.localdomain' does not match 'OPNsense'");
});

test('custom OID: counter rate, missing OID, validation', async () => {
  const counter = await api.post('/api/sensors', {
    device_id: deviceId,
    type: 'snmp_custom',
    config: { oid: '1.3.6.1.2.1.31.1.1.1.6.1', value_type: 'counter', multiplier: 8, unit: 'bit/s' },
  });
  expect(counter.name).toBe('SNMP 1.3.6.1.2.1.31.1.1.1.6.1'); // default name without a label
  const first = await api.post(`/api/sensors/${counter.id}/run`);
  expect(first.ok).toBe(true);
  expect(first.channels[0].value).toBeNull(); // baseline only
  await new Promise((r) => setTimeout(r, 2000));
  const second = await api.post(`/api/sensors/${counter.id}/run`);
  expect(second.channels[0].value).toBeGreaterThan(15_000_000); // igb0 ~2.5 MB/s * 8
  expect(second.channels[0].value).toBeLessThan(25_000_000);

  const missing = await api.post('/api/sensors/test', {
    device_id: deviceId,
    type: 'snmp_custom',
    config: { oid: '1.3.6.1.4.1.99999.1.0' },
  });
  expect(missing).toMatchObject({ ok: false, message: 'OID not found: 1.3.6.1.4.1.99999.1.0' });
  const notNumeric = await api.post('/api/sensors/test', {
    device_id: deviceId,
    type: 'snmp_custom',
    config: { oid: '1.3.6.1.2.1.1.5.0', value_type: 'gauge' },
  });
  expect(notNumeric.ok).toBe(false);
  expect(notNumeric.message).toBe("Value is not numeric: 'OPNsense.localdomain'");

  for (const config of [
    { oid: 'sysName.0' },
    { oid: '1.3.6.1.2.1.1.5.0', value_type: 'string', percent_of_oid: '1.3.6.1.2.1.1.5.0' },
    { oid: '1.3.6.1.2.1.1.5.0', multiplier: 0 },
    { oid: '1.3.6.1.2.1.1.5.0', unit: 'x'.repeat(17) },
  ]) {
    const r = await raw(api, 'POST', '/api/sensors', { device_id: deviceId, type: 'snmp_custom', config });
    expect.soft(r.status, JSON.stringify(config)).toBe(422);
  }
});

test('storage picker: pick "/" -> snmp_disk sensor with usage values', async ({ page }) => {
  const storage = await api.get(`/api/devices/${deviceId}/snmp/storage`);
  expect(storage.items.map((x: any) => [x.index, x.descr, x.type])).toEqual([
    [1, 'Physical memory', 'ram'],
    [10, 'Swap space', 'virtual_memory'],
    [31, '/', 'fixed_disk'],
    [32, '/boot/efi', 'fixed_disk'],
  ]);
  expect(storage.items[2]).toMatchObject({ size_bytes: 28_000_000 * 4096, used_bytes: 7_000_000 * 4096, existing_sensor_id: null });

  await loginViaApi(page, undefined, `/devices/${deviceId}`);
  const dlg = await openAddSensor(page, 'snmp_disk');
  await expect(dlg.getByRole('combobox', { name: 'Type', exact: true }).locator('option:checked')).toHaveText('Disk usage');
  // The add form opens the storage list right away ("Pick from device" only when collapsed).
  const pick = dlg.getByRole('button', { name: 'Pick from device' });
  if (await pick.isVisible().catch(() => false)) await pick.click();
  const picker = dlg.getByRole('table');
  await expect(picker.getByRole('row').filter({ hasText: '/boot/efi' })).toBeVisible();
  // RAM / virtual memory rows are hidden; fixed disks are preselected.
  await expect(picker.getByRole('row').filter({ hasText: 'Physical memory' })).toHaveCount(0);
  await expect(picker.getByRole('row').filter({ hasText: 'Swap space' })).toHaveCount(0);
  await expect(picker.getByRole('checkbox', { name: '/', exact: true })).toBeChecked();
  await expect(picker.getByRole('checkbox', { name: '/boot/efi' })).toBeChecked();
  await expect(picker.getByRole('row').filter({ hasText: '/boot/efi' })).toContainText('Fixed disk');
  await picker.getByRole('checkbox', { name: '/boot/efi' }).uncheck();
  await dlg.getByRole('button', { name: 'Add selected (1)' }).click();
  await expect(dlg).toBeHidden();
  await expect(page.getByText('Disk sensors added: 1, failed: 0')).toBeVisible();

  const s = await sensorByName('Disk /');
  expect(s).toMatchObject({ type: 'snmp_disk' });
  expect(s.config.storage_descr).toBe('/');
  const r = await api.post(`/api/sensors/${s.id}/run`);
  expect(r).toMatchObject({ ok: true, status: 'up' });
  expect(r.channels.find((c: any) => c.key === 'disk_used_pct').value).toBe(25);
  expect((await api.get(`/api/sensors/${s.id}`)).config.storage_index).toBe(31);
  // The picker now marks "/" as monitored.
  const again = await api.get(`/api/devices/${deviceId}/snmp/storage`);
  expect(again.items.find((x: any) => x.descr === '/').existing_sensor_id).toBe(s.id);
});

test('snmp_disk: exact match only ("/boot" is not "/boot/efi"), RAM never matches', async () => {
  const boot = await api.post('/api/sensors/test', { device_id: deviceId, type: 'snmp_disk', config: { storage_descr: '/boot' } });
  expect(boot).toMatchObject({ ok: false, message: "Storage '/boot' not found" });
  const ram = await api.post('/api/sensors/test', { device_id: deviceId, type: 'snmp_disk', config: { storage_descr: 'Physical memory' } });
  expect(ram).toMatchObject({ ok: false, message: "Storage 'Physical memory' not found" });
  const efi = await api.post('/api/sensors/test', { device_id: deviceId, type: 'snmp_disk', config: { storage_descr: '/boot/efi' } });
  expect(efi.ok).toBe(true);
  expect(efi.channels.find((c: any) => c.key === 'disk_used_pct').value).toBeCloseTo(0.38, 2);
  // Switch sim without HOST-RESOURCES-MIB: picker list is empty (not an error).
});
