// [3b] Assistant settings, configure-first switch, Test connection, certificates, access control
// (docs/architecture.md §14.0, §14.2.3, §14.2.6, §14.7, §14.11 spec 63; api.md "[3b] Assistant settings, test,
// certificate"). Owner decisions: default us-east-1 + `us.` profile, il-central-1 + `global.`; USD prices; Roles
// Anywhere recommended (CSR flow + cert upload). Leaves the assistant ON (access key, mock) for the later specs.
import { expect, test } from '@playwright/test';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { VIEWER, apiAs, loginViaApi, type Api } from '../lib/helpers';
import { SIM_DIR, ensureViewer, raw } from '../lib/lab';
import { py } from '../lib/lab3';
import { setFeatures } from '../lib/lab3';
import {
  AI_FEATURES, BEDROCK_PKI, IL_MODEL, MOCK_ENDPOINT, MODEL, PHASE3B, RA, akSettings, assistantSettings, audit, enableAssistant, ensureAdmin2,
  mock, pkiFile, raSettings, shot3b, testConnection, tokenApi, uploadCert,
} from '../lib/lab3b';

// default mode (not serial): one worker runs the tests in order; a failure restarts the worker (beforeAll is
// idempotent) instead of skipping the remaining tests, so every contract item gets a verdict
test.describe.configure({ mode: 'default' });
test.skip(!PHASE3B, 'phase-3b Bedrock mock not running');

let api: Api;
const manifest = () => JSON.parse(readFileSync(pkiFile('manifest.json'), 'utf8'));
// endpoints that answer 409 feature_disabled while assistant is OFF, and the "prepare" ones that do not
const GATED: [string, string, unknown?][] = [
  ['POST', '/api/assistant/ask', { conversation_id: null, question: 'hi' }],
  ['GET', '/api/assistant/conversations'],
  ['GET', '/api/assistant/status'],
];
// contract ambiguity: data-model lists /api/assistant/knowledge as gated by `assistant`, api.md calls it a
// "prepare" endpoint ("allowed while ai_knowledge OFF; needs assistant settings") -> accepted as prepare here
const PREPARE: [string, string][] = [['GET', '/api/assistant/settings'], ['GET', '/api/assistant/usage'], ['GET', '/api/assistant/knowledge']];

test.beforeAll(async () => {
  api = await apiAs();
  await mock.reset();
  const s = await assistantSettings(api).catch(() => null);
  if (s?.enabled) await api.put('/api/assistant/settings', { enabled: false });
});

test('fresh install: assistant and the 10 AI features OFF, USD prices, health "disabled", 409 except prepare endpoints', async () => {
  const f = (await api.get('/api/settings/public')).features;
  for (const n of ['assistant', ...AI_FEATURES]) expect(f[n], `feature ${n}`).toBe(false);
  expect((await api.get('/api/health')).assistant).toBe('disabled');
  const s = await assistantSettings(api);
  expect(s).toMatchObject({ enabled: false, state: 'disabled', secret_access_key: null, certificate: null });
  expect(s.prices.currency, 'owner decision: USD').toBe('USD');
  expect(s.ra_session_seconds).toBe(3600);
  expect(s.send_config_excerpts).toBe(false);
  // [1.4.0] pentest finding B: AI features mask internal IPs by default (architecture.md § 14, redaction item 3)
  expect(s.redact_internal_ips).toBe(true);
  expect(s.chat_redact_ips).toBe(true);
  for (const [m, p, b] of GATED) {
    const r = await raw(api, m, p, b);
    expect(r.status, `${m} ${p}`).toBe(409);
    expect(r.body.error.code).toBe('feature_disabled');
    expect(r.body.error.details).toEqual([expect.objectContaining({ feature: 'assistant' })]);
  }
  for (const [m, p] of PREPARE) expect((await raw(api, m, p)).status, `${m} ${p} (prepare)`).toBe(200);
});

