// [3] Config backup end to end against the SSH device simulator (docs/architecture.md §13.2, §13.11 spec 52,
// api.md "[3] Config backup"). SSH credentials on a group (zero per-device configuration besides the driver
// override, since the sim devices have no SNMP identity), Test, "Back up all" (Comware, ArubaOS, Check Point
// clish + bash fallback), versions, viewer, diff after a simulated change, download, search, ZIP export, and
// the read-only guarantee from the simulator's command log.
import { expect, test } from '@playwright/test';
import { apiAs, loginViaApi, type Api } from '../lib/helpers';
import { raw } from '../lib/lab';
import {
  PHASE3, SSH, clearSshLog, ensureDeviceAt, ensureGroupNamed, expectOnlyAllowedCommands, expectedConfig, setFeatures,
  shot3, sshCreds, sshDevice, sshState, tx, versions, waitBackup,
} from '../lib/lab3';

const GROUP = 'QA Backups 3';
const DEVS = [SSH.cw, SSH.aos, SSH.cp, SSH.cpBash];
// Fixture secrets that must never be stored while masking is on (tests/sim/ssh/expected.py FIXTURE_SECRETS subset)
const SECRETS = ['Pr1vateCommunity', 'R4diusSecret', '8c6976e5b5410415bde908bd4dee15dfb167a9c8', 'Gw5nmpComm',
  '$1$Rb2bVQ$Ks9q0m3Ge0Gyd3b4kM7Lz/', '$c$3$Qk9NQkFSRElBX1JFQURfQ09NTVVOSVRZ', '$h$6$Wm9vbWVyYW5nV2luZG93'];
// Comware usm-user line: the contract's driver regexes leave one of the two keys unmasked (QA contract defect,
// reported); compare everything else exactly.
const comparable = (t: string) => t.split('\n').filter((l) => !l.includes('snmp-agent usm-user')).join('\n');

test.describe.configure({ mode: 'serial' });
test.skip(!PHASE3, 'phase-3 simulators not running (tests/e2e/run.sh with E2E_PHASE3=1)');

let api: Api;
const ids: Record<string, number> = {};

test.beforeAll(async () => {
  api = await apiAs();
  for (const d of DEVS) await sshState(d.name, { version: 'v1', hang: false, huge: false });
  await clearSshLog();
});

test('feature ON, SSH credentials on a group (write-only secrets), devices inherit them', async () => {
  await setFeatures(api, { config_backup: true });
  const g = await ensureGroupNamed(api, GROUP, { ssh_credentials: sshCreds() });
  expect(g.ssh_credentials, 'group ssh_credentials echo').toMatchObject({
    username: 'netmon-backup', port: 8022, password: null, password_set: true, enable_password: null,
    enable_password_set: true, private_key: null, private_key_set: false, legacy_algorithms: false,
  });
  expect(JSON.stringify(g)).not.toContain('Backup-pass-1');
  for (const d of DEVS) {
    const dev = await ensureDeviceAt(api, g.id, d.ip, `qa3-${d.name}`, { config_backup_driver: d.driver, config_backup: 'auto' });
    ids[d.name] = dev.id;
    const full = await api.get(`/api/devices/${dev.id}`);
    expect(full.effective_ssh_source, `${d.name} credentials source`).toBe('group');
    expect(full.effective_ssh_group_id).toBe(g.id);
    expect(full.config_backup_driver).toBe(d.driver);
  }
  // unknown driver override -> 422
  const bad = await raw(api, 'PATCH', `/api/devices/${ids[SSH.cw.name]}`, { config_backup_driver: 'no_such_driver' });
  expect(bad.status, bad.text).toBe(422);
  const st = await api.get('/api/config-backups/status');
  expect(st.drivers.map((x: any) => x.id).sort()).toEqual(
    ['aruba_cx', 'checkpoint_gaia', 'cisco_ios', 'fortigate', 'hpe_arubaos_switch', 'hpe_comware', 'mikrotik_routeros']);
  expect(st.driver_errors).toEqual([]);
});

