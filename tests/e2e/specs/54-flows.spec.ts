// [3] NetFlow / IPFIX / sFlow collector end to end (docs/architecture.md §13.3, §13.11 spec 54; api.md "[3] Flows").
// sFlow (scenario s2, agent 127.0.0.91, 1:4096) and NetFlow v9 (s1, 127.0.0.92) -> exporters auto-accepted
// (attributed to devices), an unknown exporter (127.0.0.93, v5) accepted, another (127.0.0.94) ignored; top
// talkers / applications / conversations / interface totals equal the scenario's expected aggregates; the
// sampling correction is shown. Records are counted in the minute they were received; a minute is flushed after
// it closes (+5 s grace), so the test waits up to ~70 s.
import { expect, test } from '@playwright/test';
import { apiAs, loginViaApi, type Api } from '../lib/helpers';
import { raw } from '../lib/lab';
import {
  IPFIX_PORT, NETFLOW_PORT, PHASE3, SFLOW_PORT, ensureDeviceAt, ensureGroupNamed, expectedFlows, sendFlows,
  setFeatures, shot3, tx,
} from '../lib/lab3';

test.describe.configure({ mode: 'serial' });
test.skip(!PHASE3, 'phase-3 simulators not running');

const SF = '127.0.0.91';
const NF = '127.0.0.92';
const UNKNOWN = '127.0.0.93';
const IGNORED = '127.0.0.94';
let api: Api;
let since = '';
const ex: Record<string, any> = {};

async function exporters(): Promise<any[]> {
  return (await api.get('/api/flows/exporters')).items;
}
async function exporterAt(addr: string): Promise<any> {
  let e: any = null;
  await expect.poll(async () => { e = (await exporters()).find((x) => x.address === addr) ?? null; return !!e; },
    { timeout: 30_000, message: `exporter ${addr} listed` }).toBe(true);
  return e;
}
const q = (o: Record<string, string | number>) => Object.entries(o).map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join('&');

/** Waits until the exporter's interface totals reach the expected bytes (minute flushed and written). */
async function waitStored(exporterId: number, totalIn: number): Promise<void> {
  await expect.poll(async () => {
    const r = await api.get(`/api/flows/interfaces?${q({ exporter_id: exporterId, since, until: new Date(Date.now() + 120_000).toISOString() })}`);
    return r.items.reduce((a: number, i: any) => a + i.in_bytes, 0);
  }, { timeout: 120_000, intervals: [2000, 5000], message: `flows of exporter ${exporterId} stored` }).toBe(totalIn);
}

test.beforeAll(async () => {
  api = await apiAs();
  const g = await ensureGroupNamed(api, 'QA Flows 3');
  await ensureDeviceAt(api, g.id, SF, 'qa3-core-sflow');
  await ensureDeviceAt(api, g.id, NF, 'qa3-fw-netflow');
  await setFeatures(api, { flows: true });
  since = new Date(Date.now() - 60_000).toISOString();
});

test('receivers listen on the three UDP ports while ON; status counters', async () => {
  await expect.poll(async () => {
    const s = await api.get('/api/flows/status');
    return s.receivers.filter((r: any) => ['netflow', 'ipfix', 'sflow'].includes(r.kind) && /listen/i.test(r.state)).length;
  }, { timeout: 15_000, message: 'receivers listening' }).toBe(3);
  const s = await api.get('/api/flows/status');
  const ports = s.receivers.map((r: any) => r.port).sort();
  expect(ports).toEqual([NETFLOW_PORT, IPFIX_PORT, SFLOW_PORT].sort());
  expect(s.budget_per_s).toBe(10_000);
  expect(['cagg', 'table']).toContain(s.rollup);
  const h = await api.get('/api/health');
  expect(['cagg', 'table']).toContain(h.flows_rollup);
});

