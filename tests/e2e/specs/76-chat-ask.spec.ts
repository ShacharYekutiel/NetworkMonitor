// [3b] Ask from Telegram (docs/architecture.md §14.8.9, §14.11 spec 76; api.md "Telegram /ask"): a linked admin
// sends `/ask …` in a private chat -> "Working on it…" -> edited to the answer (Telegram HTML, escaped, ≤ 3500 chars)
// + "Full answer" link to the app; stored as a conversation with channel telegram; IP placeholders stay in
// Telegram (chat_redact_ips); viewer / unlinked / group chat / feature OFF refused; injected HTML escaped.
import { expect, test } from '@playwright/test';
import { BASE_URL, VIEWER, apiAs, type Api } from '../lib/helpers';
import { TG_VALID_TOKEN, clearTelegram, ensureViewer } from '../lib/lab';
import { sendSyslog, setFeatures, tgCalls, tgInject } from '../lib/lab3';
import { PHASE3B, audit, corpus, ensureAssistant, mock , changes , problem, expectNoQuestionText } from '../lib/lab3b';

// default mode (not serial): one worker runs the tests in order; a failure restarts the worker (beforeAll is
// idempotent) instead of skipping the remaining tests, so every contract item gets a verdict
test.describe.configure({ mode: 'default' });
test.skip(!PHASE3B, 'phase-3b Bedrock mock not running');

const TG_ADMIN = 525252;
const TG_VIEWER = 525253;
const TG_STRANGER = 525254;
let api: Api;
let p: Awaited<ReturnType<typeof problem>>;
const IP = '127.0.0.148';

