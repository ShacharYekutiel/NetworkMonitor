import { expect, test, type Page } from '@playwright/test';
import { Api, HTTP_TARGET_URL, MIN_INTERVAL, apiAs, card, defaultGroupId, httpTarget, loginViaApi, uid, waitForSensor } from '../lib/helpers';
import {
  TG_BAD_TOKEN,
  TG_CHAT_OK,
  TG_VALID_TOKEN,
  backendLog,
  clearTelegram,
  raw,
  telegramDelivered,
  telegramMessages,
} from '../lib/lab';

// Phase 2a (e): Telegram channel against the Bot API mock (tests/sim/telegram_mock.py, NETMON_TELEGRAM_API_BASE):
// create + test, bad token -> 502 telegram_error shown, alert rule -> down + recovery messages, and the bot
// token never appears in any API response (or the backend log).
test.describe.configure({ mode: 'serial' });

let api: Api;
const chName = uid('QA Telegram');
const badName = uid('QA Telegram bad');
let channelId = 0;
let badId = 0;
const apiBodies: string[] = [];

function recordApiResponses(page: Page) {
  page.on('response', async (r) => {
    if (!r.url().includes('/api/')) return;
    try {
      apiBodies.push(await r.text());
    } catch {
      /* redirects / aborted */
    }
  });
}

test.beforeAll(async () => {
  api = await apiAs();
  await httpTarget('ok');
});
test.afterAll(async () => {
  await httpTarget('ok');
});

