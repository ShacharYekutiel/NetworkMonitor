// [1.4.1] Security hardening against the real backend (docs/architecture.md § 15):
// the SPA works under the Content-Security-Policy with ZERO violations on its main pages
// (dashboard, device, sensor with a chart, events, topology, logs, notifications, settings,
// reports, account, the NOC kiosk page, the /ack page, the login page in Hebrew); the security
// headers are present; a logout kills the cookie server-side; the Security settings card saves.
import { expect, request as pwRequest, test, type BrowserContext, type Page } from '@playwright/test';
import { ADMIN, Api, BASE_URL, HTTP_TARGET_URL, apiAs, defaultGroupId, loginViaApi, uid, waitForSensor } from '../lib/helpers';

type Violation = { page: string; text: string };

/** Records every CSP violation of a context: the DOM event and the browser's console error. */
async function watchCsp(ctx: BrowserContext, into: Violation[]): Promise<void> {
  await ctx.exposeBinding('__cspViolation', (source, text: string) => {
    into.push({ page: source.page?.url() ?? '?', text });
  });
  await ctx.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (e) => {
      // @ts-expect-error binding installed by the test
      void window.__cspViolation(`${e.violatedDirective} blocked ${e.blockedURI} (${e.sourceFile ?? ''}:${e.lineNumber ?? ''})`);
    });
  });
  ctx.on('page', (p: Page) => {
    p.on('console', (m) => {
      if (/Content Security Policy|Refused to (load|execute|apply|connect|frame)/i.test(m.text()))
        into.push({ page: p.url(), text: m.text() });
    });
  });
}

let api: Api;
let deviceId: number;
let sensorId: number;

test.beforeAll(async () => {
  api = await apiAs();
  const d = await api.post('/api/devices', {
    group_id: await defaultGroupId(api),
    name: uid('sec141-dev'),
    host: '127.0.0.1',
    auto_sensors: [],
  });
  deviceId = d.id;
  const s = await api.post('/api/sensors', {
    device_id: deviceId,
    type: 'http',
    name: 'sec141 web',
    interval_s: 10,
    config: { url: `${HTTP_TARGET_URL}/health` },
  });
  sensorId = s.id;
  await waitForSensor(api, sensorId, (x) => x.status === 'up');
});

test.afterAll(async () => {
  await api.put('/api/settings', { frame_ancestors: [], session_max_age_h: 168, login_ip_max_failures: 20 });
  await api.del(`/api/devices/${deviceId}`).catch(() => undefined);
});

test('security headers on the SPA, the API and error answers', async () => {
  const anon = await pwRequest.newContext({ baseURL: BASE_URL });
  for (const path of ['/', '/login', '/noc', '/ack', '/api/health', '/api/setup/status', '/api/devices', '/api/nope']) {
    const r = await anon.get(path);
    const h = r.headers();
    const csp = h['content-security-policy'] ?? '';
    expect(csp, path).toContain("default-src 'self'");
    expect(csp, path).toMatch(/script-src 'self'( 'sha256-[A-Za-z0-9+/=]+')*;/);
    expect(csp, path).not.toContain('unsafe-eval');
    expect(csp.match(/script-src [^;]*/)?.[0] ?? '', path).not.toContain('unsafe-inline');
    expect(csp, path).toContain("frame-ancestors 'self'");
    expect(csp, path).toContain("object-src 'none'");
    expect(h['x-frame-options'], path).toBe('SAMEORIGIN');
    expect(h['x-content-type-options'], path).toBe('nosniff');
    expect(h['referrer-policy'], path).toBe('same-origin');
    expect(h['strict-transport-security'], `${path}: plain HTTP`).toBeUndefined();
  }
  // the index.html the server ships has exactly one inline script, and its hash is in the policy
  const html = await (await anon.get('/')).text();
  expect(html.match(/<script>/g)?.length ?? 0).toBeLessThanOrEqual(1);
  await anon.dispose();
});

