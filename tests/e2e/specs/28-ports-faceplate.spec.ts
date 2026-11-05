import { expect, test } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { raw } from '../lib/lab';
import { findDevice } from '../lib/lab2b';
import { IP, T, box, devicePorts, ensurePortLab, openPorts, portOf, refreshPorts, setLang, shot2b2, tile, type PortLab } from '../lib/lab2b2';

// [2b-2] (a) Port inventory snapshot (architecture.md §11.6 expected tables) + the front panel:
// layout per parser (§11.4) for each sim, tile states/icons (§11.5), legend, LTR inside Hebrew.
// Lab: AOS = contract .61 (here 127.0.0.81), CW = .62 (.82), CX = .63 (.83), see lib/lab2b2.ts.
test.describe.configure({ mode: 'serial' });

let api: Api;
let lab: PortLab;
const G = 1_000_000_000;

test.beforeAll(async () => {
  api = await apiAs();
  lab = await ensurePortLab(api);
});

test('class detection + effective flags (identity null)', async () => {
  const want: [any, string, boolean, boolean][] = [
    [lab.aos, 'switch', true, false],
    [lab.cw, 'switch', true, false],
    [lab.cx, 'switch', true, false],
    [lab.arp, 'router', false, true],
    [lab.legacy, 'server', false, true], // arp_collect: on
  ];
  for (const [d, cls, ports, arp] of want) {
    const dev = await api.get(`/api/devices/${d.id}`);
    expect.soft(dev.identity, d.host).toBeNull();
    expect.soft({ host: dev.host, effective_class: dev.effective_class, ports_effective: dev.ports_effective, arp_effective: dev.arp_effective }).toEqual({ host: d.host, effective_class: cls, ports_effective: ports, arp_effective: arp });
    expect.soft(['auto', 'on', 'off']).toContain(dev.port_inventory);
  }
});

