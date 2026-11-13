import { expect, test } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { ensureGroup, raw, snmpCreds } from '../lib/lab';
import { quiet } from '../lib/lab2b';
import { PORTS_GROUP, T, devicePorts, ensurePortLab, portOf, refreshPorts, type PortLab } from '../lib/lab2b2';
import {
  OID, SRC, builtinRule, conditions, deviceByHost, featureOn, linkVarbinds, marker, sendTrap, waitEventSensor, waitItems, waitReceivers,
} from '../lib/lab2b3';
import { shot13 } from '../lib/lab13';

// [1.3] 66 — trunk link-down alerts, LAG degraded, trunk settings (api.md § "[1.3] Settings", "[1.3] Event
// conditions", architecture.md § 14.2/14.5).
// port-aos (127.0.0.81): 7 = access port (1 MAC, no sensor). port-cw (127.0.0.82): ifIndex 50 = Ten-GigabitEthernet1/0/50,
// trunk only (tagged VLANs 1, 10, 20; no MACs, no neighbour -> not an uplink; ifIndex 49 is the manual-link uplink).
// lag-aos (127.0.0.88): Trk1 (ifIndex 11) = members 3 + 4 (LAG-MIB).
test.describe.configure({ mode: 'serial' });

const TRUNK = 50;
let admin: Api;
let lab: PortLab;
let aosTrap: any;
let cwTrap: any;
let lagAos: any;
let lagTrap: any;

const linkDown = (ifIndex: number, from: string, extra: string[] = []) => sendTrap({ from, oid: OID.linkDown, varbinds: [...linkVarbinds(ifIndex, false), ...extra] });
const linkUp = (ifIndex: number, from: string) => sendTrap({ from, oid: OID.linkUp, varbinds: linkVarbinds(ifIndex, true) });

async function waitConds(sensorId: number, pred: (c: any[]) => boolean, timeout = 15_000, message = 'conditions'): Promise<any[]> {
  let c: any[] = [];
  await expect.poll(async () => { c = await conditions(admin, sensorId); return pred(c); }, { timeout, intervals: [300, 600, 1000], message }).toBe(true);
  return c;
}
const hasKey = (c: any[], key: string, source = 'rule') => c.some((x) => x.key === key && x.source === source);

/** A linkDown with a marker; waits until the trap is stored, then a quiet period: returns the conditions. */
async function linkDownQuiet(ifIndex: number, from: string, sensor: any): Promise<{ trap: any; conds: any[] }> {
  const m = marker('qa66');
  linkDown(ifIndex, from, [`1.3.6.1.2.1.31.1.1.1.18.${ifIndex}=s:${m}`]);
  const [t] = await waitItems(admin, 'traps', { q: m }, (it) => it.length === 1);
  await quiet(2000);
  return { trap: t, conds: await conditions(admin, sensor.id) };
}

async function settings(body: Record<string, unknown>): Promise<void> {
  const r = await raw(admin, 'PUT', '/api/settings', body);
  expect(r.status, `PUT settings ${JSON.stringify(body)}: ${r.text}`).toBe(200);
}

test.beforeAll(async () => {
  admin = await apiAs();
  lab = await ensurePortLab(admin);
  await featureOn(admin, 'traps', 'event_alerts', 'lag');
  await waitReceivers(admin, ['trap'], 'listening');
  await settings({ event_linkdown_scope: 'important', event_linkdown_include_trunks: true, trunk_min_tagged_vlans: 2 });
  const rule = await builtinRule(admin, 'trap_link_down');
  if (!rule.enabled) await admin.patch(`/api/event-rules/${rule.id}`, { enabled: true });
  // linkUp creates the passive trap sensors when no earlier spec did
  linkUp(2, SRC.aos);
  linkUp(1, SRC.cw);
  aosTrap = await waitEventSensor(admin, lab.aos.id, 'trap');
  cwTrap = await waitEventSensor(admin, lab.cw.id, 'trap');
  for (const s of [aosTrap, cwTrap]) await raw(admin, 'POST', `/api/sensors/${s.id}/clear`);
});

test.afterAll(async () => {
  await settings({ event_linkdown_scope: 'important', event_linkdown_include_trunks: true, trunk_min_tagged_vlans: 2 });
  for (const s of [aosTrap, cwTrap, lagTrap]) if (s) await raw(admin, 'POST', `/api/sensors/${s.id}/clear`);
});

