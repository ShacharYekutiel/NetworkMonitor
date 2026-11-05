import { expect, test, type Locator, type Page } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { raw } from '../lib/lab';
import { T, devicePorts, ensurePortLab, openPorts, portOf, shot2b2, tile, type PortLab } from '../lib/lab2b2';

// [2b-2] (b) Port modal (architecture.md §11.5): every field of an access port (VLAN, mode, duplex, PoE, MAC
// with vendor + IP + hostname), an uplink with the MAC count and "Show all MACs" (include_uplink_macs), the
// AP port with its neighbour link, close by X / Escape / outside click, ?port= deep link, Start monitoring.
// default mode: a failure does not hide the results of the later tests (state is re-derived per test)
test.describe.configure({ mode: 'default' });

let api: Api;
let lab: PortLab;

test.beforeAll(async () => {
  api = await apiAs();
  lab = await ensurePortLab(api);
});

const modal = (page: Page) => page.getByTestId('port-modal');
/** The <dd> next to the <dt> whose text is `label` (modal field rows). */
function field(scope: Locator, label: string): Locator {
  return scope.locator('dt').filter({ hasText: new RegExp(`^\\s*${label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`) }).first().locator('xpath=following-sibling::dd[1]');
}

async function openTile(page: Page, deviceId: number, ifIndex: number): Promise<Locator> {
  await openPorts(page, deviceId);
  const detail = page.waitForResponse((r) => r.url().includes(`/api/devices/${deviceId}/ports/${ifIndex}`) && r.request().method() === 'GET');
  await tile(page, ifIndex).click();
  expect((await detail).status()).toBe(200);
  await expect(modal(page)).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`[?&]port=${ifIndex}\\b`));
  return modal(page);
}

test('access port 1: state, link, PoE, traffic chart (linked sensor) and the MAC row', async ({ page }) => {
  await loginViaApi(page);
  const m = await openTile(page, lab.aos.id, 1);
  await expect(m.getByRole('heading').first()).toContainText(T('ports.portN', { name: '1' }));
  await expect(m).toContainText('Reception PC'); // alias
  await expect(field(m, T('ports.field.admin'))).toHaveText(T('ports.ifStatus.up'));
  await expect(field(m, T('ports.field.oper'))).toHaveText(T('ports.ifStatus.up'));
  await expect(field(m, T('ports.field.lastChange'))).not.toHaveText('—');
  await expect(field(m, T('ports.field.speed'))).toContainText(/1\s*G/);
  await expect(field(m, T('ports.field.duplex'))).toHaveText(T('ports.duplex.full'));
  await expect(field(m, T('ports.field.mode'))).toHaveText(T('ports.mode.access'));
  await expect(field(m, T('ports.field.pvid'))).toHaveText('1');
  await expect(field(m, T('ports.field.untagged'))).toContainText('1');
  await expect(field(m, T('ports.field.poeAdmin'))).toHaveText(T('ports.poeEnabled'));
  await expect(field(m, T('ports.field.poeDetection'))).toHaveText(T('ports.poeDetection.delivering'));
  await expect(field(m, T('ports.field.poeClass'))).toHaveText('3');
  await expect(field(m, T('ports.field.poePower'))).toHaveText('—');
  // linked sensor -> history chart + link, no "Start monitoring"
  await expect(m.getByRole('link', { name: T('ports.openSensor') })).toHaveAttribute('href', `/sensors/${lab.ifSensorId}`);
  await expect(m.getByRole('button', { name: T('ports.startMonitoring') })).toHaveCount(0);
  // connected: MAC table
  const macs = m.getByTestId('mac-table');
  const row = macs.getByRole('row').filter({ hasText: '00:50:56:01:00:01' });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText(/VMware/i);
  await expect(row).toContainText('10.0.0.11');
  await expect(row).toContainText('pc-reception.lan');
  await expect(row.locator('bdi[dir="ltr"]').first()).toBeVisible(); // MAC/IP/hostname are LTR
  await shot2b2(page, 'port-modal-access-en');
});

test('access port 2 (two MACs, Cisco OUI, IP + PTR) and port 4 (random MAC, legacy-ARP IP)', async ({ page }) => {
  await loginViaApi(page);
  let m = await openTile(page, lab.aos.id, 2);
  const macs = m.getByTestId('mac-table');
  await expect(macs.getByRole('row').filter({ hasText: /\d\d:/ })).toHaveCount(2);
  const r3 = macs.getByRole('row').filter({ hasText: '00:00:0c:00:00:03' });
  await expect(r3).toContainText(/Cisco/);
  await expect(r3).toContainText('10.0.0.12');
  await expect(r3).toContainText('phone-12.lan');
  await expect(field(m, T('ports.field.poeDetection'))).toHaveText(T('ports.poeDetection.searching'));
  await page.keyboard.press('Escape');
  await expect(modal(page)).toBeHidden();
  m = await openTile(page, lab.aos.id, 4);
  const r4 = m.getByTestId('mac-table').getByRole('row').filter({ hasText: 'da:a1:19:00:00:04' });
  await expect(r4).toContainText(T('ports.mac.random'));
  await expect(r4).toContainText('10.0.0.14');
  await expect(field(m, T('ports.field.poeClass'))).toHaveText('4');
});

