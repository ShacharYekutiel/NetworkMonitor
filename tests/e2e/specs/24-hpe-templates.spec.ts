import { expect, test } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { DISCOVERY_SNMP_PORT, ensureGroup, scanResults, startScan, waitForScan } from '../lib/lab';
import { findDevice, shot2b } from '../lib/lab2b';

// [2b-1] (f) HPE templates (data-model.md § HPE switch templates, architecture.md §10.4 HPE snmpsim):
// scan 127.0.0.51-52, import with the auto-selected templates -> the documented sensors and values:
// ArubaOS CPU 7 %, Memory 1 ~39.39 %, PSU "bad" down, notPresent RPS skipped, PoE 45 W / 370 W ~12.16 %,
// ports 1+2; Comware CPU/Memory/Temperature Board, Fan 2 fanError down, PoE rows skipped (no_rows), up ports.
test.describe.configure({ mode: 'serial' });

let api: Api;
let scanId = 0;
let groupId = 0;
const R: Record<string, any> = {};
const D: Record<string, any> = {};
let importResp: any = null;

// [1.3] discovery import applies the template with interfaces "important" (api.md § [1.3] Template apply): see IF_ALLOWED.
const ARUBA = ['CPU', 'Fan Sensor', 'Memory 1', 'Over-temperature Sensor', 'Ping', 'PoE 1 budget', 'PoE 1 power', 'PoE 1 usage', 'Power Supply Sensor', 'System'];
const COMWARE = ['CPU Board', 'Fan 1', 'Fan 2', 'Memory Board', 'PSU 1', 'Ping', 'System', 'Temperature Board'];
// [1.3] interface sensors only on important ports (uplink = LLDP neighbour that is a known switch/AP device). Whether
// the neighbours exist as devices at import time depends on the earlier specs, so only these ports may appear.
const IF_ALLOWED: Record<string, string[]> = { '127.0.0.51': ['1', '2'], '127.0.0.52': ['GigabitEthernet1/0/1', 'GigabitEthernet1/0/2'] };

/** Removes the interface sensors from `s` after checking that each is an allowed port and important. */
async function interfaceSensorsOnImportantPortsOnly(ip: string, deviceId: number, s: Record<string, any>): Promise<void> {
  const ifs = Object.values(s).filter((x: any) => x.type === 'snmp_interface');
  if (ifs.length) {
    const ports = (await api.get(`/api/devices/${deviceId}/ports`)).ports;
    for (const x of ifs as any[]) {
      expect(IF_ALLOWED[ip], `${ip}: interface sensor ${x.name}`).toContain(x.name);
      expect(ports.find((p: any) => p.if_index === x.config.if_index)?.important, `${ip}: ${x.name} important`).toBe(true);
      expect(x.impact).toBe('critical');
    }
  }
  for (const x of ifs as any[]) delete s[x.name];
}

async function sensorsByName(deviceId: number): Promise<Record<string, any>> {
  const items = (await api.get(`/api/sensors?device_id=${deviceId}&page_size=1000`)).items;
  return Object.fromEntries(items.map((s: any) => [s.name, s]));
}
function channel(s: any, key = 'value'): number | null {
  const c = (s.channels ?? []).find((x: any) => x.key === key) ?? (s.channels ?? [])[0];
  return c?.last_value ?? c?.value ?? null;
}

test.beforeAll(async () => {
  api = await apiAs();
  groupId = await ensureGroup(api, 'QA HPE');
  const s = await startScan(api, {
    targets: ['127.0.0.51-52'],
    group_id: groupId,
    options: { ping: false, snmp: true, snmp_port: DISCOVERY_SNMP_PORT, snmp_timeout_s: 1, tcp: false, dns: false },
  });
  scanId = s.id;
  await waitForScan(api, scanId);
  for (const r of await scanResults(api, scanId)) R[r.ip] = r;
});

