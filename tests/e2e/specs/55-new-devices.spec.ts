// [3] New device on the network (docs/architecture.md §13.4, §13.11 spec 55; api.md "[3] New-device alerts").
// Lab: tests/sim/newdev (access switch "newdev-access" on 127.0.0.90 — the contract's .88 is the 2b-3 LAG sim, .89 the 2b-3 v3 trap source).
// Enable with learning 0 days; run 1 baseline (first read of the switch), run 2 -> one alert for the Intel MAC
// with IP/hostname, the allow-listed HP OUI "allowed", locally administered / uplink / trunk MACs ignored, the
// flood guard (25 new MACs on port 5 -> 20 events + one summary), acknowledge as known clears the sensor, and
// the webhook got exactly one WARNING message for the new_mac sensor.
import { expect, test } from '@playwright/test';
import { join } from 'node:path';
import { apiAs, clearWebhookRecords, loginViaApi, webhookRecords, WEBHOOK_URL, type Api } from '../lib/helpers';
import { SIM_DIR, raw, snmpCreds } from '../lib/lab';
import { labSwap } from '../lib/lab2b';
import { refreshPorts } from '../lib/lab2b2';
import { PHASE3, ensureDeviceAt, ensureGroupNamed, setFeatures, shot3, tx } from '../lib/lab3';

test.describe.configure({ mode: 'serial' });
test.skip(!PHASE3, 'phase-3 simulators not running');

const HOST = '127.0.0.90';
const INTEL = '00:1b:21:aa:00:01';
const HP = '3c:a8:2a:00:00:01';
const LOCAL = '02:00:00:aa:00:02';
const UPLINK_NEW = '00:16:3e:89:09:99';
const TRUNK_NEW = '00:16:3e:89:0a:03';
const FLOOD = Array.from({ length: 25 }, (_, i) => `00:0e:c6:5f:00:${String(i + 1).padStart(2, '0')}`);
let api: Api;
let sw: any;
let sensorId = 0;
let sensorBeforeDetection = 0;

async function known(qs = ''): Promise<any[]> {
  return (await api.get(`/api/new-devices?page_size=500${qs}`)).items;
}
async function newMacSensor(): Promise<any> {
  const d = await api.get(`/api/devices/${sw.id}`);
  const sensors = d.sensors ?? (await api.get(`/api/sensors?device_id=${sw.id}&page_size=100`)).items;
  return sensors.find((s: any) => s.type === 'new_mac') ?? null;
}

test.beforeAll(async () => {
  api = await apiAs();
  labSwap('snmp-newdev-access', join(SIM_DIR, 'newdev', 'run1'));
  // 25 MACs on port 5 must stay an access port (the default threshold 5 would make it an uplink: QA note)
  await api.put('/api/settings', { uplink_mac_threshold: 30, newdev_learning_days: 0, newdev_ignore_random_macs: true });
  const g = await ensureGroupNamed(api, 'QA New devices 3');
  // the switch is also the ARP source of the lab (ARP `auto` is for routers only, 2b-2): arp_collect on
  sw = await ensureDeviceAt(api, g.id, HOST, 'newdev-access', { snmp_credentials: snmpCreds(), auto_sensors: ['snmp_system'], arp_collect: 'on' });
});

test.afterAll(async () => {
  labSwap('snmp-newdev-access', join(SIM_DIR, 'newdev', 'run1'));
  await api.put('/api/settings', { uplink_mac_threshold: 5 }).catch(() => undefined);
});

