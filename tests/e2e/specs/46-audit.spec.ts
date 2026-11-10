import { expect, test } from '@playwright/test';
import { ADMIN, Api, apiAs, defaultGroupId, loginViaApi } from '../lib/helpers';
import { raw } from '../lib/lab';
import { ensurePortLab } from '../lib/lab2b2';
import { OID, SRC, featureOff, featureOn, linkVarbinds, sendTrap, shot2b3, tOr, tryLogin, uid, waitEventSensor, waitReceivers } from '../lib/lab2b3';

// [2b-3] 46 — audit log (architecture.md §12.8, data-model § audit_log, api.md [2b-3] Audit log): device
// create/update/delete, settings with a secret redacted, feature toggle, ack, login success/failure; filters;
// CSV export (BOM, columns, filters); viewers 403.
test.describe.configure({ mode: 'serial' });

let admin: Api;
const since = new Date(Date.now() - 5000).toISOString();
const NAME = uid('qa46-dev');

async function audit(q: string): Promise<any> {
  return admin.get(`/api/audit?since=${encodeURIComponent(since)}&page_size=200${q ? `&${q}` : ''}`);
}

test.beforeAll(async () => {
  admin = await apiAs();
  await featureOn(admin, 'audit');
});

test('device create/update/delete are audited in the same transaction with diffs', async () => {
  const gid = await defaultGroupId(admin);
  const d = await admin.post('/api/devices', { group_id: gid, name: NAME, host: '192.0.2.46', auto_sensors: [] });
  await admin.patch(`/api/devices/${d.id}`, { description: 'qa46 changed' });
  await admin.del(`/api/devices/${d.id}`);
  const p = await audit(`entity_type=device&entity_id=${d.id}`);
  expect(p.items.map((e: any) => e.action)).toEqual(['device.delete', 'device.update', 'device.create']);
  const [del, upd, cre] = p.items;
  expect(cre).toMatchObject({ actor_type: 'user', actor_name: ADMIN.username, entity_name: NAME, device_id: d.id });
  expect(cre.ip).toBeTruthy();
  expect(cre.changes.host[1]).toBe('192.0.2.46');
  expect(upd.changes.description).toEqual(['', 'qa46 changed']);
  expect(del.changes.name[0]).toBe(NAME);
  // action prefix filter
  expect((await audit('action=device.')).items.every((e: any) => e.action.startsWith('device.'))).toBe(true);
  expect((await audit('action=device.create')).items.every((e: any) => e.action === 'device.create')).toBe(true);
});

test('settings update: per-key changes, secrets only as "***"; feature toggles audited', async () => {
  await admin.put('/api/settings', { smtp: { password: 'Sup3r-Secret-46' }, syslog_retention_days: 31 });
  await featureOn(admin, 'timeline');
  await featureOff(admin, 'timeline');
  const p = await audit('action=settings.update');
  const text = JSON.stringify(p.items);
  expect(text).not.toContain('Sup3r-Secret-46');
  const e = p.items.find((x: any) => x.changes.syslog_retention_days);
  expect(e.changes.syslog_retention_days).toEqual([30, 31]);
  const smtp = p.items.find((x: any) => JSON.stringify(x.changes).includes('smtp'));
  expect(JSON.stringify(smtp.changes)).toContain('***');
  const f = await audit('action=feature.');
  expect(f.items.map((x: any) => `${x.action}:${x.entity_name}`)).toEqual(expect.arrayContaining(['feature.enable:timeline', 'feature.disable:timeline']));
  await admin.put('/api/settings', { syslog_retention_days: 30 });
});

test('login failure and success, logout are audited with source and reason', async () => {
  expect((await tryLogin(ADMIN.username, 'definitely-wrong')).status).toBe(401);
  const ok = await tryLogin(ADMIN.username, ADMIN.password);
  await raw(ok.api, 'POST', '/api/auth/logout');
  const fail = (await audit('action=auth.login_failed')).items[0];
  expect(fail).toMatchObject({ actor_type: 'anonymous', actor_name: ADMIN.username, source: 'local', reason: 'bad_password' });
  expect((await audit('action=auth.login')).items[0]).toMatchObject({ actor_type: 'user', source: 'local' });
  expect((await audit('action=auth.logout')).items.length).toBeGreaterThanOrEqual(1);
  expect((await audit(`actor=${ADMIN.username.slice(0, 3)}`)).items.length).toBeGreaterThan(0);
});

test('acknowledge is audited with the device', async () => {
  const lab = await ensurePortLab(admin);
  await featureOn(admin, 'traps', 'event_alerts');
  await waitReceivers(admin, ['trap'], 'listening');
  const rules = (await admin.get('/api/event-rules')).items;
  const ld = rules.find((r: any) => r.builtin === 'trap_link_down');
  if (!ld.enabled) await admin.patch(`/api/event-rules/${ld.id}`, { enabled: true });
  sendTrap({ from: SRC.aos, oid: OID.linkDown, varbinds: linkVarbinds(1, false) });
  const s = await waitEventSensor(admin, lab.aos.id, 'trap', (x) => x.status === 'warning');
  await admin.post(`/api/sensors/${s.id}/acknowledge`, { comment: 'qa46' });
  await admin.post(`/api/sensors/${s.id}/clear`);
  const a = await audit(`device_id=${lab.aos.id}`);
  expect(a.items.map((x: any) => x.action)).toEqual(expect.arrayContaining(['sensor.acknowledge', 'sensor.clear_events']));
});

test('CSV export: BOM, header, filtered rows, attachment name', async () => {
  const r = await admin.ctx.fetch(`/api/audit/export.csv?since=${encodeURIComponent(since)}&action=device.`);
  expect(r.status()).toBe(200);
  expect(r.headers()['content-type']).toMatch(/text\/csv/);
  expect(r.headers()['content-disposition']).toMatch(/attachment; filename="netmon-audit-.*\.csv"/);
  const buf = await r.body();
  expect([...buf.subarray(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
  const lines = buf.subarray(3).toString('utf8').trim().split(/\r?\n/);
  expect(lines[0]).toBe('time,actor_type,actor_name,ip,action,entity_type,entity_id,entity_name,source,reason,changes');
  expect(lines.slice(1).every((l: string) => /,device\.(create|update|delete),/.test(l))).toBe(true);
  expect(lines.some((l: string) => l.includes(NAME))).toBe(true);
});

test('viewer: 403; UI: Audit page filters, row expand, Export CSV download', async ({ page }) => {
  const v = await apiAs({ username: 'viewer1', password: 'Viewer-pass-1' }).catch(() => null);
  if (v) expect((await raw(v, 'GET', '/api/audit')).status).toBe(403);
  await loginViaApi(page, undefined, '/audit');
  const row = page.getByRole('row').filter({ hasText: NAME }).first();
  await expect(row).toBeVisible();
  await row.click();
  await expect(page.getByText('qa46 changed').first()).toBeVisible();
  await shot2b3(page, '46-audit');
  const [dl] = await Promise.all([page.waitForEvent('download'), page.getByTestId('audit-export').click()]);
  expect(dl.suggestedFilename()).toMatch(/^netmon-audit-.*\.csv$/);
});
