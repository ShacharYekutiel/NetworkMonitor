import { expect, test } from '@playwright/test';
import { Api, apiAs, card, clearWebhookRecords, defaultGroupId, loginViaApi, uid } from '../lib/helpers';
import { collectWsFrames, raw } from '../lib/lab';
import { createWebhook, hooks, httpDevice, quiet, resetClock, rule, setClock, shot2b, target, waitStatus } from '../lib/lab2b';

// [2b-1] (c) Acknowledge (data-model.md § Acknowledge, § Alert state): problem -> ack with comment (UI)
// -> acknowledged section on the dashboard; escalation and reminders stop (test clock); unack -> reminders
// resume; recovery clears the ack (auto-clear) and notifies every notified channel.
test.describe.configure({ mode: 'serial' });

let api: Api;
let dev: { device: any; sensor: any };
let ruleId = 0;
const PRIMARY = `/ack-primary-${Date.now().toString(36)}`;
const ESC = `/ack-esc-${Date.now().toString(36)}`;
const IP = '127.0.0.35';
const COMMENT = 'Replacing PSU, ETA 14:00';

test.beforeAll(async () => {
  api = await apiAs();
  await resetClock(api);
  await target(IP, 'ok');
  dev = await httpDevice(api, { name: uid('ack-dev'), ip: IP, group_id: await defaultGroupId(api) });
  const a = await createWebhook(api, PRIMARY);
  const b = await createWebhook(api, ESC);
  ruleId = (await rule(api, { sensor_id: dev.sensor.id, channel_ids: [a], escalation_after_s: 60, escalation_channel_ids: [b], repeat_every_s: 300 })).id;
  await waitStatus(api, dev.sensor.id, 'up', 45_000);
});

test.afterAll(async () => {
  await resetClock(api);
  await target(IP, 'ok');
  if (ruleId) await raw(api, 'DELETE', `/api/alert-rules/${ruleId}`);
});

test('ack on an up sensor -> 409 not_acknowledgeable', async () => {
  const r = await raw(api, 'POST', `/api/sensors/${dev.sensor.id}/acknowledge`, { comment: 'x' });
  expect(r.status).toBe(409);
  expect(r.body.error.code).toBe('not_acknowledgeable');
  const del = await raw(api, 'DELETE', `/api/sensors/${dev.sensor.id}/acknowledge`);
  expect(del.status).toBe(200);
  expect(del.body.acknowledgement).toBeNull();
});

