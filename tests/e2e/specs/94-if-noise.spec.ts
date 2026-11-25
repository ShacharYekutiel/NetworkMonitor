import { expect, test, type Browser, type Page } from '@playwright/test';
import type { ChildProcess } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { Api, apiAs, BASE_URL, loginViaApi, SCREENSHOT_DIR, uid, waitForSensor } from '../lib/helpers';
import { ensureGroup, raw, snmpCreds, startHttpHost, stopHost } from '../lib/lab';
import { IP, T, openPorts } from '../lib/lab2b2';
import { setClock, resetClock } from '../lib/lab2b';
import { freshSwitch, ifSensors } from '../lib/lab13';

// [if-noise] 94 — interface sensor event noise (docs/api.md "[if-noise]", data-model.md "[if-noise]"):
// defaults ON; a one-poll blip leaves no event-log row while the sensor's status is real time and the rows are
// still stored (noise=include); a change confirmed by the next check is shown with its real time; a flapping
// sensor collapses into one "flapping" event with a count that settles; the Events page's "Show hidden changes"
// and "Noisiest sensors"; the Settings cards (switches off restore the old behaviour); the important-port
// criteria and the per-port importance in the port card; the apply-template port filter with its preview; the
// device's SNMP source line names the group / warns about the built-in default. Hebrew + English screenshots
// (1440×900 @2x) to $NOISE_SHOT_DIR.
test.describe.configure({ mode: 'serial' });

const SHOT_DIR = process.env.NOISE_SHOT_DIR ?? join(SCREENSHOT_DIR, 'if-noise');
const HOST = '127.0.0.94';
const PORT = 18094;
const TPL = 'hpe_arubaos_switch';
const DEFAULTS = {
  sensor_event_debounce_enabled: true,
  sensor_event_debounce_polls: 1,
  sensor_event_debounce_min_s: 0,
  sensor_event_flap_enabled: true,
  flap_threshold: 5,
  flap_window_min: 10,
  flap_quiet_min: 15,
  important_port_criteria: ['uplink', 'lag', 'trunk', 'irf'],
  important_port_description_pattern: '',
};

let api: Api;
let target: ChildProcess | null = null;
let dev: any;
let sid = 0;
let sw: any;
const created: number[] = [];

async function setTarget(ok: boolean): Promise<void> {
  const r = await fetch(`http://${HOST}:${PORT}/${ok ? 'ok' : 'fail'}`, { method: 'POST' });
  expect(r.status).toBe(200);
}
/** One check now (POST /run); returns the sensor afterwards. */
async function check(): Promise<any> {
  await api.post(`/api/sensors/${sid}/run`);
  return api.get(`/api/sensors/${sid}`);
}
async function events(noise?: 'include' | 'only'): Promise<any[]> {
  const q = `/api/events?sensor_id=${sid}&kind=status_change,sensor_flapping&page_size=200${noise ? `&noise=${noise}` : ''}`;
  return (await api.get(q)).items;
}
const trans = (items: any[]) => items.map((e) => `${e.kind === 'sensor_flapping' ? 'flap' : ''}${e.old_status ?? ''}>${e.new_status}${e.noise ? `:${e.noise}` : ''}`).reverse();

async function shotPage(browser: Browser, lang: 'en' | 'he', path: string): Promise<Page> {
  const ctx = await browser.newContext({ baseURL: BASE_URL, viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, locale: lang === 'he' ? 'he-IL' : 'en-US', timezoneId: 'UTC' });
  const page = await ctx.newPage();
  await loginViaApi(page, undefined, '/');
  const r = await page.request.patch('/api/auth/me', { data: { language: lang } });
  expect(r.status(), await r.text()).toBe(200);
  await page.goto(path);
  await expect(page.locator('html')).toHaveAttribute('dir', lang === 'he' ? 'rtl' : 'ltr');
  return page;
}
async function shot(page: Page, name: string, el?: ReturnType<Page['locator']>) {
  mkdirSync(SHOT_DIR, { recursive: true });
  await page.waitForTimeout(400);
  if (el) await el.screenshot({ path: join(SHOT_DIR, `${name}.png`) });
  else await page.screenshot({ path: join(SHOT_DIR, `${name}.png`) });
}

