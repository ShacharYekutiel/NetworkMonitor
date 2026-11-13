// [3b] Hebrew / RTL and 390 px (docs/architecture.md §14.3.5, §14.10, §14.11 spec 78): a Hebrew question -> the
// model is asked to answer in Hebrew -> Hebrew answer rendered rtl with ltr IPs and code; the ask page, the
// Settings → Assistant tab and an incident card without horizontal overflow at 390 px (en and he).
import { expect, test, type Page } from '@playwright/test';
import { apiAs, loginViaApi, type Api } from '../lib/helpers';
import { horizontalOverflow } from '../lib/lab';
import { PHASE3B, askOk, ensureAssistant, lastSeq, mock, modelCalls, shot3b , problem } from '../lib/lab3b';

// default mode (not serial): one worker runs the tests in order; a failure restarts the worker (beforeAll is
// idempotent) instead of skipping the remaining tests, so every contract item gets a verdict
test.describe.configure({ mode: 'default' });
test.skip(!PHASE3B, 'phase-3b Bedrock mock not running');

let api: Api;
let p: Awaited<ReturnType<typeof problem>>;
const IP = '127.0.0.149';
const HE_Q = 'אילו מכשירים למטה עכשיו?';

async function hebrew(page: Page): Promise<void> {
  await page.request.patch('/api/auth/me', { data: { language: 'he' } });
  await page.addInitScript(() => localStorage.setItem('netmon.lang', 'he'));
}

test.beforeAll(async () => {
  test.setTimeout(120_000);
  api = await apiAs();
  await ensureAssistant(api);
  await mock.clearModes();
  p = await problem(api, IP, 'qa3b-rtl-web');
  await p.down();
});
test.afterAll(async () => {
  await api.patch('/api/auth/me', { language: 'en' });
  await p?.ctl('ok'); await p?.stop();
});

test('language detection: Hebrew question -> "Answer in Hebrew." wrapper, start.language he; mixed short -> he', async () => {
  const since = await lastSeq();
  const { res, msg } = await askOk(api, HE_Q);
  expect(res.start.language).toBe('he');
  const c1 = (await modelCalls(since))[0];
  expect(c1.question).toMatch(/Answer in Hebrew/);
  expect(msg.content).toMatch(/[֐-׿]/);
  const conv = await api.get(`/api/assistant/conversations/${msg.conversation_id}`);
  expect(conv.language).toBe('he');
  const { res: r2 } = await askOk(api, 'מה קרה ל-core-sw אתמול?');
  expect(r2.start.language).toBe('he');
  const { res: r3 } = await askOk(api, 'Which devices are down?');
  expect(r3.start.language).toBe('en');
});

for (const vp of [{ name: 'desktop', width: 1440, height: 900 }, { name: 'phone', width: 390, height: 844 }]) {
  test(`Hebrew ${vp.name}: rtl answer with ltr IPs/code; ask, settings and incident pages without horizontal overflow`, async ({ page }) => {
    test.setTimeout(150_000);
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await loginViaApi(page, undefined, '/');
    await hebrew(page);
    await page.goto('/assistant');
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
    await page.getByTestId('question').fill(HE_Q);
    await page.getByTestId('question').press('Enter');
    const answer = page.getByTestId('answer').last();
    await expect(answer).toContainText('נמצאו', { timeout: 60_000 });
    await expect(answer).toHaveAttribute('data-status', 'complete', { timeout: 60_000 });
    const para = answer.getByText(/נמצאו/).first();
    expect(await para.evaluate((el) => getComputedStyle(el).direction)).toBe('rtl');
    const ip = answer.getByText(IP).first();
    await expect(ip).toBeVisible();
    expect(await ip.evaluate((el) => getComputedStyle(el).direction), 'IPs are ltr').toBe('ltr');
    const code = answer.locator('code').first();
    if (await code.count()) expect(await code.evaluate((el) => getComputedStyle(el).direction)).toBe('ltr');
    await expect(page.getByTestId('ai-notice').first()).toContainText(/AI/);
    await shot3b(page, `78-ask-he-${vp.name}`);
    for (const path of ['/assistant', '/settings?tab=assistant', '/assistant/usage']) {
      await page.goto(path);
      await page.waitForLoadState('networkidle').catch(() => undefined);
      const o = await horizontalOverflow(page);
      expect(o.scrollWidth, `${path} at ${vp.width}px: no horizontal page overflow`).toBeLessThanOrEqual(o.clientWidth + 1);
      await shot3b(page, `78${path.replace(/[/?=]/g, '-')}-he-${vp.name}`);
    }
    const inc = await api.get('/api/incidents?page_size=1').catch(() => null);
    if (inc?.items?.length) {
      await page.goto(`/incidents/${inc.items[0].id}`);
      await page.waitForLoadState('networkidle').catch(() => undefined);
      const o = await horizontalOverflow(page);
      expect(o.scrollWidth, `incident page at ${vp.width}px`).toBeLessThanOrEqual(o.clientWidth + 1);
      await shot3b(page, `78-incident-he-${vp.name}`);
    }
  });
}

test('English phone: conversation list and question box usable at 390 px; no overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await loginViaApi(page, undefined, '/assistant');
  await expect(page.getByTestId('question')).toBeVisible();
  const box = await page.getByTestId('question').boundingBox();
  expect(box!.width).toBeGreaterThan(250);
  const o = await horizontalOverflow(page);
  expect(o.scrollWidth).toBeLessThanOrEqual(o.clientWidth + 1);
  await shot3b(page, '78-ask-en-phone');
});
