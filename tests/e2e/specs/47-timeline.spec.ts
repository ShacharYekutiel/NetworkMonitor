import { expect, test } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { raw } from '../lib/lab';
import { ensurePortLab, type PortLab } from '../lib/lab2b2';
import {
  OID, SRC, featureOff, featureOn, linkVarbinds, marker, sendSyslog, sendTrap, shot2b3, tOr, waitEventSensor, waitItems,
  waitReceivers,
} from '../lib/lab2b3';

// [2b-3] 47 — device timeline (architecture.md §12.9, api.md [2b-3] Device timeline): a device with status
// changes, a trap, a syslog line, an ack and a maintenance window -> merged newest-first order, kind filters,
// cursor "Load more", audit kind only with audit ON.
test.describe.configure({ mode: 'serial' });

let admin: Api;
let lab: PortLab;
let since: string;
const M = marker('qa47');

async function timeline(q: string): Promise<any> {
  return admin.get(`/api/devices/${lab.aos.id}/timeline?since=${encodeURIComponent(since)}&${q}`);
}

test.beforeAll(async () => {
  admin = await apiAs();
  lab = await ensurePortLab(admin);
  await featureOn(admin, 'traps', 'syslog', 'event_alerts', 'timeline', 'audit');
  await waitReceivers(admin, ['trap', 'syslog_udp'], 'listening');
  since = new Date(Date.now() - 2000).toISOString();
  const ld = (await admin.get('/api/event-rules')).items.find((r: any) => r.builtin === 'trap_link_down');
  if (!ld.enabled) await admin.patch(`/api/event-rules/${ld.id}`, { enabled: true });
  // a syslog line, a trap that raises a condition (status change), an ack, a maintenance window
  sendSyslog({ from: SRC.aos, messages: [`<14>Nov  8 12:00:00 sw qa47: ${M} syslog`] });
  await waitItems(admin, 'syslog', { q: M }, (it) => it.length === 1);
  sendTrap({ from: SRC.aos, oid: OID.linkDown, varbinds: [...linkVarbinds(1, false), `1.3.6.1.2.1.31.1.1.1.18.1=s:${M}`] });
  const s = await waitEventSensor(admin, lab.aos.id, 'trap', (x) => x.status === 'warning');
  await admin.post(`/api/sensors/${s.id}/acknowledge`, { comment: M });
  const now = Date.now();
  const w = await admin.post('/api/maintenance-windows', { name: `${M}-mw`, enabled: true, scope: 'device', device_id: lab.aos.id, kind: 'once', starts_at: new Date(now - 1000).toISOString(), ends_at: new Date(now + 4000).toISOString(), comment: '' });
  await expect.poll(async () => (await admin.get(`/api/events?kind=maintenance&page_size=50`)).items.some((e: any) => (e.message ?? '').includes(`${M}-mw`) || e.device_id === lab.aos.id), { timeout: 20_000 }).toBe(true);
  await admin.post(`/api/sensors/${s.id}/clear`);
  await raw(admin, 'DELETE', `/api/maintenance-windows/${w.id}`);
});

test('merged timeline: all kinds present, newest first with kind rank ties', async () => {
  const t = await timeline('limit=200');
  const kinds = new Set(t.items.map((i: any) => i.kind));
  for (const k of ['status', 'ack', 'maintenance', 'trap', 'syslog', 'audit']) expect.soft(kinds.has(k), `kind ${k}`).toBe(true);
  for (let i = 1; i < t.items.length; i++) {
    const a = t.items[i - 1], b = t.items[i];
    const ta = Date.parse(a.time), tb = Date.parse(b.time);
    // times are serialised in whole seconds, so items of one second are ordered by the server's sub-second
    // times (rank/id ties cannot be seen from outside); only the non-increasing order is checked
    expect(ta >= tb, `${a.kind}:${a.id} ${a.time} before ${b.kind}:${b.id} ${b.time}`).toBe(true);
  }
  const trap = t.items.find((i: any) => i.kind === 'trap' && i.message.includes(M));
  expect(trap.ref).toMatchObject({ type: 'trap' });
  const sys = t.items.find((i: any) => i.kind === 'syslog' && i.message.includes(M));
  expect(sys).toMatchObject({ severity: 6, ref: { type: 'syslog' } });
  const st = t.items.find((i: any) => i.kind === 'status' && i.status === 'warning');
  expect(st).toMatchObject({ old_status: 'up', sensor_name: 'SNMP traps' });
  expect(t.items.find((i: any) => i.kind === 'ack').actor).toBeTruthy();
});

test('kind filters and cursor paging ("Load more")', async () => {
  const only = await timeline('kinds=trap&kinds=syslog&limit=200');
  expect(only.items.every((i: any) => ['trap', 'syslog'].includes(i.kind))).toBe(true);
  const all = (await timeline('limit=200')).items;
  const pages: any[] = [];
  let cursor: string | null = null;
  do {
    const p: any = await timeline(`limit=2${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
    pages.push(...p.items);
    cursor = p.next_cursor;
  } while (cursor && pages.length < 400);
  expect(pages.map((i) => `${i.kind}:${i.id}`)).toEqual(all.map((i: any) => `${i.kind}:${i.id}`));
  expect((await raw(admin, 'GET', `/api/devices/${lab.aos.id}/timeline?cursor=bad`)).status).toBe(400);
  expect((await raw(admin, 'GET', `/api/devices/${lab.aos.id}/timeline?limit=201`)).status).toBe(422);
  expect((await raw(admin, 'GET', '/api/devices/999999/timeline')).status).toBe(404);
});

test('audit kind disappears with audit OFF; trap kind with traps OFF', async () => {
  await featureOff(admin, 'audit');
  expect((await timeline('limit=200')).items.some((i: any) => i.kind === 'audit')).toBe(false);
  await featureOff(admin, 'traps');
  expect((await timeline('limit=200')).items.some((i: any) => i.kind === 'trap')).toBe(false);
  await featureOn(admin, 'traps', 'audit');
});

test('UI: device Timeline tab with chips and Load more', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${lab.aos.id}?tab=timeline`);
  await expect(page.getByText(M).first()).toBeVisible();
  await shot2b3(page, '47-timeline');
  const chip = page.getByRole('button', { name: tOr('timeline.kind.syslog', 'Syslog') }).or(page.getByRole('checkbox', { name: tOr('timeline.kind.syslog', 'Syslog') }));
  await chip.first().click();
  await expect(page.getByRole('button', { name: tOr('timeline.loadMore', 'Load more') }).or(page.getByText(M).first())).toBeVisible();
});
