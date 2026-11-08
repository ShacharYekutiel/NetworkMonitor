import { expect, test } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { raw } from '../lib/lab';
import { ensurePortLab, type PortLab } from '../lib/lab2b2';
import {
  OID, SRC, deviceByHost, featureOn, linkVarbinds, marker, sendTrap, shot2b3, tOr, trapPage, waitItems, waitReceivers,
} from '../lib/lab2b3';

// [2b-3] 40 — SNMP traps (architecture.md §12.2.3, data-model § OID name table, api.md [2b-3] Traps):
// v1 / v2c / v2c inform / v3 (device credentials, settings trap user, two engine IDs for one user, wrong key,
// unknown user -> v3 failure card), varbind names and rendering, agent-addr / snmpTrapAddress attribution,
// community check `known`.
test.describe.configure({ mode: 'default' });

let admin: Api;
let lab: PortLab;
let v3dev: any;
const V3 = { user: 'netmon-dev', auth: 'SHA', authKey: 'devAuthKey123', priv: 'AES128', privKey: 'devPrivKey123' };
const TRAP_USER = { username: 'netmon-trap', security_level: 'authPriv', auth_protocol: 'SHA256', auth_key: 'trapAuthKey123', priv_protocol: 'AES256', priv_key: 'trapPrivKey123' };

const vb = (t: any, oidPrefix: string) => t.varbinds.find((v: any) => v.oid === oidPrefix || v.oid.startsWith(`${oidPrefix}.`));

test.beforeAll(async () => {
  admin = await apiAs();
  lab = await ensurePortLab(admin);
  await featureOn(admin, 'traps');
  await waitReceivers(admin, ['trap'], 'listening');
  v3dev = await deviceByHost(admin, SRC.v3dev);
  if (!v3dev) {
    v3dev = await admin.post('/api/devices', {
      group_id: lab.groupId, name: 'trap-v3-dev', host: SRC.v3dev, auto_sensors: [],
      snmp_credentials: { version: 'v3', port: 1161, timeout_s: 1, retries: 0, v3_username: V3.user, v3_security_level: 'authPriv',
        v3_auth_protocol: V3.auth, v3_auth_key: V3.authKey, v3_priv_protocol: V3.priv, v3_priv_key: V3.privKey },
    });
  }
});

test('v2c linkDown from 127.0.0.81 -> attributed, names, display enums, summary with port name', async () => {
  const m = marker('qa40');
  const r = sendTrap({ from: SRC.aos, oid: OID.linkDown, varbinds: [...linkVarbinds(5, false), `1.3.6.1.2.1.31.1.1.1.18.5=s:${m}`, '1.3.6.1.2.1.2.2.1.6.5=x:001a1e006105'], uptime: 4242 });
  expect(r.rc).toBe(0);
  const [t] = await waitItems(admin, 'traps', { q: m }, (it) => it.length === 1);
  expect(t).toMatchObject({
    source_ip: SRC.aos, device_id: lab.aos.id, device_name: lab.aos.name, version: 'v2c', pdu: 'trap',
    trap_oid: OID.linkDown, trap_name: 'linkDown', uptime_ticks: 4242, agent_addr: null, security_name: null,
  });
  // sysUpTime.0 and snmpTrapOID.0 are taken out of the varbind list
  expect(t.varbinds.map((v: any) => v.oid)).not.toContain('1.3.6.1.2.1.1.3.0');
  expect(t.varbinds.map((v: any) => v.oid)).not.toContain('1.3.6.1.6.3.1.1.4.1.0');
  expect.soft(vb(t, '1.3.6.1.2.1.2.2.1.1')).toMatchObject({ name: 'ifIndex.5', value: '5' });
  expect.soft(vb(t, '1.3.6.1.2.1.2.2.1.7')).toMatchObject({ name: 'ifAdminStatus.5', value: '1', display: 'up(1)' });
  expect.soft(vb(t, '1.3.6.1.2.1.2.2.1.8')).toMatchObject({ name: 'ifOperStatus.5', value: '2', display: 'down(2)' });
  expect.soft(vb(t, '1.3.6.1.2.1.31.1.1.1.18')).toMatchObject({ name: 'ifAlias.5', value: m });
  expect.soft(vb(t, '1.3.6.1.2.1.2.2.1.6')?.value, '6-byte OctetString -> MAC').toBe('00:1a:1e:00:61:05');
  for (const v of t.varbinds) expect.soft(typeof v.type === 'string' && v.type.length > 0, `type of ${v.oid}`).toBe(true);
  expect.soft(t.summary.startsWith('linkDown ')).toBe(true);
  expect.soft(t.summary).toContain('down(2)');
  expect.soft(t.summary, 'port name appended (switch_ports row for ifIndex 5)').toMatch(/ port=5$/);
  expect.soft(t.summary.length).toBeLessThanOrEqual(1000);
  expect(await admin.get(`/api/traps/${t.id}`)).toEqual(t);
});

