// [profiles] Upgrade check for release 1.7.0 (docs/connection-profiles.md § QA scenario 11 and the downgrade
// materialisation; data-model.md § [profiles] Migration 0021). The running database is taken DOWN to the 1.6
// head (0018: `netmon migrate --to 0018` with the server stopped) and the restart upgrades it to 0021 again,
// exactly as an operator's upgrade does. Existing manual and inherited credentials must behave the same afterwards
// (a device polled through its group's manual SNMP still polls), and nodes that referenced a profile before the
// downgrade keep working credentials as "manual" (the profile's value wins over dormant manual data).
//
// NOTE: the downgrade drops every connection profile in the database (by design), so this spec runs after the other
// profile specs (96, 97) and creates everything it checks itself.
import { expect, test } from '@playwright/test';
import { apiAs, loginViaApi, uid, type Api } from '../lib/helpers';
import { raw } from '../lib/lab';
import { PROF_HOSTS, PROF_SNMP_PORT, startProfSims, stopProfSims, snmpProfileCreds, type ProfSims } from '../lib/profiles';

test.describe.configure({ mode: 'serial' });
const RESTART = process.env.BACKEND_RESTART ?? '';
test.skip(!RESTART, 'BACKEND_RESTART not set (tests/e2e/run.sh)');

let admin: Api;
let sims: ProfSims;
let tag: string;
const G: Record<string, number> = {};
const D: Record<string, number> = {};
const S: Record<string, number> = {};
const before: Record<string, any> = {};
const manualSnmp = { version: 'v2c', community: 'public', port: PROF_SNMP_PORT, timeout_s: 1, retries: 0 };
const manualSsh = { username: 'qa98', password: 'qa98-Ssh-pass', port: 8022, legacy_algorithms: false };

async function sensor(deviceId: number) {
  return (await admin.post('/api/sensors', { device_id: deviceId, type: 'snmp_system', name: 'System', interval_s: 10 })).id;
}
async function expectUp(id: number) {
  await expect
    .poll(
      async () => {
        await admin.post(`/api/sensors/${id}/run`);
        return (await admin.get(`/api/sensors/${id}`)).status;
      },
      { timeout: 45_000, intervals: [500, 1000, 2000], message: `sensor ${id} up` },
    )
    .toBe('up');
}
const credView = (n: any) => ({
  snmp_mode: n.snmp_mode, ssh_mode: n.ssh_mode, snmp_credentials: n.snmp_credentials, ssh_credentials: n.ssh_credentials,
  effective_snmp: n.effective_snmp, effective_ssh: n.effective_ssh,
});

test.beforeAll(async () => {
  admin = await apiAs();
  sims = await startProfSims(PROF_HOSTS.slice(0, 2));
  tag = uid('qa98');
});
test.afterAll(async () => {
  if (admin) for (const g of Object.values(G)) await raw(admin, 'DELETE', `/api/groups/${g}`);
  stopProfSims(sims);
});

test('11: manual / inherited credentials and profile references survive the 0021 downgrade + upgrade', async ({ page }) => {
  test.setTimeout(240_000);
  // 1.6-shaped data: a group with manual SNMP + SSH, a device inheriting both, a device with its own manual SNMP
  G.man = (await admin.post('/api/groups', { name: `${tag} Manual`, parent_id: null, snmp_credentials: manualSnmp, ssh_credentials: manualSsh })).id;
  G.sub = (await admin.post('/api/groups', { name: `${tag} Inherit`, parent_id: G.man })).id;
  D.inh = (await admin.post('/api/devices', { group_id: G.sub, name: `${tag}-inh`, host: PROF_HOSTS[0], auto_sensors: [] })).id;
  D.man = (await admin.post('/api/devices', { group_id: G.sub, name: `${tag}-man`, host: PROF_HOSTS[1], snmp_credentials: manualSnmp, auto_sensors: [] })).id;
  // 1.7 data: a device referencing a profile, and one whose (non-working) manual data is dormant behind a profile
  const prof = await admin.post('/api/connection-profiles', { kind: 'snmp', name: `${tag} profile`, credentials: snmpProfileCreds() });
  G.top2 = (await admin.post('/api/groups', { name: `${tag} Refs`, parent_id: null, snmp_credentials: { ...manualSnmp, community: 'qa98-wrong' } })).id;
  D.ref = (await admin.post('/api/devices', { group_id: G.top2, name: `${tag}-ref`, host: PROF_HOSTS[0], snmp_profile_id: prof.id, auto_sensors: [] })).id;
  D.dorm = (
    await admin.post('/api/devices', { group_id: G.top2, name: `${tag}-dorm`, host: PROF_HOSTS[1], snmp_credentials: { ...manualSnmp, community: 'qa98-old' }, auto_sensors: [] })
  ).id;
  await admin.patch(`/api/devices/${D.dorm}`, { snmp_profile_id: prof.id });
  expect((await admin.get(`/api/devices/${D.dorm}`)).snmp_has_saved_manual).toBe(true);
  for (const k of Object.keys(D)) {
    S[k] = await sensor(D[k]);
    await expectUp(S[k]);
  }
  for (const k of ['inh', 'man']) before[k] = credView(await admin.get(`/api/devices/${D[k]}`));
  for (const k of ['man', 'sub']) before[`g${k}`] = credView(await admin.get(`/api/groups/${G[k]}`));
  expect(before.inh.effective_snmp).toMatchObject({ source: 'group', group_id: G.man, mode: 'manual' });

  // down to the 1.6 head, then the restart upgrades again (serve runs the migrations)
  const { execFileSync } = await import('node:child_process');
  execFileSync(RESTART, ['--to', '0018'], { stdio: 'pipe', timeout: 120_000 });
  admin = await apiAs();

  // manual and inherited nodes: byte-identical views, and they still poll
  for (const k of ['inh', 'man']) expect(credView(await admin.get(`/api/devices/${D[k]}`)), `device ${k}`).toEqual(before[k]);
  for (const k of ['man', 'sub']) expect(credView(await admin.get(`/api/groups/${G[k]}`)), `group ${k}`).toEqual(before[`g${k}`]);
  // references were materialised into manual credentials (the profile's value, not the dormant one)
  const ref = await admin.get(`/api/devices/${D.ref}`);
  const dorm = await admin.get(`/api/devices/${D.dorm}`);
  for (const d of [ref, dorm]) {
    expect(d).toMatchObject({ snmp_mode: 'manual', snmp_profile_id: null, snmp_has_saved_manual: false });
    expect(d.snmp_credentials).toMatchObject({ version: 'v2c', port: PROF_SNMP_PORT, community_set: true, credentials_unreadable: false });
  }
  expect((await raw(admin, 'GET', `/api/connection-profiles/${prof.id}`)).status).toBe(404); // dropped by the downgrade
  for (const k of Object.keys(S)) await expectUp(S[k]);

  // the UI shows the same origins as before
  await loginViaApi(page, undefined, `/devices/${D.inh}`);
  await expect(page.locator('main')).toContainText(`SNMP: Inherited from group ${tag} Manual · manual`);
  await page.goto(`/devices/${D.ref}`);
  await expect(page.locator('main')).toContainText('SNMP: Manual');
});
