// [profiles] Connection profiles in discovery and config backup (docs/connection-profiles.md § QA scenarios 8 and 9,
// owner decision Q3; api.md § [profiles] Discovery): a scan with an SNMP profile (the group's own credentials do not
// answer) and the opt-in SSH login check (an SSH profile and typed credentials), the result badges, the import with
// the per-host "save SNMP" / "save SSH" columns and their select-all headers, the device modes after the import, the
// read-only guarantee of the SSH check (login and disconnect only, from the simulator's log), and a config backup
// that runs with an SSH profile assigned to the group. Own simulators on 127.0.0.241-.244 (lib/profiles.ts).
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { SCREENSHOT_DIR, apiAs, loginViaApi, uid, type Api } from '../lib/helpers';
import { raw, waitForScan, waitNoRunningScan } from '../lib/lab';
import { features, setFeatures, waitBackup } from '../lib/lab3';
import {
  PROF_HOSTS, PROF_SNMP_PORT, SSH_USER, clearSimLog, simCommands, simLog, simState, snmpProfileCreds, sshProfileCreds,
  startProfSims, stopProfSims, type ProfSims,
} from '../lib/profiles';

test.describe.configure({ mode: 'serial' });

const SHOTS = process.env.PROFILES_SHOTS ?? join(SCREENSHOT_DIR, 'connection-profiles');
const TYPED_PASS = 'qa97-Typed-pass';
const [H1, H2, H3, H4] = PROF_HOSTS; // H1, H2: SNMP + SSH (profile works); H3: SSH only (typed works); H4: backup

let admin: Api;
let sims: ProfSims;
let tag: string;
let backupWasOn = false;
let scanId = 0;
const P: Record<string, { id: number; name: string }> = {};
const G: Record<string, number> = {};
const dev: Record<string, any> = {};

async function shot(page: Page, name: string) {
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: true });
}
/** The visible result row of an address (the page also renders a mobile card list). */
const row = (page: Page, ip: string) =>
  page.locator('tr').filter({ hasText: ip }).filter({ visible: true }).first();
const vis = (page: Page, testId: string) => page.getByTestId(testId).filter({ visible: true });

test.beforeAll(async () => {
  admin = await apiAs();
  await admin.patch('/api/auth/me', { language: 'en' });
  backupWasOn = !!(await features(admin)).config_backup;
  sims = await startProfSims([H1, H2]);
  await simState(sims, 'prof3', { password: TYPED_PASS }); // the SSH profile fails on H3, the typed credentials work
  tag = uid('qa97');
  P.snmp = await admin.post('/api/connection-profiles', { kind: 'snmp', name: `${tag} snmp`, credentials: snmpProfileCreds() });
  P.ssh = await admin.post('/api/connection-profiles', { kind: 'ssh', name: `${tag} ssh`, credentials: sshProfileCreds() });
  // the scan / import group's own SNMP credentials do not answer: only the profile does
  G.disc = (
    await admin.post('/api/groups', {
      name: `${tag} Discovery`, parent_id: null,
      snmp_credentials: { version: 'v2c', community: 'qa97-nope', port: PROF_SNMP_PORT, timeout_s: 1, retries: 0 },
    })
  ).id;
  for (const ip of PROF_HOSTS) {
    const old = (await admin.get(`/api/devices?q=${ip}&page_size=1000`)).items.filter((d: any) => d.host === ip);
    for (const d of old) await raw(admin, 'DELETE', `/api/devices/${d.id}`); // leftovers of an aborted earlier run
  }
});

test.afterAll(async () => {
  if (admin) {
    await setFeatures(admin, { config_backup: backupWasOn }).catch(() => undefined);
    for (const g of Object.values(G)) await raw(admin, 'DELETE', `/api/groups/${g}`);
    if (scanId) await raw(admin, 'DELETE', `/api/discovery/scans/${scanId}`);
    for (const p of Object.values(P)) await raw(admin, 'DELETE', `/api/connection-profiles/${p.id}`);
  }
  stopProfSims(sims);
});

