import { expect, test } from '@playwright/test';
import { createSocket } from 'node:dgram';
import { createConnection } from 'node:net';
import { Api, VIEWER, apiAs, loginViaApi } from '../lib/helpers';
import { ensureViewer, raw } from '../lib/lab';
import {
  FEATURES, OID, SRC, SYSLOG_PORT, TRAP_PORT, expectFeatureDisabled, featureOff, featureOn, linkVarbinds, marker,
  publicFeatures, sendSyslog, sendTrap, setFeatures, shot2b3, sleep, tOr, trapPage, waitItems, waitReceivers,
} from '../lib/lab2b3';

// [2b-3] 38 — feature toggles (architecture.md §12.0, data-model § Feature toggles, api.md [2b-3]):
// fresh install: all 11 toggles OFF, nav hidden, endpoints 409 feature_disabled; each toggle ON/OFF without a
// restart (traps: the sender gets through only while ON; sockets closed while OFF).
test.describe.configure({ mode: 'default' });

let admin: Api;
let viewer: Api;

test.beforeAll(async () => {
  admin = await apiAs();
  await ensureViewer(admin);
  viewer = await apiAs(VIEWER);
});

test.afterAll(async () => {
  // later specs enable what they need themselves; leave everything OFF like a fresh install
  await setFeatures(admin, Object.fromEntries(FEATURES.map((f) => [f, false])));
});

test('fresh install: every feature OFF in settings and /api/settings/public (also for viewers and anonymous)', async () => {
  const s = await admin.get('/api/settings');
  for (const f of FEATURES) expect.soft(s[`feature_${f}_enabled`], `settings feature_${f}_enabled`).toBe(false);
  const pub = await publicFeatures(admin);
  // [3] the 8 phase-3 toggles are listed too (api.md [3] Type changes)
  expect(Object.keys(pub).sort()).toEqual([...FEATURES, ...['config_backup', 'flows', 'new_device_alerts', 'sso_kerberos', 'sso_oidc', 'reports', 'noc', 'chat_ack']].sort());
  for (const f of FEATURES) expect.soft(pub[f], `public features.${f}`).toBe(false);
  expect((await viewer.get('/api/settings/public')).features).toEqual(pub);
  const anon = await fetch(`${process.env.BASE_URL}/api/settings/public`);
  expect(anon.status).toBe(200);
  const a = await anon.json();
  expect(a.features).toEqual(pub);
  expect(a.ldap ?? null).toBeNull(); // null while ldap is OFF
  // 2b-3 defaults of the other keys (data-model § Settings keys)
  expect.soft(s).toMatchObject({
    trap_retention_days: 30, syslog_retention_days: 30, audit_retention_days: 365, trap_community_check: 'any',
    trap_communities: null, trap_communities_count: 0, ingest_unknown_sources: 'store', ingest_ignored_sources: [],
    flap_threshold: 5, flap_window_min: 10, flap_quiet_min: 15, siem_targets: [], trap_v3_users: [],
  });
  expect.soft(s.ldap).toMatchObject({ servers: [], security: 'ldaps', tls_verify: true, timeout_s: 5, bind_password: null, bind_password_set: false });
});

test('OFF: endpoints answer 409 feature_disabled (with the documented exceptions)', async () => {
  const dev = (await admin.get('/api/devices?page_size=1')).items[0];
  await expectFeatureDisabled(admin, 'GET', '/api/traps', 'traps');
  await expectFeatureDisabled(admin, 'GET', '/api/traps/stats', 'traps');
  await expectFeatureDisabled(admin, 'GET', '/api/traps/1', 'traps');
  await expectFeatureDisabled(admin, 'GET', '/api/syslog', 'syslog');
  await expectFeatureDisabled(admin, 'GET', '/api/syslog/stats', 'syslog');
  await expectFeatureDisabled(viewer, 'GET', '/api/syslog', 'syslog');
  // ingest + event rules: 409 only when both traps and syslog are OFF (the detail names one of them)
  for (const p of ['/api/ingest/status', '/api/ingest/unknown-sources', '/api/event-rules']) {
    const r = await raw(admin, 'GET', p);
    expect.soft(r.status, p).toBe(409);
    expect.soft(r.body?.error?.code, p).toBe('feature_disabled');
  }
  await expectFeatureDisabled(admin, 'GET', '/api/audit', 'audit');
  await expectFeatureDisabled(admin, 'GET', '/api/audit/export.csv', 'audit');
  if (dev) await expectFeatureDisabled(admin, 'GET', `/api/devices/${dev.id}/timeline`, 'timeline');
  await expectFeatureDisabled(admin, 'GET', '/api/tokens', 'api_tokens');
  await expectFeatureDisabled(admin, 'POST', '/api/tokens', 'api_tokens', { name: 'x', role: 'viewer', expires_days: 1 });
  await expectFeatureDisabled(admin, 'POST', '/api/notification-channels', 'teams', {
    name: 'teams-off', type: 'teams', enabled: true, config: { url: 'https://prod-1.westeurope.logic.azure.com/workflows/x' },
  });
  // siem_forwarding OFF -> forwarder test 409 (ingest itself is also off here: either way 409 feature_disabled)
  const fw = await raw(admin, 'POST', '/api/ingest/forwarders/test', { target: { name: 'x', host: '127.0.0.1', port: 1, protocol: 'udp' } });
  expect.soft(fw.status).toBe(409);
  // exception: the LDAP test endpoints work while ldap is OFF (they must not answer 409)
  const lt = await raw(admin, 'POST', '/api/ldap/test', {});
  expect.soft(lt.status, `POST /api/ldap/test while OFF: ${lt.text.slice(0, 200)}`).not.toBe(409);
  // exception: a bearer token while api_tokens is OFF -> 401 unauthenticated (not 409)
  const ctx = (await fetch(`${process.env.BASE_URL}/api/devices`, { headers: { Authorization: 'Bearer nmt_' + 'x'.repeat(43) } }));
  expect.soft(ctx.status).toBe(401);
});

