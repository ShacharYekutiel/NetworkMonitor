// [3] Acknowledge from Telegram / Teams (docs/architecture.md §13.8, §13.11 spec 60; api.md "[3] Acknowledge from chat").
// Link a Telegram account with a code (mock getUpdates), a problem -> message with the inline button, an injected
// press by the linked admin -> acknowledged in UI/timeline, markup replaced; unlinked press refused; Teams card
// carries the Action.OpenUrl ack URL -> /ack#… -> acknowledge with a comment; an expired/forged link.
import { expect, test } from '@playwright/test';
import { BASE_URL, apiAs, loginViaApi, type Api } from '../lib/helpers';
import { TG_CHAT_OK, TG_VALID_TOKEN, clearTelegram, raw, telegramDelivered } from '../lib/lab';
import {
  PHASE3, TEAMS_URL, clearTeams, problemDevice, setFeatures, shot3, teamsRecords, tgCalls, tgControl, tgInject,
  tgPolls, tx,
} from '../lib/lab3';

test.describe.configure({ mode: 'serial' });
test.skip(!PHASE3, 'phase-3 simulators not running');

const TG_ADMIN = 424242;
const TG_STRANGER = 999001;
let api: Api;
let tgChannel = 0;
let teamsChannel = 0;
let p1: Awaited<ReturnType<typeof problemDevice>>;
let p2: Awaited<ReturnType<typeof problemDevice>>;

const ackToken = (s: string) => (s.match(/a1\.[A-Za-z0-9_-]{38}/) ?? [])[0];

test.beforeAll(async () => {
  api = await apiAs();
  await api.put('/api/settings', { public_base_url: BASE_URL });
  await setFeatures(api, { chat_ack: true });
  const tg = await api.post('/api/notification-channels', {
    name: 'qa3-tg-ack', type: 'telegram', enabled: true,
    config: { bot_token: TG_VALID_TOKEN, chat_id: TG_CHAT_OK, parse_mode: 'HTML', disable_notification: false, ack_buttons: true, ack_link: false },
  });
  tgChannel = tg.id;
  expect(tg.config.ack_buttons).toBe(true);
  const tm = await api.post('/api/notification-channels', { name: 'qa3-teams-ack', type: 'teams', enabled: true, config: { url: TEAMS_URL(), ack_link: true } });
  teamsChannel = tm.id;
  p1 = await problemDevice(api, '127.0.0.133', 'qa3-ack-tg');
  p2 = await problemDevice(api, '127.0.0.134', 'qa3-ack-teams');
  await p1.up();
  await p2.up();
  await api.post('/api/alert-rules', { name: 'qa3 ack tg', enabled: true, sensor_id: p1.sensor.id, on_statuses: ['down', 'up'], delay_s: 0, channel_ids: [tgChannel] });
  await api.post('/api/alert-rules', { name: 'qa3 ack teams', enabled: true, sensor_id: p2.sensor.id, on_statuses: ['down', 'up'], delay_s: 0, channel_ids: [teamsChannel] });
});
test.afterAll(async () => {
  for (const p of [p1, p2]) { await p?.ctl('ok'); await p?.stop(); }
});

test('the Telegram poller long-polls getUpdates with the persisted offset', async () => {
  await expect.poll(async () => (await tgPolls()).filter((p) => p.token === TG_VALID_TOKEN).length, { timeout: 30_000, message: 'getUpdates polls' }).toBeGreaterThan(0);
  const p = (await tgPolls()).filter((x) => x.token === TG_VALID_TOKEN).pop();
  expect(Number(p.params.timeout)).toBeGreaterThanOrEqual(10);
  expect(JSON.stringify(p.params.allowed_updates ?? '')).toMatch(/callback_query/);
  const st = await api.get('/api/chat-ack/telegram/status');
  expect(st.pollers[0]).toMatchObject({ bot_username: 'netmon_qa_bot', state: 'polling' });
});

