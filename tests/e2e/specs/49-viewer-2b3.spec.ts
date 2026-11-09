import { expect, test } from '@playwright/test';
import { Api, VIEWER, apiAs, loginViaApi } from '../lib/helpers';
import { ensureViewer, raw } from '../lib/lab';
import { ensurePortLab, type PortLab } from '../lib/lab2b2';
import { SRC, eventSensor, featureOn, marker, sendSyslog, tOr, waitItems, waitReceivers } from '../lib/lab2b3';

// [2b-3] 49 — viewer (architecture.md §12, api.md [2b-3] roles): reads logs/timeline/rules/ingest status (no
// v3 failures / forwarders), no rules edit / assign / ignore / clear, no audit, own tokens with role viewer only.
test.describe.configure({ mode: 'default' });

let admin: Api;
let viewer: Api;
let lab: PortLab;

test.beforeAll(async () => {
  admin = await apiAs();
  lab = await ensurePortLab(admin);
  await ensureViewer(admin);
  viewer = await apiAs(VIEWER);
  await featureOn(admin, 'traps', 'syslog', 'event_alerts', 'timeline', 'audit');
  await waitReceivers(admin, ['syslog_udp'], 'listening');
});

test('viewer reads syslog, traps, stats, rules, ingest status, unknown sources, timeline, conditions', async () => {
  const m = marker('qa49');
  sendSyslog({ from: SRC.aos, messages: [`<14>Nov  8 12:00:00 sw v: ${m}`] });
  await waitItems(viewer, 'syslog', { q: m }, (it) => it.length === 1);
  for (const p of ['/api/traps', '/api/syslog/stats', '/api/traps/stats', '/api/event-rules', '/api/ingest/unknown-sources', `/api/devices/${lab.aos.id}/timeline`]) {
    expect.soft((await raw(viewer, 'GET', p)).status, p).toBe(200);
  }
  const st = await viewer.get('/api/ingest/status');
  expect(st.v3_failures).toEqual([]);
  expect(st.forwarders).toEqual([]);
  // audit kind never in a viewer's timeline
  expect((await viewer.get(`/api/devices/${lab.aos.id}/timeline?limit=200`)).items.some((i: any) => i.kind === 'audit')).toBe(false);
  const s = await eventSensor(admin, lab.aos.id, 'syslog');
  if (s) expect((await raw(viewer, 'GET', `/api/sensors/${s.id}/conditions`)).status).toBe(200);
});

test('viewer writes are refused (403)', async () => {
  const rule = (await admin.get('/api/event-rules')).items[0];
  const s = await eventSensor(admin, lab.aos.id, 'syslog');
  const checks: [string, string, unknown?][] = [
    ['POST', '/api/event-rules', { name: 'x', kind: 'syslog', scope: 'global', action: 'ignore', match: { severity_max: null, facilities: [], programs: [], text: 'x', text_regex: false } }],
    ['PATCH', `/api/event-rules/${rule.id}`, { enabled: !rule.enabled }],
    ['DELETE', `/api/event-rules/${rule.id}`],
    ['POST', `/api/event-rules/${rule.id}/reset`],
    ['POST', '/api/ingest/unknown-sources/assign', { source_ip: '127.0.0.99', device_id: lab.aos.id }],
    ['PUT', '/api/settings', { ingest_ignored_sources: ['127.0.0.99'] }],
    ['POST', '/api/ingest/forwarders/test', { target_id: 'abcdef' }],
    ['POST', '/api/ldap/test', {}],
    ['POST', '/api/ldap/test-user', { username: 'jdoe', password: 'x' }],
    ['GET', '/api/audit'],
    ['GET', '/api/audit/export.csv'],
    ...(s ? ([['POST', `/api/sensors/${s.id}/clear`]] as [string, string][]) : []),
  ];
  for (const [mth, p, b] of checks) {
    const r = await raw(viewer, mth, p, b);
    expect.soft(r.status, `${mth} ${p}`).toBe(403);
  }
});

test('viewer tokens: own only, role viewer; admin role refused', async () => {
  const t = await viewer.post('/api/tokens', { name: `qa49-${marker()}`, role: 'viewer', expires_days: 1 });
  expect(t.item.role).toBe('viewer');
  expect((await raw(viewer, 'POST', '/api/tokens', { name: 'adm', role: 'admin', expires_days: 1 })).status).toBe(422);
  const va = await raw(viewer, 'GET', '/api/tokens?all=true');
  expect([200, 403]).toContain(va.status); // `all` is an admin option
  const mine = (await viewer.get('/api/tokens')).items;
  expect(mine.every((x: any) => x.user_id === t.item.user_id)).toBe(true);
  const adminTok = (await admin.post('/api/tokens', { name: `qa49-a-${marker()}`, role: 'admin', expires_days: 1 })).item;
  expect([403, 404]).toContain((await raw(viewer, 'DELETE', `/api/tokens/${adminTok.id}`)).status); // not the owner
  expect((await admin.get('/api/tokens')).items.find((x: any) => x.id === adminTok.id).state).toBe('active');
  await admin.del(`/api/tokens/${adminTok.id}`);
  await viewer.del(`/api/tokens/${t.item.id}`);
});

test('viewer UI: Logs visible, no Rules editing / Assign / Ignore, no Audit nav, sensor without Clear', async ({ page }) => {
  await loginViaApi(page, VIEWER, '/logs');
  await expect(page.getByRole('tab', { name: tOr('logs.tab.syslog', 'Syslog') })).toBeVisible();
  await expect(page.getByRole('navigation').first().getByRole('link', { name: tOr('nav.audit', 'Audit') })).toHaveCount(0);
  await page.getByRole('tab', { name: tOr('logs.tab.unknown', 'Unknown sources') }).click();
  await expect(page.getByRole('button', { name: tOr('unknownSources.assign', 'Assign to device') })).toHaveCount(0);
  await expect(page.getByRole('button', { name: tOr('unknownSources.ignore', 'Ignore') })).toHaveCount(0);
  await page.getByRole('tab', { name: tOr('logs.tab.rules', 'Rules') }).click();
  await expect(page.getByRole('button', { name: tOr('rules.new', 'New rule') })).toHaveCount(0);
  await page.goto('/audit');
  await expect(page.getByRole('table')).toHaveCount(0);
  const s = await eventSensor(admin, lab.aos.id, 'syslog');
  if (s) {
    await page.goto(`/sensors/${s.id}`);
    await expect(page.getByRole('button', { name: tOr('events.clear', 'Clear') })).toHaveCount(0);
  }
});
