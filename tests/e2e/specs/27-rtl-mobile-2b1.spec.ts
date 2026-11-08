import { expect, test, type Page } from '@playwright/test';
import { Api, apiAs, defaultGroupId, loginViaApi, uid } from '../lib/helpers';
import { horizontalOverflow, raw } from '../lib/lab';
import { httpDevice, shot2b, target, waitStatus } from '../lib/lab2b';

// [2b-1] (i) Hebrew RTL + phone width on the new pages: /maintenance, /topology, dashboard (acknowledged +
// unreachable cards), device page with dependency / maintenance badge. Screenshots en + he.
test.describe.configure({ mode: 'serial' });

let api: Api;
let dev: { device: any; sensor: any };
let winId = 0;
const IP = '127.0.0.35';

async function setLang(page: Page, lang: 'en' | 'he') {
  await page.request.patch('/api/auth/me', { data: { language: lang } });
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('dir', lang === 'he' ? 'rtl' : 'ltr');
}

test.beforeAll(async () => {
  api = await apiAs();
  await target(IP, 'fail');
  dev = await httpDevice(api, { name: uid('rtl-2b-dev'), ip: IP, group_id: await defaultGroupId(api) });
  await waitStatus(api, dev.sensor.id, 'down', 45_000);
  await api.post(`/api/sensors/${dev.sensor.id}/acknowledge`, { comment: 'החלפת ספק כוח' });
  winId = (
    await api.post('/api/maintenance-windows', {
      name: 'חלון תחזוקה QA',
      scope: 'device',
      device_id: dev.device.id,
      kind: 'once',
      starts_at: new Date(Date.now() - 60_000).toISOString(),
      ends_at: new Date(Date.now() + 3 * 3600_000).toISOString(),
      comment: 'עדכון קושחה',
    })
  ).id;
  await api.post('/api/maintenance-windows', { name: uid('Weekly RTL'), scope: 'global', kind: 'weekly', weekdays: [6, 7], start_time: '23:30', duration_min: 120, enabled: false });
});

test.afterAll(async () => {
  await target(IP, 'ok');
  await api.patch('/api/auth/me', { language: 'en' }).catch(() => undefined);
  const all = (await api.get('/api/maintenance-windows?page_size=1000')).items;
  for (const w of all.filter((x: any) => x.id === winId || /^Weekly RTL/.test(x.name))) await raw(api, 'DELETE', `/api/maintenance-windows/${w.id}`);
  if (dev) await raw(api, 'DELETE', `/api/devices/${dev.device.id}`);
});

const PAGES = [
  ['maintenance', '/maintenance'],
  ['topology', '/topology'],
  ['dashboard', '/'],
  ['device', ''],
] as const;

test('English screenshots of the new pages (desktop)', async ({ page }) => {
  await loginViaApi(page, undefined, '/');
  await setLang(page, 'en');
  for (const [name, path] of PAGES) {
    await page.goto(path || `/devices/${dev.device.id}`);
    await page.waitForLoadState('networkidle');
    await shot2b(page, `${name}-desktop-en`);
  }
});

test('Hebrew: dir=rtl, translated texts, LTR IPs; screenshots', async ({ page }) => {
  await loginViaApi(page, undefined, '/maintenance');
  await setLang(page, 'he');
  const nav = page.getByRole('navigation').first();
  // nav.maintenance / nav.map exist in Hebrew (not the English fallback).
  await expect(nav.getByRole('link', { name: 'Maintenance' })).toHaveCount(0);
  await expect(nav.getByRole('link', { name: 'Map' })).toHaveCount(0);
  await expect(page.getByText('חלון תחזוקה QA').first()).toBeVisible();
  await expect(page.locator('main')).not.toContainText(/Active now|Upcoming|Recurring|Next occurrence/);
  await shot2b(page, 'maintenance-desktop-he');

  await page.goto(`/devices/${dev.device.id}`);
  await expect(page.locator('main')).not.toContainText(/In maintenance until|Add maintenance window|Re-identify/);
  const ip = page.locator('main').getByText(IP).first();
  await expect(ip).toBeVisible();
  const dir = await ip.evaluate((el) => getComputedStyle(el).direction);
  expect(dir).toBe('ltr');
  await shot2b(page, 'device-desktop-he');

  await page.goto('/');
  await expect(page.locator('main')).not.toContainText(/Acknowledged problems|Unreachable devices|Down and warning sensors/);
  await expect(page.getByText('החלפת ספק כוח').first()).toBeAttached();
  await shot2b(page, 'dashboard-desktop-he');

  await page.goto('/topology');
  await expect(page.locator('main canvas').first()).toBeVisible();
  await expect(page.locator('main')).not.toContainText(/Refresh topology|Suggest dependencies|Include stale|Add link/);
  await page.waitForTimeout(1000);
  await shot2b(page, 'topology-desktop-he');

  await page.goto(`/sensors/${dev.sensor.id}`);
  await expect(page.locator('main')).not.toContainText(/Unacknowledge|Acknowledged by/);
  await shot2b(page, 'ack-sensor-desktop-he');
});

for (const lang of ['en', 'he'] as const) {
  test(`phone width (390 px) without horizontal overflow: ${lang}`, async ({ browser }) => {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
    const page = await ctx.newPage();
    await loginViaApi(page, undefined, '/');
    await setLang(page, lang);
    const bad: string[] = [];
    for (const [name, path] of PAGES) {
      await page.goto(path || `/devices/${dev.device.id}`);
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(500);
      const o = await horizontalOverflow(page);
      if (o.scrollWidth > o.clientWidth + 1) bad.push(`${name}: ${o.scrollWidth} > ${o.clientWidth}`);
      await shot2b(page, `${name}-phone-${lang}`);
    }
    // Maintenance drawer on the phone.
    await page.goto('/maintenance');
    await page.getByRole('main').getByRole('button', { name: lang === 'en' ? 'Add maintenance window' : 'הוספת חלון תחזוקה' }).first().click({ timeout: 5000 }).catch(() => undefined);
    const o = await horizontalOverflow(page);
    if (o.scrollWidth > o.clientWidth + 1) bad.push(`maintenance drawer: ${o.scrollWidth} > ${o.clientWidth}`);
    await shot2b(page, `maintenance-drawer-phone-${lang}`);
    expect(bad).toEqual([]);
    await ctx.close();
  });
}
