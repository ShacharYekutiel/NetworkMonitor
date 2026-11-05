import { expect, test } from '@playwright/test';
import { Api, apiAs, clearWebhookRecords, defaultGroupId, loginViaApi, expectSameDocument, markDocument, statusDot, treeDevice, uid } from '../lib/helpers';
import { raw } from '../lib/lab';
import {
  createWebhook,
  ensureChildGroup,
  flattenTree,
  hooks,
  hooksForSensors,
  httpDevice,
  labKill,
  labRevive,
  quiet,
  rule,
  shot2b,
  target,
  waitStatus,
} from '../lib/lab2b';

// [2b-1] (a) Dependencies (architecture.md §10.2/§10.4, data-model.md § Device dependencies):
// parent 127.0.0.31 (HTTP sensor, no ping -> "whole device" mode or explicit dependency sensor), children
// 127.0.0.32 (explicit dependency with sensor) and 127.0.0.33 (inherits from its group). Parent outage ->
// children unreachable (violet, live), only the parent alerts; parent up -> children re-checked -> up;
// child still down after the parent recovered -> unreachable -> down -> alerts. Cycles -> 400 dependency_cycle.
test.describe.configure({ mode: 'serial' });

let api: Api;
let parent: { device: any; sensor: any };
let child1: { device: any; sensor: any };
let child2: { device: any; sensor: any };
let branchGroup = 0;
const HOOK = `/dep-${Date.now().toString(36)}`;
const parentName = uid('dep-parent-gw');
const child1Name = uid('dep-child-1');
const child2Name = uid('dep-child-2');
const rules: number[] = [];

test.beforeAll(async () => {
  api = await apiAs();
  for (const ip of ['127.0.0.32', '127.0.0.33']) await target(ip, 'ok');
  labRevive('http-31');
  const root = await defaultGroupId(api);
  parent = await httpDevice(api, { name: parentName, ip: '127.0.0.31', group_id: root });
  child1 = await httpDevice(api, {
    name: child1Name,
    ip: '127.0.0.32',
    group_id: root,
    dependency: { mode: 'device', device_id: parent.device.id, sensor_id: parent.sensor.id },
  });
  branchGroup = await ensureChildGroup(api, uid('QA dep branch'), root);
  await api.patch(`/api/groups/${branchGroup}`, { dependency: { mode: 'device', device_id: parent.device.id, sensor_id: null } });
  child2 = await httpDevice(api, { name: child2Name, ip: '127.0.0.33', group_id: branchGroup });
  const ch = await createWebhook(api, HOOK);
  // Children rules get delay_s 20 (> parent interval 10 s), as the owner notes recommend (§10.5).
  rules.push((await rule(api, { sensor_id: parent.sensor.id, channel_ids: [ch], delay_s: 0 })).id);
  rules.push((await rule(api, { sensor_id: child1.sensor.id, channel_ids: [ch], delay_s: 20 })).id);
  rules.push((await rule(api, { sensor_id: child2.sensor.id, channel_ids: [ch], delay_s: 20 })).id);
  for (const x of [parent, child1, child2]) await waitStatus(api, x.sensor.id, 'up', 45_000);
});

test.afterAll(async () => {
  labRevive('http-31');
  for (const ip of ['127.0.0.32', '127.0.0.33']) await target(ip, 'ok').catch(() => undefined);
  for (const id of rules) await raw(api, 'DELETE', `/api/alert-rules/${id}`);
});

