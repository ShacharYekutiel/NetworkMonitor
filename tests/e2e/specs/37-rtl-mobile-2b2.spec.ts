import { expect, test, type Page } from '@playwright/test';
import { Api, apiAs, loginViaApi } from '../lib/helpers';
import { horizontalOverflow } from '../lib/lab';
import { T, box, ensurePortLab, openPorts, setLang, shot2b2, tile, type PortLab } from '../lib/lab2b2';

// [2b-2] (i) Hebrew RTL + phone width (390 px): the 52-port faceplate scrolls inside its card, no page
// overflow; port list, modal, Find and Templates in en/he without page overflow; LTR MACs/IPs; no missing
// i18n keys on the new screens. Screenshots en + he (desktop + phone) to $QA2B2_SHOT_DIR.
// default mode: a failure does not hide the results of the later tests (state is re-derived per test)
test.describe.configure({ mode: 'default' });

let api: Api;
let lab: PortLab;

test.beforeAll(async () => {
  api = await apiAs();
  lab = await ensurePortLab(api);
});
test.afterAll(async () => {
  await api.patch('/api/auth/me', { language: 'en' }).catch(() => undefined);
});

async function noOverflow(page: Page, what: string) {
  const o = await horizontalOverflow(page);
  expect(o.scrollWidth, `${what}: page scrollWidth ${o.scrollWidth} > clientWidth ${o.clientWidth}`).toBeLessThanOrEqual(o.clientWidth);
}
async function noRawKeys(page: Page, what: string) {
  const text = await page.locator('main').innerText();
  const raw = text.match(/\b(ports|find|health|templates|inventoryMode|settings\.ports)\.[a-zA-Z_.]+\b/g) ?? [];
  expect(raw, `${what}: untranslated keys`).toEqual([]);
}

for (const lang of ['en', 'he'] as const) {
  test(`52-port faceplate at 390 px (${lang}): scrolls inside the card, no page overflow`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await loginViaApi(page);
    await page.request.patch('/api/auth/me', { data: { language: lang } });
    await openPorts(page, lab.aos52.id);
    await expect(page.locator('html')).toHaveAttribute('dir', lang === 'he' ? 'rtl' : 'ltr');
    await expect(tile(page, 52)).toHaveCount(1);
    await expect(page.locator('[data-testid="faceplate"] button[data-if-index]')).toHaveCount(52);
    await expect(page.locator('[data-block="m1-uplink"] button')).toHaveCount(4); // 49-52 at 10 G
    const sc = page.getByTestId('faceplate-scroll');
    const m = await sc.evaluate((e) => ({ sw: e.scrollWidth, cw: e.clientWidth, dir: e.getAttribute('dir'), ox: getComputedStyle(e).overflowX }));
    expect(m.dir).toBe('ltr');
    expect(['auto', 'scroll']).toContain(m.ox);
    expect(m.sw, 'faceplate wider than its card').toBeGreaterThan(m.cw);
    await noOverflow(page, `ports 52 ${lang}`);
    // tiles 22 px below 640 px
    const b1 = await box(page, 1);
    expect(Math.round(b1.width)).toBe(22);
    // LTR: port 1 starts at the left edge of the scroller, even in Hebrew
    const scBox = (await sc.boundingBox())!;
    expect(b1.x - scBox.x).toBeLessThan(60);
    await sc.evaluate((e) => (e.scrollLeft = e.scrollWidth));
    await expect(tile(page, 52)).toBeInViewport();
    await noOverflow(page, `ports 52 scrolled ${lang}`);
    await shot2b2(page, `phone-faceplate52-${lang}`);
    await noRawKeys(page, `ports ${lang}`);
  });

  test(`phone (${lang}): port list, modal, Find, Templates without page overflow`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await loginViaApi(page);
    await page.request.patch('/api/auth/me', { data: { language: lang } });
    await openPorts(page, lab.aos.id, 'list');
    await noOverflow(page, `list ${lang}`);
    await shot2b2(page, `phone-list-${lang}`);
    await openPorts(page, lab.aos.id, 'faceplate', 2);
    const modal = page.getByTestId('port-modal');
    await expect(modal).toBeVisible();
    await noOverflow(page, `modal ${lang}`);
    const mac = modal.getByText('00:00:0c:00:00:03').first();
    await expect(mac).toBeVisible();
    expect(await mac.evaluate((e) => getComputedStyle(e).direction)).toBe('ltr');
    await shot2b2(page, `phone-modal-${lang}`);
    await page.goto('/find?q=005056');
    await expect(page.getByTestId('find-results')).toBeVisible();
    await noOverflow(page, `find ${lang}`);
    await shot2b2(page, `phone-find-${lang}`);
    await page.goto('/templates');
    await expect(page.locator('header code', { hasText: /^checkpoint_gaia$/ })).toBeVisible();
    await noOverflow(page, `templates ${lang}`);
  });

  test(`desktop screenshots (${lang}): faceplate, modal, list, Find, Templates`, async ({ page }) => {
    await loginViaApi(page);
    await page.request.patch('/api/auth/me', { data: { language: lang } });
    await openPorts(page, lab.aos.id);
    await expect(page.locator('html')).toHaveAttribute('dir', lang === 'he' ? 'rtl' : 'ltr');
    await expect(page.getByTestId('faceplate-legend')).toContainText(T('ports.legend.connected', {}, lang));
    await noRawKeys(page, `faceplate ${lang}`);
    await shot2b2(page, `desktop-faceplate-${lang}`);
    await openPorts(page, lab.cw.id);
    await shot2b2(page, `desktop-faceplate-comware-${lang}`);
    await openPorts(page, lab.aos.id, 'faceplate', 1);
    await expect(page.getByTestId('port-modal')).toContainText('pc-reception.lan');
    await noRawKeys(page, `modal ${lang}`);
    await shot2b2(page, `desktop-modal-${lang}`);
    await page.keyboard.press('Escape');
    await openPorts(page, lab.aos.id, 'list');
    await noRawKeys(page, `list ${lang}`);
    await shot2b2(page, `desktop-list-${lang}`);
    await page.goto('/find?q=02:00:00:00:09:01');
    await expect(page.getByTestId('find-results')).toBeVisible();
    await noRawKeys(page, `find ${lang}`);
    await shot2b2(page, `desktop-find-${lang}`);
    await page.goto('/templates');
    await expect(page.locator('header code', { hasText: /^aruba_wifi$/ })).toBeVisible();
    await noRawKeys(page, `templates ${lang}`);
    await shot2b2(page, `desktop-templates-${lang}`);
    if (lang === 'he') {
      // Hebrew device page tabs / nav
      await expect(page.getByRole('navigation').first()).toContainText(T('nav.find', {}, 'he'));
    }
  });
}

test('Hebrew device form: Port inventory / Collect ARP selects with the effective state', async ({ page }) => {
  await loginViaApi(page);
  await setLang(page, 'he').catch(() => undefined);
  await page.goto(`/devices/${lab.aos.id}`);
  await page.request.patch('/api/auth/me', { data: { language: 'he' } });
  await page.reload();
  await page.getByRole('button', { name: T('common.edit', {}, 'he') }).first().click();
  const dlg = page.getByRole('dialog');
  await expect(dlg.getByLabel(T('device.portInventory', {}, 'he'))).toBeVisible();
  await expect(dlg.getByLabel(T('device.arpCollect', {}, 'he'))).toBeVisible();
  await expect(dlg).toContainText(T('inventoryMode.auto', {}, 'he'));
  await shot2b2(page, 'device-form-inventory-he');
  await page.keyboard.press('Escape');
});
