// [3b] Alert correlation and incidents (docs/architecture.md §14.8.2, §14.11 spec 69; api.md "[3b] AI features"):
// parent switch down + 4 dependents unreachable within the window -> one incident with the deterministic title
// "<parent> down — 4 devices unreachable", root = parent; the AI summary appears (light model, non-streamed,
// root_guess validated to be a member); with the mock unavailable the next incident has only the deterministic
// title; recovery -> resolved; feature OFF -> 409.
import { expect, test } from '@playwright/test';
import { type ChildProcess } from 'node:child_process';
import { apiAs, loginViaApi, uid, type Api } from '../lib/helpers';
import { raw, startHttpHost, stopHost } from '../lib/lab';
import { httpDevice, waitStatus } from '../lib/lab2b';
import { ensureGroupNamed, setFeatures } from '../lib/lab3';
import { PHASE3B, audit, ensureAssistant, lastSeq, mock, modelCalls, shot3b } from '../lib/lab3b';

// default mode (not serial): one worker runs the tests in order; a failure restarts the worker (beforeAll is
// idempotent) instead of skipping the remaining tests, so every contract item gets a verdict
test.describe.configure({ mode: 'default' });
test.skip(!PHASE3B, 'phase-3b Bedrock mock not running');

let api: Api;
const kids: ChildProcess[] = [];
type Dev = { device: any; sensor: any; ip: string };
let parent: Dev;
let incId = 0;
const devicesOf = (inc: any) => new Set((inc?.members ?? []).map((m: any) => m.device_id)).size;
const children: Dev[] = [];
const PARENT_IP = '127.0.0.140';
const CHILD_IPS = ['127.0.0.141', '127.0.0.142', '127.0.0.143', '127.0.0.144'];
const ctl = async (ip: string, a: 'ok' | 'fail') => { await fetch(`http://${ip}:8080/${a}`, { method: 'POST' }); };

async function incidentFor(deviceId: number, since: string): Promise<any> {
  const page = await api.get(`/api/incidents?since=${encodeURIComponent(since)}&page_size=50`);
  for (const i of page.items) {
    const full = await api.get(`/api/incidents/${i.id}`);
    if (full.members.some((m: any) => m.device_id === deviceId)) return full;
  }
  return null;
}

test.beforeAll(async () => {
  test.setTimeout(240_000);
  api = await apiAs();
  await ensureAssistant(api);
  await mock.clearModes();
  await setFeatures(api, { ai_correlation: true });
  await api.put('/api/settings', { correlation_window_min: 5, correlation_min_alerts: 3 });
  const g = await ensureGroupNamed(api, 'QA Correlation 3b');
  for (const ip of [PARENT_IP, ...CHILD_IPS]) kids.push(await startHttpHost(ip, 8080));
  const pd = await httpDevice(api, { name: uid('qa3b-core-sw'), ip: PARENT_IP, group_id: g.id });
  parent = { ...pd, ip: PARENT_IP };
  for (const ip of CHILD_IPS) {
    const c = await httpDevice(api, { name: uid(`qa3b-edge-${ip.split('.').pop()}`), ip, group_id: g.id,
      dependency: { mode: 'device', device_id: parent.device.id, sensor_id: parent.sensor.id } });
    children.push({ ...c, ip });
  }
  for (const d of [parent, ...children]) await waitStatus(api, d.sensor.id, 'up', 60_000);
});
test.afterAll(async () => {
  for (const d of [parent, ...children]) { if (d) { await ctl(d.ip, 'ok'); await api.patch(`/api/devices/${d.device.id}`, { enabled: false }).catch(() => undefined); } }
  for (const k of kids) stopHost(k);
});

