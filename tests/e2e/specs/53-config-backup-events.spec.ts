// [3] Config backup events (docs/architecture.md §13.2.3, §13.2.4, §13.2.6, §13.11 spec 53):
// host-key change -> config_backup sensor down (condition host_key) -> accept new key (UI) -> next run ok;
// Comware CFGMAN syslog line with the built-in trigger rule -> one backup after the 120 s debounce (test clock)
// -> config_change event in the timeline; the backup's own login does not re-trigger it; optional alert via
// webhook when config_change_alert is on.
import { expect, test } from '@playwright/test';
import { apiAs, clearWebhookRecords, loginViaApi, webhookRecords, WEBHOOK_URL, type Api } from '../lib/helpers';
import { raw } from '../lib/lab';
import { resetClock, setClock } from '../lib/lab2b';
import {
  PHASE3, SSH, clearSshLog, ensureDeviceAt, ensureGroupNamed, restoreHostKey, rotateHostKey, sendSyslog,
  setFeatures, shot3, sshCreds, sshDevice, sshLog, sshState, tx, versions, waitBackup,
} from '../lib/lab3';

test.describe.configure({ mode: 'serial' });
test.skip(!PHASE3, 'phase-3 simulators not running');

let api: Api;
let cw: any; // the Comware sim device (127.0.0.111)
let sensorId = 0;

async function backupSensor(deviceId: number): Promise<any> {
  const tree = await api.get('/api/tree');
  const all: any[] = [];
  const walk = (gs: any[]) => gs.forEach((g) => { (g.devices ?? []).forEach((d: any) => d.id === deviceId && all.push(...d.sensors)); walk(g.children ?? []); });
  walk(tree.groups);
  return all.find((s) => s.type === 'config_backup') ?? null;
}

test.beforeAll(async () => {
  api = await apiAs();
  await setFeatures(api, { config_backup: true });
  const g = await ensureGroupNamed(api, 'QA Backups 3', { ssh_credentials: sshCreds() });
  cw = await ensureDeviceAt(api, g.id, SSH.cw.ip, 'qa3-sw-cw1', { config_backup_driver: 'hpe_comware' });
  await sshState(SSH.cw.name, { version: 'v1' });
  await restoreHostKey(SSH.cw.name);
});

test.afterAll(async () => {
  await resetClock(api).catch(() => undefined);
  await restoreHostKey(SSH.cw.name).catch(() => undefined);
  await sshState(SSH.cw.name, { version: 'v1' }).catch(() => undefined);
});

test('a config_backup event sensor exists for the eligible device', async () => {
  // a first successful backup pins the key (spec 52 may already have done it)
  expect((await raw(api, 'POST', `/api/devices/${cw.id}/config/backup`)).status).toBe(202);
  await waitBackup(api, cw.id, (s) => ['ok', 'unchanged'].includes(s.last_result));
  await expect.poll(async () => (await backupSensor(cw.id))?.id ?? 0, { timeout: 30_000, message: 'config_backup sensor auto-created' }).toBeGreaterThan(0);
  const s = await backupSensor(cw.id);
  sensorId = s.id;
  expect(s.status).toBe('up');
});

