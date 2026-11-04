import { expect, test } from '@playwright/test';
import { Api, apiAs, defaultGroupId, expectSameDocument, loginViaApi, markDocument, shot, uid } from '../lib/helpers';
import {
  DISCOVERY_SNMP_PORT,
  LAB_EXPECTED,
  LAB_RANGE,
  LAB_SCAN_OPTIONS,
  collectWsFrames,
  findDeviceByHost,
  raw,
  scanResults,
  startScan,
  waitForScan,
  waitNoRunningScan,
} from '../lib/lab';

// Phase 2a (a): discovery scan of the simulator lab 127.0.0.20-30 (tests/sim/lab.sh) with snmp_port 1161,
// extra community "printers", TCP 8080/9100, ping off (architecture.md §9.6): live progress and results
// over the WebSocket, classification + suggested template per host, the existing device flagged.
test.describe.configure({ mode: 'serial' });

let api: Api;
let existingSwitchId = 0;
let scanId = 0;

test.beforeAll(async () => {
  api = await apiAs();
  await waitNoRunningScan(api);
  // Precondition: 127.0.0.24 is already monitored -> its result must say "exists" and not be new.
  const existing = await findDeviceByHost(api, '127.0.0.24');
  existingSwitchId =
    existing?.id ??
    (
      await api.post('/api/devices', {
        group_id: await defaultGroupId(api),
        name: uid('existing-switch'),
        host: '127.0.0.24',
        auto_sensors: [],
      })
    ).id;
});

test('start a scan from the Discovery page; progress and results arrive live', async ({ page }) => {
  const ws = collectWsFrames(page);
  await loginViaApi(page, undefined, '/discovery');
  await expect(page.getByRole('heading', { level: 1, name: 'Discovery' })).toBeVisible();
  await expect(page.getByTestId('ws-indicator')).toContainText('Live');

  const form = page.locator('form').filter({ has: page.getByLabel(/^Targets/) });
  await form.getByLabel(/^Targets/).fill(LAB_RANGE);
  // Options: ping off (ICMP to loopback answers for every IP), DNS off, SNMP port 1161, extra community.
  await form.getByRole('checkbox', { name: /^Ping/ }).uncheck();
  await form.getByRole('checkbox', { name: /^(Reverse )?DNS/ }).uncheck();
  // Extra communities: write-only, masked (architecture §9.5 asks for password-type inputs).
  const community = form.getByLabel(/^Extra communities/);
  const masked = await community.evaluate(
    (el) => (el as HTMLInputElement).type === 'password' || getComputedStyle(el).getPropertyValue('-webkit-text-security') === 'disc',
  );
  expect(masked, 'extra community input is masked').toBe(true);
  await community.fill('printers');
  await form.getByRole('textbox', { name: /^TCP ports/ }).fill('8080, 9100');
  await form.getByRole('button', { name: /Advanced/ }).click();
  await form.getByLabel(/^SNMP port/).fill(String(DISCOVERY_SNMP_PORT));
  await markDocument(page);
  await form.getByRole('button', { name: /^Start scan/ }).click();

  await expect(page).toHaveURL(/\/discovery\/scans\/\d+$/);
  scanId = Number(page.url().split('/').pop());
  await expectSameDocument(page); // SPA navigation

  // Live: rows appear and the progress bar fills without a reload.
  const rows = page.getByRole('table').getByRole('row');
  await expect(page.getByRole('progressbar')).toBeVisible();
  for (const ip of Object.keys(LAB_EXPECTED)) {
    await expect(rows.filter({ hasText: ip })).toHaveCount(1, { timeout: 60_000 });
  }
  await expect(page.getByText(/Completed/).first()).toBeVisible({ timeout: 60_000 });
  await expect(page.getByRole('button', { name: 'Cancel' })).toHaveCount(0);
  await expectSameDocument(page);

  // WS: discovery.progress, one discovery.result per found host, one discovery.finished.
  expect(ws.types).toContain('discovery.progress');
  const resultFrames = ws.frames.filter((f) => f.type === 'discovery.result' && f.data.scan_id === scanId);
  expect(resultFrames.map((f) => f.data.result.ip).sort()).toEqual(Object.keys(LAB_EXPECTED).sort());
  const finished = ws.frames.filter((f) => f.type === 'discovery.finished' && f.data.scan_id === scanId);
  expect(finished).toHaveLength(1);
  expect(finished[0].data).toMatchObject({ status: 'completed', total_hosts: 11, scanned_hosts: 11, found_hosts: 5 });
  // The WS result for the existing device carries existing_device_id as well.
  const wsSwitch = resultFrames.find((f) => f.data.result.ip === '127.0.0.24');
  expect(wsSwitch.data.result.existing_device_id).toBe(existingSwitchId);
});

