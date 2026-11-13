// [4] PR 4-0a: shared AWS connections and the assistant's AWS access (docs/architecture.md § 15.1, § 15.1.11 spec 90;
// api.md "[4] Shared AWS connections"). Against the Bedrock mock with its Roles Anywhere and STS mocks
// (tests/sim/aws/mock_sts.py): the owner's two layouts —
//   (a) Roles Anywhere in the Bedrock account, the assistant uses it directly (the E2E connection of the 3b specs is
//       an access key in the Bedrock account: same "direct" shape);
//   (b) Roles Anywhere in a network account (444455556666) + sts:AssumeRole into the Bedrock account.
// Create a connection in the UI, upload its certificate (PKCS#7 from the CSR flow), Test it (direct, then with the hop),
// move the assistant onto the hop and back, a broken trust policy fails at `hop` with the set_source_identity hint,
// settings search in English and Hebrew, RTL layout at 390 px. Leaves the assistant ON on the E2E connection, direct.
import { expect, test, type Page } from '@playwright/test';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loginViaApi, type Api, apiAs } from '../lib/helpers';
import { SIM_DIR, raw } from '../lib/lab';
import { py } from '../lib/lab3';
import {
  BEDROCK_PKI, BEDROCK_URL, CONN_NAME, MOCK_ENDPOINT, PHASE3B, askOk, assistantSettings, connections, ensureAssistant,
  mock, pkiFile, shot3b,
} from '../lib/lab3b';

test.describe.configure({ mode: 'serial' });
test.skip(!PHASE3B, 'phase-3b Bedrock mock (with the 4-0a STS / Roles Anywhere mocks) not running');

const NET = '444455556666';
const BEDROCK_ACCT = '111122223333';
const NET_TA = `arn:aws:rolesanywhere:us-east-1:${NET}:trust-anchor/9a1b2c3d-0000-4000-8000-000000000001`;
const NET_PROFILE = `arn:aws:rolesanywhere:us-east-1:${NET}:profile/9a1b2c3d-0000-4000-8000-000000000002`;
const NET_BASE_ROLE = `arn:aws:iam::${NET}:role/netmon-base`;
const BEDROCK_ROLE = `arn:aws:iam::${BEDROCK_ACCT}:role/netmon-bedrock`;
const EXT_ID = 'Qx7Rm2Lp9Vn4Tc8Wb1Zd6Hk3Js5Fg0Ay';
const LEAF_CN = 'netmonitor.example.net';
const NET_NAME = 'Network account';
const SHOTS = process.env.P40A_SHOT_DIR ?? '';

let api: Api;

async function stsTrust(over: { trust_allows?: string[]; external_id?: string | null } = {}): Promise<void> {
  const r = await fetch(`${BEDROCK_URL}/_sts/roles`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      reset: true,
      roles: [{
        arn: BEDROCK_ROLE, trusted_principals: [NET_BASE_ROLE], external_id: over.external_id === undefined ? EXT_ID : over.external_id,
        trust_allows: over.trust_allows ?? ['sts:AssumeRole', 'sts:SetSourceIdentity'], require_source_identity: `CN=${LEAF_CN}`,
      }],
      callers: [{ arn: NET_BASE_ROLE, allows: ['sts:AssumeRole', 'sts:SetSourceIdentity'], resources: [BEDROCK_ROLE] }],
    }),
  });
  expect(r.status, await r.text()).toBe(200);
}

async function netConn(): Promise<any> {
  return (await connections(api)).find((c: any) => c.name === NET_NAME);
}

/** Optional extra screenshots (P40A_SHOT_DIR): 1440x900 @2x is set by the caller's context. */
async function shot(page: Page, name: string): Promise<void> {
  await shot3b(page, `90-${name}`);
  if (SHOTS) await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: true });
}

test.beforeAll(async () => {
  api = await apiAs();
  await ensureAssistant(api);
  await stsTrust();
  // a rerun starts from a clean "Network account"
  const old = await netConn();
  if (old) {
    const s = await assistantSettings(api);
    if (s.connection_id === old.id) await back(api);
    await raw(api, 'DELETE', `/api/aws/connections/${old.id}`);
  }
});

