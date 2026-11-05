import { expect, test } from '@playwright/test';
import { Api, VIEWER, apiAs, defaultGroupId, loginViaApi } from '../lib/helpers';
import { LAB_SCAN_OPTIONS, collectWsFrames, ensureViewer, raw, startScan, waitForScan } from '../lib/lab';

// Phase 2a (h): viewers see no Discovery nav, get 403 on every discovery endpoint, receive no discovery.*
// WebSocket messages, but can read templates.
test.describe.configure({ mode: 'serial' });

let admin: Api;
let viewer: Api;
let scanId = 0;

test.beforeAll(async () => {
  admin = await apiAs();
  await ensureViewer(admin);
  viewer = await apiAs(VIEWER);
  const scans = await admin.get('/api/discovery/scans');
  scanId = scans.items[0]?.id ?? 1;
});

test('viewer: no Discovery nav, Templates visible; /discovery is forbidden', async ({ page }) => {
  await loginViaApi(page, VIEWER, '/');
  const nav = page.getByRole('complementary');
  await expect(nav.getByRole('link', { name: 'Templates' })).toBeVisible();
  await expect(nav.getByRole('link', { name: 'Discovery' })).toHaveCount(0);
  await page.goto('/discovery');
  await expect(page.getByText(/forbidden|not allowed|permission/i).first()).toBeVisible();
  await expect(page.getByLabel(/^Targets/)).toHaveCount(0);
  await page.goto('/templates');
  await expect(page.getByText('UniFi access point').first()).toBeVisible();
  // No "Apply template" on the device page for viewers.
  const dev = (await viewer.get('/api/devices')).items[0];
  if (dev) {
    await page.goto(`/devices/${dev.id}`);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Apply template' })).toHaveCount(0);
  }
});

test('viewer: 403 forbidden on every discovery endpoint and apply-template; templates readable', async () => {
  const gid = await defaultGroupId(admin);
  const dev = (await admin.get('/api/devices')).items[0];
  const calls: [string, string, unknown?][] = [
    ['GET', '/api/discovery/profiles'],
    ['POST', '/api/discovery/profiles', { name: 'x', targets: ['127.0.0.21'], group_id: gid }],
    ['GET', '/api/discovery/profiles/1'],
    ['PATCH', '/api/discovery/profiles/1', { name: 'y' }],
    ['DELETE', '/api/discovery/profiles/1'],
    ['POST', '/api/discovery/profiles/1/run'],
    ['GET', '/api/discovery/scans'],
    ['POST', '/api/discovery/scans', { targets: ['127.0.0.21'], group_id: gid }],
    ['GET', `/api/discovery/scans/${scanId}`],
    ['POST', `/api/discovery/scans/${scanId}/cancel`],
    ['DELETE', `/api/discovery/scans/${scanId}`],
    ['GET', `/api/discovery/scans/${scanId}/results`],
    ['POST', `/api/discovery/scans/${scanId}/import`, { items: [{ result_id: 1, template_id: 'generic_ping' }] }],
    ['POST', `/api/devices/${dev?.id ?? 1}/apply-template`, { template_id: 'generic_ping' }],
    ['GET', `/api/devices/${dev?.id ?? 1}/snmp/storage`],
  ];
  for (const [m, p, b] of calls) {
    const r = await raw(viewer, m, p, b);
    expect.soft(r.status, `${m} ${p}`).toBe(403);
    expect.soft(r.body?.error?.code, `${m} ${p}`).toBe('forbidden');
  }
  expect((await viewer.get('/api/device-templates')).items.length).toBe(19); // 7 + [2b-1] 2 HPE + [2b-2] 10 vendor templates
  expect((await viewer.get('/api/device-templates/opnsense')).id).toBe('opnsense');
});

test('viewer sockets receive no discovery.* messages; admin sockets do', async ({ browser }) => {
  const vctx = await browser.newContext();
  const vpage = await vctx.newPage();
  const vframes = collectWsFrames(vpage);
  await loginViaApi(vpage, VIEWER, '/');
  await expect(vpage.getByTestId('ws-indicator')).toContainText('Live');

  const actx = await browser.newContext();
  const apage = await actx.newPage();
  const aframes = collectWsFrames(apage);
  await loginViaApi(apage, undefined, '/');
  await expect(apage.getByTestId('ws-indicator')).toContainText('Live');

  const s = await startScan(admin, { targets: ['127.0.0.21-23'], group_id: await defaultGroupId(admin), options: LAB_SCAN_OPTIONS });
  await waitForScan(admin, s.id);
  await expect.poll(() => aframes.types.filter((t) => t === 'discovery.finished').length, { timeout: 10_000 }).toBeGreaterThan(0);
  expect(aframes.types).toContain('discovery.result');
  expect(vframes.types.length).toBeGreaterThan(0); // hello etc.
  expect(vframes.types.filter((t) => t.startsWith('discovery.'))).toEqual([]);
  await vctx.close();
  await actx.close();
});