test('classification + auto-selected template per sysObjectID enterprise', async () => {
  expect(R['127.0.0.51']).toMatchObject({ device_class: 'switch', vendor: 'hp', suggested_template: 'hpe_arubaos_switch', classified_by: 'snmp', confidence: 'high', sys_object_id: '1.3.6.1.4.1.11.2.3.7.11.181' });
  expect(R['127.0.0.52']).toMatchObject({ device_class: 'switch', vendor: 'h3c', suggested_template: 'hpe_comware', classified_by: 'snmp', confidence: 'high', sys_object_id: '1.3.6.1.4.1.25506.11.1.177' });
  const ids = (await api.get('/api/device-templates')).items.map((x: any) => x.id);
  expect(ids).toEqual(expect.arrayContaining(['hpe_arubaos_switch', 'hpe_comware']));
});

test('import both with their templates from the results page', async ({ page }) => {
  for (const ip of ['127.0.0.51', '127.0.0.52']) {
    const d = await findDevice(api, ip);
    test.skip(!!d, `device ${ip} exists already (spec run out of order)`);
  }
  await loginViaApi(page, undefined, `/discovery/scans/${scanId}`);
  const row = (ip: string) => page.getByRole('table').getByRole('row').filter({ hasText: ip });
  await expect(row('127.0.0.52')).toBeVisible();
  for (const [ip, tpl] of [['127.0.0.51', 'hpe_arubaos_switch'], ['127.0.0.52', 'hpe_comware']]) {
    await row(ip).getByRole('checkbox', { name: `Select ${ip}` }).check();
    await expect(row(ip).getByRole('combobox', { name: `Template for ${ip}` })).toHaveValue(tpl);
  }
  await page.getByLabel('Import into group').selectOption({ label: 'QA HPE' });
  const respP = page.waitForResponse((r) => r.url().includes(`/api/discovery/scans/${scanId}/import`), { timeout: 120_000 });
  await page.getByRole('button', { name: 'Import selected (2)' }).click();
  importResp = await (await respP).json();
  const dlg = page.getByRole('dialog');
  await expect(dlg).toBeVisible({ timeout: 120_000 });
  await shot2b(page, 'hpe-import-result-en');
  await dlg.getByRole('button', { name: 'Close' }).last().click();
  expect(importResp.errors).toEqual([]);
  // Skipped rows: Comware PoE rows (no_rows); ArubaOS nothing besides possibly none.
  const cw = importResp.created.find((c: any) => c.template_id === 'hpe_comware');
  const skippedNames = cw.sensors_skipped.map((x: any) => `${x.name}:${x.reason}`);
  expect(skippedNames.filter((x: string) => /PoE/.test(x)).length).toBeGreaterThanOrEqual(1);
  for (const x of cw.sensors_skipped.filter((y: any) => /PoE/.test(y.name))) expect(x.reason).toBe('no_rows');
});

