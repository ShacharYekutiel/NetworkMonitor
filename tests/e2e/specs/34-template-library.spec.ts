import { expect, test, type Page } from '@playwright/test';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Api, VIEWER, apiAs, loginViaApi } from '../lib/helpers';
import { DISCOVERY_SNMP_PORT, collectWsFrames, ensureGroup, ensureViewer, raw, scanResults, startScan, waitForScan } from '../lib/lab';
import { findDevice } from '../lib/lab2b';
import { E2E_DATA_DIR, T, clearUserTemplates, installUserTemplates, removeUserTemplate, shot2b2, userTemplateDir } from '../lib/lab2b2';

// [2b-2] (g) Template library (data-model § Template library, api.md [2b-2] Device templates):
// 19 built-ins in order with verified/confidence; the nine converted templates equal the 2b-1 data; user
// override directory (10 fixture files, 7 errors) + reload; load errors in the UI; delete override -> back to
// built-in; Comware suggestion still hpe_comware; apply a user template; viewer sees no errors / user_dir.
// default mode: a failure does not hide the results of the later tests (state is re-derived per test)
test.describe.configure({ mode: 'default' });

let api: Api;
const ORDER = ['opnsense', 'unifi_ap', 'printer', 'apc_ups', 'aruba_cx', 'aruba_wifi', 'checkpoint_gaia', 'cisco_ios', 'dell_idrac', 'fortinet_fortigate', 'hpe_arubaos_switch', 'hpe_comware', 'hpe_ilo', 'mikrotik_routeros', 'paloalto_panos', 'nas_server', 'generic_snmp', 'http_server', 'generic_ping'];
const CONVERTED = ['opnsense', 'unifi_ap', 'printer', 'hpe_arubaos_switch', 'hpe_comware', 'nas_server', 'generic_snmp', 'http_server', 'generic_ping'];
const NEW = ORDER.filter((x) => !CONVERTED.includes(x));
const FIXTURE_2B1 = join(__dirname, '..', '..', '..', 'backend', 'tests', 'fixtures', 'templates_2b1.json');
// 2b-2-only keys (data-model § Schema: "the 2b-1 dict plus ...")
const TOP_2B2 = ['version', 'verified', 'verified_note', 'vendor', 'source', 'overrides_builtin', 'file'];
const ENTRY_2B2 = ['confidence', 'notes', 'when'];
const RULE_2B2 = ['sys_object_id_prefixes', 'sys_descr_regex'];

function strip2b2(t: any): any {
  const c = JSON.parse(JSON.stringify(t));
  for (const k of TOP_2B2) delete c[k];
  for (const e of c.entries) for (const k of ENTRY_2B2) delete e[k];
  for (const r of c.match) for (const k of RULE_2B2) delete r[k];
  return c;
}
const isVendorOid = (o: unknown) => typeof o === 'string' && o.replace(/^\./, '').startsWith('1.3.6.1.4.1.');
function readsVendorOid(e: any): boolean {
  if (e.kind === 'sensor') return ['snmp_custom', 'snmp_walk_count'].includes(e.type) && (isVendorOid(e.config?.oid) || isVendorOid(e.config?.percent_of_oid));
  if (e.kind === 'snmp_table') return isVendorOid(e.walk_oid);
  return false;
}
// the template card is the innermost <section> holding the id (the vendor group <section> contains it too)
const card = (page: Page, id: string) => page.locator('section').filter({ has: page.locator('header code', { hasText: new RegExp(`^${id}$`) }) }).last();

test.beforeAll(async () => {
  api = await apiAs();
  if (E2E_DATA_DIR) clearUserTemplates();
  await raw(api, 'POST', '/api/device-templates/reload');
});

test.afterAll(async () => {
  if (E2E_DATA_DIR) clearUserTemplates();
  await raw(api, 'POST', '/api/device-templates/reload');
});