test('parent down + 4 dependents unreachable -> one incident, deterministic title, root = parent, AI summary', async ({ page }) => {
  test.setTimeout(180_000);
  const since = new Date(Date.now() - 1000).toISOString();
  const seq = await lastSeq();
  await ctl(PARENT_IP, 'fail');
  for (const c of children) await ctl(c.ip, 'fail');
  await waitStatus(api, parent.sensor.id, 'down', 90_000);
  for (const c of children) await waitStatus(api, c.sensor.id, 'unreachable', 90_000);
  let inc: any = null;
  // members are problem transitions (a child may go down -> unreachable): count distinct devices
  await expect.poll(async () => { inc = await incidentFor(parent.device.id, since); return devicesOf(inc); }, { timeout: 60_000, message: 'incident with all 5 devices' }).toBe(5);
  await expect.poll(async () => (await api.get(`/api/incidents/${inc.id}`)).title, { timeout: 30_000 }).toBe(`${parent.device.name} down — 4 devices unreachable`);
  inc = await api.get(`/api/incidents/${inc.id}`);
  incId = inc.id;
  expect(inc.state).toBe('open');
  expect(inc.title).toBe(`${parent.device.name} down — 4 devices unreachable`);
  expect(inc.title_fields).toMatchObject({ root: parent.device.name, root_status: 'down', count: 4, unit: 'devices', status: 'unreachable' });
  expect(inc.root_device).toMatchObject({ id: parent.device.id });
  expect(inc.unreachable).toBe(4);
  expect(inc.down, 'root down (a child may have been down before the cascade)').toBeGreaterThanOrEqual(1);
  expect(inc.members.find((m: any) => m.is_root).device_id).toBe(parent.device.id);
  // exactly one incident for these devices
  const all = (await api.get(`/api/incidents?since=${encodeURIComponent(since)}&page_size=50`)).items;
  const mine = [];
  for (const i of all) { const f = await api.get(`/api/incidents/${i.id}`); if (f.members.some((m: any) => [parent, ...children].some((d) => d.device.id === m.device_id))) mine.push(i.id); }
  expect(mine.length).toBe(1);
  // AI summary (non-streamed, purpose incident, root guess must be a member)
  await expect.poll(async () => (await api.get(`/api/incidents/${inc.id}`)).ai, { timeout: 60_000, message: 'AI summary' }).not.toBeNull();
  const withAi = await api.get(`/api/incidents/${inc.id}`);
  expect(withAi.ai.summary.length).toBeGreaterThan(10);
  expect(withAi.ai.title.length).toBeLessThanOrEqual(80);
  if (withAi.ai.root_guess) expect(withAi.members.map((m: any) => m.device_id)).toContain(withAi.ai.root_guess.device_id);
  const calls = (await modelCalls(seq)).filter((c) => c.purpose === 'incident');
  expect(calls.length).toBeGreaterThanOrEqual(1);
  expect(calls[0].op, 'background features are non-streamed').toBe('converse');
  expect(JSON.stringify(calls[0].body)).toContain(parent.device.name);
  expect((await audit(api, 'action=assistant.incident_summary'))[0].actor_type).toBe('system');
  // UI: dashboard card and /incidents/:id
  await loginViaApi(page, undefined, '/');
  const card = page.getByTestId('incident-card').filter({ hasText: parent.device.name }).first();
  await expect(card).toContainText(/4 devices unreachable/i, { timeout: 30_000 });
  await expect(card.getByTestId('incident-ai')).toBeVisible();
  await shot3b(page, '69-incident-card');
  await page.goto(`/incidents/${inc.id}`);
  // members are problem transitions (a child can be down, then unreachable): ≥ one row per device
  await expect.poll(async () => page.getByTestId('incident-member').count(), { timeout: 20_000 }).toBeGreaterThanOrEqual(5);
  await expect(page.getByRole('button', { name: /Acknowledge all/i })).toHaveCount(0);
});

test('recovery -> incident resolved', async () => {
  test.setTimeout(150_000);
  await ctl(PARENT_IP, 'ok');
  for (const c of children) await ctl(c.ip, 'ok');
  for (const d of [parent, ...children]) await waitStatus(api, d.sensor.id, 'up', 90_000);
  test.skip(!incId, 'needs the incident of the previous test');
  await expect.poll(async () => (await api.get(`/api/incidents/${incId}`)).state, { timeout: 60_000 }).toBe('resolved');
});

