// [3b] AI narrative summary in reports (docs/architecture.md §14.8.10, §14.11 spec 77): report with ai_summary
// -> paragraph at the top (labelled), en and he; input is the report's own data only (no tools); numbers checked
// against the data (unverified -> dropped); mock error -> report rendered without it, run status ok; feature OFF
// -> ai_summary_status "disabled".
import { expect, test } from '@playwright/test';
import { apiAs, loginViaApi, type Api } from '../lib/helpers';
import { clearSmtp, configureSmtp, ensureGroupNamed, setFeatures, smtpMessages, snmpMeasuredDevice } from '../lib/lab3';
import { PHASE3B, audit, ensureAssistant, lastSeq, mock, modelCalls, shot3b } from '../lib/lab3b';

// default mode (not serial): one worker runs the tests in order; a failure restarts the worker (beforeAll is
// idempotent) instead of skipping the remaining tests, so every contract item gets a verdict
test.describe.configure({ mode: 'default' });
test.skip(!PHASE3B, 'phase-3b Bedrock mock not running');

let api: Api;
let dev: Awaited<ReturnType<typeof snmpMeasuredDevice>> | null = null;
const ids: Record<string, number> = {};
const period = () => ({ period_start: new Date(Date.now() - 2 * 86400_000).toISOString(), period_end: new Date().toISOString() });

async function run(reportId: number, send = false): Promise<any> {
  const r = await api.post(`/api/reports/${reportId}/run`, { ...period(), send });
  let full: any = null;
  await expect.poll(async () => { full = await api.get(`/api/reports/runs/${r.id}`); return full.status; }, { timeout: 120_000 }).toMatch(/^(ok|partial|failed)$/);
  return full;
}

test.beforeAll(async () => {
  test.setTimeout(120_000);
  api = await apiAs();
  await ensureAssistant(api);
  await mock.clearModes();
  await configureSmtp(api);
  await setFeatures(api, { reports: true, ai_report_summary: true });
  // availability needs a measured device (ping / snmp_system); HTTP-only devices are "not measured"
  const g = await ensureGroupNamed(api, 'QA Report 3b');
  const existing = (await api.get('/api/devices?q=127.0.0.154&page_size=10')).items.find((d: any) => d.host === '127.0.0.154');
  if (!existing) { dev = await snmpMeasuredDevice(api, '127.0.0.154', 'qa3b-report-snmp', 'QA Report 3b'); await dev.up(); }
  for (const lang of ['en', 'he'] as const) {
    const name = `QA3B AI summary ${lang}`;
    const existing = (await api.get('/api/reports')).items?.find((r: any) => r.name === name);
    const body = { name, enabled: true, language: lang, scope_group_ids: [g.id], scope_device_ids: [], sections: ['availability', 'top_alerts', 'config_changes'],
      schedule: 'weekly', weekday: 1, month_day: null, send_time: '07:47', email_to: ['ops@mnidom.test'], channel_ids: [], ai_summary: true };
    ids[lang] = existing ? (await api.patch(`/api/reports/${existing.id}`, body)).id : (await api.post('/api/reports', body)).id;
  }
});

test.afterAll(async () => { await dev?.stop(); });

test('en: AI paragraph at the top of the report data, email and page; numbers from the data; no tools', async ({ page }) => {
  test.setTimeout(180_000);
  await clearSmtp();
  const since = await lastSeq();
  const full = await run(ids.en, true);
  expect(full.status).toBe('ok');
  expect(full.ai_summary_status).toBe('ok');
  expect(full.data.ai_summary).toMatch(/Availability over the period was/);
  expect(full.data.ai_summary.split(/\s+/).length).toBeLessThanOrEqual(120);
  const calls = (await modelCalls(since)).filter((c) => c.purpose === 'report_summary');
  expect(calls.length).toBe(1);
  expect(calls[0].op).toBe('converse');
  expect(calls[0].offered_tools, 'no tools for report summaries').toEqual([]);
  expect(calls[0].body.inferenceConfig.maxTokens).toBeLessThanOrEqual(400);
  // every percentage in the text appears in the data (± 0.01)
  const pcts = [...full.data.ai_summary.matchAll(/(\d+(?:\.\d+)?)\s?%/g)].map((m) => Number(m[1]));
  const dataNums = [...JSON.stringify(full.data.availability ?? {}).matchAll(/-?\d+(?:\.\d+)?/g)].map((m) => Number(m[0]));
  for (const p of pcts) expect(dataNums.some((n) => Math.abs(n - p) <= 0.01), `percentage ${p} is in the data`).toBe(true);
  await expect.poll(async () => (await smtpMessages()).length, { timeout: 60_000 }).toBeGreaterThan(0);
  const m = (await smtpMessages()).at(-1);
  expect(m.html).toMatch(/AI-generated summary/);
  expect(m.html.indexOf('Availability over the period was')).toBeLessThan(m.html.search(/<table/i));
  expect((await audit(api, 'action=assistant.report_summary'))[0].actor_type).toBe('system');
  await loginViaApi(page, undefined, `/reports/runs/${full.id}`);
  await expect(page.getByText('Availability over the period was').first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/AI-generated summary/).first()).toBeVisible();
  await shot3b(page, '77-report-en');
});

test('he: Hebrew paragraph', async ({ page }) => {
  const full = await run(ids.he);
  expect(full.ai_summary_status).toBe('ok');
  expect(full.data.ai_summary).toMatch(/הזמינות בתקופה/);
  await loginViaApi(page, undefined, `/reports/runs/${full.id}`);
  await expect(page.getByText(/הזמינות בתקופה/).first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/הזמינות בתקופה/).first()).toHaveCSS('direction', 'rtl');
  await shot3b(page, '77-report-he');
});

test('numbers not in the data -> summary dropped (unverified_numbers); run still ok', async () => {
  await mock.addScenario({ name: 'qa_bad_numbers', purpose: 'report_summary', turns: [{ text: 'Availability was 12.34 % and 56.78 % for everything.' }] });
  const full = await run(ids.en);
  expect(full.status).toBe('ok');
  expect(full.ai_summary_status).toBe('unverified_numbers');
  expect(full.data.ai_summary).toBeNull();
  await mock.clearScenarios();
});

test('mock error -> report without the summary, run status ok (ai_summary_status failed); feature OFF -> disabled', async () => {
  test.setTimeout(150_000);
  await mock.mode('unavailable');
  const full = await run(ids.en);
  expect(full.status).toBe('ok');
  expect(full.ai_summary_status).toBe('failed');
  expect(full.data.ai_summary).toBeNull();
  await mock.clearModes();
  await setFeatures(api, { ai_report_summary: false });
  const off = await run(ids.en);
  expect(off.ai_summary_status).toBe('disabled');
  expect(off.data.ai_summary).toBeNull();
  await setFeatures(api, { ai_report_summary: true });
  // opt-in per report
  await api.patch(`/api/reports/${ids.en}`, { ai_summary: false });
  const since = await lastSeq();
  const noOpt = await run(ids.en);
  expect(noOpt.data.ai_summary).toBeNull();
  expect((await modelCalls(since)).filter((c) => c.purpose === 'report_summary')).toEqual([]);
  await api.patch(`/api/reports/${ids.en}`, { ai_summary: true });
});