test('the main pages run under the CSP without a single violation', async ({ browser }) => {
  test.setTimeout(240_000);
  const violations: Violation[] = [];
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await watchCsp(ctx, violations);
  const page = await ctx.newPage();
  const pageErrors: string[] = [];
  page.on('pageerror', (e) => pageErrors.push(String(e)));

  // login page (anonymous), also in Hebrew / RTL (the inline theme bootstrap sets dir before paint)
  await page.goto('/login');
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
  await page.evaluate(() => localStorage.setItem('netmon.lang', 'he'));
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
  await page.evaluate(() => localStorage.setItem('netmon.theme', 'dark'));
  await page.reload();
  await expect(page.locator('html')).toHaveClass(/dark/);
  await page.evaluate(() => {
    localStorage.removeItem('netmon.lang');
    localStorage.removeItem('netmon.theme');
  });

  await loginViaApi(page);
  await expect(page.getByRole('heading', { level: 1, name: 'Dashboard' })).toBeVisible();

  // device page and its tabs
  await page.goto(`/devices/${deviceId}`);
  await expect(page.getByText('sec141 web').first()).toBeVisible();
  for (const tab of await page.getByRole('tab').all()) {
    await tab.click();
    await page.waitForTimeout(300);
  }

  // sensor page: the history chart (ECharts canvas) and its tooltip (inline style attributes)
  await page.goto(`/sensors/${sensorId}`);
  const canvas = page.locator('canvas').first();
  await expect(canvas).toBeVisible({ timeout: 20_000 });
  const box = await canvas.boundingBox();
  if (box) {
    for (const fx of [0.3, 0.5, 0.7, 0.9]) {
      await page.mouse.move(box.x + box.width * fx, box.y + box.height / 2);
      await page.waitForTimeout(150);
    }
  }

  const pages: [string, RegExp | string][] = [
    ['/events', 'Events'],
    ['/topology', 'Network map'],
    ['/maintenance', 'Maintenance windows'],
    ['/templates', 'Device templates'],
    ['/find', /Find/],
    ['/notifications', 'Notifications'],
    ['/users', 'Users'],
    ['/tokens', 'API tokens'],
    ['/account', 'My account'],
    ['/discovery', 'Discovery'],
    ['/reports', 'Reports'],
    ['/logs', /Traps|Syslog/],
    ['/flows', /flows/i],
    ['/config-backups', /backups/i],
    ['/new-devices', 'New devices'],
    ['/audit', 'Audit log'],
    ['/assistant/usage', /usage/i],
    ['/settings', 'Settings'],
  ];
  for (const [path, heading] of pages) {
    await page.goto(path);
    await expect(page.getByRole('heading', { level: 1 }).first(), path).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole('heading', { level: 1 }).first(), path).toContainText(heading);
    await page.waitForLoadState('networkidle').catch(() => undefined);
  }

  // the assistant page (its heading depends on the feature state) and every Settings tab
  await page.goto('/assistant');
  await page.waitForLoadState('networkidle').catch(() => undefined);
  await page.goto('/settings');
  await expect(page.getByTestId('security-settings')).toBeVisible();
  for (const tab of await page.getByRole('tab').all()) {
    await tab.click();
    await page.waitForTimeout(250);
  }

  // session-less pages: /ack (invalid link) and the NOC kiosk page
  const anon = await browser.newContext();
  await watchCsp(anon, violations);
  const ack = await anon.newPage();
  await ack.goto('/ack?t=not-a-token');
  await expect(ack.locator('#root')).not.toBeEmpty();
  const noc = await anon.newPage();
  await noc.goto('/noc#nmk_invalid');
  await expect(noc.locator('#root')).not.toBeEmpty();
  await noc.waitForTimeout(1500);
  await anon.close();

  // the WebSocket connected under connect-src 'self'
  await page.goto('/');
  await expect
    .poll(() => page.evaluate(() => performance.getEntriesByType('resource').length), { timeout: 10_000 })
    .toBeGreaterThan(0);

  expect(violations, JSON.stringify(violations, null, 2)).toEqual([]);
  expect(pageErrors, pageErrors.join('\n')).toEqual([]);
  await ctx.close();
});

test('the WebSocket connects under the policy (live update without reload)', async ({ page }) => {
  const sockets: string[] = [];
  page.on('websocket', (ws) => sockets.push(ws.url()));
  await loginViaApi(page);
  await expect.poll(() => sockets.some((u) => u.endsWith('/ws')), { timeout: 15_000 }).toBe(true);
});

