// [tree-move] Moving devices and groups between groups (owner request, always on): drag a device onto
// another group (target highlighted, confirmation first), cancel keeps it in place, multi-select then
// "Move to…", a group moves with its content, a group cannot go into its own child, a viewer gets no move
// options (and the API refuses), the keyboard path in the ☰ sheet, Hebrew RTL drag, and right click / click /
// expand still work afterwards. Screenshots (en + he) go to $MOVE_SHOTS.
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Locator, type Page } from '@playwright/test';
import { SCREENSHOT_DIR, VIEWER, apiAs, loginViaApi, showTree, treeDevice, uid, type Api } from '../lib/helpers';
import { ensureViewer, raw } from '../lib/lab';

test.describe.configure({ mode: 'serial' });

const SHOTS = process.env.MOVE_SHOTS ?? join(SCREENSHOT_DIR, 'tree-move');

let admin: Api;
// top-level P (sorts first) ─ A ─ A1 (devA1) ; P ─ B ; devices d1, d2, d3 in A
let P: number;
let A: number;
let A1: number;
let B: number;
const dev: Record<string, { id: number; name: string }> = {};

const treeGroup = (page: Page, id: number) => page.locator(`aside a[href="/groups/${id}"]`);
const dropZone = (page: Page, id: number) => page.locator(`aside [data-drop-target="g${id}"]`);
const confirmDlg = (page: Page) => page.getByTestId('move-confirm');
const menu = (page: Page) => page.getByRole('menu');

async function groupOf(id: number): Promise<number> {
  return (await admin.get(`/api/devices/${id}`)).group_id;
}
async function parentOf(id: number): Promise<number | null> {
  return (await admin.get(`/api/groups/${id}`)).parent_id;
}

async function shot(page: Page, name: string) {
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: join(SHOTS, `${name}.png`) });
}

/** Starts a native drag of `src` and holds it over `dst` (the caller drops or cancels). */
async function dragOver(page: Page, src: Locator, dst: Locator) {
  await src.scrollIntoViewIfNeeded();
  await src.hover();
  await page.mouse.down();
  const s = (await src.boundingBox())!;
  await page.mouse.move(s.x + s.width / 2, s.y + s.height / 2 + 6, { steps: 3 });
  const b = (await dst.boundingBox())!;
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 10 });
}

test.beforeAll(async () => {
  admin = await apiAs();
  await ensureViewer(admin);
  const tag = uid('0qa91');
  P = (await admin.post('/api/groups', { name: tag, parent_id: null, sort_order: -1000 })).id;
  A = (await admin.post('/api/groups', { name: 'qa91 Alpha', parent_id: P })).id;
  A1 = (await admin.post('/api/groups', { name: 'qa91 Alpha child', parent_id: A })).id;
  B = (await admin.post('/api/groups', { name: 'qa91 Beta', parent_id: P })).id;
  for (const [k, g] of [
    ['d1', A],
    ['d2', A],
    ['d3', A],
    ['a1', A1],
  ] as const) {
    dev[k] = await admin.post('/api/devices', { group_id: g, name: uid(`qa91-${k}`), host: '127.0.0.1', auto_sensors: [] });
  }
});

test.afterAll(async () => {
  if (P) await raw(admin, 'DELETE', `/api/groups/${P}`);
});

test('drag a device onto another group: target highlighted, confirmation, then it moves', async ({ page }) => {
  await loginViaApi(page, undefined, '/');
  await showTree(page);
  const src = treeDevice(page, dev.d1.id);
  await expect(src).toHaveAttribute('draggable', 'true');
  await dragOver(page, src, treeGroup(page, B));
  await expect(dropZone(page, B)).toHaveAttribute('data-drop', 'ok');
  await shot(page, 'en-1-drag-highlight');
  await page.mouse.up();
  const dlg = confirmDlg(page);
  await expect(dlg).toBeVisible();
  await expect(dlg.getByRole('heading')).toHaveText('Move 1 device to qa91 Beta?');
  await expect(dlg.getByTestId('move-confirm-items')).toContainText(dev.d1.name);
  await expect(dlg.getByTestId('move-confirm-hint')).toContainText('inherited from the group');
  expect(await groupOf(dev.d1.id)).toBe(A); // nothing moved yet
  await shot(page, 'en-2-confirm');
  await dlg.getByRole('button', { name: 'Move', exact: true }).click();
  await expect(dlg).toHaveCount(0);
  await expect(page.getByText('Moved to qa91 Beta')).toBeVisible();
  await expect.poll(() => groupOf(dev.d1.id)).toBe(B);
  // the tree follows: the device is now listed under Beta
  await expect(dropZone(page, B).locator('xpath=..').locator(`a[href="/devices/${dev.d1.id}"]`)).toBeVisible();
});

