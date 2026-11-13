// [3b] Daily syslog digest (docs/architecture.md §14.8.5, §14.11 spec 72; data-model § Digest vectors): 8 days of
// syslog seeded through the real receiver with the test clock (receipt time = rt.clock.now()): a baseline of 10/day,
// then yesterday a spike (40) of the same program and 25 messages of a new type -> Run now for yesterday -> both
// listed (unusual, new type with an nm:syslog example), AI bullets with links, email captured; mock error ->
// statistics only ("AI summary unavailable").
import { expect, test } from '@playwright/test';
import { apiAs, loginViaApi, type Api } from '../lib/helpers';
import { raw } from '../lib/lab';
import { resetClock, setClock } from '../lib/lab2b';
import { clearSmtp, configureSmtp, ensureDeviceAt, ensureGroupNamed, sendSyslog, setFeatures, smtpMessages } from '../lib/lab3';
import { PHASE3B, audit, ensureAssistant, lastSeq, mock, modelCalls, setScalars, shot3b } from '../lib/lab3b';

// default mode (not serial): one worker runs the tests in order; a failure restarts the worker (beforeAll is
// idempotent) instead of skipping the remaining tests, so every contract item gets a verdict
test.describe.configure({ mode: 'default' });
test.skip(!PHASE3B, 'phase-3b Bedrock mock not running');

let api: Api;
let day = '';
let dayBefore = '';
const IP = '127.0.0.146';
const PROG = 'qa3bapp';
const msg = (text: string, sev = 4) => `<${8 + sev}>1 - qa3b-digest ${PROG} - - - ${text}`;

function localDay(tz: string, offsetDays: number): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(Date.now() - offsetDays * 86400_000));
}
async function count(q: string): Promise<number> {
  const since = new Date(Date.now() - 10 * 86400_000).toISOString(); // the list defaults to the last 24 h
  return (await api.get(`/api/syslog?q=${encodeURIComponent(q)}&since=${encodeURIComponent(since)}&limit=500`)).items.length;
}

test.beforeAll(async () => {
  test.setTimeout(300_000);
  api = await apiAs();
  await ensureAssistant(api);
  await mock.clearModes();
  await configureSmtp(api);
  await setFeatures(api, { syslog: true, ai_syslog_digest: true });
  await setScalars(api, { digest_email_to: ['noc@mnidom.test'], digest_language: 'en' });
  const g = await ensureGroupNamed(api, 'QA Assistant 3b');
  await ensureDeviceAt(api, g.id, IP, 'qa3b-digest');
  const tz = (await api.get('/api/settings')).timezone ?? 'UTC';
  day = localDay(tz, 1);
  dayBefore = localDay(tz, 2);
  if ((await count('QA3B new error type')) >= 25) return; // already seeded (worker restart)
  try {
    for (let k = 8; k >= 2; k -= 1) {
      await setClock(api, -k * 86400);
      sendSyslog(IP, Array.from({ length: 10 }, (_, i) => msg(`QA3B baseline job finished run ${k}-${i}`)));
      await new Promise((r) => setTimeout(r, 1500));
    }
    await setClock(api, -86400);
    sendSyslog(IP, Array.from({ length: 40 }, (_, i) => msg(`QA3B baseline job finished run 1-${i}`))); // same signature as the baseline
    sendSyslog(IP, Array.from({ length: 25 }, (_, i) => msg(`QA3B new error type on port ${i % 7} code 0x${(i + 16).toString(16)}`, 3)));
    await new Promise((r) => setTimeout(r, 2000));
  } finally {
    await resetClock(api);
  }
  await expect.poll(() => count('QA3B new error type'), { timeout: 30_000 }).toBe(25);
});
test.afterAll(async () => { await resetClock(api).catch(() => undefined); });

