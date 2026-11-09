import { expect, test } from '@playwright/test';
import { Api, apiAs, clearWebhookRecords, loginViaApi } from '../lib/helpers';
import { raw } from '../lib/lab';
import { createWebhook, hooks, quiet, resetClock, setClock } from '../lib/lab2b';
import { ensurePortLab, type PortLab } from '../lib/lab2b2';
import {
  OID, SRC, builtinRule, conditions, featureOn, linkVarbinds, marker, sendSyslog, sendTrap, sensorsOf, shot2b3, tOr,
  waitEventSensor, waitItems, waitReceivers,
} from '../lib/lab2b3';

// [2b-3] 41 — event rules + passive event sensors through the existing alert engine (architecture.md §12.3,
// data-model § Event rules / Built-in rules, api.md [2b-3] Event rules / Event sensors).
// + owner decision (coordinator, not yet in docs): setting `event_linkdown_scope` "important" (default) | "all":
//   important -> linkDown on a plain access port raises nothing, on a port with an interface sensor / uplink /
//   LAG it does; all -> the access port alerts too; invalid value -> 422.
// port-aos (127.0.0.81): ifIndex 1 has an snmp_interface sensor (important), 10 = uplink (manual link to
// port-cw), 7 = plain access port (1 MAC, no sensor, not LAG/uplink).
test.describe.configure({ mode: 'serial' });

const HOOK = '/qa41';
let admin: Api;
let lab: PortLab;
let trapSensor: any;
let syslogSensor: any;
let hookId: number;

const linkDown = (ifIndex: number, from: string = SRC.aos) => sendTrap({ from, oid: OID.linkDown, varbinds: linkVarbinds(ifIndex, false) });
const linkUp = (ifIndex: number, from: string = SRC.aos) => sendTrap({ from, oid: OID.linkUp, varbinds: linkVarbinds(ifIndex, true) });

async function waitConds(sensorId: number, pred: (c: any[]) => boolean, timeout = 15_000, message = 'conditions'): Promise<any[]> {
  let c: any[] = [];
  await expect.poll(async () => { c = await conditions(admin, sensorId); return pred(c); }, { timeout, intervals: [300, 600, 1000], message }).toBe(true);
  return c;
}
async function waitSensorStatus(id: number, status: string, timeout = 15_000): Promise<any> {
  let s: any;
  await expect.poll(async () => { s = await admin.get(`/api/sensors/${id}`); return s.status; }, { timeout, message: `sensor ${id} -> ${status}` }).toBe(status);
  return s;
}
async function clearAll(): Promise<void> {
  for (const s of [trapSensor, syslogSensor]) if (s) await raw(admin, 'POST', `/api/sensors/${s.id}/clear`);
}

test.beforeAll(async () => {
  admin = await apiAs();
  lab = await ensurePortLab(admin);
  await featureOn(admin, 'traps', 'syslog', 'event_alerts');
  await waitReceivers(admin, ['trap', 'syslog_udp'], 'listening');
});

test.afterAll(async () => {
  await resetClock(admin);
  await raw(admin, 'PUT', '/api/settings', { event_linkdown_scope: 'important' });
});

test('built-in rules are seeded disabled with the contract definitions', async () => {
  const items = (await admin.get('/api/event-rules')).items;
  const b = Object.fromEntries(items.filter((r: any) => r.builtin).map((r: any) => [r.builtin, r]));
  expect(Object.keys(b).sort()).toEqual(['syslog_error', 'trap_auth_failure', 'trap_link_down', 'trap_restart']);
  expect(b.trap_link_down).toMatchObject({
    name: 'Link down', kind: 'trap', priority: 1000, action: 'warning', key_varbind_oid: '1.3.6.1.2.1.2.2.1.1', clear_after_min: 1440,
    match: { trap_oids: [OID.linkDown], varbinds: [{ oid: OID.ifAdminStatus, op: 'ne', value: '2' }], ports: 'important' },
    clear_match: { trap_oids: [OID.linkUp], varbinds: [], ports: 'any' },
  });
  expect(b.trap_auth_failure).toMatchObject({ priority: 1010, clear_after_min: 60, action: 'warning' });
  expect(b.trap_restart).toMatchObject({ priority: 1020, clear_after_min: 30, match: { trap_oids: [OID.coldStart, OID.warmStart] } });
  expect(b.syslog_error).toMatchObject({ kind: 'syslog', priority: 1030, clear_after_min: 30, match: { severity_max: 3 } });
  // the seeded state is disabled (a prior run of this spec may have enabled them: only check on a fresh DB)
  if (items.every((r: any) => !r.last_matched_at)) for (const r of Object.values(b) as any[]) expect.soft(r.enabled, r.builtin).toBe(false);
  expect((await raw(admin, 'DELETE', `/api/event-rules/${b.syslog_error.id}`)).body?.error?.code).toBe('builtin_rule');
});

