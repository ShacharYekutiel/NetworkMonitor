import { expect, test } from '@playwright/test';
import { Api, apiAs } from '../lib/helpers';
import { raw } from '../lib/lab';
import { hooksForSensors, quiet, resetClock, setClock } from '../lib/lab2b';
import { createWebhook } from '../lib/lab2b';
import { ensurePortLab, type PortLab } from '../lib/lab2b2';
import {
  OID, SRC, builtinRule, conditions, featureOff, featureOn, linkVarbinds, marker, sendTrap, sleep, waitEventSensor, waitItems,
  waitReceivers,
} from '../lib/lab2b3';

// [2b-3] 43 — port flapping (architecture.md §12.4, data-model § Port flapping): 6 linkDown/linkUp pairs within
// 2 min on an important port -> one port_flap warning (message "Port 1 is flapping (N changes in 10 min)"),
// Link-down conditions of that port removed, no per-link conditions while flapping, the webhook gets one
// message for the port_flap sensor; quiet period (test clock) -> recovery.
// Owner decision (coordinator): flapping follows `event_linkdown_scope` -> with "important" a flapping plain
// access port (7) raises nothing; with "all" it does.
test.describe.configure({ mode: 'serial' });

const HOOK = '/qa43';
let admin: Api;
let lab: PortLab;

function flap(ifIndex: number, pairs: number, tag: string): void {
  for (let i = 0; i < pairs; i++) {
    sendTrap({ from: SRC.aos, oid: OID.linkDown, varbinds: [...linkVarbinds(ifIndex, false), `1.3.6.1.2.1.31.1.1.1.18.${ifIndex}=s:${tag}`] });
    sendTrap({ from: SRC.aos, oid: OID.linkUp, varbinds: [...linkVarbinds(ifIndex, true), `1.3.6.1.2.1.31.1.1.1.18.${ifIndex}=s:${tag}`] });
  }
}

test.beforeAll(async () => {
  admin = await apiAs();
  lab = await ensurePortLab(admin);
  await featureOn(admin, 'traps', 'event_alerts', 'flapping');
  await waitReceivers(admin, ['trap'], 'listening');
  const ld = await builtinRule(admin, 'trap_link_down');
  if (!ld.enabled) await admin.patch(`/api/event-rules/${ld.id}`, { enabled: true });
  await raw(admin, 'PUT', '/api/settings', { flap_threshold: 5, flap_window_min: 10, flap_quiet_min: 15, event_linkdown_scope: 'important' });
  const hook = await createWebhook(admin, HOOK);
  await admin.post('/api/alert-rules', { name: 'qa43-all', sensor_id: null, on_statuses: ['warning', 'down', 'up'], delay_s: 0, enabled: true, channel_ids: [hook] });
});

test.afterAll(async () => {
  await resetClock(admin);
  await raw(admin, 'PUT', '/api/settings', { event_linkdown_scope: 'important' });
  const r = (await admin.get('/api/alert-rules?page_size=1000')).items.find((x: any) => x.name === 'qa43-all');
  if (r) await admin.del(`/api/alert-rules/${r.id}`);
});

test('settings validation', async () => {
  for (const b of [{ flap_threshold: 2 }, { flap_threshold: 51 }, { flap_window_min: 0 }, { flap_quiet_min: 1441 }]) {
    expect.soft((await raw(admin, 'PUT', '/api/settings', b)).status, JSON.stringify(b)).toBe(422);
  }
});

