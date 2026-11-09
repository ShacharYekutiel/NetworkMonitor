import { expect, test } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { horizontalOverflow } from '../lib/lab';
import { ensurePortLab, setLang, type PortLab } from '../lib/lab2b2';
import { SRC, featureOn, marker, sendSyslog, shot2b3, waitItems, waitReceivers } from '../lib/lab2b3';

// [2b-3] 50 — Hebrew/RTL and 390 px width for the new pages (architecture.md §12.13): the page is rtl, log
// message cells stay dir="ltr", no horizontal page overflow, he locale has every new key namespace.
test.describe.configure({ mode: 'default' });

let admin: Api;
let lab: PortLab;
const M = marker('qa50');

test.beforeAll(async () => {
  admin = await apiAs();
  lab = await ensurePortLab(admin);
  await featureOn(admin, 'traps', 'syslog', 'event_alerts', 'timeline', 'audit');
  await waitReceivers(admin, ['syslog_udp'], 'listening');
  sendSyslog({ from: SRC.aos, messages: [`<187>Nov  8 12:00:00 sw rtl: ${M} interface Ten-GigabitEthernet1/0/49 down`] });
  await waitItems(admin, 'syslog', { q: M }, (it) => it.length === 1);
});

test('he locale has the new key namespaces', async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');
  const dir = path.join(__dirname, '..', '..', '..', 'frontend', 'src', 'i18n', 'locales');
  const en = JSON.parse(fs.readFileSync(path.join(dir, 'en.json'), 'utf8'));
  const he = JSON.parse(fs.readFileSync(path.join(dir, 'he.json'), 'utf8'));
  for (const ns of ['features', 'logs', 'syslogSeverity', 'syslogFacility', 'traps', 'unknownSources', 'ingest', 'timeline', 'audit', 'ldap', 'tokens', 'flapping', 'siem']) {
    expect.soft(en[ns], `en ${ns}`).toBeTruthy();
    expect.soft(he[ns], `he ${ns}`).toBeTruthy();
    if (en[ns] && he[ns] && typeof en[ns] === 'object') {
      const missing = Object.keys(en[ns]).filter((k) => !(k in he[ns]));
      expect.soft(missing, `he ${ns} missing keys`).toEqual([]);
    }
  }
  for (let i = 0; i < 8; i++) expect.soft(he.syslogSeverity?.[String(i)], `syslogSeverity.${i}`).toBeTruthy();
  for (const k of ['trap', 'syslog', 'port_flap']) expect.soft(he.sensorType?.[k], `sensorType.${k}`).toBeTruthy();
});

for (const width of [1440, 390]) {
  test(`Hebrew ${width}px: logs, audit, timeline, settings features`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await loginViaApi(page, undefined, '/logs');
    await setLang(page, 'he');
    try {
      await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
      const cell = page.locator('[dir="ltr"]').filter({ hasText: M }).first();
      await expect(cell).toBeVisible();
      expect(await horizontalOverflow(page).then((o) => o.scrollWidth - o.clientWidth)).toBeLessThanOrEqual(1);
      await shot2b3(page, `50-logs-he-${width}`);
      for (const p of ['/audit', `/devices/${lab.aos.id}?tab=timeline`, '/settings']) {
        await page.goto(p);
        await expect(page.locator('main, [role="main"]').first()).toBeVisible();
        const o = await horizontalOverflow(page);
        expect.soft(o.scrollWidth - o.clientWidth, `${p} overflow at ${width}`).toBeLessThanOrEqual(1);
        await shot2b3(page, `50-${p.replace(/[^a-z]+/g, '-')}-he-${width}`);
      }
    } finally {
      await setLang(page, 'en');
    }
  });
}