test('first attributed messages create the passive trap/syslog event sensors (up)', async () => {
  for (const k of ['trap_link_down', 'trap_auth_failure', 'trap_restart', 'syslog_error']) {
    const r = await builtinRule(admin, k);
    await admin.patch(`/api/event-rules/${r.id}`, { enabled: true });
  }
  sendTrap({ from: SRC.aos, oid: OID.linkUp, varbinds: linkVarbinds(2, true) });
  sendSyslog({ from: SRC.aos, messages: ['<14>Nov  8 12:00:00 sw qa41: hello'] });
  trapSensor = await waitEventSensor(admin, lab.aos.id, 'trap');
  syslogSensor = await waitEventSensor(admin, lab.aos.id, 'syslog');
  await clearAll();
  trapSensor = await waitSensorStatus(trapSensor.id, 'up');
  expect(trapSensor.name).toBe('SNMP traps');
  expect(syslogSensor.name).toBe('Syslog');
  expect(trapSensor.channels.map((c: any) => c.key)).toEqual(['active']);
  // passive: no run/test
  expect((await raw(admin, 'POST', `/api/sensors/${trapSensor.id}/run`)).body?.error?.code).toBe('sensor_passive');
  const types = await admin.get('/api/sensor-types');
  const list = Array.isArray(types) ? types : types.items;
  expect(list.find((t: any) => (t.type ?? t.key ?? t.id) === 'trap')?.passive).toBe(true);
  // per-sensor alert rules -> webhook (the global ones of earlier specs may also fire; we filter by path)
  hookId = await createWebhook(admin, HOOK);
  for (const s of [trapSensor, syslogSensor]) {
    await admin.post('/api/alert-rules', { name: `qa41-${s.type}`, sensor_id: s.id, on_statuses: ['warning', 'down', 'up'], delay_s: 0, enabled: true, channel_ids: [hookId] });
  }
  await clearWebhookRecords();
});

test('linkDown on an important port (interface sensor) -> trap sensor warning -> webhook; linkUp -> recovery', async () => {
  linkDown(1);
  const c = await waitConds(trapSensor.id, (x) => x.length === 1, 15_000, 'Link down condition for ifIndex 1');
  expect(c[0]).toMatchObject({ source: 'rule', key: '1', status: 'warning', last_kind: 'trap', count: 1 });
  const s = await waitSensorStatus(trapSensor.id, 'warning');
  expect(s.status_message).toMatch(/^Link down: linkDown/);
  await expect.poll(async () => (await hooks(HOOK)).map((r) => [r.body.sensor?.id, r.body.new_status]), { timeout: 20_000 }).toEqual([[trapSensor.id, 'warning']]);
  linkUp(1);
  await waitConds(trapSensor.id, (x) => x.length === 0);
  await waitSensorStatus(trapSensor.id, 'up');
  await expect.poll(async () => (await hooks(HOOK)).map((r) => r.body.new_status), { timeout: 20_000 }).toEqual(['warning', 'up']);
});

