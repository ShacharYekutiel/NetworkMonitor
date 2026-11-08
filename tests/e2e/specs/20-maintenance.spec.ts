import { expect, test } from '@playwright/test';
import { Api, apiAs, clearWebhookRecords, defaultGroupId, loginViaApi, uid } from '../lib/helpers';
import { raw } from '../lib/lab';
import { createWebhook, hooks, httpDevice, quiet, resetClock, rule, setClock, shot2b, target, waitStatus } from '../lib/lab2b';

// [2b-1] (b) Maintenance windows (data-model.md § Maintenance windows, api.md [2b-1] Maintenance windows):
// a once window covering now for one device -> its problem sends nothing and is flagged in maintenance;
// test clock past the window end -> exactly one DOWN (after_maintenance) since it is still down; recovery
// afterwards notifies. A problem that starts and ends inside a window sends nothing. Weekly window
// occurrences preview + settings.timezone.
test.describe.configure({ mode: 'serial' });

let api: Api;
let dev: { device: any; sensor: any };
let ruleId = 0;
let windowId = 0;
const HOOK = `/maint-${Date.now().toString(36)}`;
const IP = '127.0.0.34';
const winName = uid('Night patch');

test.beforeAll(async () => {
  api = await apiAs();
  await resetClock(api);
  await target(IP, 'ok');
  dev = await httpDevice(api, { name: uid('maint-dev'), ip: IP, group_id: await defaultGroupId(api) });
  const ch = await createWebhook(api, HOOK);
  ruleId = (await rule(api, { sensor_id: dev.sensor.id, channel_ids: [ch] })).id;
  await waitStatus(api, dev.sensor.id, 'up', 45_000);
});

test.afterAll(async () => {
  await resetClock(api);
  await target(IP, 'ok');
  await raw(api, 'PUT', '/api/settings', { timezone: 'UTC' });
  if (ruleId) await raw(api, 'DELETE', `/api/alert-rules/${ruleId}`);
});