/** The assistant back on the E2E connection, direct (tested + saved through the API). */
async function back(a: Api): Promise<void> {
  const e2e = (await connections(a)).find((c: any) => c.name === CONN_NAME);
  const settings = { connection_id: e2e.id, access: { mode: 'direct', role_arn: null, external_id: null } };
  const t = await raw(a, 'POST', '/api/assistant/test', { settings });
  expect(t.body.ok, JSON.stringify(t.body.steps)).toBe(true);
  const r = await raw(a, 'PUT', '/api/assistant/settings', { ...settings, test_id: t.body.test_id });
  expect(r.status, r.text).toBe(200);
}

test('Settings → AWS lists the connection the assistant uses (default, used by, last test)', async ({ page }) => {
  await loginViaApi(page, undefined, '/settings?tab=aws');
  const row = page.locator(`tr[data-connection="${CONN_NAME}"]`);
  await expect(row).toBeVisible({ timeout: 20_000 });
  await expect(row).toContainText('Assistant');
  // its Delete is disabled with the reason while the assistant uses it
  await expect(row.getByTestId('aws-delete')).toBeDisabled();
  await expect(row.getByTestId('delete-reason')).toContainText('Assistant');
  await shot(page, 'aws-list');
});

test('create "Network account" in the UI; CSR → PKCS#7 upload of the issued certificate; Test direct', async ({ page }) => {
  test.setTimeout(120_000);
  await loginViaApi(page, undefined, '/settings?tab=aws');
  await page.getByTestId('aws-add').click();
  const form = page.getByTestId('aws-connection-form');
  await form.getByTestId('aws-name').fill(NET_NAME);
  await form.getByTestId('aws-auth-roles_anywhere').check();
  // the doubled trust anchor ARN is refused before anything is sent
  await form.getByTestId('aws-ra_trust_anchor_arn').fill(`arn:aws:rolesanywhere:us-east-1:${NET}:trust-anchor/${NET_TA}`);
  await expect(form.getByText('The ARN appears twice — paste it once.')).toBeVisible();
  await expect(form.getByTestId('aws-save')).toBeDisabled();
  await form.getByTestId('aws-ra_trust_anchor_arn').fill(NET_TA);
  await form.getByTestId('aws-ra_profile_arn').fill(NET_PROFILE);
  await form.getByTestId('aws-ra_role_arn').fill(NET_BASE_ROLE);
  await form.locator('select:has(option[value="custom"])').selectOption('direct');
  for (const svc of ['sts', 'rolesanywhere']) {
    await form.getByTestId('add-override').click();
    const row = form.locator('[data-override]').last();
    await row.locator('select').selectOption(svc);
    await row.locator('input').nth(0).fill('us-east-1');
    await row.locator('input').nth(1).fill(MOCK_ENDPOINT);
  }
  await expect(form.getByTestId('save-first')).toBeVisible();
  await form.getByTestId('aws-save').click();
  // after the first save the certificate card appears
  const card = page.getByTestId('cert-card');
  await expect(card).toBeVisible({ timeout: 15_000 });
  // Generate a CSR (a CN with a space is warned about), then the pending request offers one file field
  await card.getByLabel('Generate a CSR').check();
  await card.getByTestId('csr-cn').fill('NetMonitor Bedrock');
  await expect(card.getByTestId('cn-unsafe')).toBeVisible();
  await card.getByTestId('csr-cn').fill(LEAF_CN);
  await card.getByTestId('csr-generate').click();
  await expect(card.getByTestId('csr-pending')).toBeVisible();
  await expect(card.getByTestId('cert-key')).toHaveCount(0);
  await card.getByTestId('template-name').fill('NetMon Client Auth');
  await expect(card.getByTestId('template-spaces')).toBeVisible();
  await card.getByTestId('template-name').fill('NetMonClientAuth');
  await expect(card.getByTestId('certreq-command')).toHaveText(
    'certreq -submit -attrib "CertificateTemplate:NetMonClientAuth" netmonitor.csr netmonitor.cer',
  );
  // a private key is refused in CSR mode
  await card.getByTestId('cert-file').setInputFiles(pkiFile('leaf-rsa.key'));
  await expect(card.getByTestId('not-a-key')).toBeVisible();
  await expect(card.getByTestId('cert-upload')).toBeDisabled();
  // sign the CSR with the test PKI and upload the result as a PKCS#7 bundle (what certreq / AD CS give)
  const c = await netConn();
  const dl = await api.ctx.fetch(`/api/aws/connections/${c.id}/cert/csr`);
  expect(dl.status()).toBe(200);
  const dir = mkdtempSync(join(tmpdir(), 'qa4-csr-'));
  writeFileSync(join(dir, 'req.csr'), await dl.body());
  const signed = py([join(SIM_DIR, 'bedrock', 'pki.py'), 'sign-csr', BEDROCK_PKI, join(dir, 'req.csr'), join(dir, 'issued.pem')]);
  expect(signed.status, signed.stderr).toBe(0);
  const p7 = py(['-c', [
    'import sys',
    'from cryptography import x509',
    'from cryptography.hazmat.primitives.serialization import Encoding, pkcs7',
    'leaf = x509.load_pem_x509_certificate(open(sys.argv[1], "rb").read())',
    'ca = x509.load_pem_x509_certificate(open(sys.argv[2], "rb").read())',
    'open(sys.argv[3], "wb").write(pkcs7.serialize_certificates([leaf, ca], Encoding.DER))',
  ].join('\n'), join(dir, 'issued.pem'), pkiFile('issuing.pem'), join(dir, 'netmonitor.p7b')]);
  expect(p7.status, p7.stderr).toBe(0);
  await card.getByTestId('cert-file').setInputFiles(join(dir, 'netmonitor.p7b'));
  await expect(card.getByTestId('cert-upload')).toBeEnabled();
  await card.getByTestId('cert-upload').click();
  await expect(card.getByTestId('cert-cn')).toHaveText(LEAF_CN, { timeout: 15_000 });
  await expect(card.getByTestId('cert-source-identity')).toHaveText(`CN=${LEAF_CN}`);
  await expect(card.getByTestId('csr-pending')).toHaveCount(0);
  // Test (direct): config, credentials and identity of the network account
  await form.getByTestId('aws-test').click();
  const res = page.getByTestId('aws-test-result');
  await expect(res).toHaveAttribute('data-ok', 'true', { timeout: 60_000 });
  for (const st of ['config', 'credentials', 'identity'])
    await expect(res.locator(`[data-step="${st}"]`)).toHaveAttribute('data-ok', 'true');
  await expect(res.locator('[data-step="identity"]')).toContainText(`account ${NET}`);
  await expect(res.locator('[data-step="hop"]')).toHaveCount(0);
  const saved = await netConn();
  expect(saved.last_test).toMatchObject({ ok: true, account: NET });
  expect(saved.certificate).toMatchObject({ subject_cn: LEAF_CN, source_identity: `CN=${LEAF_CN}` });
});

