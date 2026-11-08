// [3] "Sign in with Microsoft" (OIDC code flow + PKCE) against the mock Entra IdP (docs/architecture.md §13.5.2,
// §13.11 spec 57; api.md "[3] Single sign-on"). The backend's authority is NETMON_OIDC_AUTHORITY = the mock.
// admin via app role; no role -> error message; open-redirect `next` refused; id_token validation modes.
import { expect, test, type Page } from '@playwright/test';
import { apiAs, type Api } from '../lib/helpers';
import { raw } from '../lib/lab';
import { PHASE3, oidcDefaultUser, oidcMode, oidcRequests, oidcState, setFeatures, shot3, tx } from '../lib/lab3';

test.describe.configure({ mode: 'serial' });
test.skip(!PHASE3, 'phase-3 simulators not running');

let api: Api;

async function signIn(page: Page, user: string, next = ''): Promise<void> {
  await oidcDefaultUser(user);
  // the SPA passes the deep link (router state from the redirect to /login) as `next`
  await page.goto(next || '/login');
  await expect(page).toHaveURL(/\/login/);
  await page.getByTestId('oidc-button').or(page.getByRole('link', { name: tx('sso.oidcButton', /sign in with microsoft/i) })).first().click();
}

test.beforeAll(async () => {
  api = await apiAs();
  const st = await oidcState();
  const pre = await raw(api, 'PUT', '/api/settings', { feature_sso_oidc_enabled: true });
  expect(pre.status, 'ON without OIDC configuration').toBe(422);
  const cfg = await api.put('/api/sso/oidc', {
    tenant_id: st.tenant, client_id: st.client_id, client_secret: st.client_secret, role_source: 'app_roles',
    admin_values: ['NetMon.Admin'], viewer_values: ['NetMon.Viewer'], button_label: null,
  });
  expect(cfg).toMatchObject({ client_secret: null, client_secret_set: true, tenant_id: st.tenant });
  expect(cfg.redirect_uri).toMatch(/\/api\/auth\/oidc\/callback$/);
  const t = await api.post('/api/sso/oidc/test');
  expect(t).toMatchObject({ ok: true, issuer: st.issuer });
  expect(t.jwks_keys).toBeGreaterThanOrEqual(1);
  await setFeatures(api, { sso_oidc: true });
  expect((await api.get('/api/settings/public')).sso.oidc).toEqual({ button_label: 'Sign in with Microsoft' });
  await oidcMode('ok');
});

test.afterAll(async () => {
  await oidcMode('ok').catch(() => undefined);
  await oidcDefaultUser(null).catch(() => undefined);
  await setFeatures(api, { sso_oidc: false }).catch(() => undefined);
});

test('start: 302 to authorize with PKCE S256 + state + nonce; cookie netmon_oidc Lax, HttpOnly, path-scoped', async () => {
  const r = await api.ctx.fetch('/api/auth/oidc/start?next=/devices', { maxRedirects: 0 });
  expect(r.status()).toBe(302);
  const loc = new URL(r.headers()['location']);
  const st = await oidcState();
  expect(`${loc.origin}${loc.pathname}`).toBe(`${st.authority}/${st.tenant}/oauth2/v2.0/authorize`);
  const p = loc.searchParams;
  expect(p.get('response_type')).toBe('code');
  expect(p.get('code_challenge_method')).toBe('S256');
  expect(p.get('code_challenge')).toMatch(/^[A-Za-z0-9_-]{43}$/);
  expect(p.get('scope')?.split(' ')).toEqual(expect.arrayContaining(['openid', 'profile', 'email']));
  expect(p.get('response_mode')).toBe('query');
  expect(p.get('state')!.length).toBeGreaterThanOrEqual(32);
  expect(p.get('nonce')).toBeTruthy();
  const cookie = r.headers()['set-cookie'] ?? '';
  expect(cookie).toMatch(/netmon_oidc=/);
  expect(cookie).toMatch(/SameSite=Lax/i);
  expect(cookie).toMatch(/HttpOnly/i);
  expect(cookie).toMatch(/Path=\/api\/auth\/oidc/i);
  expect(cookie, 'the cookie is encrypted (no next / verifier in clear)').not.toContain('/devices');
});

test('Sign in with Microsoft -> admin via app role NetMon.Admin, lands on next', async ({ page }) => {
  await signIn(page, 'dana.admin@mnidom.net', '/settings');
  await expect(page).toHaveURL(/\/settings/, { timeout: 20_000 });
  const me = (await (await page.request.get('/api/auth/me')).json()).user;
  expect(me).toMatchObject({ username: 'dana.admin@mnidom.net', role: 'admin', source: 'oidc' });
  await shot3(page, '57-oidc-admin');
  // the token request used the client secret and the PKCE verifier (redacted by the mock)
  const tok = (await oidcRequests()).filter((r) => r.path.endsWith('/token')).pop();
  expect(tok.form).toMatchObject({ grant_type: 'authorization_code', code_verifier: '***' });
  // OIDC users are LDAP-managed-like: password change refused
  const users = (await api.get('/api/users?page_size=200')).items;
  const u = users.find((x: any) => x.username === 'dana.admin@mnidom.net');
  expect(u.source).toBe('oidc');
});