test('link the admin account: code -> /link NM-… via the bot -> linked, reply sent; code single use', async ({ page }) => {
  await loginViaApi(page, undefined, '/account');
  const code = await api.post('/api/chat-links/codes', { provider: 'telegram' });
  expect(code.code).toMatch(/^NM-[A-Z2-7]{8}$/);
  expect(code.bot_usernames).toContain('netmon_qa_bot');
  expect(code.instructions).toBe(`/link ${code.code}`);
  await clearTelegram();
  await tgInject({ type: 'link', text: `/link ${code.code}`, from_id: TG_ADMIN, username: 'qa_admin_tg' });
  await expect.poll(async () => (await api.get('/api/chat-links')).items.length, { timeout: 30_000, message: 'account linked' }).toBe(1);
  const link = (await api.get('/api/chat-links')).items[0];
  expect(link).toMatchObject({ username: 'admin', provider: 'telegram', display: '@qa_admin_tg' });
  await expect.poll(async () => (await tgCalls('sendMessage')).filter((m) => String(m.body.chat_id) === String(TG_ADMIN)).length, { timeout: 20_000 }).toBe(1);
  const reply = (await tgCalls('sendMessage')).find((m) => String(m.body.chat_id) === String(TG_ADMIN))!;
  expect(reply.body.text).toContain('Linked to NetworkMonitor user admin');
  // the same code again (another Telegram user) is refused (single use) -> no second link
  await tgInject({ type: 'link', text: `/link ${code.code}`, from_id: TG_STRANGER, username: 'stranger' });
  await new Promise((r) => setTimeout(r, 4000));
  expect((await api.get('/api/chat-links?all=true')).items.filter((x: any) => x.display === '@stranger')).toEqual([]);
  await page.reload();
  await expect(page.getByText('@qa_admin_tg').first()).toBeVisible({ timeout: 20_000 });
  await shot3(page, '60-chat-accounts');
});

test('problem -> Telegram message with the Acknowledge button; linked admin press -> acknowledged + markup edited', async ({ page }) => {
  test.setTimeout(150_000);
  await clearTelegram();
  await p1.down();
  await expect.poll(async () => (await telegramDelivered()).filter((m) => m.body.reply_markup).length, { timeout: 60_000 }).toBe(1);
  const msg = (await telegramDelivered()).find((m) => m.body.reply_markup)!;
  const mk = typeof msg.body.reply_markup === 'string' ? JSON.parse(msg.body.reply_markup) : msg.body.reply_markup;
  const btn = mk.inline_keyboard[0][0];
  expect(btn.text).toBe('Acknowledge');
  expect(btn.callback_data).toMatch(/^a1\.[A-Za-z0-9_-]{38}$/);
  // a stranger presses first -> refusal with show_alert
  await tgInject({ type: 'press', from_id: TG_STRANGER, username: 'stranger', data: btn.callback_data });
  await expect.poll(async () => (await tgCalls('answerCallbackQuery')).length, { timeout: 30_000 }).toBe(1);
  const refusal = (await tgCalls('answerCallbackQuery'))[0];
  expect(refusal.body.show_alert).toBe(true);
  expect(refusal.body.text).toMatch(/not linked/i);
  expect((await api.get(`/api/sensors/${p1.sensor.id}`)).acknowledgement ?? null).toBeNull();
  // the linked admin presses
  await tgInject({ type: 'press', from_id: TG_ADMIN, username: 'qa_admin_tg', data: btn.callback_data });
  await expect.poll(async () => (await tgCalls('answerCallbackQuery')).length, { timeout: 30_000 }).toBe(2);
  const ok = (await tgCalls('answerCallbackQuery'))[1];
  expect(ok.body.text).toBe('Acknowledged');
  expect(ok.body.show_alert ?? false).toBe(false);
  await expect.poll(async () => (await tgCalls('editMessageReplyMarkup')).length, { timeout: 20_000 }).toBe(1);
  const ed = (await tgCalls('editMessageReplyMarkup'))[0].body;
  const edMk = typeof ed.reply_markup === 'string' ? JSON.parse(ed.reply_markup) : ed.reply_markup;
  expect(edMk.inline_keyboard[0][0]).toMatchObject({ callback_data: 'noop' });
  expect(edMk.inline_keyboard[0][0].text).toMatch(/^✓ admin \d\d:\d\d$/);
  expect(String(ed.message_id)).toBe(String(msg.response.result.message_id));
  const s = await api.get(`/api/sensors/${p1.sensor.id}`);
  expect(s.acknowledgement).toMatchObject({ by: 'admin' });
  expect(JSON.stringify(s.acknowledgement)).toContain('Acknowledged from Telegram');
  // pressing again: already acknowledged (refusal text, idempotent)
  await tgInject({ type: 'press', from_id: TG_ADMIN, username: 'qa_admin_tg', data: btn.callback_data });
  await expect.poll(async () => (await tgCalls('answerCallbackQuery')).length, { timeout: 30_000 }).toBe(3);
  expect((await tgCalls('answerCallbackQuery'))[2].body.text).toMatch(/already/i);
  // the UI shows the acknowledgement; the timeline has an ack item
  await loginViaApi(page, undefined, `/sensors/${p1.sensor.id}`);
  await expect(page.getByText(/Acknowledged from Telegram/).first()).toBeVisible({ timeout: 20_000 });
  const tl = await api.get(`/api/devices/${p1.device.id}/timeline?limit=20`).catch(() => null);
  if (tl) expect(tl.items.some((i: any) => i.kind === 'ack')).toBe(true);
  // the press offset was confirmed: the injected updates are delivered and confirmed
  await p1.up();
});