test('create a once window covering now from the Maintenance page (device scope)', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${dev.device.id}`);
  // architecture.md §10.3 says "Add maintenance window"; the UI labels the button "Maintenance" (see report).
  await page.getByRole('main').getByRole('button', { name: /^(Add maintenance window|Maintenance)$/ }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel(/^Name/).fill(winName);
  // Prefilled scope = this device.
  const scopeGroup = dlg.getByRole('group', { name: 'Applies to' });
  await expect(scopeGroup.getByRole('radio', { name: 'Device', exact: true })).toBeChecked();
  await expect(dlg.locator('select').filter({ has: page.locator(`option[value="${dev.device.id}"]`) })).toHaveValue(String(dev.device.id));
  await dlg.getByRole('button', { name: 'One-time' }).click();
  // Inputs are interpreted in settings.timezone (UTC here, the browser zone sent by the wizard is UTC too).
  const pad = (n: number) => String(n).padStart(2, '0');
  const fmt = (d: Date) => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  const start = new Date(Date.now() - 5 * 60_000);
  const end = new Date(Date.now() + 60 * 60_000);
  await dlg.getByLabel(/^Starts/).fill(fmt(start));
  await dlg.getByLabel(/^Ends/).fill(fmt(end));
  await dlg.getByLabel(/^Comment/).fill('QA firmware window');
  await dlg.getByRole('button', { name: 'Save' }).click();
  await expect(dlg).toBeHidden();

  const list = await api.get(`/api/maintenance-windows?device_id=${dev.device.id}`);
  const w = list.items.find((x: any) => x.name === winName);
  expect(w).toMatchObject({ scope: 'device', device_id: dev.device.id, kind: 'once', active_now: true, comment: 'QA firmware window', enabled: true });
  windowId = w.id;
  expect(Math.abs(Date.parse(w.ends_at) - Math.floor(end.getTime() / 60_000) * 60_000)).toBeLessThan(61_000);

  await page.goto('/maintenance');
  const active = page.locator('section').filter({ has: page.getByRole('heading', { name: /^Active now/ }) });
  await expect(active).toContainText(winName);
  await shot2b(page, 'maintenance-list-en');
  // Badge on the device page ("In maintenance until HH:MM").
  await page.goto(`/devices/${dev.device.id}`);
  await expect(page.getByText(/In maintenance until/).first()).toBeVisible();
  const d = await api.get(`/api/devices/${dev.device.id}`);
  expect(d.in_maintenance).toBe(true);
  expect(d.maintenance).toMatchObject({ window_id: windowId, name: winName });
  const s = await api.get(`/api/sensors/${dev.sensor.id}`);
  expect(s.in_maintenance).toBe(true);
  const act = await api.get('/api/maintenance/active');
  expect(act.device_ids).toContain(dev.device.id);
  expect(act.windows.map((x: any) => x.id)).toContain(windowId);
  const ev = (await api.get('/api/events?kind=maintenance&page_size=100')).items;
  expect(ev.some((e: any) => e.message === `Maintenance "${winName}" started` && e.device_id === dev.device.id)).toBe(true);
});

test('problem inside the window: no notification, event flagged in_maintenance', async ({ page }) => {
  await clearWebhookRecords();
  await target(IP, 'fail');
  await waitStatus(api, dev.sensor.id, 'down', 45_000);
  await quiet(8_000);
  expect(await hooks(HOOK)).toEqual([]);
  const ev = (await api.get(`/api/events?sensor_id=${dev.sensor.id}&kind=status_change&page_size=10`)).items;
  expect(ev[0]).toMatchObject({ new_status: 'down', in_maintenance: true });
  expect((await api.get('/api/events?in_maintenance=true&page_size=100')).items.map((e: any) => e.id)).toContain(ev[0].id);
  const dash = await api.get('/api/dashboard');
  expect(dash.maintenance.active_window_ids).toContain(windowId);
  expect(dash.maintenance.device_count).toBeGreaterThanOrEqual(1);
  await loginViaApi(page, undefined, `/sensors/${dev.sensor.id}`);
  await expect(page.getByText(/In maintenance/).first()).toBeVisible();
  await shot2b(page, 'maintenance-sensor-down-badge-en');
});

test('test clock past the window end -> one DOWN (after maintenance); then recovery notifies', async () => {
  const c = await setClock(api, 2 * 3600);
  expect(c.offset_s).toBe(7200);
  await expect.poll(async () => (await api.get(`/api/devices/${dev.device.id}`)).in_maintenance, { timeout: 15_000 }).toBe(false);
  await expect.poll(async () => (await hooks(HOOK)).length, { timeout: 20_000 }).toBe(1);
  const h = (await hooks(HOOK))[0].body;
  expect(h).toMatchObject({ event: 'sensor.status_changed', stage: 'problem', after_maintenance: true, new_status: 'down' });
  expect(typeof h.problem_since).toBe('string');
  const ev = (await api.get('/api/events?kind=maintenance&page_size=100')).items;
  expect(ev.some((e: any) => e.message === `Maintenance "${winName}" ended`)).toBe(true);
  await quiet(5_000);
  expect((await hooks(HOOK)).length).toBe(1);
  // Recovery outside the window.
  await target(IP, 'ok');
  await waitStatus(api, dev.sensor.id, 'up', 45_000);
  await expect.poll(async () => (await hooks(HOOK)).map((r) => r.body.new_status), { timeout: 20_000 }).toEqual(['down', 'up']);
  await resetClock(api);
  await api.del(`/api/maintenance-windows/${windowId}`);
});

test('problem that starts and ends inside a window sends nothing', async () => {
  await clearWebhookRecords();
  const w = await api.post('/api/maintenance-windows', {
    name: uid('Short window'),
    scope: 'device',
    device_id: dev.device.id,
    kind: 'once',
    starts_at: new Date(Date.now() - 60_000).toISOString(),
    ends_at: new Date(Date.now() + 3600_000).toISOString(),
  });
  await expect.poll(async () => (await api.get(`/api/devices/${dev.device.id}`)).in_maintenance, { timeout: 15_000 }).toBe(true);
  await target(IP, 'fail');
  await waitStatus(api, dev.sensor.id, 'down', 45_000);
  await target(IP, 'ok');
  await waitStatus(api, dev.sensor.id, 'up', 45_000);
  await setClock(api, 2 * 3600);
  await expect.poll(async () => (await api.get(`/api/devices/${dev.device.id}`)).in_maintenance, { timeout: 15_000 }).toBe(false);
  await quiet(6_000);
  expect(await hooks(HOOK)).toEqual([]);
  await resetClock(api);
  await api.del(`/api/maintenance-windows/${w.id}`);
});

test('weekly window: occurrences preview in the drawer and the API; schedule text', async ({ page }) => {
  await loginViaApi(page, undefined, '/maintenance');
  await page.getByRole('main').getByRole('button', { name: 'Add maintenance window' }).click();
  const dlg = page.getByRole('dialog');
  const name = uid('Weekly backup');
  await dlg.getByLabel(/^Name/).fill(name);
  await dlg.getByRole('group', { name: 'Applies to' }).getByRole('radio', { name: 'Device', exact: true }).check();
  await dlg.locator('select').filter({ has: page.locator(`option[value="${dev.device.id}"]`) }).selectOption(String(dev.device.id));
  await dlg.getByRole('button', { name: 'Weekly' }).click();
  await dlg.getByRole('button', { name: /^Sun/ }).click();
  await dlg.getByLabel(/^Start time/).fill('02:00');
  await dlg.getByLabel(/^Duration/).fill('90');
  await expect(dlg.getByText(/Next occurrences/)).toBeVisible();
  await dlg.getByRole('button', { name: 'Save' }).click();
  await expect(dlg).toBeHidden();
  const w = (await api.get(`/api/maintenance-windows?device_id=${dev.device.id}`)).items.find((x: any) => x.name === name);
  expect(w).toMatchObject({ kind: 'weekly', weekdays: [7], start_time: '02:00', duration_min: 90, timezone: 'UTC', starts_at: null, ends_at: null });
  const occ = (await api.get(`/api/maintenance-windows/${w.id}/occurrences?days=14`)).items;
  expect(occ.length === 2 || occ.length === 3).toBe(true);
  for (const o of occ) {
    const s = new Date(o.start);
    expect(s.getUTCDay()).toBe(0);
    expect(s.getUTCHours()).toBe(2);
    expect(s.getUTCMinutes()).toBe(0);
    expect(Date.parse(o.end) - Date.parse(o.start)).toBe(90 * 60_000);
  }
  // List shows the schedule text in the configured time zone.
  const row = page.locator('section').filter({ has: page.getByRole('heading', { name: /^Recurring/ }) });
  await expect(row).toContainText(name);
  // en text per i18n maintenance.scheduleWeekly ("Every {{days}} at {{time}} · {{duration}}").
  await expect(row).toContainText(/Every Sun at 02:00 · 1h 30m/);
  // Edit -> preview from /occurrences.
  await row.locator('li').filter({ hasText: name }).getByRole('button', { name: 'Edit' }).click();
  await expect(page.getByRole('dialog').getByText(/Next occurrences/)).toBeVisible();
  await shot2b(page, 'maintenance-weekly-preview-en');
  await page.getByRole('dialog').getByRole('button', { name: /Cancel|Close/ }).first().click();

  // Validation.
  const bad = [
    { name: 'x', scope: 'device', device_id: dev.device.id, kind: 'weekly', weekdays: [], start_time: '02:00', duration_min: 90 },
    { name: 'x', scope: 'device', device_id: dev.device.id, kind: 'weekly', weekdays: [8], start_time: '02:00', duration_min: 90 },
    { name: 'x', scope: 'device', device_id: dev.device.id, kind: 'weekly', weekdays: [1], start_time: '24:00', duration_min: 90 },
    { name: 'x', scope: 'device', device_id: dev.device.id, kind: 'weekly', weekdays: [1], start_time: '02:00', duration_min: 10081 },
    { name: 'x', scope: 'device', device_id: null, kind: 'weekly', weekdays: [1], start_time: '02:00', duration_min: 9 },
    { name: 'x', scope: 'global', device_id: dev.device.id, kind: 'weekly', weekdays: [1], start_time: '02:00', duration_min: 9 },
    { name: 'x', scope: 'device', device_id: dev.device.id, kind: 'once', starts_at: '2026-11-10T22:00:00Z', ends_at: '2026-11-10T21:00:00Z' },
    { name: 'x', scope: 'device', device_id: dev.device.id, kind: 'once', starts_at: '2026-11-10T22:00:00Z', ends_at: '2028-11-10T21:00:00Z' },
  ];
  for (const b of bad) {
    const r = await raw(api, 'POST', '/api/maintenance-windows', b);
    expect.soft(r.status, JSON.stringify(b)).toBe(422);
  }
  // Time zone: occurrences follow settings.timezone.
  const tz = await raw(api, 'PUT', '/api/settings', { timezone: 'Asia/Jerusalem' });
  expect(tz.status, tz.text).toBe(200);
  expect((await api.get('/api/settings/public')).timezone).toBe('Asia/Jerusalem');
  const w2 = await api.get(`/api/maintenance-windows/${w.id}`);
  expect(w2.timezone).toBe('Asia/Jerusalem');
  const occ2 = (await api.get(`/api/maintenance-windows/${w.id}/occurrences?days=14`)).items;
  for (const o of occ2) {
    // 02:00 in Jerusalem = 00:00Z (IST, UTC+2) in November.
    expect(new Date(o.start).toISOString()).toMatch(/T00:00:00/);
  }
  expect((await raw(api, 'PUT', '/api/settings', { timezone: 'Mars/Olympus' })).status).toBe(422);
  await api.put('/api/settings', { timezone: 'UTC' });
  await api.del(`/api/maintenance-windows/${w.id}`);
});

test('settings page has a time-zone select', async ({ page }) => {
  await loginViaApi(page, undefined, '/settings');
  const sel = page.getByLabel(/Time zone/);
  await expect(sel).toBeVisible();
  await expect(sel).toHaveValue('UTC');
});