test('create a Telegram channel in the UI and send a test message', async ({ page }) => {
  recordApiResponses(page);
  await clearTelegram();
  await loginViaApi(page, undefined, '/notifications');
  await page.getByRole('button', { name: 'Add channel' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel(/^Name/).fill(chName);
  await dlg.getByLabel('Type').selectOption('telegram');
  const token = dlg.getByLabel(/^Bot token/);
  await expect(token).toHaveAttribute('type', 'password');
  await token.fill(TG_VALID_TOKEN);
  await dlg.getByLabel(/^Chat ID/i).fill(TG_CHAT_OK);
  await expect(dlg.getByText(/-100… for groups\/channels/).first()).toBeVisible();
  await expect(dlg.getByLabel(/^Message format/).locator('option:checked')).toHaveText('Formatted (HTML)');
  await dlg.getByRole('button', { name: 'Save' }).click();
  await expect(dlg).toBeHidden();

  const row = card(page, 'Notification channels').getByRole('row').filter({ hasText: chName });
  await expect(row).toContainText('Telegram');
  await row.getByRole('button', { name: 'Send test' }).click();
  await expect(page.getByText(`${chName}: test sent`)).toBeVisible();

  await expect.poll(async () => (await telegramDelivered()).length, { timeout: 15_000 }).toBe(1);
  const m = (await telegramDelivered())[0];
  expect(m.token).toBe(TG_VALID_TOKEN);
  expect(m.body).toMatchObject({ chat_id: TG_CHAT_OK, parse_mode: 'HTML', disable_web_page_preview: true, disable_notification: false });
  expect(m.body.text.split('\n')[0]).toBe('<b>[NetworkMonitor] Test notification</b>');

  const list = await api.get('/api/notification-channels');
  const ch = list.items.find((c: any) => c.name === chName);
  channelId = ch.id;
  expect(ch).toMatchObject({ type: 'telegram', enabled: true });
  expect(ch.config).toEqual({ bot_token: null, bot_token_set: true, chat_id: TG_CHAT_OK, parse_mode: 'HTML', disable_notification: false, ack_buttons: false, ack_link: false }); // [3] chat-ack keys (data-model § [3] Channel config additions)
});

test('editing keeps the stored token (write-only field)', async ({ page }) => {
  recordApiResponses(page);
  await loginViaApi(page, undefined, '/notifications');
  const row = card(page, 'Notification channels').getByRole('row').filter({ hasText: chName });
  await row.getByRole('button', { name: 'Edit' }).click();
  const dlg = page.getByRole('dialog');
  const token = dlg.getByLabel(/^Bot token/);
  await expect(token).toHaveValue('');
  await expect(token).toHaveAttribute('placeholder', /stored/i);
  await dlg.getByLabel(/Silent/i).check();
  await dlg.getByRole('button', { name: 'Save' }).click();
  await expect(dlg).toBeHidden();
  const ch = await api.get(`/api/notification-channels/${channelId}`);
  expect(ch.config).toMatchObject({ bot_token: null, bot_token_set: true, disable_notification: true });
  await clearTelegram();
  expect(await api.post(`/api/notification-channels/${channelId}/test`)).toEqual({ ok: true, message: 'sent' });
  const m = (await telegramDelivered())[0];
  expect(m.token).toBe(TG_VALID_TOKEN); // still the original token
  expect(m.body.disable_notification).toBe(true);
  await api.patch(`/api/notification-channels/${channelId}`, {
    config: { chat_id: TG_CHAT_OK, parse_mode: 'HTML', disable_notification: false },
  });
});

test('wrong token -> test shows the Telegram error (502 telegram_error)', async ({ page }) => {
  recordApiResponses(page);
  await loginViaApi(page, undefined, '/notifications');
  await page.getByRole('button', { name: 'Add channel' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel(/^Name/).fill(badName);
  await dlg.getByLabel('Type').selectOption('telegram');
  await dlg.getByLabel(/^Bot token/).fill(TG_BAD_TOKEN);
  await dlg.getByLabel(/^Chat ID/i).fill(TG_CHAT_OK);
  await dlg.getByRole('button', { name: 'Save' }).click();
  await expect(dlg).toBeHidden();
  const row = card(page, 'Notification channels').getByRole('row').filter({ hasText: badName });
  const resp = page.waitForResponse((r) => r.url().includes('/test') && r.request().method() === 'POST');
  await row.getByRole('button', { name: 'Send test' }).click();
  const r = await resp;
  expect(r.status()).toBe(502);
  const body = await r.json();
  expect(body.error).toMatchObject({ code: 'telegram_error', message: 'Unauthorized' });
  await expect(page.getByText(/Unauthorized/).first()).toBeVisible();

  badId = (await api.get('/api/notification-channels')).items.find((c: any) => c.name === badName).id;
  // Other Telegram errors come through verbatim, never with the token.
  await api.patch(`/api/notification-channels/${badId}`, { config: { bot_token: TG_VALID_TOKEN, chat_id: '404', parse_mode: 'HTML', disable_notification: false } });
  const nf = await raw(api, 'POST', `/api/notification-channels/${badId}/test`);
  expect(nf.status).toBe(502);
  expect(nf.body.error).toMatchObject({ code: 'telegram_error', message: 'Bad Request: chat not found' });
  await api.patch(`/api/notification-channels/${badId}`, { config: { chat_id: '429', parse_mode: null, disable_notification: false } });
  const rl = await raw(api, 'POST', `/api/notification-channels/${badId}/test`);
  expect(rl.body.error).toMatchObject({ code: 'telegram_error', message: 'Too Many Requests: retry after 5' });
  for (const x of [nf, rl]) expect(x.text).not.toContain(TG_VALID_TOKEN);
  // Timeout path: the mock sleeps 15 s for chat "-1009" (alias of the doc's "slow", which the chat_id
  // validation rejects); the client gives up after 10 s.
  await api.patch(`/api/notification-channels/${badId}`, { config: { chat_id: '-1009', parse_mode: 'HTML', disable_notification: false } });
  const t0 = Date.now();
  const slow = await raw(api, 'POST', `/api/notification-channels/${badId}/test`);
  expect(slow.status).toBe(502);
  expect(slow.body.error).toMatchObject({ code: 'telegram_error', message: 'Telegram request timed out' });
  expect(Date.now() - t0).toBeLessThan(14_000);
  expect(slow.text).not.toContain(TG_VALID_TOKEN);
});

test('plain-text parse mode omits parse_mode', async () => {
  await clearTelegram();
  await api.patch(`/api/notification-channels/${badId}`, { config: { chat_id: '@netmon_test', parse_mode: null, disable_notification: false } });
  expect(await api.post(`/api/notification-channels/${badId}/test`)).toEqual({ ok: true, message: 'sent' });
  const m = (await telegramDelivered())[0];
  expect(m.body.chat_id).toBe('@netmon_test');
  expect(m.body).not.toHaveProperty('parse_mode');
  expect(m.body.text).toMatch(/^\[NetworkMonitor\] Test notification\n\n/);
  await api.del(`/api/notification-channels/${badId}`);
});

test('validation: token required and well-formed, chat_id must be a string', async () => {
  const base = { name: uid('tg-invalid'), type: 'telegram', enabled: true };
  const cases: [string, any][] = [
    ['no token', { chat_id: TG_CHAT_OK }],
    ['empty token', { bot_token: '', chat_id: TG_CHAT_OK }],
    ['malformed token', { bot_token: 'abc', chat_id: TG_CHAT_OK }],
    ['numeric chat_id', { bot_token: TG_VALID_TOKEN, chat_id: -1001 }],
    ['bad chat_id', { bot_token: TG_VALID_TOKEN, chat_id: 'not a chat' }],
  ];
  for (const [label, config] of cases) {
    const r = await raw(api, 'POST', '/api/notification-channels', { ...base, config });
    expect.soft(r.status, label).toBe(422);
    expect.soft(r.text, label).not.toContain(TG_VALID_TOKEN);
  }
  const noTok = await raw(api, 'POST', '/api/notification-channels', { ...base, config: { chat_id: TG_CHAT_OK } });
  expect(JSON.stringify(noTok.body.error.details)).toContain('bot_token');
  const empty = await raw(api, 'PATCH', `/api/notification-channels/${channelId}`, { config: { bot_token: '', chat_id: TG_CHAT_OK } });
  expect(empty.status).toBe(422);
});

test('alert rule with Telegram: HTTP target down -> message; recovery -> message', async () => {
  const port = new URL(HTTP_TARGET_URL).port;
  // "<R&D>" in the device name: the HTML message must escape it (the mock rejects unparsable HTML).
  const devName = `${uid('tg-web')} <R&D>`;
  const d = await api.post('/api/devices', { group_id: await defaultGroupId(api), name: devName, host: '127.0.0.1', auto_sensors: [] });
  const s = await api.post('/api/sensors', {
    device_id: d.id,
    name: 'Web via Telegram',
    type: 'http',
    interval_s: MIN_INTERVAL,
    config: { url: `http://{host}:${port}/`, keyword: 'sim OK' },
  });
  await waitForSensor(api, s.id, (x) => x.status === 'up', 45_000, 'http up');
  const rule = await api.post('/api/alert-rules', {
    name: uid('tg rule'),
    sensor_id: s.id,
    on_statuses: ['down', 'up'],
    delay_s: 0,
    enabled: true,
    channel_ids: [channelId],
  });
  await clearTelegram();

  await httpTarget('fail');
  await waitForSensor(api, s.id, (x) => x.status === 'down', 45_000, 'http down');
  await expect.poll(async () => (await telegramDelivered()).length, { timeout: 20_000, message: 'telegram down message' }).toBe(1);
  const down = (await telegramDelivered())[0];
  expect(down.body.chat_id).toBe(TG_CHAT_OK);
  expect(down.body.parse_mode).toBe('HTML');
  expect(down.body.text).toMatch(/^<b>\[NetworkMonitor\] DOWN: .*Web via Telegram<\/b>/);
  expect(down.body.text).toContain('503');
  expect(down.body.text).toContain('&lt;R&amp;D&gt;');

  await httpTarget('ok');
  await waitForSensor(api, s.id, (x) => x.status === 'up', 45_000, 'http up again');
  await expect.poll(async () => (await telegramDelivered()).length, { timeout: 20_000, message: 'telegram recovery message' }).toBe(2);
  const up = (await telegramDelivered())[1];
  expect(up.body.text).toMatch(/^<b>\[NetworkMonitor\] UP: .*Web via Telegram<\/b>/);
  // Every request the backend sent was accepted by the mock (valid HTML etc.), and no failures were logged.
  expect((await telegramMessages()).filter((m) => m.status !== 200)).toEqual([]);
  const ev = await api.get('/api/events?kind=notification');
  expect(ev.items.filter((e: any) => e.message.includes(chName))).toEqual([]);
  await api.del(`/api/alert-rules/${rule.id}`);
  await api.del(`/api/devices/${d.id}`);
});

test('bot_token never appears in any API response or the backend log', async () => {
  expect(apiBodies.length).toBeGreaterThan(5);
  const leaks = apiBodies.filter((b) => b.includes(TG_VALID_TOKEN) || b.includes(TG_BAD_TOKEN));
  expect(leaks).toEqual([]);
  for (const p of ['/api/notification-channels', `/api/notification-channels/${channelId}`, '/api/alert-rules', '/api/events?page_size=1000']) {
    expect((await raw(api, 'GET', p)).text, p).not.toContain('TEST_TOKEN');
  }
  const log = backendLog();
  test.skip(!log, 'BACKEND_LOG not available (run through tests/e2e/run.sh)');
  expect(log).not.toContain(TG_VALID_TOKEN);
  expect(log).not.toContain(TG_BAD_TOKEN);
});