test('Test with the assume-role hop (layout b); Expected IAM policies render the trust anchor once, CN, ExternalId', async ({ page }) => {
  test.setTimeout(90_000);
  await stsTrust();
  await loginViaApi(page, undefined, '/settings?tab=aws');
  await page.locator(`tr[data-connection="${NET_NAME}"]`).getByTestId('aws-edit').click();
  const form = page.getByTestId('aws-connection-form');
  await form.getByTestId('hop-assume_role').check();
  await form.getByTestId('hop-role-arn').fill(BEDROCK_ROLE);
  await form.getByTestId('external-id').fill(EXT_ID);
  await form.getByTestId('aws-test').click();
  const res = form.getByTestId('aws-test-result');
  await expect(res).toHaveAttribute('data-ok', 'true', { timeout: 60_000 });
  await expect(res.locator('[data-step="hop"]')).toHaveAttribute('data-hop', '1');
  await expect(res.locator('[data-step="hop"]')).toContainText(BEDROCK_ROLE);
  await expect(res.locator('[data-step="hop_identity"]')).toContainText(`account ${BEDROCK_ACCT}`);
  const panel = form.getByTestId('aws-policies');
  await panel.locator('summary').click();
  const base = panel.locator('[data-policy="base_trust_policy"] pre');
  await expect(base).toContainText(NET_TA);
  expect(((await base.textContent()) ?? '').split('arn:aws:rolesanywhere').length - 1, 'trust anchor ARN once').toBe(1);
  await expect(base).toContainText(`"aws:PrincipalTag/x509Subject/CN": "${LEAF_CN}"`);
  const hop = panel.locator('[data-policy="hop_trust_policy"] pre');
  await expect(hop).toContainText(NET_BASE_ROLE);
  await expect(hop).toContainText(EXT_ID);
  await expect(hop).toContainText(`"aws:SourceIdentity": "CN=${LEAF_CN}"`);
  await shot(page, 'aws-editor-roles-anywhere');
});