test.beforeAll(async () => {
  api = await apiAs();
  await api.put('/api/settings', DEFAULTS);
  target = await startHttpHost(HOST, PORT);
  const gid = await ensureGroup(api, 'QA if-noise');
  dev = await api.post('/api/devices', { group_id: gid, name: uid('qa94-pc'), host: HOST, auto_sensors: [] });
  created.push(dev.id);
  const s = await api.post('/api/sensors', { device_id: dev.id, type: 'http', name: 'qa94 access port', interval_s: 60, config: { url: `http://${HOST}:${PORT}/` } });
  sid = s.id;
  // the scheduler's first check (the next one is 60 s later): the run-now checks below are the only ones
  await waitForSensor(api, sid, (x) => x.last_check_at !== null && x.status === 'up', 60_000);
});

test.afterAll(async () => {
  await resetClock(api).catch(() => undefined);
  await api.put('/api/settings', DEFAULTS).catch(() => undefined);
  await api.patch('/api/auth/me', { language: 'en' }).catch(() => undefined);
  for (const id of created) await raw(api, 'DELETE', `/api/devices/${id}`);
  stopHost(target);
});

test('defaults ON after install; validation', async () => {
  const s = await api.get('/api/settings');
  expect(s).toMatchObject(DEFAULTS);
  expect((await raw(api, 'PUT', '/api/settings', { sensor_event_debounce_polls: 0 })).status).toBe(422);
  expect((await raw(api, 'PUT', '/api/settings', { important_port_criteria: ['nope'] })).status).toBe(422);
  expect((await raw(api, 'GET', '/api/events?noise=bogus')).status).toBe(422);
});

test('debounce: a one-poll blip leaves no row (status is real time); a confirmed change is shown', async () => {
  expect((await check()).status).toBe('up');
  await setTarget(false);
  const down = await check();
  expect(down.status, 'current status is real time').toBe('down');
  expect(trans(await events())).toEqual(['unknown>up']);
  expect(trans(await events('include'))).toEqual(['unknown>up', 'up>down:pending']);
  await setTarget(true);
  expect((await check()).status).toBe('up');
  expect(trans(await events()), 'the blip is not in the event log').toEqual(['unknown>up']);
  expect(trans(await events('include'))).toEqual(['unknown>up', 'up>down:debounced', 'down>up:debounced']);
  // a change that holds: shown once the next check confirms it, with its real time
  await setTarget(false);
  await check();
  const pending = (await events('include')).find((e) => e.noise === 'pending');
  expect(pending).toBeTruthy();
  await check();
  const shown = await events();
  expect(trans(shown)).toEqual(['unknown>up', 'up>down']);
  expect(shown[0].time).toBe(pending.time);
  await setTarget(true);
  await check();
  await check();
  expect(trans(await events())).toEqual(['unknown>up', 'up>down', 'down>up']);
});

test('flapping: further changes collapse into one "flapping" event with a count; it settles', async ({ browser }) => {
  // 4 changes so far within the 10-minute window; the 5th starts the episode
  for (let i = 0; i < 5; i++) {
    await setTarget(i % 2 === 1);
    await check();
  }
  const flap = (await events()).find((e) => e.kind === 'sensor_flapping');
  expect(flap).toMatchObject({ flap_end: null, new_status: 'down', noise: null });
  expect(flap.flap_count).toBeGreaterThanOrEqual(7);
  expect(trans(await events())).toEqual(['unknown>up', 'up>down', 'down>up', 'flap>down']);
  const noisy = await api.get('/api/events/noisy-sensors?hours=24&limit=100');
  const mine = noisy.items.find((x: any) => x.sensor_id === sid);
  expect(mine).toMatchObject({ flapping: true, shown: 3 });
  expect(mine.changes).toBeGreaterThanOrEqual(10);

  // UI: the flapping row, "Show hidden changes" and "Noisiest sensors" (en + he)
  for (const lang of ['en', 'he'] as const) {
    const page = await shotPage(browser, lang, `/events?sensor_id=${sid}`);
    const flapTag = page.getByTestId('flap-tag').first();
    await expect(flapTag).toHaveText(T('noise.flapTitle', { count: flap.flap_count }, lang));
    await expect(page.getByTestId('noise-tag')).toHaveCount(0);
    await shot(page, `94-events-flapping-${lang}`);
    await page.getByLabel(T('noise.showHidden', {}, lang)).check();
    await expect(page.getByTestId('noise-tag').first()).toBeVisible();
    await expect(page.getByTestId('noise-tag').filter({ hasText: T('noise.hidden.debounced', {}, lang) }).first()).toBeVisible();
    await shot(page, `94-events-hidden-shown-${lang}`);
    await page.getByLabel(T('noisy.show', {}, lang)).check();
    const card = page.getByTestId('noisy-sensors');
    await expect(card.getByTestId('noisy-row').first()).toBeVisible();
    await shot(page, `94-noisiest-sensors-${lang}`, card.locator('xpath=ancestor::section[1]'));
    await page.context().close();
  }

  // quiet for flap_quiet_min (test clock +16 min): the episode ends, the log ends in the real status (down)
  await setClock(api, 16 * 60);
  await expect.poll(async () => (await events()).find((e) => e.kind === 'sensor_flapping')?.flap_end ?? null, { timeout: 20_000 }).not.toBeNull();
  await resetClock(api);
  const last = (await events())[0];
  expect(last).toMatchObject({ kind: 'status_change', new_status: 'down' });
});

