// [3b] Right-click context menu of the sidebar device tree (owner request, always on): admin actions on a
// device, viewer sees only safe actions, Copy IP, Pause / Resume through the menu, Shift+F10 opens it on the
// focused tree item, Esc closes it and focus returns to the item.
import { expect, test, type Page } from '@playwright/test';
import { BASE_URL, VIEWER, apiAs, defaultGroupId, loginViaApi, treeDevice, uid, type Api } from '../lib/helpers';
import { ensureViewer } from '../lib/lab';

test.describe.configure({ mode: 'serial' });

let admin: Api;
let dev: { id: number; name: string; host: string };

const ADMIN_ONLY = [
  'Add sensor…',
  'Apply template…',
  'Run all sensors now',
  'Maintenance window…',
  'Edit…',
  'Delete…',
];

test.beforeAll(async () => {
  admin = await apiAs();
  await ensureViewer(admin);
  const gid = await defaultGroupId(admin);
  dev = await admin.post('/api/devices', {
    group_id: gid,
    name: uid('ctx-dev'),
    host: '127.0.0.1',
    auto_sensors: ['ping'],
  });
});

test.afterAll(async () => {
  if (dev) await admin.del(`/api/devices/${dev.id}`).catch(() => undefined);
});

const menu = (page: Page) => page.getByRole('menu');
const items = async (page: Page) =>
  (await menu(page).getByRole('menuitem').allTextContents()).map((x) => x.trim());

test('admin: right click a device opens the menu at the pointer with the admin actions', async ({ page }) => {
  await loginViaApi(page, undefined, '/');
  const row = treeDevice(page, dev.id);
  await expect(row).toBeVisible();
  const box = (await row.boundingBox())!;
  await row.click({ button: 'right', position: { x: 20, y: box.height / 2 } });
  await expect(menu(page)).toBeVisible();
  await expect(menu(page)).toHaveAttribute('aria-label', `Device ${dev.name}`);
  const labels = await items(page);
  expect(labels.slice(0, 2)).toEqual(['Open', 'Open in new tab']);
  for (const l of [...ADMIN_ONLY, 'Pause', 'Copy IP address', 'Copy name']) expect(labels).toContain(l);
  // menu opens at the pointer (LTR: its left edge at the click point) and stays inside the viewport
  const m = (await menu(page).boundingBox())!;
  expect(Math.abs(m.x - (box.x + 20))).toBeLessThan(2);
  expect(m.y + m.height).toBeLessThanOrEqual(900);
  // Delete is styled as danger and confirmed
  await expect(menu(page).getByRole('menuitem', { name: 'Delete…' })).toHaveAttribute('data-danger', 'true');
  await menu(page).getByRole('menuitem', { name: 'Delete…' }).click();
  const dlg = page.getByRole('dialog');
  await expect(dlg).toBeVisible();
  await dlg.getByRole('button', { name: 'Cancel' }).click();
  await expect(treeDevice(page, dev.id)).toBeVisible();
  // the browser's own menu is only suppressed on tree items: outside, contextmenu is not prevented
  const prevented = await page.evaluate(() => {
    const ev = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
    document.querySelector('main')!.dispatchEvent(ev);
    return ev.defaultPrevented;
  });
  expect(prevented).toBe(false);
});

test('Copy IP puts the host on the clipboard', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: BASE_URL });
  await loginViaApi(page, undefined, '/');
  await treeDevice(page, dev.id).click({ button: 'right' });
  await menu(page).getByRole('menuitem', { name: 'Copy IP address' }).click();
  await expect(menu(page)).toHaveCount(0);
  await expect(page.getByText('Copied to clipboard')).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(dev.host);
});

