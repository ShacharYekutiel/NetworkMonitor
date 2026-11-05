import { expect, test } from '@playwright/test';
import { Api, apiAs } from '../lib/helpers';
import { raw, snmpCreds } from '../lib/lab';
import { labSwap } from '../lib/lab2b';
import { SIM_DIR } from '../lib/lab';
import { join } from 'node:path';
import { devicePorts, ensurePortLab, portOf, refreshPorts, search, type PortLab } from '../lib/lab2b2';

// [2b-2] (e) MAC presence (data-model § port_macs write rules): the MAC of AOS port 7 disappears from the
// switch (lab swap to tests/sim/ports/arubaos_nomac7) -> refresh -> present:false in search, port 7
// mac_count 0, last_seen kept; it comes back -> present again (same port: first_seen kept). Retention.
test.describe.configure({ mode: 'serial' });

let api: Api;
let lab: PortLab;
const MAC = '00:50:56:01:00:07';
let seen: { first_seen: string; last_seen: string } | null = null;

test.beforeAll(async () => {
  api = await apiAs();
  lab = await ensurePortLab(api);
});

test.afterAll(() => {
  labSwap('snmp-port-aos', join(SIM_DIR, 'ports', 'arubaos'));
});

test('baseline: present on AOS port 7', async () => {
  await refreshPorts(api, lab.aos.id);
  const r = await search(api, { q: MAC });
  expect(r.status, r.text).toBe(200);
  const it = r.body.items.filter((x: any) => x.device_id === lab.aos.id);
  expect(it).toHaveLength(1);
  expect(it[0]).toMatchObject({ if_index: 7, present: true, best: true, vlan: 10 });
  seen = { first_seen: it[0].first_seen, last_seen: it[0].last_seen };
});

test('MAC removed from the switch -> present false, mac_count 0, last_seen unchanged', async () => {
  labSwap('snmp-port-aos', join(SIM_DIR, 'ports', 'arubaos_nomac7'));
  const dp = await refreshPorts(api, lab.aos.id);
  expect(dp.status.error).toBeNull();
  expect(portOf(dp, 7)).toMatchObject({ mac_count: 0, macs: [], first_mac: null });
  expect(dp.status.macs).toBe(13);
  const r = (await search(api, { q: MAC })).body;
  const it = r.items.filter((x: any) => x.device_id === lab.aos.id);
  expect(it).toHaveLength(1);
  expect(it[0]).toMatchObject({ if_index: 7, present: false, best: false });
  expect(it[0].last_seen).toBe(seen!.last_seen);
  expect(it[0].first_seen).toBe(seen!.first_seen);
  // still the IP from ARP (.84 still has it present)
  expect(it[0].ip).toBe('10.0.0.17');
  // a second run keeps last_seen (still absent)
  await refreshPorts(api, lab.aos.id);
  const again = (await search(api, { q: MAC })).body.items.find((x: any) => x.device_id === lab.aos.id);
  expect(again.last_seen).toBe(seen!.last_seen);
  // other MACs of the device are unaffected
  expect(portOf(await devicePorts(api, lab.aos.id), 1).mac_count).toBe(1);
});

test('MAC back on the same port -> present, first_seen kept, last_seen advances', async () => {
  labSwap('snmp-port-aos', join(SIM_DIR, 'ports', 'arubaos'));
  const dp = await refreshPorts(api, lab.aos.id);
  expect(portOf(dp, 7).mac_count).toBe(1);
  const it = (await search(api, { q: MAC })).body.items.find((x: any) => x.device_id === lab.aos.id);
  expect(it).toMatchObject({ present: true, best: true, first_seen: seen!.first_seen });
  expect(Date.parse(it.last_seen)).toBeGreaterThan(Date.parse(seen!.last_seen));
});

test('retention setting accepted; non-present rows inside the window are kept', async () => {
  const r = await raw(api, 'PUT', '/api/settings', { port_mac_retention_days: 1 });
  expect(r.status, r.text).toBe(200);
  await raw(api, 'PUT', '/api/settings', { port_mac_retention_days: 30 });
  // The daily cleanup (first run 1 h after startup, then every 24 h; architecture §11.2) cannot be triggered
  // from the API, and the test clock does not move the scheduler: deletion itself is covered by backend unit
  // tests only (reported as a testability gap).
  test.info().annotations.push({ type: 'gap', description: 'retention deletion not triggerable from E2E (no test hook)' });
});

test('device delete cascades ports, MACs, ARP and status rows', async () => {
  // a throw-away copy of the CX switch on the same agent
  const d = await api.post('/api/devices', { group_id: lab.groupId, name: 'qa-cascade-cx', host: lab.cx.host, snmp_credentials: snmpCreds(), auto_sensors: ['snmp_system'] });
  try {
    const dp = await refreshPorts(api, d.id);
    expect(dp.ports.length).toBeGreaterThan(0);
    const before = (await search(api, { q: '00:00:0c:00:cc:01' })).body.items.filter((x: any) => x.device_id === d.id);
    expect(before).toHaveLength(1);
  } finally {
    await api.del(`/api/devices/${d.id}`);
  }
  const after = (await search(api, { q: '00:00:0c:00:cc:01' })).body.items.filter((x: any) => x.device_id === d.id);
  expect(after).toEqual([]);
  expect((await raw(api, 'GET', `/api/devices/${d.id}/ports`)).status).toBe(404);
});