test('switches OFF restore the previous behaviour (every change is a row at once)', async () => {
  await api.put('/api/settings', { sensor_event_debounce_enabled: false, sensor_event_flap_enabled: false });
  const before = (await events()).length;
  await setTarget(true);
  await check();
  await setTarget(false);
  await check();
  const after = await events();
  expect(after.length).toBe(before + 2);
  expect(after.slice(0, 2).every((e: any) => e.noise === null)).toBe(true);
  await api.put('/api/settings', DEFAULTS);
});

test('Settings: sensor event noise and important ports cards (en + he); saving the switch', async ({ browser }) => {
  for (const lang of ['en', 'he'] as const) {
    const page = await shotPage(browser, lang, '/settings');
    const noise = page.getByTestId('sensor-noise-settings');
    await expect(noise).toBeVisible();
    await expect(noise.getByLabel(T('noise.debounce', {}, lang))).toBeChecked();
    await expect(noise.getByLabel(T('noise.flap', {}, lang))).toBeChecked();
    await noise.scrollIntoViewIfNeeded();
    await shot(page, `94-settings-noise-${lang}`, noise.locator('xpath=ancestor::section[1]'));
    const imp = page.getByTestId('important-ports-settings');
    await expect(imp.getByTestId('important-legacy-notice')).toHaveCount(0); // a fresh install
    await shot(page, `94-settings-important-ports-${lang}`, imp.locator('xpath=ancestor::section[1]'));
    await page.context().close();
  }
  // the upgrade notice while the pre-upgrade meaning is on
  await api.put('/api/settings', { important_port_criteria: ['uplink', 'lag', 'trunk', 'irf', 'critical_sensor'] });
  for (const lang of ['en', 'he'] as const) {
    const page = await shotPage(browser, lang, '/settings');
    const imp = page.getByTestId('important-ports-settings');
    await expect(imp.getByTestId('important-legacy-notice')).toBeVisible();
    await shot(page, `94-settings-important-legacy-${lang}`, imp.locator('xpath=ancestor::section[1]'));
    await page.context().close();
  }
  await api.put('/api/settings', DEFAULTS);
  // turning the debounce off in the UI is saved
  const page = await shotPage(browser, 'en', '/settings');
  const noise = page.getByTestId('sensor-noise-settings');
  await noise.getByLabel(T('noise.debounce')).uncheck();
  await expect(noise.getByTestId('debounce-polls')).toBeDisabled();
  await noise.getByRole('button', { name: T('common.save') }).click();
  await expect.poll(async () => (await api.get('/api/settings')).sensor_event_debounce_enabled).toBe(false);
  await api.put('/api/settings', DEFAULTS);
  await page.context().close();
});

