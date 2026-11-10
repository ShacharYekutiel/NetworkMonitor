// [3] Feature toggles of phase 3 (docs/architecture.md §13.0, §13.11 spec 51; data-model § [3] Feature toggles).
// Fresh install: the 8 phase-3 toggles are OFF, their nav entries hidden, their endpoints 409 (kiosk 404),
// /noc and /ack show "not available"; each one can be turned ON and OFF at runtime without a restart.
import { expect, test } from '@playwright/test';
import { apiAs, loginViaApi, markDocument, expectSameDocument } from '../lib/helpers';
import { raw } from '../lib/lab';
import { GATED, P3_FEATURES, features, setFeatures, shot3, tx } from '../lib/lab3';

const NAV: Partial<Record<string, string>> = {
  config_backup: '/config-backups',
  flows: '/flows',
  new_device_alerts: '/new-devices',
  reports: '/reports',
};
// Toggles whose ON needs configuration first (422 with the missing parts): data-model § [3] Users, SSO.
const NEEDS_CONFIG = new Set(['sso_kerberos', 'sso_oidc']);

test.describe.configure({ mode: 'serial' });

test('fresh install: the 8 phase-3 toggles are OFF and their endpoints answer 409 / 404', async () => {
  const api = await apiAs();
  const f = await features(api);
  for (const n of P3_FEATURES) expect(f[n], `feature ${n} default`).toBe(false);
  const pub = await api.get('/api/settings/public');
  expect(pub.sso, 'settings/public sso while both SSO toggles are OFF').toEqual({ kerberos: null, oidc: null });
  const s = await api.get('/api/settings');
  for (const n of P3_FEATURES) expect(s[`feature_${n}_enabled`], `settings feature_${n}_enabled`).toBe(false);
  for (const n of P3_FEATURES) {
    const g = GATED[n];
    const r = await raw(api, g.method, g.path);
    expect(r.status, `${g.method} ${g.path} while ${n} is OFF: ${r.text}`).toBe(g.off);
    expect(r.body?.error?.code, `${g.path} error code`).toBe('feature_disabled');
  }
  // more endpoints of each feature
  for (const [m, p] of [
    ['GET', '/api/config-backups/status'], ['POST', '/api/config-backups/run'], ['GET', '/api/flows/exporters'],
    ['GET', '/api/flows/top/talkers'], ['GET', '/api/new-devices'], ['GET', '/api/new-devices/allowlist'],
    ['GET', '/api/reports/runs'], ['POST', '/api/chat-links/codes'], ['GET', '/api/chat-ack/telegram/status'],
  ] as const) {
    const r = await raw(api, m, p, m === 'POST' ? (p.includes('codes') ? { provider: 'telegram' } : {}) : undefined);
    expect(r.status, `${m} ${p} while OFF: ${r.text}`).toBe(409);
  }
  // OIDC callback while OFF -> redirect to /login?sso_error=feature_disabled
  const cb = await api.ctx.fetch('/api/auth/oidc/callback?code=x&state=y', { maxRedirects: 0 });
  expect([302, 303, 307]).toContain(cb.status());
  expect(cb.headers()['location'] ?? '').toContain('sso_error=feature_disabled');
  // kiosk: indistinguishable 404, also with a syntactically valid token
  const k = await api.ctx.fetch('/api/kiosk/view', { headers: { 'X-Netmon-Kiosk': 'nmk_' + 'a'.repeat(43) } });
  expect(k.status()).toBe(404);
  // admin endpoints that must stay usable while OFF (prepare before enabling)
  expect((await raw(api, 'GET', '/api/sso/kerberos')).status).toBe(200);
  expect((await raw(api, 'GET', '/api/sso/oidc')).status).toBe(200);
});

test('nav entries hidden; /noc and /ack say "not available" while OFF', async ({ page, browser }) => {
  await loginViaApi(page);
  for (const href of Object.values(NAV)) {
    await expect(page.locator(`nav a[href="${href}"], aside a[href="${href}"]`), `nav ${href} hidden`).toHaveCount(0);
  }
  // kiosk page in a context without a session (wall screen)
  const ctx = await browser.newContext();
  const kiosk = await ctx.newPage();
  await kiosk.goto('/noc#nmk_' + 'b'.repeat(43));
  await expect(kiosk.getByText(tx('noc.unavailable', /not available|unavailable/i))).toBeVisible({ timeout: 20_000 });
  expect(kiosk.url(), 'token removed from the address bar').not.toContain('nmk_');
  await shot3(kiosk, '51-noc-unavailable');
  await ctx.close();
  await page.goto('/ack#a1.' + 'A'.repeat(38));
  await expect(page.getByText(tx('chatAck.off', /not available|turned off|disabled/i))).toBeVisible({ timeout: 20_000 });
  await shot3(page, '51-ack-off');
});

