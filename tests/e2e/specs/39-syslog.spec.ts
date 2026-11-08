import { expect, test } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { raw } from '../lib/lab';
import { ensurePortLab, type PortLab } from '../lib/lab2b2';
import {
  SRC, featureOn, marker, sendSyslog, shot2b3, sleep, syslogPage, tOr, waitItems, waitReceivers,
} from '../lib/lab2b3';

// [2b-3] 39 — syslog (architecture.md §12.2, data-model § Syslog parsing, api.md [2b-3] Syslog): the data-model
// vectors over UDP and TCP from 127.0.0.81 (port-aos of the ports lab) -> stored fields, global and device
// views, every filter, stats/histogram, detail drawer, live tail without reload, next_cursor paging.
test.describe.configure({ mode: 'default' });

let admin: Api;
let lab: PortLab;
let devId: number;
const RUN = marker('qa39');

// expected parse results (receive-time independent fields only)
const EXPECT: Record<string, Record<string, unknown>> = {
  S1: { format: 'rfc5424', facility: 20, severity: 5, hostname: 'sw1.mnidom.net', program: 'evntslog', pid: '1234', msgid: 'ID47', structured_data: '[exampleSDID@32473 iut="3"]', message: 'An application event' },
  S2: { format: 'rfc5424', facility: 4, severity: 2, hostname: '', program: '', pid: '', msgid: '', structured_data: null, message: 'plain', device_time: null },
  S3: { format: 'rfc3164', facility: 4, severity: 2, hostname: 'mymachine', program: 'su', message: "'su root' failed for lonvick on /dev/pts/8" },
  S4: { format: 'rfc3164', facility: 3, severity: 6, hostname: 'gw-hq', program: 'sshd', pid: '2211', message: 'Accepted password for admin' },
  S5: { format: 'rfc3164', facility: 23, severity: 4, hostname: 'FF5945-IRF', program: 'IFNET', msgid: 'PHY_UPDOWN', message: 'Physical state on the interface Ten-GigabitEthernet1/0/49 changed to down.' },
  S6: { format: 'rfc3164', facility: 23, severity: 6, hostname: '10.0.0.2', msgid: '00076', program: 'ports', message: 'port 5 is now on-line' },
  S7: { format: 'rfc3164', facility: 1, severity: 5, hostname: '', program: 'su', message: 'test' },
  S8: { format: 'rfc3164', facility: 1, severity: 5, hostname: '', program: '', message: 'hello world', device_time: null },
  S9: { format: 'raw', facility: 1, severity: 5, message: 'just text' },
  S10: { format: 'raw', facility: 1, severity: 5, message: '<999>x' },
};

function find(items: any[], id: string): any {
  const want = EXPECT[id]?.message as string | undefined;
  return items.find((m) => (want ? m.message === want : m.message.startsWith('a�bxxx')));
}

test.beforeAll(async () => {
  admin = await apiAs();
  lab = await ensurePortLab(admin);
  devId = lab.aos.id;
  await featureOn(admin, 'syslog');
  await waitReceivers(admin, ['syslog_udp', 'syslog_tcp'], 'listening');
});

test('vectors S1-S11 over UDP from 127.0.0.81 -> parsed fields, attributed to port-aos', async () => {
  const since = new Date(Date.now() - 1000).toISOString();
  const r = sendSyslog({ from: SRC.aos, ids: ['S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8', 'S9', 'S10', 'S11'] });
  expect(r.sent).toBe(11);
  const items = await waitItems(admin, 'syslog', { device_id: devId, since }, (it) => it.length >= 11, 20_000, '11 vectors stored');
  for (const id of Object.keys(EXPECT)) {
    const m = find(items, id);
    expect.soft(m, `vector ${id} stored`).toBeTruthy();
    if (!m) continue;
    expect.soft(m, `vector ${id}`).toMatchObject({ ...EXPECT[id], transport: 'udp', source_ip: SRC.aos, device_id: devId, device_name: lab.aos.name, truncated: false });
  }
  const s1 = find(items, 'S1');
  // contract vector: 2026-11-08T10:00:00.003Z; the API serialises timestamps in whole seconds (services/serialize.iso)
  if (s1) expect.soft(Math.abs(Date.parse(s1.device_time) - Date.parse('2026-11-08T10:00:00.003Z')), `S1 device_time ${s1.device_time}`).toBeLessThan(1000);
  const s11 = find(items, 'S11');
  expect.soft(s11, 'S11 stored').toBeTruthy();
  if (s11) {
    expect.soft(s11.truncated).toBe(true);
    expect.soft(Buffer.byteLength(s11.message, 'utf8')).toBeLessThanOrEqual(8192);
    expect.soft(s11.message.includes('\u0000')).toBe(false);
    expect.soft(s11).toMatchObject({ facility: 1, severity: 6, program: 'app', hostname: 'host' });
  }
  // S3: a 3164 date has no year -> current year, minus one when that lands > 1 day in the future (rule of
  // data-model § Syslog parsing; NOTE the contract's S3 example "2025-10-11" contradicts that rule for a Nov 8
  // receive time - QA defect report). The rule is asserted, in settings.timezone (Asia/Jerusalem in Oct: +3).
  const s3 = find(items, 'S3');
  if (s3) {
    const y = new Date().getUTCFullYear();
    const cand = Date.UTC(y, 9, 11, 19, 14, 15);
    const tz = (await admin.get('/api/settings')).timezone;
    if (tz === 'Asia/Jerusalem') expect.soft(Date.parse(s3.device_time), `S3 device_time ${s3.device_time}`).toBe(cand > Date.now() + 86400_000 ? Date.UTC(y - 1, 9, 11, 19, 14, 15) : cand);
    else expect.soft(s3.device_time, 'S3 device_time set').not.toBeNull();
  }
  // time is the receive time, never the device time
  for (const m of items) expect.soft(Math.abs(Date.parse(m.time) - Date.now())).toBeLessThan(120_000);
});