test('UI: no nav entry; Settings → Assistant shows the form with a locked bottom switch and the hint', async ({ page }) => {
  await loginViaApi(page, undefined, '/');
  await expect(page.locator('nav a[href="/assistant"]')).toHaveCount(0);
  await page.goto('/settings?tab=assistant');
  await expect(page.getByTestId('assistant-connection')).toBeVisible({ timeout: 20_000 });
  const sw = page.getByTestId('assistant-switch').getByRole('switch');
  await expect(sw).toBeDisabled();
  await expect(sw).not.toBeChecked();
  await expect(page.getByTestId('enable-hint')).toBeVisible();
  await expect(page.getByTestId('test-connection')).toBeVisible();
  // the switch is at the bottom of the form, after the Test button
  const order = await page.evaluate(() => {
    const t = document.querySelector('[data-testid="test-connection"]')!;
    const s = document.querySelector('[data-testid="assistant-switch"]')!;
    return t.compareDocumentPosition(s) & Node.DOCUMENT_POSITION_FOLLOWING;
  });
  expect(order, 'switch follows the Test button').toBeTruthy();
  await shot3b(page, '63-settings-off');
});

test('cannot be enabled without a test: PUT /api/settings and PUT /api/assistant/settings -> 422 assistant_test_required', async () => {
  await setFeatures(api, { audit: true }); // isolate from the audit dependency check (order unspecified)
  for (const key of ['feature_assistant_enabled', 'feature_ai_knowledge_enabled']) {
    const r = await raw(api, 'PUT', '/api/settings', { [key]: true });
    expect(r.status, key).toBe(422);
    expect(r.body.error.code).toBe('assistant_test_required');
  }
  expect((await raw(api, 'PUT', '/api/settings', { feature_assistant_enabled: false })).status, 'OFF is accepted').toBe(200);
  const r = await raw(api, 'PUT', '/api/assistant/settings', { ...akSettings(), enabled: true });
  expect(r.status).toBe(422);
  expect(r.body.error.code).toBe('assistant_test_required');
  const fake = await raw(api, 'PUT', '/api/assistant/settings', { ...akSettings(), enabled: true, test_id: '0123456789abcdef0123456789abcdef' });
  expect(fake.body.error.code).toBe('assistant_test_required');
});

test('Test with a bad secret -> aws_auth_failed (mock saw InvalidSignatureException); switch stays locked', async ({ page }) => {
  await mock.clearRequests();
  const t = await testConnection(api, akSettings({ secret_access_key: 'wrong-secret-0000000000000000000000000000' }));
  expect(t.status).toBe(200);
  expect(t.body.ok).toBe(false);
  expect(t.body.test_id).toBeNull();
  expect(t.body.error.code).toBe('aws_auth_failed');
  const failed = t.body.steps.find((s: any) => !s.ok);
  expect(failed.error_code).toBe('aws_auth_failed');
  const reqs = await mock.requests('op=converse_stream');
  expect(reqs.length, 'converse step reached the mock').toBeGreaterThan(0);
  expect(reqs.every((r) => r.sig.error === 'InvalidSignatureException')).toBe(true);
  // UI: store the bad values (OFF: no test needed), Test -> the mapped message, switch locked
  await api.put('/api/assistant/settings', akSettings({ secret_access_key: 'wrong-secret-0000000000000000000000000000' }));
  await loginViaApi(page, undefined, '/settings?tab=assistant');
  await page.getByTestId('test-connection').click();
  await expect(page.getByTestId('test-result')).toHaveAttribute('data-ok', 'false', { timeout: 60_000 });
  await expect(page.getByTestId('test-result')).toContainText(/AWS rejected the credentials|aws_auth_failed/);
  await expect(page.getByTestId('assistant-switch').getByRole('switch')).toBeDisabled();
  await shot3b(page, '63-test-bad-secret');
  // the secret is write-only
  const s = await assistantSettings(api);
  expect(s.secret_access_key).toBeNull();
  expect(s.secret_access_key_set).toBe(true);
  expect(JSON.stringify(s)).not.toContain('wrong-secret');
});