test('event_linkdown_scope=important: access port 7 -> nothing; uplink 10 -> condition', async () => {
  expect((await admin.get('/api/settings')).event_linkdown_scope).toBe('important');
  await clearWebhookRecords();
  const m = marker('qa41scope');
  sendTrap({ from: SRC.aos, oid: OID.linkDown, varbinds: [...linkVarbinds(7, false), `1.3.6.1.2.1.31.1.1.1.18.7=s:${m}`] });
  const [t] = await waitItems(admin, 'traps', { q: m }, (it) => it.length === 1);
  expect(t.matched_rule_ids).toEqual([]);
  await quiet(2000);
  expect(await conditions(admin, trapSensor.id)).toEqual([]);
  linkDown(10);
  await waitConds(trapSensor.id, (x) => x.length === 1 && x[0].key === '10', 15_000, 'uplink port 10 is important');
  await clearAll();
  await waitSensorStatus(trapSensor.id, 'up');
});

test('event_linkdown_scope=all: the access port alerts too; invalid value -> 422', async () => {
  const bad = await raw(admin, 'PUT', '/api/settings', { event_linkdown_scope: 'some' });
  expect(bad.status).toBe(422);
  expect((await raw(admin, 'PUT', '/api/settings', { event_linkdown_scope: 'all' })).status).toBe(200);
  await clearWebhookRecords();
  linkDown(7);
  await waitConds(trapSensor.id, (x) => x.some((c) => c.key === '7'), 15_000, 'access port 7 with scope all');
  await waitSensorStatus(trapSensor.id, 'warning');
  await expect.poll(async () => (await hooks(HOOK)).map((r) => r.body.new_status), { timeout: 20_000 }).toEqual(['warning']);
  linkUp(7);
  await waitSensorStatus(trapSensor.id, 'up');
  expect((await raw(admin, 'PUT', '/api/settings', { event_linkdown_scope: 'important' })).status).toBe(200);
});

test('linkDown with ifAdminStatus down(2) (shut by an admin) raises nothing', async () => {
  sendTrap({ from: SRC.aos, oid: OID.linkDown, varbinds: linkVarbinds(1, false, false) });
  await quiet(2500);
  expect(await conditions(admin, trapSensor.id)).toEqual([]);
});

test('syslog <187> (err) -> syslog sensor warning; auto-clear after 30 min via the test clock', async () => {
  await clearWebhookRecords();
  sendSyslog({ from: SRC.aos, messages: ['<187>Nov  8 12:00:00 sw qa41: interface error'] });
  const c = await waitConds(syslogSensor.id, (x) => x.length === 1);
  expect(c[0]).toMatchObject({ status: 'warning', last_kind: 'syslog', key: '' });
  expect(c[0].expires_at).not.toBeNull();
  await waitSensorStatus(syslogSensor.id, 'warning');
  await setClock(admin, 31 * 60);
  try {
    await waitSensorStatus(syslogSensor.id, 'up', 45_000);
  } finally {
    await resetClock(admin);
  }
  await expect.poll(async () => (await hooks(HOOK)).map((r) => [r.body.sensor?.id, r.body.new_status]), { timeout: 20_000 })
    .toEqual([[syslogSensor.id, 'warning'], [syslogSensor.id, 'up']]);
});

test('acknowledge works on an event sensor and is cleared by the next status change', async () => {
  linkDown(1);
  await waitSensorStatus(trapSensor.id, 'warning');
  const acked = await admin.post(`/api/sensors/${trapSensor.id}/acknowledge`, { comment: 'qa41 on it' });
  expect(acked.acknowledgement).toBeTruthy();
  linkUp(1);
  const s = await waitSensorStatus(trapSensor.id, 'up');
  expect(s.acknowledgement).toBeNull();
});

test('maintenance suppresses the notification (sensor still turns warning)', async () => {
  const now = Date.now();
  const w = await admin.post('/api/maintenance-windows', {
    name: 'qa41-mw', enabled: true, scope: 'device', device_id: lab.aos.id, kind: 'once',
    starts_at: new Date(now - 60_000).toISOString(), ends_at: new Date(now + 3_600_000).toISOString(), comment: '',
  });
  try {
    await expect.poll(async () => (await admin.get(`/api/sensors/${trapSensor.id}`)).in_maintenance, { timeout: 15_000 }).toBe(true);
    await clearWebhookRecords();
    linkDown(1);
    await waitSensorStatus(trapSensor.id, 'warning');
    await quiet(4000);
    expect(await hooks(HOOK)).toEqual([]);
    await raw(admin, 'POST', `/api/sensors/${trapSensor.id}/clear`);
    await waitSensorStatus(trapSensor.id, 'up');
  } finally {
    await admin.del(`/api/maintenance-windows/${w.id}`);
  }
});