test('v1 generic + enterprise-specific traps: RFC 3584 conversion, agent_addr, snmpTrapEnterprise.0', async () => {
  const m = marker('qa40v1');
  sendTrap({ from: SRC.aos, version: 'v1', oid: OID.coldStart, enterprise: '1.3.6.1.4.1.11.2.3.7.11', varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}-gen`], uptime: 77 });
  sendTrap({ from: SRC.aos, version: 'v1', oid: '1.3.6.1.4.1.25506.2.6.2.0.1', varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}-ent`] });
  const items = await waitItems(admin, 'traps', { q: m }, (it) => it.length === 2);
  const gen = items.find((t) => t.summary.includes(`${m}-gen`));
  const ent = items.find((t) => t.summary.includes(`${m}-ent`));
  expect(gen).toMatchObject({ version: 'v1', trap_oid: OID.coldStart, trap_name: 'coldStart', agent_addr: SRC.aos, uptime_ticks: 77, device_id: lab.aos.id });
  expect.soft(vb(gen, '1.3.6.1.6.3.1.1.4.3.0')).toMatchObject({ name: 'snmpTrapEnterprise.0', value: '1.3.6.1.4.1.11.2.3.7.11' });
  expect.soft(vb(gen, '1.3.6.1.2.1.1.5.0')).toMatchObject({ name: 'sysName.0' });
  expect(ent.trap_oid).toBe('1.3.6.1.4.1.25506.2.6.2.0.1');
  expect.soft(ent.trap_name, 'vendor root name + remainder').toBe('h3c.2.6.2.0.1');
});

test('attribution: v1 agent_addr and snmpTrapAddress.0 when the source IP is no device', async () => {
  const m = marker('qa40attr');
  sendTrap({ from: SRC.unknown, version: 'v1', oid: OID.linkUp, agentAddr: '127.0.0.82', varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}-agent`] });
  sendTrap({ from: SRC.unknown, oid: OID.linkUp, varbinds: [`${OID.snmpTrapAddress}=a:127.0.0.82`, `1.3.6.1.2.1.1.5.0=s:${m}-relay`] });
  sendTrap({ from: SRC.unknown, oid: OID.linkUp, varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}-none`] });
  const items = await waitItems(admin, 'traps', { q: m }, (it) => it.length === 3);
  const by = (s: string) => items.find((t) => t.summary.includes(s));
  expect(by(`${m}-agent`)).toMatchObject({ source_ip: SRC.unknown, agent_addr: '127.0.0.82', device_id: lab.cw.id });
  expect(by(`${m}-relay`)).toMatchObject({ source_ip: SRC.unknown, device_id: lab.cw.id });
  expect(by(`${m}-none`)).toMatchObject({ source_ip: SRC.unknown, device_id: null, device_name: null });
  expect((await trapPage(admin, { q: m, unknown: true })).items).toHaveLength(1);
  expect((await trapPage(admin, { q: m, device_id: lab.cw.id })).items).toHaveLength(2);
});