test('API: exactly the 5 lab hosts, classified as documented', async () => {
  const scan = await waitForScan(api, scanId);
  expect(scan).toMatchObject({
    status: 'completed',
    trigger: 'manual',
    profile_id: null,
    targets: [LAB_RANGE],
    total_hosts: 11,
    scanned_hosts: 11,
    found_hosts: 5,
    created_by: 'admin',
    error: null,
  });
  expect(scan.finished_at).toBeTruthy();
  // Secrets never come back; the internal credential snapshot is never serialized.
  expect(scan.options.extra_communities).toBeNull();
  expect(scan.options.extra_communities_count).toBe(1);
  expect(scan.options.snmp_port).toBe(DISCOVERY_SNMP_PORT);
  expect(scan.options.tcp_ports).toEqual([8080, 9100]);
  expect(scan).not.toHaveProperty('credentials');
  expect(JSON.stringify(scan)).not.toContain('printers');

  const results = await scanResults(api, scanId);
  expect(results.map((r) => r.ip)).toEqual(Object.keys(LAB_EXPECTED)); // sorted by ip ascending
  for (const r of results) {
    const exp = LAB_EXPECTED[r.ip];
    expect.soft(r, r.ip).toMatchObject({
      scan_id: scanId,
      device_class: exp.device_class,
      vendor: exp.vendor,
      suggested_template: exp.suggested_template,
      snmp_credential: exp.snmp_credential,
      open_ports: exp.open_ports,
      ping_ok: false,
      imported_device_id: null,
    });
    expect.soft(r, `${r.ip} snmp`).toMatchObject(
      exp.snmp_credential ? { snmp_ok: true, snmp_version: 'v2c', sys_name: exp.sys_name } : { snmp_ok: false, snmp_version: null },
    );
    expect(r).not.toHaveProperty('snmp_credential_index');
  }
  const by = Object.fromEntries(results.map((r) => [r.ip, r]));
  expect(by['127.0.0.21'].sys_object_id).toBe('1.3.6.1.4.1.8072.3.2.8');
  expect(by['127.0.0.21'].sys_descr).toContain('stable/24.7');
  expect(by['127.0.0.23'].sys_object_id).toBe('1.3.6.1.4.1.2435.2.3.9.1');
  // Existing device flagged; everything else is new on a fresh system.
  expect(by['127.0.0.24'].existing_device_id).toBe(existingSwitchId);
  expect(by['127.0.0.24'].is_new).toBe(false);
  for (const ip of ['127.0.0.21', '127.0.0.22', '127.0.0.23', '127.0.0.25']) {
    expect.soft(by[ip].existing_device_id, ip).toBeNull();
  }
  expect(scan.new_hosts).toBe(results.filter((r) => r.is_new).length);

  // Filters
  const hidden = await scanResults(api, scanId, 'hide_existing=true');
  expect(hidden.map((r) => r.ip)).not.toContain('127.0.0.24');
  const printers = await scanResults(api, scanId, 'device_class=printer,switch');
  expect(printers.map((r) => r.ip)).toEqual(['127.0.0.23', '127.0.0.24']);
  const q = await scanResults(api, scanId, 'q=U6-Lite');
  expect(q.map((r) => r.ip)).toEqual(['127.0.0.22']);
});

