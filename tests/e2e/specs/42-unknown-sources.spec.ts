import { expect, test } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { raw } from '../lib/lab';
import { ensurePortLab, type PortLab } from '../lib/lab2b2';
import { OID, featureOn, marker, sendSyslog, sendTrap, shot2b3, sleep, syslogPage, tOr, waitItems, waitReceivers } from '../lib/lab2b3';

// [2b-3] 42 — unknown sources (architecture.md §12.2.5, api.md [2b-3] Ingest): 127.0.0.95 -> unknown list ->
// "Assign to device" -> stored rows re-attributed + next messages attributed; ignore list -> ignored_source
// counter; policy `drop` -> unknown_source counter. (Own addresses .93-.95 so .99 stays unknown for other specs.)
test.describe.configure({ mode: 'serial' });

const UNK = '127.0.0.95';
const IGN = '127.0.0.94';
const DROP = '127.0.0.93';
let admin: Api;
let lab: PortLab;
let dev: any;

test.beforeAll(async () => {
  admin = await apiAs();
  lab = await ensurePortLab(admin);
  await featureOn(admin, 'traps', 'syslog');
  await waitReceivers(admin, ['trap', 'syslog_udp'], 'listening');
  const existing = (await admin.get('/api/devices?q=qa42-dev&page_size=100')).items.find((d: any) => d.name === 'qa42-dev');
  if (existing) await admin.del(`/api/devices/${existing.id}`);
  dev = await admin.post('/api/devices', { group_id: lab.groupId, name: 'qa42-dev', host: '127.0.0.92', auto_sensors: [] });
});

test.afterAll(async () => {
  await raw(admin, 'PUT', '/api/settings', { ingest_unknown_sources: 'store', ingest_ignored_sources: [] });
  if (dev) await raw(admin, 'DELETE', `/api/devices/${dev.id}`);
});