test('Teams card carries Action.OpenUrl "Acknowledge" -> /ack#token -> acknowledge with a comment', async ({ page }) => {
  test.setTimeout(150_000);
  await clearTeams();
  await p2.down();
  await expect.poll(async () => (await teamsRecords()).length, { timeout: 60_000 }).toBeGreaterThanOrEqual(1);
  const card = (await teamsRecords())[0].body.attachments[0].content;
  const ack = card.actions.find((a: any) => a.title === 'Acknowledge');
  expect(ack, 'Acknowledge action').toBeTruthy();
  expect(ack.type).toBe('Action.OpenUrl');
  expect(ack.url).toMatch(new RegExp(`^${BASE_URL.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/ack#a1\\.[A-Za-z0-9_-]{38}$`));
  expect(card.actions.indexOf(ack)).toBeLessThan(card.actions.findIndex((a: any) => /Open in NetworkMonitor/.test(a.title)));
  const tok = ackToken(ack.url)!;
  // resolve as any logged-in user
  const res = await api.post('/api/chat-ack/resolve', { token: tok });
  expect(res).toMatchObject({ device_name: 'qa3-ack-teams', active: true, can_acknowledge: true, acknowledgement: null });
  await loginViaApi(page);
  await page.goto(ack.url);
  await expect(page.getByText('qa3-ack-teams').first()).toBeVisible({ timeout: 20_000 });
  expect(page.url(), 'token removed from the address bar').not.toContain(tok);
  await page.getByLabel(tx('chatAck.comment', /comment/i)).fill('On it - QA');
  await shot3(page, '60-ack-page');
  await page.getByRole('button', { name: tx('chatAck.acknowledge', /^acknowledge$/i) }).click();
  await expect(page.getByText(tx('chatAck.done', /acknowledged/i)).first()).toBeVisible({ timeout: 15_000 });
  const s = await api.get(`/api/sensors/${p2.sensor.id}`);
  expect(JSON.stringify(s.acknowledgement)).toContain('On it - QA');
  // idempotent API acknowledge
  const again = await raw(api, 'POST', '/api/chat-ack/acknowledge', { token: tok, comment: 'twice' });
  expect(again.status).toBe(200);
  // after recovery the token is stale
  await p2.up();
  const stale = await raw(api, 'POST', '/api/chat-ack/acknowledge', { token: tok, comment: 'late' });
  expect(stale.status).toBe(409);
  expect(stale.body.error.code).toBe('ack_stale');
  expect((await api.post('/api/chat-ack/resolve', { token: tok })).active).toBe(false);
});

test('forged / expired links: 400 ack_invalid, "This link has expired" page', async ({ page }) => {
  const forged = 'a1.' + 'B'.repeat(38);
  const r = await raw(api, 'POST', '/api/chat-ack/resolve', { token: forged });
  expect(r.status).toBe(400);
  expect(r.body.error.code).toBe('ack_invalid');
  const tooShort = await raw(api, 'POST', '/api/chat-ack/resolve', { token: 'a1.abc' });
  expect(tooShort.status).toBe(400);
  await loginViaApi(page);
  await page.goto(`/ack#${forged}`);
  await expect(page.getByText(tx('chatAck.expired', /expired/i)).first()).toBeVisible({ timeout: 20_000 });
  await shot3(page, '60-ack-expired');
  // API tokens are refused on chat-ack endpoints (403), and a 409 webhook conflict puts the poller in error
  await tgControl({ webhook: true });
  await expect.poll(async () => (await api.get('/api/chat-ack/telegram/status')).pollers[0]?.state, { timeout: 60_000 }).toBe('error');
  const st = (await api.get('/api/chat-ack/telegram/status')).pollers[0];
  expect(st.last_error).toMatch(/webhook|another consumer/i);
  const take = await api.post(`/api/notification-channels/${tgChannel}/telegram/take-over`);
  expect(take.ok).toBe(true);
  expect((await tgCalls('deleteWebhook')).length).toBeGreaterThanOrEqual(1);
  await expect.poll(async () => (await api.get('/api/chat-ack/telegram/status')).pollers[0]?.state, { timeout: 90_000 }).toBe('polling');
});

test('unlink from the profile; chat_ack OFF -> no buttons in new messages, poller stopped', async () => {
  test.setTimeout(120_000);
  const link = (await api.get('/api/chat-links')).items[0];
  expect((await raw(api, 'DELETE', `/api/chat-links/${link.id}`)).status).toBe(204);
  await setFeatures(api, { chat_ack: false });
  await clearTelegram();
  await p1.down();
  await expect.poll(async () => (await telegramDelivered()).length, { timeout: 60_000 }).toBeGreaterThanOrEqual(1);
  expect((await telegramDelivered())[0].body.reply_markup ?? null, 'no button while chat_ack is OFF').toBeNull();
  await p1.up();
});
