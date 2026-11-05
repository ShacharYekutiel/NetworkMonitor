import { expect, test } from '@playwright/test';
import { Api, apiAs, defaultGroupId, loginViaApi, uid } from '../lib/helpers';
import { DISCOVERY_SNMP_PORT, raw, scanResults, snmpCreds, startScan, waitForScan } from '../lib/lab';
import { findDevice, labLog, shot2b } from '../lib/lab2b';

// [2b-1] (e) Fingerprinting without SNMP (architecture.md §10.4 fingerprint sims, data-model.md
// § Fingerprint classification rules). Scan 127.0.0.41-49 (contract table) + 127.0.0.61-67 (QA extras:
// Aruba web, Cisco SSH, FortiGate, Synology, iLO, iDRAC, no-match) with fingerprinting on, then
// Re-identify devices (fingerprint-only and SNMP).
test.describe.configure({ mode: 'serial' });

let api: Api;
let scanId = 0;
let scanMs = 0;
const R: Record<string, any> = {};

type Exp = { device_class?: string; vendor?: string | null; model?: string | null; confidence?: string; classified_by?: string };
const EXPECTED: Record<string, Exp> = {
  '127.0.0.41': { device_class: 'switch', vendor: 'tp-link', model: 'TL-SG108E', confidence: 'medium', classified_by: 'fingerprint' },
  '127.0.0.42': { device_class: 'router', vendor: 'opnsense', confidence: 'high', classified_by: 'fingerprint' },
  '127.0.0.44': { device_class: 'switch', vendor: 'h3c', confidence: 'high', classified_by: 'fingerprint' },
  '127.0.0.45': { device_class: 'router', vendor: 'mikrotik', confidence: 'high', classified_by: 'fingerprint' },
  '127.0.0.47': { device_class: 'printer', vendor: 'hp', confidence: 'medium', classified_by: 'fingerprint' },
  '127.0.0.61': { device_class: 'switch', vendor: 'hp', model: 'JL259A', confidence: 'medium', classified_by: 'fingerprint' },
  '127.0.0.62': { device_class: 'router', vendor: 'cisco', confidence: 'medium', classified_by: 'fingerprint' },
  '127.0.0.63': { device_class: 'router', vendor: 'fortinet', confidence: 'medium', classified_by: 'fingerprint' },
  '127.0.0.64': { device_class: 'nas', vendor: 'synology', confidence: 'high', classified_by: 'fingerprint' },
  '127.0.0.65': { device_class: 'server', vendor: 'hp', confidence: 'high', classified_by: 'fingerprint' },
  '127.0.0.66': { device_class: 'server', vendor: 'dell', confidence: 'high', classified_by: 'fingerprint' },
  '127.0.0.67': { device_class: 'unknown', vendor: null, classified_by: 'ports', confidence: 'low' },
};
const FP_TIMEOUT = 1000;

test.beforeAll(async () => {
  api = await apiAs();
  const t0 = Date.now();
  const s = await startScan(api, {
    targets: ['127.0.0.41-49', '127.0.0.61-67'],
    group_id: await defaultGroupId(api),
    options: { ping: false, snmp: true, snmp_port: DISCOVERY_SNMP_PORT, snmp_timeout_s: 1, tcp: true, tcp_ports: [22, 23, 80, 443, 8080, 8443], tcp_timeout_ms: 500, dns: false, fingerprint: true, fingerprint_timeout_ms: FP_TIMEOUT },
  });
  scanId = s.id;
  expect(s.options).toMatchObject({ fingerprint: true, fingerprint_timeout_ms: FP_TIMEOUT });
  await waitForScan(api, scanId, 120_000);
  scanMs = Date.now() - t0;
  for (const r of await scanResults(api, scanId)) R[r.ip] = r;
});

test('scan finds every fingerprint host', async () => {
  const ips = Object.keys(R).sort();
  for (const ip of ['41', '42', '43', '44', '45', '46', '47', '48', '49', '61', '62', '63', '64', '65', '66', '67']) {
    expect.soft(ips, `127.0.0.${ip}`).toContain(`127.0.0.${ip}`);
  }
});