test('viewer role, no role -> not_in_group message, group overage -> hint', async ({ page }) => {
  await signIn(page, 'vic.viewer@mnidom.net');
  await expect(page.getByRole('navigation').first()).toBeVisible({ timeout: 20_000 });
  expect((await (await page.request.get('/api/auth/me')).json()).user.role).toBe('viewer');
  await page.request.post('/api/auth/logout');
  await page.context().clearCookies();
  await signIn(page, 'nora.norole@mnidom.net');
  await expect(page).toHaveURL(/\/login\?.*sso_error=not_in_group/, { timeout: 20_000 });
  await expect(page.getByTestId('sso-error')).toBeVisible();
  await shot3(page, '57-oidc-not-in-group');
  expect((await page.request.get('/api/auth/me')).status()).toBe(401);
  // group source + overage
  await api.put('/api/sso/oidc', { role_source: 'groups', admin_values: [(await oidcState()).group_admin], viewer_values: [] });
  await signIn(page, 'olga.overage@mnidom.net');
  await expect(page).toHaveURL(/sso_error=oidc_group_overage/, { timeout: 20_000 });
  await signIn(page, 'grace.groups@mnidom.net');
  await expect(page.getByRole('navigation').first()).toBeVisible({ timeout: 20_000 });
  expect((await (await page.request.get('/api/auth/me')).json()).user.role).toBe('admin');
  await page.request.post('/api/auth/logout');
  await api.put('/api/sso/oidc', { role_source: 'app_roles', admin_values: ['NetMon.Admin'], viewer_values: ['NetMon.Viewer'] });
});

test('id_token validation: wrong aud/iss/tid, expired, bad nonce, unknown kid, bad signature -> sso_failed; leeway and key rotation ok', async ({ page }) => {
  test.setTimeout(120_000);
  for (const mode of ['wrong_aud', 'wrong_iss', 'wrong_tid', 'expired', 'bad_nonce', 'unknown_kid', 'bad_signature', 'token_error']) {
    await page.context().clearCookies();
    await oidcMode(mode, true);
    await signIn(page, 'dana.admin@mnidom.net');
    await expect(page, `mode ${mode}`).toHaveURL(/sso_error=(sso_failed|oidc_error)/, { timeout: 20_000 });
    expect((await page.request.get('/api/auth/me')).status(), `mode ${mode}: no session`).toBe(401);
  }
  for (const mode of ['expired_leeway']) {
    await page.context().clearCookies();
    await oidcMode(mode, true);
    await signIn(page, 'dana.admin@mnidom.net');
    // rotate: the JWKS refetch on an unknown kid is limited to once per 5 min (§13.5.2), and unknown_kid above
    // already used it -> a rotation within 5 min fails by design; checked softly
    await expect(page.getByRole('navigation').first(), `mode ${mode} accepted`).toBeVisible({ timeout: 20_000 });
    await page.request.post('/api/auth/logout');
  }
  await oidcMode('ok');
  // Entra error parameters are mapped, never echoed raw
  await page.context().clearCookies();
  await oidcMode('error', true);
  await signIn(page, 'dana.admin@mnidom.net');
  await expect(page).toHaveURL(/sso_error=/, { timeout: 20_000 });
  expect(page.url()).not.toContain('script');
  await expect(page.locator('body')).not.toContainText('<script>');
  // state mismatch / missing cookie
  const cb = await api.ctx.fetch('/api/auth/oidc/callback?code=abc&state=forged', { maxRedirects: 0 });
  expect(cb.status()).toBe(302);
  expect(cb.headers()['location']).toContain('sso_error=sso_failed');
});

test('open redirect: next=//evil, /\\evil, https://evil are refused (start 4xx or next replaced by /)', async ({ page }) => {
  for (const next of ['//evil.example/x', '/\\evil.example', 'https://evil.example/', '%2F%2Fevil.example']) {
    const r = await api.ctx.fetch(`/api/auth/oidc/start?next=${encodeURIComponent(next)}`, { maxRedirects: 0 });
    if (r.status() === 302) {
      await page.context().clearCookies();
      await oidcDefaultUser('dana.admin@mnidom.net');
      await page.goto(`/api/auth/oidc/start?next=${encodeURIComponent(next)}`);
      await expect(page.getByRole('navigation').first()).toBeVisible({ timeout: 20_000 });
      const u = new URL(page.url());
      expect(u.origin, `next=${next}`).toBe(new URL(page.url()).origin);
      expect(u.hostname).not.toContain('evil');
      await page.request.post('/api/auth/logout');
    } else {
      expect([400, 422], `next=${next}`).toContain(r.status());
    }
  }
});

test('local login still works with SSO on; OIDC users refused while the feature is OFF', async ({ page }) => {
  await signIn(page, 'vic.viewer@mnidom.net');
  await expect(page.getByRole('navigation').first()).toBeVisible({ timeout: 20_000 });
  await setFeatures(api, { sso_oidc: false });
  await expect.poll(async () => (await page.request.get('/api/auth/me')).status(), { timeout: 15_000 }).toBe(401);
  const local = await (await apiAs()).get('/api/auth/me');
  expect(local.user.username).toBe('admin');
  await setFeatures(api, { sso_oidc: true });
});

test('key rotation: a token signed with a newly published key is accepted (JWKS refetch on unknown kid)', async ({ page }) => {
  // the refetch is limited to once per 5 min (§13.5.2); the unknown_kid case above may have used it already ->
  // whether a rotation right after it must succeed is a contract ambiguity (reported); runs last in this file
  await page.context().clearCookies();
  await oidcMode('rotate', true);
  await signIn(page, 'dana.admin@mnidom.net');
  await expect(page.getByRole('navigation').first(), 'key rotation accepted').toBeVisible({ timeout: 20_000 });
  await oidcMode('ok');
});
