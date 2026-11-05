import { expect, test } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { DISCOVERY_SNMP_PORT, ensureGroup, raw, scanResults, snmpCreds, startScan, waitForScan } from '../lib/lab';
import { findDevice } from '../lib/lab2b';
import { IP, SYNTH, T, shot2b2 } from '../lib/lab2b2';

// [2b-2] (g) New vendor templates against the sims (architecture.md §11.6 "Vendor template sims"):
// classification (rule 9a incl. the net-snmp Linux sysObjectID + checkpoint subtree probe), suggestion,
// apply -> created / skipped (probe_failed, no_rows, not_applicable) and live values; synthetic sims: every
// new template applies with no probe_failed / no_rows / snmp_error / not_applicable (mechanics; http probes
// of hpe_ilo / dell_idrac excepted: nothing listens on 443 there).
// default mode: a failure does not hide the results of the later tests (state is re-derived per test)
test.describe.configure({ mode: 'default' });

let api: Api;
let gid = 0;
const R: Record<string, any> = {};
const DEV: Record<string, any> = {};
const APPLY: Record<string, any> = {};

async function sensorsByName(deviceId: number): Promise<Record<string, any>> {
  const items = (await api.get(`/api/sensors?device_id=${deviceId}&page_size=1000`)).items;
  return Object.fromEntries(items.map((s: any) => [s.name, s]));
}
const ch = (s: any, key?: string) => (key ? s.channels.find((c: any) => c.key === key) : s.channels.find((c: any) => c.key === s.primary_channel))?.last_value ?? null;
async function waitChecked(deviceId: number, names: string[], timeout = 90_000): Promise<Record<string, any>> {
  let by: Record<string, any> = {};
  await expect
    .poll(
      async () => {
        by = await sensorsByName(deviceId);
        return names.filter((n) => !by[n] || by[n].status === 'unknown' || !by[n].last_check_at);
      },
      { timeout, intervals: [1000, 2000, 3000], message: `sensors checked on device ${deviceId}` },
    )
    .toEqual([]);
  return by;
}
async function deviceFor(host: string, name: string): Promise<any> {
  const d = await findDevice(api, host);
  if (d) return d;
  return api.post('/api/devices', { group_id: gid, name, host, snmp_credentials: snmpCreds(), auto_sensors: [] });
}
async function applyOnce(key: string, host: string, name: string, template: string): Promise<any> {
  const d = await deviceFor(host, name);
  DEV[key] = d;
  // fresh state: remove sensors left by an earlier run of this spec
  for (const s of (await api.get(`/api/sensors?device_id=${d.id}&page_size=1000`)).items) await raw(api, 'DELETE', `/api/sensors/${s.id}`);
  const r = await api.post(`/api/devices/${d.id}/apply-template`, { template_id: template });
  APPLY[key] = r;
  return r;
}
const names = (r: any) => r.created.map((c: any) => c.name).sort();
const skipped = (r: any) => Object.fromEntries(r.skipped.map((s: any) => [`${s.name}|${s.type}`, s]));

test.beforeAll(async () => {
  api = await apiAs();
  gid = await ensureGroup(api, 'QA Vendors 2b-2');
  const s = await startScan(api, {
    targets: ['127.0.0.71-73', '127.0.0.101-110'],
    group_id: gid,
    options: { ping: false, snmp: true, snmp_port: DISCOVERY_SNMP_PORT, snmp_timeout_s: 1, tcp: false, dns: false },
  });
  await waitForScan(api, s.id, 120_000);
  for (const r of await scanResults(api, s.id)) R[r.ip] = r;
});

test('classification: Check Point (enterprise 2620 and net-snmp Linux + svn probe), Aruba Instant', async () => {
  expect(R[IP.cp]).toMatchObject({ device_class: 'router', vendor: 'checkpoint', classified_by: 'snmp', confidence: 'high', suggested_template: 'checkpoint_gaia', sys_object_id: '1.3.6.1.4.1.2620.1.6.123.1.49' });
  // rule 9a via the `checkpoint` subtree probe beats rule 11 (net-snmp .10 -> server/linux)
  expect(R[IP.cpCluster]).toMatchObject({ device_class: 'router', vendor: 'checkpoint', classified_by: 'snmp', confidence: 'high', suggested_template: 'checkpoint_gaia', sys_object_id: '1.3.6.1.4.1.8072.3.2.10' });
  // Contract §11.6 expects access_point; rule 10's switch regex (2b-1) contains `arubaos`, which matches the
  // contract's own sysDescr "ArubaOS (MODEL: 515)..." -> switch (contract defect, reported). Soft so the rest runs.
  expect.soft(R[IP.instant]?.device_class, 'Aruba Instant class').toBe('access_point');
  expect(R[IP.instant]).toMatchObject({ vendor: 'aruba', suggested_template: 'aruba_wifi' });
});

test('synthetic sims: each suggests its own template', async () => {
  for (const [tpl, ip] of Object.entries(SYNTH)) expect.soft(R[ip]?.suggested_template, `${ip} -> ${tpl} (class ${R[ip]?.device_class}, vendor ${R[ip]?.vendor})`).toBe(tpl);
});