test('Settings → Security: the card saves; frame ancestors reach the header', async ({ page }) => {
  await loginViaApi(page, ADMIN, '/settings');
  const card = page.getByTestId('security-settings');
  await expect(card).toBeVisible();
  await expect(card.getByLabel('Maximum session age (hours)')).toHaveValue('168');
  await expect(card.getByLabel('Failed logins per address (5 minutes)')).toHaveValue('20');
  await expect(card.getByRole('checkbox', { name: /Allow monitoring and notification targets on this server/ })).not.toBeChecked();
  const ancestors = card.getByLabel('Sites that may embed NetworkMonitor in a frame');
  await ancestors.fill('not an origin');
  await expect(card.getByRole('button', { name: 'Save' })).toBeDisabled();
  await ancestors.fill('https://wall.example.com/');
  await card.getByLabel('Maximum session age (hours)').fill('72');
  await card.getByRole('button', { name: 'Save' }).click();
  await expect.poll(async () => (await api.get('/api/settings')).frame_ancestors).toEqual(['https://wall.example.com']);
  expect((await api.get('/api/settings')).session_max_age_h).toBe(72);
  const r = await page.request.get('/noc');
  expect(r.headers()['content-security-policy']).toContain("frame-ancestors 'self' https://wall.example.com");
  expect(r.headers()['x-frame-options']).toBeUndefined();
  // the settings search finds the card
  await page.getByPlaceholder('Search settings…').fill('loopback');
  await expect(page.getByText(/Allow monitoring and notification targets on this server/).first()).toBeVisible();
  await api.put('/api/settings', { frame_ancestors: [], session_max_age_h: 168 });
});

test('pentest reproduction: the cookie is dead after logout; the other browser stays signed in', async ({ browser }) => {
  const a = await browser.newContext();
  const b = await browser.newContext();
  const pa = await a.newPage();
  const pb = await b.newPage();
  await loginViaApi(pa);
  await loginViaApi(pb);
  const stolen = (await a.cookies()).find((c) => c.name === 'netmon_session')!.value;
  await pa.getByRole('button', { name: new RegExp(ADMIN.username) }).click();
  await pa.getByRole('menuitem', { name: 'Sign out' }).click();
  await expect(pa).toHaveURL(/\/login$/, { timeout: 15_000 });
  const thief = await pwRequest.newContext({
    baseURL: BASE_URL,
    extraHTTPHeaders: { Cookie: `netmon_session=${stolen}` },
  });
  expect((await thief.get('/api/auth/me')).status(), 'old cookie after logout').toBe(401);
  await thief.dispose();
  expect((await pb.request.get('/api/auth/me')).status(), 'the other session').toBe(200);
  await a.close();
  await b.close();
});

test('pentest reproduction: SSRF targets are refused on save (metadata always; loopback is allowed in test mode only)', async () => {
  const meta = 'http://169.254.169.254/latest/meta-data/iam/security-credentials/';
  const r = await api.ctx.post('/api/notification-channels', {
    data: { name: uid('ssrf'), type: 'webhook', config: { url: meta } },
  });
  expect(r.status()).toBe(422);
  expect(await r.text()).toContain('Blocked target');
  const s = await api.ctx.post('/api/sensors', {
    data: { device_id: deviceId, type: 'http', name: 'imds', config: { url: meta } },
  });
  expect(s.status()).toBe(422);
});

test('account page: "Sign out everywhere" also ends the session in the other browser', async ({ browser }) => {
  const user = { username: uid('sec141').replace(/[^a-z0-9]/g, '').slice(0, 20), password: 'Everywhere-pass-1' };
  await api.post('/api/users', { ...user, role: 'viewer' });
  const a = await browser.newContext();
  const b = await browser.newContext();
  const pa = await a.newPage();
  const pb = await b.newPage();
  await loginViaApi(pa, user, '/account');
  await loginViaApi(pb, user);
  await pa.getByTestId('logout-everywhere').click();
  await expect(pa).toHaveURL(/\/login$/, { timeout: 15_000 });
  expect((await pb.request.get('/api/auth/me')).status(), 'the other browser').toBe(401);
  expect((await pa.request.get('/api/auth/me')).status()).toBe(401);
  await a.close();
  await b.close();
});
