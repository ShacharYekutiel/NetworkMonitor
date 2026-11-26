import { expect, test, type Page } from '@playwright/test';
import { ADMIN, Api, apiAs, loginViaApi, menuNav, uid, VIEWER } from '../lib/helpers';
import { ensureViewer } from '../lib/lab';
import { shot2b } from '../lib/lab2b';

// Topology map default arrangement (owner report: "I opened the map and everything was one on top of
// the other"). A map of a new group: 24 unlinked devices in two subgroups (no LLDP/CDP yet) plus a
// small linked core / distribution / access tree. Checks: no two node centres closer than a tile at the
// fitted zoom, everything inside the canvas (fit), drag -> saved -> reload keeps it (admin), a viewer
// moves nodes only in their view, "Arrange automatically" (by group) replaces the arrangement, piles
// saved on one spot are spread, "Fit to view" after zooming, Hebrew RTL with the menu pinned.
test.describe.configure({ mode: 'serial' });

type Probe = { zoom: number; tile: number; nodes: { key: string; x: number; y: number; pos: [number, number] }[] };

let api: Api;
let groupId = 0;
const subGroups: number[] = [];
const keys: string[] = [];
const unlinked: string[] = [];

async function openMap(page: Page, user = ADMIN) {
  await page.addInitScript((g) => localStorage.setItem('netmon.topology.group', JSON.stringify(g)), groupId);
  await loginViaApi(page, user, '/topology');
  await expect(page.locator('main canvas').first()).toBeVisible();
  await expect.poll(async () => (await probe(page))?.nodes.length ?? 0, { timeout: 15_000 }).toBe(keys.length);
}

const probe = (page: Page) => page.evaluate(() => (window as any).__nmTopology?.() as Probe | null);

/** Chart container box in page coordinates. */
const chartBox = async (page: Page) => (await page.locator('main div[dir="ltr"][role="img"]').first().boundingBox())!;

function assertNoPile(p: Probe) {
  const close: string[] = [];
  for (let i = 0; i < p.nodes.length; i++)
    for (let j = i + 1; j < p.nodes.length; j++) {
      const a = p.nodes[i];
      const b = p.nodes[j];
      if (Math.hypot(a.x - b.x, a.y - b.y) < p.tile) close.push(`${a.key}~${b.key}`);
    }
  expect(close, `node centres closer than a tile (${p.tile.toFixed(1)}px)`).toEqual([]);
}

async function assertInside(page: Page, p: Probe) {
  const box = await chartBox(page);
  for (const n of p.nodes) {
    expect(n.x, n.key).toBeGreaterThan(0);
    expect(n.y, n.key).toBeGreaterThan(0);
    expect(n.x, n.key).toBeLessThan(box.width);
    expect(n.y, n.key).toBeLessThan(box.height);
  }
}

const savedPositions = async () => {
  const g = await api.get(`/api/topology?group_id=${groupId}`);
  return Object.fromEntries(g.nodes.map((n: any) => [n.key, n.x === null ? null : [n.x, n.y]]));
};

test.beforeAll(async () => {
  api = await apiAs();
  await ensureViewer(api);
  groupId = (await api.post('/api/groups', { name: uid('QA topo layout'), parent_id: null })).id;
  for (const name of ['Floor A', 'Floor B'])
    subGroups.push((await api.post('/api/groups', { name: uid(name), parent_id: groupId })).id);
  let ip = 1;
  const dev = async (g: number, name: string) => {
    const d = await api.post('/api/devices', { group_id: g, name, host: `127.0.89.${ip++}`, auto_sensors: [] });
    keys.push(`d:${d.id}`);
    return d.id as number;
  };
  for (let i = 0; i < 24; i++) unlinked.push(`d:${await dev(subGroups[i % 2], uid(`lay-sw-${String(i + 1).padStart(2, '0')}`))}`);
  const core = await dev(groupId, uid('lay-core'));
  const link = (a: number, b: number) =>
    api.post('/api/topology/links', { a_device_id: a, a_if_index: null, a_port: `to-${b}`, b_device_id: b, b_neighbor_id: null, b_if_index: null, b_port: `to-${a}` });
  for (let d = 0; d < 2; d++) {
    const dist = await dev(groupId, uid(`lay-dist-${d + 1}`));
    await link(core, dist);
    for (let a = 0; a < 3; a++) await link(dist, await dev(groupId, uid(`lay-acc-${d + 1}${a + 1}`)));
  }
});

test.afterAll(async () => {
  if (!groupId) return;
  for (const k of keys) await api.del(`/api/devices/${k.slice(2)}`).catch(() => undefined);
  for (const g of subGroups) await api.del(`/api/groups/${g}`).catch(() => undefined);
  await api.del(`/api/groups/${groupId}`).catch(() => undefined);
  await api.patch('/api/auth/me', { language: 'en' }).catch(() => undefined);
});

