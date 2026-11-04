import { expect, test } from '@playwright/test';
import type { ChildProcess } from 'node:child_process';
import { Api, WEBHOOK_URL, apiAs, clearWebhookRecords, defaultGroupId, loginViaApi, uid, webhookRecords } from '../lib/helpers';
import {
  TG_CHAT_OK,
  TG_VALID_TOKEN,
  clearTelegram,
  raw,
  scanResults,
  startHttpHost,
  stopHost,
  telegramDelivered,
  waitForScan,
  waitNoRunningScan,
} from '../lib/lab';

// Phase 2a (f): a discovery profile with a schedule; "Run now" (manual, sets last_run_at = the baseline);
// then a new simulated host appears and the scheduled run reports a new-host event + notifications
// (webhook + Telegram) for exactly that host (data-model.md § discovery_profiles, api.md § Discovery
// notification payload). Uses 127.0.0.40-45 so the hosts of specs 10/11 do not interfere.
test.describe.configure({ mode: 'serial' });

let api: Api;
let hookId = 0;
let tgId = 0;
let profileId = 0;
const profileName = uid('Lab profile');
let host40: ChildProcess | null = null;
let host41: ChildProcess | null = null;

test.beforeAll(async () => {
  api = await apiAs();
  await waitNoRunningScan(api);
  hookId = (await api.post('/api/notification-channels', { name: uid('disc-hook'), type: 'webhook', enabled: true, config: { url: `${WEBHOOK_URL}/discovery` } })).id;
  tgId = (
    await api.post('/api/notification-channels', {
      name: uid('disc-tg'),
      type: 'telegram',
      enabled: true,
      config: { bot_token: TG_VALID_TOKEN, chat_id: TG_CHAT_OK, parse_mode: 'HTML', disable_notification: false },
    })
  ).id;
  host40 = await startHttpHost('127.0.0.40');
});

test.afterAll(async () => {
  stopHost(host40);
  stopHost(host41);
  if (profileId) await api.patch(`/api/discovery/profiles/${profileId}`, { schedule_enabled: false }).catch(() => undefined);
});

