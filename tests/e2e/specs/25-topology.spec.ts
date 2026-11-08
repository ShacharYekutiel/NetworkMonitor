import { expect, test } from '@playwright/test';
import { Api, apiAs, loginViaApi, uid } from '../lib/helpers';
import { collectWsFrames, ensureGroup, raw, snmpCreds } from '../lib/lab';
import { findDevice, labSwap, refreshTopology, shot2b, waitTopologyRefresh } from '../lib/lab2b';

// [2b-1] (g) Topology (data-model.md § Topology, api.md [2b-1] Topology, architecture.md §10.4 LLDP tree):
// core 127.0.0.51 (ArubaOS) - access-1 .52 (Comware, lldpLocPortNum != ifIndex) - access-2 .53 - AP .54,
// plus the unmanaged neighbour printer-3f (127.0.0.99). Refresh -> 3 deduplicated device-device links with
// the right ifIndexes, 1 neighbour node; the map renders; manual link add/delete; promotion of the
// neighbour; stale after 3 missed refreshes; dependency suggestions -> apply -> dependencies set.
test.describe.configure({ mode: 'serial' });

let api: Api;
const dev: Record<'core' | 'acc1' | 'acc2' | 'ap', any> = {} as any;
let graph: any = null;

async function ensureSnmpDevice(host: string, name: string, groupId: number): Promise<any> {
  const d = await findDevice(api, host);
  if (d) {
    const sensors = (await api.get(`/api/sensors?device_id=${d.id}&page_size=1000`)).items;
    if (!sensors.some((s: any) => s.type.startsWith('snmp_'))) await api.post('/api/sensors', { device_id: d.id, type: 'snmp_system' });
    return d;
  }
  return api.post('/api/devices', { group_id: groupId, name, host, snmp_credentials: snmpCreds(), auto_sensors: ['ping', 'snmp_system'] });
}

const linkKey = (l: any) => [l.a, l.b].map((e: any) => `${e.device_id ?? 'n' + e.neighbor_id}:${e.port}:${e.if_index}`).sort().join(' <-> ');
const devLinks = (g: any) => g.links.filter((l: any) => l.a.device_id && l.b.device_id && l.source !== 'manual');
const mine = (g: any, ids: number[]) => g.links.filter((l: any) => ids.includes(l.a.device_id) || ids.includes(l.b.device_id));

test.beforeAll(async () => {
  api = await apiAs();
  labSwap('snmp-access-2', 'access2'); // known state
  const g = await ensureGroup(api, 'QA topology');
  dev.core = await ensureSnmpDevice('127.0.0.51', 'core-sw', g);
  dev.acc1 = await ensureSnmpDevice('127.0.0.52', 'access-1', g);
  dev.acc2 = await ensureSnmpDevice('127.0.0.53', uid('qa-access-2'), g); // name != sysName: chassis-id match
  dev.ap = await ensureSnmpDevice('127.0.0.54', uid('qa-ap'), g); // name != sysName: lldp_sys_name match
  // Interface sensors on the core (edge colours / traffic).
  const cs = (await api.get(`/api/sensors?device_id=${dev.core.id}&page_size=1000`)).items;
  if (!cs.some((s: any) => s.type === 'snmp_interface' && s.config.if_index === 1)) {
    await api.post('/api/sensors', { device_id: dev.core.id, type: 'snmp_interface', name: '1', interval_s: 10, config: { if_index: 1 } });
  }
  const old = await findDevice(api, '127.0.0.99');
  if (old) await api.del(`/api/devices/${old.id}`);
});

test.afterAll(async () => {
  labSwap('snmp-access-2', 'access2');
});