test('OFF: no listener on the trap/syslog ports; health reports receivers disabled', async () => {
  const h = await admin.get('/api/health');
  // [3] ingest also reports the flow receivers (api.md [3] Type changes)
  expect.soft(h.ingest).toEqual({ trap: 'disabled', syslog_udp: 'disabled', syslog_tcp: 'disabled', netflow: 'disabled', ipfix: 'disabled', sflow: 'disabled' });
  expect.soft(typeof h.trgm).toBe('boolean');
  // TCP syslog: connection refused
  const refused = await new Promise<boolean>((res) => {
    const c = createConnection({ host: '127.0.0.1', port: SYSLOG_PORT });
    c.on('connect', () => { c.destroy(); res(false); });
    c.on('error', () => res(true));
  });
  expect(refused, `TCP ${SYSLOG_PORT} must refuse while syslog is OFF`).toBe(true);
  // UDP: we can bind the trap and syslog ports ourselves, i.e. nobody holds them
  for (const port of [TRAP_PORT, SYSLOG_PORT]) {
    const free = await new Promise<boolean>((res) => {
      const s = createSocket('udp4');
      s.once('error', () => res(false));
      s.bind(port, '127.0.0.1', () => s.close(() => res(true)));
    });
    expect.soft(free, `UDP ${port} free while OFF`).toBe(true);
  }
});

test('traps: ON without restart -> received and stored; OFF -> socket closed, nothing stored; ON again -> old data visible', async () => {
  const m = marker('qa38');
  await featureOn(admin, 'traps');
  expect((await publicFeatures(admin)).traps).toBe(true);
  await waitReceivers(admin, ['trap'], 'listening');
  sendTrap({ from: SRC.unknown, oid: OID.linkDown, varbinds: [...linkVarbinds(38, false), `1.3.6.1.2.1.31.1.1.1.18.38=s:${m}-on`] });
  await waitItems(admin, 'traps', { q: `${m}-on` }, (it) => it.length === 1, 15_000, 'trap sent while ON is stored');

  await featureOff(admin, 'traps');
  await expectFeatureDisabled(admin, 'GET', '/api/traps', 'traps');
  // the port is released (we can bind it), so a trap sent now cannot be received
  await expect
    .poll(async () => new Promise<boolean>((res) => {
      const s = createSocket('udp4');
      s.once('error', () => res(false));
      s.bind(TRAP_PORT, '127.0.0.1', () => s.close(() => res(true)));
    }), { timeout: 6_000, message: 'trap socket closed within 5 s' })
    .toBe(true);
  sendTrap({ from: SRC.unknown, oid: OID.linkDown, varbinds: [`1.3.6.1.2.1.31.1.1.1.18.38=s:${m}-off`] });
  await sleep(1500);

  await featureOn(admin, 'traps');
  await waitReceivers(admin, ['trap'], 'listening');
  const all = await trapPage(admin, { q: m, limit: 50 });
  expect(all.items.map((t: any) => t.summary.includes(`${m}-on`) ? 'on' : 'off')).toEqual(['on']);
  await featureOff(admin, 'traps');
});