test('TCP framing: octet-counted + LF + CRLF frames in one connection; oversized frame closes the connection', async () => {
  const m = marker('qa39tcp');
  const hex = Buffer.from(`${`<13>1 - h a - - - ${m} octet one`.length} <13>1 - h a - - - ${m} octet one<13>${m} lf one\n<13>${m} lf two\r\n`).toString('hex');
  const r = sendSyslog({ from: SRC.aos, tcp: true, rawHex: hex });
  expect(r.closed_by_peer).toBe(false);
  const items = await waitItems(admin, 'syslog', { q: m }, (it) => it.length === 3, 15_000, '3 TCP frames');
  expect(items.map((x) => x.message).sort()).toEqual([`${m} lf one`, `${m} lf two`, `${m} octet one`].sort());
  expect(items.every((x) => x.transport === 'tcp')).toBe(true);
  const before = (await admin.get('/api/ingest/status')).counters.syslog.dropped.too_large;
  const big = sendSyslog({ from: SRC.aos, tcp: true, rawHex: Buffer.from('99999999 x').toString('hex') });
  expect(big.closed_by_peer, 'server closes a connection announcing a frame > 65536 B').toBe(true);
  await expect.poll(async () => (await admin.get('/api/ingest/status')).counters.syslog.dropped.too_large, { timeout: 10_000 }).toBeGreaterThan(before);
});

test('filters: device, group, unknown, source_ip, severity_max, facility, program, hostname, q, since/until', async () => {
  const m = marker('qa39f');
  sendSyslog({ from: SRC.aos, messages: [
    `<187>Nov  8 12:00:00 sw-a progA: ${m} err`, // local7 (23) err (3)
    `<190>Nov  8 12:00:00 sw-a progB: ${m} info`, // local7 info (6)
    `<34>Nov  8 12:00:00 sw-a progA: ${m} crit`, // auth (4) crit (2)
  ] });
  sendSyslog({ from: SRC.unknown, messages: [`<187>Nov  8 12:00:00 stray progA: ${m} unknown`] });
  await waitItems(admin, 'syslog', { q: m }, (it) => it.length === 4);
  const n = async (q: Record<string, any>) => (await syslogPage(admin, { q: m, limit: 100, ...q })).items.length;
  expect.soft(await n({ device_id: devId })).toBe(3);
  expect.soft(await n({ group_id: lab.groupId })).toBe(3);
  expect.soft(await n({ unknown: true })).toBe(1);
  expect.soft(await n({ source_ip: SRC.unknown })).toBe(1);
  expect.soft(await n({ severity_max: 3 })).toBe(3); // err + crit + unknown err
  expect.soft(await n({ severity_max: 2 })).toBe(1);
  expect.soft(await n({ facility: 4 })).toBe(1);
  expect.soft(await n({ facility: [4, 23] })).toBe(4);
  expect.soft(await n({ program: 'PROGA' })).toBe(3); // case-insensitive exact
  expect.soft(await n({ program: ['progb', 'proga'] })).toBe(4);
  expect.soft(await n({ program: 'prog' })).toBe(0); // exact, not substring
  expect.soft(await n({ hostname: 'stray' })).toBe(1);
  expect.soft(await n({ q: `${m} ERR` })).toBe(1); // case-insensitive substring
  expect.soft(await n({ until: new Date(Date.now() - 3_600_000).toISOString() })).toBe(0);
  // validation
  expect.soft((await raw(admin, 'GET', `/api/syslog?since=${new Date(Date.now() - 40 * 86400_000).toISOString()}`)).status).toBe(422);
  expect.soft((await raw(admin, 'GET', '/api/syslog?severity_max=9')).status).toBe(422);
  expect.soft((await raw(admin, 'GET', '/api/syslog?cursor=garbage!!')).status).toBe(400);
  expect.soft((await raw(admin, 'GET', '/api/syslog?q=%25_%5C')).status).toBe(200); // LIKE metacharacters are literal
  expect.soft((await syslogPage(admin, { q: '%' })).items.every((x: any) => x.message.includes('%'))).toBe(true);
});