test('certificate upload: invalid ones refused (422), .pfx accepted, metadata only (never the key), audited', async () => {
  const m = manifest();
  const cases: [string, Parameters<typeof uploadCert>[2], string][] = [
    ['leaf-catrue', {}, 'invalid_certificate'],
    ['leaf-expired', {}, 'invalid_certificate'],
    ['leaf-nodigsig', {}, 'invalid_certificate'],
    ['leaf-rsa1024', {}, 'invalid_certificate'],
    ['leaf-rsa', { key: 'leaf-ec.key' }, 'key_mismatch'],
    ['leaf-rsa', { pfx: true, password: 'wrong-password' }, 'invalid_private_key'],
    ['leaf-rsa', { key: 'leaf-rsa.enc.key', password: 'wrong-password' }, 'invalid_private_key'],
  ];
  for (const [leaf, opts, code] of cases) {
    const r = await uploadCert(api, leaf, opts);
    expect(r.status, `${leaf} ${JSON.stringify(opts)}: ${r.text}`).toBe(422);
    expect.soft(r.body.error.code, `${leaf} ${JSON.stringify(opts)}: ${r.body.error.message}`).toBe(code);
    expect(r.text).not.toMatch(/PRIVATE KEY/);
  }
  // PEM bundle + encrypted PKCS#8 key works too (then replaced by the .pfx)
  const pem = await uploadCert(api, 'leaf-ec', { key: 'leaf-ec.sec1.key' });
  expect(pem.status, pem.text).toBe(200);
  expect(pem.body).toMatchObject({ source: 'uploaded', key_type: 'ec', key_bits: 256, serial_decimal: m.leaves['leaf-ec'].serial_decimal });
  const r = await uploadCert(api, 'leaf-rsa', { pfx: true });
  expect(r.status, r.text).toBe(200);
  expect(r.body).toMatchObject({
    source: 'uploaded', key_type: 'rsa', key_bits: 2048, serial_decimal: '4660', key_unreadable: false,
    sha256: expect.stringMatching(new RegExp(`^${m.leaves['leaf-rsa'].sha256}$`, 'i')),
  });
  expect(r.body.subject).toContain('netmonitor-bedrock.mnidom.net');
  expect(r.body.issuer).toContain('QA Mnidom-PKI-SERV-CA');
  expect(r.body.chain).toEqual([expect.objectContaining({ subject: expect.stringContaining('QA Mnidom-PKI-SERV-CA') })]);
  expect(r.text).not.toMatch(/PRIVATE KEY|BEGIN/);
  const s = await assistantSettings(api);
  expect(s.certificate.serial_decimal).toBe('4660');
  expect(JSON.stringify(s)).not.toMatch(/PRIVATE KEY/);
  const rows = await audit(api, 'action=assistant.cert_upload');
  expect(rows.length).toBeGreaterThanOrEqual(2);
  expect(JSON.stringify(rows)).not.toMatch(/PRIVATE KEY|Pfx-pass-1|Key-pass-1/);
});

test('CSR flow (owner decision): key generated on the server, CSR downloaded, issued certificate uploaded alone', async () => {
  const r = await raw(api, 'POST', '/api/assistant/cert/csr', { common_name: 'netmonitor-bedrock.mnidom.net', key_type: 'rsa', key_size: 2048, organization: null, organizational_unit: null });
  expect(r.status, r.text).toBe(200);
  expect(r.body.pem).toContain('BEGIN CERTIFICATE REQUEST');
  expect(r.body.subject).toContain('netmonitor-bedrock.mnidom.net');
  expect(r.text).not.toMatch(/PRIVATE KEY/);
  const dl = await api.ctx.fetch('/api/assistant/cert/csr');
  expect(dl.status()).toBe(200);
  expect(dl.headers()['content-type']).toContain('application/pkcs10');
  expect(dl.headers()['content-disposition']).toContain('netmonitor-bedrock.csr');
  const dir = mkdtempSync(join(tmpdir(), 'qa3b-csr-'));
  writeFileSync(join(dir, 'req.csr'), await dl.body());
  const signed = py([join(SIM_DIR, 'bedrock', 'pki.py'), 'sign-csr', BEDROCK_PKI, join(dir, 'req.csr'), join(dir, 'issued.pem')]);
  expect(signed.status, signed.stderr).toBe(0);
  const s0 = await assistantSettings(api);
  expect(s0.csr).toMatchObject({ subject: expect.stringContaining('netmonitor-bedrock.mnidom.net') });
  expect(JSON.stringify(s0.csr)).not.toMatch(/PRIVATE KEY/);
  // a certificate for another key -> key_mismatch (the pending key stays)
  const wrong = await api.ctx.fetch('/api/assistant/cert', { method: 'POST', multipart: { file: { name: 'x.pem', mimeType: 'application/x-pem-file', buffer: readFileSync(pkiFile('leaf-ec.pem')) } } });
  expect(wrong.status()).toBe(422);
  expect((await wrong.json()).error.code).toBe('key_mismatch');
  const up = await api.ctx.fetch('/api/assistant/cert', { method: 'POST', multipart: { file: { name: 'issued.pem', mimeType: 'application/x-pem-file', buffer: readFileSync(join(dir, 'issued.pem')) } } });
  expect(up.status(), await up.text()).toBe(200);
  const info = await up.json();
  expect(info).toMatchObject({ source: 'uploaded', key_type: 'rsa', key_bits: 2048 });
  expect((await assistantSettings(api)).csr, 'pending CSR cleared').toBeNull();
  // the server-generated key signs CreateSession
  await mock.clearRequests();
  const t = await testConnection(api, raSettings());
  expect(t.body.ok, JSON.stringify(t.body.steps)).toBe(true);
  expect((await mock.requests('op=create_session')).pop().sig).toMatchObject({ ok: true, serial: info.serial_decimal });
  expect((await audit(api, 'action=assistant.csr_create')).length).toBeGreaterThan(0);
  // back to the uploaded .pfx for the following tests
  expect((await uploadCert(api, 'leaf-rsa', { pfx: true })).status).toBe(200);
});

