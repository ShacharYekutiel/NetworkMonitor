// [3b] Tools gated by features (docs/architecture.md §14.4, §14.6.1, §14.11 spec 65; api.md "[3b] Tools"):
// syslog OFF -> search_syslog/syslog_stats not offered and the answer says so; ON -> offered and used; config tools
// only with config_backup + send_config_excerpts + config_backup_mask_secrets; config_diff result masked (no
// secret of the unmasked config ever reaches the mock).
import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { apiAs, type Api } from '../lib/helpers';
import { SIM_DIR } from '../lib/lab';
import { SSH, ensureDeviceAt, ensureGroupNamed, sendSyslog, setFeatures, sshCreds, sshState, waitBackup } from '../lib/lab3';
import { PHASE3B, askOk, ensureAssistant, lastSeq, mock, modelCalls } from '../lib/lab3b';

// default mode (not serial): one worker runs the tests in order; a failure restarts the worker (beforeAll is
// idempotent) instead of skipping the remaining tests, so every contract item gets a verdict
test.describe.configure({ mode: 'default' });
test.skip(!PHASE3B, 'phase-3b Bedrock mock not running');

let api: Api;
let dev: any;
const CONFIG_TOOLS = ['config_versions', 'config_diff', 'config_search'];
const SYSLOG_TOOLS = ['search_syslog', 'syslog_stats'];
const SRC = '127.0.0.113';

/** Secret values of the unmasked lab config (they must never leave the server). */
function unmaskedSecrets(): string[] {
  const t = readFileSync(join(SIM_DIR, 'ssh', 'expected', SSH.cx.name, 'v2.unmasked.txt'), 'utf8');
  const out = new Set<string>();
  for (const m of t.matchAll(/(?:password|secret|community|key)\s+(?:\S+\s+)?(?:\d\s+)?(\S{6,})/gi)) out.add(m[1]);
  return [...out].filter((s) => !/^(ciphertext|plaintext|cipher|simple|sha|md5|aes\d*|read|write|ro|rw)$/i.test(s));
}

test.beforeAll(async () => {
  test.setTimeout(240_000);
  api = await apiAs();
  await ensureAssistant(api);
  await mock.clearModes();
  await setFeatures(api, { syslog: false, config_backup: true });
  await api.put('/api/assistant/settings', { send_config_excerpts: false });
  await api.put('/api/settings', { config_backup_mask_secrets: true });
  const g = await ensureGroupNamed(api, 'QA Assistant 3b', { ssh_credentials: sshCreds() });
  dev = await ensureDeviceAt(api, g.id, SSH.cx.ip, 'qa3b-cx1', { config_backup_driver: SSH.cx.driver });
  await sshState(SSH.cx.name, { version: 'v1' });
  await api.post(`/api/devices/${dev.id}/config/backup`).catch(() => undefined);
  await waitBackup(api, dev.id, (s) => ['ok', 'unchanged'].includes(s.last_result));
  await sshState(SSH.cx.name, { version: 'v2' });
  await api.post(`/api/devices/${dev.id}/config/backup`).catch(() => undefined);
  await waitBackup(api, dev.id, (s) => s.last_result === 'ok' && s.versions >= 2, 120_000).catch(async () => waitBackup(api, dev.id, (s) => s.last_result === 'ok'));
});
test.afterAll(async () => { await sshState(SSH.cx.name, { version: 'v1' }).catch(() => undefined); });

test('syslog OFF: syslog tools not offered, status says why, prompt lists it as turned off, answer says so', async () => {
  const since = await lastSeq();
  const { msg } = await askOk(api, 'Show me the syslog messages of the last day');
  const c1 = (await modelCalls(since))[0];
  for (const t of SYSLOG_TOOLS) expect(c1.offered_tools).not.toContain(t);
  expect(c1.body.system[0].text).toMatch(/Turned off in NetworkMonitor:[^\n]*syslog/i);
  expect(msg.content).toContain('Syslog is turned off');
  expect(msg.tool_calls).toBe(0);
  const st = await api.get('/api/assistant/status');
  const row = st.tools.find((t: any) => t.name === 'search_syslog');
  expect(row).toMatchObject({ available: false, reason: 'feature_off:syslog' });
});

test('syslog ON: offered and used; results cite nm:syslog refs; message capped and marked untrusted', async () => {
  await setFeatures(api, { syslog: true });
  sendSyslog(SRC, ['<13>Nov 10 10:00:00 qa3b-cx1 qa3b: QA3B-SYSLOG-MARK interface 1/1/1 up']);
  await expect.poll(async () => (await api.get('/api/syslog?q=QA3B-SYSLOG-MARK&limit=5').catch(() => ({ items: [] }))).items?.length ?? 0, { timeout: 20_000 }).toBeGreaterThan(0);
  const since = await lastSeq();
  const { msg } = await askOk(api, 'Show me the syslog messages of the last day');
  const calls = await modelCalls(since);
  expect(calls[0].offered_tools).toEqual(expect.arrayContaining(SYSLOG_TOOLS));
  expect(msg.steps.map((s: any) => s.tool)).toEqual(['search_syslog']);
  const env = calls[1].body.messages.at(-1).content[0].toolResult.content[0].json;
  expect(env.untrusted_fields).toEqual(expect.arrayContaining(['message']));
  expect(env.rows).toBeLessThanOrEqual(100);
  for (const it of env.data.items) expect(String(it.message ?? '').length).toBeLessThanOrEqual(500);
  expect(JSON.stringify(env)).toContain('QA3B-SYSLOG-MARK');
  expect(msg.content).toMatch(/\(nm:syslog\/\d+\)/);
});