test('sFlow + NetFlow v9 exporters are auto-accepted and attributed; unknown accept / ignore', async () => {
  test.setTimeout(180_000);
  const sfOut = sendFlows('sflow', 's2.yaml', SF, SFLOW_PORT);
  const nfOut = sendFlows('v9', 's1.yaml', NF, NETFLOW_PORT);
  expect(sfOut.sent).toBeGreaterThan(0);
  expect(nfOut.sent).toBe(2);
  ex.sf = await exporterAt(SF);
  ex.nf = await exporterAt(NF);
  expect(ex.sf).toMatchObject({ state: 'accepted', device_name: 'qa3-core-sflow' });
  expect(ex.nf).toMatchObject({ state: 'accepted', device_name: 'qa3-fw-netflow' });
  // protocols are recorded with the per-minute exporter update (records_total "at most once per minute")
  await expect.poll(async () => (await exporterAt(SF)).protocols, { timeout: 90_000 }).toContain('sflow5');
  await expect.poll(async () => (await exporterAt(NF)).protocols, { timeout: 30_000 }).toContain('netflow9');
  // unknown exporters: pending, data dropped (unknown_exporter) until accepted
  const before = (await api.get('/api/flows/status')).counters.dropped.unknown_exporter;
  sendFlows('v5', 's6_v5_sampled.yaml', UNKNOWN, NETFLOW_PORT);
  sendFlows('v5', 's6_v5_sampled.yaml', IGNORED, NETFLOW_PORT);
  ex.unk = await exporterAt(UNKNOWN);
  ex.ign = await exporterAt(IGNORED);
  expect(ex.unk.state).toBe('pending');
  expect(ex.unk.device_id).toBeNull();
  await expect.poll(async () => (await api.get('/api/flows/status')).counters.dropped.unknown_exporter, { timeout: 15_000 }).toBeGreaterThan(before);
  const acc = await api.patch(`/api/flows/exporters/${ex.unk.id}`, { state: 'accepted', device_id: null, sampling_override: null });
  expect(acc.state).toBe('accepted');
  const ign = await api.patch(`/api/flows/exporters/${ex.ign.id}`, { state: 'ignored' });
  expect(ign.state).toBe('ignored');
  sendFlows('v5', 's6_v5_sampled.yaml', UNKNOWN, NETFLOW_PORT);
  const ignBefore = (await api.get('/api/flows/status')).counters.dropped.ignored_exporter;
  sendFlows('v5', 's6_v5_sampled.yaml', IGNORED, NETFLOW_PORT);
  await expect.poll(async () => (await api.get('/api/flows/status')).counters.dropped.ignored_exporter, { timeout: 15_000 }).toBeGreaterThan(ignBefore);
  // viewer-level validation: bad state -> 422
  const bad = await raw(api, 'PATCH', `/api/flows/exporters/${ex.unk.id}`, { state: 'pending' });
  expect(bad.status).toBe(422);
});

test('stored aggregates equal the scenario totals (talkers, applications, conversations, interfaces)', async () => {
  test.setTimeout(240_000);
  const eSf = expectedFlows('sflow', 's2.yaml', SF);
  const eNf = expectedFlows('v9', 's1.yaml', NF);
  const inTotal = (e: any) => Object.entries(e.if).filter(([k]) => k.endsWith('/in')).reduce((a, [, v]) => a + (v as number), 0);
  await waitStored(ex.sf.id, inTotal(eSf));
  await waitStored(ex.nf.id, inTotal(eNf));
  const until = new Date(Date.now() + 60_000).toISOString();
  for (const [e, x] of [[eSf, ex.sf], [eNf, ex.nf]] as const) {
    const base = { exporter_id: x.id, since, until, limit: 100 };
    const talk = await api.get(`/api/flows/top/talkers?${q({ ...base, side: 'src' })}`);
    expect(talk.source).toBe('minute');
    expect(talk.total_bytes, `exporter ${x.address} total`).toBe(e.total_bytes);
    const got = Object.fromEntries(talk.items.map((i: any) => [i.ip, i.bytes]));
    expect(got, `${x.address} top source talkers`).toEqual(e.hosts_src);
    const dst = await api.get(`/api/flows/top/talkers?${q({ ...base, side: 'dst' })}`);
    expect(Object.fromEntries(dst.items.map((i: any) => [i.ip, i.bytes]))).toEqual(e.hosts_dst);
    const apps = await api.get(`/api/flows/top/applications?${q(base)}`);
    expect(Object.fromEntries(apps.items.map((i: any) => [`${i.protocol}/${i.port}`, i.bytes])), `${x.address} applications`).toEqual(e.apps);
    expect(apps.items.reduce((a: number, i: any) => a + i.share, 0)).toBeCloseTo(1, 2);
    const conv = await api.get(`/api/flows/top/conversations?${q(base)}`);
    expect(conv.items.reduce((a: number, i: any) => a + i.bytes, 0) + conv.other_bytes).toBe(e.total_bytes);
    expect(conv.items[0].bytes).toBe(e.conversations[0].bytes);
    expect(conv.items[0].src_ip).toBe(e.conversations[0].src_ip);
    const ifs = await api.get(`/api/flows/interfaces?${q({ exporter_id: x.id, since, until })}`);
    const gotIf: Record<string, number> = {};
    for (const i of ifs.items) {
      if (i.in_bytes) gotIf[`${i.if_index}/in`] = i.in_bytes;
      if (i.out_bytes) gotIf[`${i.if_index}/out`] = i.out_bytes;
    }
    expect(gotIf, `${x.address} interfaces`).toEqual(e.if);
    // interface series: bits/s per bucket integrate back to the bytes
    const first = ifs.items[0];
    const ser = await api.get(`/api/flows/series?${q({ exporter_id: x.id, if_index: first.if_index, since, until, split: 'none' })}`);
    const inSeries = ser.series.find((s: any) => s.direction === 'in');
    const bytes = inSeries.points.reduce((a: number, [, bps]: [string, number]) => a + (bps * ser.bucket_s) / 8, 0);
    expect(Math.round(bytes)).toBe(first.in_bytes);
  }
  // sampling correction visible per exporter
  await expect.poll(async () => (await exporterAt(SF)).sampling_seen, { timeout: 30_000 }).toBe(4096);
  expect((await exporterAt(NF)).sampling_seen ?? 1).toBe(1);
  // conversations limited to 24 h
  const long = await raw(api, 'GET', `/api/flows/top/conversations?${q({ since: new Date(Date.now() - 25 * 3600_000).toISOString() })}`);
  expect(long.status).toBe(422);
});