test('first open: a default arrangement without piles, fitted into the canvas and saved (admin)', async ({ page }) => {
  await openMap(page);
  const p = (await probe(page))!;
  assertNoPile(p);
  await assertInside(page, p);
  // the admin's first render saves the arrangement for everybody
  await expect.poll(async () => Object.values(await savedPositions()).filter((x) => x === null).length, { timeout: 15_000 }).toBe(0);
  // the unlinked devices sit in rows (a grid), not on one spot
  const rows = new Set(p.nodes.filter((n) => unlinked.includes(n.key)).map((n) => Math.round(n.pos[1])));
  expect(rows.size).toBeGreaterThan(1);
  expect(rows.size).toBeLessThan(24);
  await expect(page.getByTestId('topology-drag-hint')).toHaveText('Drag a device to move it; the arrangement is saved for everyone.');
  await expect(page.getByRole('button', { name: 'Fit to view' })).toBeVisible();
  await shot2b(page, 'topology-layout-default-en');
});

test('drag a device -> saved (PUT layout) -> reload keeps the position', async ({ page }) => {
  await openMap(page);
  const p = (await probe(page))!;
  const n = p.nodes.find((x) => x.key === unlinked[0])!;
  const box = await chartBox(page);
  const put = page.waitForResponse((r) => r.url().endsWith('/api/topology/layout') && r.request().method() === 'PUT');
  await page.mouse.move(box.x + n.x, box.y + n.y);
  await page.mouse.down();
  await page.mouse.move(box.x + n.x + 20, box.y + n.y + 30, { steps: 4 });
  await page.mouse.move(box.x + n.x + 40, box.y + n.y + 60, { steps: 4 });
  await page.mouse.up();
  expect((await put).status()).toBe(204);
  const moved = (await probe(page))!.nodes.find((x) => x.key === n.key)!;
  expect(Math.hypot(moved.pos[0] - n.pos[0], moved.pos[1] - n.pos[1])).toBeGreaterThan(30);
  await expect.poll(async () => (await savedPositions())[n.key]).toEqual(moved.pos);
  await page.reload();
  await expect.poll(async () => (await probe(page))?.nodes.find((x) => x.key === n.key)?.pos).toEqual(moved.pos);
});

// Regression (full-suite flake of the test above): on a busy system every sensor result changes the
// tree, and re-applying the chart mid-drag put the node back, so nothing was saved. Here a live
// change (a group renamed -> the tree is fetched again) lands while the node is held.
test('a live update while a node is held does not cancel the drag: it is saved', async ({ page }) => {
  await openMap(page);
  const p = (await probe(page))!;
  const n = p.nodes.find((x) => x.key === unlinked[3])!;
  const box = await chartBox(page);
  const put = page.waitForResponse((r) => r.url().endsWith('/api/topology/layout') && r.request().method() === 'PUT');
  await page.mouse.move(box.x + n.x, box.y + n.y);
  await page.mouse.down();
  await page.mouse.move(box.x + n.x + 20, box.y + n.y + 30, { steps: 4 });
  const tree = page.waitForResponse((r) => r.url().endsWith('/api/tree') && r.request().method() === 'GET');
  await api.patch(`/api/groups/${subGroups[0]}`, { name: uid('Floor A') });
  await tree;
  // let React render the new tree before the drag goes on
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  await page.mouse.move(box.x + n.x + 40, box.y + n.y + 60, { steps: 4 });
  await page.mouse.up();
  expect((await put).status()).toBe(204);
  const moved = (await probe(page))!.nodes.find((x) => x.key === n.key)!;
  expect(Math.hypot(moved.pos[0] - n.pos[0], moved.pos[1] - n.pos[1])).toBeGreaterThan(30);
  await expect.poll(async () => (await savedPositions())[n.key]).toEqual(moved.pos);
});

test('viewer: drags move the node in their view only (nothing saved); the hint says so', async ({ page }) => {
  const before = await savedPositions();
  await openMap(page, VIEWER);
  await expect(page.getByTestId('topology-drag-hint')).toHaveText(
    'Drag a device to move it in your view; only administrators can save the arrangement.',
  );
  await expect(page.getByRole('button', { name: 'Arrange automatically' })).toHaveCount(0);
  let puts = 0;
  page.on('request', (r) => {
    if (r.url().endsWith('/api/topology/layout') && r.method() !== 'GET') puts++;
  });
  const p = (await probe(page))!;
  const n = p.nodes.find((x) => x.key === unlinked[1])!;
  const box = await chartBox(page);
  await page.mouse.move(box.x + n.x, box.y + n.y);
  await page.mouse.down();
  await page.mouse.move(box.x + n.x + 25, box.y + n.y + 50, { steps: 6 });
  await page.mouse.up();
  await expect.poll(async () => (await probe(page))!.nodes.find((x) => x.key === n.key)!.pos).not.toEqual(n.pos);
  await page.waitForTimeout(500);
  expect(puts).toBe(0);
  expect(await savedPositions()).toEqual(before);
  await page.reload();
  await expect.poll(async () => (await probe(page))?.nodes.find((x) => x.key === n.key)?.pos).toEqual(before[n.key]);
});

