import { expect, test } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { ensureGroup, raw, snmpCreds } from '../lib/lab';
import { PORTS_GROUP, devicePorts, openPorts, portOf, refreshPorts, search, tile } from '../lib/lab2b2';
import { SRC, deviceByHost, featureOff, featureOn, labLogText, shot2b3, tOr } from '../lib/lab2b3';

// [2b-3] 44 — LAG membership in the ports view (architecture.md §12.6 + §12.14 expected values).
// Lab addresses: contract .86 (ArubaOS LAG) is 127.0.0.88 here (.86 is the 2b-2 52-port sim), contract .87 is
// 127.0.0.87. Manual link .87 ifIndex 49 <-> .88 ifIndex 3.
test.describe.configure({ mode: 'serial' });

let admin: Api;
let aos: any; // contract .86
let cw: any; // contract .87

async function ensureLagDevice(host: string, name: string): Promise<any> {
  const gid = await ensureGroup(admin, PORTS_GROUP);
  let d = await deviceByHost(admin, host);
  if (!d) d = await admin.post('/api/devices', { group_id: gid, name, host, snmp_credentials: snmpCreds(), auto_sensors: ['ping', 'snmp_system'] });
  d = await admin.get(`/api/devices/${d.id}`);
  // the uplink rule needs the neighbour's effective class (switch): identify like the 2b-2 lab does
  if (!d.identity) {
    const r = await raw(admin, 'POST', `/api/devices/${d.id}/identify`);
    expect(r.status, r.text).toBe(200);
    d = await admin.get(`/api/devices/${d.id}`);
  }
  expect(d.effective_class ?? d.identity?.device_class, `${name} class`).toBe('switch');
  return d;
}

test.beforeAll(async () => {
  admin = await apiAs();
  await featureOn(admin, 'lag');
  aos = await ensureLagDevice(SRC.lagAos, 'lag-aos');
  cw = await ensureLagDevice(SRC.lagCw, 'lag-cw');
  const links = (await admin.get(`/api/topology/links?device_id=${cw.id}&page_size=1000`)).items;
  if (!links.some((l: any) => [l.a?.device_id ?? l.a_device_id, l.b?.device_id ?? l.b_device_id].includes(aos.id))) {
    const r = await raw(admin, 'POST', '/api/topology/links', { a_device_id: cw.id, a_if_index: 49, b_device_id: aos.id, b_neighbor_id: null, b_if_index: 3, b_port: '3' });
    expect([201, 409], r.text).toContain(r.status);
  }
  await refreshPorts(admin, aos.id);
  await refreshPorts(admin, cw.id);
});

test.afterAll(async () => {
  await featureOn(admin, 'lag');
});

test('ArubaOS (LAG-MIB): members 3/4 of Trk1 inherit VLANs/MACs/uplink; Trk1 lists members', async () => {
  const dp = await devicePorts(admin, aos.id);
  expect(dp.status.sources.lag).toBe('ok');
  expect(dp.status.lag_mib).toBe('ieee8023');
  const trk = dp.ports.find((p: any) => p.name === 'Trk1');
  expect(trk).toMatchObject({ if_index: 11, lag_members: [3, 4], lag: null });
  expect.soft(trk.is_uplink, 'Trk1 uplink via the neighbour of member 3 (neighbours(L) = own links ∪ members\' links)').toBe(true);
  const p3 = portOf(dp, 3);
  expect(p3).toMatchObject({ lag: { if_index: 11, name: 'Trk1', attached: true }, vlans_from_lag: true, mode: 'trunk', untagged_vlans: [1], tagged_vlans: [10, 20], mac_count: 2, is_uplink: true, connected_kind: 'switch', macs: [] });
  expect.soft(p3.uplink_reason).toBe('neighbor');
  expect.soft(p3.neighbor?.device_id ?? p3.neighbor?.id).toBe(cw.id);
  const p4 = portOf(dp, 4);
  expect(p4).toMatchObject({ lag: { if_index: 11, name: 'Trk1', attached: true }, vlans_from_lag: true, untagged_vlans: [1], tagged_vlans: [10, 20], is_uplink: true });
  expect.soft(p4.uplink_reason).toBe('neighbor');
  expect.soft(p4.neighbor?.device_id ?? p4.neighbor?.id).toBe(cw.id);
  const p1 = portOf(dp, 1);
  expect(p1).toMatchObject({ lag: null, lag_members: [], vlans_from_lag: false, mac_count: 1 });
});