test('classification per the F-rules: class, vendor, model, confidence, classified_by', async () => {
  for (const [ip, exp] of Object.entries(EXPECTED)) {
    const r = R[ip];
    expect.soft(r, ip).toBeTruthy();
    if (!r) continue;
    expect.soft({ device_class: r.device_class, vendor: r.vendor, confidence: r.confidence, classified_by: r.classified_by }, ip).toMatchObject(
      Object.fromEntries(Object.entries({ device_class: exp.device_class, vendor: exp.vendor, confidence: exp.confidence, classified_by: exp.classified_by }).filter(([, v]) => v !== undefined)),
    );
    if (exp.model !== undefined) expect.soft(r.model, `${ip} model`).toBe(exp.model);
  }
  // Synology model regex \b((?:DS|RS)\d{3,4}\+?)\b: the trailing \b after "+" at the end of the title
  // cannot match, so the rule yields "DS920" (see report); accept both.
  expect.soft(R['127.0.0.64']?.model ?? '', 'synology model').toMatch(/^DS920\+?$/);
});

test('fingerprint JSON details (http/ssh/telnet), limits and the no-credential rule', async () => {
  const http = (ip: string) => (R[ip]?.fingerprint?.http ?? []) as any[];
  // .41 TP-Link: title + markers.
  expect(http('127.0.0.41')[0]).toMatchObject({ port: 80, scheme: 'http', status: 200, title: 'TL-SG108E', redirects: 0, error: null });
  expect(http('127.0.0.41')[0].markers).toEqual(expect.arrayContaining(['easy smart', 'tp-link']));
  // .42 HTTPS self-signed, one same-host redirect followed.
  expect(http('127.0.0.42')[0]).toMatchObject({ port: 443, scheme: 'https', status: 200, title: 'Login | OPNsense', server: 'OPNsense', redirects: 1, error: null });
  expect(http('127.0.0.42')[0].markers).toContain('opnsense');
  // .43 off-host redirect not followed.
  expect(http('127.0.0.43')[0]).toMatchObject({ port: 80, status: 302, redirects: 0, title: null });
  // SSH banners.
  expect(R['127.0.0.44'].fingerprint.ssh).toEqual({ port: 22, banner: 'SSH-2.0-Comware-7.1.064' });
  expect(R['127.0.0.45'].fingerprint.ssh).toEqual({ port: 22, banner: 'SSH-2.0-ROSSSH' });
  // .46 telnet: IAC removed, first non-empty line.
  expect(R['127.0.0.46'].fingerprint.telnet).toEqual({ port: 23, banner: 'User Access Verification' });
  expect(R['127.0.0.46'].fingerprint.http).toEqual([]);
  // .47 Server header, no title.
  expect(http('127.0.0.47')[0]).toMatchObject({ title: null, server: expect.stringMatching(/^HP HTTP Server; HP LaserJet/) });
  // .48 body cap 64 KiB (the marker after 64 KiB is not seen), realm captured.
  expect(http('127.0.0.48')[0]).toMatchObject({ port: 8080, status: 401, title: null, realm: 'DGS-1210-28' });
  expect(http('127.0.0.48')[0].markers).not.toContain('zyxel');
  // Contract table says model DGS-1210-28, but the model rule only scans titles (T) then telnet (N).
  expect.soft(R['127.0.0.48'].model, '.48 model (table vs. rule mismatch, see report)').toBe('DGS-1210-28');
  // .49 never answers -> timeout within fingerprint_timeout_ms.
  expect(http('127.0.0.49')[0]).toMatchObject({ port: 80, error: 'timeout', status: null, title: null });
  // The whole scan stays bounded (16 hosts, fingerprint timeout 1 s).
  expect(scanMs).toBeLessThan(60_000);

  // What the targets saw: User-Agent, no credentials/cookies, nothing sent on SSH/telnet.
  const log = labLog('fingerprint');
  test.skip(!log, 'fingerprint sim log not available (LAB_DIR)');
  const reqs = log.split('\n').filter((l) => l.startsWith('{')).map((l) => JSON.parse(l));
  const httpReqs = reqs.filter((r) => r.path);
  expect(httpReqs.length).toBeGreaterThan(5);
  for (const r of httpReqs) {
    expect.soft(r.ua, JSON.stringify(r)).toMatch(/^NetworkMonitor\/[\w.+-]+ \(discovery\)$/);
    expect.soft(r.auth, 'no Authorization header').toBeNull();
    expect.soft(r.cookie, 'no Cookie header').toBeNull();
  }
  expect(httpReqs.some((r) => r.ip === '127.0.0.42' && r.path === '/ui/')).toBe(true);
  expect(reqs.filter((r) => r.kind === 'client_sent')).toEqual([]);
});