test('port 7: VLAN 10, Start monitoring creates the snmp_interface sensor', async ({ page }) => {
  const before = (await api.get(`/api/sensors?device_id=${lab.aos.id}&page_size=1000`)).items.filter((s: any) => s.type === 'snmp_interface' && s.config?.if_index === 7);
  for (const s of before) await raw(api, 'DELETE', `/api/sensors/${s.id}`);
  await loginViaApi(page);
  const m = await openTile(page, lab.aos.id, 7);
  await expect(field(m, T('ports.field.pvid'))).toHaveText('10');
  await expect(field(m, T('ports.field.untagged'))).toContainText('10');
  await expect(m.getByTestId('mac-table').getByRole('row').filter({ hasText: '00:50:56:01:00:07' })).toContainText('10.0.0.17');
  await expect(m).toContainText(T('ports.noSensor'));
  const req = page.waitForRequest((r) => r.url().endsWith('/api/sensors') && r.method() === 'POST');
  await m.getByRole('button', { name: T('ports.startMonitoring') }).click();
  const body = (await req).postDataJSON();
  expect(body).toMatchObject({ device_id: lab.aos.id, type: 'snmp_interface', name: '7', config: { if_index: 7, if_name: '7' } });
  await expect(m.getByRole('link', { name: T('ports.openSensor') })).toBeVisible({ timeout: 20_000 });
  const created = (await api.get(`/api/sensors?device_id=${lab.aos.id}&page_size=1000`)).items.find((s: any) => s.type === 'snmp_interface' && s.config?.if_index === 7);
  expect(created).toBeTruthy();
  await expect.poll(async () => portOf(await devicePorts(api, lab.aos.id), 7).sensor_id).toBe(created.id);
  await raw(api, 'DELETE', `/api/sensors/${created.id}`);
});

test('uplink port 9: MAC count, "Show all MACs" refetches with include_uplink_macs=true', async ({ page }) => {
  await loginViaApi(page);
  const m = await openTile(page, lab.aos.id, 9);
  await expect(m).toContainText(T('ports.uplinkMacs', { count: 7 }));
  await expect(m.getByTestId('mac-table')).toHaveCount(0);
  await expect(field(m, T('ports.field.mode'))).toHaveText(T('ports.mode.trunk'));
  await expect(field(m, T('ports.field.tagged'))).toContainText(/10.*20/);
  const req = page.waitForResponse((r) => r.url().includes(`/api/devices/${lab.aos.id}/ports/9`) && r.url().includes('include_uplink_macs=true'));
  await m.getByRole('button', { name: T('ports.showAllMacs') }).click();
  const detail = await (await req).json();
  expect(detail).toMatchObject({ macs_hidden: false, is_uplink: true, mac_count: 7 });
  expect(detail.macs).toHaveLength(7);
  await expect(m.getByTestId('mac-table').getByRole('row').filter({ hasText: /02:00:00:00:09:0\d/ })).toHaveCount(7);
  await shot2b2(page, 'port-modal-uplink-en');
  // API: hidden by default
  const plain = await api.get(`/api/devices/${lab.aos.id}/ports/9`);
  expect(plain).toMatchObject({ macs_hidden: true, macs: [], mac_count: 7 });
  expect(plain.neighbors).toEqual([]);
});

test('neighbour uplink port 10 and the AP port (CW 2) link to their devices', async ({ page }) => {
  await loginViaApi(page);
  let m = await openTile(page, lab.aos.id, 10);
  await expect(m.getByRole('link', { name: lab.cw.name })).toHaveAttribute('href', `/devices/${lab.cw.id}`);
  await expect(m).toContainText('Ten-GigabitEthernet1/0/49');
  await page.keyboard.press('Escape');
  m = await openTile(page, lab.cw.id, 2);
  const link = m.getByRole('link', { name: lab.ap.name });
  await expect(link).toHaveAttribute('href', `/devices/${lab.ap.id}`);
  await expect(m).toContainText('eth0');
  const detail = await api.get(`/api/devices/${lab.cw.id}/ports/2`);
  expect(detail.neighbors.map((n: any) => [n.source, n.device_id, n.device_class])).toEqual([['lldp', lab.ap.id, 'access_point']]);
  await shot2b2(page, 'port-modal-ap-en');
  await link.click();
  await expect(page).toHaveURL(new RegExp(`/devices/${lab.ap.id}`));
});

test('closes with X, Escape and an outside click; ?port= opens it', async ({ page }) => {
  await loginViaApi(page);
  await openTile(page, lab.aos.id, 3);
  await modal(page).getByRole('button', { name: T('common.close') }).first().click();
  await expect(modal(page)).toBeHidden();
  await expect(page).not.toHaveURL(/[?&]port=/);

  await tile(page, 3).click();
  await expect(modal(page)).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(modal(page)).toBeHidden();

  await tile(page, 3).click();
  await expect(modal(page)).toBeVisible();
  const vp = page.viewportSize()!;
  await page.mouse.click(5, vp.height - 5); // backdrop
  await expect(modal(page)).toBeHidden();

  await openPorts(page, lab.aos.id, 'faceplate', 6);
  await expect(modal(page)).toBeVisible();
  await expect(modal(page).getByRole('heading').first()).toContainText(T('ports.portN', { name: '6' }));
  await expect(field(modal(page), T('ports.field.oper'))).toHaveText(T('ports.ifStatus.down'));
  await expect(tile(page, 6)).toHaveAttribute('aria-pressed', 'true'); // selected tile (ring)
  // focus is trapped in the dialog
  for (let i = 0; i < 12; i++) await page.keyboard.press('Tab');
  expect(await page.evaluate(() => !!document.activeElement?.closest('[role="dialog"]'))).toBe(true);
});
