import { expect, test } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { raw } from '../lib/lab';
import { IP, T } from '../lib/lab2b2';
import { freshSwitch, ifSensors, shot13 } from '../lib/lab13';

// [1.3] 63 — apply template: interface selection (api.md § "[1.3] Template apply: interface selection",
// architecture.md § 14.2/14.3). Own device on the ArubaOS port sim 127.0.0.81: access ports 1-8 (6/8 down),
// trunks 9/10 (tagged VLANs 10+20), Trk1 (11, LAG). Pre-existing interface sensors on access ports 1 and 2.
test.describe.configure({ mode: 'serial' });

const TPL = 'hpe_arubaos_switch';
let admin: Api;
let dev: any;
let choices: any;

async function addIf(ifIndex: number, name: string): Promise<any> {
  return admin.post('/api/sensors', { device_id: dev.id, type: 'snmp_interface', name, interval_s: 60, config: { if_index: ifIndex, if_name: String(ifIndex) } });
}

test.beforeAll(async () => {
  admin = await apiAs();
  dev = await freshSwitch(admin, IP.aos, 'qa13-apply');
  await addIf(1, 'qa13 port 1');
  await addIf(2, 'qa13 port 2');
});

test.afterAll(async () => {
  if (dev) await raw(admin, 'DELETE', `/api/devices/${dev.id}`);
});

test('API: dialog list — inventory ok, filters, trunk/LAG importance, existing sensors', async () => {
  const r = await raw(admin, 'GET', `/api/devices/${dev.id}/apply-template/interfaces?template_id=${TPL}`);
  expect(r.status, r.text).toBe(200);
  choices = r.body;
  expect(choices.inventory).toBe('ok');
  const by = Object.fromEntries(choices.items.map((i: any) => [i.if_index, i]));
  expect(choices.items.map((i: any) => i.if_index)).toEqual([...choices.items.map((i: any) => i.if_index)].sort((a, b) => a - b));
  expect(by[12], 'DEFAULT_VLAN (ifType 53) excluded by the template filters').toBeUndefined();
  for (const i of [9, 10]) {
    expect(by[i].important, `port ${i}`).toBe(true);
    expect(by[i].reasons, `port ${i}`).toContain('trunk');
  }
  expect(by[11]).toMatchObject({ important: true, reasons: expect.arrayContaining(['lag']) });
  for (const i of [1, 2, 7]) expect(by[i], `access port ${i}`).toMatchObject({ important: false, reasons: [] });
  expect(by[1]).toMatchObject({ has_sensor: true, sensor_id: expect.any(Number) });
  expect(by[2].has_sensor).toBe(true);
  expect(by[9]).toMatchObject({ has_sensor: false, sensor_id: null });
  // unknown template -> 422; unknown device -> 404
  expect((await raw(admin, 'GET', `/api/devices/${dev.id}/apply-template/interfaces?template_id=nope`)).status).toBe(422);
  expect((await raw(admin, 'GET', `/api/devices/99999999/apply-template/interfaces?template_id=${TPL}`)).status).toBe(404);
});