test('AOS (contract .61): expected port table', async () => {
  // Two runs: the first gives in_bps null, the second 0.0 (static counters).
  await refreshPorts(api, lab.aos.id);
  const dp = await devicePorts(api, lab.aos.id);
  expect(dp).toMatchObject({ device_id: lab.aos.id, ports_effective: true, effective_class: 'switch', settings: { uplink_mac_threshold: 5 } });
  const s = dp.status;
  expect(s).toMatchObject({ fdb_mib: 'q_bridge', vlan_mib: 'static', error: null, running: false, ports: 12, macs: 14 });
  expect(s.sources).toMatchObject({ bridge: 'empty', fdb: 'ok', vlans: 'ok', poe: 'ok', duplex: 'ok', if_xtable: 'ok', arp: 'skipped' });
  expect(dp.ports.map((p: any) => p.if_index)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);

  const P = (i: number) => portOf(dp, i);
  const row = (i: number) => {
    const p = P(i);
    return { state: p.state, speed: p.speed_bps, duplex: p.duplex, mode: p.mode, pvid: p.pvid, untagged: p.untagged_vlans, tagged: p.tagged_vlans,
      poe: p.poe && [p.poe.admin_enabled, p.poe.detection, p.poe.power_class], mac_count: p.mac_count, uplink: p.is_uplink ? p.uplink_reason : null,
      kind: p.connected_kind, alert: p.alert };
  };
  const exp: Record<number, any> = {
    1: { state: 'up', speed: G, duplex: 'full', mode: 'access', pvid: 1, untagged: [1], tagged: [], poe: [true, 'delivering', 3], mac_count: 1, uplink: null, kind: 'client', alert: false },
    2: { state: 'up', speed: G, duplex: 'full', mode: 'access', pvid: 1, untagged: [1], tagged: [], poe: [true, 'searching', null], mac_count: 2, uplink: null, kind: 'client', alert: false },
    3: { state: 'up', speed: 100_000_000, duplex: 'half', mode: 'access', pvid: 1, untagged: [1], tagged: [], poe: [true, 'searching', null], mac_count: 0, uplink: null, kind: 'client', alert: false },
    4: { state: 'up', speed: G, duplex: 'full', mode: 'access', pvid: 1, untagged: [1], tagged: [], poe: [true, 'delivering', 4], mac_count: 1, uplink: null, kind: 'client', alert: false },
    5: { state: 'admin_down', duplex: null, mode: 'access', pvid: 1, untagged: [1], tagged: [], poe: [false, 'disabled', null], mac_count: 0, uplink: null, kind: 'none', alert: false },
    6: { state: 'down', speed: null, duplex: 'unknown', mode: 'access', pvid: 1, untagged: [1], tagged: [], poe: [true, 'searching', null], mac_count: 0, uplink: null, kind: 'none', alert: false },
    7: { state: 'up', speed: G, duplex: 'full', mode: 'access', pvid: 10, untagged: [10], tagged: [], poe: [true, 'delivering', 0], mac_count: 1, uplink: null, kind: 'client', alert: false },
    8: { state: 'down', duplex: null, mode: 'access', pvid: 1, untagged: [1], tagged: [], poe: [true, 'fault', null], mac_count: 0, uplink: null, kind: 'none', alert: true },
    9: { state: 'up', speed: 10 * G, duplex: 'full', mode: 'trunk', pvid: 1, untagged: [1], tagged: [10, 20], poe: null, mac_count: 7, uplink: 'mac_count', kind: 'client', alert: false },
    10: { state: 'up', speed: 10 * G, duplex: 'full', mode: 'trunk', pvid: 1, untagged: [], tagged: [10, 20], poe: null, mac_count: 2, uplink: 'neighbor', kind: 'switch', alert: false },
  };
  for (const [i, e] of Object.entries(exp)) {
    const got = row(Number(i));
    if (!('speed' in e)) delete (got as any).speed;
    expect.soft(got, `AOS port ${i}`).toEqual(e);
  }
  expect.soft(P(8).alert_reasons).toEqual(['poe_fault']);
  expect.soft(P(11)).toMatchObject({ name: 'Trk1', kind: 'lag' });
  expect.soft(P(12)).toMatchObject({ name: 'DEFAULT_VLAN', kind: 'vlan' });
  for (const p of dp.ports) expect.soft(p.poe === null || p.poe.power_w === null, `power_w null on ${p.if_index}`).toBe(true);

  // MACs, OUI vendor, ARP IP, reverse DNS (NETMON_TEST_PTR_FILE)
  expect(P(1).macs).toHaveLength(1);
  expect(P(1).macs[0]).toMatchObject({ mac: '00:50:56:01:00:01', vlan: 1, ip: '10.0.0.11', ips: ['10.0.0.11'], hostname: 'pc-reception.lan', locally_administered: false });
  expect(P(1).macs[0].vendor).toMatch(/vmware/i);
  expect(P(2).first_mac).toMatchObject({ mac: '00:00:0c:00:00:03', ip: '10.0.0.12', hostname: 'phone-12.lan' });
  expect(P(2).first_mac.vendor).toMatch(/^Cisco/);
  expect(P(2).macs.map((m: any) => m.mac)).toEqual(['00:00:0c:00:00:03', '3c:52:82:00:00:02']); // sorted by vlan, mac
  expect(P(3).macs).toEqual([]); // status 4 (self) dropped
  expect(P(4).macs[0]).toMatchObject({ mac: 'da:a1:19:00:00:04', locally_administered: true, vendor: null, ip: '10.0.0.14' });
  expect(P(7).macs[0]).toMatchObject({ mac: '00:50:56:01:00:07', vlan: 10, ip: '10.0.0.17', hostname: null });
  expect(P(9)).toMatchObject({ macs: [], first_mac: null, trunk: true });
  expect(P(10)).toMatchObject({ macs: [], first_mac: null, trunk: true });
  expect(P(10).neighbor).toMatchObject({ source: 'manual', kind: 'device', device_id: lab.cw.id, device_class: 'switch', name: lab.cw.name });

  // linked sensor, last change, rates (second run)
  expect(P(1).sensor_id).toBe(lab.ifSensorId);
  expect(Date.parse(P(1).collected_at) - Date.parse(P(1).last_change_at)).toBe(10_000_000);
  for (const i of [2, 3, 4, 7, 9, 10]) expect.soft(P(i).last_change_at, `last_change ${i}`).toBeNull(); // ifLastChange 0
  expect(P(1).in_bps).toBe(0);
  expect(P(1).out_bps).toBe(0);
  expect(P(1).in_errors_delta).toBe(0);
});