test('8a: scan options: SNMP profile, opt-in SSH check (warning, SSH profile, typed credentials); results show what answered', async ({ page }) => {
  await waitNoRunningScan(admin);
  await clearSimLog(sims);
  await loginViaApi(page, undefined, '/discovery');
  await page.getByLabel('Targets').fill(`${H1}-${H3}`);
  const ping = page.getByLabel('Ping (ICMP)');
  if (await ping.isChecked()) await ping.uncheck();
  const tcp = page.getByLabel('TCP ports');
  for (let i = 0; i < (await tcp.count()); i++) {
    const el = tcp.nth(i);
    if ((await el.getAttribute('type')) === 'checkbox') {
      if (!(await el.isChecked())) await el.check();
    }
  }
  await page.getByLabel('TCP ports').and(page.locator('input:not([type=checkbox])')).fill('8022');
  await page.getByTestId('scan-snmp-profiles').locator('select').selectOption({ label: P.snmp.name });

  const ssh = page.getByTestId('scan-ssh-check');
  await expect(ssh.getByTestId('scan-ssh-warning')).toHaveCount(0); // off by default
  await ssh.getByLabel('Try to log in with SSH on the hosts found').check();
  await expect(ssh.getByTestId('scan-ssh-warning')).toContainText('Failed SSH logins may lock accounts');
  await expect(ssh.getByTestId('scan-ssh-warning')).toContainText('it sends no command');
  await ssh.getByTestId('scan-ssh-profiles').locator('select').selectOption({ label: P.ssh.name });
  await ssh.getByLabel('Also try typed SSH credentials').check();
  await ssh.getByLabel('Username').fill(SSH_USER);
  await ssh.getByLabel('Port').fill('8022');
  await ssh.getByLabel(/^Password/).first().fill(TYPED_PASS);
  await page.getByLabel('Target group').selectOption({ label: `${tag} Discovery` });
  await page.getByRole('button', { name: /Advanced/ }).click();
  await page.getByLabel('SNMP timeout (s)').fill('1');
  await shot(page, '97-08-scan-options-en');

  const req = page.waitForRequest((r) => r.url().endsWith('/api/discovery/scans') && r.method() === 'POST');
  await page.getByRole('button', { name: 'Start scan' }).click();
  const body = (await req).postDataJSON();
  expect(body.options).toMatchObject({ snmp_profile_ids: [P.snmp.id], ssh_profile_ids: [P.ssh.id], tcp: true, tcp_ports: [8022] });
  expect(body.options.ssh_credentials).toMatchObject({ username: SSH_USER, port: 8022, password: TYPED_PASS });
  await page.waitForURL(/\/discovery\/scans\/\d+/);
  scanId = Number(page.url().match(/scans\/(\d+)/)![1]);
  const scan = await waitForScan(admin, scanId);
  expect(scan.status).toBe('completed');
  expect(scan.options).toMatchObject({ ssh_profile_ids: [P.ssh.id], ssh_credentials: null, ssh_credentials_set: true, ssh_username: SSH_USER });
  expect(JSON.stringify(scan)).not.toContain(TYPED_PASS);

  const results = (await admin.get(`/api/discovery/scans/${scanId}/results?page_size=100`)).items;
  const by = (ip: string) => results.find((r: any) => r.ip === ip);
  for (const ip of [H1, H2]) {
    expect(by(ip), ip).toMatchObject({
      snmp_ok: true, snmp_credential: 'profile', snmp_profile_id: P.snmp.id, snmp_profile_name: P.snmp.name,
      ssh_attempted: true, ssh_ok: true, ssh_credential: 'profile', ssh_profile_id: P.ssh.id, ssh_profile_name: P.ssh.name,
    });
  }
  expect(by(H3)).toMatchObject({ snmp_ok: false, ssh_attempted: true, ssh_ok: true, ssh_credential: 'typed', ssh_profile_id: null });
  expect(JSON.stringify(results)).not.toContain(TYPED_PASS);

  await page.reload();
  await expect(row(page, H1).getByTestId('result-snmp-profile')).toHaveText(P.snmp.name);
  await expect(row(page, H1).getByTestId('result-ssh')).toContainText(`SSH ✓${P.ssh.name}`);
  await expect(row(page, H3).getByTestId('result-ssh')).toContainText('typed credentials');
  await expect(row(page, H3).getByTestId('result-snmp-profile')).toHaveCount(0);

  // login and disconnect only: no shell, no command, nothing else on any device
  const log = await simLog(sims);
  expect(log.filter((e) => ['shell', 'command', 'exec', 'pre_prompt'].includes(e.event)), JSON.stringify(log)).toEqual([]);
  // one attempt per candidate: H3 saw the profile (failed) then the typed credentials (ok); H1 only the profile
  const auths = (name: string) => log.filter((e) => e.device === name && e.event === 'auth_password').map((e) => e.ok);
  expect(auths('prof1')).toEqual([true]);
  expect(auths('prof3')).toEqual([false, true]);
  expect(auths('prof4')).toEqual([]); // not a target
});

