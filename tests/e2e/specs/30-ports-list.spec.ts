import { expect, test, type Page } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { T, ensurePortLab, openPorts, shot2b2, type PortLab } from '../lib/lab2b2';

// [2b-2] (c) Port list (architecture.md §11.5): default columns, sticky Port column, "Physical ports only",
// one filter across all texts + MACs in any notation + IP/hostname/vendor/neighbour/alias, column chooser
// persisted in localStorage['netmon.ports.columns'] across a reload, row click -> modal.
// default mode: a failure does not hide the results of the later tests (state is re-derived per test)
test.describe.configure({ mode: 'default' });

let api: Api;
let lab: PortLab;

test.beforeAll(async () => {
  api = await apiAs();
  lab = await ensurePortLab(api);
});

const list = (page: Page) => page.getByTestId('port-list');
const bodyRows = (page: Page) => list(page).locator('tbody tr');
const firstCells = async (page: Page) => (await list(page).locator('tbody tr td:first-child button').allInnerTexts()).map((s) => s.trim());
async function expectFilter(page: Page, q: string, want: string[]): Promise<void> {
  await list(page).getByRole('searchbox', { name: T('ports.filter') }).fill(q);
  await expect.poll(() => firstCells(page), { message: `filter "${q}"` }).toEqual(want);
}