test('ArubaOS-Switch sensors and values', async () => {
  D.aruba = await findDevice(api, '127.0.0.51');
  expect(D.aruba).toBeTruthy();
  let s = await sensorsByName(D.aruba.id);
  await interfaceSensorsOnImportantPortsOnly('127.0.0.51', D.aruba.id, s);
  // DEFECT (contract): "PoE 1 power" uses the same OID as "PoE 1 usage" (…105.1.3.1.1.4.1); the snmp_custom
  // idempotency key is config.oid, so apply skips it as `exists`. §10.4 expects it -> soft.
  expect.soft(Object.keys(s).sort(), 'ArubaOS sensor names').toEqual([...ARUBA].sort());
  expect(Object.keys(s).filter((n) => n !== 'PoE 1 power').sort()).toEqual(ARUBA.filter((n) => n !== 'PoE 1 power').sort());
  expect(Object.keys(s).some((n) => /Redundant/.test(n))).toBe(false); // notPresent skipped
  expect(s['Power Supply Sensor'].interval_s).toBe(300);
  expect(s['PoE 1 budget'].interval_s).toBe(3600);
  // Wait for the first values (status sensors interval 300 s run once right after creation).
  await expect
    .poll(async () => {
      s = await sensorsByName(D.aruba.id);
      await interfaceSensorsOnImportantPortsOnly('127.0.0.51', D.aruba.id, s);
      return ['CPU', 'Memory 1', 'Power Supply Sensor', 'Fan Sensor', 'PoE 1 usage', 'PoE 1 power', 'PoE 1 budget'].filter((n) => s[n] && s[n].status === 'unknown');
    }, { timeout: 90_000, intervals: [2000, 5000] })
    .toEqual([]);
  expect(s['Power Supply Sensor'].status).toBe('down');
  expect(s['Power Supply Sensor'].status_message).toBe('Power Supply Sensor: bad (2)');
  expect(s['Fan Sensor'].status).toBe('up');
  expect(s['Over-temperature Sensor'].status).toBe('up');
  expect(s['CPU'].status).toBe('up');
  expect(channel(s['CPU'])).toBe(7);
  expect(channel(s['Memory 1'])).toBeCloseTo(39.39, 1);
  expect(channel(s['PoE 1 usage'])).toBeCloseTo(12.16, 1);
  if (s['PoE 1 power']) expect(channel(s['PoE 1 power'])).toBe(45);
  expect(channel(s['PoE 1 budget'])).toBe(370);
  // Config of the enum sensor (snmp_custom value_type enum).
  expect(s['Power Supply Sensor'].config).toMatchObject({ value_type: 'enum', value_map_default: 'warning', oid: '1.3.6.1.4.1.11.2.14.11.1.2.6.1.4.2' });
  expect(s['Power Supply Sensor'].config.value_map['2']).toEqual({ status: 'down', text: 'bad' });
});

test('Comware sensors and values', async () => {
  D.comware = await findDevice(api, '127.0.0.52');
  expect(D.comware).toBeTruthy();
  let s = await sensorsByName(D.comware.id);
  await interfaceSensorsOnImportantPortsOnly('127.0.0.52', D.comware.id, s);
  expect(Object.keys(s).sort()).toEqual([...COMWARE].sort());
  expect(Object.keys(s).some((n) => /PoE|Vlan|NULL|InLoop|Chassis/.test(n))).toBe(false);
  await expect
    .poll(async () => {
      s = await sensorsByName(D.comware.id);
      return COMWARE.filter((n) => n !== 'Ping' && s[n].status === 'unknown');
    }, { timeout: 90_000, intervals: [2000, 5000] })
    .toEqual([]);
  expect(s['Fan 2'].status).toBe('down');
  // [1.3.1] fan/PSU use the shared HH3C fault-code table (value_map_ref hh3c_error_status, poller/value_maps.py)
  expect(s['Fan 2'].status_message).toBe('Fan 2: Fan fault (fanError, 41)');
  expect(s['Fan 1'].status).toBe('up');
  expect(s['Fan 1'].status_message).toBe('Fan 1: Normal (normal, 2)');
  expect(s['PSU 1'].status).toBe('up');
  expect(channel(s['CPU Board'])).toBe(12);
  expect(channel(s['Memory Board'])).toBe(41);
  expect(channel(s['Temperature Board'])).toBe(43);
  expect(s['CPU Board'].config).toMatchObject({ oid: '1.3.6.1.4.1.25506.2.6.1.1.1.1.6.2' });
});

test('sensor page of the enum sensor shows the mapped text; screenshot', async ({ page }) => {
  const s = await sensorsByName(D.aruba.id);
  await loginViaApi(page, undefined, `/sensors/${s['Power Supply Sensor'].id}`);
  await expect(page.getByText('Power Supply Sensor: bad (2)').first()).toBeVisible();
  await shot2b(page, 'hpe-psu-bad-en');
  // [2b-2] Ports is the default device tab for switches with port inventory (architecture §11.5)
  await page.goto(`/devices/${D.comware.id}?tab=sensors`);
  await expect(page.getByText('Fan 2').first()).toBeVisible();
  await shot2b(page, 'hpe-comware-device-en');
});