test('syslog: ON opens UDP+TCP, OFF closes them (TCP refused), toggle broadcast reaches the SPA without reload', async ({ page }) => {
  await loginViaApi(page, VIEWER);
  await page.evaluate(() => ((window as any).__e2eNoReload = true));
  const logsNav = page.getByRole('navigation').first().getByRole('link', { name: tOr('nav.logs', 'Logs') });
  await expect(logsNav).toHaveCount(0);
  await featureOn(admin, 'syslog');
  await waitReceivers(admin, ['syslog_udp', 'syslog_tcp'], 'listening');
  // features.changed -> the viewer's SPA shows the Logs nav entry without a reload
  await expect(logsNav).toBeVisible({ timeout: 10_000 });
  expect(await page.evaluate(() => (window as any).__e2eNoReload === true)).toBe(true);
  const m = marker('qa38s');
  sendSyslog({ tcp: true, messages: [`<13>Nov  8 12:00:00 sw qa: ${m}`] });
  await waitItems(admin, 'syslog', { q: m }, (it) => it.length === 1 && it[0].transport === 'tcp');
  await featureOff(admin, 'syslog');
  await expect(logsNav).toHaveCount(0, { timeout: 10_000 });
  const refused = await new Promise<boolean>((res) => {
    const c = createConnection({ host: '127.0.0.1', port: SYSLOG_PORT });
    c.on('connect', () => { c.destroy(); res(false); });
    c.on('error', () => res(true));
  });
  expect(refused).toBe(true);
});

test('each feature toggles ON and OFF at runtime (settings + public + features.changed)', async () => {
  for (const f of FEATURES) {
    if (f === 'ldap') continue; // turning ldap ON validates the stored config (45-ad-login covers it)
    await featureOn(admin, f);
    expect.soft((await publicFeatures(viewer))[f], `${f} ON`).toBe(true);
    await featureOff(admin, f);
    expect.soft((await publicFeatures(viewer))[f], `${f} OFF`).toBe(false);
  }
  // ldap ON without a config -> 422 naming the missing fields, toggle stays OFF
  const r = await raw(admin, 'PUT', '/api/settings', { feature_ldap_enabled: true });
  expect.soft(r.status, r.text).toBe(422);
  expect((await publicFeatures(admin)).ldap).toBe(false);
});

test('port in use (e.g. the Windows SNMPTRAP service holds UDP 162): toggle stays ON, receiver error + system event, server keeps running', async () => {
  // architecture.md §12.0 / §12.15: a failing start leaves the toggle ON and reports the error
  const blocker = createSocket('udp4');
  await new Promise<void>((res, rej) => { blocker.once('error', rej); blocker.bind(TRAP_PORT, '127.0.0.1', () => res()); });
  try {
    await featureOn(admin, 'traps');
    expect((await publicFeatures(admin)).traps).toBe(true);
    const st = await waitReceivers(admin, ['trap'], 'error');
    const rec = st.receivers.find((r: any) => r.kind === 'trap');
    // §12.2.1: the state carries the OS message ("[Errno 98] Address already in use"; Windows: WinError 10048)
    expect.soft(rec.error, 'receiver error includes the OS reason').toMatch(/in use|Errno 98|10048/i);
    expect((await admin.get('/api/health')).ingest.trap).toBe('error');
    const ev = await admin.get('/api/events?kind=system&page_size=20');
    expect(ev.items.some((e: any) => /cannot listen on UDP/i.test(e.message))).toBe(true);
  } finally {
    await featureOff(admin, 'traps');
    await new Promise<void>((res) => blocker.close(() => res()));
  }
  // OFF then ON after the port is free -> listening again (no restart)
  await featureOn(admin, 'traps');
  await waitReceivers(admin, ['trap'], 'listening');
  await featureOff(admin, 'traps');
});

test('viewer cannot change toggles (403)', async () => {
  const r = await raw(viewer, 'PUT', '/api/settings', { feature_traps_enabled: true });
  expect(r.status).toBe(403);
  expect((await publicFeatures(admin)).traps).toBe(false);
});

test('UI: Settings -> Features tab lists the 11 features; toggling one works; disabled features hide nav entries', async ({ page }) => {
  await loginViaApi(page);
  const nav = page.getByRole('navigation').first();
  await expect(nav.getByRole('link', { name: tOr('nav.logs', 'Logs') })).toHaveCount(0);
  await expect(nav.getByRole('link', { name: tOr('nav.audit', 'Audit') })).toHaveCount(0);
  await page.goto('/settings');
  await page.getByRole('tab', { name: tOr('settings.tab.features', 'Features') }).click();
  const toggles = page.getByRole('switch').or(page.getByRole('checkbox'));
  await expect(toggles).toHaveCount(19); // 11 (2b-3) + 8 (phase 3)
  await shot2b3(page, '38-features-tab-off');
  await page.getByRole('switch', { name: tOr('features.name.audit', 'Audit log') }).click();
  await expect.poll(async () => (await publicFeatures(admin)).audit, { timeout: 10_000 }).toBe(true);
  await expect(nav.getByRole('link', { name: tOr('nav.audit', 'Audit') })).toBeVisible();
  await featureOff(admin, 'audit');
  await expect(nav.getByRole('link', { name: tOr('nav.audit', 'Audit') })).toHaveCount(0, { timeout: 10_000 });
});