test('custom syslog rule with key regex: two instances, clear_match with the same key', async () => {
  const r = await admin.post('/api/event-rules', {
    name: 'QA41 port down', kind: 'syslog', priority: 5, scope: 'device', device_id: lab.aos.id, action: 'down',
    match: { severity_max: null, facilities: [], programs: ['qa41port'], text: 'QA41 port \\d+ down', text_regex: true },
    key_regex: 'port (\\d+)', clear_match: { severity_max: null, facilities: [], programs: ['qa41port'], text: 'QA41 port \\d+ up', text_regex: true },
  });
  expect(r).toMatchObject({ scope: 'device', scope_name: lab.aos.name, builtin: null, active_conditions: 0 });
  sendSyslog({ from: SRC.aos, messages: ['<14>Nov  8 12:00:00 sw qa41port: QA41 port 5 down', '<14>Nov  8 12:00:00 sw qa41port: QA41 port 7 down'] });
  const c = await waitConds(syslogSensor.id, (x) => x.length === 2);
  expect(c.map((x) => x.key).sort()).toEqual(['5', '7']);
  expect(c.every((x) => x.status === 'down' && x.rule_id === r.id)).toBe(true);
  const s = await waitSensorStatus(syslogSensor.id, 'down');
  expect(s.status_message).toContain('(+1 more)');
  expect((await admin.get(`/api/event-rules/${r.id}`)).active_conditions).toBe(2);
  sendSyslog({ from: SRC.aos, messages: ['<14>Nov  8 12:00:00 sw qa41port: QA41 port 5 up'] });
  const c2 = await waitConds(syslogSensor.id, (x) => x.length === 1);
  expect(c2[0].key).toBe('7');
  // editing a rule removes its conditions
  await admin.patch(`/api/event-rules/${r.id}`, { name: 'QA41 port down (edited)' });
  await waitConds(syslogSensor.id, (x) => x.length === 0);
  await waitSensorStatus(syslogSensor.id, 'up');
  await admin.del(`/api/event-rules/${r.id}`);
});

test('ignore rule stops evaluation (no condition, matched_rule_ids has it)', async () => {
  const ign = await admin.post('/api/event-rules', {
    name: 'QA41 ignore', kind: 'syslog', priority: 1, scope: 'global', action: 'ignore',
    match: { severity_max: null, facilities: [], programs: [], text: 'QA41IGNORE', text_regex: false },
  });
  try {
    const m = marker('qa41ign');
    sendSyslog({ from: SRC.aos, messages: [`<187>Nov  8 12:00:00 sw x: QA41IGNORE ${m}`] });
    const [msg] = await waitItems(admin, 'syslog', { q: m }, (it) => it.length === 1);
    expect(msg.matched_rule_ids).toEqual([ign.id]);
    await quiet(1500);
    expect(await conditions(admin, syslogSensor.id)).toEqual([]);
  } finally {
    await admin.del(`/api/event-rules/${ign.id}`);
  }
});

test('rule validation (422) and Test preview (no side effects)', async () => {
  const base = { name: 'v', kind: 'syslog', scope: 'global', match: { severity_max: 3, facilities: [], programs: [], text: null, text_regex: false } };
  for (const [what, body] of [
    ['warning without clear', { ...base, action: 'warning' }],
    ['bad regex', { ...base, action: 'warning', clear_after_min: 5, match: { ...base.match, text: '(', text_regex: true } }],
    ['ignore with clear', { ...base, action: 'ignore', clear_after_min: 5 }],
    ['key regex without group', { ...base, action: 'warning', clear_after_min: 5, key_regex: 'port \\d+' }],
    ['non-numeric trap oid', { ...base, kind: 'trap', action: 'warning', clear_after_min: 5, match: { trap_oids: ['linkDown'], varbinds: [], ports: 'any' } }],
    ['clear_after_min out of range', { ...base, action: 'warning', clear_after_min: 20000 }],
  ] as const) {
    const r = await raw(admin, 'POST', '/api/event-rules', body);
    expect.soft(r.status, what).toBe(422);
  }
  const t = await admin.post('/api/event-rules/test', { rule: { ...base, action: 'warning', clear_after_min: 30 }, hours: 24 });
  expect(t.matched).toBeGreaterThanOrEqual(1);
  expect(t.items.length).toBeLessThanOrEqual(50);
  expect(t.items[0]).toMatchObject({ kind: 'syslog', result: 'raise' });
  expect(await conditions(admin, syslogSensor.id)).toEqual([]);
});

