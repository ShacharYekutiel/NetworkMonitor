// [3b] AI note after alert notifications (docs/architecture.md §14.8.3, §14.11 spec 70; api.md "Alert-note
// deliveries"): rule with ai_note -> the alert is sent first (timestamps), then Telegram editMessageText appends
// "AI: …" (reply_markup kept), Teams gets a follow-up card "AI note: DOWN …", email gets a threaded reply
// (In-Reply-To / References = the alert's Message-ID); edit refused -> reply_to_message_id fallback; mock slower
// than alert_note_timeout_s -> alert delivered, no note (skipped timeout); recovery never gets a note.
import { expect, test } from '@playwright/test';
import { apiAs, loginViaApi, type Api } from '../lib/helpers';
import { TG_CHAT_OK, TG_VALID_TOKEN, TELEGRAM_MOCK_URL, clearTelegram } from '../lib/lab';
import { TEAMS_URL, clearSmtp, clearTeams, configureSmtp, setFeatures, smtpMessages, teamsRecords, tgCalls, tgControl, tx } from '../lib/lab3';
import { PHASE3B, audit, ensureAssistant, lastSeq, mock, modelCalls, setScalars, shot3b , problem } from '../lib/lab3b';

// default mode (not serial): one worker runs the tests in order; a failure restarts the worker (beforeAll is
// idempotent) instead of skipping the remaining tests, so every contract item gets a verdict
test.describe.configure({ mode: 'default' });
test.skip(!PHASE3B, 'phase-3b Bedrock mock not running');

let api: Api;
let p: Awaited<ReturnType<typeof problem>>;
let tg = 0, teams = 0, mail = 0, ruleId = 0;
const sentState = async () => (await fetch(`${TELEGRAM_MOCK_URL}/_sent`)).json() as Promise<Record<string, any>>;

test.beforeAll(async () => {
  test.setTimeout(180_000);
  api = await apiAs();
  await ensureAssistant(api);
  await mock.clearModes();
  await configureSmtp(api);
  await setFeatures(api, { ai_alert_notes: true });
  await setScalars(api, { alert_note_timeout_s: 45, alert_notes_daily_cap: 100 });
  tg = (await api.post('/api/notification-channels', { name: 'qa3b-tg-notes', type: 'telegram', enabled: true,
    config: { bot_token: TG_VALID_TOKEN, chat_id: TG_CHAT_OK, parse_mode: 'HTML', disable_notification: false } })).id;
  teams = (await api.post('/api/notification-channels', { name: 'qa3b-teams-notes', type: 'teams', enabled: true, config: { url: TEAMS_URL() } })).id;
  mail = (await api.post('/api/notification-channels', { name: 'qa3b-mail-notes', type: 'email', enabled: true, config: { to: ['noc@mnidom.test'], ai_note_email: true } })).id;
  p = await problem(api, '127.0.0.145', 'qa3b-notes-web');
  await p.up();
  const r = await api.post('/api/alert-rules', { name: 'qa3b ai note', enabled: true, sensor_id: p.sensor.id, on_statuses: ['down', 'up'], delay_s: 0, channel_ids: [tg, teams, mail], ai_note: true });
  ruleId = r.id;
  expect(r.ai_note).toBe(true);
});
test.afterAll(async () => {
  await tgControl({ edit_too_old: false });
  await p?.ctl('ok'); await p?.stop();
});

test('problem: alert first, then the AI note on Telegram (edit), Teams (follow-up card) and email (threaded reply)', async () => {
  test.setTimeout(180_000);
  await clearTelegram(); await clearTeams(); await clearSmtp();
  const since = await lastSeq();
  await p.down();
  // Telegram: the alert message, then an edit of the same message with "AI:"
  await expect.poll(async () => (await tgCalls('editMessageText')).length, { timeout: 90_000, message: 'Telegram edit' }).toBeGreaterThan(0);
  const send = (await tgCalls('sendMessage')).find((m) => /qa3b-notes-web/.test(m.body.text));
  const edit = (await tgCalls('editMessageText'))[0];
  expect(send, 'alert sent').toBeTruthy();
  expect(edit.body.message_id).toBe(send!.response.result.message_id);
  expect(edit.body.parse_mode).toBe('HTML');
  expect(edit.body.text.startsWith(send!.body.text)).toBe(true);
  expect(edit.body.text).toMatch(/\n\n<i>AI:<\/i> .+/);
  expect(edit.status).toBe(200);
  if (send!.body.reply_markup) expect(edit.body.reply_markup, 'reply_markup kept').toEqual(send!.body.reply_markup);
  // the model was called after the alert was sent (never delays it), once per problem, light/non-streamed
  const calls = (await modelCalls(since)).filter((c) => c.purpose === 'alert_note');
  expect(calls.length, 'one note per problem event (shared by all channels)').toBe(1);
  expect(calls[0].op).toBe('converse');
  expect(calls[0].time * 1000).toBeGreaterThanOrEqual(send!.time * 1000 - 50);
  expect(calls[0].body.inferenceConfig.maxTokens).toBeLessThanOrEqual(200);
  expect(JSON.stringify(calls[0].body)).toContain('qa3b-notes-web');
  // Teams follow-up card
  await expect.poll(async () => (await teamsRecords()).length, { timeout: 60_000 }).toBeGreaterThanOrEqual(2);
  const cards = JSON.stringify(await teamsRecords());
  expect(cards).toMatch(/AI note: DOWN/);
  // email: reply threaded to the alert
  await expect.poll(async () => (await smtpMessages()).length, { timeout: 60_000 }).toBeGreaterThanOrEqual(2);
  const mails = await smtpMessages();
  const alert = mails.find((m) => !/^Re:/.test(m.subject))!;
  const reply = mails.find((m) => /^Re:/.test(m.subject))!;
  expect(reply.subject).toBe(`Re: ${alert.subject}`);
  const mid = alert.headers['Message-ID'] ?? alert.headers['Message-Id'];
  expect(mid).toBeTruthy();
  expect(reply.headers['In-Reply-To']).toBe(mid);
  expect(reply.headers.References).toContain(mid);
  expect(reply.text + reply.html).toMatch(/AI-generated/);
  const a = (await audit(api, 'action=assistant.alert_note'))[0];
  expect(a.actor_type).toBe('system');
  expect(JSON.stringify(a.changes)).toContain(String(ruleId));
});