test('6 down/up pairs on port 1 -> one port_flap warning, link-down condition removed, one webhook message', async () => {
  const tag = marker('qa43');
  flap(1, 6, tag);
  await waitItems(admin, 'traps', { q: tag }, (it) => it.length === 12, 20_000, '12 transitions stored');
  const pf = await waitEventSensor(admin, lab.aos.id, 'port_flap', (s) => s.status === 'warning');
  expect(pf.name).toBe('Port flapping');
  const c = await conditions(admin, pf.id);
  expect(c).toHaveLength(1);
  expect(c[0]).toMatchObject({ source: 'flap', key: '1', status: 'warning' });
  expect(c[0].message).toMatch(/^Port 1 is flapping \(\d+ changes in 10 min\)$/);
  expect(c[0].count).toBeGreaterThanOrEqual(5);
  // no per-link condition for ifIndex 1 while flapping
  const trap = await waitEventSensor(admin, lab.aos.id, 'trap');
  await quiet(1500);
  expect((await conditions(admin, trap.id)).filter((x) => x.key === '1')).toEqual([]);
  // more transitions while flapping: still nothing per link, the flap condition counts up
  flap(1, 1, tag);
  await expect.poll(async () => (await conditions(admin, pf.id))[0]?.count, { timeout: 10_000 }).toBeGreaterThan(c[0].count);
  expect((await conditions(admin, trap.id)).filter((x) => x.key === '1')).toEqual([]);
  const recs = await hooksForSensors([pf.id]);
  expect(recs.map((r) => r.body.new_status)).toEqual(['warning']);
});

test('quiet period (test clock +16 min) -> flapping ends, port_flap recovers, one recovery message', async () => {
  const pf = await waitEventSensor(admin, lab.aos.id, 'port_flap');
  await setClock(admin, 16 * 60);
  try {
    await expect.poll(async () => (await admin.get(`/api/sensors/${pf.id}`)).status, { timeout: 45_000 }).toBe('up');
    expect(await conditions(admin, pf.id)).toEqual([]);
    // keep the shifted clock until the recovery is sent: the send is scheduled at the (shifted) event time
    await expect.poll(async () => (await hooksForSensors([pf.id])).map((r) => r.body.new_status), { timeout: 20_000 }).toEqual(['warning', 'up']);
  } finally {
    await resetClock(admin);
  }
  // after flapping, a single linkDown raises Link down again
  sendTrap({ from: SRC.aos, oid: OID.linkDown, varbinds: linkVarbinds(1, false) });
  const trap = await waitEventSensor(admin, lab.aos.id, 'trap');
  await expect.poll(async () => (await conditions(admin, trap.id)).some((x) => x.key === '1'), { timeout: 15_000 }).toBe(true);
  await raw(admin, 'POST', `/api/sensors/${trap.id}/clear`);
});

test('scope important: flapping access port 7 raises nothing; scope all: it flaps', async () => {
  const pf = await waitEventSensor(admin, lab.aos.id, 'port_flap');
  flap(7, 6, marker('qa43p7'));
  await quiet(3000);
  expect((await conditions(admin, pf.id)).filter((c) => c.key === '7')).toEqual([]);
  await raw(admin, 'PUT', '/api/settings', { event_linkdown_scope: 'all' });
  await sleep(500);
  // fresh window: transitions after the scope change
  flap(7, 6, marker('qa43p7all'));
  await expect.poll(async () => (await conditions(admin, pf.id)).some((c) => c.key === '7'), { timeout: 15_000, message: 'port 7 flapping with scope all' }).toBe(true);
  await raw(admin, 'PUT', '/api/settings', { event_linkdown_scope: 'important' });
});

test('flapping OFF clears flap conditions (sensor up)', async () => {
  const pf = await waitEventSensor(admin, lab.aos.id, 'port_flap');
  if ((await conditions(admin, pf.id)).length === 0) flap(1, 6, marker('qa43off'));
  await expect.poll(async () => (await conditions(admin, pf.id)).length, { timeout: 15_000 }).toBeGreaterThan(0);
  await featureOff(admin, 'flapping');
  await expect.poll(async () => (await admin.get(`/api/sensors/${pf.id}`)).status, { timeout: 15_000 }).toBe('up');
  expect(await conditions(admin, pf.id)).toEqual([]);
  await featureOn(admin, 'flapping');
});
