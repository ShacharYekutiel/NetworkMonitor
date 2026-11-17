// [premium] My account → signed-in browsers (GET/DELETE /api/auth/sessions): a second browser
// (an iPhone User-Agent) is listed with its browser · system and IP; "Sign out" on its row ends
// only that session (its cookie answers 401, this page stays signed in); "Sign out" on the row
// of this browser is the normal sign out.
import { expect, request as pwRequest, test } from '@playwright/test';
import { Api, BASE_URL, apiAs, loginViaApi, shot } from '../lib/helpers';
import { raw } from '../lib/lab';
import { T } from '../lib/lab2b2';

const USER = { username: 'qa6-sessions', password: 'Sessions-pass-1' };
const IPHONE =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1';

test.describe.configure({ mode: 'serial' });

test('the account page lists both browsers; per-row sign out ends only that one', async ({ page }) => {
  const admin = await apiAs();
  const created = await raw(admin, 'POST', '/api/users', { ...USER, role: 'viewer', language: 'en', email: null });
  expect([201, 409]).toContain(created.status);

  // the "phone": its own cookie jar and User-Agent
  const phoneCtx = await pwRequest.newContext({ baseURL: BASE_URL, extraHTTPHeaders: { 'User-Agent': IPHONE } });
  const phone = new Api(phoneCtx);
  await phone.post('/api/auth/login', USER);
  await phone.get('/api/auth/me');

  await loginViaApi(page, USER, '/account');
  const card = page.getByTestId('account-sessions');
  const rows = card.getByTestId('session-row');
  await expect(rows).toHaveCount(2, { timeout: 20_000 });
  const mine = rows.filter({ has: page.getByTestId('session-current') });
  await expect(mine).toHaveCount(1);
  await expect(mine.getByTestId('session-current')).toHaveText(T('account.thisBrowser'));
  // the browser and system of this context's User-Agent (the Playwright config may set one)
  const ua: string = await page.evaluate(() => navigator.userAgent);
  const os = /Windows/.test(ua) ? 'Windows' : /Mac OS X/.test(ua) ? 'macOS' : 'Linux';
  await expect(mine).toContainText(`Chrome · ${os}`);
  const other = rows.filter({ hasText: 'Safari · iPhone' });
  await expect(other).toHaveCount(1);
  await expect(other).toContainText('127.0.0.1');
  // the existing actions stay
  await expect(page.getByTestId('logout-here')).toBeVisible();
  await expect(page.getByTestId('logout-everywhere')).toBeVisible();
  await shot(page, '85-account-sessions');

  // the API agrees: newest activity first, exactly one current row
  const list = await raw(new Api(page.request), 'GET', '/api/auth/sessions');
  expect(list.status).toBe(200);
  expect(list.body.items.filter((s: any) => s.current)).toHaveLength(1);

  await other.getByTestId('session-signout').click();
  await expect(rows).toHaveCount(1, { timeout: 15_000 });
  await expect(rows.first().getByTestId('session-current')).toBeVisible();
  expect((await raw(phone, 'GET', '/api/auth/me')).status, 'the phone is signed out').toBe(401);
  expect((await page.request.get('/api/auth/me')).status(), 'this browser stays signed in').toBe(200);

  // the row of this browser signs out like "Sign out"
  await rows.first().getByTestId('session-signout').click();
  await expect(page).toHaveURL(/\/login$/, { timeout: 15_000 });
  expect((await page.request.get('/api/auth/me')).status()).toBe(401);
  await phoneCtx.dispose();
});
