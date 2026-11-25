// [profiles] Connection profiles, release 1.7.0 (docs/connection-profiles.md § QA scenarios 1-7, 10, 12, 13;
// api.md § Connection profiles): profiles in Settings (Hebrew RTL), reference semantics on groups and devices
// (inherit / profile / manual, the origin line, dormant manual data restored, the inherit confirmation, copy into
// manual), "used by" and the blocked delete, connection tests (SNMP + SSH, read-only, no host key pinned), the
// viewer (names only, 403), the tree-move preview naming both profiles, and the 390 px Hebrew layout.
// Own simulators on 127.0.0.241-.244 (lib/profiles.ts), so the spec does not depend on other specs' data.
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { SCREENSHOT_DIR, VIEWER, apiAs, loginViaApi, uid, type Api } from '../lib/helpers';
import { ensureViewer, horizontalOverflow, raw } from '../lib/lab';
import { features, setFeatures } from '../lib/lab3';
import {
  PROF_HOSTS, SSH_PASS, checkNow, clearSimLog, simCommands, simLog, snmpProfileCreds,
  startProfSims, stopProfSims, type ProfSims,
} from '../lib/profiles';

test.describe.configure({ mode: 'serial' });

const SHOTS = process.env.PROFILES_SHOTS ?? join(SCREENSHOT_DIR, 'connection-profiles');
const SECRET_COMMUNITY = 'qa96-Secret-Comm';
const SECRET_V3_AUTH = 'qa96-Auth-key-1';
const SECRET_V3_PRIV = 'qa96-Priv-key-1';

let admin: Api;
let sims: ProfSims;
let tag: string;
let backupWasOn = false;
const P: Record<string, { id: number; name: string }> = {}; // profiles
const G: Record<string, number> = {}; // groups
const D: Record<string, number> = {}; // devices
const S: Record<string, number> = {}; // sensors

async function shot(page: Page, name: string) {
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: join(SHOTS, `${name}.png`) });
}
const profileRow = (page: Page, name: string) => page.getByTestId('profiles-table').locator('tr').filter({ hasText: name });
const picker = (page: Page, kind: 'snmp' | 'ssh') => page.getByTestId(`cred-picker-${kind}`);
const modeBtn = (page: Page, kind: 'snmp' | 'ssh', label: string) =>
  page.getByTestId(`cred-mode-${kind}`).getByText(label, { exact: true });

async function setLang(api: Api, lang: 'en' | 'he') {
  await api.patch('/api/auth/me', { language: lang });
}
async function profileByName(name: string) {
  const list = await admin.get(`/api/connection-profiles?q=${encodeURIComponent(name)}&page_size=1000`);
  return list.items.find((p: any) => p.name === name);
}
async function snmpSensor(deviceId: number): Promise<number> {
  const s = await admin.post('/api/sensors', { device_id: deviceId, type: 'snmp_system', name: 'System', interval_s: 10 });
  return s.id;
}
/** Runs the check now and waits for the status (the real poller path, no fixed sleeps). */
async function expectSensor(id: number, want: 'up' | 'down') {
  await expect
    .poll(
      async () => {
        await checkNow(admin, id);
        return (await admin.get(`/api/sensors/${id}`)).status;
      },
      { timeout: 45_000, intervals: [500, 1000, 2000], message: `sensor ${id} -> ${want}` },
    )
    .toBe(want);
}
/** Opens the device / group edit form from its page. */
async function openEdit(page: Page, path: string) {
  await page.goto(path);
  await page.getByRole('button', { name: /^(Edit|עריכה)$/ }).first().click();
  await expect(picker(page, 'snmp')).toBeVisible();
}
async function saveForm(page: Page, urlPart: string): Promise<any> {
  const req = page.waitForRequest((r) => r.url().includes(urlPart) && r.method() === 'PATCH');
  const res = page.waitForResponse((r) => r.url().includes(urlPart) && r.request().method() === 'PATCH');
  await page.getByRole('dialog').getByRole('button', { name: /^(Save|שמירה)$/ }).last().click();
  const body = (await req).postDataJSON();
  expect((await res).status()).toBe(200);
  return body;
}