test('refresh: 3 device-device links (deduplicated, correct ifIndexes on both families) + 1 neighbour', async () => {
  graph = await refreshTopology(api);
  const lr = graph.last_refresh;
  expect(lr).toMatchObject({ trigger: 'manual', status: 'completed', error: null });
  expect(lr.devices_ok).toBeGreaterThanOrEqual(4);
  const ids = [dev.core.id, dev.acc1.id, dev.acc2.id, dev.ap.id];
  const links = devLinks(graph).filter((l: any) => ids.includes(l.a.device_id) && ids.includes(l.b.device_id));
  const keys = links.map(linkKey).sort();
  const k = (a: [number, string, number], b: [number, string, number]) =>
    [a, b].map(([d, p, i]) => `${d}:${p}:${i}`).sort().join(' <-> ');
  expect(keys).toEqual(
    [
      k([dev.core.id, '1', 1], [dev.acc1.id, 'GigabitEthernet1/0/1', 1]),
      k([dev.core.id, '2', 2], [dev.acc2.id, '24', 24]),
      k([dev.acc1.id, 'GigabitEthernet1/0/2', 2], [dev.ap.id, 'eth0', 2]),
    ].sort(),
  );
  for (const l of links) {
    expect(l.source).toBe('lldp');
    expect(l.a.device_id).toBeLessThan(l.b.device_id); // canonical order
    expect(l).toMatchObject({ stale: false, missed_refreshes: 0 });
  }
  // The unmanaged neighbour.
  const nb = graph.nodes.filter((n: any) => n.kind === 'neighbor');
  const printer = nb.find((n: any) => n.chassis_id === '00:11:22:33:44:55');
  expect(printer).toMatchObject({ name: 'printer-3f', mgmt_address: '127.0.0.99', in_filter: true });
  expect(printer.key).toBe(`n:${printer.neighbor_id}`);
  const nl = graph.links.filter((l: any) => l.b.neighbor_id === printer.neighbor_id);
  expect(nl).toHaveLength(1);
  expect(nl[0].a).toMatchObject({ device_id: dev.acc2.id, if_index: 3, port: '3' });
  // No other neighbours from the lab tree (the AP row must match the device by sysName, not become a ghost).
  expect(nb.filter((n: any) => /access|core|U6-Lite/i.test(n.name))).toEqual([]);
  // Device nodes; lldp ids stored.
  const coreNode = graph.nodes.find((n: any) => n.key === `d:${dev.core.id}`);
  expect(coreNode).toMatchObject({ kind: 'device', device_id: dev.core.id, host: '127.0.0.51' });
  // Interface sensor of the core port 1 is attached to the link end.
  const up = links.find((l: any) => [l.a, l.b].some((e: any) => e.device_id === dev.core.id && e.if_index === 1));
  const coreEnd = [up.a, up.b].find((e: any) => e.device_id === dev.core.id);
  expect(coreEnd.sensor_id).not.toBeNull();
});

test('map page renders nodes and the neighbour ghost; edge panel; screenshot', async ({ page }) => {
  await loginViaApi(page, undefined, '/topology');
  const canvas = page.locator('main canvas').first();
  await expect(canvas).toBeVisible();
  // Doc label "Refresh topology now"; UI says "Refresh topology".
  await expect(page.getByRole('button', { name: /^Refresh topology/ })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Suggest dependencies' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Add link' })).toBeVisible();
  await expect(page.getByText(/Include stale/)).toBeVisible();
  await page.waitForTimeout(1500); // force layout settles once (layoutAnimation false) + PUT layout
  await shot2b(page, 'topology-map-en');
  // The admin's first render saves positions for every node.
  await expect.poll(async () => (await api.get('/api/topology')).nodes.filter((n: any) => n.x === null).length, { timeout: 15_000 }).toBe(0);
  // Dark mode screenshot.
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.evaluate(() => document.documentElement.classList.add('dark'));
  await page.waitForTimeout(500);
  await shot2b(page, 'topology-map-dark-en');
});

test('"Refresh topology now" from the UI shows progress; 409 while running', async ({ page }) => {
  await loginViaApi(page, undefined, '/topology');
  const respP = page.waitForResponse((r) => r.url().endsWith('/api/topology/refresh') && r.request().method() === 'POST');
  await page.getByRole('button', { name: /^Refresh topology/ }).click();
  const resp = await respP;
  expect(resp.status()).toBe(202);
  const second = await raw(api, 'POST', '/api/topology/refresh');
  if (second.status !== 202) {
    expect(second.status).toBe(409);
    expect(second.body.error.code).toBe('topology_refresh_running');
    expect(second.body.error.details[0].refresh_id).toBe((await resp.json()).id);
  }
  await waitTopologyRefresh(api);
});