test('v2c INFORM is answered with a Response echoing request-id and varbinds; stored as pdu inform', async () => {
  const m = marker('qa40inf');
  const r = sendTrap({ from: SRC.aos, oid: OID.warmStart, inform: true, varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}`] });
  expect(r.rc, `inform answered: ${JSON.stringify(r.out)}`).toBe(0);
  const out = r.out[0];
  expect(out.response.pdu).toBe('ResponsePDU');
  expect(out.response.request_id).toBe(out.request_id);
  expect(out.response.varbinds.map((v: any) => v[0])).toEqual(['1.3.6.1.2.1.1.3.0', '1.3.6.1.6.3.1.1.4.1.0', '1.3.6.1.2.1.1.5.0']);
  const [t] = await waitItems(admin, 'traps', { q: m }, (it) => it.length === 1);
  expect(t).toMatchObject({ pdu: 'inform', version: 'v2c', trap_name: 'warmStart' });
});

test('v3 with the device credentials: authPriv SHA/AES128, two engine IDs for one user', async () => {
  const m = marker('qa40v3');
  for (const eid of ['0x8000000001020304', '0x80000000010203aa']) {
    const r = sendTrap({ from: SRC.v3dev, version: 'v3', oid: OID.linkDown, engineId: eid, ...V3, varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}-${eid}`] });
    expect(r.out[0].error).toBeNull();
  }
  const items = await waitItems(admin, 'traps', { q: m }, (it) => it.length === 2, 20_000, 'two v3 traps (distinct engine IDs)');
  for (const t of items) expect(t).toMatchObject({ version: 'v3', security_name: V3.user, device_id: v3dev.id, trap_name: 'linkDown' });
});

test('v3 with a settings trap user (SHA256/AES256) from an unknown source; authNoPriv', async () => {
  const s = await raw(admin, 'PUT', '/api/settings', { trap_v3_users: [TRAP_USER, { username: 'netmon-anp', security_level: 'authNoPriv', auth_protocol: 'SHA', auth_key: 'anpAuthKey123', priv_protocol: null, priv_key: null }] });
  expect(s.status, s.text).toBe(200);
  const users = s.body.trap_v3_users;
  expect(users[0]).toMatchObject({ username: 'netmon-trap', auth_key: null, auth_key_set: true, priv_key: null, priv_key_set: true });
  const m = marker('qa40tu');
  sendTrap({ from: SRC.unknown, version: 'v3', oid: OID.authFailure, user: TRAP_USER.username, auth: 'SHA256', authKey: TRAP_USER.auth_key, priv: 'AES256', privKey: TRAP_USER.priv_key, engineId: '0x80000000aabbccdd01', varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}-ap`] });
  sendTrap({ from: SRC.unknown, version: 'v3', oid: OID.authFailure, user: 'netmon-anp', auth: 'SHA', authKey: 'anpAuthKey123', engineId: '0x80000000aabbccdd02', varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}-anp`] });
  const items = await waitItems(admin, 'traps', { q: m }, (it) => it.length === 2);
  expect(items.map((t) => t.security_name).sort()).toEqual(['netmon-anp', 'netmon-trap']);
  expect(items.every((t) => t.device_id === null && t.trap_name === 'authenticationFailure')).toBe(true);
  // keys kept when the list is re-sent with null keys (secret rules)
  const again = await raw(admin, 'PUT', '/api/settings', { trap_v3_users: [{ ...TRAP_USER, auth_key: null, priv_key: null }] });
  expect(again.body.trap_v3_users[0]).toMatchObject({ auth_key_set: true, priv_key_set: true });
});

test('v3 wrong key and unknown user -> v3_auth counter + v3 failures (engine ID, username, reason)', async ({ page }) => {
  const before = (await admin.get('/api/ingest/status')).counters.trap.dropped.v3_auth;
  const m = marker('qa40bad');
  sendTrap({ from: SRC.v3dev, version: 'v3', oid: OID.linkDown, engineId: '0x80000000010203bb', ...V3, authKey: 'WRONGwrong123', varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}`] });
  sendTrap({ from: SRC.aos, version: 'v3', oid: OID.linkDown, engineId: '0x80000000010203cc', user: 'nobody-here', auth: 'SHA', authKey: 'whatever123', priv: 'AES128', privKey: 'whatever123', varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}`] });
  let st: any;
  await expect.poll(async () => { st = await admin.get('/api/ingest/status'); return st.counters.trap.dropped.v3_auth - before; }, { timeout: 10_000 }).toBeGreaterThanOrEqual(2);
  expect((await trapPage(admin, { q: m })).items).toHaveLength(0);
  const f = st.v3_failures;
  expect.soft(f.find((x: any) => x.username === 'nobody-here')).toMatchObject({ source_ip: SRC.aos, engine_id: expect.stringMatching(/80000000010203cc/i) });
  expect.soft(f.find((x: any) => x.username === V3.user)).toMatchObject({ source_ip: SRC.v3dev, engine_id: expect.stringMatching(/80000000010203bb/i) });
  expect.soft(f.length).toBeLessThanOrEqual(20);
  // viewers never see v3 failures
  // (v3_failures/forwarders are admins-only: [] for viewers; checked in 49-viewer-2b3)
  await loginViaApi(page, undefined, '/settings');
  await page.getByRole('tab', { name: tOr('settings.tab.ingest', 'Traps & syslog') }).click();
  await expect(page.getByText('nobody-here')).toBeVisible();
  await shot2b3(page, '40-v3-failures-card');
});