test.beforeAll(async () => {
  admin = await apiAs();
  await setLang(admin, 'en');
  await ensureViewer(admin);
  backupWasOn = !!(await features(admin)).config_backup;
  sims = await startProfSims(PROF_HOSTS.slice(0, 2));
  tag = uid('qa96');
  G.top = (await admin.post('/api/groups', { name: `${tag} Site`, parent_id: null })).id;
  G.floor = (await admin.post('/api/groups', { name: `${tag} Floor`, parent_id: G.top })).id;
  D.sw = (await admin.post('/api/devices', { group_id: G.floor, name: `${tag}-sw`, host: PROF_HOSTS[0], auto_sensors: [] })).id;
  S.sw = await snmpSensor(D.sw);
});

test.afterAll(async () => {
  if (admin) {
    await setLang(admin, 'en').catch(() => undefined);
    await setFeatures(admin, { config_backup: backupWasOn }).catch(() => undefined);
    for (const g of [G.top, G.moveA, G.moveB, G.del]) if (g) await raw(admin, 'DELETE', `/api/groups/${g}`);
    if (D.ref) await raw(admin, 'DELETE', `/api/devices/${D.ref}`);
    for (const p of Object.values(P)) await raw(admin, 'DELETE', `/api/connection-profiles/${p.id}`);
  }
  stopProfSims(sims);
});

test('1: Settings in Hebrew: create SNMP v2c, SNMP v3 and SSH profiles; secrets stay "stored"; names unique per kind', async ({ page }) => {
  await setLang(admin, 'he');
  try {
    await loginViaApi(page, undefined, '/settings?tab=connectionProfiles');
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
    await expect(page.getByRole('heading', { name: 'פרופילי חיבור' })).toBeVisible();
    const form = page.getByTestId('profile-form');
    const create = async (name: string, fill: () => Promise<void>, expectStatus = 201) => {
      await page.getByRole('button', { name: 'הוספת פרופיל' }).click();
      await expect(form).toBeVisible();
      await form.getByLabel('שם').first().fill(name);
      await fill();
      const res = page.waitForResponse((r) => r.url().endsWith('/api/connection-profiles') && r.request().method() === 'POST');
      await form.getByRole('button', { name: 'שמירה' }).click();
      expect((await res).status()).toBe(expectStatus);
    };
    // SNMP v2c
    P.v2c = { id: 0, name: `${tag} ליבה v2c` };
    await create(P.v2c.name, async () => {
      await form.getByLabel('Community').fill(SECRET_COMMUNITY);
      await form.getByLabel('פורט').fill('16161');
      await form.getByLabel('זמן קצוב (שניות)').fill('1');
      await form.getByLabel('ניסיונות חוזרים').fill('0');
    });
    await expect(form).toBeHidden();
    await expect(profileRow(page, P.v2c.name)).toContainText('SNMP');
    // SNMP v3 (authPriv)
    P.v3 = { id: 0, name: `${tag} ליבה v3` };
    await create(P.v3.name, async () => {
      await form.getByLabel('גרסה').selectOption('v3');
      await form.getByLabel('שם משתמש').fill('qa96user');
      await form.getByLabel('רמת אבטחה').selectOption('authPriv');
      await form.getByLabel('פרוטוקול אימות').selectOption('SHA');
      await form.getByLabel('מפתח אימות').fill(SECRET_V3_AUTH);
      await form.getByLabel('פרוטוקול הצפנה').selectOption('AES128');
      await form.getByLabel('מפתח הצפנה').fill(SECRET_V3_PRIV);
      await form.getByLabel('פורט').fill('16161');
    });
    await expect(form).toBeHidden();
    // SSH, with the same name as the SNMP v2c profile: allowed (unique per kind)
    await create(P.v2c.name, async () => {
      await form.getByTestId('profile-kind').getByText('SSH', { exact: true }).click();
      await form.getByLabel('שם משתמש').fill('netmon-backup');
      await form.getByLabel('פורט').fill('8022');
      await form.getByLabel('סיסמה', { exact: true }).fill(SSH_PASS);
    });
    await expect(form).toBeHidden();
    await expect(profileRow(page, P.v2c.name)).toHaveCount(2);
    // the same SNMP name again, another letter case: refused (409), the form stays open with the message
    await create(P.v2c.name.replace('v2c', 'V2C'), async () => {
      await form.getByLabel('Community').fill('x');
    }, 409);
    await expect(form).toBeVisible();
    await expect(form).toContainText('השם הזה כבר קיים');
    await form.getByRole('button', { name: 'ביטול' }).click();

    const list = (await admin.get('/api/connection-profiles?page_size=1000')).items.filter((p: any) => p.name.startsWith(tag));
    const v2c = list.find((p: any) => p.kind === 'snmp' && p.name === P.v2c.name);
    const v3 = list.find((p: any) => p.name === P.v3.name);
    const ssh = list.find((p: any) => p.kind === 'ssh');
    P.v2c.id = v2c.id;
    P.v3.id = v3.id;
    P.ssh = { id: ssh.id, name: ssh.name };
    expect(v2c.credentials).toMatchObject({ version: 'v2c', community: null, community_set: true, port: 16161 });
    expect(v3.credentials).toMatchObject({
      version: 'v3', v3_username: 'qa96user', v3_auth_key: null, v3_auth_key_set: true, v3_priv_key: null, v3_priv_key_set: true,
    });
    expect(ssh.credentials).toMatchObject({ username: 'netmon-backup', password: null, password_set: true });
    const listText = JSON.stringify(list);
    for (const s of [SECRET_COMMUNITY, SECRET_V3_AUTH, SECRET_V3_PRIV, SSH_PASS]) expect(listText).not.toContain(s);

    // edit: the secret field is empty with the "stored" placeholder, never the value
    await page.getByRole('button', { name: `עריכת ${P.v3.name}` }).click();
    await expect(form.getByLabel('מפתח אימות')).toHaveValue('');
    await expect(form.getByLabel('מפתח אימות')).toHaveAttribute('placeholder', '•••••• (שמור)');
    await expect(form).toContainText('אי אפשר לשנות את הסוג אחרי יצירת הפרופיל.');
    await shot(page, '96-01-profile-edit-he');
    await form.getByRole('button', { name: 'ביטול' }).click();
    expect(await page.content()).not.toContain(SECRET_V3_AUTH);
    await shot(page, '96-01-profiles-tab-he');
  } finally {
    await setLang(admin, 'en');
  }
});