test('Run now for yesterday: spike and new message type listed; AI bullets with links; email delivered', async ({ page }) => {
  test.setTimeout(180_000);
  await clearSmtp();
  // the signature register is filled by the digest job itself: digest D-2 first, so the baseline type is known on D-1
  await api.post(`/api/assistant/digests/${dayBefore}/run`, { send: false });
  await expect.poll(async () => (await api.get(`/api/assistant/digests/${dayBefore}`)).status, { timeout: 120_000 }).not.toBe('running');
  const since = await lastSeq();
  const started = await api.post(`/api/assistant/digests/${day}/run`, { send: true });
  expect(started.status).toBe('running');
  let d: any = null;
  await expect.poll(async () => { d = await api.get(`/api/assistant/digests/${day}`); return d.status; }, { timeout: 120_000 }).toBe('ok');
  expect(d.day).toBe(day);
  expect(d.baseline_days).toBeGreaterThanOrEqual(3);
  const prog = d.stats.by_program.find((r: any) => r.key === PROG);
  expect(prog, `by_program ${JSON.stringify(d.stats.by_program)}`).toBeTruthy();
  expect(prog.count).toBe(65);
  expect(prog.baseline).toBeCloseTo(10, 0);
  expect(prog.unusual).toBe(true);
  expect(prog.ratio).toBeGreaterThanOrEqual(3);
  const nt = d.stats.new_types.find((t: any) => /qa3b new error type/i.test(t.sample) || /QA3B new error type/.test(t.example));
  expect(nt, `new_types ${JSON.stringify(d.stats.new_types)}`).toBeTruthy();
  expect(nt.count).toBe(25);
  expect(nt.example_ref).toMatch(/^syslog\/\d+$/);
  expect(nt.signature).toMatch(/^[0-9a-f]{16}$/);
  expect(d.stats.new_types.some((t: any) => /baseline job finished/i.test(t.sample)), 'baseline type is not new').toBe(false);
  expect(d.ai_markdown).toMatch(/^- /m);
  expect(d.ai_markdown).toMatch(/\(nm:(syslog|device)\/\d+\)/);
  const calls = (await modelCalls(since)).filter((c) => c.purpose === 'digest');
  expect(calls.length).toBe(1);
  expect(calls[0].op).toBe('converse');
  expect(calls[0].body.inferenceConfig.maxTokens).toBeLessThanOrEqual(700);
  const sent = JSON.stringify(calls[0].body);
  expect((sent.match(/QA3B baseline job finished run/g) ?? []).length, 'no raw logs except <= 20 examples').toBeLessThanOrEqual(20);
  await expect.poll(async () => (await smtpMessages()).length, { timeout: 60_000 }).toBeGreaterThanOrEqual(1);
  const m = (await smtpMessages()).find((x) => x.rcpt_tos.includes('noc@mnidom.test'))!;
  expect(m.html).toContain(PROG);
  expect(m.html).toMatch(/AI-generated|נוצר/);
  expect(d.deliveries).toEqual(expect.arrayContaining([expect.objectContaining({ target: 'email:noc@mnidom.test', ok: true })]));
  expect((await audit(api, 'action=assistant.digest'))[0]).toBeTruthy();
  await loginViaApi(page, undefined, `/assistant/digests/${day}`);
  await expect(page.getByTestId('digest')).toContainText(PROG, { timeout: 20_000 });
  await expect(page.getByTestId('digest').locator('a[href^="/logs?tab=syslog&id="]').first()).toBeVisible();
  await shot3b(page, '72-digest');
});

test('mock error -> statistics stored and shown, AI summary unavailable', async ({ page }) => {
  test.setTimeout(120_000);
  await mock.mode('unavailable');
  await api.post(`/api/assistant/digests/${day}/run`, { send: false });
  let d: any = null;
  await expect.poll(async () => { d = await api.get(`/api/assistant/digests/${day}`); return d.status; }, { timeout: 90_000 }).not.toBe('running');
  expect(d.ai_markdown).toBeNull();
  expect(d.stats.by_program.find((r: any) => r.key === PROG).unusual).toBe(true);
  await loginViaApi(page, undefined, `/assistant/digests/${day}`);
  await expect(page.getByTestId('digest')).toContainText(/AI summary unavailable|digest\.aiUnavailable/, { timeout: 20_000 });
  await mock.clearModes();
});

test('limits: last 7 days only; > 5 manual runs a day -> 429; feature OFF -> 409; list newest first', async () => {
  const old = new Date(Date.now() - 10 * 86400_000).toISOString().slice(0, 10);
  expect((await raw(api, 'POST', `/api/assistant/digests/${old}/run`, { send: false })).status).toBe(422);
  const list = await api.get('/api/assistant/digests');
  expect(list.items[0].day >= list.items.at(-1).day).toBe(true);
  let last = 0;
  for (let i = 0; i < 6; i += 1) {
    last = (await raw(api, 'POST', `/api/assistant/digests/${day}/run`, { send: false })).status;
    if (last === 429) break;
    await expect.poll(async () => (await api.get(`/api/assistant/digests/${day}`)).status, { timeout: 60_000 }).not.toBe('running');
  }
  expect(last).toBe(429);
  await setFeatures(api, { ai_syslog_digest: false });
  expect((await raw(api, 'GET', '/api/assistant/digests')).status).toBe(409);
  await setFeatures(api, { ai_syslog_digest: true });
});
