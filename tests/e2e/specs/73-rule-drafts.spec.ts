// [3b] Rule drafts from a description (docs/architecture.md §14.8.6, §14.11 spec 73; api.md RuleDraft): "alert me
// on Telegram when any core switch is down for 2 minutes" -> draft (forced propose_alert_rule; read tools incl.
// list_notification_channels id/name/type only) validated with the normal model, NOT saved; the normal form opens
// prefilled with the banner; saving goes through the normal POST /api/alert-rules (audited as usual). Event-rule
// draft -> the event-rule form with Test.
import { expect, test } from '@playwright/test';
import { apiAs, loginViaApi, type Api } from '../lib/helpers';
import { TG_CHAT_OK, TG_VALID_TOKEN, raw } from '../lib/lab';
import { setFeatures, tx } from '../lib/lab3';
import { PHASE3B, audit, ensureAssistant, lastSeq, mock, modelCalls, shot3b } from '../lib/lab3b';

// default mode (not serial): one worker runs the tests in order; a failure restarts the worker (beforeAll is
// idempotent) instead of skipping the remaining tests, so every contract item gets a verdict
test.describe.configure({ mode: 'default' });
test.skip(!PHASE3B, 'phase-3b Bedrock mock not running');

let api: Api;
let tg = 0;
const DESC = 'alert me on Telegram when any core switch is down for 2 minutes';

test.beforeAll(async () => {
  api = await apiAs();
  await ensureAssistant(api);
  await mock.clearModes();
  await setFeatures(api, { ai_rule_drafts: true, syslog: true });
  const existing = (await api.get('/api/notification-channels')).items?.find((c: any) => c.name === 'qa3b-tg-drafts');
  tg = existing?.id ?? (await api.post('/api/notification-channels', { name: 'qa3b-tg-drafts', type: 'telegram', enabled: true,
    config: { bot_token: TG_VALID_TOKEN, chat_id: TG_CHAT_OK, parse_mode: 'HTML' } })).id;
});

test('API: alert-rule draft = writable fields (delay 120, Telegram channel), validated, nothing saved', async () => {
  const rulesBefore = (await api.get('/api/alert-rules')).items?.length ?? (await api.get('/api/alert-rules')).length;
  const since = await lastSeq();
  const d = await api.post('/api/assistant/rule-drafts', { kind: 'alert_rule', description: DESC });
  expect(d.kind).toBe('alert_rule');
  expect(d.draft).toMatchObject({ on_statuses: ['down'], delay_s: 120 });
  const chans = (await api.get('/api/notification-channels')).items;
  expect(d.draft.channel_ids.length).toBe(1);
  expect(chans.find((c: any) => c.id === d.draft.channel_ids[0])?.type, 'a Telegram channel').toBe('telegram');
  expect(Object.keys(d.draft)).not.toContain('explanation');
  expect(d.explanation.length).toBeGreaterThan(5);
  expect(d.assumptions.length).toBeGreaterThan(0);
  expect(d.validation).toEqual({ ok: true, errors: [] });
  const rulesAfter = (await api.get('/api/alert-rules')).items?.length ?? (await api.get('/api/alert-rules')).length;
  expect(rulesAfter, 'the draft endpoint saves nothing').toBe(rulesBefore);
  const calls = (await modelCalls(since)).filter((c) => c.purpose === 'rule_draft');
  expect(calls[0].offered_tools).toEqual(expect.arrayContaining(['search_devices', 'search_sensors', 'list_groups', 'list_notification_channels']));
  const final = calls.at(-1);
  // the draft comes from propose_alert_rule: forced on the final call, or already called by the model
  const proposed = calls.some((c) => c.forced_tool === 'propose_alert_rule' || (c.response_blocks ?? []).some((b: any) => b.toolUse?.name === 'propose_alert_rule'));
  expect(proposed).toBe(true);
  const schema = final.body.toolConfig.tools.find((t: any) => t.toolSpec?.name === 'propose_alert_rule').toolSpec.inputSchema.json;
  for (const k of ['name', 'on_statuses', 'delay_s', 'channel_ids']) expect(Object.keys(schema.properties)).toContain(k);
  // list_notification_channels never carries channel configs (tokens / URLs)
  const blob = JSON.stringify(calls);
  expect(blob).not.toContain(TG_VALID_TOKEN);
  expect((await audit(api, 'action=assistant.rule_draft'))[0]).toBeTruthy();
});