test('CW (contract .62): kinds, bridge mapping, fdbId 1010 -> VLAN 10, PoE, neighbours', async () => {
  const dp = await devicePorts(api, lab.cw.id);
  const P = (i: number) => portOf(dp, i);
  expect(dp.status).toMatchObject({ fdb_mib: 'q_bridge', vlan_mib: 'static', error: null });
  expect(dp.status.sources).toMatchObject({ bridge: 'ok', fdb: 'ok', poe: 'ok' });
  expect(dp.ports.filter((p: any) => p.kind === 'physical').map((p: any) => p.if_index)).toEqual([1, 2, 3, 4, 49, 50, 101, 102]);
  expect(P(400)).toMatchObject({ name: 'M-GigabitEthernet0/0/0', kind: 'mgmt' });
  expect(P(200)).toMatchObject({ name: 'Vlan-interface1', kind: 'vlan' });
  expect(P(300).kind).toBe('other');
  expect(P(301).kind).toBe('other');
  expect(P(4)).toMatchObject({ mode: 'access', pvid: 10, untagged_vlans: [10] });
  expect(P(4).macs.map((m: any) => [m.mac, m.vlan])).toEqual([['00:0c:29:00:01:04', 10]]);
  expect(P(1).macs.map((m: any) => [m.mac, m.vlan])).toEqual([['00:0c:29:00:01:01', 1]]);
  expect(P(101).macs.map((m: any) => m.mac)).toEqual(['00:0c:29:00:01:07']); // bp 7 -> ifIndex 101
  expect(P(3).macs.map((m: any) => m.mac)).toEqual(['02:00:00:00:09:01']);
  expect(P(49)).toMatchObject({ mode: 'trunk', untagged_vlans: [1], tagged_vlans: [10, 20], is_uplink: true, uplink_reason: 'neighbor', connected_kind: 'switch' });
  expect(P(49).neighbor).toMatchObject({ device_id: lab.aos.id, kind: 'device' });
  // Contract text: "Ports 49/50: trunk, tagged [10, 20]". By the VLAN rule (bp 6 is in VLAN 1 egress, not
  // untagged) port 50 is tagged [1, 10, 20]; the algorithm is asserted (contract inconsistency reported).
  expect(P(50)).toMatchObject({ mode: 'trunk', untagged_vlans: [], tagged_vlans: [1, 10, 20] });
  expect(P(1).poe).toMatchObject({ detection: 'delivering', admin_enabled: true });
  expect(P(2).poe).toMatchObject({ detection: 'searching' });
  // QA extra: LLDP neighbour on port 2 = the 2b-1 UniFi AP (identified access_point) -> AP tile
  expect(P(2)).toMatchObject({ state: 'up', connected_kind: 'ap', is_uplink: true, uplink_reason: 'neighbor' });
  expect(P(2).neighbor).toMatchObject({ source: 'lldp', kind: 'device', device_id: lab.ap.id, device_class: 'access_point', port: 'eth0' });
});