test('results page: model column, confidence chip and the expandable fingerprint row', async ({ page }) => {
  await loginViaApi(page, undefined, `/discovery/scans/${scanId}`);
  const row = page.getByRole('row').filter({ hasText: '127.0.0.41' }).first();
  await expect(row).toContainText('TL-SG108E');
  await expect(row).toContainText(/medium/i);
  await row.getByRole('button', { name: /Fingerprint|Details|Expand/ }).first().click();
  await expect(page.getByText('TL-SG108E').nth(1)).toBeVisible();
  const r46 = page.getByRole('row').filter({ hasText: '127.0.0.46' }).first();
  await r46.getByRole('button', { name: /Fingerprint|Details|Expand/ }).first().click();
  await expect(page.getByText('User Access Verification')).toBeVisible();
  await shot2b(page, 'fingerprint-results-en');
});

test('Re-identify: fingerprint-only device (UI) and an SNMP device (API)', async ({ page }) => {
  let d = await findDevice(api, '127.0.0.41');
  if (!d) d = await api.post('/api/devices', { group_id: await defaultGroupId(api), name: uid('tl-sg108e'), host: '127.0.0.41', auto_sensors: [] });
  await loginViaApi(page, undefined, `/devices/${d.id}`);
  await page.getByRole('button', { name: 'Re-identify' }).click();
  const result = page.locator('section, [role=dialog]').filter({ hasText: /Suggested template|suggested/i }).first();
  await expect(result).toBeVisible({ timeout: 45_000 });
  await expect(result).toContainText('TL-SG108E');
  await expect(result).toContainText(/tp-link|TP-Link/i);
  await shot2b(page, 'reidentify-result-en');
  const dd = await api.get(`/api/devices/${d.id}`);
  expect(dd.identity).toMatchObject({ device_class: 'switch', vendor: 'tp-link', model: 'TL-SG108E', classified_by: 'fingerprint', snmp_ok: false });
  expect(dd.identity.open_ports).toContain(80);
  expect(typeof dd.identity.identified_at).toBe('string');

  // SNMP device: the ArubaOS agent with device credentials (port 1161).
  let a = await findDevice(api, '127.0.0.51');
  if (!a) a = await api.post('/api/devices', { group_id: await defaultGroupId(api), name: uid('aruba-ident'), host: '127.0.0.51', snmp_credentials: snmpCreds(), auto_sensors: [] });
  const t0 = Date.now();
  const id = await api.post(`/api/devices/${a.id}/identify`);
  expect(Date.now() - t0).toBeLessThan(40_000);
  expect(id).toMatchObject({ device_class: 'switch', vendor: 'hp', classified_by: 'snmp', confidence: 'high', snmp_ok: true, suggested_template: 'hpe_arubaos_switch', sys_object_id: '1.3.6.1.4.1.11.2.3.7.11.181' });
  expect(id.sys_descr).toContain('2930F');
  // Viewer / unknown device.
  expect((await raw(api, 'POST', '/api/devices/99999999/identify')).status).toBe(404);
});