test('host key rotated -> host_key_changed before auth, sensor down, UI shows both fingerprints, accept -> ok', async ({ page }) => {
  test.setTimeout(150_000);
  const before = await sshDevice(SSH.cw.name);
  const rot = await rotateHostKey(SSH.cw.name);
  expect(rot.fingerprint).not.toBe(before.fingerprint);
  await clearSshLog();
  expect((await raw(api, 'POST', `/api/devices/${cw.id}/config/backup`)).status).toBe(202);
  const st = await waitBackup(api, cw.id, (s) => s.last_result === 'failed', 60_000, 'host-key failure');
  expect(st.last_error).toBe('host_key_changed');
  expect(st.eligible).toBe(false);
  expect(st.reason).toBe('host_key_changed');
  expect(st.host_key).toMatchObject({ state: 'changed', fingerprint: before.fingerprint, presented_fingerprint: rot.fingerprint });
  const auth = (await sshLog(SSH.cw.name)).filter((e) => e.event.startsWith('auth'));
  expect(auth, 'no credentials were offered to the impostor').toEqual([]);
  await expect.poll(async () => (await api.get(`/api/sensors/${sensorId}`)).status, { timeout: 30_000 }).toBe('down');
  const conds = await api.get(`/api/sensors/${sensorId}/conditions`);
  expect(conds.items.map((c: any) => c.source)).toContain('host_key');
  // a system event carries both fingerprints
  const ev = await api.get(`/api/events?kind=system&page_size=50`);
  expect(JSON.stringify(ev.items ?? ev), 'system event with both fingerprints').toContain(rot.fingerprint);
  // further attempts are skipped until accept
  const again = await raw(api, 'POST', `/api/devices/${cw.id}/config/backup`);
  expect(again.status, again.text).toBe(400);
  expect(JSON.stringify(again.body?.error?.details ?? [])).toContain('host_key_changed');
  // accept with a stale fingerprint -> 409 host_key_mismatch
  const mm = await raw(api, 'POST', `/api/devices/${cw.id}/config/host-key/accept`, { fingerprint: before.fingerprint });
  expect(mm.status, mm.text).toBe(409);
  expect(mm.body.error.code).toBe('host_key_mismatch');
  // UI accept
  await loginViaApi(page, undefined, `/devices/${cw.id}`);
  await page.getByRole('tab', { name: tx('devices.tab.config', /configuration/i) }).click();
  await expect(page.getByText(before.fingerprint).first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(rot.fingerprint).first()).toBeVisible();
  await shot3(page, '53-host-key-changed');
  await page.getByRole('button', { name: tx('hostKey.accept', /accept new key/i) }).click();
  const dlg = page.getByRole('dialog');
  if (await dlg.isVisible().catch(() => false)) await dlg.getByRole('button', { name: /accept|confirm|ok/i }).last().click();
  await expect.poll(async () => (await api.get(`/api/devices/${cw.id}/config`)).host_key?.state, { timeout: 20_000 }).toBe('pinned');
  const hk = (await api.get(`/api/devices/${cw.id}/config`)).host_key;
  expect(hk.fingerprint).toBe(rot.fingerprint);
  expect(hk.accepted_by).toBe('admin');
  // condition removed, next run ok
  await expect.poll(async () => (await api.get(`/api/sensors/${sensorId}`)).status, { timeout: 30_000 }).toBe('up');
  expect((await raw(api, 'POST', `/api/devices/${cw.id}/config/backup`)).status).toBe(202);
  await waitBackup(api, cw.id, (s) => ['ok', 'unchanged'].includes(s.last_result) && s.host_key?.state === 'pinned');
  // accept again -> 400 no_host_key_change
  const nc = await raw(api, 'POST', `/api/devices/${cw.id}/config/host-key/accept`, { fingerprint: rot.fingerprint });
  expect(nc.status).toBe(400);
  expect(nc.body.error.code).toBe('no_host_key_change');
  // reset the pin -> next connection pins again (back to key 0 for the other specs)
  await restoreHostKey(SSH.cw.name);
  expect((await raw(api, 'DELETE', `/api/devices/${cw.id}/config/host-key`)).status).toBe(204);
  expect((await raw(api, 'POST', `/api/devices/${cw.id}/config/backup`)).status).toBe(202);
  const re = await waitBackup(api, cw.id, (s) => ['ok', 'unchanged'].includes(s.last_result) && !!s.host_key);
  expect(re.host_key.fingerprint).toBe((await sshDevice(SSH.cw.name)).fingerprint);
});