test('mock unavailable -> the next incident has the deterministic title only; alerts unaffected', async ({ page }) => {
  test.setTimeout(180_000);
  await mock.mode('unavailable');
  const since = new Date(Date.now() - 1000).toISOString();
  await ctl(PARENT_IP, 'fail');
  for (const c of children.slice(0, 3)) await ctl(c.ip, 'fail');
  await waitStatus(api, parent.sensor.id, 'down', 90_000);
  let inc: any = null;
  await expect.poll(async () => { inc = await incidentFor(parent.device.id, since); return devicesOf(inc); }, { timeout: 90_000 }).toBe(4);
  expect(inc.title).toMatch(new RegExp(`^${parent.device.name} down — \\d+ devices? unreachable$`));
  await new Promise((r) => setTimeout(r, 8000));
  expect((await api.get(`/api/incidents/${inc.id}`)).ai).toBeNull();
  await loginViaApi(page, undefined, `/incidents/${inc.id}`);
  await expect(page.getByTestId('incident-ai')).toHaveCount(0);
  await mock.clearModes();
  await ctl(PARENT_IP, 'ok');
  for (const c of children) await ctl(c.ip, 'ok');
});

test('graph refresh: devices/dependencies added after the graph was built are correlated at once', async () => {
  // contract §14.8.2: "graph cached, refreshed on topology/dependency changes" (not only by a TTL)
  test.setTimeout(240_000);
  const ips = ['127.0.0.150', '127.0.0.151', '127.0.0.152', '127.0.0.153'];
  // 1) a transition on an existing device makes the listener build (and cache) the graph now
  await ctl(PARENT_IP, 'fail');
  await waitStatus(api, parent.sensor.id, 'down', 90_000);
  await ctl(PARENT_IP, 'ok');
  await waitStatus(api, parent.sensor.id, 'up', 90_000);
  // 2) within the 60 s cache TTL: new devices with dependencies, then their outage
  for (const ip of ips) kids.push(await startHttpHost(ip, 8080));
  const g = await ensureGroupNamed(api, 'QA Correlation 3b');
  const p2 = await httpDevice(api, { name: uid('qa3b-core2'), ip: ips[0], group_id: g.id });
  const c2 = [];
  for (const ip of ips.slice(1)) c2.push(await httpDevice(api, { name: uid('qa3b-edge2'), ip, group_id: g.id,
    dependency: { mode: 'device', device_id: p2.device.id, sensor_id: p2.sensor.id } }));
  for (const d of [p2, ...c2]) await waitStatus(api, d.sensor.id, 'up', 60_000);
  const since = new Date(Date.now() - 1000).toISOString();
  for (const ip of ips) await ctl(ip, 'fail');
  await waitStatus(api, p2.sensor.id, 'down', 90_000);
  let inc: any = null;
  try {
    await expect.poll(async () => { inc = await incidentFor(p2.device.id, since); return devicesOf(inc); }, { timeout: 45_000, message: 'incident for devices created < 60 s ago' }).toBe(4);
  } finally {
    for (const ip of ips) await ctl(ip, 'ok');
    for (const d of [p2, ...c2]) await api.patch(`/api/devices/${d.device.id}`, { enabled: false }).catch(() => undefined);
  }
});

test('feature OFF -> /api/incidents 409; incidents kept', async () => {
  await setFeatures(api, { ai_correlation: false });
  const r = await raw(api, 'GET', '/api/incidents');
  expect(r.status).toBe(409);
  expect(r.body.error.code).toBe('feature_disabled');
  await setFeatures(api, { ai_correlation: true });
  expect((await api.get('/api/incidents?page_size=5')).items.length).toBeGreaterThan(0);
});