test('data before template -> no_template, then stored; IPFIX with options sampling', async () => {
  test.setTimeout(180_000);
  const s0 = (await api.get('/api/flows/status')).counters.dropped.no_template;
  // an exporter that has never sent v9 template 256 (NF already has it cached from s1)
  sendFlows('v9', 's3_data_before_template.yaml', UNKNOWN, NETFLOW_PORT);
  await expect.poll(async () => (await api.get('/api/flows/status')).counters.dropped.no_template, { timeout: 15_000 }).toBeGreaterThan(s0);
  sendFlows('ipfix', 's5_ipfix_sampled.yaml', UNKNOWN, IPFIX_PORT);
  const e = expectedFlows('ipfix', 's5_ipfix_sampled.yaml', UNKNOWN);
  await expect.poll(async () => {
    const t = await api.get(`/api/flows/top/applications?${q({ exporter_id: ex.unk.id, since, until: new Date(Date.now() + 60_000).toISOString() })}`);
    return t.items.find((i: any) => i.protocol === 6 && i.port === 80)?.bytes ?? 0;
  }, { timeout: 120_000, intervals: [2000, 5000], message: 'IPFIX http bytes x100' }).toBe(e.apps['6/80'] + expectedFlows('v9', 's3_data_before_template.yaml', UNKNOWN).apps['6/80']);
  expect((await exporterAt(UNKNOWN)).protocols).toEqual(expect.arrayContaining(['netflow5', 'ipfix']));
});

test('Flows page: exporters with device links and sampling, top talkers with the scenario hosts', async ({ page }) => {
  await loginViaApi(page, undefined, '/flows');
  const main = page.getByRole('main');
  await expect(main.getByRole('cell', { name: '10.0.20.57' }).first()).toBeVisible({ timeout: 20_000 });
  const ipCell = main.getByRole('cell', { name: '10.0.20.57' }).first();
  expect(await ipCell.evaluate((el) => getComputedStyle(el).direction)).toBe('ltr');
  await shot3(page, '54-flows-top');
  await main.getByRole('tab', { name: tx('flows.tab.exporters', /exporters/i) }).click();
  await expect(main.getByText('qa3-core-sflow').first()).toBeVisible({ timeout: 20_000 });
  await expect(main.getByText('qa3-fw-netflow').first()).toBeVisible();
  await expect(main.getByText(/4[,.\u202f\u00a0]?096/).first(), 'sampling 1:4096 shown').toBeVisible();
  await expect(main.getByText(IGNORED).first(), 'ignored exporter listed').toBeVisible();
  await shot3(page, '54-flows-exporters');
  void tx;
});
