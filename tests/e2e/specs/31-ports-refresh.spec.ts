import { expect, test } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { collectWsFrames, raw } from '../lib/lab';
import { T, devicePorts, ensurePortLab, openPorts, refreshPorts, search, waitRun, type PortLab } from '../lib/lab2b2';

// [2b-2] (d) Refresh + job status + errors (api.md [2b-2] Errors, PortInventoryStatus), the WS ports.updated
// live update, the inventory flags auto/on/off (data-model § Devices: inventory flags), settings validation.
// default mode: a failure does not hide the results of the later tests (state is re-derived per test)
test.describe.configure({ mode: 'default' });

let api: Api;
let lab: PortLab;

test.beforeAll(async () => {
  api = await apiAs();
  lab = await ensurePortLab(api);
});

test.afterAll(async () => {
  // restore the §11.6 setup for the following specs
  await raw(api, 'PATCH', `/api/devices/${lab.legacy.id}`, { arp_collect: 'on' });
  await raw(api, 'PATCH', `/api/devices/${lab.aos.id}`, { port_inventory: 'auto', arp_collect: 'auto' });
  await raw(api, 'PUT', '/api/settings', { port_inventory_min: 10 });
});

test('job status after a run: timestamps, duration, counts, next_run_at, arp_sources', async () => {
  const dp = await refreshPorts(api, lab.aos.id);
  const s = dp.status;
  expect(s.error).toBeNull();
  expect(s.running).toBe(false);
  expect(s.last_ok_at).toBe(s.last_run_at);
  expect(s.duration_ms).toBeGreaterThanOrEqual(0);
  expect(s.duration_ms).toBeLessThan(90_000);
  expect(Date.parse(s.next_run_at) - Date.parse(s.last_run_at)).toBe(dp.settings.interval_min * 60_000);
  expect(dp.settings.interval_min).toBe(10);
  expect({ ports: s.ports, macs: s.macs, arp_entries: s.arp_entries }).toEqual({ ports: 12, macs: 14, arp_entries: 0 });
  // arp_sources = devices currently collecting ARP (global): ours .84 + .85, plus any other router with SNMP
  const devs = (await api.get('/api/devices?page_size=1000')).items;
  const sources = devs.filter((d: any) => d.arp_effective).map((d: any) => d.host);
  expect(sources).toEqual(expect.arrayContaining([lab.arp.host, lab.legacy.host]));
  expect(s.arp_sources).toBe(sources.length);
  const ar = (await devicePorts(api, lab.arp.id)).status;
  expect(ar).toMatchObject({ error: null, arp_entries: 4, ports: 0, fdb_mib: null });
  expect(ar.sources).toMatchObject({ arp: 'ok', fdb: 'skipped', if_xtable: 'skipped' });
  const lg = (await devicePorts(api, lab.legacy.id)).status;
  expect(lg).toMatchObject({ error: null, arp_entries: 1 });
  expect(lg.sources.arp).toBe('ok');
});

test('never-run device: nulls, zeros, sources skipped; viewer-independent shape', async () => {
  const dp = await devicePorts(api, lab.ap.id); // AP device: class access_point, never inventoried
  expect(dp).toMatchObject({ ports_effective: false, arp_effective: false, effective_class: 'access_point', ports: [] });
  expect(dp.status).toMatchObject({ last_run_at: null, last_ok_at: null, duration_ms: null, error: null, running: false, next_run_at: null, ports: 0, macs: 0, arp_entries: 0, fdb_mib: null, vlan_mib: null });
  expect(Object.values(dp.status.sources)).toEqual(Array(7).fill('skipped'));
});

test('409 ports_disabled: legacy ARP with arp_collect auto (class server); PATCH on -> entries appear', async () => {
  let d = await api.patch(`/api/devices/${lab.legacy.id}`, { arp_collect: 'auto' });
  expect(d).toMatchObject({ arp_collect: 'auto', arp_effective: false, ports_effective: false, effective_class: 'server' });
  const r = await raw(api, 'POST', `/api/devices/${lab.legacy.id}/ports/refresh`);
  expect(r.status, r.text).toBe(409);
  expect(r.body.error.code).toBe('ports_disabled');
  expect(r.body.error.details).toEqual([{ port_inventory: 'auto', arp_collect: 'auto', effective_class: 'server' }]);
  d = await api.patch(`/api/devices/${lab.legacy.id}`, { arp_collect: 'on' });
  expect(d.arp_effective).toBe(true);
  const dp = await refreshPorts(api, lab.legacy.id);
  expect(dp.status.arp_entries).toBe(1);
  const hit = (await search(api, { ip: '10.0.0.14' })).body;
  expect(hit.arp.map((a: any) => [a.ip, a.mac, a.source_device_id, a.present])).toEqual([['10.0.0.14', 'da:a1:19:00:00:04', lab.legacy.id, true]]);
});