test('CX (contract .63): BRIDGE-MIB FDB fallback (vlan null), current VLAN table, PVID from untagged', async () => {
  const dp = await devicePorts(api, lab.cx.id);
  const P = (i: number) => portOf(dp, i);
  expect(dp.status).toMatchObject({ fdb_mib: 'bridge', vlan_mib: 'current', error: null });
  expect(dp.status.sources.poe).toBe('ok');
  expect(P(1).macs.map((m: any) => [m.mac, m.vlan])).toEqual([['00:00:0c:00:cc:01', null]]);
  for (let i = 1; i <= 6; i++) expect.soft(P(i), `CX ${i}`).toMatchObject({ mode: 'access', pvid: 1, untagged_vlans: [1] });
  expect(P(2000)).toMatchObject({ name: 'mgmt', kind: 'mgmt' });
  expect(P(1000)).toMatchObject({ name: 'vlan1', kind: 'vlan' });
  for (let i = 1; i <= 6; i++) expect.soft(P(i).poe, `CX PoE ${i}`).not.toBeNull();
  expect(P(1).poe.detection).toBe('delivering');
});

test('kind filter + 404 for an unknown ifIndex', async () => {
  const dp = await devicePorts(api, lab.cw.id, ['physical', 'lag']);
  expect(new Set(dp.ports.map((p: any) => p.kind))).toEqual(new Set(['physical']));
  expect((await raw(api, 'GET', `/api/devices/${lab.cw.id}/ports/999`)).status).toBe(404);
  const r = await raw(api, 'GET', `/api/devices/${lab.cw.id}/ports/999`);
  expect(r.body.error?.code ?? r.body.code).toBe('not_found');
});

test('health strip: 2b-1 HPE sims (127.0.0.51 / .52)', async () => {
  const core = await findDevice(api, '127.0.0.51');
  test.skip(!core, '2b-1 HPE devices not imported (spec 24 not run)');
  const h = (await devicePorts(api, core.id)).health;
  const by = Object.fromEntries(h.map((x: any) => [x.category, x]));
  expect(h.map((x: any) => x.category)).toEqual(['cpu', 'memory', 'temperature', 'poe', 'psu', 'fan']);
  expect(by.cpu).toMatchObject({ status: 'up', value: 7, unit: '%' });
  expect(by.memory.status).toBe('up');
  expect(by.memory.value).toBeCloseTo(39.4, 0);
  expect(by.temperature).toMatchObject({ status: 'up', text: '1/1' });
  expect(by.poe.status).toBe('up');
  expect(by.poe.value).toBeCloseTo(12.16, 1);
  expect(by.psu).toMatchObject({ status: 'down', text: '0/1', value: null, unit: '' });
  expect(by.fan).toMatchObject({ status: 'up', text: '1/1' });
  const cw = await findDevice(api, '127.0.0.52');
  if (cw) {
    const h2 = Object.fromEntries((await devicePorts(api, cw.id)).health.map((x: any) => [x.category, x]));
    expect(Object.keys(h2)).toEqual(['cpu', 'memory', 'temperature', 'psu', 'fan']);
    expect(h2.temperature).toMatchObject({ value: 43, unit: '°C' });
    expect(h2.psu.status).toBe('up');
    expect(h2.fan).toMatchObject({ status: 'down', text: '1/2' });
  }
});

