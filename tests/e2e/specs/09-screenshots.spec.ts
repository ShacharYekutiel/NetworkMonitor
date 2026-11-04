import { expect, test, type Browser, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { ADMIN, BASE_URL, SCREENSHOT_DIR, apiAs } from '../lib/helpers';

// Screenshots of key screens of the real system (runs last, so the history has more points).
// Not assertions about pixels: it only checks that each screen rendered.

async function open(browser: Browser, scale: number, lang: 'en' | 'he', opts: { width?: number; height?: number; dark?: boolean } = {}) {
  const ctx = await browser.newContext({
    baseURL: BASE_URL,
    viewport: { width: opts.width ?? 1440, height: opts.height ?? 900 },
    deviceScaleFactor: scale,
    colorScheme: opts.dark ? 'dark' : 'light',
  });
  const page = await ctx.newPage();
  const r = await page.request.post('/api/auth/login', { data: ADMIN });
  expect(r.status()).toBe(200);
  await page.request.patch('/api/auth/me', { data: { language: lang } });
  return { ctx, page };
}

async function save(page: Page, name: string) {
  mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.waitForLoadState('networkidle');
  await page.screenshot({ path: join(SCREENSHOT_DIR, `${name}.png`) });
}

test.afterAll(async () => {
  const api = await apiAs();
  await api.patch('/api/auth/me', { language: 'en' });
});

test('dashboard (en, he)', async ({ browser }) => {
  for (const lang of ['en', 'he'] as const) {
    const { ctx, page } = await open(browser, 2, lang);
    await page.goto('/');
    await expect(page.locator('html')).toHaveAttribute('dir', lang === 'he' ? 'rtl' : 'ltr');
    await expect(page.locator('main h1')).toBeVisible();
    // Expand all devices in the tree for a fuller picture.
    await page.locator('aside a[href^="/devices/"]').first().waitFor();
    const closed = page.locator('aside button[aria-expanded="false"]:not(.invisible)');
    while ((await closed.count()) > 0) await closed.first().click();
    await save(page, `dashboard-${lang}@2x`);
    await ctx.close();
  }
});

test('sensor pages with real charts', async ({ browser }) => {
  const api = await apiAs();
  const sensors = (await api.get('/api/sensors?page_size=1000')).items as any[];
  const ping = sensors.filter((s) => s.type === 'ping' && s.status !== 'paused').sort((a, b) => a.id - b.id)[0];
  const eth0 = sensors.find((s) => s.type === 'snmp_interface' && s.name === 'eth0');
  const http = sensors.find((s) => s.type === 'http');

  const { ctx, page } = await open(browser, 2, 'en');
  if (ping) {
    await page.goto(`/sensors/${ping.id}`);
    await page.getByRole('button', { name: '1 hour' }).click();
    await expect(page.locator('main canvas').first()).toBeVisible();
    await save(page, 'sensor-ping-chart@2x');
  }
  await ctx.close();

  const s1 = await open(browser, 1, 'en');
  if (eth0) {
    await s1.page.goto(`/sensors/${eth0.id}`);
    await s1.page.getByRole('button', { name: '1 hour' }).click();
    await expect(s1.page.locator('main canvas').first()).toBeVisible();
    await save(s1.page, 'sensor-snmp-eth0-chart');
  }
  if (http) {
    await s1.page.goto(`/sensors/${http.id}`);
    await save(s1.page, 'sensor-http');
  }
  await s1.page.goto('/events');
  await save(s1.page, 'events');
  await s1.page.goto('/notifications');
  await save(s1.page, 'notifications');
  await s1.page.goto('/settings');
  await save(s1.page, 'settings');
  await s1.ctx.close();

  const he = await open(browser, 1, 'he');
  if (ping) {
    await he.page.goto(`/sensors/${ping.id}`);
    await he.page.getByRole('button', { name: 'שעה' }).click().catch(() => undefined);
    await save(he.page, 'sensor-ping-chart-he');
  }
  if (eth0) {
    await he.page.goto(`/devices/${eth0.device_id}`);
    await save(he.page, 'device-snmp-he');
  }
  await he.ctx.close();

  const dark = await open(browser, 1, 'en', { dark: true });
  await dark.page.goto(ping ? `/sensors/${ping.id}` : '/');
  await save(dark.page, 'sensor-dark');
  await dark.ctx.close();

  const phone = await open(browser, 1, 'en', { width: 390, height: 844 });
  await phone.page.goto('/');
  await save(phone.page, 'dashboard-phone');
  await phone.ctx.close();
});

test('setup/login screens', async ({ browser }) => {
  const ctx = await browser.newContext({ baseURL: BASE_URL, viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  await page.goto('/login');
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
  await save(page, 'login');
  await ctx.close();
});
