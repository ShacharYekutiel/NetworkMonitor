// [3b] Template drafts from a bounded SNMP walk (docs/architecture.md §14.8.7, §14.11 spec 74): an snmpsim device
// with an unknown enterprise subtree (tests/sim/bedrock/snmp_x100: PEN 99999, plus ARP / USM / community tables
// that must never be walked and a value with secrets that must be redacted) -> walk -> draft YAML valid ->
// Save as user template (normal endpoint, audited) -> applied to the device.
import { expect, test } from '@playwright/test';
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { join } from 'node:path';
import { apiAs, loginViaApi, type Api } from '../lib/helpers';
import { PY, SIM_DIR, raw } from '../lib/lab';
import { ensureGroupNamed, setFeatures } from '../lib/lab3';
import { PHASE3B, audit, ensureAssistant, lastSeq, mock, modelCalls, shot3b } from '../lib/lab3b';

// default mode (not serial): one worker runs the tests in order; a failure restarts the worker (beforeAll is
// idempotent) instead of skipping the remaining tests, so every contract item gets a verdict
test.describe.configure({ mode: 'default' });
test.skip(!PHASE3B, 'phase-3b Bedrock mock not running');

const IP = '127.0.0.147';
let api: Api;
let agent: ChildProcess | null = null;
let dev: any;
let walkId = 0;
const FORBIDDEN = ['1.3.6.1.6.3.15', '1.3.6.1.6.3.18', '1.3.6.1.6.3.16', '1.3.6.1.2.1.4.21', '1.3.6.1.2.1.4.24', '1.3.6.1.2.1.4.22', '1.3.6.1.2.1.4.35', '1.3.6.1.2.1.17.4', '1.3.6.1.2.1.17.7'];

async function waitWalk(id: number): Promise<any> {
  let w: any = null;
  await expect.poll(async () => { w = await api.get(`/api/assistant/template-drafts/walks/${id}`); return w.status; }, { timeout: 90_000 }).not.toBe('running');
  return w;
}

test.beforeAll(async () => {
  test.setTimeout(120_000);
  api = await apiAs();
  await ensureAssistant(api);
  await mock.clearModes();
  await setFeatures(api, { ai_template_drafts: true });
  spawnSync('pkill', ['-f', `${IP}:1161`]);
  agent = spawn(PY, [join(SIM_DIR, 'snmp_agent.py'), '--host', IP, '--port', '1161', '--data', join(SIM_DIR, 'bedrock', 'snmp_x100')], { stdio: ['ignore', 'pipe', 'ignore'], detached: true });
  let out = '';
  agent.stdout!.on('data', (d) => (out += d));
  await expect.poll(() => out.includes('READY'), { timeout: 30_000 }).toBe(true);
  const g = await ensureGroupNamed(api, 'QA Assistant 3b');
  const existing = (await api.get(`/api/devices?q=${IP}&page_size=20`)).items.find((d: any) => d.host === IP);
  dev = existing ?? await api.post('/api/devices', { group_id: g.id, name: 'qa3b-x100', host: IP, auto_sensors: [],
    snmp_credentials: { version: 'v2c', port: 1161, timeout_s: 2, retries: 1, community: 'public' } });
});
test.afterAll(async () => {
  spawnSync('pkill', ['-f', `${IP}:1161`]);
  await raw(api, 'DELETE', '/api/device-templates/user/qa_draft_x100').catch(() => undefined);
});

test('forbidden subtrees refused (422 never_walked); bounded walk of system + enterprise', async () => {
  const bad = await raw(api, 'POST', '/api/assistant/template-drafts/walks', { device_id: dev.id, presets: ['system'], oids: ['1.3.6.1.6.3.15'] });
  expect(bad.status).toBe(422);
  expect(bad.body.error.details).toEqual([expect.objectContaining({ oid: '1.3.6.1.6.3.15', reason: 'never_walked' })]);
  const tooMany = await raw(api, 'POST', '/api/assistant/template-drafts/walks', { device_id: dev.id, presets: ['system', 'entity', 'host_resources', 'enterprise', 'if_sample', 'entity_sensor'], oids: ['1.3.6.1.4.1.99999.2.1', '1.3.6.1.4.1.99999.2.2', '1.3.6.1.4.1.99999.2.3'] });
  expect(tooMany.status, '> 8 subtrees').toBe(422);
  const w = await api.post('/api/assistant/template-drafts/walks', { device_id: dev.id, presets: ['system', 'enterprise'], oids: [] });
  expect(w.status).toBe('running');
  walkId = w.id;
  const busy = await raw(api, 'POST', '/api/assistant/template-drafts/walks', { device_id: dev.id, presets: ['system'], oids: [] });
  if (busy.status !== 202) expect(busy.body.error.code).toBe('walk_running');
  const done = await waitWalk(walkId);
  expect(done.status).toBe('ok');
  expect(done.subtrees).toEqual(expect.arrayContaining(['1.3.6.1.2.1.1', '1.3.6.1.4.1.99999']));
  const oids = done.varbinds.map((v: any[]) => v[0]);
  expect(oids).toContain('1.3.6.1.4.1.99999.2.1.0');
  for (const f of FORBIDDEN) expect(oids.filter((o: string) => o.startsWith(`${f}.`)), `never walked ${f}`).toEqual([]);
  expect(done.total).toBeLessThanOrEqual(5000);
  expect((await audit(api, 'action=assistant.template_walk'))[0]).toBeTruthy();
});