test('messages from an unknown IP are stored with device_id null and listed under unknown sources', async () => {
  const m = marker('qa42');
  sendSyslog({ from: UNK, count: 3, messages: [`<14>Nov  8 12:00:00 stray-host app: ${m} {i}`] });
  sendTrap({ from: UNK, oid: OID.coldStart, varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}`] });
  await waitItems(admin, 'syslog', { q: m, unknown: true }, (it) => it.length === 3);
  await waitItems(admin, 'traps', { q: m, unknown: true }, (it) => it.length === 1);
  const u = (await admin.get('/api/ingest/unknown-sources')).items.find((x: any) => x.source_ip === UNK);
  expect(u).toMatchObject({ source_ip: UNK, last_hostname: 'stray-host', ignored: false });
  expect(u.syslog_count).toBeGreaterThanOrEqual(3);
  expect(u.trap_count).toBeGreaterThanOrEqual(1);
  expect((await raw(admin, 'GET', '/api/ingest/unknown-sources?days=31')).status).toBe(422);
});

test('UI: Unknown sources tab lists the IP with Assign / Ignore', async ({ page }) => {
  await loginViaApi(page, undefined, '/logs');
  await page.getByRole('tab', { name: tOr('logs.tab.unknown', 'Unknown sources') }).click();
  const row = page.getByRole('row').filter({ hasText: UNK });
  await expect(row).toBeVisible();
  await expect(row.getByRole('button', { name: tOr('unknownSources.assign', 'Assign to device') })).toBeVisible();
  await expect(row.getByRole('button', { name: tOr('unknownSources.ignore', 'Ignore') })).toBeVisible();
  await shot2b3(page, '42-unknown-sources');
});

test('assign to device: source_addresses appended, stored rows re-attributed, next messages attributed', async () => {
  const bad = await raw(admin, 'POST', '/api/ingest/unknown-sources/assign', { source_ip: 'not-an-ip', device_id: dev.id });
  expect(bad.status).toBe(422);
  expect((await raw(admin, 'POST', '/api/ingest/unknown-sources/assign', { source_ip: UNK, device_id: 999999 })).status).toBe(404);
  const r = await admin.post('/api/ingest/unknown-sources/assign', { source_ip: UNK, device_id: dev.id });
  expect(r).toMatchObject({ device_id: dev.id, source_addresses: [UNK], reassigning: true });
  expect((await raw(admin, 'POST', '/api/ingest/unknown-sources/assign', { source_ip: UNK, device_id: dev.id })).status).toBe(422);
  expect((await admin.get(`/api/devices/${dev.id}`)).source_addresses).toEqual([UNK]);
  await expect.poll(async () => (await syslogPage(admin, { source_ip: UNK, unknown: true })).items.length, { timeout: 30_000, message: 'old rows re-attributed' }).toBe(0);
  expect((await syslogPage(admin, { source_ip: UNK, device_id: dev.id })).items.length).toBeGreaterThanOrEqual(3);
  const m = marker('qa42next');
  sendSyslog({ from: UNK, messages: [`<14>Nov  8 12:00:00 stray-host app: ${m}`] });
  const [x] = await waitItems(admin, 'syslog', { q: m }, (it) => it.length === 1);
  expect(x.device_id).toBe(dev.id);
  expect((await admin.get('/api/ingest/unknown-sources')).items.find((u: any) => u.source_ip === UNK && !u.ignored)).toBeFalsy();
});

test('source_addresses validation and conflicts warning', async () => {
  expect((await raw(admin, 'PATCH', `/api/devices/${dev.id}`, { source_addresses: ['10.0.0.0/24'] })).status).toBe(422);
  expect((await raw(admin, 'PATCH', `/api/devices/${dev.id}`, { source_addresses: Array.from({ length: 17 }, (_, i) => `10.9.9.${i + 1}`) })).status).toBe(422);
  const p = await admin.patch(`/api/devices/${dev.id}`, { source_addresses: [UNK, lab.aos.host] });
  expect(p.source_address_conflicts).toEqual([expect.objectContaining({ address: lab.aos.host, device_id: lab.aos.id })]);
  await admin.patch(`/api/devices/${dev.id}`, { source_addresses: [UNK] });
});

test('ignore list: dropped before parsing (ignored_source), nothing stored', async () => {
  const before = (await admin.get('/api/ingest/status')).counters.syslog.dropped.ignored_source;
  await admin.put('/api/settings', { ingest_ignored_sources: [IGN, '10.9.0.0/16'] });
  const m = marker('qa42ign');
  sendSyslog({ from: IGN, count: 4, messages: [`<14>Nov  8 12:00:00 h app: ${m}`] });
  await expect.poll(async () => (await admin.get('/api/ingest/status')).counters.syslog.dropped.ignored_source - before, { timeout: 10_000 }).toBe(4);
  expect((await syslogPage(admin, { q: m })).items).toHaveLength(0);
  expect((await raw(admin, 'PUT', '/api/settings', { ingest_ignored_sources: ['nonsense'] })).status).toBe(422);
});

test('policy drop: unknown sources are counted, not stored; store again afterwards', async () => {
  await admin.put('/api/settings', { ingest_unknown_sources: 'drop' });
  const before = (await admin.get('/api/ingest/status')).counters.syslog.dropped.unknown_source;
  const m = marker('qa42drop');
  sendSyslog({ from: DROP, count: 2, messages: [`<14>Nov  8 12:00:00 h app: ${m}`] });
  await expect.poll(async () => (await admin.get('/api/ingest/status')).counters.syslog.dropped.unknown_source - before, { timeout: 10_000 }).toBe(2);
  await sleep(1000);
  expect((await syslogPage(admin, { q: m })).items).toHaveLength(0);
  // a known device still gets through
  const k = marker('qa42known');
  sendSyslog({ from: UNK, messages: [`<14>Nov  8 12:00:00 h app: ${k}`] });
  await waitItems(admin, 'syslog', { q: k }, (it) => it.length === 1);
  await admin.put('/api/settings', { ingest_unknown_sources: 'store' });
});

test('device delete keeps its history as unknown source (device_id set NULL in background)', async () => {
  await admin.del(`/api/devices/${dev.id}`);
  const deleted = dev;
  dev = null;
  await expect.poll(async () => (await syslogPage(admin, { source_ip: UNK })).items.every((x: any) => x.device_id === null), { timeout: 30_000 }).toBe(true);
  expect((await syslogPage(admin, { source_ip: UNK })).items.length).toBeGreaterThan(0);
  void deleted;
});
