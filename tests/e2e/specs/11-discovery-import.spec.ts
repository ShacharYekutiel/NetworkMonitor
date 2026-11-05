import { expect, test } from '@playwright/test';
import { Api, apiAs, defaultGroupId, loginViaApi, uid } from '../lib/helpers';
import {
  DISCOVERY_SNMP_PORT,
  LAB_RANGE,
  LAB_SCAN_OPTIONS,
  ensureGroup,
  findDeviceByHost,
  raw,
  scanResults,
  startScan,
  waitForScan,
} from '../lib/lab';

// Phase 2a (b): import selected lab hosts with their templates into a group -> devices and the documented
// sensors are created (architecture.md §9.6, data-model.md § Built-in templates), and the sensors get data:
// UniFi clients per VAP (snmp_custom via snmp_table), printer toner %, OPNsense disk (snmp_disk), interfaces.
test.describe.configure({ mode: 'serial' });

let api: Api;
let scanId = 0;
let groupId = 0;
const groupName = 'Lab import';
const apName = uid('AP Office');
const results: Record<string, any> = {};
const devices: Record<string, any> = {};

const EXPECTED_SENSORS: Record<string, { template: string; names: string[]; skipped: Record<string, string> }> = {
  '127.0.0.21': {
    template: 'opnsense',
    names: ['CPU Load', 'Disk /', 'Memory', 'Ping', 'System', 'igb0', 'igb1'],
    skipped: { 'Web GUI': 'probe_failed' }, // nothing listens on https://127.0.0.21/
  },
  '127.0.0.22': {
    template: 'unifi_ap',
    names: ['CPU Load', 'Clients HomeWiFi (na)', 'Clients HomeWiFi (ng)', 'Memory', 'Ping', 'System', 'eth0'],
    skipped: {},
  },
  '127.0.0.23': {
    template: 'printer',
    names: ['Page counter', 'Ping', 'Supply Black Toner Cartridge', 'Supply Drum Unit', 'System'],
    skipped: { HTTP: 'probe_failed' },
  },
  '127.0.0.25': {
    template: 'http_server',
    names: ['HTTP 8080', 'Ping'],
    skipped: { HTTP: 'probe_failed', HTTPS: 'probe_failed', 'HTTPS 8443': 'probe_failed' },
  },
};

test.beforeAll(async () => {
  api = await apiAs();
  groupId = await ensureGroup(api, groupName);
  // 127.0.0.24 exists already (spec 10 or here), so it is not importable.
  if (!(await findDeviceByHost(api, '127.0.0.24'))) {
    await api.post('/api/devices', { group_id: await defaultGroupId(api), name: uid('existing-switch'), host: '127.0.0.24', auto_sensors: [] });
  }
  for (const ip of Object.keys(EXPECTED_SENSORS)) {
    expect(await findDeviceByHost(api, ip), `precondition: no device ${ip} yet`).toBeNull();
  }
  const s = await startScan(api, { targets: [LAB_RANGE], group_id: await defaultGroupId(api), options: LAB_SCAN_OPTIONS });
  scanId = s.id;
  await waitForScan(api, scanId);
  for (const r of await scanResults(api, scanId)) results[r.ip] = r;
});

test('import 4 selected hosts into a group from the results page', async ({ page }) => {
  await loginViaApi(page, undefined, `/discovery/scans/${scanId}`);
  const table = page.getByRole('table');
  const row = (ip: string) => table.getByRole('row').filter({ hasText: ip });
  await expect(row('127.0.0.25')).toBeVisible();
  for (const ip of Object.keys(EXPECTED_SENSORS)) {
    await row(ip).getByRole('checkbox', { name: `Select ${ip}` }).check();
    await expect(row(ip).getByRole('combobox', { name: `Template for ${ip}` })).toHaveValue(EXPECTED_SENSORS[ip].template);
  }
  // Editable name for the AP.
  await row('127.0.0.22').getByRole('textbox', { name: 'Device name for 127.0.0.22' }).fill(apName);

  await page.getByLabel('Import into group').selectOption({ label: groupName });
  await page.getByRole('button', { name: 'Import selected (4)' }).click();
  const dlg = page.getByRole('dialog');
  await expect(dlg).toBeVisible({ timeout: 120_000 });
  for (const name of ['OPNsense.localdomain', apName, 'BRN3C2AF4', '127.0.0.25']) await expect(dlg).toContainText(name);
  // Skipped probes are listed with a translated reason (no raw enum values).
  await expect(dlg).toContainText('Web GUI');
  await expect(dlg).not.toContainText('probe_failed');
  await dlg.getByRole('button', { name: 'Close' }).last().click();

  // Imported rows now point to their devices.
  await expect(row('127.0.0.21').getByRole('link', { name: /Exists|Imported/ })).toBeVisible();
});

