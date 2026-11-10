// [3b] Config diff explanation and risk flags (docs/architecture.md §14.8.4, §14.11 spec 71; data-model
// § Diff risk-flag vectors): Comware change adding `snmp-agent community write` and removing `ssh server acl`
// (tests/sim/ssh/configs/comware/risk-a.txt -> risk-b.txt) -> deterministic high flags + AI explanation (forced
// tool report_diff_explanation, masked diff only); mock error -> flags still shown; idle without config excerpts.
import { expect, test } from '@playwright/test';
import { apiAs, loginViaApi, type Api } from '../lib/helpers';
import { raw } from '../lib/lab';
import { SSH, ensureDeviceAt, ensureGroupNamed, setFeatures, sshCreds, sshState, versions, waitBackup } from '../lib/lab3';
import { PHASE3B, audit, ensureAssistant, lastSeq, mock, modelCalls, shot3b } from '../lib/lab3b';

// default mode (not serial): one worker runs the tests in order; a failure restarts the worker (beforeAll is
// idempotent) instead of skipping the remaining tests, so every contract item gets a verdict
test.describe.configure({ mode: 'default' });
test.skip(!PHASE3B, 'phase-3b Bedrock mock not running');

let api: Api;
let dev: any;
let from = 0, to = 0;

async function backup(state: string): Promise<void> {
  await sshState(SSH.cw.name, { version: state });
  await api.post(`/api/devices/${dev.id}/config/backup`).catch(() => undefined);
  await waitBackup(api, dev.id, (s) => ['ok', 'unchanged'].includes(s.last_result), 120_000);
}

test.beforeAll(async () => {
  test.setTimeout(300_000);
  api = await apiAs();
  await ensureAssistant(api);
  await mock.clearModes();
  await setFeatures(api, { config_backup: true, ai_config_diff: true });
  await api.put('/api/settings', { config_backup_mask_secrets: true });
  await api.put('/api/assistant/settings', { send_config_excerpts: true });
  const g = await ensureGroupNamed(api, 'QA Assistant 3b', { ssh_credentials: sshCreds() });
  dev = await ensureDeviceAt(api, g.id, SSH.cw.ip, 'qa3b-cw1', { config_backup_driver: SSH.cw.driver });
  await backup('risk-a');
  await backup('risk-b');
  const v = await versions(api, dev.id);
  [to, from] = [v[0].id, v[1].id];
});
test.afterAll(async () => {
  await sshState(SSH.cw.name, { version: 'v1' }).catch(() => undefined);
  await api.put('/api/assistant/settings', { send_config_excerpts: false });
});

test('deterministic flags: snmp_community_added (high), mgmt_acl_removed (high), ntp_changed (info)', async () => {
  const r = await api.get(`/api/assistant/config-diff/flags?device_id=${dev.id}&from=${from}&to=${to}`);
  const ids = Object.fromEntries(r.flags.map((f: any) => [f.rule_id, f]));
  expect(ids.snmp_community_added).toMatchObject({ severity: 'high', side: 'added', title_key: 'diffRisk.snmp_community_added' });
  expect(ids.mgmt_acl_removed).toMatchObject({ severity: 'high', side: 'removed' });
  expect(ids.ntp_changed).toMatchObject({ severity: 'info', side: 'added' });
  for (const f of r.flags) expect(f.lines.length).toBeGreaterThan(0);
});

test('explanation: forced report_diff_explanation over the masked diff; risks lines validated; cached per pair', async () => {
  const since = await lastSeq();
  const e = await api.post('/api/assistant/config-diff/explain', { device_id: dev.id, from, to, refresh: false });
  expect(e.status).toBe('ok');
  expect(e.ai.summary.length).toBeGreaterThan(0);
  expect(e.ai.summary.length).toBeLessThanOrEqual(5);
  expect(e.ai.risks.map((x: any) => x.severity)).toContain('high');
  expect(e.flags.map((f: any) => f.rule_id)).toEqual(expect.arrayContaining(['snmp_community_added', 'mgmt_acl_removed']));
  const calls = (await modelCalls(since)).filter((c) => c.purpose === 'diff_explain');
  expect(calls.length).toBe(1);
  expect(calls[0]).toMatchObject({ op: 'converse', forced_tool: 'report_diff_explanation' });
  expect(calls[0].body.inferenceConfig.maxTokens).toBeLessThanOrEqual(800);
  const body = JSON.stringify(calls[0].body);
  expect(body).toContain('snmp-agent community write');
  expect(body, 'secret masked before it left').not.toContain('UVFJTkpfV1JJVEVfQ09NTVVOSVRZ');
  expect(body).toContain('ssh server acl 2001');
  // same pair again -> cached (no model call)
  const s2 = await lastSeq();
  await api.post('/api/assistant/config-diff/explain', { device_id: dev.id, from, to, refresh: false });
  expect((await modelCalls(s2)).length).toBe(0);
  expect((await audit(api, 'action=assistant.diff_explain'))[0].changes).toBeTruthy();
});

test('UI: Explain changes panel shows coloured flags and the AI text (labelled)', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${dev.id}?tab=configuration&from=${from}&to=${to}`);
  const panel = page.getByTestId('diff-explain');
  await expect(panel).toBeVisible({ timeout: 30_000 });
  await expect(panel.getByTestId('diff-flags')).toContainText(/snmp|SNMP|diffRisk\.snmp_community_added/);
  await panel.getByTestId('diff-explain-button').click().catch(() => undefined);
  await expect(panel.getByTestId('diff-ai')).toContainText('SNMP write community added', { timeout: 60_000 });
  await expect(page.getByTestId('ai-notice').first()).toBeVisible();
  await shot3b(page, '71-diff-explain');
});

test('mock error -> flags still shown, explanation area shows the error', async ({ page }) => {
  await mock.mode('unavailable');
  const e = await api.post('/api/assistant/config-diff/explain', { device_id: dev.id, from, to, refresh: true });
  expect(e.status).toBe('failed');
  expect(e.error.code).toBe('bedrock_unavailable');
  expect(e.flags.length).toBeGreaterThanOrEqual(2);
  await loginViaApi(page, undefined, `/devices/${dev.id}?tab=configuration&from=${from}&to=${to}`);
  await expect(page.getByTestId('diff-flags')).toBeVisible({ timeout: 30_000 });
  await mock.clearModes();
});

test('idle without send_config_excerpts -> 409 feature_idle; feature OFF -> 409 feature_disabled', async () => {
  await api.put('/api/assistant/settings', { send_config_excerpts: false });
  const r = await raw(api, 'POST', '/api/assistant/config-diff/explain', { device_id: dev.id, from, to, refresh: true });
  expect(r.status).toBe(409);
  expect(r.body.error).toMatchObject({ code: 'feature_idle', details: [{ feature: 'ai_config_diff', needs: ['send_config_excerpts'] }] });
  await setFeatures(api, { ai_config_diff: false });
  const r2 = await raw(api, 'POST', '/api/assistant/config-diff/explain', { device_id: dev.id, from, to, refresh: true });
  expect(r2.body.error.code).toBe('feature_disabled');
  await setFeatures(api, { ai_config_diff: true });
  await api.put('/api/assistant/settings', { send_config_excerpts: true });
});