test('results table: class, vendor, template, Exists link, filters', async ({ page }) => {
  await loginViaApi(page, undefined, `/discovery/scans/${scanId}`);
  const table = page.getByRole('table');
  const row = (ip: string) => table.getByRole('row').filter({ hasText: ip });
  await expect(row('127.0.0.21')).toContainText('Router');
  await expect(row('127.0.0.21')).toContainText(/opnsense/i); // vendor badge
  await expect(row('127.0.0.21')).toContainText('OPNsense.localdomain');
  await expect(row('127.0.0.22')).toContainText('Access point');
  await expect(row('127.0.0.23')).toContainText('Printer');
  await expect(row('127.0.0.23')).toContainText('9100');
  await expect(row('127.0.0.24')).toContainText('Switch');
  await expect(row('127.0.0.25')).toContainText('Unknown');
  await expect(row('127.0.0.25')).toContainText('8080');

  // Per-row template select defaults to the suggestion.
  const tmpl = (ip: string) => row(ip).getByRole('combobox', { name: `Template for ${ip}` });
  await expect(tmpl('127.0.0.21')).toHaveValue('opnsense');
  await expect(tmpl('127.0.0.22')).toHaveValue('unifi_ap');
  await expect(tmpl('127.0.0.23')).toHaveValue('printer');
  await expect(tmpl('127.0.0.25')).toHaveValue('http_server');

  // The existing device: an "Exists" link to it, row not selectable.
  const existsLink = row('127.0.0.24').getByRole('link', { name: /Exists/ });
  await expect(existsLink).toHaveAttribute('href', `/devices/${existingSwitchId}`);
  await expect(row('127.0.0.24').getByRole('checkbox', { name: 'Select 127.0.0.24' })).toBeDisabled();
  await expect(row('127.0.0.21')).toContainText('New');

  // "Hide existing" filter
  await page.getByRole('checkbox', { name: 'Hide existing devices' }).check();
  await expect(row('127.0.0.24')).toHaveCount(0);
  await expect(row('127.0.0.21')).toHaveCount(1);
  await page.getByRole('checkbox', { name: 'Hide existing devices' }).uncheck();
  await expect(row('127.0.0.24')).toHaveCount(1);
  await shot(page, 'discovery-results-en');

  // The scan is listed on the Scans tab.
  await page.goto('/discovery');
  const scanRow = page.getByRole('row').filter({ has: page.locator(`a[href="/discovery/scans/${scanId}"]`) });
  await expect(scanRow).toContainText('Completed');
});

test('scan with ping on: every loopback IP answers ICMP', async () => {
  const s = await startScan(api, {
    targets: [LAB_RANGE],
    group_id: await defaultGroupId(api),
    options: { ...LAB_SCAN_OPTIONS, ping: true },
  });
  expect(s.status).toBe('running');
  const done = await waitForScan(api, s.id);
  expect(done).toMatchObject({ status: 'completed', total_hosts: 11, found_hosts: 11 });
  const results = await scanResults(api, s.id);
  const by = Object.fromEntries(results.map((r) => [r.ip, r]));
  expect(by['127.0.0.20']).toMatchObject({ ping_ok: true, snmp_ok: false, device_class: 'unknown', suggested_template: 'generic_ping' });
  expect(by['127.0.0.20'].ping_rtt_ms).toEqual(expect.any(Number));
  expect(by['127.0.0.21']).toMatchObject({ ping_ok: true, device_class: 'router', suggested_template: 'opnsense' });
  // Hosts found by the earlier scan are known now, so not new again.
  expect(by['127.0.0.21'].is_new).toBe(false);
});

test('target validation: too_many_hosts, multicast, unknown option, no probe', async () => {
  const gid = await defaultGroupId(api);
  const big = await raw(api, 'POST', '/api/discovery/scans', { targets: ['10.0.0.0/8'], group_id: gid });
  expect(big.status).toBe(422);
  expect(big.body.error.code).toBe('too_many_hosts');
  expect(big.body.error.details).toEqual([{ count: 16777214, max: 4096 }]);

  const two = await raw(api, 'POST', '/api/discovery/scans', { targets: ['10.0.0.0/20', '10.1.0.0/24'], group_id: gid });
  expect(two.status).toBe(422);
  expect(two.body.error.code).toBe('too_many_hosts');

  const mc = await raw(api, 'POST', '/api/discovery/scans', { targets: ['127.0.0.21', '224.0.0.1'], group_id: gid });
  expect(mc.status).toBe(422);
  expect(mc.body.error.code).toBe('validation_error');
  expect(mc.body.error.details.some((d: any) => JSON.stringify(d.loc) === JSON.stringify(['body', 'targets', 1]))).toBe(true);

  for (const bad of ['0.0.0.1', '255.255.255.255', '240.0.0.1', 'ff02::1', '::', '10.0.0.0/7', '10.0.0.9-5']) {
    const r = await raw(api, 'POST', '/api/discovery/scans', { targets: [bad], group_id: gid });
    expect.soft(r.status, bad).toBe(422);
  }
  const unk = await raw(api, 'POST', '/api/discovery/scans', { targets: ['127.0.0.21'], group_id: gid, options: { bogus: 1 } });
  expect(unk.status).toBe(422);
  const none = await raw(api, 'POST', '/api/discovery/scans', {
    targets: ['127.0.0.21'],
    group_id: gid,
    options: { ping: false, snmp: false, tcp: false },
  });
  expect(none.status).toBe(422);
  // Nothing was started by the rejected requests.
  const scans = await api.get('/api/discovery/scans');
  expect(scans.items.filter((s: any) => s.status === 'running')).toHaveLength(0);
});