test('API: devices with the documented sensors, names and credentials', async () => {
  for (const [ip, exp] of Object.entries(EXPECTED_SENSORS)) {
    const d = await findDeviceByHost(api, ip);
    expect(d, `device ${ip}`).toBeTruthy();
    devices[ip] = d;
    expect.soft(d.group_id, ip).toBe(groupId);
    expect.soft(d.enabled, ip).toBe(true);
    const sensors = (await api.get(`/api/sensors?device_id=${d.id}&page_size=1000`)).items;
    expect.soft(sensors.map((s: any) => s.name).sort(), `${ip} sensors`).toEqual([...exp.names].sort());
  }
  expect(devices['127.0.0.21'].name).toBe('OPNsense.localdomain'); // name = sys_name
  expect(devices['127.0.0.21'].description).toContain('FreeBSD OPNsense.localdomain');
  expect(devices['127.0.0.22'].name).toBe(apName);
  expect(devices['127.0.0.25'].name).toBe('127.0.0.25'); // no sys_name/hostname -> the IP

  // Credentials: group credential + snmp_port override 1161 != group port 161 -> own credentials;
  // the printer answered an extra community -> own credentials; no SNMP -> inherit.
  for (const ip of ['127.0.0.21', '127.0.0.22', '127.0.0.23']) {
    expect.soft(devices[ip].snmp_credentials, ip).toMatchObject({
      version: 'v2c',
      port: DISCOVERY_SNMP_PORT,
      timeout_s: 2,
      retries: 1,
      community: null,
      community_set: true,
    });
    expect.soft(devices[ip].effective_snmp_source, ip).toBe('device');
  }
  expect(devices['127.0.0.25'].snmp_credentials).toBeNull();

  // Results remember the import and are now "existing".
  for (const r of await scanResults(api, scanId)) {
    if (EXPECTED_SENSORS[r.ip]) {
      expect.soft(r.imported_device_id, r.ip).toBe(devices[r.ip].id);
      expect.soft(r.existing_device_id, r.ip).toBe(devices[r.ip].id);
    }
  }
});

test('API: importing again -> device_exists per item; bad template -> 422', async () => {
  const again = await api.post(`/api/discovery/scans/${scanId}/import`, {
    group_id: groupId,
    items: [{ result_id: results['127.0.0.21'].id, template_id: 'opnsense', name: null }],
  });
  expect(again.created).toEqual([]);
  expect(again.errors).toEqual([
    expect.objectContaining({ result_id: results['127.0.0.21'].id, code: 'device_exists', device_id: devices['127.0.0.21'].id }),
  ]);
  const bad = await raw(api, 'POST', `/api/discovery/scans/${scanId}/import`, {
    items: [{ result_id: results['127.0.0.22'].id, template_id: 'no_such_template' }],
  });
  expect(bad.status).toBe(422);
  const empty = await raw(api, 'POST', `/api/discovery/scans/${scanId}/import`, { items: [] });
  expect(empty.status).toBe(422);
});

async function sensorsOf(ip: string): Promise<Record<string, any>> {
  const list = (await api.get(`/api/sensors?device_id=${devices[ip].id}&page_size=1000`)).items;
  return Object.fromEntries(list.map((s: any) => [s.name, s]));
}
async function run(id: number) {
  return api.post(`/api/sensors/${id}/run`);
}
const ch = (s: any, key: string) => s.channels.find((c: any) => c.key === key);

test('UniFi AP: clients per SSID/radio (snmp_custom), CPU, memory, uplink', async () => {
  const s = await sensorsOf('127.0.0.22');
  const ng = s['Clients HomeWiFi (ng)'];
  const na = s['Clients HomeWiFi (na)'];
  expect(ng).toMatchObject({ type: 'snmp_custom', primary_channel: 'value' });
  expect(ng.config).toMatchObject({ oid: '1.3.6.1.4.1.41112.1.6.1.2.1.8.1', value_type: 'gauge', unit: 'clients', label: 'HomeWiFi (ng)' });
  expect(na.config).toMatchObject({ oid: '1.3.6.1.4.1.41112.1.6.1.2.1.8.2', label: 'HomeWiFi (na)' });
  expect(ch(ng, 'value').unit).toBe('clients');

  const rNg = await run(ng.id);
  expect(rNg).toMatchObject({ ok: true, status: 'up' });
  expect(rNg.channels[0]).toMatchObject({ key: 'value', value: 5 });
  expect(rNg.message).toMatch(/^HomeWiFi \(ng\): 5/);
  const rNa = await run(na.id);
  expect(rNa.channels[0]).toMatchObject({ key: 'value', value: 12 });
  const after = (await api.get(`/api/sensors/${na.id}`));
  expect(after.status).toBe('up');
  expect(ch(after, 'value').last_value).toBe(12);

  const cpu = await run(s['CPU Load'].id);
  expect(cpu.channels.find((c: any) => c.key === 'cpu_load_pct').value).toBe(17);
  const mem = await run(s['Memory'].id);
  expect(mem.channels.find((c: any) => c.key === 'mem_used_pct').value).toBe(40);
  expect(s.eth0.config).toMatchObject({ if_index: 2, if_name: 'eth0' });
});

