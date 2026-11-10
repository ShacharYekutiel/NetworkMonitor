// [3] Windows integrated login (Kerberos / SPNEGO) against an MIT KDC (docs/architecture.md §13.5.1, §13.11 spec
// 56; api.md "[3] Single sign-on"). Linux only: skipped when tests/e2e/run.sh could not start tests/sim/kdc.
// Roles come from the 2b-3 fake AD (jdoe = viewer, asmith = admin via nested IT-Team, nogroup = no group).
// Browser: a separate Chromium with --auth-server-allowlist and KRB5CCNAME pointing at a kinit cache.
import { chromium, expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { BASE_URL, apiAs, type Api } from '../lib/helpers';
import { SIM_DIR, raw } from '../lib/lab';
import { KDC_DIR, PHASE3, configureFakeAd, py, setFeatures, shot3, tx } from '../lib/lab3';

test.describe.configure({ mode: 'serial' });
test.skip(!PHASE3 || !KDC_DIR, 'no KDC (krb5kdc missing or tests/sim/kdc failed to start)');

const CHROMIUM = process.env.PW_CHROMIUM ?? '/opt/pw-browsers/chromium';
let api: Api;

function token(user: string, service = 'HTTP@127.0.0.1'): string {
  const r = py([join(SIM_DIR, 'kdc', 'client_token.py'), 'spnego', '--user', user, '--kdc-dir', KDC_DIR, '--service', service]);
  if (r.status !== 0) throw new Error(r.stderr);
  return r.stdout.trim();
}
function fixture(kind: string[]): string {
  return py([join(SIM_DIR, 'kdc', 'client_token.py'), ...kind]).stdout.trim();
}
async function negotiate(value: string | null) {
  const ctx = (await apiAs()).ctx; // own cookie jar
  const r = await ctx.fetch('/api/auth/sso/kerberos', { headers: value ? { Authorization: value } : {} });
  return { status: r.status(), headers: r.headers(), body: await r.json().catch(() => null) };
}
function kinit(user: string): string {
  const cc = join(KDC_DIR, `ccache_browser_${user}`);
  const pw = Object.fromEntries(readFileSync(join(KDC_DIR, 'passwords'), 'utf8').trim().split('\n').map((l) => l.split(' ')));
  execFileSync('kinit', ['-c', `FILE:${cc}`, user], { input: `${pw[user]}\n`, env: { ...process.env, KRB5_CONFIG: join(KDC_DIR, 'krb5.conf') } });
  return cc;
}
// Chrome builds the SPN from the host name; use localhost (keytab has HTTP/localhost) rather than an IP literal
const SSO_BASE = BASE_URL.replace('127.0.0.1', 'localhost');
async function browserFor(ccache: string) {
  const host = new URL(SSO_BASE).hostname;
  return chromium.launch({
    executablePath: existsSync(CHROMIUM) ? CHROMIUM : undefined,
    args: [`--auth-server-allowlist=${host},localhost,127.0.0.1`, '--auth-negotiate-delegate-allowlist=', '--disable-auth-negotiate-cname-lookup'],
    env: { ...process.env, KRB5CCNAME: `FILE:${ccache}`, KRB5_CONFIG: join(KDC_DIR, 'krb5.conf') },
  });
}

test.beforeAll(async () => {
  api = await apiAs();
  await configureFakeAd(api);
  await setFeatures(api, { ldap: true });
});

test('enabling needs a keytab; upload shows principals/etypes (no key material); RC4-only warning', async () => {
  const pre = await raw(api, 'PUT', '/api/settings', { feature_sso_kerberos_enabled: true });
  expect(pre.status, 'ON without keytab').toBe(422);
  const put = await api.put('/api/sso/kerberos', { realms: ['MNIDOM.TEST'], auto_login: true });
  expect(put.realms).toEqual(['MNIDOM.TEST']);
  const rc4 = await api.ctx.fetch('/api/sso/kerberos/keytab', { method: 'POST', multipart: { file: { name: 'rc4.keytab', mimeType: 'application/octet-stream', buffer: readFileSync(join(KDC_DIR, 'rc4.keytab')) } } });
  expect(rc4.status(), await rc4.text()).toBe(200);
  const w = await rc4.json();
  expect(w.warnings).toEqual(expect.arrayContaining(['rc4_only']));
  const bad = await api.ctx.fetch('/api/sso/kerberos/keytab', { method: 'POST', multipart: { file: { name: 'x.keytab', mimeType: 'application/octet-stream', buffer: Buffer.from([5, 1, 0, 0, 0, 0]) } } });
  expect(bad.status()).toBe(422);
  expect((await bad.json()).error.code).toBe('invalid_keytab');
  const up = await api.ctx.fetch('/api/sso/kerberos/keytab', { method: 'POST', multipart: { file: { name: 'http.keytab', mimeType: 'application/octet-stream', buffer: readFileSync(join(KDC_DIR, 'http.keytab')) } } });
  expect(up.status(), await up.text()).toBe(200);
  const k = await up.json();
  expect(k.keytab_set).toBe(true);
  expect(k.keytab_entries).toEqual(expect.arrayContaining([
    expect.objectContaining({ principal: 'HTTP/localhost@MNIDOM.TEST', enctype: 18, enctype_name: 'aes256-cts-hmac-sha1-96' }),
    expect.objectContaining({ principal: 'HTTP/127.0.0.1@MNIDOM.TEST', enctype: 17 }),
  ]));
  expect(k.warnings).not.toContain('rc4_only');
  expect(JSON.stringify(k)).not.toMatch(/"key"|keytab_enc/);
  await setFeatures(api, { sso_kerberos: true });
  expect((await api.get('/api/settings/public')).sso.kerberos).toEqual({ auto_login: true });
  await expect.poll(async () => (await api.get('/api/sso/kerberos')).state, { timeout: 10_000 }).toBe('ready');
  expect((await api.get('/api/health')).kerberos).toBe('ready');
});

test('API: challenge, valid ticket -> viewer jdoe / admin asmith, NTLM refused, wrong realm, 48 KB header', async () => {
  const ch = await negotiate(null);
  expect(ch.status).toBe(401);
  expect(ch.headers['www-authenticate']).toBe('Negotiate');
  expect(ch.body.error.code).toBe('sso_challenge');
  const j = await negotiate(`Negotiate ${token('jdoe')}`);
  expect(j.status, JSON.stringify(j.body)).toBe(200);
  expect(j.body.user).toMatchObject({ username: 'jdoe', role: 'viewer', source: 'ldap' });
  expect(j.headers['set-cookie'] ?? '').toMatch(/netmon/i);
  const a = await negotiate(`Negotiate ${token('asmith')}`);
  expect(a.status).toBe(200);
  expect(a.body.user).toMatchObject({ username: 'asmith', role: 'admin' });
  const ng = await negotiate(`Negotiate ${token('nogroup')}`);
  expect(ng.status).toBe(401);
  for (const k of [['ntlm', 'raw'], ['ntlm', 'spnego'], ['ntlm', 'spnego-mechlist-krb5-ntlm-token']]) {
    const n = await negotiate(`Negotiate ${fixture(k)}`);
    expect(n.status, `NTLM ${k[1]}`).toBe(401);
    expect(n.body.error.code).toBe('sso_ntlm');
    expect(n.headers['www-authenticate'], 'no new challenge after NTLM').toBeUndefined();
  }
  const g = await negotiate(`Negotiate ${fixture(['garbage'])}`);
  expect(g.status).toBe(401);
  expect(g.body.error.code).toBe('sso_failed');
  expect(g.headers['www-authenticate']).toBeUndefined();
  // a ~48 KB Negotiate header (big PAC) reaches the app: an SSO answer, not a 400/431 from the HTTP layer
  const big = await negotiate(`Negotiate ${fixture(['big', '--size', '49152'])}`);
  expect(big.status).toBe(401);
  expect(big.body?.error?.code).toBe('sso_failed');
  // wrong realm: MNIDOM.TEST no longer accepted
  await api.put('/api/sso/kerberos', { realms: ['MNIDOM.NET'], auto_login: true });
  const wr = await negotiate(`Negotiate ${token('jdoe')}`);
  expect(wr.status).toBe(401);
  expect(wr.body.error.code).toBe('sso_failed');
  await api.put('/api/sso/kerberos', { realms: ['MNIDOM.TEST'], auto_login: true });
  // replay of an already accepted token -> refused by the MIT replay cache (KRB5RCACHEDIR on the data volume)
  const t = token('jdoe');
  expect((await negotiate(`Negotiate ${t}`)).status).toBe(200);
  const rep = await negotiate(`Negotiate ${t}`);
  expect(rep.status, 'replayed SPNEGO token').toBe(401);
});

// Playwright's Chromium (chromium-1243 headless) never answers the Negotiate challenge in this container even with
// --auth-server-allowlist + KRB5CCNAME (verified with a bare 401-Negotiate server): opt in with E2E_KRB_BROWSER=1
// on a desktop Chrome with GSSAPI.
test('browser: auto sign-in as jdoe (viewer); logout does not re-login; ?sso=0 shows the form; no ticket -> form', async () => {
  test.skip(process.env.E2E_KRB_BROWSER !== '1', 'Chromium GSSAPI not available in this environment (set E2E_KRB_BROWSER=1)');
  test.setTimeout(120_000);
  const b = await browserFor(kinit('jdoe'));
  try {
    const page = await (await b.newContext({ baseURL: SSO_BASE })).newPage();
    await page.goto('/login');
    await expect(page.getByRole('navigation').first()).toBeVisible({ timeout: 20_000 });
    const me = await page.request.get('/api/auth/me');
    expect((await me.json()).user).toMatchObject({ username: 'jdoe', role: 'viewer' });
    await shot3(page, '56-kerberos-signed-in');
    // logout -> the login form, no automatic sign-in (sessionStorage netmon.sso.skip)
    await page.request.post('/api/auth/logout');
    await page.evaluate(() => sessionStorage.setItem('netmon.sso.skip', '1'));
    await page.goto('/login');
    await expect(page.getByLabel('Username')).toBeVisible({ timeout: 15_000 });
    await page.waitForTimeout(2000);
    expect((await page.request.get('/api/auth/me')).status()).toBe(401);
    await expect(page.getByRole('button', { name: tx('sso.kerberosButton', /sign in with windows/i) })).toBeVisible();
    // the button signs in again
    await page.getByRole('button', { name: tx('sso.kerberosButton', /sign in with windows/i) }).click();
    await expect(page.getByRole('navigation').first()).toBeVisible({ timeout: 20_000 });
    await page.request.post('/api/auth/logout');
    // ?sso=0 in a fresh tab
    const p2 = await (await b.newContext({ baseURL: SSO_BASE })).newPage();
    await p2.goto('/login?sso=0');
    await expect(p2.getByLabel('Username')).toBeVisible({ timeout: 15_000 });
    await p2.waitForTimeout(2000);
    expect((await p2.request.get('/api/auth/me')).status()).toBe(401);
  } finally {
    await b.close();
  }
  // no ticket at all -> the 401 reaches the SPA, the form is shown
  const empty = join(KDC_DIR, 'ccache_empty');
  writeFileSync(empty, '');
  const b2 = await browserFor(empty);
  try {
    const page = await (await b2.newContext({ baseURL: SSO_BASE })).newPage();
    await page.goto('/login');
    await expect(page.getByLabel('Username')).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText(tx('sso.kerberosFailed', /windows|kerberos/i)).first()).toBeVisible();
    await shot3(page, '56-kerberos-no-ticket');
  } finally {
    await b2.close();
  }
});

test.afterAll(async () => {
  await setFeatures(api, { sso_kerberos: false }).catch(() => undefined);
});
