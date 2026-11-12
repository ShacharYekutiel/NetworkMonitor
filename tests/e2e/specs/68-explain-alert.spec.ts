// [3b] Explain this alert (docs/architecture.md §14.8.1, §14.11 spec 68; api.md "[3b] AI features"):
// a device goes down after syslog lines -> Explain on the dashboard problem row -> drawer with the sections and
// links (streamed, purpose explain, gathered input contains the preceding syslog), second click uses the cache
// (no model call), Refresh calls again; feature OFF -> 409 + no button; viewer -> no button.
import { expect, test } from '@playwright/test';
import { VIEWER, apiAs, loginViaApi, type Api } from '../lib/helpers';
import { ensureViewer, raw } from '../lib/lab';
import { sendSyslog, setFeatures } from '../lib/lab3';
import { PHASE3B, ask, audit, ensureAssistant, lastSeq, mock, modelCalls, shot3b , changes , problem } from '../lib/lab3b';

// default mode (not serial): one worker runs the tests in order; a failure restarts the worker (beforeAll is
// idempotent) instead of skipping the remaining tests, so every contract item gets a verdict
test.describe.configure({ mode: 'default' });
test.skip(!PHASE3B, 'phase-3b Bedrock mock not running');

let api: Api;
let p: Awaited<ReturnType<typeof problem>>;
const IP = '127.0.0.139';
const SECTIONS = [/What happened/, /What preceded it/, /Affected/, /Likely cause/, /Next checks/];

test.beforeAll(async () => {
  test.setTimeout(180_000);
  api = await apiAs();
  await ensureAssistant(api);
  await mock.clearModes();
  await setFeatures(api, { syslog: true, ai_explain_alert: true });
  p = await problem(api, IP, 'qa3b-explain-web');
  await p.up();
  sendSyslog(IP, ['<11>Nov 10 10:00:00 qa3b-explain-web kernel: QA3B-EXPLAIN uplink eth0 link down']);
  await new Promise((r) => setTimeout(r, 2000));
  await p.down();
});
test.afterAll(async () => { await p?.ctl('ok'); await p?.stop(); });

test('Explain on the dashboard problem row -> drawer with the five sections, links and the AI label', async ({ page }) => {
  test.setTimeout(120_000);
  const since = await lastSeq();
  await loginViaApi(page, undefined, '/');
  const row = page.locator('main').getByText('qa3b-explain-web').first().locator('xpath=ancestor::*[self::tr or self::li][1]');
  await row.getByTestId('explain-button').click();
  const drawer = page.getByRole('dialog').last();
  await expect(drawer).toBeVisible();
  for (const s of SECTIONS) await expect(drawer.getByTestId('answer').last()).toContainText(s, { timeout: 60_000 });
  await expect(drawer.getByTestId('ai-notice')).toBeVisible();
  await expect(drawer.getByRole('link', { name: /Continue in Ask|assistant\.continueInAsk/i })).toBeVisible();
  await shot3b(page, '68-explain-drawer');
  const calls = await modelCalls(since);
  expect(calls[0].purpose).toBe('explain');
  expect(calls[0].offered_tools, 'ask tools available for follow-up lookups').toContain('list_events');
  const input = JSON.stringify(calls[0].body.messages[0]);
  expect(input, 'gathered input has the sensor').toContain(String(p.sensor.id));
  expect(input, 'preceding syslog of the device is in the input').toContain('QA3B-EXPLAIN');
  expect(input.length, 'compact input ≤ 24 KiB').toBeLessThan(24 * 1024 + 4096);
  expect(changes((await audit(api, 'action=assistant.explain_alert'))[0])).toMatchObject({ sensor_id: p.sensor.id });
});

test('second click within 30 min uses the cache (no model call); Refresh asks again', async ({ page }) => {
  const since = await lastSeq();
  const r = await ask(api, '', null, '/api/assistant/explain', { sensor_id: p.sensor.id, event_id: null, refresh: false });
  expect(r.status).toBe(200);
  expect(r.events.map((e) => e.event)).toEqual(['start', 'done']);
  expect((await modelCalls(since)).length).toBe(0);
  const conv = await api.get(`/api/assistant/conversations/${r.start.conversation_id}`);
  expect(conv).toMatchObject({ kind: 'explain', ref: { sensor_id: p.sensor.id } });
  // UI: the cached state is shown in the drawer ("Explained <time>" + Refresh), not on the button itself
  const s1 = await lastSeq();
  await loginViaApi(page, undefined, `/sensors/${p.sensor.id}`);
  await page.getByTestId('explain-button').first().click();
  const drawer = page.getByRole('dialog').last();
  await expect(drawer.getByText(/Explained|explain\.explainedAt/).first()).toBeVisible({ timeout: 20_000 });
  await expect(drawer.getByRole('button', { name: /Refresh|explain\.refresh/ })).toBeVisible();
  expect((await modelCalls(s1)).length, 'second click uses the cache').toBe(0);
  const r2 = await ask(api, '', null, '/api/assistant/explain', { sensor_id: p.sensor.id, event_id: null, refresh: true });
  expect(r2.message.status).toBe('complete');
  expect((await modelCalls(since)).length).toBeGreaterThan(0);
});

test('a follow-up question can continue the explain conversation', async () => {
  const convs = await api.get('/api/assistant/conversations?kind=explain');
  const id = convs.items[0].id;
  const r = await ask(api, 'What about the second check?', id);
  expect(r.message.status).toBe('complete');
  expect(r.message.conversation_id).toBe(id);
});

test('feature OFF -> 409 feature_disabled and no Explain button; viewer never sees it; 404 unknown sensor', async ({ page }) => {
  expect((await raw(api, 'POST', '/api/assistant/explain', { sensor_id: 99999999, event_id: null, refresh: false })).status).toBe(404);
  await setFeatures(api, { ai_explain_alert: false });
  const r = await raw(api, 'POST', '/api/assistant/explain', { sensor_id: p.sensor.id, event_id: null, refresh: false });
  expect(r.status).toBe(409);
  expect(r.body.error).toMatchObject({ code: 'feature_disabled', details: [expect.objectContaining({ feature: 'ai_explain_alert' })] });
  await loginViaApi(page, undefined, `/sensors/${p.sensor.id}`);
  await expect(page.getByText('qa3b-explain-web').first()).toBeVisible();
  await expect(page.getByTestId('explain-button')).toHaveCount(0);
  await setFeatures(api, { ai_explain_alert: true });
  await ensureViewer(api);
  const viewer = await apiAs(VIEWER);
  expect((await raw(viewer, 'POST', '/api/assistant/explain', { sensor_id: p.sensor.id, event_id: null, refresh: false })).status).toBe(403);
});