// ---------------------------------------------------------------- front panel UI
test('AOS faceplate: odd top / even bottom, uplink block 9/10, list-only kinds absent', async ({ page }) => {
  await loginViaApi(page);
  await openPorts(page, lab.aos.id);
  await expect(page.getByTestId('faceplate')).toHaveAttribute('data-mode', 'parsed');
  for (let i = 1; i <= 10; i++) await expect(tile(page, i)).toBeVisible();
  await expect(tile(page, 11)).toHaveCount(0); // Trk1 (lag)
  await expect(tile(page, 12)).toHaveCount(0); // DEFAULT_VLAN
  const main = page.locator('[data-block="m1-0"]');
  const up = page.locator('[data-block="m1-uplink"]');
  for (const i of [1, 2, 3, 4, 5, 6, 7, 8]) await expect(main.locator(`button[data-if-index="${i}"]`)).toHaveCount(1);
  for (const i of [9, 10]) await expect(up.locator(`button[data-if-index="${i}"]`)).toHaveCount(1);
  const b: Record<number, any> = {};
  for (let i = 1; i <= 10; i++) b[i] = await box(page, i);
  for (const i of [1, 3, 5, 7, 9]) {
    expect(b[i].y, `odd ${i} on top`).toBeLessThan(b[i + 1].y);
    expect(Math.abs(b[i].x - b[i + 1].x), `${i}/${i + 1} same column`).toBeLessThan(2);
  }
  expect(b[3].x).toBeGreaterThan(b[1].x);
  expect(b[9].x - b[7].x, 'uplink block separated').toBeGreaterThan(b[7].x - b[5].x);
  expect(await up.getAttribute('aria-label')).toBe(T('ports.uplinkGroup'));
  await shot2b2(page, 'faceplate-aos-en');
});

test('CW faceplate: member 2 row, uplink 49/50, mgmt/vlan not drawn', async ({ page }) => {
  await loginViaApi(page);
  await openPorts(page, lab.cw.id);
  const fp = page.getByTestId('faceplate');
  await expect(fp.getByText(T('ports.member', { n: 1 }), { exact: true })).toBeVisible();
  await expect(fp.getByText(T('ports.member', { n: 2 }), { exact: true })).toBeVisible();
  await expect(page.locator('[data-block="m1-0"] button')).toHaveCount(4);
  await expect(page.locator('[data-block="m1-uplink"] button')).toHaveCount(2);
  await expect(page.locator('[data-block="m2-0"] button')).toHaveCount(2);
  for (const i of [200, 300, 301, 400]) await expect(tile(page, i)).toHaveCount(0);
  const [p1, p2, p49, p50, m21, m22] = [await box(page, 1), await box(page, 2), await box(page, 49), await box(page, 50), await box(page, 101), await box(page, 102)];
  expect(p1.y).toBeLessThan(p2.y);
  expect(p49.y).toBeLessThan(p50.y);
  expect(p49.x).toBeGreaterThan(p1.x);
  expect(m21.y, 'member 2 below member 1').toBeGreaterThan(p2.y);
  expect(m21.y).toBeLessThan(m22.y);
  // labels: port number p as text (member 2 port 1 shows "1")
  await expect(page.locator('[data-block="m2-0"]')).toContainText('1');
  await shot2b2(page, 'faceplate-cw-en');
});

test('CX faceplate: module block m1-1 + uplink by speed (49/50), mgmt absent', async ({ page }) => {
  await loginViaApi(page);
  await openPorts(page, lab.cx.id);
  await expect(page.locator('[data-block="m1-1"] button')).toHaveCount(6);
  await expect(page.locator('[data-block="m1-uplink"] button')).toHaveCount(2);
  await expect(tile(page, 2000)).toHaveCount(0);
  await expect(tile(page, 1000)).toHaveCount(0);
});