test('UI: "Choose ports" preselects important + monitored, "Remove unselected" deletes port 1', async ({ page }) => {
  await loginViaApi(page, undefined, `/devices/${dev.id}`);
  await page.getByRole('button', { name: T('templates.apply') }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel(/Template/).selectOption(TPL);
  const ports = dlg.getByTestId('apply-ports');
  await expect(ports).toBeVisible();
  // default choice: important ports
  await expect(ports.getByRole('radio', { name: T('applyPorts.choice.important') })).toBeChecked();
  await ports.getByRole('radio', { name: T('applyPorts.choice.choose') }).check();
  const list = dlg.getByTestId('apply-ports-list');
  await expect(list).toBeVisible({ timeout: 60_000 });
  const row = (name: string) => list.getByTestId('apply-port-row').filter({ has: page.getByText(name, { exact: true }) });
  const box = (name: string) => row(name).getByRole('checkbox');
  const preselected = choices.items.filter((i: any) => i.important || i.has_sensor).map((i: any) => i.if_index);
  await expect(dlg.getByTestId('apply-ports-count')).toHaveText(T('applyPorts.selectedOf', { n: preselected.length, total: choices.items.length }));
  for (const n of ['1', '2', '9', '10']) await expect(box(n), `port ${n} preselected`).toBeChecked();
  await expect(box('7')).not.toBeChecked();
  await expect(row('9')).toContainText(T('applyPorts.reason.trunk'));
  await expect(row('1').getByTestId('has-sensor')).toBeVisible();
  // search narrows the list (name or alias)
  await dlg.getByRole('searchbox', { name: T('applyPorts.search') }).fill('reception');
  await expect(list.getByTestId('apply-port-row')).toHaveCount(1);
  await box('1').uncheck();
  await dlg.getByRole('searchbox', { name: T('applyPorts.search') }).fill('');
  await expect(list.getByTestId('apply-port-row')).toHaveCount(choices.items.length);
  await expect(dlg.getByTestId('apply-ports-count')).toHaveText(T('applyPorts.selectedOf', { n: preselected.length - 1, total: choices.items.length }));
  await dlg.getByLabel(T('applyPorts.removeUnselected')).check();
  await expect(dlg.getByTestId('apply-ports-remove-count')).toHaveText(T('applyPorts.removeCount', { count: 1 }));
  await shot13(page, '63-apply-choose-ports');

  await dlg.getByRole('button', { name: /^Apply/ }).click();
  const result = page.getByRole('dialog');
  await expect(result.getByTestId('apply-removed')).toBeVisible({ timeout: 120_000 });
  await expect(result.getByTestId('apply-removed')).toContainText(T('applyPorts.removedTitle', { count: 1 }));
  await expect(result.getByTestId('apply-removed')).toContainText('qa13 port 1');
  await shot13(page, '63-apply-result-removed');

  const sensors = await ifSensors(admin, dev.id);
  const idx = sensors.map((s) => s.config.if_index).sort((a: number, b: number) => a - b);
  expect(idx).toEqual(preselected.filter((i: number) => i !== 1).sort((a: number, b: number) => a - b));
  const at = (i: number) => sensors.find((s) => s.config.if_index === i);
  expect(at(9).impact, 'new sensor on a trunk: critical').toBe('critical');
  expect(at(2).impact, 'existing access-port sensor kept as it was').toBe('warning');
  // the audit row of the apply carries the removed sensor
  // (feature `audit` may be off in this run: then 409 and nothing to check)
  const audit = await raw(admin, 'GET', `/api/audit?action=device.apply_template&device_id=${dev.id}&page_size=5`);
  if (audit.status === 200) expect.soft(JSON.stringify(audit.body.items[0]?.changes ?? {}), 'audit changes.removed').toContain('qa13 port 1');
});

test('API: ifIndex list skips unknown / excluded interfaces; "none" + remove deletes every interface sensor', async () => {
  const r = await admin.post(`/api/devices/${dev.id}/apply-template`, { template_id: TPL, interfaces: [7, 12, 99] });
  const created = r.created.map((c: any) => c.name);
  expect(created).toContain('7');
  const sk = r.skipped.filter((s: any) => s.reason === 'not_applicable').map((s: any) => s.message ?? s.detail ?? s.name);
  expect(JSON.stringify(r.skipped)).toContain('Interface 99 not found');
  expect(JSON.stringify(r.skipped)).toContain('Excluded by the template');
  expect(sk.length).toBeGreaterThanOrEqual(2);
  expect(r.removed).toEqual([]);
  expect((await ifSensors(admin, dev.id)).find((s) => s.config.if_index === 7)?.impact, 'access port 7 -> warning').toBe('warning');

  const before = await ifSensors(admin, dev.id);
  const none = await admin.post(`/api/devices/${dev.id}/apply-template`, { template_id: TPL, interfaces: 'none', remove_unselected_interfaces: true });
  expect(none.removed.map((x: any) => x.sensor_id).sort()).toEqual(before.map((s) => s.id).sort());
  expect(none.created.filter((c: any) => c.type === 'snmp_interface')).toEqual([]);
  expect(await ifSensors(admin, dev.id)).toEqual([]);
  // validation: an unknown keyword is rejected
  expect((await raw(admin, 'POST', `/api/devices/${dev.id}/apply-template`, { template_id: TPL, interfaces: 'some' })).status).toBe(422);
});

test('API: default ("important") creates sensors on uplinks / LAG / trunks only', async () => {
  const r = await admin.post(`/api/devices/${dev.id}/apply-template`, { template_id: TPL });
  const idx = (await ifSensors(admin, dev.id)).map((s) => s.config.if_index).sort((a: number, b: number) => a - b);
  const imp = choices.items.filter((i: any) => i.important && i.oper_status === 'up').map((i: any) => i.if_index);
  for (const i of imp) expect(idx, `important up port ${i}`).toContain(i);
  for (const i of [1, 2, 3, 4, 7]) expect(idx, `access port ${i}`).not.toContain(i);
  expect(r.removed).toEqual([]);
});