test('Test (ssh/test): connect, host key, auth, prompt, setup, command; masked preview; nothing stored', async () => {
  const id = ids[SSH.aos.name];
  const r = await raw(api, 'POST', `/api/devices/${id}/ssh/test`, { ssh_credentials: {}, driver: null });
  expect(r.status, r.text).toBe(200);
  expect(r.body.ok).toBe(true);
  expect(r.body.driver).toBe('hpe_arubaos_switch');
  const steps = r.body.steps.map((s: any) => s.step);
  for (const s of ['connect', 'host_key', 'auth', 'prompt', 'command']) expect(steps, `step ${s}`).toContain(s);
  expect(r.body.steps.every((s: any) => s.ok)).toBe(true);
  const fp = (await sshDevice(SSH.aos.name)).fingerprint;
  expect(JSON.stringify(r.body.steps), 'host_key step shows the SHA256 fingerprint').toContain(fp);
  expect(r.body.output_preview.split('\n').length).toBeLessThanOrEqual(21);
  for (const s of SECRETS) expect(r.body.output_preview).not.toContain(s);
  expect(await versions(api, id), 'Test never stores a version').toEqual([]);
  // wrong password -> 502 ssh_error with step auth (the stored credentials are not changed)
  const w = await raw(api, 'POST', `/api/devices/${id}/ssh/test`, { ssh_credentials: { password: 'wrong' } });
  expect(w.status, w.text).toBe(502);
  expect(w.body.error.code).toBe('ssh_error');
  expect(w.body.error.details?.[0]).toMatchObject({ error: 'auth_failed', step: 'auth' });
  expect(w.body.error.message).toMatch(/^auth_failed/);
});

test('"Back up all" in the UI: every device stores an initial version equal to the normalised fixture', async ({ page }) => {
  test.setTimeout(180_000);
  await loginViaApi(page, undefined, '/config-backups');
  for (const d of DEVS) await expect(page.getByRole('main').getByText(`qa3-${d.name}`).first()).toBeVisible({ timeout: 20_000 });
  await page.getByRole('button', { name: tx('configBackup.backupAll', /back up all/i) }).click();
  const dlg = page.getByRole('dialog');
  if (await dlg.isVisible().catch(() => false)) await dlg.getByRole('button', { name: /back up|confirm|ok|yes/i }).last().click();
  for (const d of DEVS) {
    const s = await waitBackup(api, ids[d.name], (x) => x.last_result === 'ok', 120_000, `initial backup ${d.name}`);
    expect(s.last_trigger, `${d.name} trigger`).toBe('manual');
    expect(s.driver).toBe(d.driver);
    expect(s.host_key?.state).toBe('pinned');
    expect(s.host_key?.fingerprint).toBe((await sshDevice(d.name)).fingerprint);
    const v = await versions(api, ids[d.name]);
    expect(v).toHaveLength(1);
    expect(v[0]).toMatchObject({ initial: true, is_latest: true, masked: true, version_no: 1, added: 0, removed: 0 });
    const full = await api.get(`/api/devices/${ids[d.name]}/config/versions/${v[0].id}`);
    const want = expectedConfig(d.name, 'v1');
    if (d === SSH.cw) expect(comparable(full.content)).toBe(comparable(want));
    else expect(full.content, `${d.name} stored content`).toBe(want);
    for (const s of SECRETS) expect(full.content, `${d.name} secret ${s} masked`).not.toContain(s);
    expect(full.lines).toBe(want.split('\n').length - 1);
  }
  await page.reload();
  await expect(page.getByRole('main').getByText(`qa3-${SSH.cp.name}`).first()).toBeVisible();
  await shot3(page, '52-config-backups');
});

test('second run unchanged (volatile lines ignored), then a change -> version 2 with +/- and a diff', async ({ page }) => {
  test.setTimeout(180_000);
  const id = ids[SSH.aos.name];
  const r1 = await raw(api, 'POST', `/api/devices/${id}/config/backup`);
  expect(r1.status, r1.text).toBe(202);
  let s = await waitBackup(api, id, (x) => x.last_result === 'unchanged', 60_000, 'unchanged run');
  expect(await versions(api, id)).toHaveLength(1);
  await sshState(SSH.aos.name, { version: 'v2' });
  expect((await raw(api, 'POST', `/api/devices/${id}/config/backup`)).status).toBe(202);
  s = await waitBackup(api, id, (x) => x.last_result === 'ok' && x.versions === 2, 60_000, 'changed run');
  expect(s.last_change_at).not.toBeNull();
  const v = await versions(api, id);
  expect(v[0]).toMatchObject({ version_no: 2, initial: false, is_latest: true, added: 2, removed: 1 });
  expect(v[1]).toMatchObject({ version_no: 1, is_latest: false });
  const d = await api.get(`/api/devices/${id}/config/diff?from=${v[1].id}&to=${v[0].id}`);
  expect(d).toMatchObject({ added: 2, removed: 1, truncated: false });
  expect(d.diff).toContain('-   name "pc-1234"');
  expect(d.diff).toContain('+   name "pc-5678"');
  expect(d.diff).toContain('+   ip igmp');
  // the UI: device page -> Configuration tab -> viewer + diff
  await loginViaApi(page, undefined, `/devices/${id}`);
  await page.getByRole('tab', { name: tx('devices.tab.config', /configuration/i) }).click();
  await page.getByRole('row').getByRole('button', { name: tx('configBackup.view', /^view$/i), exact: true }).first().click();
  await expect(page.getByText('hostname "sw-aos1"').first()).toBeVisible({ timeout: 20_000 });
  await page.getByRole('button', { name: /^(diff|compare versions)$/i }).first().click();
  await expect(page.getByText('pc-5678').first()).toBeVisible();
  await expect(page.getByText('pc-1234').first()).toBeVisible();
  await shot3(page, '52-config-diff');
});