test('devices saved on one spot (0/0) are spread on the next open and saved again', async ({ page }) => {
  await api.put('/api/topology/layout', { positions: unlinked.slice(0, 6).map((node) => ({ node, x: 0, y: 0 })) });
  await openMap(page);
  assertNoPile((await probe(page))!);
  await expect
    .poll(async () => {
      const s = await savedPositions();
      return new Set(unlinked.slice(0, 6).map((k) => JSON.stringify(s[k]))).size;
    }, { timeout: 15_000 })
    .toBe(6);
});

test('"Arrange automatically" (by group) replaces the arrangement for everybody; Fit to view after zooming', async ({ page }) => {
  // a deliberately bad arrangement: everything on one diagonal line, far apart
  await api.put('/api/topology/layout', { positions: keys.map((node, i) => ({ node, x: i * 900, y: i * 700 })) });
  await openMap(page);
  await page.getByRole('button', { name: 'Arrange automatically' }).click();
  const dlg = page.getByRole('dialog');
  await expect(dlg.getByText('Every device on this map gets a new position. This replaces the saved arrangement for everyone.')).toBeVisible();
  await dlg.getByRole('radio', { name: 'By group' }).check();
  await shot2b(page, 'topology-layout-arrange-dialog-en');
  const put = page.waitForResponse((r) => r.url().endsWith('/api/topology/layout') && r.request().method() === 'PUT');
  await dlg.getByRole('button', { name: 'Arrange', exact: true }).click();
  const body = (await put).request().postDataJSON();
  expect(body.positions.map((x: any) => x.node).sort()).toEqual([...keys].sort());
  await expect(dlg).toBeHidden();
  await expect(page.getByText('Map arranged.')).toBeVisible();
  const p = (await probe(page))!;
  assertNoPile(p);
  await assertInside(page, p);
  // by group: the two floors' boxes do not intersect
  const g = await api.get(`/api/topology?group_id=${groupId}`);
  const boxOf = (gid: number) => {
    const ps = g.nodes.filter((n: any) => n.group_id === gid).map((n: any) => [n.x, n.y]);
    return [Math.min(...ps.map((q: number[]) => q[0])), Math.min(...ps.map((q: number[]) => q[1])), Math.max(...ps.map((q: number[]) => q[0])), Math.max(...ps.map((q: number[]) => q[1]))];
  };
  const [a, b] = [boxOf(subGroups[0]), boxOf(subGroups[1])];
  expect(a[2] < b[0] || b[2] < a[0] || a[3] < b[1] || b[3] < a[1]).toBe(true);
  // zoom in with the wheel, then "Fit to view" brings the whole map back
  const box = await chartBox(page);
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let i = 0; i < 5; i++) await page.mouse.wheel(0, -300);
  await expect.poll(async () => (await probe(page))!.zoom).toBeGreaterThan(p.zoom * 1.5);
  await page.getByRole('button', { name: 'Fit to view' }).click();
  await expect.poll(async () => Math.abs((await probe(page))!.zoom - p.zoom)).toBeLessThan(0.01);
  await assertInside(page, (await probe(page))!);
  await shot2b(page, 'topology-layout-by-group-en');
});

test('Hebrew RTL with the menu pinned: no piles, inside the canvas, drag saves, hint in Hebrew', async ({ page }) => {
  await api.patch('/api/auth/me', { language: 'he' });
  try {
    await openMap(page);
    await expect(page.getByTestId('topology-drag-hint')).toHaveText('גררו התקן כדי להזיז אותו; הסידור נשמר לכל המשתמשים.');
    await menuNav(page, { pin: true });
    // the canvas narrows: the map is fitted again (the user did not zoom yet)
    await expect.poll(async () => {
      const p = (await probe(page))!;
      const box = await chartBox(page);
      return p.nodes.every((n) => n.x > 0 && n.x < box.width && n.y > 0 && n.y < box.height);
    }).toBe(true);
    const p = (await probe(page))!;
    assertNoPile(p);
    const n = p.nodes.find((x) => x.key === unlinked[2])!;
    const box = await chartBox(page);
    const put = page.waitForResponse((r) => r.url().endsWith('/api/topology/layout') && r.request().method() === 'PUT');
    await page.mouse.move(box.x + n.x, box.y + n.y);
    await page.mouse.down();
    await page.mouse.move(box.x + n.x - 30, box.y + n.y + 40, { steps: 6 });
    await page.mouse.up();
    expect((await put).status()).toBe(204);
    await shot2b(page, 'topology-layout-he-pinned');
  } finally {
    await api.patch('/api/auth/me', { language: 'en' });
  }
});
