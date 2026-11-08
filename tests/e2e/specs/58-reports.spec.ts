// [3] Reports (docs/architecture.md §13.6, §13.11 spec 58; api.md "[3] Reports"; data-model § Report vectors).
// Weekly report definition, "Run now" over a short window that contains a real outage of a toggled HTTP sensor,
// availability equal to the contract formula applied to the sensor's own status_change events, print page (A4
// print media), email captured by the SMTP sink (HTML tables, no remote resources, plain-text part), Hebrew RTL.
import { expect, test } from '@playwright/test';
import { apiAs, loginViaApi, type Api } from '../lib/helpers';
import { raw } from '../lib/lab';
import { PHASE3, clearSmtp, configureSmtp, setFeatures, shot3, smtpMessages, snmpMeasuredDevice, tx } from '../lib/lab3';

test.describe.configure({ mode: 'serial' });
test.skip(!PHASE3, 'phase-3 simulators not running');

let api: Api;
// availability is measured on ping, else snmp_system (§13.6.2): an HTTP-only device is "not measured"
let prob: Awaited<ReturnType<typeof snmpMeasuredDevice>>;
let periodStart = '';
let periodEnd = '';
let reportId = 0;
let runId = 0;

/** Contract availability (architecture §13.6.2) from status_change events; no maintenance in this test. */
function availability(events: any[], ps: number, pe: number) {
  const ch = events.filter((e) => e.kind === 'status_change').map((e) => ({ t: Date.parse(e.time), s: e.new_status })).sort((a, b) => a.t - b.t);
  let state = 'unknown';
  for (const c of ch) if (c.t <= ps) state = c.s;
  let t = ps;
  const spans: [string, number, number][] = [];
  for (const c of ch.filter((c) => c.t > ps && c.t < pe)) { spans.push([state, t, c.t]); state = c.s; t = c.t; }
  spans.push([state, t, pe]);
  let excluded = 0, down = 0, outages = 0, longest = 0;
  spans.forEach(([s, a, b], i) => {
    const d = (b - a) / 1000;
    if (['paused', 'unknown', 'unreachable'].includes(s)) excluded += d;
    if (s === 'down') {
      down += d;
      longest = Math.max(longest, d);
      if (i === 0 || spans[i - 1][0] !== 'down') outages += 1;
    }
  });
  const measured = (pe - ps) / 1000 - excluded;
  return { measured, down, outages, longest, availability: measured > 0 ? Math.round(((measured - down) / measured) * 10000) / 100 : null };
}

test.beforeAll(async () => {
  test.setTimeout(300_000);
  api = await apiAs();
  await configureSmtp(api);
  await setFeatures(api, { reports: true });
  periodStart = new Date(Date.now() - 5_000).toISOString();
  prob = await snmpMeasuredDevice(api, '127.0.0.131', 'qa3-report-web');
  await prob.up();
  await new Promise((r) => setTimeout(r, 15_000));
  await prob.down();
  await new Promise((r) => setTimeout(r, 25_000));
  await prob.up();
  await new Promise((r) => setTimeout(r, 5_000));
  periodEnd = new Date().toISOString();
});
test.afterAll(async () => { await prob?.stop(); });

test('weekly definition: validation, next_run_at, scope', async () => {
  const bad = await raw(api, 'POST', '/api/reports', { name: 'x', language: 'en', sections: [], schedule: 'weekly', weekday: 8, send_time: '25:00' });
  expect(bad.status).toBe(422);
  const r = await api.post('/api/reports', {
    name: 'QA weekly 3', enabled: true, language: 'en', scope_group_ids: [prob.device.group_id], scope_device_ids: [],
    sections: ['availability', 'top_alerts', 'config_changes', 'new_devices', 'top_talkers', 'flapping_ports', 'port_errors'],
    schedule: 'weekly', weekday: 1, month_day: null, send_time: '07:47', email_to: ['ops@mnidom.test'], channel_ids: [],
  });
  reportId = r.id;
  expect(r.next_run_at).not.toBeNull();
  const next = new Date(r.next_run_at);
  expect(next.getTime()).toBeGreaterThan(Date.now());
  expect(next.getTime() - Date.now()).toBeLessThanOrEqual(7 * 86400_000 + 3600_000);
  const dup = await raw(api, 'POST', '/api/reports', { ...r, id: undefined, name: 'qa WEEKLY 3' });
  expect(dup.status, 'names are unique case-insensitively').toBe(409);
});