test('community check "known": unknown community dropped (bad_community), device community accepted', async () => {
  const m = marker('qa40comm');
  await raw(admin, 'PUT', '/api/settings', { trap_community_check: 'known', trap_communities: ['extra-comm'] });
  try {
    const before = (await admin.get('/api/ingest/status')).counters.trap.dropped.bad_community;
    sendTrap({ from: SRC.aos, oid: OID.linkUp, community: 'not-known', varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}-bad`] });
    sendTrap({ from: SRC.aos, oid: OID.linkUp, community: 'public', varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}-dev`] });
    sendTrap({ from: SRC.unknown, oid: OID.linkUp, community: 'extra-comm', varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}-extra`] });
    const items = await waitItems(admin, 'traps', { q: m }, (it) => it.length === 2);
    expect(items.map((t) => t.summary.includes('-bad'))).toEqual([false, false]);
    await expect.poll(async () => (await admin.get('/api/ingest/status')).counters.trap.dropped.bad_community - before, { timeout: 5_000 }).toBe(1);
    const s = await admin.get('/api/settings');
    expect(s.trap_communities).toBeNull();
    expect(s.trap_communities_count).toBe(1);
  } finally {
    await raw(admin, 'PUT', '/api/settings', { trap_community_check: 'any', trap_communities: [] });
  }
});

test('trap stats and filters (trap_oid prefix, source_ip, q)', async () => {
  const m = marker('qa40st');
  sendTrap({ from: SRC.aos, oid: OID.linkDown, repeat: 3, varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}`] });
  await waitItems(admin, 'traps', { q: m }, (it) => it.length === 3);
  expect((await trapPage(admin, { q: m, trap_oid: '1.3.6.1.6.3.1.1.5' })).items).toHaveLength(3);
  expect((await trapPage(admin, { q: m, trap_oid: OID.linkUp })).items).toHaveLength(0);
  expect((await trapPage(admin, { q: m, source_ip: SRC.aos })).items).toHaveLength(3);
  const st = await admin.get(`/api/traps/stats?q=${m}`);
  expect(st.total).toBe(3);
  expect(st.top_traps[0]).toMatchObject({ trap_oid: OID.linkDown, trap_name: 'linkDown', count: 3 });
  expect(st.top_sources[0]).toMatchObject({ device_id: lab.aos.id, count: 3 });
  expect(st.buckets.reduce((a: number, b: any) => a + b.count, 0)).toBe(3);
});

test('UI: Traps tab table and drawer with the varbind table', async ({ page }) => {
  const m = marker('qa40ui');
  sendTrap({ from: SRC.aos, oid: OID.linkDown, varbinds: [...linkVarbinds(7, false), `1.3.6.1.2.1.31.1.1.1.18.7=s:${m}`] });
  await waitItems(admin, 'traps', { q: m }, (it) => it.length === 1);
  await loginViaApi(page, undefined, '/logs');
  await page.getByRole('tab', { name: tOr('logs.tab.traps', 'Traps') }).click();
  const row = page.getByRole('row').filter({ hasText: m });
  await expect(row).toBeVisible();
  await expect(row).toContainText('linkDown');
  await row.click();
  const d = page.getByRole('dialog');
  await expect(d).toContainText('ifOperStatus.7');
  await expect(d).toContainText('1.3.6.1.2.1.2.2.1.8.7');
  await expect(d).toContainText('down(2)');
  await shot2b3(page, '40-trap-drawer');
});