test('Check Point standalone (.71): created / not_applicable and values', async () => {
  const r = await applyOnce('cp', IP.cp, 'gw-hq', 'checkpoint_gaia');
  expect(names(r)).toEqual(['CPU', 'Concurrent connections', 'Connection table usage', 'Disk /', 'Disk /var/log', 'Fan 1', 'Fan 2', 'Memory', 'Ping', 'Power supply 1', 'Power supply 2', 'System', 'Temperature CPU', 'Temperature Intake', 'VPN tunnels up', 'eth0', 'eth1'].sort());
  expect(r.skipped.map((s: any) => [s.name, s.reason]).sort()).toEqual([['ClusterXL state', 'not_applicable'], ['ClusterXL status', 'not_applicable']]);
  for (const s of r.skipped) expect.soft(s.message).toBe('Condition not met: 1.3.6.1.4.1.2620.1.5.2.0');
  const by = await waitChecked(DEV.cp.id, ['CPU', 'Memory', 'Concurrent connections', 'Connection table usage', 'VPN tunnels up', 'Temperature CPU', 'Temperature Intake', 'Fan 1', 'Fan 2', 'Power supply 1', 'Power supply 2']);
  expect.soft([by.CPU.status, ch(by.CPU)]).toEqual(['up', 23]);
  expect.soft(by.Memory.status).toBe('up');
  expect.soft(ch(by.Memory)).toBeCloseTo(25, 1);
  expect.soft(ch(by['Concurrent connections'])).toBe(12345);
  expect.soft([by['Connection table usage'].status, ch(by['Connection table usage'])]).toEqual(['up', null]);
  const vpn = by['VPN tunnels up'];
  expect.soft(vpn.type).toBe('snmp_walk_count');
  expect.soft({ status: vpn.status, rows: ch(vpn, 'rows'), matched: ch(vpn, 'matched'), unmatched: ch(vpn, 'unmatched') }).toEqual({ status: 'warning', rows: 3, matched: 2, unmatched: 1 });
  // threshold breach -> the Phase 1 threshold message ("unmatched 1 > 0"); else "<label>: 2 of 3" (contract ambiguity)
  expect.soft(vpn.status_message).toMatch(/2 of 3|unmatched 1 > 0/);
  expect.soft(ch(by['Temperature CPU'])).toBe(44);
  expect.soft(ch(by['Temperature Intake'])).toBe(38.5);
  expect.soft([by['Fan 1'].status, by['Fan 2'].status]).toEqual(['up', 'down']);
  expect.soft([by['Power supply 1'].status, by['Power supply 2'].status]).toEqual(['up', 'down']);
  // health strip of the firewall's ports view (categorize: cpu, memory, temperature, psu, fan)
  const h = (await api.get(`/api/devices/${DEV.cp.id}/ports`)).health;
  expect.soft(h.map((x: any) => x.category)).toEqual(['cpu', 'memory', 'temperature', 'psu', 'fan']);
  // categorize(): the temperature rule precedes cpu/memory (data-model § Health strip, QA finding #3), so
  // "Temperature CPU" (44) is a temperature and the chip shows the max of both readings.
  const cpuIds = h.find((x: any) => x.category === 'cpu').sensor_ids;
  expect.soft(cpuIds, 'cpu chip sensors (CPU only)').toHaveLength(1);
  const temp = h.find((x: any) => x.category === 'temperature');
  expect.soft(temp.sensor_ids, 'temperature chip sensors (Temperature CPU + Intake)').toHaveLength(2);
  expect.soft(temp).toMatchObject({ value: 44, unit: '°C' });
  expect.soft(h.find((x: any) => x.category === 'psu')).toMatchObject({ status: 'down', text: '1/2' });
  expect.soft(h.find((x: any) => x.category === 'fan')).toMatchObject({ status: 'down', text: '1/2' });
});

test('Check Point ClusterXL member (.72): ClusterXL rows created, tunnels/hardware skipped', async () => {
  const r = await applyOnce('cpCluster', IP.cpCluster, 'gw-cl1', 'checkpoint_gaia');
  expect(names(r)).toEqual(expect.arrayContaining(['Ping', 'System', 'CPU', 'Memory', 'ClusterXL state', 'ClusterXL status', 'Disk /', 'Disk /var/log']));
  const sk = r.skipped.map((s: any) => [s.name, s.reason, s.message]);
  expect(sk).toEqual(expect.arrayContaining([['VPN tunnels up', 'probe_failed', 'No rows']]));
  const tables = r.skipped.filter((s: any) => s.reason === 'no_rows').map((s: any) => s.name);
  expect(tables).toHaveLength(3); // temperature, fans, power supplies
  expect(r.skipped.filter((s: any) => !['no_rows', 'probe_failed'].includes(s.reason))).toEqual([]);
  const by = await waitChecked(DEV.cpCluster.id, ['CPU', 'ClusterXL state', 'ClusterXL status']);
  expect.soft([by.CPU.status, ch(by.CPU)]).toEqual(['up', 41]);
  expect.soft(by['ClusterXL state'].status).toBe('up');
  expect.soft(by['ClusterXL state'].status_message).toContain('ClusterXL: active');
  expect.soft(by['ClusterXL status'].status).toBe('up');
  expect.soft(by['ClusterXL status'].status_message).toContain('OK');
});

