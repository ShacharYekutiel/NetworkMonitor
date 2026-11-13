import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { raw } from '../lib/lab';
import { T, devicePorts, ensurePortLab, openPorts, portOf, type PortLab } from '../lib/lab2b2';
import { shot13 } from '../lib/lab13';

// [1.3] 65 — ports: connected IP / name / LLDP neighbour columns and ports.csv (api.md § "[1.3] Type changes",
// "[1.3] Ports: CSV export", architecture.md § 14.4). 2b-2 lab: port-aos port 1 = 00:50:56:01:00:01 ->
// 10.0.0.11 (fw-arp) -> pc-reception.lan (PTR file); port 9/10 trunks; port-cw port 2 = LLDP U6-Lite-Office
// (mgmt 127.0.0.54).
test.describe.configure({ mode: 'serial' });

let admin: Api;
let lab: PortLab;

/** The neighbour name the ports view shows for port-cw port 2 (the LLDP neighbour 127.0.0.54). */
async function apName(): Promise<string> {
  return (await devicePorts(admin, lab.cw.id)).ports.find((p: any) => p.if_index === 2)?.neighbor?.name ?? 'U6-Lite-Office';
}

test.beforeAll(async () => {
  admin = await apiAs();
  lab = await ensurePortLab(admin);
});

test.afterEach(async ({ page }) => {
  await page.evaluate(() => { try { localStorage.removeItem('netmon.ports.columns'); localStorage.removeItem('netmon.ports.columns.known'); } catch { /* */ } }).catch(() => {});
});

test('API: important / reasons, connected {count, first}, neighbour name + mgmt address', async () => {
  const aos = await devicePorts(admin, lab.aos.id);
  const p1 = portOf(aos, 1);
  expect(p1).toMatchObject({ important: false, important_reasons: [] });
  expect(p1.connected).toMatchObject({ count: 1, first: { mac: '00:50:56:01:00:01', ip: '10.0.0.11', hostname: 'pc-reception.lan' } });
  const p2 = portOf(aos, 2);
  expect(p2.connected.count).toBe(2);
  expect(portOf(aos, 9).important_reasons).toContain('trunk');
  expect(portOf(aos, 9).important).toBe(true);
  const p10 = portOf(aos, 10);
  expect(p10.important_reasons).toEqual(expect.arrayContaining(['uplink', 'trunk']));
  expect(p10.connected.first, 'uplink: MACs not loaded').toBeNull();
  expect(p10.connected.count).toBe(p10.mac_count);
  const cw = await devicePorts(admin, lab.cw.id);
  // neighbor_name = neighbor.name: the LLDP sysName, or the name of the matched device (127.0.0.54; other specs
  // may have created a differently named device for that address)
  expect(portOf(cw, 2)).toMatchObject({ neighbor_name: await apName(), neighbor_mgmt_address: '127.0.0.54' });
  const onAp = (await admin.get('/api/devices?q=127.0.0.54&page_size=1000')).items.filter((d: any) => d.host === '127.0.0.54').map((d: any) => d.name);
  expect(['U6-Lite-Office', ...onAp]).toContain(portOf(cw, 2).neighbor_name);
  expect(portOf(cw, 1).neighbor_name).toBeNull();
});

