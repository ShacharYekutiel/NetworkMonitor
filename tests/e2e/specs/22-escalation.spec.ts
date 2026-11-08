import { expect, test } from '@playwright/test';
import { Api, apiAs, clearWebhookRecords, defaultGroupId, loginViaApi, uid } from '../lib/helpers';
import { raw } from '../lib/lab';
import {
  canRestartBackend,
  createWebhook,
  hooks,
  httpDevice,
  quiet,
  resetClock,
  restartBackend,
  rule,
  setClock,
  shot2b,
  target,
  waitStatus,
} from '../lib/lab2b';

// [2b-1] (d) Escalation + persistent alert state (data-model.md § Alert state): rule with an escalation
// channel -> after escalation_after_s (test clock) the escalation webhook fires once; reminders at the
// interval to the primary channel (one per interval, no burst); recovery goes to both channels. The backend
// is restarted between the problem and the escalation: the state lives in alert_state and survives.
test.describe.configure({ mode: 'serial' });

let api: Api;
let dev: { device: any; sensor: any };
let ruleId = 0;
let primary = 0;
let esc = 0;
const PRIMARY = `/esc-primary-${Date.now().toString(36)}`;
const ESC = `/esc-esc-${Date.now().toString(36)}`;
const IP = '127.0.0.36';

test.beforeAll(async () => {
  api = await apiAs();
  await resetClock(api);
  await target(IP, 'ok');
  dev = await httpDevice(api, { name: uid('esc-dev'), ip: IP, group_id: await defaultGroupId(api) });
  primary = await createWebhook(api, PRIMARY);
  esc = await createWebhook(api, ESC);
  await waitStatus(api, dev.sensor.id, 'up', 45_000);
});

test.afterAll(async () => {
  await resetClock(api).catch(() => undefined);
  await target(IP, 'ok');
  if (ruleId) await raw(api, 'DELETE', `/api/alert-rules/${ruleId}`);
});

test('alert rule form: Escalation section (minutes in the UI, seconds in the API)', async ({ page }) => {
  await loginViaApi(page, undefined, '/notifications');
  await page.getByRole('button', { name: 'Add rule' }).click();
  const dlg = page.getByRole('dialog');
  const name = uid('Esc rule');
  await dlg.getByLabel(/^Name/).fill(name);
  await dlg.getByLabel('Applies to').selectOption({ label: `${dev.device.name} / ${dev.sensor.name}` });
  await dlg.getByRole('checkbox', { name: new RegExp((await api.get(`/api/notification-channels/${primary}`)).name) }).first().check();
  await dlg.getByRole('button', { name: /Escalation and reminders/ }).click();
  await dlg.getByRole('checkbox', { name: /Escalate if not acknowledged/ }).check();
  await dlg.getByRole('spinbutton', { name: /^Escalate after/ }).fill('1');
  const escName = (await api.get(`/api/notification-channels/${esc}`)).name;
  await dlg.getByRole('group', { name: /Escalation channels/ }).getByRole('checkbox', { name: new RegExp(escName) }).check();
  await dlg.getByRole('checkbox', { name: /Remind until acknowledged/ }).check();
  await dlg.getByRole('spinbutton', { name: /^Remind every/ }).fill('5');
  await shot2b(page, 'escalation-rule-form-en');
  await dlg.getByRole('button', { name: 'Save' }).click();
  await expect(dlg).toBeHidden();
  const r = (await api.get('/api/alert-rules')).items.find((x: any) => x.name === name);
  expect(r).toMatchObject({ escalation_after_s: 60, escalation_channel_ids: [esc], repeat_every_s: 300, channel_ids: [primary], delay_s: 0 });
  ruleId = r.id;
  // List shows the escalation icon.
  await expect(page.getByRole('row').filter({ hasText: name }).getByLabel('Escalation and reminders')).toBeVisible();
});

