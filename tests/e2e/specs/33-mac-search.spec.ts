import { expect, test } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { T, ensurePortLab, search, shot2b2, type PortLab } from '../lib/lab2b2';

// [2b-2] (f) Global MAC / IP search (api.md [2b-2] Search) and the Find page: best access port, uplink rows,
// ARP hits, prefix search, validation; the port cell links to /devices/{id}?tab=ports&port={if_index}.
// default mode: a failure does not hide the results of the later tests (state is re-derived per test)
test.describe.configure({ mode: 'default' });

let api: Api;
let lab: PortLab;

test.beforeAll(async () => {
  api = await apiAs();
  lab = await ensurePortLab(api);
});

const ours = (items: any[]) => items.filter((x) => [lab.aos.id, lab.cw.id, lab.cx.id].includes(x.device_id));

test('q=00:50:56:01:00:07 -> one item, AOS port 7, vlan 10, ip 10.0.0.17, best', async () => {
  const r = await search(api, { q: '00:50:56:01:00:07' });
  expect(r.status, r.text).toBe(200);
  expect(r.body.query).toEqual({ mac: '00:50:56:01:00:07', mac_prefix: null, ip: null });
  expect(r.body.items).toHaveLength(1);
  expect(r.body.items[0]).toMatchObject({
    mac: '00:50:56:01:00:07', device_id: lab.aos.id, device_name: lab.aos.name, device_host: lab.aos.host, if_index: 7, port: '7',
    port_alias: 'Accounting', vlan: 10, ip: '10.0.0.17', ips: ['10.0.0.17'], hostname: null, is_uplink: false, present: true, best: true, locally_administered: false,
  });
  expect(r.body.items[0].vendor).toMatch(/vmware/i);
  expect(r.body.arp.map((a: any) => [a.ip, a.source_device_id, a.present])).toEqual([['10.0.0.17', lab.arp.id, true]]);
});

test('q=02-00-00-00-09-01 -> CW port 3 (best, access) then AOS port 9 (uplink); ip 10.0.0.99', async () => {
  const r = (await search(api, { q: '02-00-00-00-09-01' })).body;
  expect(r.query.mac).toBe('02:00:00:00:09:01');
  const it = ours(r.items);
  expect(it.map((x: any) => [x.device_id, x.if_index, x.best, x.is_uplink])).toEqual([
    [lab.cw.id, 3, true, false],
    [lab.aos.id, 9, false, true],
  ]);
  for (const x of it) expect(x).toMatchObject({ ip: '10.0.0.99', locally_administered: true, vendor: null });
  expect(r.items.filter((x: any) => x.best)).toHaveLength(1);
});

test('notations: aabb.ccdd.eeff, bare hex, upper case -> same MAC', async () => {
  for (const q of ['0050.5601.0007', '005056010007', '00:50:56:01:00:07'.toUpperCase()]) {
    const r = (await search(api, { q })).body;
    expect.soft(r.query.mac, q).toBe('00:50:56:01:00:07');
    expect.soft(r.items.map((x: any) => x.if_index), q).toEqual([7]);
  }
});

test('ip=10.0.0.12 -> AOS port 2, arp from the ARP router (present)', async () => {
  const r = (await search(api, { ip: '10.0.0.12' })).body;
  expect(r.query).toEqual({ mac: null, mac_prefix: null, ip: '10.0.0.12' });
  expect(r.items.map((x: any) => [x.device_id, x.if_index, x.mac, x.hostname, x.best])).toEqual([[lab.aos.id, 2, '00:00:0c:00:00:03', 'phone-12.lan', true]]);
  expect(r.arp).toEqual([expect.objectContaining({ ip: '10.0.0.12', mac: '00:00:0c:00:00:03', source_device_id: lab.arp.id, source_device_name: lab.arp.name, present: true, hostname: 'phone-12.lan' })]);
});

