// [3b] Errors, retries, Stop, timeouts, loop limits, caps (daily, monthly tokens, USD cost), busy, usage page
// (docs/architecture.md §14.2.4, §14.3.2, §14.3.3, §14.6.3, §14.11 spec 66; api.md "[3b] Errors").
import { expect, test } from '@playwright/test';
import { apiAs, loginViaApi, type Api } from '../lib/helpers';
import { raw } from '../lib/lab';
import {
  PHASE3B, ask, askInUi, askOk, audit, ensureAdmin2, ensureAssistant, lastSeq, mock, modelCalls, openStream, setScalars, shot3b, changes } from '../lib/lab3b';

// default mode (not serial): one worker runs the tests in order; a failure restarts the worker (beforeAll is
// idempotent) instead of skipping the remaining tests, so every contract item gets a verdict
test.describe.configure({ mode: 'default' });
test.skip(!PHASE3B, 'phase-3b Bedrock mock not running');

let api: Api;
const Q = 'what is a VLAN? [qa:notools]';

test.beforeAll(async () => {
  api = await apiAs();
  await ensureAssistant(api);
  await mock.clearModes();
});
test.afterEach(async () => { await mock.clearModes(); });
test.afterAll(async () => {
  await setScalars(api, { assistant_daily_questions_per_user: 1000, assistant_max_model_calls: 8, assistant_max_tool_calls: 12,
    assistant_question_timeout_s: 120, assistant_monthly_cost_cap: null, assistant_max_output_tokens: 2000 });
  await api.put('/api/assistant/settings', { prices: { currency: 'USD', input_per_mtok: null, output_per_mtok: null, cache_read_per_mtok: null, cache_write_per_mtok: null } });
});

test('throttling: retried (1 s / 4 s) then success with a "retrying" notice; > 2 retries -> bedrock_throttled', async () => {
  test.setTimeout(90_000);
  await mock.mode('throttle:2');
  const since = await lastSeq();
  const r = await ask(api, Q);
  expect(r.message.status).toBe('complete');
  const calls = await modelCalls(since);
  expect(calls.map((c) => c.status)).toEqual([429, 429, 200]);
  const gap1 = calls[1].time - calls[0].time;
  const gap2 = calls[2].time - calls[1].time;
  expect(gap1).toBeGreaterThan(0.7);
  expect(gap2).toBeGreaterThan(3.0);
  expect(r.events.some((e) => e.event === 'notice' && e.data.code === 'retrying')).toBe(true);
  await mock.mode('throttle:3');
  const r2 = await ask(api, Q);
  expect(r2.error).toMatchObject({ code: 'bedrock_throttled', message: expect.stringContaining('Amazon Bedrock is busy') });
  expect(r2.message.status).toBe('error');
  const st = await api.get('/api/assistant/status');
  expect(['degraded', 'ready']).toContain(st.state);
});

for (const [mode, code, text] of [
  ['access_denied', 'bedrock_access_denied', /AWS denied access/],
  ['model_access', 'bedrock_model_access', /not enabled for your AWS account in us-east-1/],
  ['on_demand_unsupported', 'bedrock_model_invalid', /inference profile/i],
  ['model_invalid', 'bedrock_model_invalid', /model ID is not valid in us-east-1/],
  ['quota', 'bedrock_quota', /quota/],
  ['validation', 'bedrock_validation', /Malformed input request/],
  ['expired_token', 'aws_auth_failed', /AWS rejected the credentials/],
  ['clock_skew', 'aws_clock_skew', /clock/],
  ['unavailable', 'bedrock_unavailable', /could not answer right now/],
  ['stream_error:2', 'bedrock_unavailable', /could not answer right now/],
] as const) {
  test(`mode ${mode} -> ${code} (SSE error + done status error; health state)`, async () => {
    test.setTimeout(60_000);
    await mock.mode(mode);
    const r = await ask(api, Q);
    expect(r.status).toBe(200);
    expect(r.error?.code, JSON.stringify(r.events.slice(-3))).toBe(code);
    expect(r.error.message).toMatch(text);
    expect(r.message.status).toBe('error');
    expect(r.message.error.code).toBe(code);
    if (['bedrock_access_denied', 'bedrock_model_access', 'bedrock_model_invalid', 'aws_auth_failed', 'aws_clock_skew'].includes(code)) {
      expect((await api.get('/api/health')).assistant).toBe('error');
      const st = await api.get('/api/assistant/status');
      expect(st.state_error.code).toBe(code);
    }
    await mock.clearModes();
    // an error state does not block questions: the next one succeeds and the state recovers
    const ok = await ask(api, Q);
    expect(ok.message.status).toBe('complete');
    expect((await api.get('/api/health')).assistant).toMatch(/^(ready|degraded)$/);
  });
}