test('Aruba Instant VC (.73): APs up (warning 2 of 3), per-AP rows, Wi-Fi clients 5; controller rows skipped', async () => {
  const r = await applyOnce('instant', IP.instant, 'instant-vc', 'aruba_wifi');
  expect(names(r)).toEqual(expect.arrayContaining(['Ping', 'System', 'APs up', 'AP AP-Lobby', 'AP AP-Office', 'AP AP-Store', 'Wi-Fi clients']));
  const sk = r.skipped.map((s: any) => [s.name, s.type, s.reason]);
  expect(sk).toEqual(expect.arrayContaining([['CPU Load', 'snmp_cpu', 'probe_failed'], ['Memory', 'snmp_memory', 'probe_failed'], ['APs up', 'snmp_walk_count', 'probe_failed'], ['Wi-Fi clients', 'snmp_custom', 'probe_failed']]));
  expect(r.skipped.filter((s: any) => s.reason === 'no_rows')).toHaveLength(1); // controller AP {label} table
  const by = await waitChecked(DEV.instant.id, ['APs up', 'AP AP-Lobby', 'AP AP-Office', 'AP AP-Store', 'Wi-Fi clients']);
  expect.soft({ s: by['APs up'].status, rows: ch(by['APs up'], 'rows'), m: ch(by['APs up'], 'matched') }).toEqual({ s: 'warning', rows: 3, m: 2 });
  expect.soft([by['AP AP-Lobby'].status, by['AP AP-Office'].status, by['AP AP-Store'].status]).toEqual(['up', 'up', 'down']);
  expect.soft(ch(by['Wi-Fi clients'], 'matched')).toBe(5);
  expect.soft(by['Wi-Fi clients'].status_message).toMatch(/5 of 5/);
});

test('apply result dialog shows not_applicable (UI, Check Point re-apply)', async ({ page }) => {
  const cp = await findDevice(api, IP.cp);
  test.skip(!cp, 'Check Point device not created (earlier test failed)');
  await loginViaApi(page, undefined, `/devices/${cp.id}`);
  await page.getByRole('button', { name: T('templates.apply') }).first().click();
  const dlg = page.getByRole('dialog');
  await dlg.getByRole('combobox').first().selectOption('checkpoint_gaia');
  await dlg.getByRole('button', { name: T('templates.applyButton') }).click();
  const res = dlg.getByTestId('apply-result');
  await expect(res).toBeVisible({ timeout: 120_000 });
  await expect(res).toContainText(T('templates.skip.not_applicable'));
  await expect(res).toContainText('ClusterXL state');
  await shot2b2(page, 'apply-not-applicable-en');
});

test('synthetic sims: mechanics of every new template (no failed/empty/inapplicable entries)', async () => {
  const bad = new Set(['probe_failed', 'no_rows', 'snmp_error', 'not_applicable']);
  const problems: string[] = [];
  for (const [tpl, ip] of Object.entries(SYNTH)) {
    const r = await applyOnce(`syn-${tpl}`, ip, `syn-${tpl}`, tpl);
    const tplDef = await api.get(`/api/device-templates/${tpl}`);
    for (const s of r.skipped) {
      const httpWeb = s.type === 'http' && ['hpe_ilo', 'dell_idrac'].includes(tpl);
      if (bad.has(s.reason) && !httpWeb) problems.push(`${tpl}: ${s.name} (${s.type}) ${s.reason} ${s.message}`);
    }
    // every sensor/table entry produced something (interfaces excluded)
    const expectMin = tplDef.entries.filter((e: any) => e.kind !== 'snmp_interfaces' && !(e.kind === 'sensor' && e.type === 'http')).length;
    if (r.created.length < expectMin) problems.push(`${tpl}: only ${r.created.length} created for ${expectMin} entries`);
  }
  expect(problems).toEqual([]);
});

test('synthetic sims: created sensors produce values (no check errors)', async () => {
  const problems: string[] = [];
  for (const [tpl, ip] of Object.entries(SYNTH)) {
    const d = await findDevice(api, ip);
    if (!d) {
      problems.push(`${tpl}: device missing`);
      continue;
    }
    const created = (await api.get(`/api/sensors?device_id=${d.id}&page_size=1000`)).items.filter((c: any) => c.type !== 'http' && c.type !== 'ping');
    const by = await waitChecked(d.id, created.map((c: any) => c.name), 120_000).catch(() => sensorsByName(d.id));
    for (const c of created) {
      const s = by[c.name];
      if (!s) problems.push(`${tpl}: ${c.name} missing`);
      else if (s.status === 'down' && /error|timeout|no such|not.*found|invalid/i.test(s.status_message)) problems.push(`${tpl}: ${c.name} ${s.status} "${s.status_message}"`);
    }
  }
  expect(problems).toEqual([]);
});