test('default columns, physical-only toggle, connected texts', async ({ page }) => {
  await loginViaApi(page);
  await page.evaluate(() => localStorage.removeItem('netmon.ports.columns'));
  await openPorts(page, lab.aos.id, 'list');
  const heads = (await list(page).locator('thead th').allInnerTexts()).map((s) => s.trim().toLowerCase());
  expect(heads).toEqual(['port', 'status', 'connected', 'vlan', 'mode', 'speed', 'poe', 'rxtx'].map((c) => T(`ports.col.${c}`).toLowerCase()));
  // physical + lag by default: 1-10 + Trk1, not DEFAULT_VLAN
  expect(await firstCells(page)).toEqual(['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'Trk1']);
  await list(page).getByRole('checkbox', { name: T('ports.physicalOnly') }).uncheck();
  expect(await firstCells(page)).toContain('DEFAULT_VLAN');
  await list(page).getByRole('checkbox', { name: T('ports.physicalOnly') }).check();
  const row = (name: string) => bodyRows(page).filter({ has: page.locator('td:first-child', { hasText: new RegExp(`^\\s*${name}\\b`) }) }).first();
  await expect(row('9')).toContainText(T('ports.macCount', { count: 7 })); // uplink: "7 MACs"
  await expect(row('10')).toContainText(lab.cw.name); // neighbour name linked to its device
  await expect(row('10').getByRole('link', { name: lab.cw.name })).toHaveAttribute('href', `/devices/${lab.cw.id}`);
  await expect(row('2')).toContainText('phone-12.lan'); // first_mac: hostname first
  await expect(row('2')).toContainText('+1');
  await expect(row('7')).toContainText('10.0.0.17'); // no hostname -> IP
  await expect(row('7')).toContainText('10'); // VLAN (PVID)
  await expect(row('1')).toContainText(T('ports.poeOnClass', { cls: 3 }));
  await expect(row('9')).toContainText(T('ports.mode.trunk'));
  await expect(row('5')).toContainText(T('ports.state.admin_down'));
  await shot2b2(page, 'port-list-en');
});

test('sticky Port column stays visible while the table scrolls horizontally', async ({ page }) => {
  await page.setViewportSize({ width: 480, height: 800 });
  await loginViaApi(page);
  await openPorts(page, lab.aos.id, 'list');
  const th = list(page).locator('thead th').first();
  expect(await th.evaluate((e) => getComputedStyle(e).position)).toBe('sticky');
  const scroller = page.getByTestId('port-list-scroll');
  const canScroll = await scroller.evaluate((e) => e.scrollWidth > e.clientWidth);
  expect(canScroll, 'list wider than the card at 480 px').toBe(true);
  const cell = list(page).locator('tbody tr').first().locator('td').first();
  const x0 = (await cell.boundingBox())!.x;
  await scroller.evaluate((e) => (e.scrollLeft = e.scrollWidth));
  const x1 = (await cell.boundingBox())!.x;
  expect(Math.abs(x1 - x0), 'Port cell did not move').toBeLessThan(2);
  const doc = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
  expect(doc[0], 'no page overflow').toBeLessThanOrEqual(doc[1]);
});

test('filter: MAC in 3 notations, prefix, IP, hostname, vendor, neighbour, alias, state text', async ({ page }) => {
  await loginViaApi(page);
  await openPorts(page, lab.aos.id, 'list');
  await expectFilter(page, '0050.5601.0007', ['7']);
  await expectFilter(page, '00-50-56-01-00-07', ['7']);
  await expectFilter(page, '005056010007', ['7']);
  await expectFilter(page, '00:50:56:01:00:01', ['1']);
  await expectFilter(page, '10.0.0.12', ['2']);
  await expectFilter(page, 'PHONE-12', ['2']);
  await expectFilter(page, 'cisco', ['2']);
  await expectFilter(page, lab.cw.name, ['10']);
  await expectFilter(page, 'reception', ['1']); // alias + hostname
  await expectFilter(page, 'zzz-no-such-port', []);
  await expect(list(page)).toContainText(T('ports.noMatch'));
  await expectFilter(page, '', ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'Trk1']);
});

test('column chooser: Alias + IP + Vendor persist after a reload (localStorage)', async ({ page }) => {
  await loginViaApi(page);
  await page.evaluate(() => localStorage.removeItem('netmon.ports.columns'));
  await openPorts(page, lab.aos.id, 'list');
  await list(page).getByRole('button', { name: T('ports.columns') }).click();
  const chooser = page.getByTestId('column-chooser');
  for (const c of ['alias', 'ip', 'vendor']) await chooser.getByRole('checkbox', { name: T(`ports.col.${c}`) }).check();
  await chooser.getByRole('checkbox', { name: T('ports.col.rxtx') }).uncheck();
  await page.keyboard.press('Escape');
  const heads = async () => (await list(page).locator('thead th').allInnerTexts()).map((s) => s.trim());
  expect(await heads()).toEqual(expect.arrayContaining([T('ports.col.alias'), T('ports.col.ip'), T('ports.col.vendor')]));
  expect(await heads()).not.toContain(T('ports.col.rxtx'));
  const stored = JSON.parse((await page.evaluate(() => localStorage.getItem('netmon.ports.columns'))) ?? 'null');
  expect(stored).toEqual(expect.arrayContaining(['alias', 'ip', 'vendor']));
  expect(stored).not.toContain('rxtx');
  await page.reload();
  await expect(list(page)).toBeVisible();
  expect(await heads()).toEqual(expect.arrayContaining([T('ports.col.alias'), T('ports.col.ip'), T('ports.col.vendor')]));
  expect(await heads()).not.toContain(T('ports.col.rxtx'));
  const r1 = bodyRows(page).first();
  await expect(r1).toContainText('Reception PC');
  await expect(r1).toContainText(/VMware/i);
  // corrupt value -> defaults, no crash
  await page.evaluate(() => localStorage.setItem('netmon.ports.columns', '{oops'));
  await page.reload();
  await expect(list(page).locator('thead th')).toHaveCount(8);
  await page.evaluate(() => localStorage.removeItem('netmon.ports.columns'));
});

test('row click opens the modal; view toggle keeps ?view=list', async ({ page }) => {
  await loginViaApi(page);
  await openPorts(page, lab.aos.id, 'list');
  await bodyRows(page).nth(6).locator('td').nth(2).click(); // port 7, Connected cell
  await expect(page.getByTestId('port-modal')).toBeVisible();
  await expect(page).toHaveURL(/view=list.*port=7|port=7.*view=list/);
  await page.keyboard.press('Escape');
  await page.getByRole('tab', { name: T('ports.view.faceplate') }).click();
  await expect(page.getByTestId('faceplate')).toBeVisible();
  await page.getByRole('tab', { name: T('ports.view.list') }).click();
  await expect(page).toHaveURL(/view=list/);
});