test('invalid draft -> one repair round with the validation errors sent back to the model', async () => {
  await mock.addScenario({ name: 'qa_bad_draft', purpose: 'rule_draft', match: 'repair-me|error|invalid|validation', turns: [
    { tool_use: [{ name: 'propose_alert_rule', input: { name: '', on_statuses: ['down'], delay_s: 120, enabled: true, channel_ids: [] } }] }] });
  const since = await lastSeq();
  const d = await api.post('/api/assistant/rule-drafts', { kind: 'alert_rule', description: 'repair-me please, alert on down' });
  const calls = (await modelCalls(since)).filter((c) => c.purpose === 'rule_draft');
  expect(calls.length, 'first call + one repair call').toBe(2);
  expect(JSON.stringify(calls[1].body.messages.at(-1))).toMatch(/channel_ids|name/);
  expect(d.validation.ok).toBe(false);
  expect(d.validation.errors.length).toBeGreaterThan(0);
  await mock.clearScenarios();
});

test('UI: Describe the rule -> Draft -> normal form prefilled + banner -> save via the normal API', async ({ page }) => {
  await loginViaApi(page, undefined, '/notifications');
  await page.getByRole('button', { name: /New alert rule|Add rule|alertRules\.new/i }).first().click().catch(() => undefined);
  const describe = page.getByTestId('describe-rule');
  await expect(describe).toBeVisible({ timeout: 20_000 });
  await describe.click();
  await page.getByTestId('describe-text').fill(DESC);
  await page.getByTestId('draft-button').click();
  await expect(page.getByTestId('draft-banner')).toContainText(/Review every field|draft\.banner/i, { timeout: 60_000 });
  await shot3b(page, '73-alert-draft');
  await expect(page.locator('form').getByText('qa3b-tg-drafts').first()).toBeVisible();
  const created = page.waitForResponse((r) => r.url().endsWith('/api/alert-rules') && r.request().method() === 'POST');
  await page.getByRole('button', { name: /^(Save|Create|common\.save)$/ }).last().click();
  const resp = await created;
  expect(resp.status()).toBe(201);
  const rule = await resp.json();
  expect(rule).toMatchObject({ delay_s: 120, on_statuses: ['down'] });
  expect(rule.channel_ids.length).toBe(1);
  expect((await audit(api, 'action=alert_rule.create'))[0].entity_id).toBe(rule.id);
});

test('event-rule draft (syslog) -> valid EventRule fields; the event-rule form highlights Test', async ({ page }) => {
  const d = await api.post('/api/assistant/rule-drafts', { kind: 'event_rule', description: 'raise a warning on syslog login failures' });
  expect(d.kind).toBe('event_rule');
  expect(d.draft).toMatchObject({ kind: 'syslog', action: 'warning', scope: 'global' });
  expect(d.validation.ok).toBe(true);
  const t = await api.post('/api/event-rules/test', { rule: d.draft, hours: 24 });
  expect(t.scanned).toBeGreaterThanOrEqual(0);
  await loginViaApi(page, undefined, '/logs?tab=rules');
  await page.getByRole('button', { name: /New rule|eventRules\.new|Add/i }).first().click().catch(() => undefined);
  await page.getByTestId('describe-rule').click();
  await page.getByTestId('describe-text').fill('raise a warning on syslog login failures');
  await page.getByTestId('draft-button').click();
  await expect(page.getByTestId('draft-banner')).toBeVisible({ timeout: 60_000 });
  await expect(page.getByRole('button', { name: tx('eventRules.test', /^Test( against the last 24 h)?$/i) }).first()).toBeVisible();
});

// [3b owner] "make me coffee" -> the explicit cannot-draft outcome (200, draft null, reason in the description's
// language); the UI shows the reason + an example, never a broken form.
const COFFEE = 'תכין לי קפה';
const SSH_HE = 'התרע כשמישהו נכשל בהתחברות SSH למתג יותר מ-3 פעמים ב-5 דקות';
const isHebrew = (s: string) => /[֐-׿]/.test(s);