test('Pause / Resume through the menu (tree and API follow)', async ({ page }) => {
  await loginViaApi(page, undefined, '/');
  const row = treeDevice(page, dev.id);
  await row.click({ button: 'right' });
  await menu(page).getByRole('menuitem', { name: 'Pause', exact: true }).click();
  await expect(page.getByText('Device paused')).toBeVisible();
  await expect.poll(async () => (await admin.get(`/api/devices/${dev.id}`)).enabled).toBe(false);
  // the menu now offers Resume (tree refreshed) and Run all is disabled
  await expect
    .poll(async () => {
      await row.click({ button: 'right' });
      const l = await items(page);
      await page.keyboard.press('Escape');
      return l.includes('Resume');
    })
    .toBe(true);
  await row.click({ button: 'right' });
  await expect(menu(page).getByRole('menuitem', { name: 'Run all sensors now' })).toHaveAttribute('aria-disabled', 'true');
  await menu(page).getByRole('menuitem', { name: 'Resume', exact: true }).click();
  await expect(page.getByText('Device resumed')).toBeVisible();
  await expect.poll(async () => (await admin.get(`/api/devices/${dev.id}`)).enabled).toBe(true);
});

test('keyboard: Shift+F10 opens it on the focused item, arrows move, Esc closes and focus returns', async ({ page }) => {
  await loginViaApi(page, undefined, '/');
  const row = treeDevice(page, dev.id);
  await row.focus();
  await page.keyboard.press('Shift+F10');
  await expect(menu(page)).toBeVisible();
  await expect(menu(page).getByRole('menuitem', { name: 'Open', exact: true })).toBeFocused();
  await page.keyboard.press('ArrowDown');
  await expect(menu(page).getByRole('menuitem', { name: 'Open in new tab' })).toBeFocused();
  await page.keyboard.press('End');
  await expect(menu(page).getByRole('menuitem', { name: 'Delete…' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(menu(page)).toHaveCount(0);
  await expect(row).toBeFocused();
  // Enter on "Open" navigates to the device
  await page.keyboard.press('Shift+F10');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(new RegExp(`/devices/${dev.id}$`));
  // outside click closes
  await row.click({ button: 'right' });
  await expect(menu(page)).toBeVisible();
  await page.mouse.click(900, 600);
  await expect(menu(page)).toHaveCount(0);
});

test('viewer: only the safe actions', async ({ page }) => {
  await loginViaApi(page, VIEWER, '/');
  await treeDevice(page, dev.id).click({ button: 'right' });
  await expect(menu(page)).toBeVisible();
  const labels = await items(page);
  expect(labels).toContain('Open');
  expect(labels).toContain('Copy IP address');
  expect(labels).toContain('Copy name');
  for (const l of [...ADMIN_ONLY, 'Pause', 'Resume', 'Explain with AI']) expect(labels).not.toContain(l);
  await page.keyboard.press('Escape');
  await expect(menu(page)).toHaveCount(0);
});

test('Hebrew: translated and mirrored (opens to the left of the pointer)', async ({ page }) => {
  await loginViaApi(page, undefined, '/');
  await page.request.patch('/api/auth/me', { data: { language: 'he' } });
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
  const row = treeDevice(page, dev.id);
  const box = (await row.boundingBox())!;
  const x = box.width - 20;
  await row.click({ button: 'right', position: { x, y: box.height / 2 } });
  await expect(menu(page)).toHaveAttribute('aria-label', `התקן ${dev.name}`);
  await expect(menu(page).getByRole('menuitem').first()).toHaveText('פתיחה');
  const m = (await menu(page).boundingBox())!;
  const vw = page.viewportSize()!.width;
  // its right edge at the pointer, or flipped to its right when there is no room (sidebar at the right in RTL)
  const px = box.x + x;
  expect(Math.abs(m.x + m.width - px) < 2 || Math.abs(m.x - px) < 2).toBe(true);
  expect(m.x).toBeGreaterThanOrEqual(0);
  expect(m.x + m.width).toBeLessThanOrEqual(vw);
  await page.keyboard.press('Escape');
  await page.request.patch('/api/auth/me', { data: { language: 'en' } });
});