test('q=005056 prefix -> the two VMware MACs; ip=10.0.0.50 (invalid ARP) -> none', async () => {
  const r = (await search(api, { q: '005056' })).body;
  expect(r.query).toEqual({ mac: null, mac_prefix: '005056', ip: null });
  expect([...new Set(ours(r.items).map((x: any) => x.mac))].sort()).toEqual(['00:50:56:01:00:01', '00:50:56:01:00:07']);
  const p8 = (await search(api, { q: '0050.5601' })).body;
  expect(p8.query.mac_prefix).toBe('00505601');
  const none = (await search(api, { ip: '10.0.0.50' })).body;
  expect(none.items).toEqual([]);
  expect(none.arp).toEqual([]);
  const local = (await search(api, { ip: '10.0.0.1' })).body; // type local(5): dropped
  expect(local.items).toEqual([]);
});

test('validation: q=zz, both q and ip, neither, short prefix, bad ip -> 422', async () => {
  for (const q of [{ q: 'zz' }, { q: '00:50:56:01:00:07', ip: '10.0.0.17' }, {}, { q: '00505' }, { q: '0050560100071' }, { ip: '10.0.0.256' }, { ip: 'fe80::1' }]) {
    const r = await search(api, q as Record<string, string>);
    expect.soft(r.status, JSON.stringify(q)).toBe(422);
    if (r.status === 422) expect.soft(r.body.error.code).toBe('validation_error');
  }
});

test('Find page: MAC search -> best row highlighted, port link opens the modal', async ({ page }) => {
  await loginViaApi(page);
  await page.getByRole('navigation').first().getByRole('link', { name: T('nav.find') }).click();
  await expect(page).toHaveURL(/\/find/);
  const input = page.locator('main').getByRole('textbox', { name: T('find.label') }); // the header has a quick-find box too
  await input.fill('02:00:00:00:09:01');
  await input.press('Enter');
  await expect(page).toHaveURL(/[?&]q=02/);
  const results = page.getByTestId('find-results');
  await expect(results).toBeVisible();
  await expect(page.getByTestId('find-best')).toContainText(lab.cw.name);
  await expect(page.getByTestId('find-best')).toContainText('10.0.0.99');
  const rows = results.locator('tbody tr');
  await expect(rows.filter({ hasText: lab.cw.name })).toHaveCount(1);
  await expect(rows.filter({ hasText: lab.aos.name })).toContainText(T('find.uplink'));
  await shot2b2(page, 'find-mac-en');
  const link = rows.filter({ hasText: lab.cw.name }).getByTestId('find-port-link');
  await expect(link).toHaveAttribute('href', `/devices/${lab.cw.id}?tab=ports&port=3`);
  await link.click();
  await expect(page.getByTestId('port-modal')).toBeVisible();
  await expect(page.getByTestId('port-modal')).toContainText('02:00:00:00:09:01');
});

test('Find page: IPv4 -> ?ip=, ARP hits; invalid input -> validation message', async ({ page }) => {
  await loginViaApi(page, undefined, '/find');
  const input = page.locator('main').getByRole('textbox', { name: T('find.label') }); // the header has a quick-find box too
  await input.fill('10.0.0.11');
  await input.press('Enter');
  await expect(page).toHaveURL(/[?&]ip=10\.0\.0\.11/);
  await expect(page.getByTestId('find-best')).toContainText('pc-reception.lan');
  await expect(page.getByTestId('find-best').getByRole('link').first()).toBeVisible();
  await page.goto('/find?q=00:50:56:01:00:07');
  await expect(page.getByTestId('find-best')).toContainText(lab.aos.name);
  await page.getByTestId('find-port-link').first().click();
  await expect(page).toHaveURL(new RegExp(`/devices/${lab.aos.id}\\?.*port=7`));
  await expect(page.getByTestId('port-modal')).toBeVisible();
  await page.goto('/find');
  await input.fill('zz');
  await input.press('Enter');
  await expect(page.getByText(T('errors.validation_error'))).toBeVisible();
});