test('built-in library: 19 templates, order, zero errors, verified + confidence + names', async () => {
  const r = await api.get('/api/device-templates');
  expect(r.items.map((t: any) => t.id)).toEqual(ORDER);
  expect(r.library).toMatchObject({ builtin_count: 19, user_count: 0, overridden: [], errors: [] });
  expect(r.library.user_dir).toBeTruthy();
  if (E2E_DATA_DIR) expect(r.library.user_dir).toBe(userTemplateDir());
  for (const t of r.items) {
    expect.soft(t.source, t.id).toBe('builtin');
    expect.soft(t.overrides_builtin, t.id).toBe(false);
    expect.soft(t.file, t.id).toMatch(new RegExp(`^[a-z]+/${t.id}\\.yaml$`));
    expect.soft(t.version, t.id).toBeGreaterThanOrEqual(1);
    expect.soft(t.name.en && t.name.he, `${t.id} en+he names`).toBeTruthy();
    for (const [i, e] of t.entries.entries()) {
      if (readsVendorOid(e)) expect.soft(['high', 'medium', 'low'], `${t.id} entries[${i}] confidence`).toContain(e.confidence);
      expect.soft(typeof e.notes, `${t.id} entries[${i}].notes`).toBe('string');
    }
    for (const rule of t.match) expect.soft(Object.keys(rule)).toEqual(expect.arrayContaining(['sys_object_id_prefixes', 'sys_descr_regex', 'enterprises']));
  }
  const by = Object.fromEntries(r.items.map((t: any) => [t.id, t]));
  for (const id of ['generic_ping', 'http_server', 'generic_snmp']) expect.soft(by[id].verified, id).toBe(true);
  for (const id of [...NEW, 'nas_server', 'printer', 'unifi_ap', 'opnsense', 'hpe_arubaos_switch', 'hpe_comware']) expect.soft(by[id].verified, id).toBe(false);
  for (const id of NEW) expect.soft(by[id].priority, id).toBe(50);
  expect(by.dell_idrac.match.some((m: any) => (m.sys_object_id_prefixes ?? []).includes('1.3.6.1.4.1.674.10892.5'))).toBe(true);
  expect(by.hpe_ilo.match.some((m: any) => m.sys_descr_regex)).toBe(true);
  expect(by.checkpoint_gaia.entries.find((e: any) => e.name?.en === 'ClusterXL state').when).toEqual([{ oid: '1.3.6.1.4.1.2620.1.5.2.0', in: [1], not_in: null }]);
});

test('the nine converted templates equal the 2b-1 data (fixture) with 2b-2 keys removed', async () => {
  test.skip(!existsSync(FIXTURE_2B1), 'backend/tests/fixtures/templates_2b1.json missing');
  const fixture = JSON.parse(readFileSync(FIXTURE_2B1, 'utf8'));
  const r = await api.get('/api/device-templates');
  const nine = r.items.filter((t: any) => CONVERTED.includes(t.id)).map(strip2b2);
  expect(nine.map((t: any) => t.id)).toEqual(fixture.map((t: any) => t.id));
  for (let i = 0; i < nine.length; i++) expect.soft(nine[i], nine[i].id).toEqual(fixture[i]);
});