test('tile states, icons and badges (AOS + CW AP port) + legend', async ({ page }) => {
  // up + alert: an snmp_interface sensor on AOS port 4 forced to warning (in_bps warning_min 1; counters static)
  const s = await api.post('/api/sensors', { device_id: lab.aos.id, type: 'snmp_interface', name: 'qa-alert-4', interval_s: 10, config: { if_index: 4, if_name: '4' }, channels: [{ key: 'in_bps', warning_min: 1 }] });
  try {
    await expect.poll(async () => (await api.get(`/api/sensors/${s.id}`)).status, { timeout: 60_000 }).toBe('warning');
    await expect.poll(async () => portOf(await devicePorts(api, lab.aos.id), 4).alert_reasons).toEqual(['sensor']);
    await loginViaApi(page);
    await openPorts(page, lab.aos.id);
    const kind = async (i: number) => tile(page, i).getAttribute('data-tile');
    expect({ 1: await kind(1), 4: await kind(4), 5: await kind(5), 6: await kind(6), 8: await kind(8), 9: await kind(9), 10: await kind(10) }).toEqual({
      1: 'up', 4: 'alert', 5: 'disabled', 6: 'empty', 8: 'emptyAlert', 9: 'up', 10: 'up',
    });
    // contract colours (§11.5 table)
    const glyph = (i: number) => tile(page, i).locator(':scope > span').first();
    await expect(glyph(1)).toHaveClass(/bg-green-500/);
    await expect(glyph(4)).toHaveClass(/bg-orange-500/);
    await expect(glyph(5)).toHaveClass(/bg-gray-200/);
    await expect(glyph(6)).toHaveClass(/bg-white/);
    await expect(glyph(8)).toHaveClass(/bg-orange-200/);
    // icons: client monitor on 1, crossed-out on 5, none on 6 / 8 (peach, no icon), switch on 10
    await expect(glyph(1).locator('svg')).toHaveCount(1);
    await expect(glyph(5).locator('svg')).toHaveCount(1);
    await expect(glyph(6).locator('svg')).toHaveCount(0);
    await expect(glyph(8).locator('svg')).toHaveCount(0);
    // badges: MAC count uplink 9 -> "7"; trunk badge on 9/10 (stacked-lines icon); 10 has no count (neighbour uplink)
    await expect(glyph(9)).toContainText('7');
    await expect(glyph(10).locator('svg')).toHaveCount(2);
    await expect(glyph(10)).not.toContainText(/\d/);
    // aria-labels "Port 7, up, client, VLAN 10"
    await expect(tile(page, 7)).toHaveAttribute('aria-label', [T('ports.portN', { name: '7' }), T('ports.state.up'), T('ports.connectedKind.client'), T('ports.vlanN', { vlan: 10 })].join(', '));
    expect(await tile(page, 10).getAttribute('aria-label')).toContain(T('ports.connectedKind.switch'));
    expect(await tile(page, 4).getAttribute('aria-label')).toContain(T('ports.alert'));
    expect(await tile(page, 5).getAttribute('aria-label')).toContain(T('ports.state.admin_down'));
    // legend: five tiles + three icons
    const legend = page.getByTestId('faceplate-legend');
    for (const k of ['empty', 'connected', 'alert', 'emptyAlert', 'disabled', 'client', 'ap', 'switch']) await expect(legend).toContainText(T(`ports.legend.${k}`));
    await shot2b2(page, 'faceplate-states-en');

    await openPorts(page, lab.cw.id);
    expect(await tile(page, 2).getAttribute('data-tile')).toBe('up');
    expect(await tile(page, 2).getAttribute('aria-label')).toContain(T('ports.connectedKind.ap'));
    await expect(tile(page, 2).locator(':scope > span').first().locator('svg')).toHaveCount(1);
  } finally {
    await raw(api, 'DELETE', `/api/sensors/${s.id}`);
  }
});

test('Hebrew: page RTL, faceplate stays LTR (not mirrored)', async ({ page }) => {
  await loginViaApi(page);
  await openPorts(page, lab.aos.id);
  try {
    await setLang(page, 'he');
    await expect(page.getByTestId('faceplate')).toBeVisible();
    await expect(page.getByTestId('faceplate-scroll')).toHaveAttribute('dir', 'ltr');
    const [b1, b3, b9] = [await box(page, 1), await box(page, 3), await box(page, 9)];
    expect(b1.x).toBeLessThan(b3.x);
    expect(b3.x).toBeLessThan(b9.x);
    await expect(page.getByTestId('faceplate-legend')).toContainText(T('ports.legend.client', {}, 'he'));
    await expect(tile(page, 7)).toHaveAttribute('aria-label', new RegExp(T('ports.state.up', {}, 'he')));
    await shot2b2(page, 'faceplate-aos-he');
  } finally {
    await page.request.patch('/api/auth/me', { data: { language: 'en' } });
  }
});
