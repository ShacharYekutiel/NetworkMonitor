import { expect, test, type Browser, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { Api, SCREENSHOT_DIR, apiAs, defaultGroupId } from '../lib/helpers';
import { LAB_RANGE, LAB_SCAN_OPTIONS, horizontalOverflow, startScan, waitForScan } from '../lib/lab';

// Phase 2a (i): Discovery page in Hebrew (RTL) and on a phone (no horizontal page overflow), plus
// screenshots of real discovery results (en/he, desktop/phone) for the report.
const SHOTS = process.env.QA2A_SHOT_DIR ?? join(SCREENSHOT_DIR, 'qa-2a');

let api: Api;
let scanId = 0;

test.beforeAll(async () => {
  api = await apiAs();
  const scans = (await api.get('/api/discovery/scans')).items;
  const lab = scans.find((s: any) => s.status === 'completed' && s.targets.join() === LAB_RANGE && s.options.ping === false);
  if (lab) scanId = lab.id;
  else {
    const s = await startScan(api, { targets: [LAB_RANGE], group_id: await defaultGroupId(api), options: LAB_SCAN_OPTIONS });
    scanId = (await waitForScan(api, s.id)).id;
  }
});
test.afterAll(async () => {
  await api.patch('/api/auth/me', { language: 'en' });
});

async function open(browser: Browser, lang: 'en' | 'he', viewport = { width: 1440, height: 900 }, scale = 1) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: scale, locale: lang === 'he' ? 'he-IL' : 'en-US', timezoneId: 'UTC' });
  const page = await ctx.newPage();
  const r = await page.request.post('/api/auth/login', { data: { username: 'admin', password: 'Admin-pass-1' } });
  expect(r.status()).toBe(200);
  await page.request.patch('/api/auth/me', { data: { language: lang } });
  return { ctx, page };
}
async function save(page: Page, name: string) {
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: true });
}

test('Hebrew: Discovery page and results are RTL and translated', async ({ browser }) => {
  const { ctx, page } = await open(browser, 'he');
  await page.goto('/discovery');
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
  await expect(page.locator('html')).toHaveAttribute('lang', 'he');
  const h1 = page.getByRole('heading', { level: 1 });
  await expect(h1).toBeVisible();
  await expect(h1).not.toHaveText('Discovery');
  await expect(h1).toHaveText(/[֐-׿]/);
  // No untranslated English chrome on the form.
  const main = page.locator('main');
  for (const en of ['Targets', 'Start scan', 'New scan', 'Saved profiles', 'Extra communities (v2c)', 'TCP ports', 'Advanced', 'Reverse DNS']) {
    await expect.soft(main.getByText(en, { exact: true }), en).toHaveCount(0);
  }
  await save(page, 'discovery-scans-he');

  await page.goto(`/discovery/scans/${scanId}`);
  const table = page.getByRole('table');
  const row = (ip: string) => table.getByRole('row').filter({ hasText: ip });
  await expect(row('127.0.0.21')).toBeVisible();
  // Class names from deviceClass.* (he), api.md § Discovery notification payload.
  await expect(row('127.0.0.21')).toContainText('נתב');
  await expect(row('127.0.0.22')).toContainText('נקודת גישה');
  await expect(row('127.0.0.23')).toContainText('מדפסת');
  await expect(row('127.0.0.24')).toContainText('מתג');
  await expect(row('127.0.0.25')).toContainText('לא ידוע');
  // Template options use the localized names.
  // Imported/existing rows have no template select; selectable rows show localized template names.
  const sel = table.getByRole('combobox').first();
  if (await sel.count()) await expect(sel.locator('option').first()).toHaveText(/[\u0590-\u05FF]/);
  // IP addresses render LTR inside the RTL table.
  const ipCell = table.getByText('127.0.0.21', { exact: true }).first();
  const dir = await ipCell.evaluate((el) => getComputedStyle(el).direction);
  expect(dir).toBe('ltr');
  for (const en of ['Router / firewall', 'Printer', 'Switch', 'Unknown', 'Hide existing devices', 'Only new', 'Import into group']) {
    await expect.soft(page.locator('main').getByText(en, { exact: true }), en).toHaveCount(0);
  }
  await save(page, 'discovery-results-he');
  await page.goto('/templates');
  await expect(page.getByText('נקודת גישה UniFi', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('חומת אש OPNsense / pfSense', { exact: true }).first()).toBeVisible();
  await save(page, 'templates-he');
  await ctx.close();
});

test('English results screenshot (desktop, @2x)', async ({ browser }) => {
  const { ctx, page } = await open(browser, 'en', { width: 1440, height: 900 }, 2);
  await page.goto(`/discovery/scans/${scanId}`);
  await expect(page.getByRole('table').getByRole('row').filter({ hasText: '127.0.0.25' })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('dir', 'ltr');
  await save(page, 'discovery-results-en');
  await page.goto('/discovery');
  await expect(page.getByLabel(/^Targets/)).toBeVisible();
  await save(page, 'discovery-scans-en');
  await page.goto('/templates');
  await expect(page.getByText('UniFi access point').first()).toBeVisible();
  await save(page, 'templates-en');
  await ctx.close();
});

for (const lang of ['en', 'he'] as const) {
  test(`phone (${lang}): no horizontal overflow on /discovery and the results page`, async ({ browser }) => {
    const { ctx, page } = await open(browser, lang, { width: 390, height: 844 }, 2);
    for (const [path, name] of [
      ['/discovery', 'discovery-scans'],
      [`/discovery/scans/${scanId}`, 'discovery-results'],
    ] as const) {
      await page.goto(path);
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
      if (path !== '/discovery') await expect(page.getByText('127.0.0.25').first()).toBeAttached();
      await page.waitForLoadState('networkidle');
      const o = await horizontalOverflow(page);
      expect.soft(o.scrollWidth, `${path} ${lang}: scrollWidth ${o.scrollWidth} > clientWidth ${o.clientWidth}`).toBeLessThanOrEqual(o.clientWidth + 1);
      await save(page, `${name}-${lang}-phone`);
    }
    await ctx.close();
  });
}