test('2: a profile on a group reaches a device two levels below ("Inherited from group X · profile Y"); its SNMP sensor goes up', async ({ page }) => {
  // the community is the secret one: point it at the simulator's community first (API), then assign in the UI
  await admin.patch(`/api/connection-profiles/${P.v2c.id}`, { credentials: { community: 'public' } });
  await expectSensor(S.sw, 'down'); // the built-in default (port 161) does not reach the simulator
  await loginViaApi(page, undefined, `/groups/${G.top}`);
  await openEdit(page, `/groups/${G.top}`);
  await modeBtn(page, 'snmp', 'Profile').click();
  await page.getByTestId('cred-profile-snmp').selectOption({ label: P.v2c.name });
  await expect(picker(page, 'snmp')).toContainText('A reference: later changes to the profile apply here at once.');
  const body = await saveForm(page, `/api/groups/${G.top}`);
  expect(body.snmp_profile_id).toBe(P.v2c.id);
  expect(body).not.toHaveProperty('snmp_credentials'); // the 1.7 UI sends credentials only for a changed kind
  expect(body).not.toHaveProperty('ssh_credentials');
  expect(body).not.toHaveProperty('ssh_profile_id');

  await page.goto(`/devices/${D.sw}`);
  await expect(page.locator('main')).toContainText(`SNMP: Inherited from group ${tag} Site · profile ${P.v2c.name}`);
  const dev = await admin.get(`/api/devices/${D.sw}`);
  expect(dev).toMatchObject({ snmp_mode: 'inherit', snmp_profile_id: null, snmp_credentials: null });
  expect(dev.effective_snmp).toMatchObject({ source: 'group', group_id: G.top, mode: 'profile', profile_id: P.v2c.id, profile_name: P.v2c.name });
  await expectSensor(S.sw, 'up');
});