test('stats: buckets per severity, totals, top sources/programs', async () => {
  const m = marker('qa39st');
  sendSyslog({ from: SRC.aos, messages: [`<187>Nov  8 12:00:00 sw statprog: ${m}`, `<187>Nov  8 12:00:00 sw statprog: ${m}`] });
  await waitItems(admin, 'syslog', { q: m }, (it) => it.length === 2);
  const st = await admin.get(`/api/syslog/stats?q=${m}`);
  expect(st.total).toBe(2);
  expect(st.by_severity).toHaveLength(8);
  expect(st.by_severity[3]).toBe(2);
  expect([60, 300, 900, 3600, 21600, 86400]).toContain(st.bucket_s);
  expect(st.buckets.every((b: any) => b.counts.length === 8)).toBe(true);
  expect(st.buckets.reduce((a: number, b: any) => a + b.counts[3], 0)).toBe(2);
  expect(st.top_sources[0]).toMatchObject({ device_id: devId, source_ip: SRC.aos, count: 2 });
  expect(st.top_programs[0]).toMatchObject({ program: 'statprog', count: 2 });
});

test('cursor paging: limit + next_cursor, time DESC/id DESC, no duplicates', async () => {
  const m = marker('qa39pg');
  sendSyslog({ from: SRC.aos, count: 7, messages: [`<14>Nov  8 12:00:00 sw pg: ${m} n{i}`] });
  await waitItems(admin, 'syslog', { q: m }, (it) => it.length === 7);
  const seen: number[] = [];
  let cursor: string | null = null;
  let pages = 0;
  do {
    const p: any = await syslogPage(admin, { q: m, limit: 3, ...(cursor ? { cursor } : {}) });
    expect(p.limit).toBe(3);
    seen.push(...p.items.map((x: any) => x.id));
    cursor = p.next_cursor;
    pages++;
  } while (cursor && pages < 10);
  expect(pages).toBe(3);
  expect(new Set(seen).size).toBe(7);
  const all = (await syslogPage(admin, { q: m })).items;
  expect(all.map((x: any) => x.id)).toEqual(seen);
  for (let i = 1; i < all.length; i++) {
    const a = all[i - 1], b = all[i];
    expect(Date.parse(a.time) > Date.parse(b.time) || (a.time === b.time && a.id > b.id)).toBe(true);
  }
  const one = await admin.get(`/api/syslog/${all[0].id}`);
  expect(one).toEqual(all[0]);
  expect((await raw(admin, 'GET', '/api/syslog/999999999')).status).toBe(404);
});

test('UI: Logs page (global) and device Logs tab: table, filter, histogram, drawer, ltr messages', async ({ page }) => {
  await featureOn(admin, 'traps'); // the Traps tab is only shown while traps are on
  const m = marker('qa39ui');
  sendSyslog({ from: SRC.aos, ids: ['S1'] });
  sendSyslog({ from: SRC.aos, messages: [`<187>Nov  8 12:00:00 sw-ui uiprog: ${m} שלום error`] });
  await waitItems(admin, 'syslog', { q: m }, (it) => it.length === 1);
  await loginViaApi(page, undefined, '/logs');
  await expect(page.getByRole('tab', { name: tOr('logs.tab.syslog', 'Syslog') })).toBeVisible();
  for (const t of [tOr('logs.tab.traps', 'Traps'), tOr('logs.tab.unknown', 'Unknown sources'), tOr('logs.tab.rules', 'Rules')]) {
    await expect(page.getByRole('tab', { name: t })).toBeVisible();
  }
  const row = page.getByRole('row').filter({ hasText: m });
  await expect(row).toBeVisible();
  await expect(row.locator('[dir="ltr"]').filter({ hasText: m })).toBeVisible();
  // histogram (ECharts renders a canvas)
  await expect(page.locator('canvas').first()).toBeVisible();
  await shot2b3(page, '39-logs-syslog');
  await row.click();
  const drawer = page.getByRole('dialog');
  await expect(drawer).toContainText('uiprog');
  await expect(drawer).toContainText(tOr('logs.createRule', 'Create rule from this message'));
  await page.keyboard.press('Escape');
  // device page: Logs tab pre-filtered by device
  await page.goto(`/devices/${devId}?tab=logs`);
  await expect(page.getByRole('row').filter({ hasText: m })).toBeVisible();
});

test('UI: live tail prepends new messages without reload', async ({ page }) => {
  await loginViaApi(page, undefined, '/logs');
  await page.evaluate(() => ((window as any).__e2eNoReload = true));
  const live = page.getByRole('switch', { name: tOr('logs.live', 'Live') }).or(page.getByRole('button', { name: tOr('logs.live', 'Live') })).or(page.getByRole('checkbox', { name: tOr('logs.live', 'Live') }));
  await live.first().click();
  await sleep(1500); // subscribe round trip
  const m = marker('qa39live');
  sendSyslog({ from: SRC.aos, messages: [`<14>Nov  8 12:00:00 sw live: ${m}`] });
  await expect(page.getByRole('row').filter({ hasText: m })).toBeVisible({ timeout: 10_000 });
  expect(await page.evaluate(() => (window as any).__e2eNoReload === true)).toBe(true);
});
