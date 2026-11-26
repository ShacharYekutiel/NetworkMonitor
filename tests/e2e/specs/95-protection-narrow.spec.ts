import { expect, test, type Page } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { horizontalOverflow } from '../lib/lab';
import { setLang } from '../lib/lab2b2';
import { SRC, featureOn, marker, sendSyslog, waitReceivers } from '../lib/lab2b3';

// [1.5.1] 95 — production finding: Logs → Flood protection at ~590 px in Hebrew had its counter
// cards and the "by source" table cut off on the left (the summary grid's implicit track grew to
// the table's width inside an overflow-hidden card). Down to 360 px nothing may stick out of its
// card, the page must not scroll horizontally and the wide table scrolls inside its own container.
//
// [1.5.1 acceptance round] The "busiest sources" (top talkers) table was 890 px wide inside a
// scroller: at 590 px only source / program / severity showed, the sticky "Create rule" column
// covered the program and the "unknown" badge was cut. Now the table is shown only where it fits
// (no sideways scroll under the sticky column) and each source is a card below that. With a real
// row (an unknown sender, so the badge shows) every field and the button must be in view, uncut,
// and the button must not overlap any text.
test.describe.configure({ mode: 'default' });

let admin: Api;
const M = marker('qa95');
const PROGRAM = 'qa95prog';

test.beforeAll(async () => {
  admin = await apiAs();
  await featureOn(admin, 'syslog');
  await waitReceivers(admin, ['syslog_udp'], 'listening');
  // duplicates -> the per-source counters ("by source" table) have a row
  sendSyslog({ from: SRC.aos, count: 4, messages: [`<188>Nov 25 12:00:00 sw fan: ${M} fan speed low`] });
  // an unknown sender with its own program -> a top-talkers row with the "unknown" badge
  sendSyslog({ from: SRC.unknown, messages: [`<187>Nov 25 12:00:01 stray ${PROGRAM}: ${M} link flap`] });
  await expect
    .poll(async () => (await admin.get('/api/ingest/status')).syslog_protection.top_sources.length, { timeout: 20_000 })
    .toBeGreaterThan(0);
  await expect
    .poll(
      async () =>
        (await admin.get('/api/syslog/top-talkers?hours=24&limit=100')).items.filter(
          (x: any) => x.source_ip === SRC.unknown && x.program === PROGRAM,
        ).length,
      { timeout: 20_000 },
    )
    .toBe(1);
});

type Box = { left: number; right: number; top: number; bottom: number };

/** The unknown sender's top-talkers row (table) or card, measured. */
async function talker(page: Page) {
  return page.evaluate(
    ({ ip, program }) => {
      const box = (el: Element): Box => {
        const r = el.getBoundingClientRect();
        return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
      };
      const cut = (el: Element) => (el as HTMLElement).scrollWidth > (el as HTMLElement).clientWidth + 1;
      const cards = document.querySelector('[data-testid=top-talkers-cards]');
      const table = document.querySelector('[data-testid=top-talkers]');
      const units = cards
        ? [...cards.querySelectorAll('[data-testid=top-talkers-card]')]
        : [...(table?.querySelectorAll('tbody > tr') ?? [])];
      const unit = units.find((u) => u.textContent!.includes(ip) && u.textContent!.includes(program));
      if (!unit) return null;
      const fields = cards
        ? [...unit.querySelectorAll('dd')]
        : [...unit.querySelectorAll('td')].filter((td) => !td.querySelector('[data-testid=talker-create-rule]'));
      const labels = cards ? [...unit.querySelectorAll('dt')] : [];
      const button = unit.querySelector('[data-testid=talker-create-rule]')!;
      // text boxes: each field's inline content (what a reader sees), plus the labels
      const texts = [...fields, ...labels].flatMap((el) => [...el.querySelectorAll('*'), el]).filter((el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && el.children.length === 0 && el.textContent!.trim() !== '';
      });
      const scroller = table?.parentElement as HTMLElement | undefined;
      const badge = [...unit.querySelectorAll('span')].find((s) => s.children.length === 0 && /לא מוכר|Unknown/i.test(s.textContent!));
      return {
        layout: cards ? 'cards' : 'table',
        fieldCount: fields.length,
        fields: fields.map((f) => ({ text: f.textContent!.trim(), box: box(f), cut: cut(f) })),
        texts: texts.map((el) => ({ text: el.textContent!.trim(), box: box(el) })),
        button: box(button),
        badge: badge ? { box: box(badge), cut: cut(badge), parent: box(badge.closest('dd, td')!) } : null,
        scroller: scroller ? { scrollWidth: scroller.scrollWidth, clientWidth: scroller.clientWidth } : null,
      };
    },
    { ip: SRC.unknown, program: PROGRAM },
  );
}

const overlaps = (a: Box, b: Box) =>
  Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;

for (const width of [1440, 1024, 800, 590, 390, 360]) {
  test(`Hebrew ${width}px: flood protection fits its cards, busiest sources fully visible`, async ({ page }) => {
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

      // ---- busiest sources: the unknown sender's row
      await expect(page.getByTestId('talker-create-rule').first()).toBeVisible();
      const r = await talker(page);
      expect(r, `talker row at ${width}`).not.toBeNull();
      const row = r!;
      if (width <= 590) expect(row.layout, `cards at ${width}`).toBe('cards');
      // all six fields: source, program, severity, rows, share, last seen
      expect(row.fieldCount, `fields at ${width}`).toBe(6);
      const problems: string[] = [];
      const inView = (b: Box) => b.left >= -0.5 && b.right <= width + 0.5;
      for (const f of row.fields) {
        if (!f.text) problems.push(`empty field at ${width}`);
        if (!inView(f.box)) problems.push(`field "${f.text}" outside the viewport: ${Math.round(f.box.left)}..${Math.round(f.box.right)}`);
        if (f.cut) problems.push(`field "${f.text}" cut`);
      }
      for (const x of row.texts) {
        if (!inView(x.box)) problems.push(`text "${x.text}" outside the viewport: ${Math.round(x.box.left)}..${Math.round(x.box.right)}`);
        if (overlaps(x.box, row.button)) problems.push(`button overlaps "${x.text}"`);
      }
      if (!inView(row.button)) problems.push(`button outside the viewport: ${Math.round(row.button.left)}..${Math.round(row.button.right)}`);
      // the "unknown" badge next to the source is whole
      expect(row.badge, `unknown badge at ${width}`).not.toBeNull();
      const b = row.badge!;
      if (b.cut || b.box.left < b.parent.left - 0.5 || b.box.right > b.parent.right + 0.5)
        problems.push(`unknown badge cut: ${Math.round(b.box.left)}..${Math.round(b.box.right)} in ${Math.round(b.parent.left)}..${Math.round(b.parent.right)}`);
      // the table is only shown where it fits: nothing scrolls under the sticky actions column
      if (row.layout === 'table' && row.scroller && row.scroller.scrollWidth > row.scroller.clientWidth + 1)
        problems.push(`table scrolls sideways at ${width}: ${row.scroller.scrollWidth} > ${row.scroller.clientWidth}`);
      expect(problems, `busiest sources at ${width}`).toEqual([]);
    } finally {
      await setLang(page, 'en');
    }
  });
}
