// [3b] Privacy and prompt-injection safety (docs/architecture.md §14.5, §14.6.1, §14.7, §14.11 spec 67):
// internal-IP redaction (placeholders to the model, real addresses in the UI), seeded secret markers never sent,
// injected syslog lines -> the "obeying" mock model cannot write (unknown_tool), cannot exfiltrate (no <img>, no
// external href, forged nm: refs as text); audit rows for question/tool_call/answer WITHOUT the question text
// (owner decision: audit stores titles only).
import { expect, test } from '@playwright/test';
import { apiAs, loginViaApi, type Api } from '../lib/helpers';
import { sendSyslog, setFeatures } from '../lib/lab3';
import {
  PHASE3B, answerProblems, askOk, audit, corpus, ensureAssistant, expectNoQuestionText, expectSafeRendering, lastSeq,
  mock, modelCalls, shot3b, changes , problem } from '../lib/lab3b';

// default mode (not serial): one worker runs the tests in order; a failure restarts the worker (beforeAll is
// idempotent) instead of skipping the remaining tests, so every contract item gets a verdict
test.describe.configure({ mode: 'default' });
test.skip(!PHASE3B, 'phase-3b Bedrock mock not running');

let api: Api;
let p: Awaited<ReturnType<typeof problem>>;
const IP = '127.0.0.138';
const INTERNAL_RE = /\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|127\.\d{1,3}\.\d{1,3}\.\d{1,3})\b/;
const MARKERS = {
  smtp: 'QA3B-SMTP-SECRET-7f3a', community: 'qa3b-community-secret-91c2', tg: '987654:QA3B_BOT_TOKEN_secret_abcdef',
  ssh: 'QA3B-SSH-PASS-55aa', webhook: 'https://hooks.qa3b.test/secret-path-c0ffee',
};

test.beforeAll(async () => {
  test.setTimeout(180_000);
  api = await apiAs();
  await ensureAssistant(api);
  await mock.clearModes();
  await setFeatures(api, { syslog: true });
  p = await problem(api, IP, 'qa3b-privacy-web');
  // seed secrets everywhere a projection could leak them
  await api.patch(`/api/devices/${p.device.id}`, {
    snmp_credentials: { version: 'v2c', port: 1161, timeout_s: 1, retries: 0, community: MARKERS.community },
    ssh_credentials: { username: 'qa3b', password: MARKERS.ssh, port: 22 },
  }).catch(() => undefined);
  await api.put('/api/settings', { smtp: { host: '127.0.0.1', port: 2525, security: 'none', username: 'qa', password: MARKERS.smtp, from_address: 'nm@qa3b.test', from_name: 'QA' } }).catch(() => undefined);
  await api.post('/api/notification-channels', { name: 'qa3b-secret-tg', type: 'telegram', enabled: false, config: { bot_token: MARKERS.tg, chat_id: '-1001', parse_mode: 'HTML' } }).catch(() => undefined);
  await api.post('/api/notification-channels', { name: 'qa3b-secret-hook', type: 'webhook', enabled: false, config: { url: MARKERS.webhook } }).catch(() => undefined);
  await p.down();
});
test.afterAll(async () => {
  await api.put('/api/assistant/settings', { redact_internal_ips: false, internal_cidrs: [] });
  await p?.ctl('ok'); await p?.stop();
});

