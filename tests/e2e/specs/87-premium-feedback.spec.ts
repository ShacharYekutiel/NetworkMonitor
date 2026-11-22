// Owner feedback on the premium design: select all + bulk Delete on the device's sensor table (api.md
// § Sensor bulk delete), movable / resizable table columns remembered per browser with "Reset columns",
// and a very long status message never hides the row's action buttons (actions pinned at the end edge).
import { expect, test, type Page } from '@playwright/test';
import { HTTP_TARGET_URL, VIEWER, apiAs, defaultGroupId, loginViaApi, uid, type Api } from '../lib/helpers';
import { ensureViewer, raw } from '../lib/lab';
import { featureOn } from '../lib/lab2b3';
import { T } from '../lib/lab2b2';

test.describe.configure({ mode: 'serial' });

let admin: Api;
let dev: { id: number; name: string };
let gone: { id: number; name: string }[] = [];
const since = new Date(Date.now() - 5000).toISOString();
const LONG_KW = `qa86-${'very-long-maintenance-banner-text-'.repeat(8)}end`;

const sensorTable = (page: Page) => page.getByTestId('sensor-table');
const head = (page: Page, col: string) => sensorTable(page).locator(`thead th[data-col="${col}"]`);
const heads = async (page: Page) =>
  sensorTable(page).locator('thead th').evaluateAll((ths) => ths.map((th) => th.getAttribute('data-col')));

test.beforeAll(async () => {
  admin = await apiAs();
  await ensureViewer(admin);
  await featureOn(admin, 'audit');
  const gid = await defaultGroupId(admin);
  dev = await admin.post('/api/devices', { group_id: gid, name: uid('qa86-dev'), host: '127.0.0.1', auto_sensors: [] });
  for (const n of ['qa86 alpha', 'qa86 beta', 'qa86 gamma']) {
    gone.push(await admin.post('/api/sensors', { device_id: dev.id, type: 'ping', name: n, interval_s: 60, config: { count: 1 } }));
  }
});

test.afterAll(async () => {
  if (dev) await raw(admin, 'DELETE', `/api/devices/${dev.id}`);
});

test('viewer: no checkboxes, no bulk bar; the API refuses bulk-delete', async ({ page }) => {
  await loginViaApi(page, VIEWER, `/devices/${dev.id}?tab=sensors`);
  await expect(sensorTable(page).getByRole('link', { name: 'qa86 alpha' })).toBeVisible();
  await expect(page.getByTestId('sensor-select-all')).toHaveCount(0);
  await expect(page.getByTestId('sensor-select')).toHaveCount(0);
  await expect(page.getByTestId('sensor-bulk-bar')).toHaveCount(0);
  const viewer = await apiAs(VIEWER);
  expect((await raw(viewer, 'POST', '/api/sensors/bulk-delete', { sensor_ids: [gone[0].id] })).status).toBe(403);
  // unknown id: 422, nothing deleted (as bulk-impact)
  const bad = await raw(admin, 'POST', '/api/sensors/bulk-delete', { sensor_ids: [gone[0].id, 99999999] });
  expect(bad.status).toBe(422);
  expect(bad.text).toContain('99999999');
  expect((await admin.get(`/api/sensors?device_id=${dev.id}`)).items).toHaveLength(3);
});