test('settings: defaults and limits (trunk_min_tagged_vlans 1-100, include_trunks bool)', async () => {
  const s = await admin.get('/api/settings');
  expect(s).toMatchObject({ event_linkdown_include_trunks: true, trunk_min_tagged_vlans: 2 });
  for (const v of [0, 101, 'x']) expect((await raw(admin, 'PUT', '/api/settings', { trunk_min_tagged_vlans: v })).status, `min ${v}`).toBe(422);
  expect((await raw(admin, 'PUT', '/api/settings', { event_linkdown_include_trunks: 'maybe' })).status).toBe(422);
  expect((await admin.get('/api/settings')).trunk_min_tagged_vlans).toBe(2);
});

test('port-cw 50 is important only as a trunk; port-aos 7 is not important', async () => {
  const cw = await devicePorts(admin, lab.cw.id);
  expect(portOf(cw, TRUNK)).toMatchObject({ important: true, important_reasons: ['trunk'], mode: 'trunk', tagged_vlans: [1, 10, 20] });
  expect(portOf(await devicePorts(admin, lab.aos.id), 7)).toMatchObject({ important: false, important_reasons: [] });
});

test('linkDown on trunk port 50 (not an uplink) -> Link down condition; linkUp clears it', async () => {
  linkDown(TRUNK, SRC.cw);
  const c = await waitConds(cwTrap.id, (x) => hasKey(x, String(TRUNK)), 15_000, 'Link down for trunk port 50');
  expect(c.find((x) => x.key === String(TRUNK))).toMatchObject({ source: 'rule', status: 'warning', last_kind: 'trap' });
  await expect.poll(async () => (await admin.get(`/api/sensors/${cwTrap.id}`)).status, { timeout: 15_000 }).toBe('warning');
  linkUp(TRUNK, SRC.cw);
  await waitConds(cwTrap.id, (x) => !hasKey(x, String(TRUNK)), 15_000, 'linkUp clears port 50');
  await expect.poll(async () => (await admin.get(`/api/sensors/${cwTrap.id}`)).status, { timeout: 15_000 }).toBe('up');
});

test('scope important: linkDown on access port 7 raises nothing', async () => {
  const { trap, conds } = await linkDownQuiet(7, SRC.aos, aosTrap);
  expect(trap.matched_rule_ids).toEqual([]);
  expect(hasKey(conds, '7')).toBe(false);
});

test('event_linkdown_include_trunks off -> trunk 50 is quiet; trunk_min_tagged_vlans 4 -> 50 is no trunk', async () => {
  await settings({ event_linkdown_include_trunks: false });
  let r = await linkDownQuiet(TRUNK, SRC.cw, cwTrap);
  expect(r.trap.matched_rule_ids, 'include_trunks off: the Link-down rule does not match').toEqual([]);
  expect(hasKey(r.conds, String(TRUNK)), 'include_trunks off: no condition for trunk 50').toBe(false);
  // the ports view keeps listing the trunk reason (contract: "trunks always listed")
  expect(portOf(await devicePorts(admin, lab.cw.id), TRUNK).important_reasons).toEqual(['trunk']);
  await settings({ event_linkdown_include_trunks: true, trunk_min_tagged_vlans: 4 });
  expect(portOf(await devicePorts(admin, lab.cw.id), TRUNK)).toMatchObject({ important: false, important_reasons: [] });
  r = await linkDownQuiet(TRUNK, SRC.cw, cwTrap);
  expect(hasKey(r.conds, String(TRUNK)), 'min 4 tagged VLANs: port 50 (3 tagged) is no trunk').toBe(false);
  await settings({ trunk_min_tagged_vlans: 2 });
  linkDown(TRUNK, SRC.cw);
  await waitConds(cwTrap.id, (x) => hasKey(x, String(TRUNK)), 15_000, 'back to 2: trunk again');
  linkUp(TRUNK, SRC.cw);
  await waitConds(cwTrap.id, (x) => !hasKey(x, String(TRUNK)));
});