test('each phase-3 toggle goes ON and OFF at runtime (no restart), nav follows live', async ({ page }) => {
  test.setTimeout(240_000);
  const api = await apiAs();
  await loginViaApi(page);
  await markDocument(page);
  for (const n of P3_FEATURES) {
    const g = GATED[n];
    if (NEEDS_CONFIG.has(n)) {
      const r = await raw(api, 'PUT', '/api/settings', { [`feature_${n}_enabled`]: true });
      expect(r.status, `enabling ${n} without configuration: ${r.text}`).toBe(422);
      expect((await features(api))[n]).toBe(false);
      continue;
    }
    const t0 = Date.now();
    await setFeatures(api, { [n]: true });
    await expect
      .poll(async () => (await raw(api, g.method, g.path)).status, { timeout: 5_000, message: `${g.path} after ${n} ON` })
      .not.toBe(409);
    expect(Date.now() - t0, `${n} applied within 5 s (+ poll slack)`).toBeLessThan(8_000);
    if (NAV[n]) await expect(page.locator(`nav a[href="${NAV[n]}"], aside a[href="${NAV[n]}"]`).first()).toBeVisible({ timeout: 10_000 });
    await setFeatures(api, { [n]: false });
    await expect
      .poll(async () => (await raw(api, g.method, g.path)).status, { timeout: 5_000, message: `${g.path} after ${n} OFF` })
      .toBe(g.off);
    if (NAV[n]) await expect(page.locator(`nav a[href="${NAV[n]}"], aside a[href="${NAV[n]}"]`)).toHaveCount(0, { timeout: 10_000 });
  }
  await expectSameDocument(page);
  // the backend kept running the whole time
  expect((await raw(api, 'GET', '/api/health')).status).toBe(200);
});

test('Features page: the 8 phase-3 features appear in the read-only overview (five headings), each linking to its tab', async ({ page }) => {
  await loginViaApi(page, undefined, '/settings?tab=features');
  for (const h of ['Monitoring', 'Logs & events', 'Network', 'Access', 'Notifications']) {
    await expect(page.getByRole('heading', { name: h }).first(), `heading ${h}`).toBeVisible({ timeout: 15_000 });
  }
  const overview = page.getByTestId('features-overview');
  for (const n of P3_FEATURES) {
    const label = tx(`features.name.${n}`, new RegExp(n.replace(/_/g, '.?'), 'i'));
    const row = overview.locator(`[data-feature-overview="${n}"]`);
    await expect(row.getByText(label).first(), `overview ${n}`).toBeVisible();
    await expect(row.getByRole('link')).toBeVisible();
  }
  // switched only in their own tab: no phase-3 switch on the Features tab
  for (const n of P3_FEATURES) await expect(page.locator(`[data-feature="${n}"]`)).toHaveCount(0);
  await shot3(page, '51-features-tab');
});

test('own tabs: toggle card at the top, settings only while ON (Config backup)', async ({ page }) => {
  const api = await apiAs();
  await setFeatures(api, { config_backup: false });
  await loginViaApi(page, undefined, '/settings?tab=features');
  await page.getByTestId('features-overview').locator('[data-feature-overview="config_backup"]').getByRole('link').click();
  await expect(page).toHaveURL(/tab=backup/);
  const card = page.getByTestId('feature-toggles');
  const sw = card.locator('[data-feature="config_backup"]').getByRole('switch');
  await expect(sw).toHaveAttribute('aria-checked', 'false');
  const title = page.getByRole('heading', { name: tx('settings3.backupTitle', /configuration backup/i) });
  await expect(title).toHaveCount(0);
  await shot3(page, '51-backup-tab-off');
  await sw.click();
  await expect.poll(async () => (await features(api)).config_backup, { timeout: 10_000 }).toBe(true);
  await expect(title).toBeVisible();
  await sw.click();
  await expect.poll(async () => (await features(api)).config_backup, { timeout: 10_000 }).toBe(false);
  await expect(title).toHaveCount(0);
});

test('SSO tab is configure-first: both forms shown while OFF, switches at their bottom stay locked until validated', async ({ page }) => {
  const api = await apiAs();
  await setFeatures(api, { sso_kerberos: false, sso_oidc: false });
  await loginViaApi(page, undefined, '/settings?tab=sso');
  await expect(page.getByRole('heading', { name: tx('kerberos.title', /kerberos/i) })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole('heading', { name: tx('oidc.title', /entra|microsoft/i) })).toBeVisible();
  const krb = page.locator('[data-testid="feature-toggles"][data-feature="sso_kerberos"]');
  const oidc = page.locator('[data-testid="feature-toggles"][data-feature="sso_oidc"]');
  await expect(krb.getByRole('switch')).toBeDisabled();
  await expect(krb.getByTestId('enable-hint')).toBeVisible();
  await expect(oidc.getByRole('switch')).toBeDisabled();
  await expect(oidc.getByTestId('enable-hint')).toHaveText(tx('features.enableNeedsTest', /test the connection to enable/i));
  await shot3(page, '51-sso-tab-off');
});
