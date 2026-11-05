import { expect, test } from '@playwright/test';
import { Api, apiAs, defaultGroupId, loginViaApi, uid } from '../lib/helpers';
import { findDeviceByHost, raw, snmpCreds } from '../lib/lab';

// Phase 2a (c): apply a template to an existing device -> created/skipped summary; re-applying is idempotent
// (nothing new, reason "exists"). Templates are readable by everyone (/templates).
test.describe.configure({ mode: 'serial' });

let api: Api;
let switchId = 0;

test.beforeAll(async () => {
  api = await apiAs();
  // The Cisco switch sim (127.0.0.24, community public, port 1161, no HOST-RESOURCES-MIB).
  const d = await findDeviceByHost(api, '127.0.0.24');
  switchId =
    d?.id ??
    (await api.post('/api/devices', { group_id: await defaultGroupId(api), name: uid('existing-switch'), host: '127.0.0.24', auto_sensors: [] })).id;
  // Precondition: device-level credentials for the sim (port 1161), and no sensors yet.
  await api.patch(`/api/devices/${switchId}`, { snmp_credentials: snmpCreds() });
  for (const s of (await api.get(`/api/sensors?device_id=${switchId}&page_size=1000`)).items) {
    await api.del(`/api/sensors/${s.id}`);
  }
});

test('API: built-in templates, sorted by priority', async () => {
  const t = await api.get('/api/device-templates');
  expect(t.items.map((x: any) => x.id)).toEqual([
    'opnsense',
    'unifi_ap',
    'printer',
    // priority 50, by id: [2b-1] HPE + [2b-2] vendor library (data-model § New vendor templates, final order)
    'apc_ups',
    'aruba_cx',
    'aruba_wifi',
    'checkpoint_gaia',
    'cisco_ios',
    'dell_idrac',
    'fortinet_fortigate',
    'hpe_arubaos_switch',
    'hpe_comware',
    'hpe_ilo',
    'mikrotik_routeros',
    'paloalto_panos',
    'nas_server',
    'generic_snmp',
    'http_server',
    'generic_ping',
  ]);
  const unifi = t.items.find((x: any) => x.id === 'unifi_ap');
  expect(unifi.name).toEqual({ en: 'UniFi access point', he: 'נקודת גישה UniFi' });
  expect(unifi.priority).toBe(80);
  expect(unifi.entries.find((e: any) => e.kind === 'snmp_table')).toMatchObject({
    walk_oid: '1.3.6.1.4.1.41112.1.6.1.2.1.8',
    label_oid: '1.3.6.1.4.1.41112.1.6.1.2.1.6',
    label2_oid: '1.3.6.1.4.1.41112.1.6.1.2.1.9',
    max: 16,
  });
  expect((await api.get('/api/device-templates/printer')).id).toBe('printer');
  expect((await raw(api, 'GET', '/api/device-templates/nope')).status).toBe(404);
  const bad = await raw(api, 'POST', `/api/devices/${switchId}/apply-template`, { template_id: 'nope' });
  expect(bad.status).toBe(422);
  expect(JSON.stringify(bad.body.error.details)).toContain('template_id');
});

test('apply "Generic SNMP device" from the device page -> created/skipped summary', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${switchId}`);
  await page.getByRole('button', { name: 'Apply template' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel(/Template/).selectOption('generic_snmp');
  await expect(dlg).toContainText('Generic SNMP device');
  await dlg.getByRole('button', { name: /^Apply/ }).click();

  // Result: Ping, System, the two up interfaces created; CPU/Memory skipped (no HOST-RESOURCES-MIB).
  const result = page.getByRole('dialog');
  await expect(result).toContainText(/Created/i, { timeout: 120_000 });
  for (const n of ['Ping', 'System', 'Gi1/0/1', 'Gi1/0/2', 'CPU Load', 'Memory']) await expect(result).toContainText(n);
  await expect(result).not.toContainText('Gi1/0/3'); // oper down
  await expect(result).not.toContainText('probe_failed'); // translated reason
  await result.getByRole('button', { name: 'Close' }).last().click();
  await expect(page.getByRole('link', { name: 'Gi1/0/1' })).toBeVisible(); // device sensor list refreshed

  const sensors = (await api.get(`/api/sensors?device_id=${switchId}&page_size=1000`)).items;
  expect(sensors.map((s: any) => s.name).sort()).toEqual(['Gi1/0/1', 'Gi1/0/2', 'Ping', 'System']);
  const gi1 = sensors.find((s: any) => s.name === 'Gi1/0/1');
  expect(gi1.config).toMatchObject({ if_index: 10101, if_name: 'Gi1/0/1' });
});

test('re-apply is idempotent (API): 0 created, existing entries skipped with "exists"', async () => {
  const r = await api.post(`/api/devices/${switchId}/apply-template`, { template_id: 'generic_snmp' });
  expect(r).toMatchObject({ device_id: switchId, template_id: 'generic_snmp', created: [] });
  const reasons = Object.fromEntries(r.skipped.map((s: any) => [s.name, s.reason]));
  expect(reasons).toMatchObject({
    Ping: 'exists',
    System: 'exists',
    'Gi1/0/1': 'exists',
    'Gi1/0/2': 'exists',
    'CPU Load': 'probe_failed',
    Memory: 'probe_failed',
  });
  expect((await api.get(`/api/sensors?device_id=${switchId}&page_size=1000`)).items).toHaveLength(4);
});

test('re-apply from the UI shows "already exists" and creates nothing', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${switchId}`);
  await page.getByRole('button', { name: 'Apply template' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel(/Template/).selectOption('generic_snmp');
  await dlg.getByRole('button', { name: /^Apply/ }).click();
  await expect(page.getByRole('dialog')).toContainText(/Already (monitored|exists) \(4\)/, { timeout: 120_000 });
  await expect(page.getByRole('dialog')).not.toContainText('probe_failed'); // translated reasons
  expect((await api.get(`/api/sensors?device_id=${switchId}&page_size=1000`)).items).toHaveLength(4);
});

test('apply to a disabled device: sensors created paused', async () => {
  const d = await api.post('/api/devices', {
    group_id: await defaultGroupId(api),
    name: uid('paused-ap'),
    host: '127.0.0.22',
    enabled: false,
    snmp_credentials: snmpCreds(),
    auto_sensors: [],
  });
  const r = await api.post(`/api/devices/${d.id}/apply-template`, { template_id: 'unifi_ap' });
  expect(r.created.map((c: any) => c.name).sort()).toEqual(
    ['CPU Load', 'Clients HomeWiFi (na)', 'Clients HomeWiFi (ng)', 'Memory', 'Ping', 'System', 'eth0'].sort(),
  );
  const sensors = (await api.get(`/api/sensors?device_id=${d.id}&page_size=1000`)).items;
  for (const s of sensors) expect.soft(s.status, s.name).toBe('paused');
  await api.del(`/api/devices/${d.id}`);
});

test('/templates lists the templates with localized names and entries', async ({ page }) => {
  await loginViaApi(page, undefined, '/templates');
  await expect(page.getByRole('heading', { level: 1, name: 'Templates' })).toBeVisible();
  for (const n of ['OPNsense / pfSense firewall', 'UniFi access point', 'Printer', 'Generic SNMP device', 'Web server', 'Ping only']) {
    await expect(page.getByText(n, { exact: true }).first()).toBeVisible();
  }
  await expect(page.getByText(/Interfaces that are up \(max 16\)/).first()).toBeVisible();
});