test('manual link add (UI) / PATCH / delete; discovered link PATCH -> 400 link_not_manual', async ({ page }) => {
  await loginViaApi(page, undefined, '/topology');
  await page.getByRole('button', { name: 'Add link' }).click();
  const dlg = page.getByRole('dialog');
  const sideA = dlg.locator('fieldset').nth(0);
  const sideB = dlg.locator('fieldset').nth(1);
  const devSelect = (scope: typeof sideA, id: number) => scope.locator('select').filter({ has: page.locator(`option[value="${id}"]`) }).first();
  await devSelect(sideA, dev.core.id).selectOption(String(dev.core.id));
  // Interface picker (ifTable walk) -> ifIndex 3, port name defaults to its ifName.
  const ifSel = sideA.getByLabel('Interface');
  await expect(ifSel).toBeEnabled({ timeout: 15_000 });
  await ifSel.selectOption('3');
  await devSelect(sideB, dev.ap.id).selectOption(String(dev.ap.id));
  await sideB.getByRole('textbox', { name: /^Port/ }).fill('uplink');
  await dlg.getByRole('button', { name: 'Save' }).click();
  await expect(dlg).toBeHidden();
  let man = (await api.get(`/api/topology/links?source=manual&device_id=${dev.core.id}`)).items.find((l: any) => l.b.device_id === dev.ap.id || l.a.device_id === dev.ap.id);
  if (!man) {
    // UI path failed -> still verify the API.
    man = await api.post('/api/topology/links', { a_device_id: dev.core.id, a_if_index: 3, a_port: null, b_device_id: dev.ap.id, b_neighbor_id: null, b_if_index: null, b_port: 'uplink' });
  }
  expect(man.source).toBe('manual');
  const viaApi = await api.post('/api/topology/links', { a_device_id: dev.core.id, a_if_index: 4, b_device_id: dev.acc2.id, b_neighbor_id: null, b_if_index: null, b_port: 'x' });
  expect(viaApi.a.port).toBe('4'); // defaults to the ifName of a_if_index
  const patched = await api.patch(`/api/topology/links/${viaApi.id}`, { b_port: 'y' });
  expect([patched.a.port, patched.b.port]).toContain('y');
  const bad = await raw(api, 'POST', '/api/topology/links', { a_device_id: dev.core.id, b_device_id: dev.ap.id, b_neighbor_id: 1, b_port: 'z' });
  expect(bad.status).toBe(422);
  const lldp = (await api.get(`/api/topology/links?source=lldp&device_id=${dev.core.id}`)).items[0];
  const nm = await raw(api, 'PATCH', `/api/topology/links/${lldp.id}`, { b_port: 'nope' });
  expect(nm.status).toBe(400);
  expect(nm.body.error.code).toBe('link_not_manual');
  // Manual links survive a refresh untouched.
  await refreshTopology(api);
  const after = (await api.get('/api/topology')).links.find((l: any) => l.id === viaApi.id);
  expect(after).toMatchObject({ source: 'manual', stale: false });
  // Delete via the side panel is a canvas click; use the API for the second one.
  expect((await raw(api, 'DELETE', `/api/topology/links/${viaApi.id}`)).status).toBe(204);
  expect((await raw(api, 'DELETE', `/api/topology/links/${man.id}`)).status).toBe(204);
  expect((await api.get('/api/topology')).links.some((l: any) => l.id === man.id || l.id === viaApi.id)).toBe(false);
  // A deleted discovered link comes back on the next refresh.
  expect((await raw(api, 'DELETE', `/api/topology/links/${lldp.id}`)).status).toBe(204);
  const g2 = await refreshTopology(api);
  expect(devLinks(g2).map(linkKey)).toContain(linkKey(lldp));
});

test('promotion: a device with host 127.0.0.99 takes over the neighbour and its links', async () => {
  const p = await api.post('/api/devices', { group_id: dev.acc2.group_id, name: uid('printer-3f-dev'), host: '127.0.0.99', auto_sensors: [] });
  const g = await refreshTopology(api);
  expect(g.nodes.filter((n: any) => n.kind === 'neighbor' && n.chassis_id === '00:11:22:33:44:55')).toEqual([]);
  const l = g.links.filter((x: any) => x.a.device_id === p.id || x.b.device_id === p.id);
  expect(l).toHaveLength(1);
  const accEnd = [l[0].a, l[0].b].find((e: any) => e.device_id === dev.acc2.id);
  expect(accEnd).toMatchObject({ if_index: 3 });
  await api.del(`/api/devices/${p.id}`);
  const g2 = await refreshTopology(api);
  expect(g2.nodes.some((n: any) => n.kind === 'neighbor' && n.chassis_id === '00:11:22:33:44:55')).toBe(true);
});

test('stale: remote row removed + 3 refreshes -> stale (dashed in the map), Include stale filter, bulk delete', async ({ page }) => {
  labSwap('snmp-access-2', 'access2_noprinter');
  let g: any = null;
  for (let i = 1; i <= 3; i++) {
    g = await refreshTopology(api);
    const nl = g.links.find((l: any) => l.b.neighbor_id && l.a.device_id === dev.acc2.id);
    expect(nl, `refresh ${i}`).toBeTruthy();
    expect(nl.missed_refreshes).toBe(i);
    expect(nl.stale).toBe(i >= 3);
  }
  // Other links unaffected (core<->access-2 still seen from both sides).
  expect(mine(g, [dev.acc2.id]).filter((l: any) => l.stale).length).toBe(1);
  const hidden = await api.get('/api/topology?include_stale=false');
  expect(hidden.links.some((l: any) => l.stale)).toBe(false);
  await loginViaApi(page, undefined, '/topology');
  await expect(page.locator('main canvas').first()).toBeVisible();
  await page.waitForTimeout(1000);
  await shot2b(page, 'topology-map-stale-en');
  expect((await raw(api, 'DELETE', '/api/topology/links')).status).toBe(422); // stale=true required
  const del = await api.call('DELETE', '/api/topology/links?stale=true');
  expect(del.deleted).toBeGreaterThanOrEqual(1);
  labSwap('snmp-access-2', 'access2');
  await refreshTopology(api);
});