test('config tools: not offered without send_config_excerpts; idle reason in status', async () => {
  const since = await lastSeq();
  const { msg } = await askOk(api, `[qa:config-diff] what changed in the configuration of device ${dev.id}?`);
  const c1 = (await modelCalls(since))[0];
  for (const t of CONFIG_TOOLS) expect(c1.offered_tools).not.toContain(t);
  expect(msg.content).toMatch(/not available/i);
  const st = await api.get('/api/assistant/status');
  expect(st.tools.find((t: any) => t.name === 'config_diff').available).toBe(false);
});

test('config tools: offered with send_config_excerpts + masking; config_diff result masked; never with masking OFF', async () => {
  await api.put('/api/assistant/settings', { send_config_excerpts: true });
  const since = await lastSeq();
  const { msg } = await askOk(api, `[qa:config-diff] what changed in the configuration of device ${dev.id}?`);
  const calls = await modelCalls(since);
  expect(calls[0].offered_tools).toEqual(expect.arrayContaining(CONFIG_TOOLS));
  expect(msg.steps[0]).toMatchObject({ tool: 'config_diff', ok: true });
  const env = calls[1].body.messages.at(-1).content[0].toolResult.content[0].json;
  expect(env.ok).toBe(true);
  expect(env.untrusted_fields).toEqual(expect.arrayContaining(['diff']));
  const blob = JSON.stringify(calls);
  for (const s of unmaskedSecrets()) expect(blob, `unmasked secret ${s.slice(0, 3)}… sent to the model`).not.toContain(s);
  expect(msg.content).toMatch(/\(nm:config(-diff)?\/\d+/);
  // masking OFF -> config tools never offered (stored configs then contain secrets)
  await api.put('/api/settings', { config_backup_mask_secrets: false });
  const s2 = await lastSeq();
  await askOk(api, `[qa:config-diff] what changed in the configuration of device ${dev.id}?`);
  for (const t of CONFIG_TOOLS) expect((await modelCalls(s2))[0].offered_tools).not.toContain(t);
  await api.put('/api/settings', { config_backup_mask_secrets: true });
  // config_backup OFF -> not offered either
  await setFeatures(api, { config_backup: false });
  const s3 = await lastSeq();
  await askOk(api, `[qa:config-diff] what changed in the configuration of device ${dev.id}?`);
  for (const t of CONFIG_TOOLS) expect((await modelCalls(s3))[0].offered_tools).not.toContain(t);
  await setFeatures(api, { config_backup: true });
  await api.put('/api/assistant/settings', { send_config_excerpts: false });
});

test('flows / traps / new devices / timeline tools follow their features', async () => {
  await setFeatures(api, { flows: false, traps: false, new_device_alerts: false, timeline: false });
  let since = await lastSeq();
  await askOk(api, 'Who are the top talkers on the uplink in the last hour?');
  let offered = (await modelCalls(since))[0].offered_tools;
  for (const t of ['flow_top', 'flow_interface_series', 'search_traps', 'list_new_devices', 'get_timeline']) expect(offered).not.toContain(t);
  await setFeatures(api, { flows: true, traps: true, new_device_alerts: true, timeline: true });
  since = await lastSeq();
  await askOk(api, 'Who are the top talkers on the uplink in the last hour?');
  offered = (await modelCalls(since))[0].offered_tools;
  for (const t of ['flow_top', 'flow_interface_series', 'search_traps', 'list_new_devices', 'get_timeline']) expect(offered).toContain(t);
  expect(offered).not.toContain('search_knowledge');
  await setFeatures(api, { flows: false });
});

test('tool errors go back as status error + envelope; the model retries (invalid_arguments)', async () => {
  const since = await lastSeq();
  const { msg } = await askOk(api, 'please check [qa:retry]');
  const calls = await modelCalls(since);
  expect(calls.length).toBe(3);
  const tr = calls[1].body.messages.at(-1).content[0].toolResult;
  expect(tr.status).toBe('error');
  expect(tr.content[0].json).toMatchObject({ ok: false, tool: 'get_device', error: 'invalid_arguments' });
  expect(msg.steps[0]).toMatchObject({ tool: 'get_device', ok: false, error: 'invalid_arguments' });
  expect(msg.steps[1]).toMatchObject({ tool: 'list_problems', ok: true });
  expect(msg.status).toBe('complete');
});

test('three rounds and parallel tool calls (<= 4 at once): one assistant turn with N toolUse, one user turn with N results', async () => {
  let since = await lastSeq();
  let { msg } = await askOk(api, 'investigate the problems [qa:3rounds]');
  expect((await modelCalls(since)).length).toBe(4);
  expect(msg.steps.map((s: any) => s.round)).toEqual([1, 2, 3]);
  expect(msg.notes.map((n: any) => n.round)).toEqual(expect.arrayContaining([1, 2]));
  since = await lastSeq();
  ({ msg } = await askOk(api, 'overview [qa:parallel]'));
  const calls = await modelCalls(since);
  expect(calls.length).toBe(2);
  const last = calls[1].body.messages;
  expect(last.at(-2).content.filter((b: any) => b.toolUse).length).toBe(3);
  expect(last.at(-1).content.filter((b: any) => b.toolResult).length).toBe(3);
  expect(msg.tool_calls).toBe(3);
});