test('enable with learning 0 days; run 1 = baseline (first read of the switch, nothing alerted)', async () => {
  test.setTimeout(180_000);
  await setFeatures(api, { new_device_alerts: true });
  // spec 51 already turned the feature on once (learning_until = first ON + 7 days): restart with 0 days
  const st = await api.post('/api/new-devices/restart-learning');
  expect(st.learning, 'learning period 0 days').toBe(false);
  const ch = await api.post('/api/notification-channels', { name: 'qa3-newdev-hook', type: 'webhook', config: { url: `${WEBHOOK_URL}/qa3-newdev` } });
  // global rule (the sensor may only appear with the first condition); records are filtered by device name
  await api.post('/api/alert-rules', { name: 'qa3 new devices', enabled: true, sensor_id: null, on_statuses: ['warning'], delay_s: 0, channel_ids: [ch.id] });
  await clearWebhookRecords();
  // the inventory scheduler may already have a run of the new device in flight (started before the feature was ON,
  // so it does not detect); a second refresh is then the first FDB read "since ON"
  let dp = await refreshPorts(api, sw.id);
  if ((await known()).length === 0) dp = await refreshPorts(api, sw.id);
  expect(dp.status.ok ?? true).toBe(true);
  await expect.poll(async () => (await known()).length, { timeout: 20_000, message: 'baseline registered' }).toBeGreaterThan(0);
  const rows = await known();
  const macs = rows.map((r) => r.mac);
  for (const m of ['00:50:56:89:00:01', '3c:52:82:89:00:02', '00:23:24:89:00:04']) expect(macs, `baseline ${m}`).toContain(m);
  expect(rows.filter((r) => r.mac.startsWith('00:16:3e:89')), 'uplink/trunk MACs are never registered').toEqual([]);
  expect(rows.every((r) => r.state === 'baseline')).toBe(true);
  // §13.4: the passive new_mac sensor exists for every in-scope switch while ON - here the switch became in scope
  // (class detected, port inventory effective) after the feature was turned on; it must exist before any detection
  await expect.poll(async () => (await newMacSensor())?.id ?? 0, { timeout: 30_000 }).toBeGreaterThan(0).catch(() => undefined);
  sensorBeforeDetection = (await newMacSensor())?.id ?? 0;
  const s2 = await api.get('/api/new-devices/status');
  expect(s2.switches_baselined).toBeGreaterThanOrEqual(1);
  expect(s2.new_unacknowledged).toBe(0);
  const ns = await newMacSensor();
  if (ns) expect(ns.status).toBe('up');
});

test('run 2: Intel MAC alerted with IP + hostname, HP OUI allowed, random/uplink/trunk ignored, flood guard', async () => {
  test.setTimeout(180_000);
  const al = await api.post('/api/new-devices/allowlist', { prefix: '3C-A8-2A', comment: 'HP printers' });
  expect(al).toMatchObject({ prefix: '3c:a8:2a', bits: 24 });
  const dup = await raw(api, 'POST', '/api/new-devices/allowlist', { prefix: '3ca82a', comment: 'dup' });
  expect(dup.status).toBe(409);
  const inv = await raw(api, 'POST', '/api/new-devices/allowlist', { prefix: '3c:a8', comment: 'too short' });
  expect(inv.status).toBe(422);
  labSwap('snmp-newdev-access', join(SIM_DIR, 'newdev', 'run2'));
  await refreshPorts(api, sw.id);
  await expect.poll(async () => (await known('&state=new')).length, { timeout: 30_000, message: 'new MACs registered' }).toBe(26);
  const all = await known();
  const by = Object.fromEntries(all.map((r) => [r.mac, r]));
  expect(by[INTEL]).toMatchObject({
    state: 'new', first_device_name: 'newdev-access', first_port_name: '3', first_vlan: 20,
    first_ip: '10.0.20.57', first_hostname: 'pc-1234.mnidom.net', locally_administered: false,
  });
  expect(by[INTEL].vendor ?? '').toMatch(/intel/i);
  expect(by[INTEL].detected_at).not.toBeNull();
  expect(by[HP]?.state, 'HP OUI allow-listed').toBe('allowed');
  expect(by[LOCAL], 'locally administered MAC is not stored').toBeUndefined();
  expect(by[UPLINK_NEW], 'MAC on the uplink port is ignored').toBeUndefined();
  expect(by[TRUNK_NEW], 'MAC on the trunk port is ignored').toBeUndefined();
  for (const m of FLOOD) expect(by[m]?.state, `flood MAC ${m} stored new`).toBe('new');
  // flood guard counts per switch and run: 26 new (Intel + 25) -> the first 20 detailed + "+6 more" (the contract
  // does not define "first"; ports in ifIndex order puts the Intel MAC on port 3 among them)
  const ev = await api.get(`/api/events?kind=new_device&device_id=${sw.id}&page_size=100`);
  const msgs: string[] = (ev.items ?? ev).map((e: any) => e.message);
  expect(msgs.filter((m) => /^New device /.test(m))).toHaveLength(20);
  expect(msgs.filter((m) => /^\+6 more new devices on newdev-access/.test(m))).toHaveLength(1);
  const intelMsg = msgs.find((m) => m.includes(INTEL));
  expect(intelMsg, 'the Intel MAC is among the 20 detailed events (or the summary covers it)').toBeTruthy();
  expect(intelMsg).toMatch(/New device 00:1b:21:aa:00:01 \(Intel.*\) on newdev-access port 3, VLAN 20, 10\.0\.20\.57 pc-1234\.mnidom\.net/);
  await expect.poll(async () => (await newMacSensor())?.id ?? 0, { timeout: 30_000, message: 'new_mac sensor exists after detection' }).toBeGreaterThan(0);
  sensorId = (await newMacSensor()).id;
  const s = await api.get(`/api/sensors/${sensorId}`);
  expect(s.status).toBe('warning');
  const conds = await api.get(`/api/sensors/${sensorId}/conditions`);
  const nc = conds.items.filter((c: any) => c.source === 'new_mac').length;
  expect(nc, 'conditions for the detailed MACs (20) or all new MACs (26) - contract ambiguity').toBeGreaterThanOrEqual(20);
  expect(nc).toBeLessThanOrEqual(26);
  expect((await api.get('/api/new-devices/status')).new_unacknowledged).toBe(26);
  // webhook: exactly one WARNING notification for the sensor
  await expect.poll(async () => (await webhookRecords()).filter((r) => r.path === '/qa3-newdev' && JSON.stringify(r.body).includes('newdev-access')).length, { timeout: 30_000 }).toBe(1);
  const hook = (await webhookRecords()).find((r) => r.path === '/qa3-newdev' && JSON.stringify(r.body).includes('newdev-access'))!;
  expect(JSON.stringify(hook.body)).toMatch(/warning/i);
});