test('UI: Roles Anywhere Test passes -> switch unlocks -> ON; nav appears; editing the region locks it again', async ({ page }) => {
  test.setTimeout(120_000);
  await setFeatures(api, { audit: true });
  if (!(await assistantSettings(api)).certificate) expect((await uploadCert(api, 'leaf-rsa', { pfx: true })).status).toBe(200);
  await api.put('/api/assistant/settings', raSettings());
  await mock.clearRequests();
  await loginViaApi(page, undefined, '/settings?tab=assistant');
  await expect(page.getByTestId('cert-info')).toContainText('netmonitor-bedrock.mnidom.net');
  const sw = page.getByTestId('assistant-switch').getByRole('switch');
  await expect(sw).toBeDisabled();
  await page.getByTestId('test-connection').click();
  await expect(page.getByTestId('test-result')).toHaveAttribute('data-ok', 'true', { timeout: 60_000 });
  await shot3b(page, '63-test-ok');
  // what reached the mock: CreateSession signed with the leaf (decimal serial), then a ConverseStream with the
  // forced dummy tool `ping`, signed with the session credentials
  const cs = (await mock.requests('op=create_session')).pop();
  expect(cs.sig).toMatchObject({ ok: true, alg: 'AWS4-X509-RSA-SHA256', serial: '4660' });
  expect(cs.body).toMatchObject({ durationSeconds: 3600, profileArn: RA.profile, roleArn: RA.role, trustAnchorArn: RA.trust_anchor });
  const cv = (await mock.requests('op=converse_stream')).pop();
  expect(cv.sig.ok).toBe(true);
  expect(cv.sig.access_key_id).toBe(cs.issued_access_key_id);
  expect(cv).toMatchObject({ model_id: MODEL, region: 'us-east-1', purpose: 'test', forced_tool: 'ping' });
  expect(cv.body.inferenceConfig.maxTokens).toBe(50);
  expect(cv.model_id_encoded, 'model id percent-encoded in the path').toBe(true);
  // unlocked -> ON
  await expect(sw).toBeEnabled();
  await sw.click();
  await expect.poll(async () => (await assistantSettings(api)).enabled, { timeout: 15_000 }).toBe(true);
  await page.reload();
  await expect(page.locator('nav a[href="/assistant"]').first()).toBeVisible({ timeout: 15_000 });
  expect((await api.get('/api/health')).assistant).toMatch(/^(ready|degraded)$/);
  // editing a connection field locks the switch again (ON: a save needs a new test)
  await page.getByTestId('region').fill('us-west-2');
  await expect(page.getByText(/Run a successful Test|changeNeedsTest|test/i).first()).toBeVisible();
  const save = await raw(api, 'PUT', '/api/assistant/settings', { region: 'us-west-2' });
  expect(save.status, 'connection change while ON without test_id').toBe(422);
  expect(save.body.error.code).toBe('assistant_test_required');
  // non-connection fields never need a test
  expect((await raw(api, 'PUT', '/api/assistant/settings', { redact_internal_ips: false, internal_cidrs: [] })).status).toBe(200);
  expect((await raw(api, 'PUT', '/api/settings', { assistant_daily_questions_per_user: 1000 })).status).toBe(200);
  const rows = await audit(api, 'action=assistant.test');
  expect(rows[0].changes).toBeTruthy();
  expect(JSON.stringify(rows[0].changes)).toMatch(/"ok"/);
});