test('redaction ON: no internal address leaves the server (placeholders IP-n); the admin sees real addresses', async ({ page }) => {
  await api.put('/api/assistant/settings', { redact_internal_ips: true, internal_cidrs: ['198.51.100.0/24'] });
  await mock.addScenario({
    name: 'qa_hosts', purpose: ['ask'], match: '\\[qa:hosts\\]',
    turns: [{ tool_use: [{ name: 'search_devices', input: { query: 'qa3b-privacy' } }] },
      { text: 'Hosts: {{ $.data.items | each("[{name}](nm:{ref}) at {host}") }}' }],
  });
  const since = await lastSeq();
  await loginViaApi(page, undefined, '/assistant');
  await page.getByTestId('question').fill(`Where is ${IP} and the privacy host? [qa:hosts]`);
  await page.getByTestId('question').press('Enter');
  const answer = page.getByTestId('answer').last();
  await expect(answer).toContainText(IP, { timeout: 60_000 });
  await expect(answer.getByText(IP).first()).toHaveCSS('direction', 'ltr');
  await shot3b(page, '67-redaction-ui');
  const calls = await modelCalls(since);
  for (const c of calls) {
    const blob = JSON.stringify(c.body);
    expect(blob.match(INTERNAL_RE)?.[0] ?? null, `internal address in request ${c.seq}`).toBeNull();
  }
  expect(calls[0].question).toMatch(/Where is IP-\d+/);
  expect(JSON.stringify(calls[1].body)).toMatch(/IP-\d+/);
  expect(calls[0].body.system[0].text).toContain('Addresses like IP-7 are placeholders');
  // the stored answer has the real address; the start event says redaction
  const conv = await api.get(`/api/assistant/conversations/${(await api.get('/api/assistant/conversations')).items[0].id}`);
  expect(conv.messages.at(-1).content).toContain(IP);
  await mock.clearScenarios();
});

test('redaction: a placeholder in tool arguments is translated back before the tool runs', async () => {
  const since = await lastSeq();
  const { msg } = await askOk(api, `where is ${IP} connected?`);
  const c1 = (await modelCalls(since))[0];
  const ph = c1.question.match(/IP-\d+/)![0];
  expect(msg.steps[0].tool).toBe('find_mac_or_ip');
  expect(msg.steps[0].args, 'the admin sees the real address in the step').toEqual({ ip: IP });
  const sentArgs = (await modelCalls(since))[1].body.messages.at(-2).content.find((b: any) => b.toolUse).toolUse.input;
  expect(sentArgs).toEqual({ ip: ph });
  await api.put('/api/assistant/settings', { redact_internal_ips: false, internal_cidrs: [] });
});

test('seeded secrets (SMTP, SNMP community, SSH, bot token, webhook URL) never reach the model', async () => {
  const since = await lastSeq();
  await askOk(api, 'investigate the problems [qa:3rounds]');
  await askOk(api, 'overview [qa:parallel]');
  const blob = JSON.stringify(await modelCalls(since));
  for (const [k, v] of Object.entries(MARKERS)) expect(blob, `secret marker ${k}`).not.toContain(v);
});