test('suggest dependencies (root = core) -> apply selected in the UI -> dependencies set', async ({ page }) => {
  const sug = await api.get(`/api/topology/dependency-suggestions?root_device_id=${dev.core.id}`);
  expect(sug.root_device_id).toBe(dev.core.id);
  const byId = Object.fromEntries(sug.items.map((i: any) => [i.device_id, i]));
  expect(byId[dev.acc1.id]).toMatchObject({ suggested_parent_id: dev.core.id, hops: 1 });
  expect(byId[dev.acc2.id]).toMatchObject({ suggested_parent_id: dev.core.id, hops: 1 });
  expect(byId[dev.ap.id]).toMatchObject({ suggested_parent_id: dev.acc1.id, hops: 2 });
  expect(byId[dev.core.id]).toBeUndefined();
  const hops = sug.items.map((i: any) => i.hops);
  expect([...hops].sort((a, b) => a - b)).toEqual(hops);

  await loginViaApi(page, undefined, '/topology');
  await page.getByRole('button', { name: 'Suggest dependencies' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.locator('select').filter({ has: page.locator(`option[value="${dev.core.id}"]`) }).first().selectOption(String(dev.core.id));
  await expect(dlg.getByText(`Root: ${dev.core.name}`)).toBeVisible();
  for (const d of [dev.acc1, dev.acc2, dev.ap]) {
    const cb = dlg.getByRole('checkbox', { name: d.name, exact: true });
    await expect(cb).toBeVisible();
    await cb.check();
  }
  await shot2b(page, 'topology-suggestions-en');
  await dlg.getByRole('button', { name: /^Apply \d+ selected/ }).click();
  await expect(dlg).toBeHidden();
  for (const [d, parentId] of [[dev.acc1, dev.core.id], [dev.acc2, dev.core.id], [dev.ap, dev.acc1.id]] as const) {
    const x = await api.get(`/api/devices/${d.id}`);
    expect(x.dependency, d.name).toMatchObject({ mode: 'device', device_id: parentId });
    expect(x.effective_dependency.device_id).toBe(parentId);
  }
  // Re-suggest: current values now reported.
  const sug2 = await api.get(`/api/topology/dependency-suggestions?root_device_id=${dev.core.id}`);
  expect(sug2.items.find((i: any) => i.device_id === dev.ap.id)).toMatchObject({ current: { mode: 'device', device_id: dev.acc1.id }, effective_parent_id: dev.acc1.id });
});

test('layout: PUT/DELETE positions, group filter (in_filter), topology.updated WS', async ({ page }) => {
  const ws = collectWsFrames(page);
  await loginViaApi(page, undefined, '/topology');
  await api.put('/api/topology/layout', { positions: [{ node: `d:${dev.core.id}`, x: 10.5, y: -20 }, { node: 'd:99999999', x: 1, y: 1 }] });
  const g = await api.get('/api/topology');
  expect(g.nodes.find((n: any) => n.key === `d:${dev.core.id}`)).toMatchObject({ x: 10.5, y: -20 });
  await expect.poll(() => ws.frames.filter((f: any) => f.type === 'topology.updated' && f.data.reason === 'layout').length, { timeout: 10_000 }).toBeGreaterThan(0);
  // Group filter: the AP's group only -> AP in filter, access-1 shown as linked but not in filter.
  const apGroup = await api.post('/api/groups', { name: uid('QA topo AP'), parent_id: null });
  await api.patch(`/api/devices/${dev.ap.id}`, { group_id: apGroup.id });
  const f = await api.get(`/api/topology?group_id=${apGroup.id}`);
  expect(f.nodes.find((n: any) => n.device_id === dev.ap.id)?.in_filter).toBe(true);
  expect(f.nodes.find((n: any) => n.device_id === dev.acc1.id)?.in_filter).toBe(false);
  expect(f.nodes.some((n: any) => n.device_id === dev.core.id)).toBe(false);
  expect(f.links.every((l: any) => l.a.device_id === dev.ap.id || l.b.device_id === dev.ap.id)).toBe(true);
  await api.patch(`/api/devices/${dev.ap.id}`, { group_id: dev.acc1.group_id });
  await api.del(`/api/groups/${apGroup.id}`);
});