test('printer: toner % via percent_of_oid, drum "level not reported", page counter', async () => {
  const s = await sensorsOf('127.0.0.23');
  const toner = s['Supply Black Toner Cartridge'];
  expect(toner.config).toMatchObject({
    oid: '1.3.6.1.2.1.43.11.1.1.9.1.1',
    percent_of_oid: '1.3.6.1.2.1.43.11.1.1.8.1.1',
    value_type: 'gauge',
    unit: '%',
    label: 'Black Toner Cartridge',
  });
  expect(ch(toner, 'value')).toMatchObject({ warning_min: 15, down_min: 5 });
  const r = await run(toner.id);
  expect(r).toMatchObject({ ok: true, status: 'up' });
  expect(r.channels[0].value).toBe(40);

  const drum = await run(s['Supply Drum Unit'].id);
  expect(drum).toMatchObject({ ok: true, status: 'up' });
  expect(drum.channels[0].value).toBeNull();
  expect(drum.message).toContain('level not reported (raw=-3)');

  const pages = s['Page counter'];
  expect(pages.interval_s).toBe(3600);
  expect(pages.config).toMatchObject({ oid: '1.3.6.1.2.1.43.10.2.1.4.1.1', unit: 'pages', label: 'Pages' });
  const pr = await run(pages.id);
  expect(pr.channels[0].value).toBe(12345);

  // The printer device uses community "printers": the System sensor works through it.
  const sys = await run(s.System.id);
  expect(sys).toMatchObject({ ok: true, status: 'up' });
});

test('OPNsense: disk / via snmp_disk, CPU, memory, interface traffic', async () => {
  const s = await sensorsOf('127.0.0.21');
  const disk = s['Disk /'];
  expect(disk).toMatchObject({ type: 'snmp_disk', primary_channel: 'disk_used_pct' });
  const r = await run(disk.id);
  expect(r).toMatchObject({ ok: true, status: 'up' });
  const v = Object.fromEntries(r.channels.map((c: any) => [c.key, c.value]));
  expect(v).toEqual({
    disk_used_pct: 25,
    disk_used_bytes: 7_000_000 * 4096,
    disk_free_bytes: 21_000_000 * 4096,
    disk_total_bytes: 28_000_000 * 4096,
  });
  expect((await api.get(`/api/sensors/${disk.id}`)).config.storage_index).toBe(31);
  expect(ch(disk, 'disk_used_pct')).toMatchObject({ warning_max: 85, down_max: 95 });

  expect((await run(s['CPU Load'].id)).channels[0].value).toBe(20);
  expect((await run(s.Memory.id)).channels.find((c: any) => c.key === 'mem_used_pct').value).toBe(25);

  // Interface rates need two polls.
  const igb0 = s.igb0;
  expect(igb0.config).toMatchObject({ if_index: 1, if_name: 'igb0' });
  await run(igb0.id);
  await new Promise((res) => setTimeout(res, 2000));
  const second = await run(igb0.id);
  const inBps = second.channels.find((c: any) => c.key === 'in_bps').value;
  expect(inBps).toBeGreaterThan(15_000_000); // ~2.5 MB/s * 8 = 20 Mbit/s
  expect(inBps).toBeLessThan(25_000_000);
});

test('HTTP-only host: Ping + HTTP 8080 up; device page shows the sensors', async ({ page }) => {
  const s = await sensorsOf('127.0.0.25');
  expect(s['HTTP 8080'].config).toMatchObject({ url: 'http://{host}:8080/' });
  expect(await run(s['HTTP 8080'].id)).toMatchObject({ ok: true, status: 'up' });
  expect(await run(s.Ping.id)).toMatchObject({ ok: true, status: 'up' });

  await loginViaApi(page, undefined, `/devices/${devices['127.0.0.22'].id}`);
  await expect(page.getByRole('heading', { level: 1 })).toContainText(apName);
  await expect(page.getByRole('link', { name: 'Clients HomeWiFi (na)' }).first()).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: 'Clients HomeWiFi (na)' }).first()).toContainText('12');
});