test('disabling a built-in rule clears its conditions; reset restores it', async () => {
  linkDown(1);
  await waitConds(trapSensor.id, (x) => x.length === 1);
  const ld = await builtinRule(admin, 'trap_link_down');
  await admin.patch(`/api/event-rules/${ld.id}`, { enabled: false, name: 'renamed' });
  await waitConds(trapSensor.id, (x) => x.length === 0);
  await waitSensorStatus(trapSensor.id, 'up');
  const reset = await admin.post(`/api/event-rules/${ld.id}/reset`);
  expect(reset).toMatchObject({ name: 'Link down', enabled: false });
  await admin.patch(`/api/event-rules/${ld.id}`, { enabled: true });
});

test('UI: Rules tab shows built-ins with badge; event sensor page shows active conditions + Clear', async ({ page }) => {
  linkDown(1);
  await waitSensorStatus(trapSensor.id, 'warning');
  await loginViaApi(page, undefined, '/logs');
  await page.getByRole('tab', { name: tOr('logs.tab.rules', 'Rules') }).click();
  await expect(page.getByText('Link down').first()).toBeVisible();
  await expect(page.getByText(tOr('rules.builtin', 'Built-in')).first()).toBeVisible();
  await page.goto(`/sensors/${trapSensor.id}`);
  await expect(page.getByText(tOr('events.activeConditions', 'Active conditions')).first()).toBeVisible();
  await shot2b3(page, '41-event-sensor');
  await page.getByRole('button', { name: tOr('events.clear', 'Clear') }).click();
  const confirm = page.getByRole('dialog').getByRole('button', { name: /Clear|OK|Confirm/ });
  if (await confirm.count()) await confirm.first().click();
  await waitSensorStatus(trapSensor.id, 'up');
});

test('deleting the trap event sensor -> not re-created (optout); creating it manually removes the optout', async () => {
  await admin.del(`/api/sensors/${trapSensor.id}`);
  expect((await admin.get(`/api/devices/${lab.aos.id}`)).event_sensor_optout).toContain('trap');
  linkDown(1);
  await quiet(3000);
  expect((await sensorsOf(admin, lab.aos.id)).filter((s) => s.type === 'trap')).toEqual([]);
  const s = await admin.post('/api/sensors', { device_id: lab.aos.id, type: 'trap', name: 'SNMP traps', config: {} });
  expect((await admin.get(`/api/devices/${lab.aos.id}`)).event_sensor_optout).not.toContain('trap');
  const dup = await raw(admin, 'POST', '/api/sensors', { device_id: lab.aos.id, type: 'trap', name: 'again', config: {} });
  expect(dup.status).toBe(409);
  trapSensor = s;
});

test('turning event_alerts OFF clears rule conditions, sensors up with "Event alerts are turned off"', async () => {
  linkDown(1);
  await waitSensorStatus(trapSensor.id, 'warning');
  await raw(admin, 'PUT', '/api/settings', { feature_event_alerts_enabled: false });
  const s = await waitSensorStatus(trapSensor.id, 'up');
  expect(s.status_message).toBe('Event alerts are turned off');
  expect(await conditions(admin, trapSensor.id)).toEqual([]); // /conditions still works while OFF
  // rule CRUD stays available while OFF
  expect((await raw(admin, 'GET', '/api/event-rules')).status).toBe(200);
  await featureOn(admin, 'event_alerts');
});
