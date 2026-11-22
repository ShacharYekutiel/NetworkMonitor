import { expect, test, type Browser, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import {
  Api,
  HTTP_TARGET_URL,
  MIN_INTERVAL,
  SCREENSHOT_DIR,
  WEBHOOK_URL,
  apiAs,
  defaultGroupId,
  expectSameDocument,
  loginViaApi,
  markDocument,
  uid,
  waitForSensor,
  webhookRecords,
} from '../lib/helpers';
import { raw } from '../lib/lab';
import { T, setLang } from '../lib/lab2b2';
import { PHASE3B, ensureAssistant } from '../lib/lab3b';

// [severity] 86 — alert severity (api.md "[severity] Alert severity"; feature alert_severity, OFF by default).
// OFF: no severity UI on Home / Events / the sensor form. ON (switched in Settings → Features): a down sensor
// with impact critical is Critical on Home and Events; a mapping change in Settings re-classifies the active
// alert live while the old event keeps its severity; a sensor override wins; an alert rule limited to
// Critical does not notify for a Warning-level alert (another rule for every severity does); Hebrew labels.
// Own device and HTTP sensors whose keyword never matches (always down, independent of the toggle target).
test.describe.configure({ mode: 'serial' });

let admin: Api;
let deviceId = 0;
let deviceName = '';
const A = uid('qa-sev A');
const C = uid('qa-sev C');
const B = uid('qa-sev B');
let aId = 0;
let cId = 0;
let bId = 0;
const hooks: number[] = [];
const rules: number[] = [];
const port = new URL(HTTP_TARGET_URL).port;
const SHOTS = process.env.SEV_SHOT_DIR ?? join(SCREENSHOT_DIR, 'severity');

/** An HTTP sensor that is always down (the keyword is never in the page). */
async function deadSensor(name: string, extra: Record<string, unknown> = {}): Promise<any> {
  return admin.post('/api/sensors', {
    device_id: deviceId,
    name,
    type: 'http',
    interval_s: MIN_INTERVAL,
    impact: 'critical',
    config: { url: `http://{host}:${port}/`, keyword: 'qa-severity-never-present' },
    ...extra,
  });
}
async function setSeverityFeature(on: boolean) {
  const r = await raw(admin, 'PUT', '/api/settings', { feature_alert_severity_enabled: on });
  expect(r.status, r.text).toBe(200);
}
const problemRow = (page: Page, name: string) =>
  page.getByTestId('problem-row').filter({ has: page.getByRole('link', { name, exact: true }) });

test.beforeAll(async () => {
  admin = await apiAs();
  await setSeverityFeature(false);
  await raw(admin, 'POST', '/api/settings/severity-mapping/reset');
  deviceName = uid('qa-sev-dev');
  const d = await admin.post('/api/devices', {
    group_id: await defaultGroupId(admin),
    name: deviceName,
    host: '127.0.0.1',
    auto_sensors: [],
  });
  deviceId = d.id;
  aId = (await deadSensor(A)).id;
  await waitForSensor(admin, aId, (s) => s.status === 'down', 60_000, 'A down');
});

test.afterAll(async () => {
  for (const r of rules) await raw(admin, 'DELETE', `/api/alert-rules/${r}`);
  for (const h of hooks) await raw(admin, 'DELETE', `/api/notification-channels/${h}`);
  if (deviceId) await raw(admin, 'DELETE', `/api/devices/${deviceId}`);
  await raw(admin, 'POST', '/api/settings/severity-mapping/reset');
  await setSeverityFeature(false);
  await raw(admin, 'PATCH', '/api/auth/me', { language: 'en' });
});

test('feature OFF: no severity UI on Home, Events or the sensor form; API fields null', async ({ page }) => {
  const a = await admin.get(`/api/sensors/${aId}`);
  expect(a.severity).toBeNull();
  const dash = await admin.get('/api/dashboard');
  expect(dash.severity_counts).toBeNull();
  expect(dash.severity_counts_unacknowledged).toBeNull();

  await loginViaApi(page, undefined, '/');
  await expect(problemRow(page, A)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId('severity-counts')).toHaveCount(0);
  await expect(page.getByTestId('severity-tag')).toHaveCount(0);
  await shotThemes(page, 'home-off', { only: ['light-en'] });

  await page.goto(`/events?sensor_id=${aId}`);
  await expect(page.getByTestId('event-entry').first()).toBeVisible();
  await expect(page.getByTestId('event-severity-bar')).toHaveCount(0);
  await expect(page.getByTestId('severity-tag')).toHaveCount(0);
  expect(page.url()).not.toContain('severity');

  await page.goto(`/sensors/${aId}`);
  await expect(page.getByTestId('sensor-severity')).toHaveCount(0);
  await page.getByRole('button', { name: T('common.edit'), exact: true }).first().click();
  await expect(page.getByTestId('impact-field')).toBeVisible();
  await expect(page.getByTestId('severity-overrides')).toHaveCount(0);
  await expect(page.getByText(T('severity.whenDown'))).toHaveCount(0);
});

test('switch ON in Settings → Features: active alerts are classified; Critical on Home and Events', async ({ page }) => {
  await loginViaApi(page, undefined, '/settings?tab=features');
  const row = page.locator('[data-feature="alert_severity"]');
  await expect(row).toContainText(T('features.desc.alert_severity').slice(0, 40));
  await row.getByRole('switch', { name: T('features.name.alert_severity') }).click();
  await expect.poll(async () => (await admin.get('/api/settings/public')).features.alert_severity, { timeout: 10_000 }).toBe(true);
  // turning it on resolves the alert that was already active (its opening event stays without one)
  await expect.poll(async () => (await admin.get(`/api/sensors/${aId}`)).severity, { timeout: 15_000 }).toBe('critical');
  const old = await admin.get(`/api/events?sensor_id=${aId}&kind=status_change&page_size=5`);
  expect(old.items.find((e: any) => e.new_status === 'down')?.severity ?? null).toBeNull();
  // the mapping card is on the General tab now
  await page.getByRole('tab', { name: T('settings.tab.general') }).click();
  await expect(page.getByTestId('severity-mapping-card')).toBeVisible();

  // a new alert while ON: its event carries the severity
  cId = (await deadSensor(C)).id;
  await waitForSensor(admin, cId, (s) => s.status === 'down' && s.severity === 'critical', 60_000, 'C down, critical');
  await page.goto('/');
  await expect(problemRow(page, A).getByTestId('severity-tag')).toHaveText(T('severity.level.critical'));
  await expect(problemRow(page, C).getByTestId('severity-tag')).toHaveText(T('severity.level.critical'));
  const chip = page.getByTestId('severity-counts').locator('[data-testid="severity-count"][data-severity="critical"]');
  await expect(chip).toBeVisible();
  const n = (await admin.get('/api/dashboard')).severity_counts_unacknowledged.critical;
  expect(n).toBeGreaterThanOrEqual(2);
  await expect(chip).toContainText(String(n));
  // the chip filters the list
  await chip.click();
  await expect(chip).toHaveAttribute('aria-pressed', 'true');
  await expect(page).toHaveURL(/severity=critical/);
  await expect(problemRow(page, C)).toBeVisible();
  await expect(page.getByTestId('problem-row').getByTestId('severity-tag').filter({ hasNotText: T('severity.level.critical') })).toHaveCount(0);

  await page.goto(`/events?sensor_id=${cId}`);
  const bar = page.getByTestId('event-severity-bar');
  await expect(bar).toBeVisible();
  const downEntry = page.getByTestId('event-entry').filter({ hasText: C }).first();
  await expect(downEntry.getByTestId('severity-tag')).toHaveText(T('severity.level.critical'));
  await bar.getByTestId('event-severity-filter').getByLabel(T('severity.level.minor')).check();
  await expect(page).toHaveURL(/severity=minor/);
  await expect(page.getByTestId('event-entry')).toHaveCount(0);
  await bar.getByTestId('event-severity-filter').getByLabel(T('severity.level.critical')).check();
  await expect(page).toHaveURL(/severity=critical%2Cminor|severity=critical,minor/);
  await expect(downEntry).toBeVisible();
  await bar.getByRole('tab', { name: T('severity.sort.severity') }).click();
  await expect(page).toHaveURL(/sort=severity/);
  await expect(downEntry).toBeVisible();
});

test('mapping change in Settings re-classifies the active alert live; the old event keeps its severity', async ({ page, context }) => {
  await loginViaApi(page, undefined, '/');
  await expect(problemRow(page, C).getByTestId('severity-tag')).toHaveText(T('severity.level.critical'));
  await markDocument(page);
  const settings = await context.newPage();
  await settings.goto('/settings?tab=general');
  const card = settings.getByTestId('severity-mapping-card');
  await expect(card.getByTestId('severity-reclassify-note')).toBeVisible();
  const rowDC = card.locator('[data-key="down_critical"]');
  await rowDC.getByRole('combobox').selectOption('major');
  await expect(rowDC.getByTestId('severity-changed')).toBeVisible();
  await card.getByRole('button', { name: T('common.save'), exact: true }).click();
  await expect(settings.getByText(T('severity.mapping.saved'))).toBeVisible();
  expect((await admin.get('/api/settings/severity-mapping')).mapping.down_critical).toBe('major');
  await settings.close();

  // live (WS sensor.severity), no reload
  await expect(problemRow(page, C).getByTestId('severity-tag')).toHaveText(T('severity.level.major'), { timeout: 15_000 });
  await expect(problemRow(page, A).getByTestId('severity-tag')).toHaveText(T('severity.level.major'));
  await expectSameDocument(page);
  expect((await admin.get(`/api/sensors/${cId}`)).severity).toBe('major');
  // history is stable
  const ev = await admin.get(`/api/events?sensor_id=${cId}&kind=status_change&page_size=5`);
  expect(ev.items.find((e: any) => e.new_status === 'down').severity).toBe('critical');
  await page.goto(`/events?sensor_id=${cId}`);
  await expect(page.getByTestId('event-entry').filter({ hasText: C }).first().getByTestId('severity-tag')).toHaveText(T('severity.level.critical'));
  const audit = await raw(admin, 'GET', `/api/audit?action=sensor.severity&entity_id=${cId}&page_size=5`);
  if (audit.status === 200) expect.soft(audit.body.items[0]?.changes?.severity).toEqual(['critical', 'major']);
});

test('a sensor override wins over the mapping (sensor form; Automatic shows the mapping)', async ({ page }) => {
  await loginViaApi(page, undefined, `/sensors/${cId}`);
  await expect(page.getByTestId('sensor-severity')).toHaveText(T('severity.level.major'));
  await page.getByRole('button', { name: T('common.edit'), exact: true }).first().click();
  const down = page.getByTestId('severity-down');
  await expect(down.locator('option').first()).toHaveText(T('severity.automaticIs', { level: T('severity.level.major') }));
  await expect(down).toHaveValue('');
  await down.selectOption('minor');
  await page.getByRole('dialog').getByRole('button', { name: T('common.save'), exact: true }).click();
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect.poll(async () => (await admin.get(`/api/sensors/${cId}`)).severity, { timeout: 10_000 }).toBe('minor');
  expect((await admin.get(`/api/sensors/${cId}`)).severity_down).toBe('minor');
  await expect(page.getByTestId('sensor-severity')).toHaveText(T('severity.level.minor'));
});

test('an alert rule limited to Critical does not notify for a Warning-level alert; a rule for all severities does', async ({ page }) => {
  // B: down with the override "warning" (a Warning-level alert); paused until the rules exist
  bId = (await deadSensor(B, { enabled: false, severity_down: 'warning' })).id;
  for (const [name, path] of [['crit', 'sev-crit'], ['all', 'sev-all']]) {
    const h = await admin.post('/api/notification-channels', {
      name: uid(`qa-sev hook ${name}`),
      type: 'webhook',
      enabled: true,
      config: { url: `${WEBHOOK_URL}/${path}` },
    });
    hooks.push(h.id);
  }
  // the Critical-only rule through the form
  await loginViaApi(page, undefined, '/notifications');
  await page.getByRole('button', { name: T('rules.add') }).click();
  const dlg = page.getByRole('dialog');
  const critName = uid('qa-sev critical only');
  await dlg.getByLabel(/^Name/).fill(critName);
  await dlg.getByRole('combobox', { name: new RegExp(`^${T('rules.scope')}`) }).selectOption({ label: `${deviceName} / ${B}` });
  const sev = dlg.getByTestId('rule-severities');
  await expect(dlg.getByText(T('severity.rule.hint'))).toBeVisible();
  await sev.getByLabel(T('severity.level.critical')).check();
  await dlg.getByLabel(/^Delay/).fill('0');
  const hookName = (await admin.get(`/api/notification-channels`)).items.find((c: any) => c.id === hooks[0]).name;
  await dlg.locator('label').filter({ hasText: `${hookName} (` }).locator('input[type=checkbox]').check();
  await dlg.getByRole('button', { name: T('common.save'), exact: true }).click();
  await expect(dlg).toBeHidden();
  const crit = (await admin.get(`/api/alert-rules?sensor_id=${bId}`)).items.find((r: any) => r.name === critName);
  expect(crit.severities).toEqual(['critical']);
  rules.push(crit.id);
  await expect(page.getByText(T('severity.rule.summary', { list: T('severity.level.critical') })).first()).toBeVisible();
  const all = await admin.post('/api/alert-rules', {
    name: uid('qa-sev all'), sensor_id: bId, on_statuses: ['down', 'up'], delay_s: 0, enabled: true,
    channel_ids: [hooks[1]],
  });
  expect(all.severities).toEqual([]);
  rules.push(all.id);

  await admin.patch(`/api/sensors/${bId}`, { enabled: true });
  await waitForSensor(admin, bId, (s) => s.status === 'down', 60_000, 'B down');
  expect((await admin.get(`/api/sensors/${bId}`)).severity).toBe('warning');
  const forB = async (p: string) =>
    (await webhookRecords()).filter((r) => r.path.startsWith(`/${p}`) && r.body?.sensor?.id === bId && r.body?.new_status === 'down');
  await expect.poll(async () => (await forB('sev-all')).length, { timeout: 30_000, message: 'all-severities rule notified' }).toBe(1);
  expect((await forB('sev-all'))[0].body.severity).toBe('warning');
  // both rules were evaluated on the same status change: give the other one time, then expect nothing
  await page.waitForTimeout(4_000);
  expect(await forB('sev-crit')).toEqual([]);
});

test('Hebrew labels: tags, counts, Events filter, sensor form', async ({ page }) => {
  await loginViaApi(page, undefined, '/');
  await setLang(page, 'he');
  await expect(problemRow(page, C).getByTestId('severity-tag')).toHaveText(T('severity.level.minor', {}, 'he'));
  await expect(problemRow(page, B).getByTestId('severity-tag')).toHaveText(T('severity.level.warning', {}, 'he'));
  await expect(page.getByRole('group', { name: T('severity.filterLabel', {}, 'he') })).toBeVisible();
  await expect(problemRow(page, C).getByTestId('severity-tag')).toHaveAttribute('title', `${T('severity.tagTitle', { level: T('severity.level.minor', {}, 'he') }, 'he')} (Minor)`);
  await page.goto(`/events?sensor_id=${cId}`);
  await expect(page.getByTestId('event-severity-bar')).toContainText(T('severity.filter', {}, 'he'));
  await expect(page.getByRole('tab', { name: T('severity.sort.severity', {}, 'he') })).toBeVisible();
  await page.goto(`/sensors/${cId}`);
  await page.getByRole('button', { name: T('common.edit', {}, 'he'), exact: true }).first().click();
  await expect(page.getByText(T('severity.whenDown', {}, 'he'))).toBeVisible();
  await expect(page.getByText(T('severity.whenWarning', {}, 'he'))).toBeVisible();
  await setLang(page, 'en');
});

test('screenshots (light-he, dark-he, light-en)', async ({ browser }) => {
  test.setTimeout(300_000);
  // a kiosk screen for the wall view
  await raw(admin, 'PUT', '/api/settings', { feature_noc_enabled: true });
  const k = await admin.post('/api/kiosk-screens', {
    name: uid('qa-sev wall'), enabled: true, allowed_sources: ['127.0.0.1/32'], scope_group_id: null,
    widgets: ['summary', 'problems', 'recent_alerts'], language: 'en', show_addresses: false, refresh_s: 10,
  });
  const kHe = await admin.post('/api/kiosk-screens', {
    name: uid('qa-sev wall he'), enabled: true, allowed_sources: ['127.0.0.1/32'], scope_group_id: null,
    widgets: ['summary', 'problems', 'recent_alerts'], language: 'he', show_addresses: false, refresh_s: 10,
  });
  // an incident (three alerts on one device within the window) when the assistant mock runs
  let incidentId: number | null = null;
  await raw(admin, 'POST', '/api/settings/severity-mapping/reset');
  const corr = PHASE3B ? await admin.get('/api/settings') : null;
  if (PHASE3B) {
    await ensureAssistant(admin);
    await raw(admin, 'PUT', '/api/settings', { feature_ai_correlation_enabled: true, correlation_window_min: 5, correlation_min_alerts: 3 });
    const since = new Date(Date.now() - 1000).toISOString();
    const ids: number[] = [];
    for (const n of ['I1', 'I2', 'I3']) ids.push((await deadSensor(uid(`qa-sev ${n}`), n === 'I3' ? { impact: 'none' } : {})).id);
    for (const id of ids) await waitForSensor(admin, id, (x) => x.status === 'down', 60_000, 'incident member down');
    await expect.poll(async () => {
      const l = await raw(admin, 'GET', `/api/incidents?since=${encodeURIComponent(since)}&page_size=20`);
      incidentId = l.body?.items?.find((i: any) => i.severity)?.id ?? null;
      return incidentId !== null;
    }, { timeout: 60_000, message: 'incident with a severity' }).toBe(true);
  }
  const views: [string, string | ((lang: string) => string), (p: Page) => Promise<void>][] = [
    ['home', '/', async (p) => {
      await expect(p.getByTestId('severity-counts')).toBeVisible();
      await p.evaluate(() => document.getElementById('nm-problems')?.scrollIntoView({ block: 'start' }));
    }],
    ['events', `/events?severity=critical,minor,warning`, async (p) => { await expect(p.getByTestId('event-severity-bar')).toBeVisible(); }],
    ['mapping', '/settings?tab=general', async (p) => {
      const c = p.getByTestId('severity-mapping-card');
      await expect(c).toBeVisible();
      await c.scrollIntoViewIfNeeded();
    }],
    ['sensor-form', `/sensors/${cId}`, async (p) => {
      await p.getByRole('button', { name: /^(Edit|עריכה)$/ }).first().click();
      await expect(p.getByTestId('severity-overrides')).toBeVisible();
      await p.getByTestId('severity-overrides').scrollIntoViewIfNeeded();
    }],
    ['alert-rule-form', '/notifications', async (p) => {
      await p.getByRole('button', { name: /^(Add rule|הוספת כלל)$/ }).click();
      await expect(p.getByTestId('rule-severities')).toBeVisible();
      await p.getByTestId('rule-severities').locator('label').first().click();
      await p.getByTestId('rule-severities').scrollIntoViewIfNeeded();
    }],
    ['kiosk', (lang: string) => `/noc#${lang === 'he' ? kHe.token : k.token}`, async (p) => { await expect(p.getByTestId('kiosk-severity')).toBeVisible({ timeout: 30_000 }); }],
  ];
  if (incidentId) views.push(['incident', '/events?tab=incidents', async (p) => { await expect(p.getByTestId('incident-severity').first()).toBeVisible(); }]);
  for (const [name, path, ready] of views) await shotThemes(null, name, { browser, path, ready });
  await raw(admin, 'DELETE', `/api/kiosk-screens/${k.item.id}`);
  await raw(admin, 'DELETE', `/api/kiosk-screens/${kHe.item.id}`);
  await raw(admin, 'PUT', '/api/settings', { feature_noc_enabled: false });
  if (corr)
    await raw(admin, 'PUT', '/api/settings', {
      feature_ai_correlation_enabled: false,
      correlation_window_min: corr.correlation_window_min,
      correlation_min_alerts: corr.correlation_min_alerts,
    });
});

type Theme = 'light-he' | 'dark-he' | 'light-en';
/** Screenshots at 1440×900, deviceScaleFactor 2, per theme (a fresh context each). */
async function shotThemes(
  page: Page | null,
  name: string,
  o: { only?: Theme[]; browser?: Browser; path?: string | ((lang: string) => string); ready?: (p: Page) => Promise<void> } = {},
) {
  mkdirSync(SHOTS, { recursive: true });
  const themes: Theme[] = o.only ?? ['light-he', 'dark-he', 'light-en'];
  for (const th of themes) {
    const [scheme, lang] = th.split('-') as ['light' | 'dark', 'he' | 'en'];
    const browser = o.browser ?? page!.context().browser()!;
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, baseURL: process.env.BASE_URL ?? 'http://localhost:8080', colorScheme: scheme, locale: 'en-US', timezoneId: 'UTC' });
    const p = await ctx.newPage();
    await p.addInitScript((s) => localStorage.setItem('netmon.theme', s), scheme);
    await loginViaApi(p, undefined, '/');
    await p.request.patch('/api/auth/me', { data: { language: lang } });
    const path = typeof o.path === 'function' ? o.path(lang) : o.path;
    await p.goto(path ?? page!.url().replace(/^https?:\/\/[^/]+/, ''));
    await expect(p.locator('html')).toHaveAttribute('dir', lang === 'he' ? 'rtl' : 'ltr');
    if (o.ready) await o.ready(p);
    await p.waitForTimeout(600);
    await p.screenshot({ path: join(SHOTS, `${name}--${th}.png`) });
    await p.request.patch('/api/auth/me', { data: { language: 'en' } });
    await ctx.close();
  }
}