test('user directory: 10 files -> 3 user templates, 1 override, 7 errors; reload broadcasts', async ({ page }) => {
  test.skip(!E2E_DATA_DIR, 'needs E2E_DATA_DIR (run.sh)');
  const files = installUserTemplates();
  expect(files).toHaveLength(10);
  const ws = collectWsFrames(page);
  await loginViaApi(page, undefined, '/templates');
  const lib = await api.post('/api/device-templates/reload');
  expect(lib).toMatchObject({ builtin_count: 19, user_count: 3, overridden: ['hpe_comware'], user_dir: userTemplateDir() });
  const errs = lib.errors.map((e: any) => ({ file: e.file, id: e.template_id, path: e.path, msg: e.message, source: e.source }));
  const byFile = Object.fromEntries(errs.map((e: any) => [e.file, e]));
  expect(Object.keys(byFile).sort()).toEqual(['bad/badoid.yaml', 'bad/broken.yaml', 'bad/noconf.yaml', 'bad/unknown_key.yaml', 'bad/wrong_name.yaml', 'dup/acme_ups.yml', 'override/generic_snmp.yaml']);
  expect(errs).toHaveLength(7);
  for (const e of errs) expect.soft(e.source).toBe('user');
  expect.soft(byFile['dup/acme_ups.yml']).toMatchObject({ id: 'acme_ups', msg: 'duplicate id "acme_ups" (also in acme/acme_ups.yaml)' });
  expect.soft(byFile['bad/broken.yaml'].msg).toMatch(/^invalid YAML: /);
  expect.soft(byFile['bad/broken.yaml'].id).toBeNull();
  expect.soft(byFile['bad/broken.yaml'].path).toBe('');
  expect.soft(byFile['bad/unknown_key.yaml']).toMatchObject({ msg: 'unknown key: prio', path: '' });
  expect.soft(byFile['bad/wrong_name.yaml'].msg).toBe('id must match the file name (expected "wrong_name")');
  expect.soft(byFile['bad/badoid.yaml']).toMatchObject({ msg: 'invalid OID: abc', path: 'entries[0].walk_oid' });
  expect.soft(byFile['bad/noconf.yaml']).toMatchObject({ msg: 'confidence required for vendor OIDs', path: 'entries[2]' });
  expect.soft(byFile['override/generic_snmp.yaml'].msg).toBe('unknown sensor type: foo');
  expect.soft(byFile['override/generic_snmp.yaml'].id).toBe('generic_snmp');

  const list = await api.get('/api/device-templates');
  const by = Object.fromEntries(list.items.map((t: any) => [t.id, t]));
  expect(by.hpe_comware).toMatchObject({ source: 'user', overrides_builtin: true, priority: 55, file: 'override/hpe_comware.yaml' });
  expect(by.acme_ups).toMatchObject({ source: 'user', overrides_builtin: false, file: 'acme/acme_ups.yaml', priority: 30 });
  expect(by.acme_json).toMatchObject({ source: 'user', file: 'json/acme_json.json' });
  expect(by.acme_json.name.he).toBe(by.acme_json.name.en); // he defaults to en
  expect(by.generic_snmp).toMatchObject({ source: 'builtin', overrides_builtin: false });
  expect(list.items).toHaveLength(21);
  const ids = list.items.map((t: any) => t.id);
  expect(ids.indexOf('hpe_comware')).toBeLessThan(ids.indexOf('apc_ups')); // priority 55 before the 50s
  expect(list.library.errors).toHaveLength(7);
  expect((await api.get('/api/device-templates/hpe_comware')).source).toBe('user');

  await expect.poll(() => ws.frames.filter((f) => f.type === 'templates.reloaded').length).toBeGreaterThanOrEqual(1);
  const f = ws.frames.filter((x) => x.type === 'templates.reloaded').pop();
  expect(f.data).toMatchObject({ count: 21, error_count: 7 });
  expect(f.data.loaded_at).toBe(lib.loaded_at);

  // UI (live via templates.reloaded, no reload of the page): errors panel, user badges, user dir
  const panel = page.getByTestId('template-errors');
  await expect(panel).toBeVisible();
  await expect(panel).toContainText(T('templates.loadErrors', { count: 7 }));
  for (const e of errs) {
    await expect.soft(panel).toContainText(e.file);
    await expect.soft(panel).toContainText(e.msg.slice(0, 30));
  }
  await expect(page.getByText(userTemplateDir())).toBeVisible();
  const cw = card(page, 'hpe_comware');
  await expect(cw).toContainText(T('templates.source.user'));
  await expect(cw).toContainText(T('templates.overrides'));
  await expect(card(page, 'acme_ups')).toContainText(T('templates.source.user'));
  await expect(card(page, 'generic_snmp')).toContainText(T('templates.source.builtin'));
  await shot2b2(page, 'templates-errors-en');
});

test('Reload button (admin) shows counts; Comware apply/suggestion with the override; acme_ups applies', async ({ page }) => {
  test.skip(!E2E_DATA_DIR, 'needs E2E_DATA_DIR (run.sh)');
  installUserTemplates(); // self-contained (beforeAll of a restarted worker clears the directory)
  await loginViaApi(page, undefined, '/templates');
  const resp = page.waitForResponse((r) => r.url().endsWith('/api/device-templates/reload') && r.request().method() === 'POST');
  await page.getByRole('button', { name: T('templates.reload') }).click();
  expect((await resp).status()).toBe(200);
  await expect(page.getByText(T('templates.reloadDone', { builtin: 19, user: 3, errors: 7 }))).toBeVisible();
  // apply-template validates against the current registry (user templates included)
  const gid = await ensureGroup(api, 'QA Templates 2b-2');
  const d = await api.post('/api/devices', { group_id: gid, name: 'qa-acme-ups', host: '127.0.0.108', snmp_credentials: { version: 'v2c', port: DISCOVERY_SNMP_PORT, community: 'public', timeout_s: 1, retries: 0 }, auto_sensors: [] });
  try {
    const res = await api.post(`/api/devices/${d.id}/apply-template`, { template_id: 'acme_ups' });
    expect(res.created.map((s: any) => s.name)).toEqual(expect.arrayContaining(['Ping', 'System']));
    expect(res.skipped.map((s: any) => [s.name, s.reason])).toEqual([['Battery', 'probe_failed']]);
    expect((await raw(api, 'POST', `/api/devices/${d.id}/apply-template`, { template_id: 'acme_nope' })).status).toBe(422);
  } finally {
    await api.del(`/api/devices/${d.id}`);
  }
});

