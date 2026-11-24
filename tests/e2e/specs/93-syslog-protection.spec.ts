import { expect, test, type Browser, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { Api, apiAs, BASE_URL, loginViaApi, SCREENSHOT_DIR } from '../lib/helpers';
import { raw } from '../lib/lab';
import { T } from '../lib/lab2b2';
import { featureOn, marker, sendSyslog, sleep, waitItems, waitReceivers } from '../lib/lab2b3';
import { features, setFeatures } from '../lib/lab3';

// [syslog-protection] 93 — syslog flood protection (docs/api.md "[syslog-protection]", data-model.md
// "[syslog-protection]"): a filter rule drops matching messages and counts them (rule hits, ingest counters, per
// source); "keep newest N"; dedup folds identical lines into one row with a repeat count (×N in the Logs UI);
// the per-source rate limit drops the excess and counts it; turning dedup / the rate limit off restores the old
// behaviour; top talkers → "Create rule" pre-fills the form; the cleanup offer shows the exact number and deletes
// only after confirmation; Hebrew. Screenshots (1440×900 @2x, en + he) to $SYSLOG_SHOT_DIR.
test.describe.configure({ mode: 'serial' });

const SHOT_DIR = process.env.SYSLOG_SHOT_DIR ?? join(SCREENSHOT_DIR, 'syslog-protection');
// own loopback sources (no device: shown by address), so no other spec's traffic mixes in
const SRC = { drop: '127.0.0.94', keep: '127.0.0.95', rate: '127.0.0.96', dedup: '127.0.0.97', noisy: '127.0.0.93' };
const DEFAULTS = {
  syslog_dedup_enabled: true,
  syslog_dedup_window_s: 60,
  syslog_rate_limit_enabled: true,
  syslog_rate_limit_per_min: 3000,
};
let api: Api;
let auditWas: boolean | undefined;
const RUN = marker('qa93');
let dedupMsg = '';

const line = (src: string, prog: string, text: string, pri = 14) => `<${pri}>Nov 24 10:00:00 ${src} ${prog}: ${text}`;

async function counters(): Promise<any> {
  return (await api.get('/api/ingest/status')).counters.syslog;
}
async function filters(): Promise<any[]> {
  return (await api.get('/api/syslog-filters')).items;
}

/** Own context at 1440×900, deviceScaleFactor 2, signed in, in `lang`. */
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

async function shot(page: Page, name: string) {
  mkdirSync(SHOT_DIR, { recursive: true });
  await page.waitForTimeout(500);
  await page.screenshot({ path: join(SHOT_DIR, `${name}.png`) });
}

test.beforeAll(async () => {
  api = await apiAs();
  auditWas = (await features(api)).audit;
  await setFeatures(api, { audit: true });
  await featureOn(api, 'syslog');
  await waitReceivers(api, ['syslog_udp', 'syslog_tcp'], 'listening');
  await api.put('/api/settings', DEFAULTS);
  for (const f of await filters()) await api.del(`/api/syslog-filters/${f.id}`);
});

test.afterAll(async () => {
  for (const f of await filters().catch(() => [])) await api.del(`/api/syslog-filters/${f.id}`).catch(() => undefined);
  await api.put('/api/settings', DEFAULTS).catch(() => undefined);
  if (auditWas === false) await setFeatures(api, { audit: false }).catch(() => undefined);
  await api.patch('/api/auth/me', { language: 'en' }).catch(() => undefined);
});

test('defaults after the upgrade: dedup and the rate limit ON, no filter rules; admin only', async () => {
  const s = await api.get('/api/settings');
  expect(s).toMatchObject(DEFAULTS);
  expect(await filters()).toEqual([]);
  const st = (await api.get('/api/ingest/status')).syslog_protection;
  expect(st).toMatchObject({ dedup_enabled: true, dedup_window_s: 60, rate_limit_enabled: true, rate_limit_per_min: 3000, filter_rules: 0 });
  // validation
  expect((await raw(api, 'POST', '/api/syslog-filters', { name: 'no condition' })).status).toBe(422);
  expect((await raw(api, 'PUT', '/api/settings', { syslog_rate_limit_per_min: 5 })).status).toBe(422);
});

test('filter rule "drop": matching messages are not stored, counted on the rule and per reason', async () => {
  const before = await counters();
  const rule = await api.post('/api/syslog-filters', { name: `${RUN} drop`, sources: [SRC.drop], program: 'DROPME', severity_min: 6 });
  expect(rule).toMatchObject({ action: 'drop', enabled: true, hits: 0 });
  sendSyslog({ from: SRC.drop, count: 5, messages: [line('h', 'DROPME', `${RUN} drop {i}`)] });
  sendSyslog({ from: SRC.drop, messages: [line('h', 'DROPME', `${RUN} drop error`, 11), line('h', 'other', `${RUN} drop other`)] });
  await waitItems(api, 'syslog', { q: `${RUN} drop` }, (it) => it.length === 2);
  await expect.poll(async () => (await counters()).dropped.filter_rule - before.dropped.filter_rule, { timeout: 10_000 }).toBe(5);
  const items = (await api.get(`/api/syslog?q=${encodeURIComponent(`${RUN} drop`)}`)).items.map((x: any) => x.message).sort();
  expect(items).toEqual([`${RUN} drop error`, `${RUN} drop other`]);
  // hit counter (persisted every 10 s; the list adds the hits not yet written)
  await expect.poll(async () => (await filters()).find((f) => f.id === rule.id)?.hits, { timeout: 15_000 }).toBe(5);
  const src = (await api.get('/api/ingest/status')).syslog_protection.top_sources.find((x: any) => x.source_ip === SRC.drop);
  expect(src.filter_rule).toBe(5);
  // disabled: stored again
  await api.patch(`/api/syslog-filters/${rule.id}`, { enabled: false });
  sendSyslog({ from: SRC.drop, messages: [line('h', 'DROPME', `${RUN} drop after`)] });
  await waitItems(api, 'syslog', { q: `${RUN} drop after` }, (it) => it.length === 1);
  await api.del(`/api/syslog-filters/${rule.id}`);
});

test('filter rule "keep newest N": only the newest 3 per source stay', async () => {
  const rule = await api.post('/api/syslog-filters', { name: `${RUN} keep`, program: 'KEEPME', action: 'keep', keep_n: 3 });
  for (let i = 0; i < 8; i++) {
    sendSyslog({ from: SRC.keep, messages: [line('h', 'KEEPME', `${RUN} keep n${i}`)] });
    await sleep(120);
  }
  const items = await waitItems(api, 'syslog', { q: `${RUN} keep` }, (it) => it.length === 3 && it.some((x) => x.message.endsWith('n7')), 20_000);
  expect(items.map((x) => x.message).sort()).toEqual([5, 6, 7].map((i) => `${RUN} keep n${i}`));
  await expect.poll(async () => (await counters()).pruned, { timeout: 10_000 }).toBeGreaterThanOrEqual(5);
  await api.del(`/api/syslog-filters/${rule.id}`);
});

test('dedup: identical messages are one row with a repeat count; the Logs UI shows ×6', async ({ browser }) => {
  dedupMsg = `${RUN} fan 2 failed`;
  const before = await counters();
  sendSyslog({ from: SRC.dedup, count: 6, messages: [line('sw', 'fan', dedupMsg, 12)] });
  const items = await waitItems(api, 'syslog', { q: dedupMsg }, (it) => it.length === 1 && it[0].repeat_count === 6, 20_000);
  expect(items[0].last_seen).toBeTruthy();
  expect((await counters()).deduplicated - before.deduplicated).toBe(5);
  const st = await api.get(`/api/syslog/stats?q=${encodeURIComponent(dedupMsg)}`);
  expect([st.total, st.rows]).toEqual([6, 1]);
  for (const lang of ['en', 'he'] as const) {
    const page = await shotPage(browser, lang, '/logs?tab=syslog');
    const row = page.getByRole('row').filter({ hasText: dedupMsg });
    await expect(row).toBeVisible();
    await expect(row.getByTestId('repeat-count')).toHaveText('×6');
    await shot(page, `93-dedup-row-${lang}`);
    await row.click();
    await expect(page.getByTestId('detail-repeats')).toContainText('×6');
    await page.close();
  }
});

test('dedup off restores the old behaviour: every message is a row', async () => {
  await api.put('/api/settings', { syslog_dedup_enabled: false });
  const m = `${RUN} dedup off`;
  sendSyslog({ from: SRC.dedup, count: 3, messages: [line('sw', 'fan', m, 12)] });
  const items = await waitItems(api, 'syslog', { q: m }, (it) => it.length === 3);
  expect(items.every((x) => x.repeat_count === 1)).toBe(true);
  await api.put('/api/settings', { syslog_dedup_enabled: true });
});

test('rate limit: above N rows per minute from one source the excess is dropped and counted; off = all stored', async ({ browser }) => {
  await api.put('/api/settings', { syslog_rate_limit_per_min: 10 });
  const before = await counters();
  const m = `${RUN} storm`;
  sendSyslog({ from: SRC.rate, count: 25, rate: 0, messages: [line('sw', 'app', `${m} {i}`)] });
  let dropped = 0;
  await expect
    .poll(async () => {
      const c = await counters();
      dropped = c.dropped.source_rate_limit - before.dropped.source_rate_limit;
      const stored = (await api.get(`/api/syslog?limit=500&q=${encodeURIComponent(m)}`)).items.length;
      return stored + dropped === 25 && dropped > 0;
    }, { timeout: 15_000 })
    .toBe(true);
  // one minute window of the limiter (a run that straddles a minute boundary lets up to 20 through)
  expect(dropped).toBeGreaterThanOrEqual(5);
  const src = (await api.get('/api/ingest/status')).syslog_protection.top_sources.find((x: any) => x.source_ip === SRC.rate);
  expect(src.source_rate_limit).toBe(dropped);
  // a system event says so (once an hour per source)
  await expect
    .poll(async () => (await api.get('/api/events?kind=system&page_size=50')).items.some((e: any) => String(e.message).includes(`${SRC.rate} exceeds 10 rows per minute`)), { timeout: 10_000 })
    .toBe(true);
  // the protection tab shows the counter by reason and by source
  const page = await shotPage(browser, 'en', '/logs?tab=protection');
  const counters_ = page.getByTestId('protection-counters');
  await expect(counters_.locator('[data-reason="source_rate_limit"]')).not.toContainText(/^0$/);
  await expect(page.getByTestId('protection-sources').getByRole('row').filter({ hasText: SRC.rate })).toBeVisible();
  await page.close();
  // off: nothing limited
  await api.put('/api/settings', { syslog_rate_limit_enabled: false });
  sendSyslog({ from: SRC.rate, count: 15, rate: 0, messages: [line('sw', 'app', `${RUN} calm {i}`)] });
  await waitItems(api, 'syslog', { q: `${RUN} calm` }, (it) => it.length === 15);
  await api.put('/api/settings', DEFAULTS);
});

test('top talkers → "Create rule" pre-fills the form; the cleanup offer shows the number, deletes only after confirmation', async ({ browser }) => {
  const m = `${RUN} seqNO`;
  sendSyslog({ from: SRC.noisy, count: 40, messages: [line('noisy', 'NOISY', `${m}={i} PART=1 get sysName.0`)] });
  await waitItems(api, 'syslog', { q: m }, (it) => it.length === 40);
  const top = await api.get('/api/syslog/top-talkers');
  expect(top.items.find((x: any) => x.source_ip === SRC.noisy && x.program === 'NOISY')).toMatchObject({ rows: 40, severity: 6, device_id: null });

  const page = await shotPage(browser, 'en', '/logs?tab=protection');
  const talker = page.getByTestId('top-talkers').getByRole('row').filter({ hasText: SRC.noisy }).filter({ hasText: 'NOISY' });
  await expect(talker).toBeVisible();
  await shot(page, '93-top-talkers-en');
  await talker.getByTestId('talker-create-rule').click();
  const dlg = page.getByTestId('syslog-filter-editor');
  await expect(dlg).toBeVisible();
  await expect(dlg.getByTestId('filter-sources')).toHaveValue(SRC.noisy);
  await expect(dlg.getByTestId('filter-program')).toHaveValue('NOISY');
  await expect(dlg.getByTestId('filter-severity')).toHaveValue('6');
  await expect(dlg.getByTestId('filter-name')).not.toHaveValue('');
  await shot(page, '93-rule-editor-en');
  await dlg.getByRole('button', { name: T('common.save') }).click();
  // the offer: the exact number, nothing deleted yet
  const offer = page.getByTestId('syslog-cleanup');
  await expect(offer).toBeVisible();
  await expect(offer.getByTestId('cleanup-count')).toHaveText('40');
  await shot(page, '93-cleanup-confirm-en');
  await offer.getByRole('button', { name: T('syslogFilters.cleanup.keep') }).click();
  await expect(offer).toBeHidden();
  await sleep(1500);
  expect((await api.get(`/api/syslog?limit=500&q=${encodeURIComponent(m)}`)).items).toHaveLength(40);
  // the rule list (screenshot) — new lines from the source are dropped now
  const rule = (await filters()).find((f) => f.sources.includes(`${SRC.noisy}/32`));
  expect(rule).toMatchObject({ program: 'NOISY', severity_min: 6, action: 'drop' });
  sendSyslog({ from: SRC.noisy, count: 3, messages: [line('noisy', 'NOISY', `${RUN} later {i}`)] });
  await expect.poll(async () => (await filters()).find((f) => f.id === rule.id)?.hits, { timeout: 15_000 }).toBe(3);
  const row = page.getByTestId('syslog-filters').locator(`[data-filter-id="${rule.id}"]`);
  await expect(row.getByTestId('filter-hits')).toHaveText('3', { timeout: 20_000 });
  await shot(page, '93-filter-rules-en');
  // later: "Clean up stored" → the same offer → confirm → deleted
  await row.getByTestId('filter-cleanup').click();
  await expect(offer.getByTestId('cleanup-count')).toHaveText('40');
  await offer.getByTestId('cleanup-confirm').click();
  await expect.poll(async () => (await api.get(`/api/syslog?limit=500&q=${encodeURIComponent(m)}`)).items.length, { timeout: 30_000 }).toBe(0);
  await expect.poll(async () => (await api.get('/api/syslog-filters/cleanup')).cleanup?.state, { timeout: 30_000 }).toBe('done');
  expect((await api.get('/api/syslog-filters/cleanup')).cleanup).toMatchObject({ expected: 40, deleted: 40, rule_id: rule.id });
  const audit = await api.get('/api/audit?page_size=50');
  const actions = (audit.items ?? []).map((a: any) => a.action);
  expect(actions).toContain('syslog_filter.cleanup');
  expect(actions).toContain('syslog_filter.create');
  await page.close();
});

test('Hebrew: protection tab, rules, top talkers, cleanup offer (RTL)', async ({ browser }) => {
  const m = `${RUN} he-noisy`;
  sendSyslog({ from: SRC.noisy, count: 12, messages: [line('noisy', 'CHATTY', `${m} {i}`)] });
  await waitItems(api, 'syslog', { q: m }, (it) => it.length === 12);
  const page = await shotPage(browser, 'he', '/logs?tab=protection');
  await expect(page.getByRole('tab', { name: T('logs.tab.protection', {}, 'he') })).toBeVisible();
  await expect(page.getByText(T('syslogFilters.title', {}, 'he'), { exact: true })).toBeVisible();
  await expect(page.getByText(T('syslogFilters.summaryTitle', {}, 'he'), { exact: true })).toBeVisible();
  await shot(page, '93-filter-rules-he');
  const talker = page.getByTestId('top-talkers').getByRole('row').filter({ hasText: 'CHATTY' });
  await talker.scrollIntoViewIfNeeded();
  await shot(page, '93-top-talkers-he');
  await talker.getByTestId('talker-create-rule').click();
  const dlg = page.getByTestId('syslog-filter-editor');
  await expect(dlg.getByTestId('filter-program')).toHaveValue('CHATTY');
  await expect(dlg).toContainText(T('syslogFilters.conditions', {}, 'he'));
  await shot(page, '93-rule-editor-he');
  await dlg.getByRole('button', { name: T('common.save', {}, 'he') }).click();
  const offer = page.getByTestId('syslog-cleanup');
  await expect(offer.getByTestId('cleanup-count')).toHaveText('12');
  await expect(offer).toContainText(T('syslogFilters.cleanup.question', {}, 'he'));
  await shot(page, '93-cleanup-confirm-he');
  await offer.getByRole('button', { name: T('syslogFilters.cleanup.keep', {}, 'he') }).click();
  // the settings card, in Hebrew
  await page.goto('/settings?tab=ingest');
  const card = page.getByTestId('syslog-protection-settings');
  await card.scrollIntoViewIfNeeded();
  await expect(card).toContainText(T('syslogProtection.dedup', {}, 'he'));
  await expect(card.getByTestId('rate-per-min')).toHaveValue('3000');
  await shot(page, '93-settings-he');
  await page.close();
});