test('CFGMAN syslog with the built-in rule -> one backup 120 s after the last trigger -> config_change in the timeline', async ({ page }) => {
  test.setTimeout(240_000);
  await setFeatures(api, { syslog: true, timeline: true });
  const rules = (await api.get('/api/event-rules?kind=syslog')).items;
  const rule = rules.find((r: any) => r.builtin === 'syslog_comware_cfg_changed');
  expect(rule, 'built-in syslog_comware_cfg_changed seeded').toBeTruthy();
  expect(rule.action).toBe('config_backup');
  await api.patch(`/api/event-rules/${rule.id}`, { enabled: true });
  // optional alert: config_change_alert on + webhook rule for the config_backup sensor
  await api.put('/api/settings', { config_change_alert: true, config_change_clear_min: 5 });
  const ch = await api.post('/api/notification-channels', { name: 'qa3-cfg-hook', type: 'webhook', config: { url: `${WEBHOOK_URL}/qa3-config` } });
  await api.post('/api/alert-rules', { name: 'qa3 config change', enabled: true, sensor_id: sensorId, on_statuses: ['warning', 'down'], delay_s: 0, channel_ids: [ch.id] });
  await clearWebhookRecords();
  const v0 = (await versions(api, cw.id)).length;
  await sshState(SSH.cw.name, { version: 'v2' });
  await clearSshLog();
  const line = (n: number) => `<189>Nov  8 12:0${n}:00 2026 sw-cw1 %%10CFGMAN/5/CFGMAN_CFGCHANGED: -EventIndex=${n}-CommandSource=CLI-ConfigSource=Running-ConfigDestination=Running; Configuration changed.`;
  sendSyslog(SSH.cw.ip, [line(1)]);
  await new Promise((r) => setTimeout(r, 1500));
  sendSyslog(SSH.cw.ip, [line(2)]);
  await new Promise((r) => setTimeout(r, 1500));
  sendSyslog(SSH.cw.ip, [line(3)]);
  // the records are attributed to the device and matched by the rule
  await expect.poll(async () => {
    const p = await api.get(`/api/syslog?device_id=${cw.id}&limit=10`);
    return (p.items ?? []).filter((x: any) => (x.matched_rule_ids ?? []).includes(rule.id)).length;
  }, { timeout: 30_000, message: 'CFGMAN lines matched by the built-in rule' }).toBeGreaterThanOrEqual(3);
  const due = await api.get(`/api/devices/${cw.id}/config`);
  // contract: next_due_at = next scheduled/retry/DEBOUNCED run (data-model config_backup_status)
  expect.soft(due.next_due_at, 'debounced run scheduled (next_due_at)').not.toBeNull();
  // nothing runs before the debounce has passed
  await new Promise((r) => setTimeout(r, 5000));
  expect((await sshLog(SSH.cw.name)).filter((e) => e.event === 'connect')).toEqual([]);
  await setClock(api, 130);
  const st = await waitBackup(api, cw.id, (s) => s.last_trigger === 'event' && s.last_result === 'ok', 90_000, 'triggered backup');
  expect(st.versions).toBe(v0 + 1);
  const connects = (await sshLog(SSH.cw.name)).filter((e) => e.event === 'connect');
  expect(connects, 'one backup for a burst of three lines').toHaveLength(1);
  // config_change event + timeline item kind config
  const tl = await api.get(`/api/devices/${cw.id}/timeline?kinds=config&limit=10`);
  const item = tl.items.find((x: any) => x.kind === 'config');
  expect(item, 'timeline item kind config').toBeTruthy();
  expect(item.message).toMatch(/Configuration changed \(\+5 .2 lines\), version \d+/);
  // the backup's own session (session-only commands) produced no new trigger: no further run is due
  await new Promise((r) => setTimeout(r, 3000));
  const after = await api.get(`/api/devices/${cw.id}/config`);
  expect(after.running).toBe(false);
  // optional alert (config_change_alert): config_changed condition -> warning -> webhook
  await expect.poll(async () => (await api.get(`/api/sensors/${sensorId}`)).status, { timeout: 30_000 }).toBe('warning');
  await expect.poll(async () => (await webhookRecords()).filter((r) => r.path === '/qa3-config').length, { timeout: 30_000 }).toBeGreaterThanOrEqual(1);
  await loginViaApi(page, undefined, `/devices/${cw.id}`);
  await page.getByRole('tab', { name: tx('devices.tab.timeline', /timeline/i) }).click();
  await expect(page.getByText(/Configuration changed|configuration/i).first()).toBeVisible({ timeout: 20_000 });
  await shot3(page, '53-timeline-config');
  await api.patch(`/api/event-rules/${rule.id}`, { enabled: false });
  await api.put('/api/settings', { config_change_alert: false });
  await resetClock(api);
});
