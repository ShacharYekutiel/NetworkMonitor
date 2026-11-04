import { expect, test } from '@playwright/test';
import { ADMIN, loginViaUi } from '../lib/helpers';

// Scenario (a): setup wizard creates the admin and lands on the dashboard; logout/login; wrong password.
test.describe.configure({ mode: 'serial' });

test('setup wizard creates the admin and lands on the dashboard', async ({ page, request }) => {
  const status = await (await request.get('/api/setup/status')).json();
  test.skip(!status.setup_required, 'needs a fresh database (run via tests/e2e/run.sh)');

  await page.goto('/');
  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByRole('heading', { name: 'Welcome to NetworkMonitor' })).toBeVisible();

  // Step 1: language
  await expect(page.getByLabel(/Interface and notification language/)).toHaveValue('en');
  await page.getByRole('button', { name: 'Next' }).click();

  // Step 2: admin account. The submit button stays disabled until the passwords match.
  await page.getByLabel(/Administrator username/).fill(ADMIN.username);
  await page.getByLabel(/^Password/).fill(ADMIN.password);
  await page.getByLabel(/Confirm password/).fill('mismatch-123');
  await expect(page.getByText('The passwords do not match.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create administrator' })).toBeDisabled();
  await page.getByLabel(/Confirm password/).fill(ADMIN.password);
  await page.getByRole('button', { name: 'Create administrator' }).click();

  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();
  // The Default group is created by setup and shows in the tree.
  await expect(page.locator('aside').getByText('Default', { exact: true })).toBeVisible();
  // Live connection indicator turns "Live" once the WebSocket is open.
  await expect(page.getByTestId('ws-indicator')).toContainText('Live');

  // The setup endpoint is closed now.
  const again = await request.post('/api/setup', {
    data: { username: 'x-admin', password: 'whatever-123' },
  });
  expect(again.status()).toBe(409);
  expect((await again.json()).error.code).toBe('setup_already_done');
  // And the wizard route redirects into the app.
  await page.goto('/setup');
  await expect(page).toHaveURL(/\/$/);
});

test('logout, then login again', async ({ page }) => {
  await loginViaUi(page);
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();

  await page.getByRole('button', { name: new RegExp(ADMIN.username) }).click();
  await page.getByRole('menuitem', { name: 'Sign out' }).click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();

  // The session cookie is really gone: the API says 401 and a deep link goes back to login.
  const me = await page.request.get('/api/auth/me');
  expect(me.status()).toBe(401);
  await page.goto('/events');
  await expect(page).toHaveURL(/\/login$/);

  // After login the SPA returns to the deep link it came from.
  await page.getByLabel('Username').fill(ADMIN.username);
  await page.getByLabel('Password').fill(ADMIN.password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page).toHaveURL(/\/events$/);
  await expect(page.getByRole('heading', { name: 'Events' })).toBeVisible();
});

test('wrong password shows an error and stays on login', async ({ page }) => {
  await page.goto('/login');
  await page.getByLabel('Username').fill(ADMIN.username);
  await page.getByLabel('Password').fill('definitely-wrong');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('alert')).toContainText('Wrong username or password.');
  await expect(page).toHaveURL(/\/login$/);
  const me = await page.request.get('/api/auth/me');
  expect(me.status()).toBe(401);
});