test('regions: il-central-1 needs a global. profile (us. -> bedrock_model_invalid); us-east-1 + us. passes', async () => {
  const bad = await testConnection(api, akSettings({ region: 'il-central-1', model_id: MODEL }));
  expect(bad.body.ok).toBe(false);
  expect(bad.body.error.code).toBe('bedrock_model_invalid');
  const good = await testConnection(api, akSettings({ region: 'il-central-1', model_id: IL_MODEL }));
  expect(good.body.ok, JSON.stringify(good.body.steps)).toBe(true);
  expect(good.body.warnings, 'global. profile: warning only, never refused').toContain('global_inference_profile');
  const s = await assistantSettings(api);
  expect(s.recommended).toEqual({ region: 'us-east-1', profile_prefix: 'us' });
  expect(s.profile).toMatchObject({ scope: 'geo', prefix: 'us' });
  expect(s.profile_scopes.map((x: any) => x.scope)).toEqual(expect.arrayContaining(['global', 'geo', 'in_region', 'arn']));
  expect((await mock.requests('op=converse_stream')).pop()).toMatchObject({ region: 'il-central-1', model_id: IL_MODEL });
});

test('test_id rules: bound to the admin and the connection values; audit OFF -> 422 feature_dependency', async () => {
  const admin2 = await ensureAdmin2(api);
  const t = await testConnection(api, akSettings());
  expect(t.body.ok).toBe(true);
  expect(t.body.test_id).toMatch(/^[0-9a-f-]{16,}$/i);
  // another admin cannot use it
  const other = await raw(admin2, 'PUT', '/api/assistant/settings', { ...akSettings(), test_id: t.body.test_id });
  expect(other.body.error.code).toBe('assistant_test_required');
  // a changed connection field invalidates it
  const changed = await raw(api, 'PUT', '/api/assistant/settings', { ...akSettings({ model_id: 'us.anthropic.claude-haiku-4-5-20251001-v1:0' }), test_id: t.body.test_id });
  expect(changed.body.error.code).toBe('assistant_test_required');
  // audit OFF while assistant ON -> 422; and ON with audit OFF -> 422
  const auditOff = await raw(api, 'PUT', '/api/settings', { feature_audit_enabled: false });
  expect(auditOff.status).toBe(422);
  expect(auditOff.body.error.code).toBe('feature_dependency');
  expect(auditOff.body.error.details).toEqual([{ feature: 'assistant', requires: 'audit' }]);
  // with the matching test_id the connection change is accepted
  const ok = await raw(api, 'PUT', '/api/assistant/settings', { ...akSettings(), test_id: t.body.test_id });
  expect(ok.status, ok.text).toBe(200);
  expect(ok.body).toMatchObject({ enabled: true, auth_mode: 'access_key', model_id: MODEL, region: 'us-east-1' });
});

test('OFF is always possible; enabling with audit OFF refused; ON again (access key) for the next specs', async () => {
  const off = await raw(api, 'PUT', '/api/assistant/settings', { enabled: false });
  expect(off.status).toBe(200);
  expect((await api.get('/api/health')).assistant).toBe('disabled');
  expect((await raw(api, 'GET', '/api/assistant/conversations')).status).toBe(409);
  await setFeatures(api, { audit: false });
  const t = await testConnection(api, akSettings());
  expect(t.body.ok).toBe(true);
  const r = await raw(api, 'PUT', '/api/assistant/settings', { ...akSettings(), enabled: true, test_id: t.body.test_id });
  expect(r.status).toBe(422);
  expect(r.body.error.code).toBe('feature_dependency');
  expect(r.body.error.details).toEqual([{ feature: 'assistant', requires: 'audit' }]);
  await enableAssistant(api, akSettings());
  expect((await api.get('/api/settings/public')).features.assistant).toBe(true);
  const fe = await audit(api, 'action=feature.enable');
  expect(fe.some((e: any) => JSON.stringify(e).includes('assistant'))).toBe(true);
});