test('UI: error banner with the mapped message and Retry; Retry succeeds', async ({ page }) => {
  test.setTimeout(90_000);
  // Retry is offered only for retryable codes (throttled / unavailable / network / timeout), not for access errors
  await mock.mode('access_denied', 'converse', 1);
  await loginViaApi(page, undefined, '/assistant');
  await askInUi(page, Q);
  const denied = page.getByTestId('answer-error').last();
  await expect(denied).toContainText(/AWS denied access|bedrock_access_denied/, { timeout: 30_000 });
  await expect(denied.getByRole('button', { name: /retry|assistant\.retry/i })).toHaveCount(0);
  await mock.mode('throttle:3');
  await askInUi(page, Q);
  const err = page.getByTestId('answer-error').last();
  await expect(err).toContainText(/Amazon Bedrock is busy|bedrock_throttled/, { timeout: 30_000 });
  await shot3b(page, '66-error-banner');
  await err.getByRole('button', { name: /retry|assistant\.retry/i }).click();
  await expect(page.getByTestId('answer').last()).toContainText('logical broadcast domain', { timeout: 30_000 });
});

test('Stop mid-stream (UI): cancelled within 1 s, Bedrock stream closed, partial text kept, usage counted, audited', async ({ page }) => {
  test.setTimeout(120_000);
  const since = await lastSeq();
  await loginViaApi(page, undefined, '/assistant');
  await askInUi(page, 'the long one [qa:long]');
  await expect(page.getByTestId('answer').last()).toContainText('Line 01', { timeout: 30_000 });
  const t0 = Date.now() / 1000;
  await page.getByTestId('stop').click();
  const call = async () => (await modelCalls(since)).find((c) => c.op === 'converse_stream');
  await expect.poll(async () => (await call())?.closed_by_client, { timeout: 5_000, message: 'Bedrock stream closed' }).toBe(true);
  const c = await call();
  expect(c.closed_at - t0, 'closed within 1 s of Stop').toBeLessThan(1.5);
  expect(c.complete).toBeFalsy();
  const convs = await api.get('/api/assistant/conversations');
  const conv = await api.get(`/api/assistant/conversations/${convs.items[0].id}`);
  const m = conv.messages.at(-1);
  expect(m.status).toBe('cancelled');
  expect(m.content).toContain('Line 01');
  expect(m.content).not.toContain('Line 07');
  // contract gap: Bedrock sends usage only in the final `metadata` event; a cancelled stream never gets it, so
  // "usage counts what was consumed" needs an estimate (reported)
  expect.soft(m.usage.input_tokens, 'usage of a cancelled answer').toBeGreaterThan(0);
  expect(changes((await audit(api, 'action=assistant.cancel'))[0])).toMatchObject({ message_id: m.id });
  // cancel is idempotent; a finished message returns its status
  expect((await api.post(`/api/assistant/messages/${m.id}/cancel`)).status).toBe('cancelled');
  await shot3b(page, '66-stopped');
});

test('busy: a second question of the same user while one runs -> 409 assistant_busy with the message id', async () => {
  const s = await openStream(api, '/api/assistant/ask', { conversation_id: null, question: 'busy test [qa:long]' });
  const start = await s.waitFor((e) => e.event === 'start');
  const r = await raw(api, 'POST', '/api/assistant/ask', { conversation_id: null, question: Q });
  expect(r.status).toBe(409);
  expect(r.body.error.code).toBe('assistant_busy');
  expect(r.body.error.details).toEqual([{ message_id: start.data.message_id }]);
  expect((await api.get('/api/assistant/status')).me.running_message_id).toBe(start.data.message_id);
  await api.post(`/api/assistant/messages/${start.data.message_id}/cancel`);
  s.abort();
});

test('timeout: a stalled Bedrock stream -> assistant_timeout (idle 30 s); no extra no-tools call', async () => {
  test.setTimeout(120_000);
  await setScalars(api, { assistant_question_timeout_s: 30 });
  await mock.mode('stall:1');
  const since = await lastSeq();
  const t0 = Date.now();
  const r = await ask(api, 'stall [qa:long]');
  expect(r.error?.code).toBe('assistant_timeout');
  expect(r.message.status).toBe('error');
  expect(Date.now() - t0).toBeLessThan(60_000);
  expect((await modelCalls(since)).length, 'timeout does not get the final extra call').toBe(1);
  await setScalars(api, { assistant_question_timeout_s: 120 });
});

test('limits: model calls cap -> one more call without toolChoice ("Do not call tools") -> limit_reached notice', async () => {
  await setScalars(api, { assistant_max_model_calls: 3 });
  const since = await lastSeq();
  const r = await ask(api, 'loop please [qa:loop]');
  const calls = await modelCalls(since);
  expect(calls.length).toBe(4);
  const last = calls.at(-1);
  expect(last.final_call).toBe(true);
  expect(last.body.toolConfig, 'tools still sent (history has tool blocks)').toBeTruthy();
  expect(last.body.toolConfig.toolChoice).toBeUndefined();
  expect(JSON.stringify(last.body)).toMatch(/Do not call tools/);
  expect(r.events.some((e) => e.event === 'notice' && e.data.code === 'limit_reached')).toBe(true);
  expect(r.message.status).toBe('limit_reached');
  expect(r.message.notices).toContain('limit_reached');
  expect(r.message.content).toContain('I stopped after');
  await setScalars(api, { assistant_max_model_calls: 8 });
});

