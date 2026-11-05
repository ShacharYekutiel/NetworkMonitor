import { expect, test } from '@playwright/test';
import { Api, VIEWER, apiAs, loginViaApi } from '../lib/helpers';
import { ensureViewer, raw } from '../lib/lab';
import { T, ensurePortLab, openPorts, search, tile, type PortLab } from '../lib/lab2b2';

// [2b-2] (h) Viewer (architecture.md §11.7): can view ports, the port modal and search; cannot refresh,
// reload templates, start monitoring or change the inventory flags / ports settings (403 + hidden buttons).
test.describe.configure({ mode: 'serial' });

let admin: Api;
let viewer: Api;
let lab: PortLab;

test.beforeAll(async () => {
  admin = await apiAs();
  lab = await ensurePortLab(admin);
  await ensureViewer(admin);
  viewer = await apiAs(VIEWER);
});

test('viewer reads ports, a port, search (MACs/IPs/hostnames are shown to every user)', async () => {
  const dp = await viewer.get(`/api/devices/${lab.aos.id}/ports`);
  expect(dp.ports.length).toBe(12);
  expect(dp.ports.find((p: any) => p.if_index === 1).macs[0]).toMatchObject({ ip: '10.0.0.11', hostname: 'pc-reception.lan' });
  expect((await raw(viewer, 'GET', `/api/devices/${lab.aos.id}/ports/9?include_uplink_macs=true`)).status).toBe(200);
  const r = await search(viewer, { q: '00:50:56:01:00:07' });
  expect(r.status).toBe(200);
  expect(r.body.items).toHaveLength(1);
  expect((await search(viewer, { ip: '10.0.0.12' })).status).toBe(200);
  expect((await raw(viewer, 'GET', '/api/device-templates')).status).toBe(200);
});

test('viewer writes -> 403 forbidden', async () => {
  const checks: [string, string, unknown?][] = [
    ['POST', `/api/devices/${lab.aos.id}/ports/refresh`],
    ['POST', `/api/devices/${lab.legacy.id}/ports/refresh`],
    ['POST', '/api/device-templates/reload'],
    ['POST', '/api/sensors', { device_id: lab.aos.id, type: 'snmp_interface', name: '7', config: { if_index: 7, if_name: '7' } }],
    ['PATCH', `/api/devices/${lab.aos.id}`, { port_inventory: 'off' }],
    ['PATCH', `/api/devices/${lab.legacy.id}`, { arp_collect: 'auto' }],
    ['PUT', '/api/settings', { port_inventory_min: 0 }],
    ['POST', `/api/devices/${lab.aos.id}/apply-template`, { template_id: 'aruba_cx' }],
  ];
  for (const [m, p, b] of checks) {
    const r = await raw(viewer, m, p, b);
    expect.soft(r.status, `${m} ${p}`).toBe(403);
    if (r.status === 403) expect.soft(r.body.error.code, `${m} ${p}`).toBe('forbidden');
  }
  // nothing changed
  const d = await admin.get(`/api/devices/${lab.aos.id}`);
  expect(d.port_inventory).toBe('auto');
});

test('viewer UI: ports tab without Refresh / Enable / Start monitoring; Find works', async ({ page }) => {
  await loginViaApi(page, VIEWER);
  await openPorts(page, lab.aos.id);
  await expect(tile(page, 7)).toBeVisible();
  await expect(page.getByRole('button', { name: T('ports.refresh') })).toHaveCount(0);
  await tile(page, 7).click();
  const m = page.getByTestId('port-modal');
  await expect(m).toBeVisible();
  await expect(m).toContainText('00:50:56:01:00:07');
  await expect(m.getByRole('button', { name: T('ports.startMonitoring') })).toHaveCount(0);
  await page.keyboard.press('Escape');
  await page.getByRole('tab', { name: T('ports.view.list') }).click();
  await expect(page.getByTestId('port-list')).toBeVisible();
  // a device without effective inventory: explanation, but no "Enable port inventory" for viewers
  await page.goto(`/devices/${lab.legacy.id}?tab=ports`);
  await expect(page.getByTestId('ports-tab')).toBeVisible();
  await expect(page.getByRole('button', { name: T('ports.enable') })).toHaveCount(0);
  await page.goto('/find?q=00:50:56:01:00:07');
  await expect(page.getByTestId('find-best')).toContainText(lab.aos.name);
  await page.goto('/templates');
  await expect(page.getByRole('button', { name: T('templates.reload') })).toHaveCount(0);
});

test('admin sees Enable port inventory on a non-effective device and it PATCHes "on"', async ({ page }) => {
  const before = await admin.get(`/api/devices/${lab.legacy.id}`);
  try {
    await loginViaApi(page);
    await page.goto(`/devices/${lab.legacy.id}?tab=ports`);
    const btn = page.getByRole('button', { name: T('ports.enable') });
    await expect(btn).toBeVisible();
    const req = page.waitForRequest((r) => r.url().endsWith(`/api/devices/${lab.legacy.id}`) && r.method() === 'PATCH');
    await btn.click();
    expect((await req).postDataJSON()).toEqual({ port_inventory: 'on' });
    await expect.poll(async () => (await admin.get(`/api/devices/${lab.legacy.id}`)).ports_effective).toBe(true);
  } finally {
    await admin.patch(`/api/devices/${lab.legacy.id}`, { port_inventory: before.port_inventory });
  }
});