test('recovery is not noted (problem stage only)', async () => {
  const since = await lastSeq();
  await clearTelegram();
  await p.up();
  await expect.poll(async () => (await tgCalls('sendMessage')).filter((m) => /qa3b-notes-web/.test(m.body.text)).length, { timeout: 60_000 }).toBe(1);
  await new Promise((r) => setTimeout(r, 8000));
  expect((await modelCalls(since)).filter((c) => c.purpose === 'alert_note')).toEqual([]);
  expect(await tgCalls('editMessageText')).toEqual([]);
});

test('Telegram edit refused -> sendMessage replying to the alert', async () => {
  test.setTimeout(150_000);
  await clearTelegram();
  await tgControl({ edit_too_old: true });
  await p.down();
  await expect.poll(async () => (await tgCalls('sendMessage')).filter((m) => m.body.reply_to_message_id).length, { timeout: 90_000 }).toBe(1);
  const alert = (await tgCalls('sendMessage')).find((m) => /qa3b-notes-web/.test(m.body.text) && !m.body.reply_to_message_id)!;
  const note = (await tgCalls('sendMessage')).find((m) => m.body.reply_to_message_id)!;
  expect(note.body.reply_to_message_id).toBe(alert.response.result.message_id);
  expect(note.body.text).toMatch(/AI note:/);
  expect(note.status).toBe(200);
  await tgControl({ edit_too_old: false });
  await p.up();
});

test('mock slower than alert_note_timeout_s -> the alert is delivered on time, no note (skipped: timeout)', async () => {
  test.setTimeout(180_000);
  await setScalars(api, { alert_note_timeout_s: 10 });
  await mock.mode('slow:20');
  await clearTelegram(); await clearTeams(); await clearSmtp();
  const t0 = Date.now();
  await p.down();
  await expect.poll(async () => (await tgCalls('sendMessage')).filter((m) => /qa3b-notes-web/.test(m.body.text)).length, { timeout: 60_000 }).toBe(1);
  const sent = (await tgCalls('sendMessage')).find((m) => /qa3b-notes-web/.test(m.body.text))!;
  expect(sent.time * 1000 - t0, 'alert not delayed by the model').toBeLessThan(45_000);
  await new Promise((r) => setTimeout(r, 25_000));
  expect(await tgCalls('editMessageText')).toEqual([]);
  expect((await teamsRecords()).filter((r) => /AI note/.test(JSON.stringify(r)))).toEqual([]);
  const a = (await audit(api, 'action=assistant.alert_note'))[0];
  expect(JSON.stringify(a.changes)).toMatch(/timeout/);
  await mock.clearModes();
  await setScalars(api, { alert_note_timeout_s: 45 });
  await p.up();
});

test('UI: rule form shows the AI note checkbox while the feature is ON', async ({ page }) => {
  await loginViaApi(page, undefined, '/notifications');
  await page.getByRole('button', { name: tx('rules.add', /Add rule/i) }).click();
  const dlg = page.getByRole('dialog').last();
  await expect(dlg.getByText(tx('rules.aiNote', /AI note/i)).first()).toBeVisible({ timeout: 20_000 });
  await shot3b(page, '70-rule-ai-note');
  await setFeatures(api, { ai_alert_notes: false });
  const r = await api.get(`/api/alert-rules/${ruleId}`);
  expect(r.ai_note, 'flag kept while the feature is OFF').toBe(true);
  await setFeatures(api, { ai_alert_notes: true });
});