test('API: dependency + effective_dependency shapes (explicit, group-inherited, whole-device mode)', async () => {
  const c1 = await api.get(`/api/devices/${child1.device.id}`);
  expect(c1.dependency).toMatchObject({ mode: 'device', device_id: parent.device.id, device_name: parentName, sensor_id: parent.sensor.id, sensor_name: 'Web' });
  expect(c1.effective_dependency).toMatchObject({ device_id: parent.device.id, device_name: parentName, sensor_id: parent.sensor.id, source: 'device', group_id: null, parent_down: false });
  const c2 = await api.get(`/api/devices/${child2.device.id}`);
  expect(c2.dependency).toMatchObject({ mode: 'inherit', device_id: null, sensor_id: null });
  // The parent has no ping sensor -> no dependency sensor ("whole device" mode).
  expect(c2.effective_dependency).toMatchObject({ device_id: parent.device.id, source: 'group', group_id: branchGroup, sensor_id: null, parent_down: false });
  const p = await api.get(`/api/devices/${parent.device.id}`);
  expect(p.effective_dependency).toBeNull();
  const tree = await api.get('/api/tree');
  const td = flattenTree(tree).devices.find((d: any) => d.id === child2.device.id);
  expect(td.parent_device_id).toBe(parent.device.id);
  // Validation (422): sensor of another device, unknown device, ids with mode inherit.
  const bad = [
    { mode: 'device', device_id: parent.device.id, sensor_id: child1.sensor.id },
    { mode: 'device', device_id: 99999999, sensor_id: null },
    { mode: 'inherit', device_id: parent.device.id, sensor_id: null },
    { mode: 'device', device_id: null, sensor_id: null },
  ];
  for (const dep of bad) {
    const r = await raw(api, 'PATCH', `/api/devices/${child1.device.id}`, { dependency: dep });
    expect.soft(r.status, JSON.stringify(dep)).toBe(422);
  }
});