test('select all -> Delete…: the sheet names every sensor and the history; sensors gone, one audit row each', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${dev.id}?tab=sensors`);
  const all = page.getByTestId('sensor-select-all');
  await expect(all).toHaveAccessibleName(T('common.selectAll'));
  await page.getByTestId('sensor-select').first().check();
  expect(await all.evaluate((e) => (e as HTMLInputElement).indeterminate)).toBe(true);
  await all.check();
  const bar = page.getByTestId('sensor-bulk-bar');
  await expect(bar).toContainText(T('impact.selected', { count: 3 }));
  await bar.getByTestId('sensor-bulk-delete').click();
  const sheet = page.getByTestId('bulk-delete');
  await expect(sheet.getByRole('heading')).toHaveText(T('sensorBulk.deleteTitle', { count: 3 }));
  await expect(sheet).toContainText(T('sensorBulk.deleteConfirm', { count: 3 }));
  await expect(sheet.getByTestId('bulk-delete-list').getByRole('listitem')).toHaveText(['qa86 alpha', 'qa86 beta', 'qa86 gamma']);
  await sheet.getByTestId('bulk-delete-confirm').click();
  await expect(page.getByText(T('sensorBulk.deleted', { count: 3 }))).toBeVisible();
  await expect(sheet).toHaveCount(0);
  await expect(bar).toHaveCount(0);
  await expect(sensorTable(page)).toHaveCount(0);
  expect((await admin.get(`/api/sensors?device_id=${dev.id}`)).items).toHaveLength(0);
  for (const s of gone) expect((await raw(admin, 'GET', `/api/sensors/${s.id}`)).status).toBe(404);
  const a = await admin.get(`/api/audit?since=${encodeURIComponent(since)}&action=sensor.delete&page_size=200`);
  const ids = a.items.map((e: any) => e.entity_id);
  for (const s of gone) expect(ids).toContain(s.id);
});

test('columns: drag a header to move it, drag an edge to resize; both survive a reload; Reset columns', async ({ page }) => {
  for (const n of ['qa86 one', 'qa86 two']) {
    await admin.post('/api/sensors', { device_id: dev.id, type: 'ping', name: n, interval_s: 60, config: { count: 1 } });
  }
  await loginViaApi(page, undefined, `/devices/${dev.id}?tab=sensors`);
  await page.evaluate(() => localStorage.removeItem('netmon.table.sensors'));
  await page.reload();
  await expect(head(page, 'type')).toBeVisible();
  expect(await heads(page)).toEqual(['select', 'status', 'name', 'type', 'value', 'message', 'lastCheck', 'actions']);
  // move "Type" before "Name" with the mouse: a drop line shows while dragging
  const tb = (await head(page, 'type').boundingBox())!;
  const nb = (await head(page, 'name').boundingBox())!;
  await page.mouse.move(tb.x + tb.width / 2, tb.y + tb.height / 2);
  await page.mouse.down();
  await page.mouse.move(tb.x + tb.width / 2 - 20, tb.y + tb.height / 2, { steps: 3 });
  await page.mouse.move(nb.x + 8, nb.y + nb.height / 2, { steps: 6 });
  await expect(head(page, 'name')).toHaveAttribute('data-drop', 'before');
  await page.mouse.up();
  expect(await heads(page)).toEqual(['select', 'status', 'type', 'name', 'value', 'message', 'lastCheck', 'actions']);
  // widen "Value" by dragging its end edge
  const edge = head(page, 'value').getByTestId('col-resize');
  const w0 = (await head(page, 'value').boundingBox())!.width;
  const eb = (await edge.boundingBox())!;
  await page.mouse.move(eb.x + eb.width / 2, eb.y + eb.height / 2);
  await page.mouse.down();
  await page.mouse.move(eb.x + eb.width / 2 + 80, eb.y + eb.height / 2, { steps: 5 });
  await page.mouse.up();
  const w1 = (await head(page, 'value').boundingBox())!.width;
  expect(w1 - w0).toBeGreaterThan(60);
  // keyboard: the focused edge takes the arrow keys
  await edge.focus();
  await page.keyboard.press('ArrowRight');
  await expect.poll(async () => (await head(page, 'value').boundingBox())!.width).toBeGreaterThan(w1 + 10);
  const stored = JSON.parse((await page.evaluate(() => localStorage.getItem('netmon.table.sensors'))) ?? 'null');
  expect(stored.order.slice(0, 2)).toEqual(['status', 'type']);
  expect(stored.widths.value).toBeGreaterThan(w0 + 60);
  await page.reload();
  await expect(head(page, 'type')).toBeVisible();
  expect(await heads(page)).toEqual(['select', 'status', 'type', 'name', 'value', 'message', 'lastCheck', 'actions']);
  expect((await head(page, 'value').boundingBox())!.width).toBeGreaterThan(w0 + 60);
  // the popover moves columns without a mouse and resets everything
  await page.getByRole('button', { name: T('tableCols.columns') }).click();
  const pop = page.getByTestId('sensor-columns');
  await pop.getByRole('button', { name: T('tableCols.moveDown', { name: T('sensors.status') }) }).click();
  expect(await heads(page)).toEqual(['select', 'type', 'status', 'name', 'value', 'message', 'lastCheck', 'actions']);
  await pop.getByTestId('columns-reset').click();
  expect(await heads(page)).toEqual(['select', 'status', 'name', 'type', 'value', 'message', 'lastCheck', 'actions']);
  expect(await page.evaluate(() => localStorage.getItem('netmon.table.sensors'))).toBeNull();
  await page.keyboard.press('Escape');
  await expect(pop).toHaveCount(0);
});

test('a very long status message never hides the action buttons (1280 and 1440 wide, no page scroll)', async ({ page }) => {
  const s = await admin.post('/api/sensors', {
    device_id: dev.id,
    type: 'http',
    name: 'qa86 long message',
    interval_s: 10,
    config: { url: `${HTTP_TARGET_URL}/health`, keyword: LONG_KW },
  });
  await expect
    .poll(async () => (await admin.get(`/api/sensors/${s.id}`)).status_message ?? '', { timeout: 60_000 })
    .toContain(LONG_KW.slice(0, 40));
  for (const width of [1280, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await loginViaApi(page, undefined, `/devices/${dev.id}?tab=sensors`);
    const row = sensorTable(page).getByRole('row').filter({ has: page.getByRole('link', { name: 'qa86 long message' }) });
    await expect(row.getByTestId('sensor-message')).toContainText(LONG_KW.slice(0, 20));
    // the full text is on the cell's title; the cell itself is one truncated line
    expect(await row.getByTestId('sensor-message').getAttribute('title')).toContain(LONG_KW);
    for (const name of [T('sensors.runNow'), T('sensors.pause')]) {
      const btn = row.getByRole('button', { name, exact: true });
      await expect(btn).toBeVisible();
      const b = (await btn.boundingBox())!;
      expect(b.x, `${name} at ${width}`).toBeGreaterThanOrEqual(0);
      expect(b.x + b.width, `${name} inside the viewport at ${width}`).toBeLessThanOrEqual(width);
      // and inside the table's visible box (not scrolled away)
      const box = (await page.getByTestId('sensor-table').locator('..').boundingBox())!;
      expect(b.x + b.width).toBeLessThanOrEqual(box.x + box.width + 1);
    }
    const doc = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
    expect(doc[0], `no page overflow at ${width}`).toBeLessThanOrEqual(doc[1]);
  }
});