test('cannot draft: Hebrew nonsense -> 200 {draft: null, cannot_draft: {reason (he)}}, no repair round', async () => {
  for (const kind of ['alert_rule', 'event_rule']) {
    const since = await lastSeq();
    const d = await api.post('/api/assistant/rule-drafts', { kind, description: COFFEE });
    expect(d.draft).toBeNull();
    expect(d.cannot_draft).toMatchObject({ source: 'model' });
    expect(isHebrew(d.cannot_draft.reason)).toBe(true);
    expect(d.language).toBe('he');
    expect(d.validation).toEqual({ ok: false, errors: [] });
    const calls = (await modelCalls(since)).filter((c) => c.purpose === 'rule_draft');
    expect(calls.length).toBe(1);
    expect(calls[0].question).toContain('(Answer in Hebrew.)');
  }
});

test('UI: cannot draft shows the reason and an example, the form stays empty', async ({ page }) => {
  await loginViaApi(page, undefined, '/logs?tab=rules');
  await page.getByRole('button', { name: /New rule|eventRules\.new|Add/i }).first().click().catch(() => undefined);
  await page.getByTestId('describe-rule').click();
  await page.getByTestId('describe-text').fill(COFFEE);
  await page.getByTestId('draft-button').click();
  const notice = page.getByTestId('cannot-draft');
  await expect(notice).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId('cannot-draft-reason')).toContainText('קפה');
  await expect(notice).toContainText(tx('draft.cannotExampleEvent', /SSH login failure/i));
  await expect(page.getByTestId('draft-banner')).toHaveCount(0);
  await shot3b(page, '73-cannot-draft');
});

test('Hebrew description -> Hebrew name/explanation; the drafted regex reaches the form Text field and the saved rule', async ({ page }) => {
  const d = await api.post('/api/assistant/rule-drafts', { kind: 'event_rule', description: SSH_HE });
  expect(d.cannot_draft).toBeNull();
  expect(d.language).toBe('he');
  expect(isHebrew(d.draft.name) && isHebrew(d.explanation)).toBe(true);
  expect(d.draft.match).toMatchObject({ text: '[Ff]ail(ed|ure)?', text_regex: true });
  await loginViaApi(page, undefined, '/logs?tab=rules');
  await page.getByRole('button', { name: /New rule|eventRules\.new|Add/i }).first().click().catch(() => undefined);
  await page.getByTestId('describe-rule').click();
  await page.getByTestId('describe-text').fill(SSH_HE);
  await page.getByTestId('draft-button').click();
  await expect(page.getByTestId('draft-banner')).toBeVisible({ timeout: 60_000 });
  const editor = page.getByTestId('rule-editor');
  await expect(editor.getByRole('textbox', { name: /^Text(?! is)/ }).first()).toHaveValue('[Ff]ail(ed|ure)?');
  const created = page.waitForResponse((r) => r.url().endsWith('/api/event-rules') && r.request().method() === 'POST');
  await editor.getByRole('button', { name: /^(Save|common\.save)$/ }).last().click();
  const resp = await created;
  expect(resp.status()).toBe(201);
  const rule = await resp.json();
  expect(rule.match).toMatchObject({ text: '[Ff]ail(ed|ure)?', text_regex: true });
  await raw(api, 'DELETE', `/api/event-rules/${rule.id}`).catch(() => undefined);
});

test('validation and gates: description 5–1000 chars; feature OFF -> 409 and no Describe box', async ({ page }) => {
  expect((await raw(api, 'POST', '/api/assistant/rule-drafts', { kind: 'alert_rule', description: 'x' })).status).toBe(422);
  expect((await raw(api, 'POST', '/api/assistant/rule-drafts', { kind: 'nope', description: 'alert me' })).status).toBe(422);
  await setFeatures(api, { ai_rule_drafts: false });
  const r = await raw(api, 'POST', '/api/assistant/rule-drafts', { kind: 'alert_rule', description: DESC });
  expect(r.body.error.code).toBe('feature_disabled');
  await loginViaApi(page, undefined, '/notifications');
  await page.getByRole('button', { name: /New alert rule|Add rule|alertRules\.new/i }).first().click().catch(() => undefined);
  await expect(page.getByTestId('describe-rule')).toHaveCount(0);
  await setFeatures(api, { ai_rule_drafts: true });
});