test('the assistant moves onto the hop (Test shows the hop steps; answers through it) and back', async ({ page }) => {
  test.setTimeout(180_000);
  await stsTrust();
  await loginViaApi(page, undefined, '/settings?tab=assistant');
  const access = page.getByTestId('assistant-aws-access');
  await expect(access).toBeVisible({ timeout: 20_000 });
  const net = await netConn();
  await access.getByTestId('access-connection').selectOption(String(net.id));
  await access.getByTestId('access-assume_role').check();
  await access.getByTestId('access-role-arn').fill(BEDROCK_ROLE);
  await access.getByTestId('external-id').fill(EXT_ID);
  await page.getByTestId('test-connection').click();
  const res = page.getByTestId('test-result');
  await expect(res).toHaveAttribute('data-ok', 'true', { timeout: 90_000 });
  for (const st of ['config', 'credentials', 'identity', 'hop', 'hop_identity', 'endpoint', 'converse'])
    await expect(res.locator(`[data-step="${st}"]`), st).toHaveAttribute('data-ok', 'true');
  await shot(page, 'assistant-picker');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect.poll(async () => (await assistantSettings(api)).access.mode, { timeout: 15_000 }).toBe('assume_role');
  const s = await assistantSettings(api);
  expect(s).toMatchObject({ connection_id: net.id, access: { role_arn: BEDROCK_ROLE, external_id: EXT_ID }, connection: { name: NET_NAME } });
  // a question goes through the chain: Bedrock is called with the credentials AssumeRole issued
  await mock.clearRequests();
  await askOk(api, 'Which devices are down right now?');
  const ar = (await mock.requests('op=sts_assume_role')).pop();
  expect(ar, 'AssumeRole reached the STS mock').toBeTruthy();
  expect(ar.external_id_sent).toBe(EXT_ID);
  const calls = await mock.requests('op=converse,converse_stream');
  expect(calls.length).toBeGreaterThan(0);
  expect(calls.every((c: any) => c.sig.access_key_id === ar.issued_access_key_id)).toBe(true);
  const status = await api.get('/api/assistant/status');
  expect(status.aws).toMatchObject({ connection_id: net.id, access_mode: 'assume_role' });
  expect(status.aws.hops.map((h: any) => h.hop)).toEqual([0, 1]);
  expect(status.aws.hops[1]).toMatchObject({ kind: 'assume_role', account: BEDROCK_ACCT });
  // the network account connection now lists the assistant (assumes a role) and cannot be deleted
  expect((await netConn()).used_by).toEqual([expect.objectContaining({ kind: 'assistant', access_mode: 'assume_role' })]);
  // and back: the E2E connection, direct (rollback at steps 4-6 of § 15.1.9)
  await back(api);
  expect((await assistantSettings(api)).access.mode).toBe('direct');
  await askOk(api, 'And now?');
});