test('8b: import with "save SNMP" / "save SSH" per host and select-all; the devices get profile / inherit / manual', async ({ page }) => {
  await loginViaApi(page, undefined, `/discovery/scans/${scanId}`);
  await expect(row(page, H3)).toBeVisible();
  await page.getByRole('checkbox', { name: 'Select all' }).check();
  const r1 = (await admin.get(`/api/discovery/scans/${scanId}/results?page_size=100`)).items;
  const rid = (ip: string) => r1.find((r: any) => r.ip === ip).id;
  const snmpAll = vis(page, 'save-snmp-all');
  const sshAll = vis(page, 'save-ssh-all');
  // defaults: on where a credential worked, disabled where none did
  await expect(snmpAll).toBeChecked();
  await expect(vis(page, `save-snmp-${rid(H3)}`)).toBeDisabled();
  await expect(vis(page, `save-ssh-${rid(H3)}`)).toBeChecked();
  // clear all SNMP, then re-check H1 only: the header becomes mixed and the hint explains inheriting
  await snmpAll.click();
  await expect(vis(page, `save-snmp-${rid(H1)}`)).not.toBeChecked();
  await expect(vis(page, `save-snmp-${rid(H2)}`)).not.toBeChecked();
  await vis(page, `save-snmp-${rid(H1)}`).check();
  expect(await snmpAll.evaluate((e: HTMLInputElement) => e.indeterminate)).toBe(true);
  await expect(vis(page, 'save-snmp-off-hint')).toContainText('inherits them from the group');
  // SSH select-all off then on again: every eligible host back on
  await sshAll.click();
  await expect(vis(page, `save-ssh-${rid(H1)}`)).not.toBeChecked();
  await sshAll.click();
  await expect(vis(page, `save-ssh-${rid(H1)}`)).toBeChecked();
  await vis(page, `save-ssh-${rid(H2)}`).uncheck();
  await shot(page, '97-08-import-columns-en');

  await page.getByLabel('Import into group').selectOption({ label: `${tag} Discovery` });
  const req = page.waitForRequest((r) => r.url().endsWith(`/api/discovery/scans/${scanId}/import`));
  await page.getByRole('button', { name: /^Import selected/ }).click();
  const body = (await req).postDataJSON();
  const item = (ip: string) => body.items.find((i: any) => i.result_id === rid(ip));
  expect(item(H1)).toMatchObject({ save_snmp: true, save_ssh: true });
  expect(item(H2)).toMatchObject({ save_snmp: false, save_ssh: false });
  expect(item(H3)).toMatchObject({ save_ssh: true });
  expect(item(H3)).not.toHaveProperty('save_snmp'); // SNMP did not answer: the key is left out
  const summary = page.getByTestId('import-summary');
  await expect(summary).toContainText('Devices created: 3');
  await expect(summary).toContainText(`SSH: linked to profile ${P.ssh.name}`);
  await expect(summary).toContainText('SSH: own copy of the credentials');
  await shot(page, '97-08-import-summary-en');

  const results = (await admin.get(`/api/discovery/scans/${scanId}/results?page_size=100`)).items;
  for (const ip of [H1, H2, H3]) {
    const r = results.find((x: any) => x.ip === ip);
    expect(r.imported_device_id, ip).not.toBeNull();
    dev[ip] = await admin.get(`/api/devices/${r.imported_device_id}`);
  }
  expect(dev[H1]).toMatchObject({ snmp_mode: 'profile', snmp_profile_id: P.snmp.id, ssh_mode: 'profile', ssh_profile_id: P.ssh.id });
  expect(dev[H2]).toMatchObject({ snmp_mode: 'inherit', ssh_mode: 'inherit' });
  expect(dev[H3]).toMatchObject({ snmp_mode: 'inherit', ssh_mode: 'manual', ssh_profile_id: null });
  expect(dev[H3].ssh_credentials).toMatchObject({ username: SSH_USER, port: 8022, password: null, password_set: true });
  // the device linked to the profile answers SNMP through it
  const sensors = (await admin.get(`/api/sensors?device_id=${dev[H1].id}&page_size=1000`)).items;
  const snmpSensor = sensors.find((s: any) => s.type.startsWith('snmp_'));
  expect(snmpSensor, 'an SNMP sensor from the template').toBeTruthy();
  await expect
    .poll(async () => {
      await admin.post(`/api/sensors/${snmpSensor.id}/run`);
      return (await admin.get(`/api/sensors/${snmpSensor.id}`)).status;
    }, { timeout: 45_000, intervals: [1000, 2000] })
    .not.toMatch(/^(down|unknown)$/);
  await page.goto(`/devices/${dev[H1].id}`);
  await expect(page.locator('main')).toContainText(`SNMP: Profile ${P.snmp.name}`);
  await expect(page.locator('main')).toContainText(`SSH: Profile ${P.ssh.name}`);
});