test('device page: dependency picker shows the effective parent, header shows "Parent: X"', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${child2.device.id}`);
  await expect(page.locator('main')).toContainText(new RegExp(`Parent:\\s*${parentName}`));
  await page.getByRole('button', { name: /^Edit/ }).first().click();
  const dlg = page.getByRole('dialog');
  await expect(dlg.getByRole('radio', { name: /Inherit from group/ })).toBeChecked();
  await expect(dlg).toContainText(new RegExp(`Effective: depends on\\s*${parentName}`));
  await expect(dlg.getByRole('radio', { name: /Depends on device/ })).toBeVisible();
  await expect(dlg.getByRole('radio', { name: /^None/ })).toBeVisible();
  await dlg.getByRole('button', { name: /Cancel|Close/ }).first().click();
});

test('parent outage: children unreachable (violet, live), only the parent alerts', async ({ page }) => {
  await clearWebhookRecords();
  await loginViaApi(page, undefined, `/devices/${child1.device.id}`);
  // Outage: parent target stopped (connection refused), children fail too (the uplink is gone).
  labKill('http-31');
  await target('127.0.0.32', 'fail');
  await target('127.0.0.33', 'fail');
  await waitStatus(api, parent.sensor.id, 'down', 45_000);
  const c1 = await waitStatus(api, child1.sensor.id, 'unreachable', 45_000);
  const c2 = await waitStatus(api, child2.sensor.id, 'unreachable', 45_000);
  for (const c of [c1, c2]) expect(c.status_message).toMatch(new RegExp(`^Unreachable \\(parent ${parentName} down\\): `));

  // Live in the UI (no reload): violet status, banner on the device page, tree dot.
  // The banner is checked live in the last test of this file; here it must at least be there (reload if needed).
  const apiParentDown = (await api.get(`/api/devices/${child1.device.id}`)).effective_dependency?.parent_down;
  expect(apiParentDown).toBe(true);
  if (!(await page.locator('main').getByText(/is down — sensors that fail/).count())) {
    // Not live (defect); it must at least be there after a reload.
    await page.reload();
    await expect(page.locator('main').getByText(/is down — sensors that fail/)).toBeVisible();
    await markDocument(page);
  }
  const badge = page.locator('main').getByText(/^Unreachable$/).first();
  await expect(badge).toBeVisible();
  const color = await badge.evaluate((el) => {
    let e: Element | null = el;
    while (e) {
      const cs = getComputedStyle(e);
      for (const v of [cs.backgroundColor, cs.color]) if (/139,\s*92,\s*246|#8b5cf6|oklch\(0\.606 0\.25 292/.test(v)) return v;
      e = e.parentElement;
    }
    return getComputedStyle(el).color + ' / ' + getComputedStyle(el).backgroundColor;
  });
  expect(color, 'violet-500 (#8b5cf6)').toMatch(/139,\s*92,\s*246|8b5cf6|292/);
  await expect(statusDot(treeDevice(page, child1.device.id).locator('xpath=..'), 'Unreachable').first()).toBeVisible();
  await shot2b(page, 'dependency-child-unreachable-en');

  // Rollups: device status unreachable; counts include the key.
  const d1 = await api.get(`/api/devices/${child1.device.id}`);
  expect(d1.status).toBe('unreachable');
  expect(d1.effective_dependency.parent_down).toBe(true);
  expect(d1.status_counts.unreachable).toBe(1);
  const dash = await api.get('/api/dashboard');
  expect(dash.unreachable_devices.map((d: any) => d.id)).toEqual(expect.arrayContaining([child1.device.id, child2.device.id]));
  expect(dash.unreachable_devices.find((d: any) => d.id === child1.device.id)).toMatchObject({ parent_device_id: parent.device.id, parent_device_name: parentName });
  expect(dash.sensor_counts.unreachable).toBeGreaterThanOrEqual(2);
  const unr = await api.get('/api/sensors?status=unreachable&page_size=1000');
  expect(unr.items.map((s: any) => s.id)).toEqual(expect.arrayContaining([child1.sensor.id, child2.sensor.id]));

  // Exactly one notification (the parent's DOWN) after the children's delay has passed.
  await expect.poll(async () => (await hooks(HOOK)).length, { timeout: 20_000 }).toBeGreaterThanOrEqual(1);
  await quiet(25_000);
  const recs = await hooks(HOOK);
  expect(recs.map((r) => `${r.body.sensor?.id}:${r.body.new_status}`)).toEqual([`${parent.sensor.id}:down`]);
  expect(await hooksForSensors([child1.sensor.id, child2.sensor.id])).toEqual([]);
  const ev = (await api.get(`/api/events?sensor_id=${child1.sensor.id}&page_size=50`)).items;
  expect(ev.some((e: any) => e.new_status === 'unreachable')).toBe(true);
});

test('parent recovers: children re-checked within seconds and back to up', async () => {
  await clearWebhookRecords();
  await target('127.0.0.32', 'ok');
  await target('127.0.0.33', 'ok');
  labRevive('http-31');
  await waitStatus(api, parent.sensor.id, 'up', 30_000);
  const t0 = Date.now();
  await waitStatus(api, child1.sensor.id, 'up', 20_000);
  await waitStatus(api, child2.sensor.id, 'up', 20_000);
  // run_soon jitter 0-5 s (+ check time); the regular 10 s schedule alone could also do it, so only bound it.
  expect(Date.now() - t0).toBeLessThan(15_000);
  await expect.poll(async () => (await hooks(HOOK)).map((r) => `${r.body.sensor?.id}:${r.body.new_status}`), { timeout: 15_000 }).toEqual([
    `${parent.sensor.id}:up`,
  ]);
  expect(await hooksForSensors([child1.sensor.id, child2.sensor.id])).toEqual([]);
});

test('child still down after the parent recovered: unreachable -> down -> alerts', async () => {
  labKill('http-31');
  await target('127.0.0.33', 'fail');
  await waitStatus(api, parent.sensor.id, 'down', 45_000);
  await waitStatus(api, child2.sensor.id, 'unreachable', 45_000);
  await clearWebhookRecords();
  labRevive('http-31'); // parent up, child 2 target still failing
  await waitStatus(api, parent.sensor.id, 'up', 30_000);
  await waitStatus(api, child2.sensor.id, 'down', 30_000);
  const ev = (await api.get(`/api/events?sensor_id=${child2.sensor.id}&page_size=50`)).items;
  expect(ev.some((e: any) => e.old_status === 'unreachable' && e.new_status === 'down')).toBe(true);
  // delay_s 20 on the child's rule.
  await expect
    .poll(async () => (await hooks(HOOK)).filter((r) => r.body.sensor?.id === child2.sensor.id).map((r) => r.body.new_status), { timeout: 45_000 })
    .toEqual(['down']);
  await target('127.0.0.33', 'ok');
  await waitStatus(api, child2.sensor.id, 'up', 30_000);
});

test('cycles: direct, via group inheritance and via bulk apply -> 400 dependency_cycle; self rule allowed', async ({ page }) => {
  // Direct: parent -> child1 while child1 -> parent.
  let r = await raw(api, 'PATCH', `/api/devices/${parent.device.id}`, { dependency: { mode: 'device', device_id: child1.device.id, sensor_id: null } });
  expect(r.status, r.text).toBe(400);
  expect(r.body.error.code).toBe('dependency_cycle');
  const path: number[] = r.body.error.details[0].device_ids;
  expect(path[0]).toBe(path[path.length - 1]);
  expect(path).toEqual(expect.arrayContaining([parent.device.id, child1.device.id]));
  // Explicit self dependency is a cycle.
  r = await raw(api, 'PATCH', `/api/devices/${child1.device.id}`, { dependency: { mode: 'device', device_id: child1.device.id, sensor_id: null } });
  expect(r.status).toBe(400);
  expect(r.body.error.code).toBe('dependency_cycle');
  // Via group inheritance: parent -> child2 (child2 inherits parent from its group).
  r = await raw(api, 'PATCH', `/api/devices/${parent.device.id}`, { dependency: { mode: 'device', device_id: child2.device.id, sensor_id: null } });
  expect(r.status).toBe(400);
  expect(r.body.error.code).toBe('dependency_cycle');
  // Bulk: atomic, cycle on the final state -> nothing applied.
  r = await raw(api, 'POST', '/api/devices/dependencies', {
    items: [
      { device_id: child1.device.id, dependency: { mode: 'none', device_id: null, sensor_id: null } },
      { device_id: parent.device.id, dependency: { mode: 'device', device_id: child2.device.id, sensor_id: null } },
    ],
  });
  expect(r.status).toBe(400);
  expect(r.body.error.code).toBe('dependency_cycle');
  expect((await api.get(`/api/devices/${child1.device.id}`)).dependency.mode).toBe('device');
  // Self rule: moving the parent into the group that depends on it is fine (the inherited self-parent is ignored).
  r = await raw(api, 'PATCH', `/api/devices/${parent.device.id}`, { group_id: branchGroup });
  expect(r.status, r.text).toBe(200);
  expect(r.body.effective_dependency).toBeNull();
  await api.patch(`/api/devices/${parent.device.id}`, { group_id: await defaultGroupId(api) });

  // UI: the cycle error is shown with the path names.
  await loginViaApi(page, undefined, `/devices/${parent.device.id}`);
  await page.getByRole('button', { name: /^Edit/ }).first().click();
  const dlg = page.getByRole('dialog');
  await dlg.getByRole('radio', { name: /Depends on device/ }).check();
  await dlg.getByRole('combobox', { name: /^Parent device/ }).selectOption(String(child1.device.id));
  await dlg.getByRole('button', { name: 'Save' }).click();
  await expect(dlg.getByText(/would create a loop/)).toBeVisible();
  await expect(dlg).toContainText(child1Name);
  await expect(dlg).toContainText(parentName);
  await shot2b(page, 'dependency-cycle-error-en');
  // Unchanged.
  expect((await api.get(`/api/devices/${parent.device.id}`)).dependency.mode).toBe('inherit');
});

test('cleanup: deleting the parent resets dependents to none', async () => {
  const extra = await httpDevice(api, { name: uid('dep-tmp-parent'), ip: '127.0.0.31', group_id: await defaultGroupId(api) });
  await api.post('/api/devices/dependencies', {
    items: [{ device_id: child1.device.id, dependency: { mode: 'device', device_id: extra.device.id, sensor_id: null } }],
  });
  expect((await api.get(`/api/devices/${child1.device.id}`)).dependency.device_id).toBe(extra.device.id);
  await api.del(`/api/devices/${extra.device.id}`);
  expect((await api.get(`/api/devices/${child1.device.id}`)).dependency).toMatchObject({ mode: 'none', device_id: null });
});

test('device page: the violet "Parent X is down" banner appears live (no reload)', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${child2.device.id}`);
  await expect(page.locator('main')).toContainText(new RegExp(`Parent:\\s*${parentName}`));
  await markDocument(page);
  try {
    labKill('http-31');
    await waitStatus(api, parent.sensor.id, 'down', 45_000);
    expect((await api.get(`/api/devices/${child2.device.id}`)).effective_dependency.parent_down).toBe(true);
    await expect(page.locator('main').getByText(/is down — sensors that fail are shown as unreachable/)).toBeVisible({ timeout: 20_000 });
    await expectSameDocument(page);
  } finally {
    labRevive('http-31');
  }
  await waitStatus(api, parent.sensor.id, 'up', 45_000);
  await expect(page.locator('main').getByText(/is down — sensors that fail/)).toBeHidden({ timeout: 20_000 });
});