test('cancel leaves the device where it was', async ({ page }) => {
  await loginViaApi(page, undefined, '/');
  await showTree(page);
  await dragOver(page, treeDevice(page, dev.d2.id), treeGroup(page, B));
  await expect(dropZone(page, B)).toHaveAttribute('data-drop', 'ok');
  await page.mouse.up();
  await expect(confirmDlg(page)).toBeVisible();
  await confirmDlg(page).getByRole('button', { name: 'Cancel' }).click();
  await expect(confirmDlg(page)).toHaveCount(0);
  expect(await groupOf(dev.d2.id)).toBe(A);
  await expect(treeDevice(page, dev.d2.id)).toBeVisible();
});

test('multi-select (Ctrl-click, Shift-click) then "Move to…"', async ({ page }) => {
  await loginViaApi(page, undefined, '/');
  await showTree(page);
  await treeDevice(page, dev.d2.id).click({ modifiers: ['ControlOrMeta'] });
  await treeDevice(page, dev.d3.id).click({ modifiers: ['ControlOrMeta'] });
  await expect(page).toHaveURL(/\/$/); // selecting does not navigate
  await expect(treeDevice(page, dev.d2.id)).toHaveAttribute('data-selected', 'true');
  const bar = page.getByTestId('tree-selection-bar');
  await expect(bar).toContainText('2 selected');
  // right click on a selected row offers the whole selection
  await treeDevice(page, dev.d3.id).click({ button: 'right' });
  await expect(menu(page).getByRole('menuitem', { name: 'Move 2 selected items to…' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(menu(page)).toHaveCount(0);
  await bar.getByTestId('tree-selection-move').click();
  const picker = page.getByTestId('move-picker');
  await expect(picker).toBeVisible();
  await expect(picker.getByRole('heading')).toHaveText('Move 2 devices to…');
  await expect(picker.getByLabel('qa91 Alpha', { exact: true })).toBeDisabled(); // current location
  await picker.getByRole('textbox').fill('qa91 Beta');
  await picker.getByLabel('qa91 Beta').check();
  await shot(page, 'en-3-multiselect-move-to');
  await picker.getByTestId('move-picker-next').click();
  await expect(confirmDlg(page).getByRole('heading')).toHaveText('Move 2 devices to qa91 Beta?');
  await confirmDlg(page).getByRole('button', { name: 'Move', exact: true }).click();
  await expect(confirmDlg(page)).toHaveCount(0);
  await expect.poll(() => groupOf(dev.d2.id)).toBe(B);
  expect(await groupOf(dev.d3.id)).toBe(B);
  await expect(bar).toHaveCount(0); // the selection is cleared after the move
});

test('a group moves with its content; it cannot go into its own child', async ({ page }) => {
  await loginViaApi(page, undefined, '/');
  await showTree(page);
  // into its own child: refused (dimmed, no-drop) and nothing opens on release
  await dragOver(page, treeGroup(page, A), treeGroup(page, A1));
  await expect(dropZone(page, A1)).toHaveAttribute('data-drop', 'no');
  await page.mouse.up();
  await expect(confirmDlg(page)).toHaveCount(0);
  expect(await parentOf(A)).toBe(P);
  // the server refuses it too
  const r = await raw(admin, 'POST', '/api/tree/move', { group_ids: [A], target_group_id: A1 });
  expect(r.status).toBe(400);
  expect(r.body.error.code).toBe('group_cycle');
  // into Beta: Alpha takes its child group and that group's device along
  await dragOver(page, treeGroup(page, A), treeGroup(page, B));
  await expect(dropZone(page, B)).toHaveAttribute('data-drop', 'ok');
  await page.mouse.up();
  await expect(confirmDlg(page).getByRole('heading')).toHaveText('Move 1 group to qa91 Beta?');
  await confirmDlg(page).getByRole('button', { name: 'Move', exact: true }).click();
  await expect.poll(() => parentOf(A)).toBe(B);
  expect(await parentOf(A1)).toBe(A);
  expect(await groupOf(dev.a1.id)).toBe(A1);
  await expect(treeGroup(page, A1)).toBeVisible();
  // the audit log has the move
  const audit = await raw(admin, 'GET', `/api/audit?action=group.move&entity_id=${A}`);
  if (audit.status === 200) expect(audit.body.items[0].changes.parent_id).toEqual([P, B]);
});

test('keyboard only, in the ☰ sheet: Shift+F10 → Move to… → pick → confirm', async ({ page }) => {
  await loginViaApi(page, undefined, '/');
  await page.evaluate(() => localStorage.setItem('netmon.menu.pinned', '0'));
  await page.reload();
  await page.getByTestId('menu-button').click();
  const inv = page.locator('#nm-menu').getByRole('tab').nth(1);
  if ((await inv.getAttribute('aria-selected')) !== 'true') await inv.click();
  const row = treeDevice(page, dev.d3.id);
  await row.focus();
  await page.keyboard.press('Shift+F10');
  await expect(menu(page)).toBeVisible();
  await page.keyboard.press('End'); // Delete…
  await page.keyboard.press('ArrowUp'); // Edit…
  await page.keyboard.press('ArrowUp'); // Move to…
  await expect(menu(page).getByRole('menuitem', { name: 'Move to…' })).toBeFocused();
  await page.keyboard.press('Enter');
  const picker = page.getByTestId('move-picker');
  await expect(picker).toBeVisible();
  await page.keyboard.type('qa91 Alpha child');
  await page.keyboard.press('Tab'); // the (only) radio
  await expect(picker.getByLabel('qa91 Alpha child')).toBeFocused();
  await page.keyboard.press('Space');
  await page.keyboard.press('Enter');
  await expect(confirmDlg(page).getByRole('heading')).toHaveText('Move 1 device to qa91 Alpha child?');
  // Escape closes the dialog, not the menu sheet
  await page.keyboard.press('Escape');
  await expect(confirmDlg(page)).toHaveCount(0);
  await expect(page.locator('#nm-menu')).toHaveAttribute('data-open', 'true');
  expect(await groupOf(dev.d3.id)).toBe(B);
  await row.focus();
  await page.keyboard.press('Shift+F10');
  await page.keyboard.press('End');
  await page.keyboard.press('ArrowUp');
  await page.keyboard.press('ArrowUp');
  await expect(menu(page).getByRole('menuitem', { name: 'Move to…' })).toBeFocused();
  await page.keyboard.press('Enter');
  await page.keyboard.type('qa91 Alpha child');
  await page.keyboard.press('Tab');
  await page.keyboard.press('Enter'); // Enter on an enabled option picks it
  await expect(confirmDlg(page)).toBeVisible();
  await confirmDlg(page).getByRole('button', { name: 'Move', exact: true }).focus();
  await page.keyboard.press('Enter');
  await expect.poll(() => groupOf(dev.d3.id)).toBe(A1);
});

test('viewer: no drag, no selection, no "Move to…"; the API refuses', async ({ page }) => {
  await loginViaApi(page, VIEWER, '/');
  await showTree(page);
  const row = treeDevice(page, dev.d1.id);
  await expect(row).toBeVisible();
  await expect(row).not.toHaveAttribute('draggable', 'true');
  await row.click({ button: 'right' });
  await expect(menu(page)).toBeVisible();
  const labels = (await menu(page).getByRole('menuitem').allTextContents()).map((x) => x.trim());
  expect(labels.filter((l) => l.startsWith('Move'))).toEqual([]);
  await page.keyboard.press('Escape');
  await row.click({ modifiers: ['ControlOrMeta'] }).catch(() => undefined);
  await expect(page.getByTestId('tree-selection-bar')).toHaveCount(0);
  const viewer = await apiAs(VIEWER);
  const r = await raw(viewer, 'POST', '/api/tree/move', { device_ids: [dev.d1.id], target_group_id: A });
  expect(r.status).toBe(403);
  expect(await groupOf(dev.d1.id)).toBe(B);
  // no checkboxes on the group page either
  await page.goto(`/groups/${B}`);
  await expect(page.getByTestId('device-row').first()).toBeVisible();
  await expect(page.getByTestId('move-select')).toHaveCount(0);
});

test('group page: tick rows, "Move to…" the top level', async ({ page }) => {
  await loginViaApi(page, undefined, `/groups/${B}`);
  const sub = page.getByTestId('subgroup-row').filter({ hasText: 'qa91 Alpha' });
  await sub.getByTestId('move-select').check();
  await expect(page.getByTestId('group-selection-bar')).toContainText('1 selected');
  await page.getByTestId('group-selection-move').click();
  const picker = page.getByTestId('move-picker');
  await picker.getByLabel('Top level').check();
  await picker.getByTestId('move-picker-next').click();
  await expect(confirmDlg(page).getByRole('heading')).toHaveText('Move 1 group to the top level?');
  await confirmDlg(page).getByRole('button', { name: 'Move', exact: true }).click();
  await expect.poll(() => parentOf(A)).toBe(null);
  await expect(sub).toHaveCount(0);
  // and back under P through the API (the rest of the spec expects it there)
  await admin.post('/api/tree/move', { group_ids: [A], target_group_id: P });
});

test('Hebrew RTL (dark): drag works, the dialog reads in Hebrew', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'dark' });
  await loginViaApi(page, undefined, '/');
  await showTree(page);
  await page.request.patch('/api/auth/me', { data: { language: 'he' } });
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
  try {
    await dragOver(page, treeDevice(page, dev.d1.id), treeGroup(page, A));
    await expect(dropZone(page, A)).toHaveAttribute('data-drop', 'ok');
    await shot(page, 'he-1-drag-highlight');
    await page.mouse.up();
    const dlg = confirmDlg(page);
    await expect(dlg.getByRole('heading')).toHaveText('להעביר התקן אחד אל qa91 Alpha?');
    await expect(dlg.getByTestId('move-confirm-hint')).toContainText('בירושה מהקבוצה');
    await shot(page, 'he-2-confirm');
    await dlg.getByRole('button', { name: 'העברה', exact: true }).click();
    await expect.poll(() => groupOf(dev.d1.id)).toBe(A);
    // multi-select + "Move to…" in Hebrew (screenshot only; cancelled)
    await treeDevice(page, dev.d1.id).click({ modifiers: ['ControlOrMeta'] });
    await treeDevice(page, dev.d2.id).click({ modifiers: ['ControlOrMeta'] });
    await expect(page.getByTestId('tree-selection-bar')).toContainText('שני פריטים נבחרו');
    await page.getByTestId('tree-selection-move').click();
    await expect(page.getByTestId('move-picker').getByRole('heading')).toHaveText('העברת שני התקנים אל…');
    await page.getByTestId('move-picker').getByLabel('qa91 Alpha child').check();
    await shot(page, 'he-3-multiselect-move-to');
    await page.getByTestId('move-picker').getByRole('button', { name: 'ביטול' }).click();
    await page.keyboard.press('Escape'); // clears the selection
    await expect(page.getByTestId('tree-selection-bar')).toHaveCount(0);
  } finally {
    await page.request.patch('/api/auth/me', { data: { language: 'en' } });
  }
});

