import { expect, test, type Browser, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { Api, apiAs, BASE_URL, loginViaApi, SCREENSHOT_DIR, uid, WEBHOOK_URL } from '../lib/helpers';
import { T } from '../lib/lab2b2';

// [default-rule] 90 — the system never creates an alert rule by itself, so the notifications page offers one:
// (1) the create-channel dialog, while no alert rule exists at all, shows "Create a default alert rule" (checked):
//     saved → a rule for every sensor, down only, every severity, no delay, enabled, sending to the new channel,
//     named in the UI language; unticked → no rule;
// (2) channels but no enabled rule → a warning banner ("No alert will be sent…") whose button creates the same
//     rule for every enabled channel (named in the banner); the banner then disappears.
// The spec empties the alert-rule table for its cases and recreates the earlier specs' rules in afterAll.
// Screenshots (1440×900 @2x, en + he) to $RULE_SHOT_DIR.
test.describe.configure({ mode: 'serial' });

const SHOT_DIR = process.env.RULE_SHOT_DIR ?? join(SCREENSHOT_DIR, 'default-rule');
const RULE_FIELDS = [
  'name',
  'sensor_id',
  'on_statuses',
  'delay_s',
  'enabled',
  'channel_ids',
  'escalation_after_s',
  'escalation_channel_ids',
  'repeat_every_s',
  'ai_note',
  'severities',
] as const;
let api: Api;
let saved: any[] = [];
const channels: number[] = [];

async function allRules(): Promise<any[]> {
  return (await api.get('/api/alert-rules?page_size=1000')).items;
}
async function deleteAllRules() {
  for (const r of await allRules()) await api.del(`/api/alert-rules/${r.id}`);
}
async function enabledChannels(): Promise<any[]> {
  return (await api.get('/api/notification-channels?page_size=1000')).items.filter((c: any) => c.enabled);
}
async function webhookChannel(name: string, enabled = true): Promise<number> {
  const c = await api.post('/api/notification-channels', {
    name,
    type: 'webhook',
    enabled,
    config: { url: `${WEBHOOK_URL}/hook` },
  });
  channels.push(c.id);
  return c.id;
}

/** Own context at 1440×900, deviceScaleFactor 2, signed in, in `lang`, on the notifications page. */
async function notificationsPage(browser: Browser, lang: 'en' | 'he'): Promise<Page> {
  const ctx = await browser.newContext({
    baseURL: BASE_URL,
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    locale: lang === 'he' ? 'he-IL' : 'en-US',
    timezoneId: 'UTC',
  });
  const page = await ctx.newPage();
  await loginViaApi(page, undefined, '/');
  const r = await page.request.patch('/api/auth/me', { data: { language: lang } });
  expect(r.status(), await r.text()).toBe(200);
  await page.goto('/notifications');
  await expect(page.locator('html')).toHaveAttribute('dir', lang === 'he' ? 'rtl' : 'ltr');
  await expect(page.getByTestId('rules')).toBeVisible();
  return page;
}

async function shot(page: Page, name: string) {
  mkdirSync(SHOT_DIR, { recursive: true });
  await page.waitForTimeout(400);
  await page.screenshot({ path: join(SHOT_DIR, `${name}.png`) });
}

/** Opens "Add channel" and fills a webhook channel (not saved). */
async function fillChannel(page: Page, name: string, lang: 'en' | 'he' = 'en') {
  await page.getByRole('button', { name: T('channels.add', {}, lang) }).first().click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel(new RegExp(`^${T('common.name', {}, lang)}`)).fill(name);
  await dlg.getByLabel(T('channels.type', {}, lang)).selectOption('webhook');
  await dlg.getByRole('textbox', { name: new RegExp(`^${T('channels.url', {}, lang)}`) }).fill(`${WEBHOOK_URL}/hook`);
  return dlg;
}

function expectDefaultRule(rule: any, channelIds: number[], lang: 'en' | 'he' = 'en') {
  expect(rule).toMatchObject({
    name: T('rules.defaultRule.name', {}, lang),
    sensor_id: null,
    on_statuses: ['down'],
    severities: [],
    delay_s: 0,
    enabled: true,
    escalation_after_s: null,
    repeat_every_s: null,
  });
  expect([...rule.channel_ids].sort((a: number, b: number) => a - b)).toEqual([...channelIds].sort((a, b) => a - b));
}

test.beforeAll(async () => {
  api = await apiAs();
  saved = await allRules();
});

test.afterAll(async () => {
  await api.patch('/api/auth/me', { language: 'en' }).catch(() => undefined);
  await deleteAllRules().catch(() => undefined);
  for (const id of channels) await api.del(`/api/notification-channels/${id}`).catch(() => undefined);
  // the earlier specs' rules, as they were (new ids)
  for (const r of saved) {
    const body = Object.fromEntries(RULE_FIELDS.map((k) => [k, r[k]]));
    await api.post('/api/alert-rules', body).catch((e) => console.warn(`restore rule ${r.name}: ${e}`));
  }
});

test('no rule: the create-channel dialog offers the default rule (checked) and creates it', async ({ browser }) => {
  await deleteAllRules();
  const page = await notificationsPage(browser, 'en');
  const name = uid('qa-default-rule A');
  const dlg = await fillChannel(page, name);
  const box = dlg.getByRole('checkbox', { name: T('rules.defaultRule.create') });
  await expect(box).toBeChecked();
  await expect(dlg.getByTestId('default-rule-option')).toContainText(T('rules.defaultRule.createHint'));
  await dlg.getByTestId('default-rule-option').scrollIntoViewIfNeeded();
  await shot(page, 'channel-dialog-default-rule--en');
  await dlg.getByRole('button', { name: T('common.save') }).click();
  await expect(dlg).toBeHidden();
  await expect(page.getByText(T('rules.defaultRule.created'))).toBeVisible();

  const ch = (await api.get('/api/notification-channels?page_size=1000')).items.find((c: any) => c.name === name);
  channels.push(ch.id);
  const rules = await allRules();
  expect(rules).toHaveLength(1);
  expectDefaultRule(rules[0], [ch.id]);
  // an ordinary rule on the page
  const row = page.getByTestId('rule-row').filter({ hasText: T('rules.defaultRule.name') });
  await expect(row).toBeVisible();
  await expect(row).toContainText(T('rules.global'));
  await expect(page.getByTestId('no-active-rule')).toHaveCount(0);

  // a rule exists now: the next channel is not offered one
  await fillChannel(page, uid('qa-default-rule unused'));
  await expect(page.getByRole('dialog').getByTestId('default-rule-option')).toHaveCount(0);
  await page.getByRole('dialog').getByRole('button', { name: T('common.cancel') }).click();
  await page.context().close();
});

test('no rule: unticked → the channel is created without a rule, and the banner warns', async ({ browser }) => {
  await deleteAllRules();
  const page = await notificationsPage(browser, 'en');
  const name = uid('qa-default-rule B');
  const dlg = await fillChannel(page, name);
  const box = dlg.getByRole('checkbox', { name: T('rules.defaultRule.create') });
  await box.uncheck();
  await expect(box).not.toBeChecked();
  await dlg.getByRole('button', { name: T('common.save') }).click();
  await expect(dlg).toBeHidden();
  const ch = (await api.get('/api/notification-channels?page_size=1000')).items.find((c: any) => c.name === name);
  expect(ch).toBeTruthy();
  channels.push(ch.id);
  expect(await allRules()).toHaveLength(0);
  await expect(page.getByTestId('no-active-rule')).toContainText(T('rules.defaultRule.bannerTitle'));
  await page.context().close();
});

test('channels but no enabled rule: the banner names the enabled channels and creates the rule in one click', async ({ browser }) => {
  await deleteAllRules();
  const off = uid('qa-default-rule off');
  const offId = await webhookChannel(off, false);
  const enabled = await enabledChannels();
  expect(enabled.length).toBeGreaterThan(0);
  // a disabled rule does not count
  const disabled = await api.post('/api/alert-rules', {
    name: uid('qa-default-rule disabled'),
    sensor_id: null,
    on_statuses: ['down'],
    enabled: false,
    channel_ids: [enabled[0].id],
  });
  const page = await notificationsPage(browser, 'en');
  const banner = page.getByTestId('no-active-rule');
  await expect(banner).toContainText(T('rules.defaultRule.bannerTitle'));
  await expect(banner).toContainText(
    T('rules.defaultRule.bannerHint', { channels: enabled.map((c: any) => c.name).join(', ') }),
  );
  await expect(banner).not.toContainText(off);
  // a rule exists (disabled): the dialog does not offer one
  await fillChannel(page, uid('qa-default-rule unused'));
  await expect(page.getByRole('dialog').getByTestId('default-rule-option')).toHaveCount(0);
  await page.getByRole('dialog').getByRole('button', { name: T('common.cancel') }).click();
  await shot(page, 'banner-no-active-rule--en');

  await banner.getByRole('button', { name: T('rules.defaultRule.bannerButton') }).click();
  await expect(page.getByText(T('rules.defaultRule.created'))).toBeVisible();
  await expect(banner).toHaveCount(0);
  const made = (await allRules()).filter((r: any) => r.id !== disabled.id);
  expect(made).toHaveLength(1);
  expectDefaultRule(
    made[0],
    enabled.map((c: any) => c.id),
  );
  expect(made[0].channel_ids).not.toContain(offId);
  await page.context().close();
});

test('Hebrew: the banner, the dialog option and the rule name are in Hebrew (RTL)', async ({ browser }) => {
  await deleteAllRules();
  const page = await notificationsPage(browser, 'he');
  const banner = page.getByTestId('no-active-rule');
  await expect(banner).toContainText('שום התראה לא תישלח: אין כלל התראה פעיל');
  await expect(banner.getByRole('button', { name: T('rules.defaultRule.bannerButton', {}, 'he') })).toBeVisible();
  await shot(page, 'banner-no-active-rule--he');

  const name = uid('qa-default-rule he');
  const dlg = await fillChannel(page, name, 'he');
  const box = dlg.getByRole('checkbox', { name: 'צור כלל התראה ברירת מחדל' });
  await expect(box).toBeChecked();
  await expect(dlg.getByTestId('default-rule-option')).toContainText(T('rules.defaultRule.createHint', {}, 'he'));
  await dlg.getByTestId('default-rule-option').scrollIntoViewIfNeeded();
  await shot(page, 'channel-dialog-default-rule--he');
  await dlg.getByRole('button', { name: T('common.save', {}, 'he') }).click();
  await expect(dlg).toBeHidden();
  await expect(banner).toHaveCount(0);
  const ch = (await api.get('/api/notification-channels?page_size=1000')).items.find((c: any) => c.name === name);
  channels.push(ch.id);
  const rules = await allRules();
  expect(rules).toHaveLength(1);
  expectDefaultRule(rules[0], [ch.id], 'he');
  await expect(page.getByTestId('rule-row').filter({ hasText: T('rules.defaultRule.name', {}, 'he') })).toBeVisible();
  await page.context().close();
});