test('apply template: filter out access ports, preview, only the chosen ports get sensors', async ({ browser }) => {
  sw = await freshSwitch(api, IP.aos, 'qa94-sw');
  created.push(sw.id);
  const list = (await api.get(`/api/devices/${sw.id}/apply-template/interfaces?template_id=${TPL}`)).items;
  expect(list.some((i: any) => i.mode === 'access')).toBe(true);
  const trunks = list.filter((i: any) => i.mode === 'trunk').map((i: any) => i.if_index);
  expect(trunks.length).toBeGreaterThan(0);
  for (const lang of ['he', 'en'] as const) {
    const page = await shotPage(browser, lang, `/devices/${sw.id}`);
    await page.getByRole('button', { name: T('templates.apply', {}, lang) }).click();
    const dlg = page.getByRole('dialog');
    await dlg.getByRole('combobox').first().selectOption(TPL);
    const ports = dlg.getByTestId('apply-ports');
    await expect(ports.getByTestId('apply-ports-preview')).toBeVisible({ timeout: 60_000 });
    const imp = list.filter((i: any) => i.important).length;
    await expect(ports.getByTestId('apply-ports-preview')).toHaveText(T('applyFilter.preview', { will: imp, wont: list.length - imp }, lang));
    await ports.getByRole('radio', { name: T('applyPorts.choice.choose', {}, lang) }).check();
    await expect(dlg.getByTestId('apply-ports-list')).toBeVisible({ timeout: 60_000 });
    await dlg.getByTestId('apply-ports-filter-toggle').click();
    await dlg.getByTestId('filter-mode').selectOption('trunk');
    await dlg.getByTestId('filter-apply').click();
    await expect(dlg.getByTestId('apply-ports-count')).toHaveText(T('applyPorts.selectedOf', { n: trunks.length, total: list.length }, lang));
    await expect(dlg.getByTestId('apply-ports-preview')).toHaveText(T('applyFilter.preview', { will: trunks.length, wont: list.length - trunks.length }, lang));
    await shot(page, `94-apply-filter-${lang}`, dlg);
    if (lang === 'en') {
      await dlg.getByRole('button', { name: /^Apply/ }).click();
      await expect.poll(async () => (await ifSensors(api, sw.id)).length, { timeout: 120_000 }).toBeGreaterThan(0);
      const idx = (await ifSensors(api, sw.id)).map((s) => s.config.if_index).sort((a: number, b: number) => a - b);
      expect(idx).toEqual([...trunks].sort((a: number, b: number) => a - b));
    }
    await page.context().close();
  }
});

test('port card: mark a port important / not important (never sent to the device)', async ({ browser }) => {
  const portsBefore = (await api.get(`/api/devices/${sw.id}/ports`)).ports;
  const access = portsBefore.find((p: any) => p.mode === 'access' && !p.important);
  expect(access).toBeTruthy();
  for (const lang of ['en', 'he'] as const) {
    const page = await shotPage(browser, lang, '/');
    await openPorts(page, sw.id, 'list', access.if_index);
    const modal = page.getByTestId('port-modal');
    const chooser = modal.getByTestId('importance-chooser');
    await expect(chooser).toBeVisible();
    // en first: automatic; he afterwards shows the choice made in en
    const want = lang === 'en' ? T('portImportance.auto') : T('portImportance.important', {}, 'he');
    await expect(chooser.getByRole('radio', { name: want, exact: true })).toHaveAttribute('aria-checked', 'true');
    if (lang === 'en') {
      await chooser.getByRole('radio', { name: T('portImportance.important'), exact: true }).click();
      await expect(chooser.getByRole('radio', { name: T('portImportance.important'), exact: true })).toHaveAttribute('aria-checked', 'true');
      const p = (await api.get(`/api/devices/${sw.id}/ports`)).ports.find((x: any) => x.if_index === access.if_index);
      expect(p).toMatchObject({ important: true, importance_override: true, important_reasons: ['marked'] });
    }
    await chooser.scrollIntoViewIfNeeded();
    await shot(page, `94-port-importance-${lang}`, chooser);
    await page.context().close();
  }
  await api.put(`/api/devices/${sw.id}/ports/${access.if_index}/importance`, { important: null });
});