test('flags: off disables a switch (409), on forces, auto follows the class; 422 on bad values', async () => {
  let d = await api.patch(`/api/devices/${lab.aos.id}`, { port_inventory: 'off' });
  expect(d).toMatchObject({ port_inventory: 'off', ports_effective: false, arp_effective: false });
  const r = await raw(api, 'POST', `/api/devices/${lab.aos.id}/ports/refresh`);
  expect(r.status).toBe(409);
  expect(r.body.error.code).toBe('ports_disabled');
  expect(r.body.error.details[0]).toMatchObject({ port_inventory: 'off', arp_collect: 'auto', effective_class: 'switch' });
  // the last snapshot stays readable while not effective
  expect((await devicePorts(api, lab.aos.id)).ports.length).toBe(12);
  d = await api.patch(`/api/devices/${lab.aos.id}`, { port_inventory: 'auto', arp_collect: 'on' });
  expect(d).toMatchObject({ ports_effective: true, arp_effective: true });
  const dp = await refreshPorts(api, lab.aos.id);
  expect(dp.status.sources.arp).toBe('empty'); // no ARP tables on the switch sim
  d = await api.patch(`/api/devices/${lab.aos.id}`, { arp_collect: 'auto' });
  expect(d.arp_effective).toBe(false);
  // forcing ports on a router: the ARP sim has a 2-row ifTable and no bridge MIB
  d = await api.patch(`/api/devices/${lab.arp.id}`, { port_inventory: 'on' });
  expect(d.ports_effective).toBe(true);
  const ap = await refreshPorts(api, lab.arp.id);
  expect(ap.ports.map((p: any) => p.name)).toEqual(['igb0', 'igb1']);
  expect(ap.status.sources).toMatchObject({ fdb: 'empty', bridge: 'empty', arp: 'ok' });
  await api.patch(`/api/devices/${lab.arp.id}`, { port_inventory: 'auto' });
  for (const body of [{ port_inventory: 'yes' }, { arp_collect: '' }, { port_inventory: null }]) {
    const x = await raw(api, 'PATCH', `/api/devices/${lab.aos.id}`, body);
    expect.soft(x.status, JSON.stringify(body)).toBe(422);
  }
});

test('409 ports_refresh_running for a second refresh while one runs', async () => {
  await expect.poll(async () => (await devicePorts(api, lab.aos52.id)).status.running).toBe(false);
  const prev = (await devicePorts(api, lab.aos52.id)).status.last_run_at;
  await new Promise((r) => setTimeout(r, 1100)); // whole-second timestamps: make the new run visibly newer
  const [a, b] = await Promise.all([
    raw(api, 'POST', `/api/devices/${lab.aos52.id}/ports/refresh`),
    raw(api, 'POST', `/api/devices/${lab.aos52.id}/ports/refresh`),
  ]);
  const st = [a.status, b.status].sort();
  expect(st, `${a.text} | ${b.text}`).toEqual([202, 409]);
  const conflict = a.status === 409 ? a : b;
  expect(conflict.body.error).toMatchObject({ code: 'ports_refresh_running', details: [{ device_id: lab.aos52.id }] });
  await waitRun(api, lab.aos52.id, prev);
});

test('settings: port_inventory_min / uplink threshold / retention / reverse_dns validation', async () => {
  const s = await api.get('/api/settings');
  expect(s).toMatchObject({ port_inventory_min: 10, uplink_mac_threshold: 5, port_mac_retention_days: 30, reverse_dns: true });
  for (const [k, v] of [['port_inventory_min', 4], ['port_inventory_min', 1441], ['port_inventory_min', -1], ['uplink_mac_threshold', 0], ['uplink_mac_threshold', 1001], ['port_mac_retention_days', 0], ['port_mac_retention_days', 366]] as const) {
    const r = await raw(api, 'PUT', '/api/settings', { [k]: v });
    expect.soft(r.status, `${k}=${v}`).toBe(422);
  }
  for (const [k, v] of [['port_inventory_min', 0], ['port_inventory_min', 5], ['port_inventory_min', 1440], ['uplink_mac_threshold', 1000]] as const) {
    const r = await raw(api, 'PUT', '/api/settings', { [k]: v });
    expect.soft(r.status, `${k}=${v}`).toBe(200);
  }
  // interval 0: next_run_at null, manual refresh still works
  await api.put('/api/settings', { port_inventory_min: 0, uplink_mac_threshold: 5 });
  const dp = await refreshPorts(api, lab.cx.id);
  expect(dp.status.next_run_at).toBeNull();
  expect(dp.settings.interval_min).toBe(0);
  await api.put('/api/settings', { port_inventory_min: 10 });
});