test('list view: IP and Name / neighbor columns by default (also for a stored pre-1.3 column choice)', async ({ page }) => {
  await loginViaApi(page);
  // a column choice saved by 1.2 (no "known" key): the new default columns are added once
  await page.evaluate(() => {
    localStorage.setItem('netmon.ports.columns', JSON.stringify(['status', 'vlan', 'connected']));
    localStorage.removeItem('netmon.ports.columns.known');
  });
  await openPorts(page, lab.aos.id, 'list');
  const list = page.getByTestId('port-list');
  const heads = async () => (await list.locator('thead th').allInnerTexts()).map((s) => s.trim().toLowerCase());
  expect(await heads()).toEqual(expect.arrayContaining([T('ports.col.connIp').toLowerCase(), T('ports.col.connName').toLowerCase()]));
  const row1 = list.locator('tbody tr').first();
  await expect(row1.getByTestId('port-conn-ip')).toHaveText('10.0.0.11');
  await expect(row1.getByTestId('port-conn-name')).toContainText('pc-reception.lan');
  const row2 = list.locator('tbody tr').nth(1);
  await expect(row2.getByTestId('port-conn-more')).toHaveText('+1');
  await shot13(page, '65-ports-list-ip-name');

  await openPorts(page, lab.cw.id, 'list');
  const cwRow2 = page.getByTestId('port-list').locator('tbody tr').filter({ hasText: 'GigabitEthernet1/0/2' }).first();
  await expect(cwRow2.getByTestId('port-conn-name')).toContainText(T('ports.conn.neighbor'));
  await expect(cwRow2.getByTestId('port-conn-name')).toContainText(await apName());
  await expect(cwRow2.getByTestId('port-conn-ip')).toHaveText('127.0.0.54');
  await shot13(page, '65-ports-list-lldp-neighbour');
});

test('mobile: the IP column is hidden and the IP shows under the name', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await loginViaApi(page);
  await openPorts(page, lab.aos.id, 'list');
  const row1 = page.getByTestId('port-list').locator('tbody tr').first();
  await expect(row1.getByTestId('port-conn-ip')).toBeHidden();
  await expect(row1.getByTestId('port-conn-name')).toContainText('10.0.0.11');
  await shot13(page, '65-ports-list-mobile');
});

test('CSV: "Export CSV" downloads ports-<device>.csv (BOM, CRLF, header, one row per port x MAC)', async ({ page }) => {
  await loginViaApi(page);
  await openPorts(page, lab.aos.id, 'list');
  const [dl] = await Promise.all([page.waitForEvent('download'), page.getByTestId('ports-csv').click()]);
  expect(dl.suggestedFilename()).toBe(`ports-${lab.aos.name}.csv`);
  const buf = readFileSync((await dl.path()) as string);
  expect([...buf.subarray(0, 3)], 'UTF-8 BOM').toEqual([0xef, 0xbb, 0xbf]);
  const text = buf.subarray(3).toString('utf8');
  expect(text).toContain('\r\n');
  const lines: string[] = text.split('\r\n').filter(Boolean);
  expect(lines[0]).toBe('port,alias,status,vlan,mode,mac,ip,hostname,vendor,neighbor');
  const rows: string[][] = lines.slice(1).map((l: string) => l.split(','));
  const of = (port: string) => rows.filter((r) => r[0] === port);
  expect(of('1')).toHaveLength(1);
  expect(of('1')[0]).toEqual(expect.arrayContaining(['Reception PC', '00:50:56:01:00:01', '10.0.0.11', 'pc-reception.lan']));
  expect(of('2')).toHaveLength(2);
  expect(of('10'), 'uplink: one row, empty mac/ip').toHaveLength(1);
  expect(of('10')[0][5]).toBe('');
  expect(of('9')[0][4]).toBe('trunk');
  expect(of('5')).toHaveLength(1); // no MACs -> one row

  const r = await admin.ctx.fetch(`/api/devices/${lab.aos.id}/ports.csv`);
  expect(r.status()).toBe(200);
  expect(r.headers()['content-type']).toMatch(/^text\/csv; charset=utf-8/i);
  expect(r.headers()['content-disposition']).toContain(`filename="ports-${lab.aos.name}.csv"`);
  // LLDP neighbour column: "<name> (<mgmt address>)"
  const cw = (await (await admin.ctx.fetch(`/api/devices/${lab.cw.id}/ports.csv`)).text()).split('\r\n');
  expect(cw.find((l) => l.startsWith('GigabitEthernet1/0/2,'))).toContain(`${await apName()} (127.0.0.54)`);
  expect((await raw(admin, 'GET', '/api/devices/99999999/ports.csv')).status).toBe(404);
});