test('create a scheduled profile in the UI (Daily, notify on new hosts)', async ({ page }) => {
  await loginViaApi(page, undefined, '/discovery');
  await page.getByRole('tab', { name: 'Saved profiles' }).click();
  await page.getByRole('button', { name: 'Add profile' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel(/^Name/).fill(profileName);
  await dlg.getByLabel(/^Targets/).fill('127.0.0.40-45');
  await dlg.getByRole('checkbox', { name: /^Ping/ }).uncheck();
  await dlg.getByRole('checkbox', { name: /^SNMP/ }).first().uncheck();
  await dlg.getByRole('checkbox', { name: /^(Reverse )?DNS/ }).uncheck();
  await dlg.getByRole('textbox', { name: /^TCP ports/ }).fill('8080');
  await dlg.getByLabel(/^Runs/).selectOption({ label: 'Daily' });
  await dlg.getByRole('checkbox', { name: /Notify on new hosts/ }).check();
  await dlg.getByRole('checkbox', { name: new RegExp((await api.get(`/api/notification-channels/${hookId}`)).name) }).check();
  await dlg.getByRole('checkbox', { name: new RegExp((await api.get(`/api/notification-channels/${tgId}`)).name) }).check();
  await dlg.getByRole('button', { name: 'Save' }).click();
  await expect(dlg).toBeHidden();
  await expect(page.getByRole('row').filter({ hasText: profileName })).toBeVisible();

  const p = (await api.get('/api/discovery/profiles')).items.find((x: any) => x.name === profileName);
  profileId = p.id;
  expect(p).toMatchObject({
    targets: ['127.0.0.40-45'],
    group_id: await defaultGroupId(api),
    schedule_enabled: true,
    schedule_every_hours: 24,
    notify_new_hosts: true,
    last_run_at: null,
  });
  expect([...p.notify_channel_ids].sort()).toEqual([hookId, tgId].sort());
  expect(p.options).toMatchObject({ ping: false, snmp: false, dns: false, tcp: true, tcp_ports: [8080] });
  // next_run_at = now + 1 minute when the schedule is enabled.
  const dt = Date.parse(p.next_run_at) - Date.now();
  expect(dt).toBeGreaterThan(0);
  expect(dt).toBeLessThan(70_000);
});

test('"Run now" -> manual scan finds 127.0.0.40, no notification', async ({ page }) => {
  await clearWebhookRecords();
  await clearTelegram();
  await loginViaApi(page, undefined, '/discovery');
  await page.getByRole('tab', { name: 'Saved profiles' }).click();
  await page.getByRole('row').filter({ hasText: profileName }).getByRole('button', { name: 'Run now' }).click();
  await expect(page).toHaveURL(/\/discovery\/scans\/\d+$/);
  const scanId = Number(page.url().split('/').pop());
  const scan = await waitForScan(api, scanId);
  expect(scan).toMatchObject({ status: 'completed', trigger: 'manual', profile_id: profileId, profile_name: profileName, total_hosts: 6, found_hosts: 1, new_hosts: 1 });
  expect((await scanResults(api, scanId)).map((r) => r.ip)).toEqual(['127.0.0.40']);
  const p = await api.get(`/api/discovery/profiles/${profileId}`);
  expect(p.last_scan_id).toBe(scanId);
  expect(p.last_run_at).toBeTruthy();
  // Manual runs never notify.
  await new Promise((r) => setTimeout(r, 2000));
  expect((await webhookRecords()).filter((r) => r.body?.event === 'discovery.new_hosts')).toEqual([]);
  expect(await telegramDelivered()).toEqual([]);
});

test('scheduled run after a new host appears -> event + notifications for that host only', async () => {
  test.setTimeout(240_000);
  host41 = await startHttpHost('127.0.0.41');
  let sched: any = null;
  await expect
    .poll(
      async () => {
        const scans = await api.get(`/api/discovery/scans?profile_id=${profileId}`);
        sched = scans.items.find((s: any) => s.trigger === 'scheduled' && s.status !== 'running');
        return !!sched;
      },
      { timeout: 180_000, intervals: [2000, 5000], message: 'scheduled scan of the profile (next_run_at + 30 s loop)' },
    )
    .toBe(true);
  expect(sched).toMatchObject({ status: 'completed', profile_id: profileId, created_by: null, found_hosts: 2, new_hosts: 1 });
  const res = await scanResults(api, sched.id);
  expect(res.map((r) => [r.ip, r.is_new])).toEqual([
    ['127.0.0.40', false],
    ['127.0.0.41', true],
  ]);

  // next_run_at moved by schedule_every_hours.
  const p = await api.get(`/api/discovery/profiles/${profileId}`);
  const ahead = Date.parse(p.next_run_at) - Date.now();
  expect(ahead).toBeGreaterThan(23 * 3600_000);
  expect(p.last_scan_id).toBe(sched.id);

  // discovery event
  const ev = (await api.get('/api/events?kind=discovery')).items.filter((e: any) => e.message.includes(profileName));
  expect(ev).toHaveLength(1);
  expect(ev[0]).toMatchObject({ sensor_id: null, device_id: null, group_id: null });
  expect(ev[0].message).toContain(`Discovery "${profileName}": 1 new host`);
  expect(ev[0].message).toContain('127.0.0.41');
  expect(ev[0].message).not.toContain('127.0.0.40');

  // webhook payload
  await expect.poll(async () => (await webhookRecords()).filter((r) => r.body?.event === 'discovery.new_hosts').length, { timeout: 20_000 }).toBe(1);
  const hook = (await webhookRecords()).find((r) => r.body?.event === 'discovery.new_hosts');
  expect(hook.body).toMatchObject({
    event: 'discovery.new_hosts',
    event_id: ev[0].id,
    old_status: null,
    new_status: null,
    sensor: null,
    device: null,
    rule: null,
    profile: { id: profileId, name: profileName },
    scan_id: sched.id,
    hosts: [{ ip: '127.0.0.41', hostname: null, device_class: 'unknown', vendor: null }],
  });
  expect(hook.body.message.startsWith(`Discovery "${profileName}": 1 new host`)).toBe(true);
  expect(hook.body.group).toMatchObject({ id: await defaultGroupId(api) });

  // telegram
  await expect.poll(async () => (await telegramDelivered()).length, { timeout: 20_000 }).toBe(1);
  const tg = (await telegramDelivered())[0].body.text as string;
  expect(tg.split('\n')[0]).toBe(`<b>[NetworkMonitor] New hosts discovered: ${profileName} (1)</b>`);
  expect(tg).toContain('127.0.0.41');
  expect(tg).not.toContain('127.0.0.40');
});

test('profile API: secrets write-only, PATCH merge, validation, 409 on duplicate name', async () => {
  const gid = await defaultGroupId(api);
  const name = uid('api-profile');
  const p = await api.post('/api/discovery/profiles', {
    name,
    targets: ['127.0.0.50'],
    group_id: gid,
    options: { extra_communities: ['secret-comm'], snmp_port: 1161 },
  });
  expect(p).toMatchObject({ schedule_enabled: false, next_run_at: null, notify_new_hosts: false, notify_channel_ids: [] });
  expect(p.options).toMatchObject({ extra_communities: null, extra_communities_count: 1, snmp_port: 1161 });
  expect(JSON.stringify(p)).not.toContain('secret-comm');
  const merged = await api.patch(`/api/discovery/profiles/${p.id}`, { options: { tcp_ports: [80] } });
  expect(merged.options).toMatchObject({ extra_communities_count: 1, snmp_port: 1161, tcp_ports: [80] });
  const cleared = await api.patch(`/api/discovery/profiles/${p.id}`, { options: { extra_communities: [] } });
  expect(cleared.options.extra_communities_count).toBe(0);

  expect((await raw(api, 'POST', '/api/discovery/profiles', { name: name.toUpperCase(), targets: ['127.0.0.50'], group_id: gid })).status).toBe(409);
  expect((await raw(api, 'POST', '/api/discovery/profiles', { name: uid('p'), targets: ['127.0.0.50'], group_id: 999999 })).status).toBe(422);
  expect((await raw(api, 'POST', '/api/discovery/profiles', { name: uid('p'), targets: ['127.0.0.50'], group_id: gid, notify_channel_ids: [999999] })).status).toBe(422);
  const tooMany = await raw(api, 'PATCH', `/api/discovery/profiles/${p.id}`, { targets: ['10.0.0.0/8'] });
  expect(tooMany.status).toBe(422);
  expect(tooMany.body.error.code).toBe('too_many_hosts');
  expect((await raw(api, 'PATCH', `/api/discovery/profiles/${p.id}`, { schedule_every_hours: 721 })).status).toBe(422);

  // Schedule toggling sets/clears next_run_at.
  const on = await api.patch(`/api/discovery/profiles/${p.id}`, { schedule_enabled: true, schedule_every_hours: 168 });
  expect(Date.parse(on.next_run_at) - Date.now()).toBeLessThan(70_000);
  const off = await api.patch(`/api/discovery/profiles/${p.id}`, { schedule_enabled: false });
  expect(off.next_run_at).toBeNull();
  await api.del(`/api/discovery/profiles/${p.id}`);
  expect((await raw(api, 'GET', `/api/discovery/profiles/${p.id}`)).status).toBe(404);
});
