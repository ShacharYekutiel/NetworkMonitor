import { expect, test } from '@playwright/test';
import { apiAs, defaultGroupId, loginViaApi, uid } from '../lib/helpers';

// Cross-cutting UI checks against the real backend (each test is independent).

test('login page uses settings.default_language when nothing is stored locally', async ({ browser }) => {
  const api = await apiAs();
  await api.put('/api/settings', { default_language: 'he' });
  try {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await page.goto('/login');
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
    await expect(page.getByRole('heading', { name: 'כניסה' })).toBeVisible();
    const pub = await (await page.request.get('/api/settings/public')).json();
    // [2b-1] settings/public gained `timezone` (additive, api.md [2b-1] Settings).
    expect(pub).toEqual({ default_language: 'he', timezone: expect.any(String) });
    await ctx.close();
  } finally {
    await api.put('/api/settings', { default_language: 'en' });
  }
});

test('a lost session (401) sends the SPA back to the login page', async ({ page, context }) => {
  await loginViaApi(page);
  await expect(page.getByRole('heading', { level: 1, name: 'Dashboard' })).toBeVisible();
  await context.clearCookies();
  // Client-side navigation triggers new API calls, which now answer 401.
  await page.getByRole('complementary').getByRole('link', { name: 'Events' }).click();
  await expect(page).toHaveURL(/\/login$/, { timeout: 15_000 });
});

test('a validation error from the API is shown with its details', async ({ page }) => {
  const api = await apiAs();
  const d = await api.post('/api/devices', {
    group_id: await defaultGroupId(api),
    name: uid('val-dev'),
    host: '127.0.0.1',
    auto_sensors: [],
  });
  await loginViaApi(page, undefined, `/devices/${d.id}`);
  await page.getByRole('button', { name: 'Add sensor' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel('Type').selectOption('http');
  await dlg.getByRole('textbox', { name: /^URL/ }).fill('http://{host}/');
  // 99 passes the client's int-list check but violates the API's 100..599 range -> 422.
  await dlg.getByRole('textbox', { name: /^Expected status/ }).fill('99');
  await dlg.getByRole('button', { name: 'Create sensor' }).click();
  await expect(dlg.getByRole('alert')).toContainText('Some values are invalid.');
  await expect(dlg.getByRole('alert')).toContainText('expected_status');
});

test('duplicate group name shows the conflict message', async ({ page }) => {
  await loginViaApi(page);
  await page.getByRole('button', { name: 'Add group' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel(/^Name/).fill('Default');
  await dlg.getByRole('button', { name: 'Save' }).click();
  await expect(dlg.getByRole('alert')).toHaveText('That name already exists.');
});

test('SNMP interface picker shows the snmp_error reason when the agent does not answer', async ({ page }) => {
  const api = await apiAs();
  const d = await api.post('/api/devices', {
    group_id: await defaultGroupId(api),
    name: uid('no-agent'),
    host: '127.0.0.1',
    // Nothing listens on UDP port 9 (discard) here.
    snmp_credentials: { version: 'v2c', port: 9, timeout_s: 1, retries: 0, community: 'public' },
    auto_sensors: [],
  });
  await loginViaApi(page, undefined, `/devices/${d.id}`);
  await page.getByRole('button', { name: 'Add sensor' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel('Type').selectOption('snmp_interface');
  await expect(dlg.getByRole('alert')).toContainText('SNMP request failed:');
  await expect(dlg.getByRole('button', { name: 'Retry' })).toBeVisible();
});

test.describe('known UI defects (expected behavior)', () => {
  test('modal backdrop covers the whole viewport', async ({ page }) => {
    await loginViaApi(page);
    await page.getByRole('button', { name: 'Add group' }).click();
    const overlay = page.getByRole('dialog').locator('xpath=..');
    const box = await overlay.boundingBox();
    // The overlay is `fixed inset-0`, but it is rendered inside a `space-y-*` container and gets
    // margin-top: 1rem, so it starts 16px below the top and leaves the header strip undimmed.
    expect(box!.y).toBe(0);
  });

  test('dialogs have an accessible name', async ({ page }) => {
    await loginViaApi(page);
    await page.getByRole('button', { name: 'Add group' }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.getByRole('dialog', { name: 'Add group' })).toBeVisible({ timeout: 2_000 });
  });

  test('alert-rule checkboxes are labelled by their own text only', async ({ page }) => {
    await loginViaApi(page, undefined, '/notifications');
    await page.getByRole('button', { name: 'Add rule' }).click();
    const dlg = page.getByRole('dialog');
    // The "Notify on" and "Channels" fields wrap the checkbox <label>s in another <label>, so the
    // first checkbox gets the whole field text as its accessible name.
    await expect(dlg.getByRole('checkbox', { name: 'Down', exact: true })).toBeVisible({ timeout: 2_000 });
    await expect(dlg.getByRole('checkbox', { name: 'Warning', exact: true })).toBeVisible({ timeout: 2_000 });
    await expect(dlg.getByRole('checkbox', { name: 'Up', exact: true })).toBeVisible({ timeout: 2_000 });
  });

  test('no horizontal overflow on a phone-sized viewport', async ({ browser }) => {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await ctx.newPage();
    await loginViaApi(page);
    for (const path of ['/', '/events']) {
      await page.goto(path);
      await expect(page.locator('main h1')).toBeVisible();
      const overflow = await page.evaluate(() => {
        const m = document.querySelector('main')!;
        return { doc: document.documentElement.scrollWidth - window.innerWidth, main: m.scrollWidth - m.clientWidth };
      });
      expect(overflow.doc, `${path} document overflow`).toBeLessThanOrEqual(0);
      // Wide tables must scroll inside their card, not widen the page content (dashboard cards
      // are grid items without min-w-0, so they grow to the table width and <main> scrolls sideways).
      expect(overflow.main, `${path} <main> horizontal overflow`).toBeLessThanOrEqual(0);
    }
    await ctx.close();
  });
});