test('3: editing the profile changes the device at its next check, with no device edit (reference semantics)', async ({ page }) => {
  await loginViaApi(page, undefined, '/settings?tab=connectionProfiles');
  await page.getByRole('button', { name: `Edit ${P.v2c.name}` }).first().click();
  const form = page.getByTestId('profile-form');
  await expect(form).toContainText('Saving applies at once to 1 device that uses this profile.');
  await form.getByLabel('Community').fill('qa96-wrong');
  const res = page.waitForResponse((r) => r.url().endsWith(`/api/connection-profiles/${P.v2c.id}`) && r.request().method() === 'PATCH');
  await form.getByRole('button', { name: 'Save' }).click();
  expect((await res).status()).toBe(200);
  await expectSensor(S.sw, 'down');
  await admin.patch(`/api/connection-profiles/${P.v2c.id}`, { credentials: { community: 'public' } });
  await expectSensor(S.sw, 'up');
  expect((await admin.get(`/api/devices/${D.sw}`)).snmp_mode).toBe('inherit'); // the device was never edited
});

test('4: device overrides: manual wins, another profile, dormant manual restored, inherit asks first', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${D.sw}`);
  // manual on the device wins over the group's profile
  await openEdit(page, `/devices/${D.sw}`);
  await modeBtn(page, 'snmp', 'Manual').click();
  await picker(page, 'snmp').getByLabel('Community').fill('public');
  await picker(page, 'snmp').getByLabel('Port').fill('16161');
  let body = await saveForm(page, `/api/devices/${D.sw}`);
  expect(body.snmp_credentials).toMatchObject({ community: 'public', port: 16161 });
  expect(body).not.toHaveProperty('snmp_profile_id');
  await expect(page.locator('main')).toContainText('SNMP: Manual');
  await expectSensor(S.sw, 'up');

  // another profile (v3: the simulator has no such user -> down); the manual data is kept dormant
  await openEdit(page, `/devices/${D.sw}`);
  await modeBtn(page, 'snmp', 'Profile').click();
  await expect(picker(page, 'snmp')).toContainText('The manual settings stored here are kept');
  await page.getByTestId('cred-profile-snmp').selectOption({ label: P.v3.name });
  body = await saveForm(page, `/api/devices/${D.sw}`);
  expect(body).toEqual(expect.objectContaining({ snmp_profile_id: P.v3.id }));
  expect(body).not.toHaveProperty('snmp_credentials');
  await expect(page.locator('main')).toContainText(`SNMP: Profile ${P.v3.name}`);
  let dev = await admin.get(`/api/devices/${D.sw}`);
  expect(dev).toMatchObject({ snmp_mode: 'profile', snmp_profile_id: P.v3.id, snmp_has_saved_manual: true, snmp_credentials: null });
  await expectSensor(S.sw, 'down');

  // back to manual: "restore the saved manual settings" sends {snmp_profile_id: null}; the old values work again
  await openEdit(page, `/devices/${D.sw}`);
  await modeBtn(page, 'snmp', 'Manual').click();
  await page.getByTestId('cred-restore-snmp').getByText('Restore the saved manual settings').click();
  body = await saveForm(page, `/api/devices/${D.sw}`);
  expect(body.snmp_profile_id).toBeNull();
  expect(body).not.toHaveProperty('snmp_credentials');
  dev = await admin.get(`/api/devices/${D.sw}`);
  expect(dev).toMatchObject({ snmp_mode: 'manual', snmp_has_saved_manual: false });
  expect(dev.snmp_credentials).toMatchObject({ port: 16161, community: null, community_set: true });
  await expectSensor(S.sw, 'up');

  // profile again, then Inherit: the confirmation says the kept manual data is deleted; cancel keeps the mode
  await admin.patch(`/api/devices/${D.sw}`, { snmp_profile_id: P.v3.id });
  await openEdit(page, `/devices/${D.sw}`);
  await modeBtn(page, 'snmp', 'Inherit').click();
  const confirm = page.getByRole('dialog').filter({ hasText: 'Delete the manual settings?' });
  await expect(confirm).toContainText('including manual settings kept under a profile');
  await confirm.getByRole('button', { name: 'Cancel' }).click();
  await expect(picker(page, 'snmp')).toContainText(P.v3.name);
  await modeBtn(page, 'snmp', 'Inherit').click();
  await confirm.getByRole('button', { name: 'Switch to Inherit' }).click();
  body = await saveForm(page, `/api/devices/${D.sw}`);
  expect(body.snmp_credentials).toBeNull();
  dev = await admin.get(`/api/devices/${D.sw}`);
  expect(dev).toMatchObject({ snmp_mode: 'inherit', snmp_has_saved_manual: false, snmp_profile_id: null });
  await expect(page.locator('main')).toContainText(`SNMP: Inherited from group ${tag} Site · profile ${P.v2c.name}`);
  await expectSensor(S.sw, 'up');
});

test('5: an in-use profile cannot be deleted: "used by" lists the group, the device and the discovery profile', async ({ page }) => {
  const name = `${tag} del`;
  P.del = await admin.post('/api/connection-profiles', { kind: 'snmp', name, credentials: snmpProfileCreds() });
  G.del = (await admin.post('/api/groups', { name: `${tag} Del group`, parent_id: null, snmp_profile_id: P.del.id })).id;
  D.ref = (await admin.post('/api/devices', { group_id: G.floor, name: `${tag}-ref`, host: PROF_HOSTS[1], snmp_profile_id: P.del.id, auto_sensors: [] })).id;
  const dp = await admin.post('/api/discovery/profiles', {
    name: `${tag} disc`, targets: [PROF_HOSTS[1]], group_id: G.floor, options: { snmp_profile_ids: [P.del.id] },
  });
  try {
    expect((await raw(admin, 'DELETE', `/api/connection-profiles/${P.del.id}`)).body).toMatchObject({
      error: { code: 'profile_in_use', details: [{ groups: 1, devices: 1, discovery_profiles: 1 }] },
    });
    await loginViaApi(page, undefined, '/settings?tab=connectionProfiles');
    await expect(profileRow(page, name)).toContainText('1 group · 1 device · 1 discovery profile');
    await page.getByRole('button', { name: `Delete ${name}` }).click();
    const dlg = page.getByRole('dialog').filter({ hasText: `${name} is in use` });
    await expect(dlg).toContainText('A profile in use cannot be deleted.');
    await expect(dlg.getByRole('link', { name: `${tag} Del group` })).toHaveAttribute('href', `/groups/${G.del}`);
    // a top-level group: path = ancestors only (empty), so the line is the name alone, no " / "
    await expect(dlg.getByRole('listitem').filter({ has: page.getByRole('link', { name: `${tag} Del group` }) }))
      .toHaveText(`${tag} Del group`);
    await expect(dlg.getByRole('link', { name: `${tag}-ref` })).toHaveAttribute('href', `/devices/${D.ref}`);
    await expect(dlg).toContainText(`${tag} disc`);
    await shot(page, '96-05-delete-blocked-en');
    await dlg.getByRole('button', { name: 'Close' }).last().click();
    await expect(profileRow(page, name)).toBeVisible();
  } finally {
    await admin.patch(`/api/groups/${G.del}`, { snmp_credentials: null });
    await raw(admin, 'DELETE', `/api/devices/${D.ref}`);
    D.ref = 0;
    await raw(admin, 'DELETE', `/api/discovery/profiles/${dp.id}`);
  }
  await page.reload();
  await expect(profileRow(page, name)).toContainText('Not used');
  await page.getByRole('button', { name: `Delete ${name}` }).click();
  const confirm = page.getByRole('dialog').filter({ hasText: `Delete profile ${name}?` });
  const res = page.waitForResponse((r) => r.url().endsWith(`/api/connection-profiles/${P.del.id}`) && r.request().method() === 'DELETE');
  await confirm.getByRole('button', { name: 'Delete' }).click();
  expect((await res).status()).toBe(204);
  await expect(profileRow(page, name)).toHaveCount(0);
  delete P.del;
});

test('6: copy from profile into manual; a later profile change does not reach the device', async ({ page }) => {
  await admin.patch(`/api/devices/${D.sw}`, { snmp_profile_id: P.v2c.id });
  await loginViaApi(page, undefined, `/devices/${D.sw}`);
  await openEdit(page, `/devices/${D.sw}`);
  const req = page.waitForRequest((r) => r.url().includes(`/api/devices/${D.sw}/credentials/snmp/copy-from-profile`));
  await page.getByTestId('cred-copy-snmp').click();
  const r = await req;
  expect(r.postDataJSON()).toEqual({ profile_id: P.v2c.id });
  await expect(picker(page, 'snmp')).toContainText('In effect now: Manual');
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel' }).last().click();
  const dev = await admin.get(`/api/devices/${D.sw}`);
  expect(dev).toMatchObject({ snmp_mode: 'manual', snmp_profile_id: null, snmp_has_saved_manual: false });
  expect(dev.snmp_credentials).toMatchObject({ version: 'v2c', port: 16161, community: null, community_set: true });
  await admin.patch(`/api/connection-profiles/${P.v2c.id}`, { credentials: { community: 'qa96-wrong' } });
  try {
    await expectSensor(S.sw, 'up'); // the copied community, not the profile's new one
    await page.reload();
    await expect(page.locator('main')).toContainText('SNMP: Manual');
  } finally {
    await admin.patch(`/api/connection-profiles/${P.v2c.id}`, { credentials: { community: 'public' } });
  }
  await admin.patch(`/api/devices/${D.sw}`, { snmp_credentials: null }); // back to inherit for later tests
});

test('7: test connection: SNMP (device, address, failure) and SSH (ok, wrong password); read-only, no host key pinned', async ({ page }) => {
  await setFeatures(admin, { config_backup: true }); // pinning would be allowed now: a profile test must still not pin
  await clearSimLog(sims);
  await loginViaApi(page, undefined, '/settings?tab=connectionProfiles');
  const dlg = page.getByTestId('profile-test-dialog');
  const run = async (status: number) => {
    const res = page.waitForResponse((r) => /\/api\/connection-profiles\/(\d+\/)?test$/.test(r.url()));
    await dlg.getByRole('button', { name: 'Run test' }).click();
    expect((await res).status()).toBe(status);
  };
  const pickDevice = async (name: string) => {
    const sel = dlg.locator('select').first();
    await sel.selectOption((await sel.locator('option').filter({ hasText: name }).getAttribute('value'))!);
  };
  const toAddress = async (host: string) => {
    await dlg.getByText('An address', { exact: true }).click();
    await dlg.getByLabel('Address').last().fill(host);
  };
  // SNMP against the device
  await page.getByRole('button', { name: `Test ${P.v2c.name}` }).first().click();
  await expect(dlg).toContainText('Read-only');
  await pickDevice(`${tag}-sw`);
  await run(200);
  await expect(dlg.getByTestId('profile-test-result')).toContainText('netmon-sim');
  await expect(dlg.getByTestId('profile-test-result')).toContainText('1.3.6.1.4.1.8072.3.2.10');
  // against an address with an agent, then one without
  await toAddress(PROF_HOSTS[1]);
  await run(200);
  await expect(dlg.getByTestId('profile-test-result')).toContainText(PROF_HOSTS[1]);
  await toAddress(PROF_HOSTS[3]);
  await run(502);
  await expect(dlg).toContainText('SNMP request failed');
  await dlg.getByRole('button', { name: 'Close' }).last().click();

  // SSH against the device's host (comware sim): connect, host key, auth (no driver -> nothing else)
  await page.getByRole('button', { name: `Test ${P.ssh.name}` }).last().click();
  await expect(dlg).toContainText('no backup or other command is sent and no host key is pinned');
  await pickDevice(`${tag}-sw`);
  await run(200);
  const result = dlg.getByTestId('profile-test-result');
  await expect(result).toContainText('ssh-ed25519 SHA256:');
  await expect(result).toContainText('netmon-backup');
  await shot(page, '96-07-ssh-test-ok-en');
  await dlg.getByRole('button', { name: 'Close' }).last().click();
  // wrong password (unsaved edit in the form) -> ssh_error at step auth
  await page.getByRole('button', { name: `Edit ${P.ssh.name}` }).last().click();
  const form = page.getByTestId('profile-form');
  await form.getByLabel(/^Password/).first().fill('wrong-password');
  await page.getByTestId('profile-form-test').click();
  await toAddress(PROF_HOSTS[0]);
  await run(502);
  await expect(dlg).toContainText('SSH test failed');
  await dlg.getByRole('button', { name: 'Close' }).last().click();
  await form.getByRole('button', { name: 'Cancel' }).click();
  // the stored password was not changed by the unsaved test
  const ok = await admin.post(`/api/connection-profiles/${P.ssh.id}/test`, { host: PROF_HOSTS[0], driver: 'hpe_comware' });
  expect(ok.steps.map((s: any) => s.step)).toEqual(['connect', 'host_key', 'auth', 'prompt', 'setup']);

  // nothing pinned on the device, and only session commands (no backup command) reached the simulator
  expect((await admin.get(`/api/devices/${D.sw}/config`)).host_key).toBeNull();
  const allowedSetup = new Set(['screen-length disable', 'exit', 'quit', 'logout', '']);
  const cmds = await simCommands(sims);
  expect(cmds.filter((c) => !allowedSetup.has(c)), `commands sent: ${JSON.stringify(cmds)}`).toEqual([]);
  const auths = (await simLog(sims)).filter((e) => e.event === 'auth_password');
  expect(auths.some((e) => e.ok === false)).toBe(true);
  expect(auths.some((e) => e.ok === true)).toBe(true);
});

test('10: viewer: no Connection profiles tab, profile names only, every profile endpoint 403', async ({ page }) => {
  await admin.patch(`/api/devices/${D.sw}`, { snmp_profile_id: P.v2c.id, ssh_profile_id: P.ssh.id });
  try {
    await loginViaApi(page, VIEWER, '/');
    await page.request.patch('/api/auth/me', { data: { language: 'en' } });
    await page.goto(`/devices/${D.sw}`);
    await expect(page.locator('main')).toContainText(`SNMP: Profile ${P.v2c.name}`);
    await expect(page.locator('main')).toContainText(`SSH: Profile ${P.ssh.name}`);
    await expect(page.getByRole('button', { name: 'Edit' })).toHaveCount(0);
    const dev = await (await page.request.get(`/api/devices/${D.sw}`)).json();
    expect(dev).toMatchObject({ snmp_profile_name: P.v2c.name, snmp_credentials: null, ssh_credentials: null });
    const grp = await (await page.request.get(`/api/groups/${G.top}`)).json();
    expect(grp.snmp_profile_name).toBe(P.v2c.name);
    for (const t of [JSON.stringify(dev), JSON.stringify(grp), await page.content()]) {
      for (const s of [SECRET_COMMUNITY, SECRET_V3_AUTH, SECRET_V3_PRIV, SSH_PASS, 'public']) expect(t).not.toContain(s);
    }
    for (const path of ['/settings', '/settings?tab=connectionProfiles']) {
      await page.goto(path);
      await expect(page.locator('main')).toContainText('You do not have permission for this action.');
      await expect(page.locator('main')).not.toContainText('Connection profiles');
      await expect(page.getByTestId('profiles-table').or(page.getByTestId('profiles-list'))).toHaveCount(0);
    }
    const calls: [string, string, unknown?][] = [
      ['GET', '/api/connection-profiles'],
      ['GET', `/api/connection-profiles/${P.v2c.id}`],
      ['GET', `/api/connection-profiles/${P.v2c.id}/usage`],
      ['POST', '/api/connection-profiles', { kind: 'snmp', name: 'x', credentials: { version: 'v2c' } }],
      ['PATCH', `/api/connection-profiles/${P.v2c.id}`, { description: 'x' }],
      ['DELETE', `/api/connection-profiles/${P.v2c.id}`],
      ['POST', `/api/connection-profiles/${P.v2c.id}/test`, { host: PROF_HOSTS[0] }],
      ['POST', '/api/connection-profiles/test', { kind: 'snmp', credentials: { version: 'v2c' }, host: PROF_HOSTS[0] }],
      ['POST', `/api/devices/${D.sw}/credentials/snmp/copy-from-profile`, {}],
      ['POST', `/api/groups/${G.top}/credentials/snmp/copy-from-profile`, {}],
      ['PATCH', `/api/devices/${D.sw}`, { snmp_profile_id: P.v3.id }],
    ];
    for (const [m, u, b] of calls) {
      const r = await page.request.fetch(u, { method: m, data: b });
      expect(r.status(), `${m} ${u}`).toBe(403);
    }
  } finally {
    await admin.patch(`/api/devices/${D.sw}`, { snmp_credentials: null, ssh_credentials: null });
  }
});

test('12: tree move between groups with different profiles: the preview names both profiles', async ({ page }) => {
  G.moveA = (await admin.post('/api/groups', { name: `${tag} MoveA`, parent_id: null, snmp_profile_id: P.v2c.id })).id;
  G.moveB = (await admin.post('/api/groups', { name: `${tag} MoveB`, parent_id: null, snmp_profile_id: P.v3.id })).id;
  const dv = await admin.post('/api/devices', { group_id: G.moveA, name: `${tag}-mv`, host: PROF_HOSTS[1], auto_sensors: [] });
  await snmpSensor(dv.id);
  const pv = await admin.post('/api/tree/move/preview', { device_ids: [dv.id], target_group_id: G.moveB });
  const snmp = pv.effects.find((e: any) => e.setting === 'snmp');
  expect(snmp.before).toMatchObject({ source: 'group', group_id: G.moveA, profile_id: P.v2c.id, profile_name: P.v2c.name });
  expect(snmp.after).toMatchObject({ source: 'group', group_id: G.moveB, profile_id: P.v3.id, profile_name: P.v3.name });

  await loginViaApi(page, undefined, `/groups/${G.moveA}`);
  await page.getByTestId('device-row').filter({ hasText: `${tag}-mv` }).getByTestId('move-select').check();
  await page.getByTestId('group-selection-move').click();
  const pick = page.getByTestId('move-picker');
  await pick.getByLabel(`${tag} MoveB`).check();
  await pick.getByTestId('move-picker-next').click();
  const eff = page.getByTestId('move-confirm').getByTestId('move-effect-snmp');
  await expect(eff).toContainText(`inherited from group ${tag} MoveA · profile ${P.v2c.name}`);
  await expect(eff).toContainText(`inherited from group ${tag} MoveB · profile ${P.v3.name}`);
  await shot(page, '96-12-tree-move-preview-en');
  await page.getByTestId('move-confirm').getByRole('button', { name: 'Cancel' }).click();
  expect((await admin.get(`/api/devices/${dv.id}`)).group_id).toBe(G.moveA);
});

test('13: Hebrew RTL at 390 px: the profiles tab and the credential picker fit, no horizontal scroll', async ({ page }) => {
  await setLang(admin, 'he');
  try {
    await page.setViewportSize({ width: 390, height: 844 });
    await loginViaApi(page, undefined, '/settings?tab=connectionProfiles');
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
    const list = page.getByTestId('profiles-list');
    await expect(list).toContainText(P.v2c.name);
    await list.scrollIntoViewIfNeeded();
    let o = await horizontalOverflow(page);
    expect(o.scrollWidth, 'profiles tab overflow').toBeLessThanOrEqual(o.clientWidth);
    await shot(page, '96-13-mobile-profiles-he');
    await page.goto(`/devices/${D.sw}`);
    await page.getByRole('button', { name: 'עריכה' }).first().click();
    await modeBtn(page, 'snmp', 'פרופיל').click();
    await expect(picker(page, 'snmp')).toContainText('פרופיל חיבור');
    await picker(page, 'snmp').scrollIntoViewIfNeeded();
    o = await horizontalOverflow(page);
    expect(o.scrollWidth, 'device form overflow').toBeLessThanOrEqual(o.clientWidth);
    const box = await picker(page, 'snmp').boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(390);
    await shot(page, '96-13-mobile-picker-he');
  } finally {
    await setLang(admin, 'en');
  }
});