test('9: config backup: an SSH profile on a group makes its device eligible, and a backup runs with it', async ({ page }) => {
  await setFeatures(admin, { config_backup: true });
  G.backup = (await admin.post('/api/groups', { name: `${tag} Backup`, parent_id: null })).id;
  const d = await admin.post('/api/devices', {
    group_id: G.backup, name: `${tag}-cw`, host: H4, auto_sensors: [], config_backup_driver: 'hpe_comware', config_backup: 'auto',
  });
  expect((await admin.get(`/api/devices/${d.id}/config`)).reason).toBe('no_credentials');
  // assign the SSH profile to the group in the UI (the SSH section of the group form)
  await loginViaApi(page, undefined, `/groups/${G.backup}`);
  await page.getByRole('button', { name: 'Edit' }).first().click();
  const sshPicker = page.getByTestId('cred-picker-ssh');
  if (!(await sshPicker.isVisible())) await page.getByRole('dialog').getByText(/^SSH credentials/).first().click();
  await page.getByTestId('cred-mode-ssh').getByText('Profile', { exact: true }).click();
  await page.getByTestId('cred-profile-ssh').selectOption({ label: P.ssh.name });
  const req = page.waitForRequest((r) => r.url().includes(`/api/groups/${G.backup}`) && r.method() === 'PATCH');
  await page.getByRole('dialog').getByRole('button', { name: 'Save' }).last().click();
  const body = (await req).postDataJSON();
  expect(body.ssh_profile_id).toBe(P.ssh.id);
  expect(body).not.toHaveProperty('snmp_credentials');
  expect(body).not.toHaveProperty('ssh_credentials');

  await expect.poll(async () => (await admin.get(`/api/devices/${d.id}/config`)).eligible, { timeout: 20_000 }).toBe(true);
  const full = await admin.get(`/api/devices/${d.id}`);
  expect(full.effective_ssh).toMatchObject({ source: 'group', group_id: G.backup, mode: 'profile', profile_id: P.ssh.id });
  await clearSimLog(sims);
  expect((await raw(admin, 'POST', `/api/devices/${d.id}/config/backup`)).status).toBe(202);
  const st = await waitBackup(admin, d.id, (x) => x.last_result === 'ok', 90_000, 'backup with the SSH profile');
  expect(st.versions).toBe(1);
  expect(st.host_key).toMatchObject({ state: 'pinned' }); // the backup pins (TOFU), as before
  const cmds = await simCommands(sims, 'prof4');
  expect(cmds).toContain('display current-configuration');
  const allowed = new Set(['screen-length disable', 'display current-configuration', 'exit', 'quit', 'logout', '']);
  expect(cmds.filter((c) => !allowed.has(c)), JSON.stringify(cmds)).toEqual([]);
  // without the profile the device is no longer eligible
  await admin.patch(`/api/groups/${G.backup}`, { ssh_credentials: null });
  await expect.poll(async () => (await admin.get(`/api/devices/${d.id}/config`)).reason, { timeout: 20_000 }).toBe('no_credentials');
});