test('uplink threshold boundary: 7 MACs -> uplink at 6, not at 7', async () => {
  try {
    await api.put('/api/settings', { uplink_mac_threshold: 7 });
    let p9 = (await devicePorts(api, lab.aos.id)).ports.find((p: any) => p.if_index === 9);
    expect(p9).toMatchObject({ is_uplink: false, uplink_reason: null, mac_count: 7 });
    expect(p9.macs).toHaveLength(7);
    expect(p9.first_mac).not.toBeNull();
    await api.put('/api/settings', { uplink_mac_threshold: 6 });
    p9 = (await devicePorts(api, lab.aos.id)).ports.find((p: any) => p.if_index === 9);
    expect(p9).toMatchObject({ is_uplink: true, uplink_reason: 'mac_count', macs: [], first_mac: null });
  } finally {
    await api.put('/api/settings', { uplink_mac_threshold: 5 });
  }
});

test('UI: Refresh button -> spinner -> ports.updated over WS -> status line updates (no reload)', async ({ page }) => {
  const ws = collectWsFrames(page);
  await loginViaApi(page);
  await openPorts(page, lab.cw.id);
  const status = page.getByTestId('ports-status');
  await expect(status).toBeVisible();
  const before = (await devicePorts(api, lab.cw.id)).status.last_run_at;
  const reqs: string[] = [];
  page.on('request', (r) => r.url().includes(`/api/devices/${lab.cw.id}/ports`) && reqs.push(`${r.method()} ${r.url()}`));
  await page.getByRole('button', { name: T('ports.refresh') }).click();
  await expect.poll(() => ws.frames.some((f) => f.type === 'ports.updated' && f.data?.device_id === lab.cw.id), { timeout: 90_000 }).toBe(true);
  const f = ws.frames.find((x) => x.type === 'ports.updated' && x.data?.device_id === lab.cw.id);
  expect(f.data).toMatchObject({ device_id: lab.cw.id, ok: true });
  expect(Date.parse(f.data.collected_at)).toBeGreaterThan(Date.parse(before));
  // the open view refetched (WS invalidation), and the button is enabled again
  await expect.poll(() => reqs.filter((r) => r.startsWith('GET') && /\/ports(\?|$)/.test(r)).length).toBeGreaterThanOrEqual(1);
  await expect(page.getByRole('button', { name: T('ports.refresh') })).toBeEnabled({ timeout: 30_000 });
  // ports.updated of ANOTHER device that is an ARP source refetches the open view, debounced (trailing, at most
  // once per 30 s) without any focus event (api.md, client behaviour on ports.updated).
  const n = reqs.length;
  await refreshPorts(api, lab.arp.id);
  await expect.poll(() => ws.frames.some((x) => x.type === 'ports.updated' && x.data?.device_id === lab.arp.id)).toBe(true);
  await expect.poll(() => reqs.length, { message: 'ports view refetched after an ARP source\'s ports.updated', timeout: 45_000 }).toBeGreaterThan(n);
});

test('failed run: SNMP timeout -> ok:false, error "Timeout", snapshot kept', async () => {
  const { labKill, labRevive } = await import('../lib/lab2b');
  const before = await devicePorts(api, lab.cx.id);
  labKill('snmp-port-cx');
  try {
    const dp = await refreshPorts(api, lab.cx.id, 120_000);
    expect(dp.status.error).toBeTruthy();
    expect(dp.status.error).toMatch(/timeout|no snmp response/i);
    expect(dp.status.last_ok_at).toBe(before.status.last_ok_at);
    expect(dp.ports.length).toBe(before.ports.length); // snapshot untouched
  } finally {
    labRevive('snmp-port-cx');
  }
  const ok = await refreshPorts(api, lab.cx.id);
  expect(ok.status.error).toBeNull();
});