test('device page: the SNMP source names the group (link); the built-in default with SNMP sensors warns', async ({ browser }) => {
  const parent = await api.post('/api/groups', { name: uid('qa94-snmp-group'), parent_id: null, snmp_credentials: snmpCreds() });
  const inGroup = await api.post('/api/devices', { group_id: parent.id, name: uid('qa94-inherit'), host: '127.0.0.98', auto_sensors: [] });
  created.push(inGroup.id);
  const bare = await api.post('/api/groups', { name: uid('qa94-no-snmp'), parent_id: null });
  const plain = await api.post('/api/devices', { group_id: bare.id, name: uid('qa94-default'), host: '127.0.0.99', auto_sensors: [] });
  created.push(plain.id);
  expect((await api.get(`/api/devices/${plain.id}`)).effective_snmp_source).toBe('default');
  await api.post('/api/sensors', { device_id: plain.id, type: 'snmp_interface', name: 'qa94 if', interval_s: 300, config: { if_index: 1, if_name: '1' } });
  for (const lang of ['en', 'he'] as const) {
    let page = await shotPage(browser, lang, `/devices/${inGroup.id}`);
    const link = page.getByTestId('snmp-source-group');
    await expect(link).toHaveText(parent.name);
    await expect(link).toHaveAttribute('href', `/groups/${parent.id}`);
    await shot(page, `94-snmp-source-group-${lang}`, page.getByTestId('snmp-source'));
    await page.context().close();
    page = await shotPage(browser, lang, `/devices/${plain.id}`);
    const warn = page.getByTestId('snmp-source');
    await expect(warn).toHaveAttribute('data-source', 'default');
    await expect(warn).toContainText(T('snmp.defaultWarning', {}, lang));
    await shot(page, `94-snmp-source-default-warning-${lang}`, warn);
    await page.context().close();
  }
  await raw(api, 'DELETE', `/api/devices/${inGroup.id}`);
  await raw(api, 'DELETE', `/api/devices/${plain.id}`);
  await raw(api, 'DELETE', `/api/groups/${parent.id}`);
  await raw(api, 'DELETE', `/api/groups/${bare.id}`);
});

