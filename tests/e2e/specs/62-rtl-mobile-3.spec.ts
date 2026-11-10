// [3] Hebrew / RTL and 390 px width for the phase-3 pages (docs/architecture.md §13.10, §13.11 spec 62):
// configs, diffs, IPs and MACs stay `ltr`; no horizontal page overflow on a phone.
import { expect, test, type Page } from '@playwright/test';
import { apiAs, loginViaApi } from '../lib/helpers';
import { horizontalOverflow } from '../lib/lab';
import { PHASE3, SSH, ensureDeviceAt, ensureGroupNamed, setFeatures, shot3, sshCreds, tx, waitBackup } from '../lib/lab3';

test.describe.configure({ mode: 'serial' });
test.skip(!PHASE3, 'phase-3 simulators not running');

async function hebrew(page: Page): Promise<void> {
  await page.request.patch('/api/auth/me', { data: { language: 'he' } });
  await page.addInitScript(() => localStorage.setItem('netmon.lang', 'he'));
}
const dirOf = (page: Page, text: string | RegExp) => page.getByText(text).first().evaluate((el) => getComputedStyle(el).direction);

let devId = 0;
test.beforeAll(async () => {
  const api = await apiAs();
  await setFeatures(api, { config_backup: true, flows: true, new_device_alerts: true, reports: true });
  const g = await ensureGroupNamed(api, 'QA Backups 3', { ssh_credentials: sshCreds() });
  const d = await ensureDeviceAt(api, g.id, SSH.ios.ip, 'qa3-r1', { config_backup_driver: 'cisco_ios' });
  devId = d.id;
  await api.post(`/api/devices/${d.id}/config/backup`).catch(() => undefined);
  await waitBackup(api, d.id, (s) => ['ok', 'unchanged'].includes(s.last_result));
});
test.afterAll(async () => {
  const api = await apiAs();
  await api.patch('/api/auth/me', { language: 'en' });
});

for (const vp of [{ name: 'desktop', width: 1440, height: 900 }, { name: 'phone', width: 390, height: 844 }]) {
  test(`Hebrew ${vp.name}: config viewer ltr inside an rtl page; pages without horizontal overflow`, async ({ page }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await loginViaApi(page, undefined, '/');
    await hebrew(page);
    await page.goto(`/devices/${devId}`);
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
    await page.getByRole('tab', { name: tx('devices.tab.config', /תצורה|configuration/i, {}, 'he') }).click();
    await page.getByRole('row').getByRole('button', { name: tx('configBackup.view', /^(הצגה|view)$/i, {}, 'he'), exact: true }).first().click();
    await expect(page.getByText('hostname r1').first()).toBeVisible({ timeout: 20_000 });
    expect(await dirOf(page, 'hostname r1'), 'config text is ltr').toBe('ltr');
    await shot3(page, `62-config-he-${vp.name}`);
    for (const path of ['/config-backups', '/flows', '/new-devices', '/reports']) {
      await page.goto(path);
      await expect(page.locator('main, [role="main"]').first()).toBeVisible({ timeout: 20_000 });
      await page.waitForLoadState('networkidle').catch(() => undefined);
      const o = await horizontalOverflow(page);
      expect(o.scrollWidth, `${path} at ${vp.width}px: no horizontal page overflow`).toBeLessThanOrEqual(o.clientWidth + 1);
      await shot3(page, `62${path.replace(/\//g, '-')}-he-${vp.name}`);
    }
    // IPs / MACs ltr where present (flows exporters, new devices)
    await page.goto('/flows');
    const ip = page.getByText(/^127\.0\.0\.9\d$/).first();
    if (await ip.isVisible().catch(() => false)) expect(await ip.evaluate((el) => getComputedStyle(el).direction)).toBe('ltr');
    await page.goto('/new-devices');
    const mac = page.getByText(/^([0-9a-f]{2}:){5}[0-9a-f]{2}$/).first();
    if (await mac.isVisible().catch(() => false)) expect(await mac.evaluate((el) => getComputedStyle(el).direction)).toBe('ltr');
  });
}