test('a broken trust policy (sts:SetSourceIdentity removed) fails at `hop` with the set_source_identity hint', async ({ page }) => {
  test.setTimeout(90_000);
  await stsTrust({ trust_allows: ['sts:AssumeRole'] });
  try {
    await loginViaApi(page, undefined, '/settings?tab=aws');
    await page.locator(`tr[data-connection="${NET_NAME}"]`).getByTestId('aws-edit').click();
    const form = page.getByTestId('aws-connection-form');
    await form.getByTestId('hop-assume_role').check();
    await form.getByTestId('hop-role-arn').fill(BEDROCK_ROLE);
    await form.getByTestId('external-id').fill(EXT_ID);
    await form.getByTestId('aws-test').click();
    const res = form.getByTestId('aws-test-result');
    await expect(res).toHaveAttribute('data-ok', 'false', { timeout: 60_000 });
    await expect(res.locator('[data-step="identity"]')).toHaveAttribute('data-ok', 'true');
    const hop = res.locator('[data-step="hop"]');
    await expect(hop).toHaveAttribute('data-ok', 'false');
    const hint = hop.locator('[data-hint="set_source_identity"]');
    await expect(hint).toContainText('sts:SetSourceIdentity in two places');
    await expect(hint.locator('[data-value="source_identity"]')).toHaveText(`CN=${LEAF_CN}`);
    await expect(hint.locator('[data-value="role_arn"]')).toHaveText(BEDROCK_ROLE);
    await shot(page, 'test-hop-failing-hint');
    // a wrong ExternalId names the trust policy / ExternalId (API)
    await stsTrust();
    const net = await netConn();
    const wrong = await raw(api, 'POST', '/api/aws/connections/test', {
      connection_id: net.id, connection: null, access: { mode: 'assume_role', role_arn: BEDROCK_ROLE, external_id: 'wrong-external-id' },
    });
    const failed = wrong.body.steps.find((s: any) => !s.ok);
    expect(failed).toMatchObject({ step: 'hop', hop: 1, error_code: 'aws_assume_role_denied' });
    expect(failed.hints.map((h: any) => h.code)).toEqual(expect.arrayContaining(['caller_policy', 'trust_policy', 'external_id']));
  } finally {
    await stsTrust();
  }
});

test('settings search finds ExternalId and Trust anchor in English and Hebrew', async ({ page }) => {
  await loginViaApi(page, undefined, '/settings');
  const search = page.getByTestId('settings-search');
  await search.fill('ExternalId');
  await expect(page.getByTestId('settings-search-result').first()).toBeVisible();
  await page.getByTestId('settings-search-result').first().click();
  await expect(page).toHaveURL(/tab=assistant/);
  await search.fill('trust anchor');
  await page.getByTestId('settings-search-result').first().click();
  await expect(page).toHaveURL(/tab=aws/);
  // Hebrew UI: Hebrew and English technical words
  await page.request.patch('/api/auth/me', { data: { language: 'he' } });
  try {
    await page.evaluate(() => localStorage.setItem('netmon.lang', 'he'));
    await page.goto('/settings');
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
    await page.getByTestId('settings-search').fill('עוגן האמון');
    await expect(page.getByTestId('settings-search-result').first()).toContainText('עוגן האמון');
    await page.getByTestId('settings-search-result').first().click();
    await expect(page).toHaveURL(/tab=aws/);
    await page.getByTestId('settings-search').fill('ExternalId');
    await page.getByTestId('settings-search-result').first().click();
    await expect(page).toHaveURL(/tab=assistant/);
  } finally {
    await page.request.patch('/api/auth/me', { data: { language: 'en' } });
    await page.evaluate(() => localStorage.removeItem('netmon.lang'));
  }
});

test('RTL at 390 px: the AWS tab and the editor without horizontal page overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await loginViaApi(page, undefined, '/');
  await page.request.patch('/api/auth/me', { data: { language: 'he' } });
  try {
    await page.evaluate(() => localStorage.setItem('netmon.lang', 'he'));
    await page.goto('/settings?tab=aws');
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
    await expect(page.locator(`tr[data-connection="${NET_NAME}"]`)).toBeVisible({ timeout: 20_000 });
    await page.locator(`tr[data-connection="${NET_NAME}"]`).getByTestId('aws-edit').click();
    await expect(page.getByTestId('aws-connection-form')).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, 'horizontal page overflow (px)').toBeLessThanOrEqual(1);
    // ARNs stay left-to-right inside the Hebrew page
    expect(await page.getByTestId('aws-ra_trust_anchor_arn').getAttribute('dir')).toBe('ltr');
    await shot(page, 'aws-rtl-390');
  } finally {
    await page.request.patch('/api/auth/me', { data: { language: 'en' } });
    await page.evaluate(() => localStorage.removeItem('netmon.lang'));
  }
});