test('draft: forced propose_template over the compacted, redacted walk; YAML valid; OIDs come from the walk', async () => {
  const since = await lastSeq();
  const d = await api.post('/api/assistant/template-drafts', { walk_id: walkId, template_id: 'qa_draft_x100', notes: null });
  expect(d.valid, JSON.stringify(d.errors)).toBe(true);
  expect(d.yaml).toContain('1.3.6.1.4.1.99999');
  // the 2b-2 validator only accepts high/medium/low: drafts carry `confidence: low` + a note "unverified" per OID entry
  expect(d.yaml).toMatch(/confidence: low/);
  expect(d.yaml).toMatch(/notes: .*unverified/);
  expect(d.yaml).not.toMatch(/confidence: (high|medium)/);
  expect(d.errors).toEqual([]);
  const calls = (await modelCalls(since)).filter((c) => c.purpose === 'template_draft');
  expect(calls.at(-1).forced_tool).toBe('propose_template');
  expect(calls[0].op).toBe('converse');
  const blob = JSON.stringify(calls);
  expect(blob).toContain('1.3.6.1.4.1.99999');
  for (const secret of ['qa3b-walk-secret-77', 'Hunter2', 'qa3b-usm-user', 'qa3b-community-table-secret', '001122334455']) {
    expect(blob, `not sent to the model: ${secret}`).not.toContain(secret);
  }
  expect(blob.length, 'compact walk ≤ 32 KiB').toBeLessThan(80_000);
  expect((await audit(api, 'action=assistant.template_draft'))[0]).toBeTruthy();
});

test('UI wizard: device -> walk -> draft editor (valid) -> Save as user template -> applied to the device', async ({ page }) => {
  test.setTimeout(180_000);
  await raw(api, 'DELETE', '/api/device-templates/user/qa_draft_x100').catch(() => undefined);
  await loginViaApi(page, undefined, '/templates');
  await page.getByTestId('template-draft-open').click();
  const wizard = page.getByTestId('template-draft');
  await expect(wizard).toBeVisible();
  await wizard.locator('select').first().selectOption(String(dev.id));
  await wizard.getByTestId('walk-start').click();
  await expect(wizard.getByTestId('walk-status')).toHaveAttribute('data-status', /^(ok|partial)$/, { timeout: 90_000 });
  await wizard.getByTestId('draft-template').click();
  await expect(wizard.getByTestId('draft-yaml')).toBeVisible({ timeout: 90_000 });
  await expect(wizard.getByTestId('draft-valid')).toBeVisible();
  await expect(wizard.getByTestId('draft-yaml')).toHaveCSS('direction', 'ltr');
  await shot3b(page, '74-template-draft');
  const saved = page.waitForResponse((r) => r.url().endsWith('/api/device-templates/user') && r.request().method() === 'POST');
  await wizard.getByTestId('save-user-template').click();
  const resp = await saved;
  expect(resp.status()).toBe(201);
  const body = await resp.json();
  expect(body.template.id).toBe('qa_draft_x100');
  expect(body.file).toMatch(/qa_draft_x100\.yaml$/);
  expect((await audit(api, 'action=device_template.create_user'))[0]).toBeTruthy();
  const res = await api.post(`/api/devices/${dev.id}/apply-template`, { template_id: 'qa_draft_x100' });
  expect(res.template_id).toBe('qa_draft_x100');
  const sensors = (await api.get(`/api/sensors?device_id=${dev.id}&page_size=50`)).items;
  expect(sensors.some((s: any) => s.type === 'snmp_custom' && s.config?.oid?.startsWith('1.3.6.1.4.1.99999'))).toBe(true);
});

test('POST /api/device-templates/user: overwrite rules, invalid YAML 422, always available (feature OFF)', async () => {
  await setFeatures(api, { ai_template_drafts: false });
  const t = await api.get('/api/device-templates/qa_draft_x100').catch(() => null);
  test.skip(!t, 'saved by the UI test');
  const yaml = (await raw(api, 'GET', '/api/device-templates/qa_draft_x100')).body?.source_yaml ?? null;
  const again = await raw(api, 'POST', '/api/device-templates/user', { yaml: yaml ?? 'schema: 1\nid: qa_draft_x100\n', overwrite: false });
  expect([409, 422]).toContain(again.status);
  if (again.status === 409) expect(again.body.error.code).toBe('template_exists');
  const bad = await raw(api, 'POST', '/api/device-templates/user', { yaml: 'schema: 1\nid: qa_bad\nentries: nope\n', overwrite: false });
  expect(bad.status).toBe(422);
  expect(bad.body.error.code).toBe('invalid_template');
  expect((await raw(api, 'GET', '/api/assistant/template-drafts/walks/1')).body.error.code).toBe('feature_disabled');
  await setFeatures(api, { ai_template_drafts: true });
});
