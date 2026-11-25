import { expect, test } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { horizontalOverflow } from '../lib/lab';
import { setLang } from '../lib/lab2b2';
import { SRC, featureOn, marker, sendSyslog, waitReceivers } from '../lib/lab2b3';

// [1.5.1] 95 — production finding: Logs → Flood protection at ~590 px in Hebrew had its counter
// cards and the "by source" table cut off on the left (the summary grid's implicit track grew to
// the table's width inside an overflow-hidden card). Down to 360 px nothing may stick out of its
// card, the page must not scroll horizontally and the wide table scrolls inside its own container.
test.describe.configure({ mode: 'default' });

let admin: Api;
const M = marker('qa95');

test.beforeAll(async () => {
  admin = await apiAs();
  await featureOn(admin, 'syslog');
  await waitReceivers(admin, ['syslog_udp'], 'listening');
  // duplicates -> the per-source counters ("by source" table) have a row
  sendSyslog({ from: SRC.aos, count: 4, messages: [`<188>Nov 25 12:00:00 sw fan: ${M} fan speed low`] });
  await expect
    .poll(async () => (await admin.get('/api/ingest/status')).syslog_protection.top_sources.length, { timeout: 20_000 })
    .toBeGreaterThan(0);
});

for (const width of [1440, 590, 390, 360]) {
  test(`Hebrew ${width}px: flood protection fits its cards`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await loginViaApi(page, undefined, '/logs?tab=protection');
    await setLang(page, 'he');
    try {
      await expect(page.getByTestId('protection-sources')).toBeVisible();
      const o = await horizontalOverflow(page);
      expect(o.scrollWidth - o.clientWidth, `page overflow at ${width}`).toBeLessThanOrEqual(1);
      const out = await page.evaluate(() => {
        const bad: string[] = [];
        const summary = document.querySelector('[data-testid=protection-summary]')!;
        const card = summary.closest('section')!.getBoundingClientRect();
        const items = [
          ...summary.querySelectorAll(':scope > div > div'),
          ...summary.querySelectorAll('[data-testid=protection-counters] > div'),
          document.querySelector('[data-testid=protection-sources]')!.parentElement!, // its scroller
        ];
        for (const el of items) {
          const r = el.getBoundingClientRect();
          if (r.left < card.left - 1 || r.right > card.right + 1)
            bad.push(`${el.getAttribute('data-testid') ?? el.textContent?.slice(0, 30)}: ${Math.round(r.left)}..${Math.round(r.right)} vs card ${Math.round(card.left)}..${Math.round(card.right)}`);
        }
        return bad;
      });
      expect(out, `clipped at ${width}`).toEqual([]);
    } finally {
      await setLang(page, 'en');
    }
  });
}