test('problem -> acknowledge with a comment in the UI -> Acknowledged card, sensor.ack live', async ({ page }) => {
  await clearWebhookRecords();
  await target(IP, 'fail');
  await waitStatus(api, dev.sensor.id, 'down', 45_000);
  await expect.poll(async () => (await hooks(PRIMARY)).length, { timeout: 20_000 }).toBe(1);
  expect((await hooks(PRIMARY))[0].body).toMatchObject({ stage: 'problem', new_status: 'down', after_maintenance: false });

  const ws = collectWsFrames(page);
  await loginViaApi(page, undefined, `/sensors/${dev.sensor.id}`);
  await page.getByRole('button', { name: 'Acknowledge' }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel(/Comment/).fill(COMMENT);
  await dlg.getByRole('button', { name: 'Acknowledge' }).click();
  await expect(dlg).toBeHidden();
  await expect(page.getByRole('button', { name: 'Unacknowledge' })).toBeVisible();
  await expect.poll(() => ws.types.includes('sensor.ack'), { timeout: 10_000 }).toBe(true);
  await shot2b(page, 'ack-sensor-page-en');

  const s = await api.get(`/api/sensors/${dev.sensor.id}`);
  expect(s.status).toBe('down'); // never hides state
  expect(s.acknowledgement).toMatchObject({ by: 'admin', comment: COMMENT, status: 'down' });
  const ev = (await api.get(`/api/events?kind=acknowledge&sensor_id=${dev.sensor.id}`)).items;
  expect(ev[0]).toMatchObject({ message: `Acknowledged by admin: ${COMMENT}`, old_status: 'down', new_status: 'down' });
  expect((await api.get('/api/sensors?acknowledged=true&page_size=1000')).items.map((x: any) => x.id)).toContain(dev.sensor.id);

  const dash = await api.get('/api/dashboard');
  expect(dash.problem_sensors.map((x: any) => x.id)).not.toContain(dev.sensor.id);
  expect(dash.acknowledged_sensors.map((x: any) => x.id)).toContain(dev.sensor.id);
  expect(dash.acknowledged_count).toBeGreaterThanOrEqual(1);
  await page.goto('/');
  await expect(card(page, /^Acknowledged problems/)).toContainText(dev.sensor.name);
  await expect(card(page, 'Down and warning sensors')).not.toContainText(dev.device.name);
  await shot2b(page, 'ack-dashboard-en');
  // "Acknowledged by admin at <time>: comment" (inline details on the dashboard).
  await expect(card(page, /^Acknowledged problems/)).toContainText(new RegExp(`Acknowledged by admin at .+: ${COMMENT}`));
  // Sensor page: check badge with the tooltip "Acknowledged by admin at …: comment".
  await page.goto(`/sensors/${dev.sensor.id}`);
  await expect(page.locator(`main [title*="${COMMENT}"]`).first()).toBeAttached();
});

test('acknowledged: escalation and reminders do not fire (test clock)', async () => {
  await setClock(api, 90); // past escalation_after_s = 60
  await quiet(6_000);
  expect(await hooks(ESC)).toEqual([]);
  await setClock(api, 400); // past repeat_every_s = 300
  await quiet(6_000);
  expect(await hooks(ESC)).toEqual([]);
  expect((await hooks(PRIMARY)).length).toBe(1);
});

test('unacknowledge -> reminders resume, escalation does not restart', async ({ page }) => {
  await loginViaApi(page, undefined, `/sensors/${dev.sensor.id}`);
  await page.getByRole('button', { name: 'Unacknowledge' }).click();
  await expect(page.getByRole('button', { name: 'Acknowledge' })).toBeVisible();
  const ev = (await api.get(`/api/events?kind=acknowledge&sensor_id=${dev.sensor.id}`)).items;
  expect(ev[0].message).toBe('Acknowledgement removed by admin');
  // next_reminder_at = now(+400) + 300 -> due at +700.
  await setClock(api, 710);
  await expect.poll(async () => (await hooks(PRIMARY)).length, { timeout: 15_000 }).toBe(2);
  expect((await hooks(PRIMARY))[1].body).toMatchObject({ event: 'sensor.reminder', stage: 'reminder' });
  await quiet(4_000);
  expect(await hooks(ESC)).toEqual([]);
});

test('re-ack, then recovery: ack auto-cleared, recovery sent to the notified channel', async () => {
  await api.post(`/api/sensors/${dev.sensor.id}/acknowledge`, { comment: 'second' });
  expect((await api.get(`/api/sensors/${dev.sensor.id}`)).acknowledgement.comment).toBe('second');
  await target(IP, 'ok');
  const s = await waitStatus(api, dev.sensor.id, 'up', 45_000);
  expect(s.acknowledgement).toBeNull();
  await expect.poll(async () => (await hooks(PRIMARY)).map((r) => r.body.new_status), { timeout: 20_000 }).toEqual(['down', 'down', 'up']);
  expect(await hooks(ESC)).toEqual([]); // never notified -> no recovery there
  // Auto-clear writes no acknowledge event.
  const ev = (await api.get(`/api/events?kind=acknowledge&sensor_id=${dev.sensor.id}`)).items;
  expect(ev[0].message).toBe('Acknowledged by admin: second');
});

test('ack without comment: event text, auto-clear on recovery', async () => {
  await target(IP, 'fail');
  await waitStatus(api, dev.sensor.id, 'down', 45_000);
  await api.post(`/api/sensors/${dev.sensor.id}/acknowledge`, {});
  expect((await api.get(`/api/sensors/${dev.sensor.id}`)).acknowledgement).toMatchObject({ comment: '', status: 'down' });
  const ev = (await api.get(`/api/events?kind=acknowledge&sensor_id=${dev.sensor.id}`)).items;
  expect(ev[0].message).toBe('Acknowledged by admin');
  await target(IP, 'ok');
  await waitStatus(api, dev.sensor.id, 'up', 45_000);
  expect((await api.get(`/api/sensors/${dev.sensor.id}`)).acknowledgement).toBeNull();
});