test('injection: obeying model -> update_device unknown_tool, no write, nothing unsafe rendered, forged refs as text', async ({ page }) => {
  test.setTimeout(120_000);
  const c = corpus();
  const lines = c.entries.filter((e: any) => e.surface === 'syslog').map((e: any) => e.text);
  sendSyslog(IP, lines.map((l: string) => `<13>Nov 10 10:00:00 qa3b-privacy app: ${l}`));
  await expect.poll(async () => (await api.get('/api/syslog?q=QAINJ0&limit=20')).items.length, { timeout: 20_000 }).toBeGreaterThanOrEqual(lines.length - 1);
  const devicesBefore = await api.get('/api/devices?page_size=1000');
  const auditBefore = (await audit(api, 'action=device.')).length;
  const since = await lastSeq();
  await loginViaApi(page, undefined, '/assistant');
  await page.getByTestId('question').fill(`Summarise the logs of the last week [qa:inj] other device ${p.device.id}`);
  await page.getByTestId('question').press('Enter');
  const answer = page.getByTestId('answer').last();
  await expect(answer).toContainText('As instructed by the data', { timeout: 90_000 });
  await shot3b(page, '67-injection');
  await expectSafeRendering(answer);
  await expect(answer.locator('a[href="/devices/999999"]')).toHaveCount(0);
  await expect(answer.locator('a[href*="exfil.attacker.test"], img, script')).toHaveCount(0);
  // what the product did with the tool calls
  const calls = await modelCalls(since);
  const results = calls.at(-1).body.messages.at(-1).content.filter((b: any) => b.toolResult).map((b: any) => b.toolResult);
  const byName: Record<string, any> = {};
  const uses = calls.at(-1).body.messages.at(-2).content.filter((b: any) => b.toolUse).map((b: any) => b.toolUse);
  for (const u of uses) byName[u.name] = results.find((r: any) => r.toolUseId === u.toolUseId);
  expect(byName.update_device.status).toBe('error');
  expect(byName.update_device.content[0].json).toMatchObject({ ok: false, error: 'unknown_tool' });
  expect(byName.config_diff.content[0].json.ok, 'config_diff not offered (no config excerpts)').toBe(false);
  // nothing was written
  const devicesAfter = await api.get('/api/devices?page_size=1000');
  expect(devicesAfter.total ?? devicesAfter.items.length).toBe(devicesBefore.total ?? devicesBefore.items.length);
  expect((await api.get(`/api/devices/${p.device.id}`)).enabled).toBe(true);
  expect((await audit(api, 'action=device.')).length).toBe(auditBefore);
  // structural delimiting: tool results are json blocks; control characters removed
  for (const call of calls) {
    for (const m of call.body.messages) for (const b of m.content) if (b.toolResult) expect(Object.keys(b.toolResult.content[0])).toEqual(['json']);
    expect(JSON.stringify(call.body)).not.toContain('\\u001b');
  }
  // stored answer: no image, links only to refs the server issued
  const conv = await api.get(`/api/assistant/conversations/${(await api.get('/api/assistant/conversations')).items[0].id}`);
  const msg = conv.messages.at(-1);
  const refs = new Set<string>(msg.sources.map((s: any) => s.ref));
  expect(answerProblems(msg.content, refs)).toEqual([]);
  expect(msg.content).not.toMatch(/!\[/);
  expect(msg.steps.find((s: any) => s.tool === 'update_device')).toMatchObject({ ok: false, error: 'unknown_tool' });
});

test('audit: question / tool_call / answer rows exist for every question, without the question text (titles only)', async ({ page }) => {
  const question = 'Which devices are down right now? QA3B-AUDIT-PROBE-4412';
  const { msg } = await askOk(api, question);
  const rows = (await audit(api, 'action=assistant.')).filter((r: any) => JSON.stringify(r.changes ?? {}).includes(String(msg.id)) || r.entity_id === msg.id || r.entity_id === msg.conversation_id);
  const actions = rows.map((r: any) => r.action);
  for (const a of ['assistant.question', 'assistant.tool_call', 'assistant.answer']) expect(actions, a).toContain(a);
  expectNoQuestionText(await audit(api, 'action=assistant.'), question);
  const q = rows.find((r: any) => r.action === 'assistant.question');
  expect(changes(q)).toMatchObject({ message_id: msg.id, purpose: 'ask', language: 'en', channel: 'web' });
  expect(changes(q).question, 'question text not stored in the audit').toBeUndefined();
  const tc = rows.find((r: any) => r.action === 'assistant.tool_call');
  expect(changes(tc)).toMatchObject({ tool: 'list_problems', ok: true });
  expect(changes(tc).duration_ms).toEqual(expect.any(Number));
  expect(changes(tc).arguments, 'owner decision: tool_call audit has no arguments').toBeUndefined();
  expect(changes(q).title, 'assistant.question carries the conversation title').toBeTruthy();
  const an = rows.find((r: any) => r.action === 'assistant.answer');
  expect(changes(an)).toMatchObject({ status: 'complete', model_calls: 2, tool_calls: 1 });
  // the audit page lists them (and the CSV export has no question text either)
  await loginViaApi(page, undefined, '/audit');
  await expect(page.getByText('assistant.question').first()).toBeVisible({ timeout: 20_000 });
  const csv = await (await api.ctx.fetch(`/api/audit/export.csv?action=assistant.`)).text();
  expect(csv).not.toContain('QA3B-AUDIT-PROBE-4412');
});