test('download one version and the ZIP of all latest; both audited content stays masked', async () => {
  const id = ids[SSH.aos.name];
  const v = await versions(api, id);
  const r = await api.ctx.fetch(`/api/devices/${id}/config/versions/${v[0].id}/download`);
  expect(r.status()).toBe(200);
  expect(r.headers()['content-type']).toMatch(/^text\/plain/);
  expect(r.headers()['content-disposition']).toMatch(/attachment; filename="?qa3-sw-aos1_\d{8}-\d{4}\.cfg"?/);
  expect(await r.text()).toBe(expectedConfig(SSH.aos.name, 'v2'));
  const z = await api.ctx.fetch(`/api/config-backups/export.zip`);
  expect(z.status()).toBe(200);
  expect(z.headers()['content-type']).toMatch(/application\/zip/);
  expect(z.headers()['content-disposition']).toMatch(/netmon-configs-\d{8}-\d{4}\.zip/);
  const buf = await z.body();
  expect(buf.subarray(0, 2).toString()).toBe('PK');
  const names = buf.toString('latin1');
  for (const d of DEVS) expect(names, `zip entry for qa3-${d.name}`).toContain(`qa3-${d.name}.cfg`);
  for (const s of SECRETS) expect(names).not.toContain(s);
});

test('search across devices (latest versions), regex validation, and the UI search box', async ({ page }) => {
  const r = await api.get('/api/config-backups/search?q=' + encodeURIComponent('vlan 20'));
  const byName = Object.fromEntries(r.items.map((x: any) => [x.device_name, x]));
  expect(byName['qa3-sw-cw1'], 'Comware has "vlan 20"').toBeTruthy();
  expect(byName['qa3-sw-aos1'], 'ArubaOS has "vlan 20"').toBeTruthy();
  expect(byName['qa3-sw-aos1'].matches.some((m: any) => m.text.includes('vlan 20') && m.line > 0)).toBe(true);
  expect(byName['qa3-gw-hq'], 'Check Point has no "vlan 20"').toBeUndefined();
  const bad = await raw(api, 'GET', '/api/config-backups/search?regex=true&q=' + encodeURIComponent('(unclosed'));
  expect(bad.status, bad.text).toBe(422);
  const short = await raw(api, 'GET', '/api/config-backups/search?q=v');
  expect(short.status).toBe(422);
  // masked secrets are not searchable
  const sec = await api.get('/api/config-backups/search?q=Pr1vateCommunity');
  expect(sec.items).toEqual([]);
  await loginViaApi(page, undefined, '/config-backups');
  const main = page.getByRole('main');
  await main.getByLabel(tx('configBackup.searchLabel', /text to find/i)).fill('ntp-service unicast-server');
  await main.getByRole('button', { name: tx('configBackup.searchGo', /^search$/i), exact: true }).click();
  await expect(main.getByText(/ntp-service unicast-server 10\.0\.0\.10/).first()).toBeVisible({ timeout: 15_000 });
  await shot3(page, '52-config-search');
});

test('read-only guarantee: the simulator received only allow-listed commands; bash login used the fallback', async () => {
  await expectOnlyAllowedCommands();
  const s = await waitBackup(api, ids[SSH.cpBash.name], (x) => x.last_result === 'ok');
  expect(s.versions).toBe(1);
  const log = (await (await fetch(`${process.env.SSH_SIM_URL}/log?device=gw-bash&event=exec`)).json()) as any[];
  expect(log.map((x) => x.command)).toContain('clish -c "show configuration"');
  // (kept last: a failure here must not skip the read-only check above)
  const rx = await api.get('/api/config-backups/search?regex=true&q=' + encodeURIComponent('^set dns (primary|secondary)'));
  // line-anchored regex (results are lines): PostgreSQL `~*` on the whole text anchors ^ at the text start only
  expect(rx.items.map((x: any) => x.device_name).sort(), 'line-anchored regex search').toEqual(['qa3-gw-bash', 'qa3-gw-hq']);
  await sshState(SSH.aos.name, { version: 'v1' });
});