test('LAG degraded: member 3 of Trk1 linkDown -> lag_degraded warning; linkUp -> removed', async ({ page }) => {
  const gid = await ensureGroup(admin, PORTS_GROUP);
  lagAos = await deviceByHost(admin, SRC.lagAos);
  if (!lagAos) lagAos = await admin.post('/api/devices', { group_id: gid, name: 'lag-aos', host: SRC.lagAos, snmp_credentials: snmpCreds(), auto_sensors: ['ping', 'snmp_system'] });
  if (!(await admin.get(`/api/devices/${lagAos.id}`)).identity) expect((await raw(admin, 'POST', `/api/devices/${lagAos.id}/identify`)).status).toBe(200);
  await refreshPorts(admin, lagAos.id);
  expect(portOf(await devicePorts(admin, lagAos.id), 11).lag_members).toEqual([3, 4]);
  linkUp(1, SRC.lagAos); // trap sensor of lag-aos
  lagTrap = await waitEventSensor(admin, lagAos.id, 'trap');
  await raw(admin, 'POST', `/api/sensors/${lagTrap.id}/clear`);
  await waitConds(lagTrap.id, (x) => x.length === 0);

  linkDown(3, SRC.lagAos);
  let c = await waitConds(lagTrap.id, (x) => hasKey(x, '11', 'lag_degraded'), 20_000, 'lag_degraded for Trk1');
  expect(c.find((x) => x.source === 'lag_degraded')).toMatchObject({ key: '11', status: 'warning', rule_id: null, message: 'LAG Trk1 degraded (1 of 2 members up)' });
  c = await waitConds(lagTrap.id, (x) => hasKey(x, '3'), 10_000, `the member itself is important (LAG member) -> Link down; conditions ${JSON.stringify(c)}`);
  await expect.poll(async () => (await admin.get(`/api/sensors/${lagTrap.id}`)).status, { timeout: 15_000 }).toBe('warning');

  await loginViaApi(page, undefined, `/sensors/${lagTrap.id}`);
  await expect(page.getByText(T('conditions.source.lag_degraded')).first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText('LAG Trk1 degraded (1 of 2 members up)').first()).toBeVisible();
  await shot13(page, '66-lag-degraded-condition');

  // both members down: not "degraded" any more (fully down stays the link-down alert)
  linkDown(4, SRC.lagAos);
  await waitConds(lagTrap.id, (x) => !hasKey(x, '11', 'lag_degraded') && hasKey(x, '4'), 20_000, 'all members down -> no lag_degraded');
  linkUp(4, SRC.lagAos);
  await waitConds(lagTrap.id, (x) => hasKey(x, '11', 'lag_degraded'), 20_000, 'one member back -> degraded again');
  linkUp(3, SRC.lagAos);
  await waitConds(lagTrap.id, (x) => !hasKey(x, '11', 'lag_degraded') && !hasKey(x, '3') && !hasKey(x, '4'), 20_000, 'all up -> cleared');
  await expect.poll(async () => (await admin.get(`/api/sensors/${lagTrap.id}`)).status, { timeout: 15_000 }).toBe('up');
});

test('UI: Traps & syslog settings — "Include all trunk ports" + trunk threshold persist; invalid value blocks Save', async ({ page }) => {
  await loginViaApi(page, undefined, '/settings');
  await page.getByRole('tab', { name: T('settings.tab.ingest') }).click();
  const box = page.getByTestId('trunk-scope');
  await expect(box).toBeVisible();
  const form = page.locator('form').filter({ has: box });
  const include = box.getByRole('checkbox', { name: T('ingestSettings.trunks.include') });
  const min = page.getByTestId('trunk-min-tagged');
  await expect(include).toBeChecked();
  await expect(min).toHaveValue('2');
  await min.fill('0');
  await expect(box).toContainText(T('ingestSettings.trunks.minTaggedErr'));
  await expect(form.getByRole('button', { name: T('common.save'), exact: true })).toBeDisabled();
  await min.fill('4');
  await include.uncheck();
  await shot13(page, '66-trunk-settings');
  await form.getByRole('button', { name: T('common.save'), exact: true }).click();
  await expect.poll(async () => { const s = await admin.get('/api/settings'); return [s.event_linkdown_include_trunks, s.trunk_min_tagged_vlans]; }, { timeout: 10_000 }).toEqual([false, 4]);
  await page.reload();
  await page.getByRole('tab', { name: T('settings.tab.ingest') }).click();
  await expect(page.getByTestId('trunk-scope').getByRole('checkbox', { name: T('ingestSettings.trunks.include') })).not.toBeChecked();
  await expect(page.getByTestId('trunk-min-tagged')).toHaveValue('4');
  // back to the defaults through the UI
  await page.getByTestId('trunk-scope').getByRole('checkbox', { name: T('ingestSettings.trunks.include') }).check();
  await page.getByTestId('trunk-min-tagged').fill('2');
  await page.locator('form').filter({ has: page.getByTestId('trunk-scope') }).getByRole('button', { name: T('common.save'), exact: true }).click();
  await expect.poll(async () => { const s = await admin.get('/api/settings'); return [s.event_linkdown_include_trunks, s.trunk_min_tagged_vlans]; }, { timeout: 10_000 }).toEqual([true, 2]);
});