test('delete the override + reload -> hpe_comware built-in again; Comware suggestion still hpe_comware', async () => {
  test.skip(!E2E_DATA_DIR, 'needs E2E_DATA_DIR (run.sh)');
  installUserTemplates();
  removeUserTemplate('override/hpe_comware.yaml');
  const lib = await api.post('/api/device-templates/reload');
  expect(lib).toMatchObject({ user_count: 2, overridden: [] });
  expect(lib.errors).toHaveLength(7); // the override file had no error; generic_snmp's broken override stays
  const t = await api.get('/api/device-templates/hpe_comware');
  expect(t).toMatchObject({ source: 'builtin', overrides_builtin: false, priority: 50, file: 'hpe/hpe_comware.yaml' });
  // discovery suggestion for the 2b-1 Comware sim (127.0.0.52) and the 2b-2 Comware port sim (127.0.0.82)
  const gid = await ensureGroup(api, 'QA Templates 2b-2');
  const s = await startScan(api, { targets: ['127.0.0.52', '127.0.0.82'], group_id: gid, options: { ping: false, snmp: true, snmp_port: DISCOVERY_SNMP_PORT, snmp_timeout_s: 1, tcp: false, dns: false } });
  await waitForScan(api, s.id);
  const res = Object.fromEntries((await scanResults(api, s.id)).map((r: any) => [r.ip, r]));
  expect(res['127.0.0.52']).toMatchObject({ device_class: 'switch', suggested_template: 'hpe_comware' });
  expect(res['127.0.0.82']).toMatchObject({ device_class: 'switch', suggested_template: 'hpe_comware' });
  // unknown template on import -> 422 (validated against the current registry)
  const imp = await raw(api, 'POST', `/api/discovery/scans/${s.id}/import`, { group_id: gid, items: [{ result_id: res['127.0.0.82'].id, template_id: 'no_such_tpl' }] });
  expect(imp.status).toBe(422);
});

test('viewer: library without errors and user_dir; reload 403; no Reload button', async ({ page }) => {
  await ensureViewer(api);
  const viewer = await apiAs(VIEWER);
  const r = await viewer.get('/api/device-templates');
  expect(r.library.errors).toEqual([]);
  expect(r.library.user_dir).toBe('');
  expect(r.library.builtin_count).toBe(19);
  expect((await raw(viewer, 'POST', '/api/device-templates/reload')).status).toBe(403);
  await loginViaApi(page, VIEWER, '/templates');
  await expect(card(page, 'checkpoint_gaia')).toBeVisible();
  await expect(page.getByRole('button', { name: T('templates.reload') })).toHaveCount(0);
  await expect(page.getByTestId('template-errors')).toHaveCount(0);
});

test('Templates page: all 19 cards, verified/unverified badges, version, confidence chips, when, notes', async ({ page }) => {
  if (E2E_DATA_DIR) {
    clearUserTemplates();
    await api.post('/api/device-templates/reload');
  }
  await loginViaApi(page, undefined, '/templates');
  for (const id of ORDER) await expect.soft(card(page, id), id).toHaveCount(1);
  await expect(card(page, 'generic_snmp')).toContainText(T('templates.verified'));
  await expect(card(page, 'checkpoint_gaia')).toContainText(T('templates.unverified'));
  await expect(card(page, 'checkpoint_gaia')).toContainText(T('templates.version', { version: 1 }));
  const cp = card(page, 'checkpoint_gaia');
  for (const c of ['high', 'medium', 'low']) await expect.soft(cp).toContainText(T(`confidence.${c}`));
  await expect(cp).toContainText(T('templates.when'));
  await expect(cp).toContainText('1.3.6.1.4.1.2620.1.5.2.0');
  const unver = cp.getByText(T('templates.unverified'), { exact: true });
  const title = await unver.evaluate((e) => (e.closest('[title]') as HTMLElement | null)?.title ?? '');
  expect(title).toBeTruthy(); // tooltip: verified_note or the unverified hint
  await cp.scrollIntoViewIfNeeded();
  await shot2b2(page, 'templates-en');
  await page.request.patch('/api/auth/me', { data: { language: 'he' } });
  try {
    await page.reload();
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
    await expect(card(page, 'checkpoint_gaia')).toContainText(T('templates.unverified', {}, 'he'));
    await expect(card(page, 'checkpoint_gaia')).toContainText('חומת אש Check Point Gaia');
    await card(page, 'checkpoint_gaia').scrollIntoViewIfNeeded();
    await shot2b2(page, 'templates-he');
  } finally {
    await page.request.patch('/api/auth/me', { data: { language: 'en' } });
  }
});

test('converted hpe_arubaos_switch re-applied to the 2b-1 device creates nothing new (identical entries)', async () => {
  const core = await findDevice(api, '127.0.0.51');
  test.skip(!core, '2b-1 devices missing (spec 24 not run)');
  const res = await api.post(`/api/devices/${core.id}/apply-template`, { template_id: 'hpe_arubaos_switch' });
  expect(res.created).toEqual([]);
  expect(res.skipped.filter((s: any) => s.reason === 'exists').length).toBeGreaterThanOrEqual(10);
});