test('limits: tool calls cap (4 parallel per round, cap 5) and max_tokens -> limit_reached', async () => {
  await setScalars(api, { assistant_max_tool_calls: 5 });
  const r = await ask(api, 'loop please [qa:loop-parallel]');
  expect(r.message.status).toBe('limit_reached');
  expect(r.message.tool_calls).toBeLessThanOrEqual(5);
  await setScalars(api, { assistant_max_tool_calls: 12 });
  const r2 = await ask(api, 'cut me [qa:maxtokens]');
  expect(r2.message.status).toBe('limit_reached');
  expect(r2.message.notices).toContain('limit_reached');
});

test('daily cap 2: third question refused 429 assistant_cap_reached user_daily (fresh admin)', async () => {
  const admin2 = await ensureAdmin2(api);
  await setScalars(api, { assistant_daily_questions_per_user: 2 });
  const st = await admin2.get('/api/assistant/status');
  let used = st.me.questions_today;
  while (used < 2) { await askOk(admin2, Q); used += 1; }
  const r = await raw(admin2, 'POST', '/api/assistant/ask', { conversation_id: null, question: Q });
  expect(r.status).toBe(429);
  expect(r.body.error.code).toBe('assistant_cap_reached');
  expect(r.body.error.details[0]).toMatchObject({ cap: 'user_daily', limit: 2, used: 2, resets_at: expect.any(String) });
  await setScalars(api, { assistant_daily_questions_per_user: 1000 });
  expect((await ask(admin2, Q)).message.status).toBe('complete');
});

test('USD prices: cost estimate per answer = pricing formula; monthly cost cap refuses with monthly_cost', async () => {
  await api.put('/api/assistant/settings', { prices: { currency: 'USD', input_per_mtok: 3, output_per_mtok: 15, cache_read_per_mtok: null, cache_write_per_mtok: null } });
  const { msg } = await askOk(api, 'Which sensors are down?');
  const u = msg.usage;
  const want = (u.input_tokens * 3 + u.output_tokens * 15 + u.cache_read_tokens * 0.3 + u.cache_write_tokens * 3.75) / 1e6;
  expect(msg.currency).toBe('USD');
  expect(Math.abs(msg.cost_estimate - want)).toBeLessThan(1e-6);
  await setScalars(api, { assistant_monthly_cost_cap: 0.000001 });
  const r = await raw(api, 'POST', '/api/assistant/ask', { conversation_id: null, question: Q });
  expect(r.status).toBe(429);
  expect(r.body.error.details[0]).toMatchObject({ cap: 'monthly_cost' });
  const usage = await api.get('/api/assistant/usage');
  expect(usage).toMatchObject({ currency: 'USD', cost_cap: 0.000001 });
  expect(usage.pct_of_cap).toBeGreaterThanOrEqual(100);
  await setScalars(api, { assistant_monthly_cost_cap: null });
});

test('usage page: this month\'s tokens grow by exactly the mock usage (cap formula), KPI tiles and chart shown', async ({ page }) => {
  const before = await api.get('/api/assistant/usage');
  const since = await lastSeq();
  await askOk(api, 'Which sensors are down?');
  const calls = await modelCalls(since);
  const t = calls.reduce((a, c) => a + c.usage.inputTokens + c.usage.outputTokens + c.usage.cacheWriteInputTokens + 0.1 * c.usage.cacheReadInputTokens, 0);
  const after = await api.get('/api/assistant/usage');
  expect(Math.abs(after.tokens - before.tokens - t), `tokens delta vs mock ${t}`).toBeLessThanOrEqual(1);
  expect(after.by_purpose.find((p: any) => p.purpose === 'ask').calls).toBeGreaterThanOrEqual(2);
  expect(after.top_tools.find((x: any) => x.tool === 'list_problems')).toBeTruthy();
  expect(after.errors.map((e: any) => e.code)).toEqual(expect.arrayContaining(['bedrock_access_denied']));
  expect(after.by_user.some((u: any) => u.username === 'admin')).toBe(true);
  expect(JSON.stringify(after), 'usage never contains conversation content').not.toContain('Which sensors are down');
  await loginViaApi(page, undefined, '/assistant/usage');
  await expect(page.getByTestId('usage-tiles')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('usage-chart')).toBeVisible();
  await expect(page.getByTestId('usage-tiles')).toContainText(/\$|USD/);
  await shot3b(page, '66-usage');
});