// [if-noise] per-sensor / per-device / per-group override of the two protections ("always give the user the choice"):
// inherit / on / off at each level, the effective value and its source on the sensor / device / group pages, the
// sensor and device forms, the "Noisiest sensors" quick action, the sensor bulk action, and an override change
// releasing at once what a protection that is now OFF was holding back. Screenshots prefixed `override-`.
test('overrides: sensor / device / group, quick action, bulk; a change releases at once', async ({ browser }) => {
  test.setTimeout(600_000);
  await api.put('/api/settings', DEFAULTS);
  const gid = await ensureGroup(api, 'QA if-noise');
  const grp = await api.post('/api/groups', { name: uid('qa94-ov-group'), parent_id: gid });
  const dev2 = await api.post('/api/devices', { group_id: grp.id, name: uid('qa94-ov-dev'), host: HOST, auto_sensors: [] });
  created.push(dev2.id);
  const mk = (name: string) => api.post('/api/sensors', { device_id: dev2.id, type: 'http', name, interval_s: 60, config: { url: `http://${HOST}:${PORT}/` } });
  const s2 = await mk('qa94 ov port');
  const s3 = await mk('qa94 ov uplink');
  expect(s2.event_noise).toEqual({ debounce: 'inherit', flap: 'inherit', debounce_polls: null, debounce_min_s: null });
  expect(s2.effective_event_noise.debounce).toMatchObject({ value: true, source: 'global' });
  await setTarget(true);
  await waitForSensor(api, s2.id, (x) => x.last_check_at !== null && x.status === 'up', 60_000);
  const run2 = async () => {
    await api.post(`/api/sensors/${s2.id}/run`);
  };
  const ev2 = async (noise?: 'include') =>
    trans((await api.get(`/api/events?sensor_id=${s2.id}&kind=status_change,sensor_flapping&page_size=200${noise ? `&noise=${noise}` : ''}`)).items);

  // 1) sensor form: debounce OFF for this sensor only (the global switch stays ON)
  for (const lang of ['en', 'he'] as const) {
    const page = await shotPage(browser, lang, `/sensors/${s2.id}`);
    await expect(page.getByTestId('noise-override-card')).toHaveCount(0); // everything from Settings
    await page.getByRole('button', { name: T('common.edit', {}, lang) }).click();
    const dlg = page.getByRole('dialog');
    const fields = dlg.getByTestId('noise-override');
    await expect(fields).toBeVisible();
    await expect(fields).toContainText(T('noiseOv.now', { value: T('noiseOv.value.on', {}, lang), source: T('noiseOv.source.global', {}, lang) }, lang));
    await dlg.getByTestId('noise-override-debounce').selectOption('off');
    await fields.scrollIntoViewIfNeeded();
    await shot(page, `override-sensor-form-${lang}`, dlg);
    if (lang === 'en') {
      await dlg.getByRole('button', { name: T('common.save') }).click();
      await expect(dlg).toBeHidden();
    } else {
      await dlg.getByRole('button', { name: T('common.cancel', {}, 'he') }).click();
    }
    await page.context().close();
  }
  expect((await api.get(`/api/sensors/${s2.id}`)).event_noise.debounce).toBe('off');
  await setTarget(false);
  await run2();
  expect(await ev2('include'), 'shown at once, never pending').toEqual(['unknown>up', 'up>down']);
  await setTarget(true);
  await run2();
  expect(await ev2(), 'a blip is a row each, as without the protection').toEqual(['unknown>up', 'up>down', 'down>up']);
  // the neighbour without an override is still protected
  await waitForSensor(api, s3.id, (x) => x.last_check_at !== null, 60_000);
  for (const lang of ['en', 'he'] as const) {
    const page = await shotPage(browser, lang, `/sensors/${s2.id}`);
    const card = page.getByTestId('noise-override-card');
    await expect(card.getByTestId('noise-override-debounce')).toContainText(T('noiseOv.value.off', {}, lang));
    await expect(card.getByTestId('noise-override-debounce')).toContainText(T('noiseOv.source.self.sensor', {}, lang));
    await expect(card.getByTestId('noise-override-flap')).toContainText(T('noiseOv.source.global', {}, lang));
    await card.scrollIntoViewIfNeeded();
    await shot(page, `override-sensor-page-${lang}`, card.locator('xpath=ancestor::section[1]'));
    await page.context().close();
  }

  // 2) device form: flapping collapse OFF for the device; its sensors inherit it, the page shows where from
  {
    const page = await shotPage(browser, 'en', `/devices/${dev2.id}`);
    await page.getByRole('button', { name: T('common.edit') }).first().click();
    const dlg = page.getByRole('dialog');
    await dlg.getByTestId('noise-override-flap').selectOption('off');
    await dlg.getByTestId('noise-override-min-s').fill('120');
    await dlg.getByRole('button', { name: T('common.save') }).click();
    await expect(dlg).toBeHidden();
    await page.context().close();
  }
  const d2 = await api.get(`/api/devices/${dev2.id}`);
  expect(d2.event_noise).toMatchObject({ flap: 'off', debounce_min_s: 120 });
  const e3 = (await api.get(`/api/sensors/${s3.id}`)).effective_event_noise;
  expect(e3.flap).toMatchObject({ value: false, source: 'device', source_id: dev2.id });
  expect(e3.debounce_min_s).toMatchObject({ value: 120, source: 'device' });
  for (const lang of ['en', 'he'] as const) {
    const page = await shotPage(browser, lang, `/devices/${dev2.id}`);
    const card = page.getByTestId('noise-override-card');
    await expect(card.getByTestId('noise-override-flap')).toContainText(T('noiseOv.source.self.device', {}, lang));
    await shot(page, `override-device-page-${lang}`, card.locator('xpath=ancestor::section[1]'));
    await page.getByTestId('noise-override-edit').click();
    const dlg = page.getByRole('dialog');
    const fields = dlg.getByTestId('noise-override');
    await fields.scrollIntoViewIfNeeded();
    await shot(page, `override-device-form-${lang}`, fields);
    await dlg.getByRole('button', { name: T('common.cancel', {}, lang) }).click();
    // the sensor page names the device as the source
    await page.goto(`/sensors/${s3.id}`);
    await expect(page.getByTestId('noise-override-flap')).toContainText(T('noiseOv.source.device', { name: `\u2068${dev2.name}\u2069` }, lang));
    await page.context().close();
  }

  // 3) group level: debounce OFF on the group reaches the sensor that inherits
  await api.patch(`/api/groups/${grp.id}`, { event_noise: { debounce: 'off' } });
  expect((await api.get(`/api/sensors/${s3.id}`)).effective_event_noise.debounce).toMatchObject({ value: false, source: 'group', source_id: grp.id });
  for (const lang of ['en', 'he'] as const) {
    const page = await shotPage(browser, lang, `/groups/${grp.id}`);
    const card = page.getByTestId('noise-override-card');
    await expect(card.getByTestId('noise-override-debounce')).toContainText(T('noiseOv.source.self.group', {}, lang));
    await shot(page, `override-group-page-${lang}`, card.locator('xpath=ancestor::section[1]'));
    await page.context().close();
  }
  await api.patch(`/api/groups/${grp.id}`, { event_noise: { debounce: 'inherit' } });

  // 4) "Noisiest sensors": quick action sets the sensor back to inherit (debounce ON again via Settings)
  for (const lang of ['en', 'he'] as const) {
    const page = await shotPage(browser, lang, `/events?sensor_id=${s2.id}`);
    await page.getByLabel(T('noisy.show', {}, lang)).check();
    const card = page.getByTestId('noisy-sensors');
    const row = card.getByTestId('noisy-row').filter({ hasText: 'qa94 ov port' });
    await expect(row.getByTestId('noise-override-tag')).toBeVisible();
    await shot(page, `override-noisiest-${lang}`, card.locator('xpath=ancestor::section[1]'));
    await row.getByTestId('noisy-protection').click();
    const dlg = page.getByTestId('sensor-noise-modal');
    await expect(dlg.getByTestId('noise-override-debounce')).toHaveValue('off');
    await shot(page, `override-quick-action-${lang}`, dlg);
    if (lang === 'he') {
      await dlg.getByTestId('noise-override-debounce').selectOption('inherit');
      await dlg.getByRole('button', { name: T('common.save', {}, 'he') }).click();
      await expect(dlg).toBeHidden();
      await expect(row.getByTestId('noise-override-tag')).toHaveCount(0);
    } else {
      await dlg.getByRole('button', { name: T('common.cancel') }).click();
    }
    await page.context().close();
  }
  expect((await api.get(`/api/sensors/${s2.id}`)).effective_event_noise.debounce).toMatchObject({ value: true, source: 'global' });

  // 5) immediate release: a pending change is shown as soon as the sensor's debounce is set OFF
  await setTarget(false);
  await run2();
  expect((await ev2('include')).slice(-1)).toEqual(['up>down:pending']);
  await api.patch(`/api/sensors/${s2.id}`, { event_noise: { debounce: 'off' } });
  expect((await ev2()).slice(-1), 'shown without waiting for another check').toEqual(['up>down']);
  await setTarget(true);
  await run2();

  // 6) bulk: the sensor bulk bar of the device page
  for (const lang of ['en', 'he'] as const) {
    const page = await shotPage(browser, lang, `/devices/${dev2.id}`);
    await page.getByTestId('sensor-select-all').check();
    await page.getByTestId('sensor-bulk-noise').click();
    const dlg = page.getByTestId('bulk-noise');
    await dlg.getByTestId('bulk-noise-debounce').selectOption('inherit');
    await dlg.getByTestId('bulk-noise-flap').selectOption('on');
    await shot(page, `override-bulk-${lang}`, dlg);
    if (lang === 'en') {
      await dlg.getByRole('button', { name: T('impact.apply') }).click();
      await expect(dlg).toBeHidden();
    } else {
      await dlg.getByRole('button', { name: T('common.cancel', {}, 'he') }).click();
    }
    await page.context().close();
  }
  for (const s of [s2, s3]) {
    const x = await api.get(`/api/sensors/${s.id}`);
    expect(x.event_noise).toMatchObject({ debounce: 'inherit', flap: 'on' });
    expect(x.effective_event_noise.flap).toMatchObject({ value: true, source: 'sensor' });
  }
  await raw(api, 'DELETE', `/api/devices/${dev2.id}`);
  await raw(api, 'DELETE', `/api/groups/${grp.id}`);
});