test('Run now (send): availability equals the formula on the sensor events; email captured', async () => {
  test.setTimeout(120_000);
  await clearSmtp();
  const run = await api.post(`/api/reports/${reportId}/run`, { period_start: periodStart, period_end: periodEnd, send: true });
  expect(run.status).toBe('running');
  runId = run.id;
  let full: any = null;
  await expect.poll(async () => { full = await api.get(`/api/reports/runs/${runId}`); return full.status; }, { timeout: 60_000 }).toMatch(/^(ok|partial)$/);
  expect(full.status).toBe('ok');
  expect(full.deliveries).toEqual([expect.objectContaining({ target: 'email:ops@mnidom.test', ok: true })]);
  const ev = await api.get(`/api/events?sensor_id=${prob.sensor.id}&page_size=200`);
  const want = availability(ev.items ?? ev, Date.parse(periodStart), Date.parse(periodEnd));
  const row = full.data.availability.devices.find((d: any) => d.device_id === prob.device.id);
  expect(row, 'device row').toBeTruthy();
  expect(row.sensor_id).toBe(prob.sensor.id);
  const diag = `row ${JSON.stringify(row)} oracle ${JSON.stringify(want)} period ${periodStart}..${periodEnd} events ${JSON.stringify((ev.items ?? ev).filter((e: any) => e.kind === 'status_change').map((e: any) => [e.time, e.old_status, e.new_status]))}`;
  expect(row.outages, diag).toBe(want.outages);
  expect(Math.abs(row.downtime_s - want.down)).toBeLessThanOrEqual(3);
  expect(Math.abs(row.measured_s - want.measured), 'measured seconds (event times are ms, report s)').toBeLessThanOrEqual(3);
  // the window is only ~2 min, so 1 s of the measured/down tolerance above moves availability by ~1 %: check the
  // row's own maths exactly instead (availability = (measured - downtime) / measured, 2 decimals)
  const own = ((row.measured_s - row.downtime_s) / row.measured_s) * 100;
  const tol = 200 / row.measured_s; // whole-second rounding of measured_s and downtime_s on a ~2 min window
  expect(Math.abs(row.availability - own), `row ${JSON.stringify(row)} oracle ${JSON.stringify(want)}`).toBeLessThanOrEqual(tol);
  expect(Math.abs(row.availability - (want.availability as number)), `row ${JSON.stringify(row)} oracle ${JSON.stringify(want)}`).toBeLessThanOrEqual(3);
  expect(row.availability).toBeLessThan(100);
  // sections whose features are OFF are omitted with a note
  const notes = Object.fromEntries(full.data.notes.map((n: any) => [n.section, n.note]));
  expect(['feature_off', 'no_data']).toContain(notes.top_talkers ?? 'no_data');
  // email
  await expect.poll(async () => (await smtpMessages()).length, { timeout: 30_000 }).toBe(1);
  const m = (await smtpMessages())[0];
  expect(m.rcpt_tos).toEqual(['ops@mnidom.test']);
  expect(m.subject).toMatch(/^\[NetworkMonitor\] .*report: QA weekly 3 \(\d\d\.\d\d.\d\d\.\d\d\.\d{4}\)/);
  expect(m.html).toContain('<table');
  expect(m.html).toContain('qa3-report-web');
  expect(m.remote_resources, 'no remote images/scripts in the HTML email').toEqual([]);
  expect(m.html).not.toMatch(/<script/i);
  expect(m.size).toBeLessThan(1024 * 1024);
  expect(m.text, 'plain-text part').toMatch(/availab/i);
  // stored HTML download = the email body
  const h = await api.ctx.fetch(`/api/reports/runs/${runId}/html`);
  expect(h.status()).toBe(200);
  expect(h.headers()['content-disposition']).toMatch(/netmon-report-.*-\d{8}\.html/);
  expect(await h.text()).toContain('qa3-report-web');
  // report_running guard
  const r1 = await raw(api, 'POST', `/api/reports/${reportId}/run`, { period_start: periodStart, period_end: periodEnd, send: false });
  const r2 = await raw(api, 'POST', `/api/reports/${reportId}/run`, { period_start: periodStart, period_end: periodEnd, send: false });
  expect([r1.status, r2.status]).toContain(202);
  if (r2.status !== 202) expect(r2.body.error.code).toBe('report_running');
  const tooLong = await raw(api, 'POST', `/api/reports/${reportId}/run`, { period_start: new Date(Date.now() - 93 * 86400_000).toISOString(), period_end: new Date().toISOString() });
  expect([409, 422]).toContain(tooLong.status);
});

test('report page: print stylesheet (A4 media emulation), Print / Save as PDF button, no navigation in print', async ({ page }) => {
  await loginViaApi(page, undefined, `/reports/runs/${runId}`);
  await expect(page.getByText('qa3-report-web').first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole('button', { name: tx('reports.print', /print/i) })).toBeVisible();
  await shot3(page, '58-report-page');
  await page.emulateMedia({ media: 'print' });
  await expect(page.getByRole('main').getByText('qa3-report-web').first()).toBeVisible();
  await expect(page.getByRole('navigation').first()).toBeHidden();
  await page.pdf?.({ path: `${process.env.QA3_SHOT_DIR ?? '/tmp'}/58-report.pdf`, format: 'A4' }).catch(() => undefined);
  await page.emulateMedia({ media: 'screen' });
});

test('Hebrew report: RTL email and page', async ({ page }) => {
  test.setTimeout(90_000);
  await clearSmtp();
  const run = await api.post('/api/reports/preview', {
    report: { name: 'QA he 3', language: 'he', scope_group_ids: [prob.device.group_id], scope_device_ids: [], sections: ['availability'], schedule: 'none', email_to: [], channel_ids: [] },
    period_start: periodStart, period_end: periodEnd,
  });
  expect(run.report_id).toBeNull();
  await expect.poll(async () => (await api.get(`/api/reports/runs/${run.id}`)).status, { timeout: 60_000 }).toBe('ok');
  const html = await (await api.ctx.fetch(`/api/reports/runs/${run.id}/html`)).text();
  expect(html).toMatch(/dir="rtl"/);
  await loginViaApi(page, undefined, `/reports/runs/${run.id}`);
  await expect(page.getByRole('main').getByText('qa3-report-web').first()).toBeVisible({ timeout: 20_000 });
  const dir = await page.locator('[dir="rtl"]').count();
  expect(dir, 'the Hebrew report content is RTL').toBeGreaterThan(0);
  await shot3(page, '58-report-he');
});

test('Reports page lists the definition and runs; viewer-safe UI', async ({ page }) => {
  await loginViaApi(page, undefined, '/reports');
  await expect(page.getByText('QA weekly 3').first()).toBeVisible({ timeout: 20_000 });
  await shot3(page, '58-reports');
});