test('after moving: click, right click and expand / collapse still work', async ({ page }) => {
  await loginViaApi(page, undefined, '/');
  await showTree(page);
  const row = treeDevice(page, dev.d1.id);
  await row.click({ button: 'right' });
  await expect(menu(page)).toBeVisible();
  await expect(menu(page).getByRole('menuitem', { name: 'Open', exact: true })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(menu(page)).toHaveCount(0);
  // collapse / expand Alpha with its toggle
  const toggle = dropZone(page, A).getByRole('button', { name: /^(Collapse|Expand)$/ });
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await toggle.click();
  await expect(row).toHaveCount(0);
  await toggle.click();
  await expect(row).toBeVisible();
  // a collapsed group opens while a drag hovers it
  await toggle.click();
  await expect(treeGroup(page, A1)).toHaveCount(0);
  await dragOver(page, treeDevice(page, dev.d2.id), treeGroup(page, A));
  await expect(treeGroup(page, A1)).toBeVisible({ timeout: 5000 });
  await page.mouse.up(); // dropped on Alpha: the confirmation, cancelled
  await expect(confirmDlg(page)).toBeVisible();
  await confirmDlg(page).getByRole('button', { name: 'Cancel' }).click();
  expect(await groupOf(dev.d2.id)).toBe(B);
  // a plain click opens the device
  await row.click();
  await expect(page).toHaveURL(new RegExp(`/devices/${dev.d1.id}$`));
});