test('Comware (ifStackTable only): 49/50 members of Bridge-Aggregation1, attached null, via_lag MAC', async () => {
  const dp = await devicePorts(admin, cw.id);
  expect(dp.status.lag_mib).toBe('if_stack');
  const p49 = portOf(dp, 49);
  expect(p49).toMatchObject({ lag: { if_index: 600, name: 'Bridge-Aggregation1', attached: null }, mode: 'trunk', untagged_vlans: [1], tagged_vlans: [30], mac_count: 1, is_uplink: true });
  expect.soft(p49.uplink_reason).toBe('neighbor');
  const viaLag = (p49.macs ?? []).concat(p49.first_mac ? [p49.first_mac] : []);
  if (viaLag.length) expect.soft(viaLag[0]).toMatchObject({ via_lag: true, vlan: 30 });
  const p50 = portOf(dp, 50);
  expect(p50.lag).toMatchObject({ if_index: 600, name: 'Bridge-Aggregation1' });
  expect(p50.oper_status ?? p50.state).toMatch(/down/);
  expect(portOf(dp, 600).lag_members).toEqual([49, 50]);
});

test('MAC search: a MAC learned on Trk1 shows port Trk1 with lag_members', async () => {
  const r = await search(admin, { q: '02:00:00:00:86:01' });
  expect(r.status).toBe(200);
  const it = r.body.items.find((x: any) => (x.device_id ?? x.device?.id) === aos.id);
  expect(it).toMatchObject({ port: 'Trk1', lag_members: ['3', '4'] });
});

test('UI: faceplate bars + legend, list chips, LAG members line, modal "Member of Trk1"', async ({ page }) => {
  await loginViaApi(page);
  await openPorts(page, aos.id);
  await expect(page.getByTestId('faceplate')).toContainText('Trk1: 3, 4');
  await expect(tile(page, 3)).toHaveAttribute('aria-label', /member of Trk1/);
  await shot2b3(page, '44-lag-faceplate');
  await tile(page, 3).click();
  const modal = page.getByTestId('port-modal');
  await expect(modal).toContainText(/member of Trk1/i);
  await page.keyboard.press('Escape');
  await openPorts(page, aos.id, 'list');
  const list = page.getByTestId('port-list');
  await expect(list.getByRole('row', { name: /^3 Trk1\b/ })).toBeVisible();
  await expect(list.getByRole('row', { name: /^1 / })).not.toContainText('Trk1');
  await expect(list).toContainText('Members: 3, 4');
  await shot2b3(page, '44-lag-list');
});

test('lag OFF: fields null/[]/false, members show their own (empty) VLANs, walks #13/#14 not sent', async ({ page }) => {
  await featureOff(admin, 'lag');
  const offset = labLogText('snmp-lag-aos').length;
  await refreshPorts(admin, aos.id);
  const dp = await devicePorts(admin, aos.id);
  for (const p of dp.ports) {
    expect.soft(p.lag, `port ${p.if_index}`).toBeNull();
    expect.soft(p.lag_members, `port ${p.if_index}`).toEqual([]);
    expect.soft(p.vlans_from_lag, `port ${p.if_index}`).toBe(false);
  }
  expect(portOf(dp, 3)).toMatchObject({ untagged_vlans: [], tagged_vlans: [] });
  const log = labLogText('snmp-lag-aos').slice(offset);
  expect(log.length, 'snmpsim logged the refresh').toBeGreaterThan(0);
  expect(log).not.toContain('1.2.840.10006.300.43.1.2.1.1.13');
  expect(log).not.toContain('1.3.6.1.2.1.31.1.2.1.3');
  await loginViaApi(page);
  await openPorts(page, aos.id);
  await expect(page.getByTestId('faceplate')).not.toContainText('Trk1: 3, 4');
  await featureOn(admin, 'lag');
});