test('UI: Detected tab lists the Intel device ltr; acknowledge as known (bulk) clears the sensor', async ({ page }) => {
  test.setTimeout(120_000);
  await loginViaApi(page, undefined, '/new-devices');
  const row = page.getByRole('row').filter({ hasText: INTEL });
  await expect(row).toBeVisible({ timeout: 20_000 });
  await expect(row).toContainText('10.0.20.57');
  await expect(row).toContainText('pc-1234.mnidom.net');
  expect(await row.getByText(INTEL).evaluate((el) => getComputedStyle(el).direction)).toBe('ltr');
  await shot3(page, '55-new-devices');
  await row.getByRole('button', { name: tx('newDevices.acknowledge', /acknowledge as known/i) }).click();
  const dlg = page.getByRole('dialog');
  if (await dlg.isVisible().catch(() => false)) {
    const c = dlg.getByRole('textbox').first();
    if (await c.isVisible().catch(() => false)) await c.fill('New laptop for Dana');
    await dlg.getByRole('button', { name: /acknowledge|confirm|ok/i }).last().click();
  }
  await expect.poll(async () => (await known()).find((r) => r.mac === INTEL)?.state, { timeout: 20_000 }).toBe('known');
  const k = (await known()).find((r) => r.mac === INTEL)!;
  expect(k.ack_by).toBe('admin');
  // acknowledge the flood via the API -> sensor up
  const r = await api.post('/api/new-devices/acknowledge', { macs: FLOOD, comment: 'lab flood' });
  expect(r.acknowledged).toBe(25);
  await expect.poll(async () => (await api.get(`/api/sensors/${sensorId}`)).status, { timeout: 30_000 }).toBe('up');
  expect((await api.get('/api/new-devices/status')).new_unacknowledged).toBe(0);
  // forget -> detected again later; allow-list delete
  expect((await raw(api, 'DELETE', `/api/new-devices/${encodeURIComponent(INTEL)}`)).status).toBe(204);
  const al = (await api.get('/api/new-devices/allowlist')).items.find((x: any) => x.prefix === '3c:a8:2a');
  expect((await raw(api, 'DELETE', `/api/new-devices/allowlist/${al.id}`)).status).toBe(204);
});

test('turning the feature OFF removes new_mac conditions and 409s the API; data kept', async () => {
  await setFeatures(api, { new_device_alerts: false });
  expect((await raw(api, 'GET', '/api/new-devices')).status).toBe(409);
  await expect.poll(async () => (await api.get(`/api/sensors/${sensorId}`)).status, { timeout: 20_000 }).toBe('up');
  await setFeatures(api, { new_device_alerts: true });
  expect((await known()).length).toBeGreaterThan(20);
  await setFeatures(api, { new_device_alerts: false });
  // contract (§13.4): the passive new_mac sensor is "auto-created for in-scope switches while ON" - i.e. before
  // the first detection (checked last so a deviation does not skip the rest of this serial file)
  expect(sensorBeforeDetection, 'new_mac sensor existed after the baseline run (switch in scope, before any new MAC)').toBeGreaterThan(0);
});