test('default_chain: credentials from the mock IMDSv2 (token PUT, role, creds) fetched directly, not via a proxy', async () => {
  const admin2 = await ensureAdmin2(api);
  await mock.clearRequests();
  const t = await testConnection(admin2, { auth_mode: 'default_chain', region: 'us-east-1', model_id: MODEL, endpoint_url: MOCK_ENDPOINT, proxy_mode: 'env' });
  expect(t.body.ok, JSON.stringify(t.body.steps)).toBe(true);
  const ops = (await mock.requests()).map((r) => r.op);
  expect(ops).toEqual(expect.arrayContaining(['imds_token', 'imds_creds', 'converse_stream']));
  const tok = (await mock.requests('op=imds_token'))[0];
  expect(Number(tok.headers['x-aws-ec2-metadata-token-ttl-seconds'])).toBe(21600);
  expect(tok.proxied, 'IMDS is always direct').toBeFalsy();
  const creds = (await mock.requests('op=imds_creds'))[0];
  expect((await mock.requests('op=converse_stream'))[0].sig.access_key_id).toBe(creds.issued_access_key_id);
});

test('Test is rate limited: 10 per admin per 10 min -> 429 rate_limited', async () => {
  test.setTimeout(180_000);
  const admin2 = await ensureAdmin2(api);
  const codes: number[] = [];
  for (let i = 0; i < 12 && !codes.includes(429); i += 1) codes.push((await testConnection(admin2, akSettings())).status);
  expect(codes, codes.join(',')).toContain(429);
  expect(codes.filter((c) => c === 200).length, 'at most 10 tests per admin in 10 min').toBeLessThanOrEqual(10);
  expect(codes.filter((c) => c === 200).length).toBeGreaterThanOrEqual(9);
});

test('viewer: no entry point, every /api/assistant/* and AI endpoint 403; API tokens 403 (also admin tokens)', async ({ page }) => {
  await ensureViewer(api);
  const viewer = await apiAs(VIEWER);
  const token = await tokenApi(api, 'admin');
  const endpoints: [string, string, unknown?][] = [
    ['GET', '/api/assistant/settings'], ['PUT', '/api/assistant/settings', { redact_internal_ips: false }],
    ['POST', '/api/assistant/test', {}], ['GET', '/api/assistant/status'], ['GET', '/api/assistant/usage'],
    ['POST', '/api/assistant/ask', { conversation_id: null, question: 'hi' }], ['GET', '/api/assistant/conversations'],
    ['POST', '/api/assistant/explain', { sensor_id: 1, event_id: null, refresh: false }], ['GET', '/api/incidents'],
    ['POST', '/api/assistant/rule-drafts', { kind: 'alert_rule', description: 'alert me when x is down' }],
    ['GET', '/api/assistant/digests'], ['GET', '/api/assistant/knowledge'], ['POST', '/api/assistant/models', {}],
    ['POST', '/api/device-templates/user', { yaml: 'x', overwrite: false }],
  ];
  for (const [m, p, b] of endpoints) {
    const v = await raw(viewer, m, p, b);
    expect(v.status, `viewer ${m} ${p}`).toBe(403);
    expect(v.body.error.code).toBe('forbidden');
  }
  for (const [m, p, b] of endpoints.filter(([, p]) => p.startsWith('/api/assistant') || p === '/api/device-templates/user')) {
    const t = await raw(token, m, p, b);
    expect(t.status, `admin API token ${m} ${p}`).toBe(403);
  }
  await page.request.post('/api/auth/login', { data: VIEWER });
  await page.goto('/');
  await expect(page.getByRole('navigation').first()).toBeVisible();
  await expect(page.locator('a[href="/assistant"]')).toHaveCount(0);
  await page.goto('/assistant');
  await expect(page.getByTestId('question')).toHaveCount(0);
  await page.goto('/settings?tab=assistant');
  await expect(page.getByTestId('assistant-connection')).toHaveCount(0);
});