test('validation of the escalation fields (422)', async () => {
  const base = { name: uid('bad'), sensor_id: dev.sensor.id, on_statuses: ['down', 'up'], delay_s: 0, enabled: true, channel_ids: [primary] };
  const bad = [
    { escalation_after_s: 60, escalation_channel_ids: [] },
    { escalation_after_s: null, escalation_channel_ids: [esc] },
    { escalation_after_s: 59, escalation_channel_ids: [esc] },
    { escalation_after_s: 604801, escalation_channel_ids: [esc] },
    { escalation_after_s: 60, escalation_channel_ids: [999999] },
    { repeat_every_s: 299 },
    { delay_s: 120, escalation_after_s: 60, escalation_channel_ids: [esc] },
    { on_statuses: ['up'], repeat_every_s: 300 },
  ];
  for (const b of bad) {
    const r = await raw(api, 'POST', '/api/alert-rules', { ...base, ...b });
    expect.soft(r.status, JSON.stringify(b)).toBe(422);
  }
  // PATCH escalation_after_s: null also clears the channel list.
  const tmp = await api.post('/api/alert-rules', { ...base, enabled: false, escalation_after_s: 60, escalation_channel_ids: [esc] });
  const p = await api.patch(`/api/alert-rules/${tmp.id}`, { escalation_after_s: null });
  expect(p).toMatchObject({ escalation_after_s: null, escalation_channel_ids: [] });
  await api.del(`/api/alert-rules/${tmp.id}`);
});

test('problem -> primary; backend restart; escalation fires once after the delay (state persisted)', async () => {
  test.skip(!ruleId, 'rule from the UI test');
  await clearWebhookRecords();
  await target(IP, 'fail');
  await waitStatus(api, dev.sensor.id, 'down', 45_000);
  await expect.poll(async () => (await hooks(PRIMARY)).length, { timeout: 20_000 }).toBe(1);
  const first = (await hooks(PRIMARY))[0].body;
  expect(first).toMatchObject({ event: 'sensor.status_changed', stage: 'problem', new_status: 'down', after_maintenance: false });
  const since = first.problem_since;
  expect(await hooks(ESC)).toEqual([]);

  if (canRestartBackend()) {
    restartBackend();
    api = await apiAs(); // new session cookie is still valid, but re-login is cheap
  } else {
    test.info().annotations.push({ type: 'warning', description: 'BACKEND_RESTART not set: restart part skipped' });
  }
  // The test clock is in-memory: after a restart it is 0 again.
  await setClock(api, 75);
  await expect.poll(async () => (await hooks(ESC)).length, { timeout: 20_000 }).toBe(1);
  const e = (await hooks(ESC))[0].body;
  expect(e).toMatchObject({ event: 'sensor.escalation', stage: 'escalation', new_status: 'down', problem_since: since });
  expect(e.sensor.id).toBe(dev.sensor.id);
  // Exactly once, also after another restart.
  if (canRestartBackend()) {
    restartBackend();
    api = await apiAs();
    await setClock(api, 90);
  }
  await quiet(6_000);
  expect((await hooks(ESC)).length).toBe(1);
  expect((await hooks(PRIMARY)).length).toBe(1);
});

test('reminders at the interval to the primary channel, one per interval (no burst)', async () => {
  test.skip(!ruleId, 'rule from the UI test');
  await setClock(api, 320);
  await expect.poll(async () => (await hooks(PRIMARY)).length, { timeout: 20_000 }).toBe(2);
  expect((await hooks(PRIMARY))[1].body).toMatchObject({ event: 'sensor.reminder', stage: 'reminder' });
  await quiet(4_000);
  expect((await hooks(PRIMARY)).length).toBe(2);
  // Jump far ahead: only one overdue reminder.
  await setClock(api, 5000);
  await expect.poll(async () => (await hooks(PRIMARY)).length, { timeout: 20_000 }).toBe(3);
  await quiet(5_000);
  expect((await hooks(PRIMARY)).length).toBe(3);
  expect((await hooks(ESC)).length).toBe(1);
});

test('recovery goes to both the primary and the escalation channel', async ({ page }) => {
  test.skip(!ruleId, 'rule from the UI test');
  await target(IP, 'ok');
  await waitStatus(api, dev.sensor.id, 'up', 45_000);
  await expect.poll(async () => (await hooks(PRIMARY)).map((r) => r.body.new_status), { timeout: 20_000 }).toEqual(['down', 'down', 'down', 'up']);
  await expect.poll(async () => (await hooks(ESC)).map((r) => r.body.new_status), { timeout: 20_000 }).toEqual(['down', 'up']);
  const rec = (await hooks(ESC))[1].body;
  expect(rec).toMatchObject({ event: 'sensor.status_changed', stage: 'recovery' });
  await resetClock(api);
  await loginViaApi(page, undefined, `/events?sensor_id=${dev.sensor.id}`);
  await shot2b(page, 'escalation-events-en');
});