const toUser = async (uid: number, method = 'sendMessage') => (await tgCalls(method)).filter((m) => String(m.body.chat_id) === String(uid));
const ALLOWED_TAGS = new Set(['b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del', 'code', 'pre', 'a', 'blockquote']);
function telegramProblems(text: string): string[] {
  const out: string[] = [];
  for (const m of text.matchAll(/<\/?([a-zA-Z][\w-]*)([^>]*)>/g)) {
    if (!ALLOWED_TAGS.has(m[1].toLowerCase())) out.push(`<${m[1]}>`);
    const href = m[2].match(/href="([^"]*)"/)?.[1];
    if (href && !href.startsWith(BASE_URL)) out.push(`href ${href}`);
  }
  if (/&(?!(amp|lt|gt|quot|#\d+);)/.test(text)) out.push('bare &');
  return out;
}
async function link(user: Api, tgId: number, username: string): Promise<void> {
  const code = await user.post('/api/chat-links/codes', { provider: 'telegram' });
  await tgInject({ type: 'link', text: `/link ${code.code}`, from_id: tgId, username });
  await expect.poll(async () => (await user.get('/api/chat-links')).items.length, { timeout: 30_000 }).toBeGreaterThan(0);
}
async function askTg(tgId: number, text: string, chatType = 'private'): Promise<void> {
  await tgInject({ type: 'link', text, from_id: tgId, username: `u${tgId}`, chat_type: chatType, chat_id: -1001 });
}

test.beforeAll(async () => {
  test.setTimeout(180_000);
  api = await apiAs();
  await ensureAssistant(api);
  await mock.clearModes();
  await api.put('/api/settings', { public_base_url: BASE_URL });
  await api.put('/api/assistant/settings', { chat_redact_ips: true });
  await setFeatures(api, { chat_ack: true, ai_chat_ask: true, syslog: true });
  const old = (await api.get('/api/notification-channels')).items.find((c: any) => c.name === 'qa3b-tg-ask');
  const ch = old ?? await api.post('/api/notification-channels', { name: 'qa3b-tg-ask', type: 'telegram', enabled: true,
    config: { bot_token: TG_VALID_TOKEN, chat_id: '-1001', parse_mode: 'HTML', ai_ask: true, ack_buttons: true } });
  // ack_buttons keeps the chat_ack poller on this bot while ai_chat_ask is OFF (data-model: "Asking is turned off" is
  // answered only while the poller runs for chat_ack)
  expect(ch.config.ai_ask).toBe(true);
  if (!(await api.get('/api/chat-links')).items.length) await link(api, TG_ADMIN, 'qa3b_admin_tg');
  await ensureViewer(api);
  const viewer = await apiAs(VIEWER);
  if (!(await viewer.get('/api/chat-links')).items.length) await link(viewer, TG_VIEWER, 'qa3b_viewer_tg');
  p = await problem(api, IP, 'qa3b-tgask-web');
  await p.down();
});
test.afterAll(async () => { await p?.ctl('ok'); await p?.stop(); });

test('linked admin: "Working on it…" then the same message edited to the answer + Full answer link', async () => {
  test.setTimeout(120_000);
  await clearTelegram();
  await askTg(TG_ADMIN, '/ask Which devices are down right now?');
  await expect.poll(async () => (await toUser(TG_ADMIN, 'editMessageText')).length, { timeout: 60_000 }).toBeGreaterThan(0);
  const working = (await toUser(TG_ADMIN))[0];
  expect(working.body.text).toMatch(/Working on it/);
  const edit = (await toUser(TG_ADMIN, 'editMessageText')).at(-1)!;
  expect(edit.status, JSON.stringify(edit.response)).toBe(200);
  expect(edit.body.message_id).toBe(working.response.result.message_id);
  expect(edit.body.parse_mode).toBe('HTML');
  expect(edit.body.text).toContain('qa3b-tgask-web');
  const m = edit.body.text.match(/<a href="([^"]+)\/assistant\/c\/(\d+)">Full answer<\/a>/);
  expect(m, edit.body.text).toBeTruthy();
  expect(m![1]).toBe(BASE_URL);
  expect(edit.body.text.length).toBeLessThanOrEqual(3500 + 200);
  expect(telegramProblems(edit.body.text)).toEqual([]);
  // nm: links became absolute app URLs
  expect(edit.body.text).toMatch(new RegExp(`href="${BASE_URL.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/(sensors|devices)/\\d+"`));
  const conv = await api.get(`/api/assistant/conversations/${m![2]}`);
  expect(conv).toMatchObject({ channel: 'telegram' });
  const q = (await audit(api, 'action=assistant.question'))[0];
  expect(changes(q)).toMatchObject({ channel: 'telegram' });
  expect(q.source).toBe('telegram');
  // question-text check: spec 67 (title vs question decision)
});

test('/ask@BotName works; IP placeholders stay in Telegram, the app shows real addresses', async () => {
  await clearTelegram();
  await mock.addScenario({ name: 'qa_tg_hosts', purpose: ['chat_ask', 'ask'], match: '\\[qa:tghosts\\]', turns: [
    { tool_use: [{ name: 'search_devices', input: { query: 'qa3b-tgask' } }] }, { text: 'Hosts: {{ $.data.items | each("{name} at {host}") }}' }] });
  await askTg(TG_ADMIN, '/ask@netmon_qa_bot where is the web host? [qa:tghosts]');
  await expect.poll(async () => (await toUser(TG_ADMIN, 'editMessageText')).length, { timeout: 60_000 }).toBeGreaterThan(0);
  const edit = (await toUser(TG_ADMIN, 'editMessageText')).at(-1)!;
  expect(edit.body.text).toMatch(/IP-\d+/);
  expect(edit.body.text).not.toContain(IP);
  const id = edit.body.text.match(/\/assistant\/c\/(\d+)/)![1];
  const conv = await api.get(`/api/assistant/conversations/${id}`);
  expect(conv.messages.at(-1).content).toContain(IP);
  await mock.clearScenarios();
});

test('refusals: viewer, unlinked user, group chat -> refusal texts, no question started', async () => {
  await clearTelegram();
  const before = (await mock.requests('op=converse,converse_stream')).length;
  await askTg(TG_VIEWER, '/ask Which devices are down?');
  await askTg(TG_STRANGER, '/ask Which devices are down?');
  await askTg(TG_ADMIN, '/ask Which devices are down?', 'group');
  await expect.poll(async () => (await tgCalls('sendMessage')).length, { timeout: 30_000 }).toBeGreaterThanOrEqual(3);
  await new Promise((r) => setTimeout(r, 3000));
  const texts = (await tgCalls('sendMessage')).map((m) => `${m.body.chat_id}: ${m.body.text}`).join('\n');
  expect(texts).toMatch(/private chat/i);
  expect((await toUser(TG_VIEWER)).length).toBe(1);
  expect((await toUser(TG_STRANGER)).length).toBe(1);
  expect((await toUser(TG_VIEWER, 'editMessageText')).length).toBe(0);
  expect((await mock.requests('op=converse,converse_stream')).length, 'no model call for refused questions').toBe(before);
});

test('injected HTML in tool results is escaped in the Telegram answer', async () => {
  test.setTimeout(120_000);
  const c = corpus();
  const lines = c.entries.filter((e: any) => ['syslog-telegram-html', 'syslog-html-script', 'syslog-image-exfil'].includes(e.id)).map((e: any) => e.text);
  sendSyslog(IP, lines.map((l: string) => `<13>Nov 10 10:00:00 qa3b-tgask app: ${l}`));
  await new Promise((r) => setTimeout(r, 3000));
  await clearTelegram();
  await askTg(TG_ADMIN, '/ask summarise the logs [qa:inj]');
  await expect.poll(async () => (await toUser(TG_ADMIN, 'editMessageText')).length, { timeout: 90_000 }).toBeGreaterThan(0);
  const edit = (await toUser(TG_ADMIN, 'editMessageText')).at(-1)!;
  expect(edit.status, `Telegram accepted the HTML: ${JSON.stringify(edit.response)}`).toBe(200);
  expect(telegramProblems(edit.body.text)).toEqual([]);
  expect(edit.body.text).not.toMatch(/href="https:\/\/exfil/);
});

test('busy and feature OFF: "Still working…" while running; OFF -> "Asking is turned off"', async () => {
  test.setTimeout(120_000);
  await clearTelegram();
  await askTg(TG_ADMIN, '/ask the long one [qa:long]');
  await expect.poll(async () => (await toUser(TG_ADMIN)).length, { timeout: 30_000 }).toBe(1);
  await askTg(TG_ADMIN, '/ask Which devices are down?');
  await expect.poll(async () => (await toUser(TG_ADMIN)).map((m) => m.body.text).join('\n'), { timeout: 30_000 }).toMatch(/Still working on your previous question/);
  await expect.poll(async () => (await toUser(TG_ADMIN, 'editMessageText')).length, { timeout: 90_000 }).toBeGreaterThan(0);
  await setFeatures(api, { ai_chat_ask: false });
  await clearTelegram();
  await askTg(TG_ADMIN, '/ask Which devices are down?');
  await expect.poll(async () => (await toUser(TG_ADMIN)).map((m) => m.body.text).join('\n'), { timeout: 30_000 }).toMatch(/Asking is turned off/);
  await setFeatures(api, { ai_chat_ask: true });
});
