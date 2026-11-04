import { expect, test } from '@playwright/test';
import { ADMIN, apiAs, loginViaApi, shot } from '../lib/helpers';

// Scenario (e): language switch to Hebrew -> dir=rtl, texts in Hebrew; persists after reload and in
// a fresh browser (user.language on the server).
test.describe.configure({ mode: 'serial' });

test.afterAll(async () => {
  // Leave the admin in English for the other specs.
  const api = await apiAs();
  await api.patch('/api/auth/me', { language: 'en' });
});

test('switch to Hebrew in the top bar', async ({ page }) => {
  await loginViaApi(page);
  await expect(page.locator('html')).toHaveAttribute('dir', 'ltr');
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');

  const meSaved = page.waitForResponse(
    (r) => r.url().endsWith('/api/auth/me') && r.request().method() === 'PATCH',
  );
  await page.getByRole('button', { name: /^Language:/ }).click();
  await page.getByRole('listbox').getByRole('option', { name: 'עברית' }).click();
  expect((await meSaved).status()).toBe(200);

  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
  await expect(page.locator('html')).toHaveAttribute('lang', 'he');
  await expect(page.getByRole('heading', { level: 1, name: 'לוח בקרה' })).toBeVisible();
  const nav = page.getByRole('complementary');
  await expect(nav.getByRole('link', { name: 'אירועים' })).toBeVisible();
  await expect(nav.getByRole('link', { name: 'התראות' })).toBeVisible();
  await expect(page.getByRole('heading', { level: 2, name: 'אירועים אחרונים' })).toBeVisible();
  await expect(page.getByTestId('ws-indicator')).toContainText('מחובר');
  await expect(page.getByRole('button', { name: 'הוספת התקן' })).toBeVisible();
  // No English UI chrome left on the dashboard.
  await expect(page.getByText('Recent events')).toHaveCount(0);
  await expect(page.getByText('Add device')).toHaveCount(0);

  // The sidebar is on the right in RTL.
  const aside = await page.getByRole('complementary').boundingBox();
  const main = await page.locator('main').boundingBox();
  expect(aside!.x).toBeGreaterThan(main!.x);

  const me = await (await page.request.get('/api/auth/me')).json();
  expect(me.user.language).toBe('he');
});

test('Hebrew persists after reload and in a new browser session', async ({ page, browser }) => {
  await loginViaApi(page);
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
  await expect(page.getByRole('heading', { level: 1, name: 'לוח בקרה' })).toBeVisible();

  // A fresh context has no localStorage: the language must come from user.language.
  const ctx = await browser.newContext();
  const p2 = await ctx.newPage();
  await p2.goto('/login');
  await p2.getByLabel(/Username|שם משתמש/).fill(ADMIN.username);
  await p2.getByLabel(/Password|סיסמה/).fill(ADMIN.password);
  await p2.getByRole('button', { name: /Sign in|כניסה/ }).click();
  await expect(p2.locator('html')).toHaveAttribute('dir', 'rtl');
  await expect(p2.getByRole('heading', { level: 1, name: 'לוח בקרה' })).toBeVisible();
  await ctx.close();
});

test('Hebrew sensor/device pages keep numbers and hosts LTR', async ({ page }) => {
  await loginViaApi(page);
  await page.locator('aside a[href^="/groups/"]').first().waitFor();
  const firstDevice = page.locator('aside a[href^="/devices/"]').first();
  test.skip((await firstDevice.count()) === 0, 'no device yet (run the full suite)');
  await firstDevice.click();
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
  // The host (IP) is wrapped in an LTR element.
  const host = page.locator('main [dir="ltr"]', { hasText: /\d+\.\d+\.\d+\.\d+/ }).first();
  await expect(host).toBeVisible();
  await shot(page, 'device-he');
});

test('switch back to English', async ({ page }) => {
  await loginViaApi(page);
  await page.getByRole('button', { name: /^(Language|שפה):/ }).click();
  await page.getByRole('listbox').getByRole('option', { name: 'English' }).click();
  await expect(page.locator('html')).toHaveAttribute('dir', 'ltr');
  await expect(page.getByRole('heading', { level: 1, name: 'Dashboard' })).toBeVisible();
  await expect
    .poll(async () => (await (await page.request.get('/api/auth/me')).json()).user.language)
    .toBe('en');
});
