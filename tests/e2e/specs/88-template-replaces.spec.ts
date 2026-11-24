import { expect, test, type Browser, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { Api, apiAs, BASE_URL, loginViaApi, SCREENSHOT_DIR, uid } from '../lib/helpers';
import { ensureGroup, raw, snmpCreds } from '../lib/lab';
import { T } from '../lib/lab2b2';
import { features, setFeatures } from '../lib/lab3';

// [template-switch] 88 — apply template: the sensors the new template replaces (docs/api.md § "[template-switch]
// Apply template: sensors the template replaces", data-model.md § "Sensors a template replaces"). The owner's case:
// an HPE Comware switch (sim 127.0.0.52 "access-1", no HOST-RESOURCES-MIB) added with the default sensors, whose
// generic "CPU Load" / "Memory" fail forever; applying the "HPE Comware" template (replaces: [snmp_cpu, snmp_memory])
// lists them pre-ticked; applied → they are gone (sensor.delete audit rows), the template's own CPU / memory exist.
// Unticked → they stay. Screenshots (1440×900 @2x, en + he) to $TPL_SHOT_DIR.
test.describe.configure({ mode: 'serial' });

const HOST = '127.0.0.52';
const TPL = 'hpe_comware';
const SHOT_DIR = process.env.TPL_SHOT_DIR ?? join(SCREENSHOT_DIR, 'template-switch');
let api: Api;
let gid = 0;
let auditWas: boolean | undefined;
const devices: number[] = [];

type Dev = { id: number; cpu: any; mem: any };

/** A Comware switch with the default sensors; CPU Load / Memory checked once (they fail: no HOST-RESOURCES-MIB). */
async function comwareWithDefaults(prefix: string): Promise<Dev> {
  const d = await api.post('/api/devices', {
    group_id: gid,
    name: uid(prefix),
    host: HOST,
    snmp_credentials: snmpCreds(),
    auto_sensors: ['ping', 'snmp_system', 'snmp_cpu', 'snmp_memory'],
  });
  devices.push(d.id);
  const items = (await api.get(`/api/sensors?device_id=${d.id}&page_size=100`)).items;
  const cpu = items.find((s: any) => s.type === 'snmp_cpu');
  const mem = items.find((s: any) => s.type === 'snmp_memory');
  for (const s of [cpu, mem]) {
    const r = await raw(api, 'POST', `/api/sensors/${s.id}/run`);
    expect(r.status, r.text).toBe(200);
  }
  const now = async (id: number) => api.get(`/api/sensors/${id}`);
  await expect.poll(async () => (await now(mem.id)).status, { timeout: 30_000 }).toBe('down');
  await expect.poll(async () => (await now(cpu.id)).status, { timeout: 30_000 }).toBe('down');
  expect((await now(mem.id)).status_message).toContain('hrStorageRam not found');
  return { id: d.id, cpu: await now(cpu.id), mem: await now(mem.id) };
}

async function names(deviceId: number): Promise<string[]> {
  return (await api.get(`/api/sensors?device_id=${deviceId}&page_size=1000`)).items.map((s: any) => s.name);
}

async function openApply(page: Page, deviceId: number, lang: 'en' | 'he' = 'en') {
  await page.goto(`/devices/${deviceId}`);
  await expect(page.locator('html')).toHaveAttribute('dir', lang === 'he' ? 'rtl' : 'ltr');
  await page.getByRole('button', { name: T('templates.apply', {}, lang) }).click();
  const dlg = page.getByRole('dialog');
  await dlg.getByRole('combobox').first().selectOption(TPL);
  const section = dlg.getByTestId('apply-replaces');
  await expect(section).toBeVisible({ timeout: 30_000 });
  return { dlg, section };
}

/** Own context at 1440×900, deviceScaleFactor 2, signed in, in `lang`. */
async function shotPage(browser: Browser, lang: 'en' | 'he'): Promise<Page> {
  const ctx = await browser.newContext({ baseURL: BASE_URL, viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, locale: lang === 'he' ? 'he-IL' : 'en-US', timezoneId: 'UTC' });
  const page = await ctx.newPage();
  await loginViaApi(page, undefined, '/');
  const r = await page.request.patch('/api/auth/me', { data: { language: lang } });
  expect(r.status(), await r.text()).toBe(200);
  return page;
}

async function shot(page: Page, name: string) {
  mkdirSync(SHOT_DIR, { recursive: true });
  await page.waitForTimeout(400);
  await page.screenshot({ path: join(SHOT_DIR, `${name}.png`) });
}

test.beforeAll(async () => {
  api = await apiAs();
  gid = await ensureGroup(api, 'QA template switch');
  auditWas = (await features(api)).audit;
  await setFeatures(api, { audit: true });
});

test.afterAll(async () => {
  await api.patch('/api/auth/me', { language: 'en' }).catch(() => undefined);
  for (const id of devices) await raw(api, 'DELETE', `/api/devices/${id}`);
  if (auditWas === false) await setFeatures(api, { audit: false });
});

test('API: candidates, failing flag, a non-candidate id is rejected', async () => {
  const d = await comwareWithDefaults('qa-tpl-api');
  const r = await raw(api, 'GET', `/api/devices/${d.id}/apply-template/replaces?template_id=${TPL}`);
  expect(r.status, r.text).toBe(200);
  expect(r.body.items.map((i: any) => [i.sensor_id, i.type, i.reason, i.failing])).toEqual([
    [d.cpu.id, 'snmp_cpu', 'replaced', true],
    [d.mem.id, 'snmp_memory', 'replaced', true],
  ]);
  // the generic template makes them itself: nothing to replace
  const g = await raw(api, 'GET', `/api/devices/${d.id}/apply-template/replaces?template_id=generic_snmp`);
  expect(g.body.items).toEqual([]);
  const ping = (await api.get(`/api/sensors?device_id=${d.id}&page_size=100`)).items.find((s: any) => s.type === 'ping');
  const bad = await raw(api, 'POST', `/api/devices/${d.id}/apply-template`, { template_id: TPL, remove_sensor_ids: [ping.id] });
  expect(bad.status, bad.text).toBe(422);
  expect(await names(d.id), 'nothing applied').toEqual(expect.arrayContaining(['CPU Load', 'Memory']));
  expect((await names(d.id)).some((n) => n.startsWith('CPU Board'))).toBe(false);
});

test('UI: failing generic CPU / memory listed pre-ticked → apply → removed, audited, template sensors created', async ({ browser }) => {
  const d = await comwareWithDefaults('qa-tpl-ui');
  const page = await shotPage(browser, 'en');
  const { dlg, section } = await openApply(page, d.id);
  const rows = section.getByTestId('apply-replace-row');
  await expect(rows).toHaveCount(2);
  const box = (n: string) => section.getByRole('checkbox', { name: T('applyReplaces.rowLabel', { name: n }) });
  await expect(box('CPU Load')).toBeChecked();
  await expect(box('Memory')).toBeChecked();
  await expect(section.getByTestId('apply-replace-failing')).toHaveCount(2);
  await expect(rows.first()).toContainText(T('applyReplaces.reason.replaced'));
  await expect(section).toContainText('hrStorageRam not found');
  await expect(dlg.getByTestId('apply-replaces-count')).toHaveText(T('applyReplaces.deleteCount', { count: 2 }));
  await section.scrollIntoViewIfNeeded();
  await shot(page, 'apply-dialog-replaces--en');

  await dlg.getByRole('button', { name: /^Apply/ }).click();
  const done = page.getByRole('dialog').getByTestId('apply-replaced');
  await expect(done).toBeVisible({ timeout: 120_000 });
  await expect(done).toContainText(T('applyReplaces.resultTitle', { count: 2 }));
  await expect(done).toContainText('CPU Load');
  await expect(done).toContainText('Memory');
  await shot(page, 'apply-summary-replaced--en');
  await page.context().close();

  for (const s of [d.cpu, d.mem]) expect((await raw(api, 'GET', `/api/sensors/${s.id}`)).status, s.name).toBe(404);
  const left = await names(d.id);
  expect(left).not.toContain('CPU Load');
  expect(left).not.toContain('Memory');
  expect(left).toEqual(expect.arrayContaining(['Ping', 'System']));
  expect(left.some((n) => n.startsWith('CPU ')), `template CPU sensor in ${left}`).toBe(true);
  expect(left.some((n) => n.startsWith('Memory ')), `template memory sensor in ${left}`).toBe(true);
  const audit = await raw(api, 'GET', `/api/audit?action=sensor.delete&device_id=${d.id}&page_size=50`);
  expect(audit.status, audit.text).toBe(200);
  expect(audit.body.items.map((a: any) => a.entity_id).sort()).toEqual([d.cpu.id, d.mem.id].sort());
  const applied = await raw(api, 'GET', `/api/audit?action=device.apply_template&device_id=${d.id}&page_size=5`);
  expect(JSON.stringify(applied.body.items[0]?.changes?.replaced ?? [])).toContain(`CPU Load (#${d.cpu.id})`);
  // nothing left to offer
  const again = await raw(api, 'GET', `/api/devices/${d.id}/apply-template/replaces?template_id=${TPL}`);
  expect(again.body.items).toEqual([]);
});

test('UI (Hebrew): the section and the summary in RTL', async ({ browser }) => {
  const d = await comwareWithDefaults('qa-tpl-he');
  const page = await shotPage(browser, 'he');
  try {
    const { dlg, section } = await openApply(page, d.id, 'he');
    await expect(section).toContainText(T('applyReplaces.title', {}, 'he'));
    await expect(section.getByRole('checkbox', { name: T('applyReplaces.rowLabel', { name: 'CPU Load' }, 'he') })).toBeChecked();
    await expect(dlg.getByTestId('apply-replaces-count')).toHaveText('2 חיישנים יימחקו, עם ההיסטוריה שלהם.');
    await section.scrollIntoViewIfNeeded();
    await shot(page, 'apply-dialog-replaces--he');
    await dlg.getByRole('button', { name: T('templates.applyButton', {}, 'he') }).click();
    const done = page.getByRole('dialog').getByTestId('apply-replaced');
    await expect(done).toBeVisible({ timeout: 120_000 });
    await expect(done).toContainText('נמחקו 2 חיישנים שהתבנית מחליפה');
    await shot(page, 'apply-summary-replaced--he');
  } finally {
    await page.request.patch('/api/auth/me', { data: { language: 'en' } });
    await page.context().close();
  }
  expect(await names(d.id)).not.toContain('Memory');
});

test('UI: unticked → the generic sensors stay', async ({ page }) => {
  const d = await comwareWithDefaults('qa-tpl-keep');
  await loginViaApi(page, undefined, `/devices/${d.id}`);
  const { dlg, section } = await openApply(page, d.id);
  for (const n of ['CPU Load', 'Memory']) {
    const box = section.getByRole('checkbox', { name: T('applyReplaces.rowLabel', { name: n }) });
    await expect(box).toBeChecked();
    await box.uncheck();
  }
  await expect(dlg.getByTestId('apply-replaces-count')).toHaveText(T('applyReplaces.keepAll'));
  await dlg.getByRole('button', { name: /^Apply/ }).click();
  await expect(page.getByRole('dialog').getByTestId('apply-result')).toBeVisible({ timeout: 120_000 });
  await expect(page.getByRole('dialog').getByTestId('apply-replaced')).toHaveCount(0);
  const left = await names(d.id);
  expect(left).toEqual(expect.arrayContaining(['CPU Load', 'Memory']));
  expect(left.some((n) => n.startsWith('CPU ') && n !== 'CPU Load'), `template CPU sensor in ${left}`).toBe(true);
  for (const s of [d.cpu, d.mem]) expect((await raw(api, 'GET', `/api/sensors/${s.id}`)).status).toBe(200);
  const audit = await raw(api, 'GET', `/api/audit?action=sensor.delete&device_id=${d.id}&page_size=50`);
  expect(audit.body.items).toEqual([]);
});
