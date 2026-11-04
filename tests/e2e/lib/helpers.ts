import { expect, request as pwRequest, type APIRequestContext, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

export const BASE_URL = process.env.BASE_URL ?? 'http://localhost:8080';
export const SNMP_PORT = Number(process.env.SNMP_PORT ?? 11161);
export const HTTP_TARGET_URL = process.env.HTTP_TARGET_URL ?? 'http://127.0.0.1:18081';
export const WEBHOOK_URL = process.env.WEBHOOK_URL ?? 'http://127.0.0.1:18082';
export const SCREENSHOT_DIR = process.env.SCREENSHOT_DIR ?? join(__dirname, '..', 'test-results', 'screenshots');

export const ADMIN = { username: 'admin', password: 'Admin-pass-1' };
export const VIEWER = { username: 'viewer1', password: 'Viewer-pass-1' };
/** Minimum allowed by the API (data-model: 10 <= interval_s). */
export const MIN_INTERVAL = 10;

export function uid(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}${Math.floor(Math.random() * 1000)}`;
}

export class Api {
  constructor(readonly ctx: APIRequestContext) {}

  async call<T = any>(method: string, path: string, body?: unknown, okStatus?: number[]): Promise<T> {
    const r = await this.ctx.fetch(path, {
      method,
      data: body === undefined ? undefined : body,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    });
    const ok = okStatus ? okStatus.includes(r.status()) : r.ok();
    const text = await r.text();
    if (!ok) throw new Error(`${method} ${path} -> ${r.status()} ${text}`);
    return (text ? JSON.parse(text) : null) as T;
  }
  get<T = any>(p: string) {
    return this.call<T>('GET', p);
  }
  post<T = any>(p: string, b?: unknown) {
    return this.call<T>('POST', p, b);
  }
  patch<T = any>(p: string, b: unknown) {
    return this.call<T>('PATCH', p, b);
  }
  put<T = any>(p: string, b: unknown) {
    return this.call<T>('PUT', p, b);
  }
  del(p: string) {
    return this.call('DELETE', p);
  }
}

/** An API client with its own cookie jar, logged in as the given user. */
export async function apiAs(user = ADMIN): Promise<Api> {
  const ctx = await pwRequest.newContext({ baseURL: BASE_URL });
  const api = new Api(ctx);
  await ensureSetup(api);
  await api.post('/api/auth/login', user);
  return api;
}

/**
 * When a spec runs on its own against a fresh DB (without 01-setup), create the admin through the
 * API so the precondition holds. In the full run the wizard in 01-setup has already done it.
 */
export async function ensureSetup(api: Api): Promise<void> {
  const s = await api.get<{ setup_required: boolean }>('/api/setup/status');
  if (s.setup_required) {
    await api.post('/api/setup', { ...ADMIN, email: null, language: 'en' });
  }
}

/** Logs the page's browser context in through the API (shares the cookie jar), then opens `path`. */
export async function loginViaApi(page: Page, user = ADMIN, path = '/'): Promise<void> {
  const r = await page.request.post('/api/auth/login', { data: user });
  expect(r.status(), await r.text()).toBe(200);
  await page.goto(path);
  await expect(page.getByRole('navigation').first()).toBeVisible();
}

export async function loginViaUi(page: Page, user = ADMIN): Promise<void> {
  await page.goto('/login');
  await page.getByLabel('Username').fill(user.username);
  await page.getByLabel('Password').fill(user.password);
  await page.getByRole('button', { name: 'Sign in' }).click();
}

/** Waits until GET /api/sensors/{id} satisfies `pred` (polling the real backend). */
export async function waitForSensor(
  api: Api,
  id: number,
  pred: (s: any) => boolean,
  timeout = 60_000,
  message = 'sensor condition',
): Promise<any> {
  let last: any = null;
  await expect
    .poll(
      async () => {
        last = await api.get(`/api/sensors/${id}`);
        return pred(last);
      },
      { timeout, intervals: [500, 1000, 2000], message },
    )
    .toBe(true);
  return last;
}

/** The tree row (<a href="/sensors/ID">) of a sensor in the side navigation. */
export function treeSensor(page: Page, id: number) {
  return page.locator(`aside a[href="/sensors/${id}"]`);
}
export function treeDevice(page: Page, id: number) {
  return page.locator(`aside a[href="/devices/${id}"]`);
}

/** Status dots carry aria-label = translated status ("Up", "Down", ...). */
export function statusDot(scope: ReturnType<Page['locator']>, label: string) {
  return scope.locator(`[aria-label="${label}"]`);
}

export async function webhookRecords(): Promise<any[]> {
  const r = await fetch(`${WEBHOOK_URL}/records`);
  return (await r.json()) as any[];
}
export async function clearWebhookRecords(): Promise<void> {
  await fetch(`${WEBHOOK_URL}/records`, { method: 'DELETE' });
}
export async function httpTarget(action: 'ok' | 'fail' | 'toggle' | 'state'): Promise<{ healthy: boolean }> {
  const r = await fetch(`${HTTP_TARGET_URL}/${action}`, { method: 'POST' });
  return (await r.json()) as { healthy: boolean };
}

export async function shot(page: Page, name: string): Promise<string> {
  mkdirSync(SCREENSHOT_DIR, { recursive: true });
  const path = join(SCREENSHOT_DIR, `${name}.png`);
  await page.screenshot({ path, fullPage: false });
  return path;
}

/** Marks the current document; if the page reloads/navigates hard, the marker is gone. */
export async function markDocument(page: Page): Promise<void> {
  await page.evaluate(() => ((window as any).__e2eNoReload = true));
}
export async function expectSameDocument(page: Page): Promise<void> {
  expect(await page.evaluate(() => (window as any).__e2eNoReload === true)).toBe(true);
}

/** Creates a group + device + sensors via the API as test preconditions. */
export async function createDevice(
  api: Api,
  body: Record<string, unknown>,
): Promise<{ id: number; group_id: number; name: string }> {
  return api.post('/api/devices', body);
}

export async function defaultGroupId(api: Api): Promise<number> {
  const tree = await api.get<{ groups: { id: number; name: string }[] }>('/api/tree');
  const g = tree.groups.find((x) => x.name === 'Default') ?? tree.groups[0];
  return g.id;
}

/** Devices are collapsed in the tree by default; expand one so its sensor rows render. */
export async function expandTreeDevice(page: Page, deviceId: number): Promise<void> {
  const row = page.locator(`aside a[href="/devices/${deviceId}"]`).locator('xpath=..');
  await expect(row).toBeVisible();
  const toggle = row.getByRole('button', { name: /^(Expand|Collapse|הרחב|כווץ)$/ }).first();
  if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click();
}

/** A Card (<section>) by its <h2> title. */
export function card(page: Page, title: string | RegExp) {
  return page.locator('section').filter({ has: page.getByRole('heading', { level: 2, name: title }) });
}

/**
 * A checkbox by its own visible label text. (Some forms nest checkbox <label>s inside a field
 * <label>, which pollutes the accessible name of the first checkbox - see 08-ui-checks.spec.ts.)
 */
export function checkboxByText(scope: ReturnType<Page['locator']>, text: string | RegExp) {
  const re = typeof text === 'string' ? new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`) : text;
  return scope
    .locator('label')
    .filter({ has: scope.page().locator('input[type=checkbox]') })
    .filter({ has: scope.page().locator('span', { hasText: re }) })
    .last()
    .locator('input[type=checkbox]')
    .last();
}
