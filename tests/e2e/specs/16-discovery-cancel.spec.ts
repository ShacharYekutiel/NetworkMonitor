import { expect, test } from '@playwright/test';
import { Api, apiAs, defaultGroupId, loginViaApi } from '../lib/helpers';
import { raw, startScan, waitForScan, waitNoRunningScan } from '../lib/lab';

// Phase 2a (g): cancel a running scan (UI); a second scan while one runs -> 409 scan_running (UI + API).
// A /20 (4094 hosts) is accepted; at NETMON_DISCOVERY_RATE = 100 hosts/s it runs for ~40 s.
test.describe.configure({ mode: 'serial' });

let api: Api;
const LONG = { targets: ['127.1.0.0/20'], options: { ping: false, snmp: false, dns: false, tcp: true, tcp_ports: [8080], tcp_timeout_ms: 100 } };

test.beforeAll(async () => {
  api = await apiAs();
  await waitNoRunningScan(api);
});

test('cancel a running scan from its page', async ({ page }) => {
  const s = await startScan(api, { ...LONG, group_id: await defaultGroupId(api) });
  expect(s).toMatchObject({ status: 'running', total_hosts: 4094 });
  await loginViaApi(page, undefined, `/discovery/scans/${s.id}`);
  await expect(page.getByRole('progressbar')).toBeVisible();
  // Progress moves while we watch (WS discovery.progress).
  const scanned0 = (await api.get(`/api/discovery/scans/${s.id}`)).scanned_hosts;
  await expect.poll(async () => (await api.get(`/api/discovery/scans/${s.id}`)).scanned_hosts, { timeout: 15_000 }).toBeGreaterThan(scanned0);

  const t0 = Date.now();
  await page.getByRole('button', { name: 'Cancel scan' }).click();
  // Confirmation dialog ("Stop the scan now? Hosts found so far are kept.")
  const confirm = page.getByRole('dialog');
  await expect(confirm).toContainText('Hosts found so far are kept');
  await confirm.getByRole('button', { name: 'Cancel scan' }).click();
  await expect(page.getByText(/Cancelled/).first()).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole('button', { name: 'Cancel scan' })).toHaveCount(0);
  const done = await waitForScan(api, s.id, 10_000);
  expect(Date.now() - t0).toBeLessThan(10_000);
  expect(done.status).toBe('cancelled');
  expect(done.finished_at).toBeTruthy();
  expect(done.scanned_hosts).toBeLessThan(4094);

  const again = await raw(api, 'POST', `/api/discovery/scans/${s.id}/cancel`);
  expect(again.status).toBe(409);
  expect(again.body.error.code).toBe('scan_not_running');
});

test('second concurrent scan -> 409 scan_running (shown in the UI)', async ({ page }) => {
  const gid = await defaultGroupId(api);
  const running = await startScan(api, { ...LONG, group_id: gid });
  try {
    const r = await raw(api, 'POST', '/api/discovery/scans', { targets: ['127.0.0.21'], group_id: gid });
    expect(r.status).toBe(409);
    expect(r.body.error).toMatchObject({ code: 'scan_running', details: [{ scan_id: running.id }] });

    // Other actions that need no running scan.
    expect((await raw(api, 'DELETE', `/api/discovery/scans/${running.id}`)).body.error.code).toBe('scan_running');
    const imp = await raw(api, 'POST', `/api/discovery/scans/${running.id}/import`, { items: [{ result_id: 1, template_id: 'generic_ping' }] });
    expect(imp.status).toBe(409);
    const prof = await api.post('/api/discovery/profiles', { name: `busy-${running.id}`, targets: ['127.0.0.21'], group_id: gid });
    const pr = await raw(api, 'POST', `/api/discovery/profiles/${prof.id}/run`);
    expect(pr.status).toBe(409);
    expect(pr.body.error.code).toBe('scan_running');
    await api.del(`/api/discovery/profiles/${prof.id}`);

    // UI: submitting the New scan form shows the translated error, stays on the page.
    await loginViaApi(page, undefined, '/discovery');
    const form = page.locator('form').filter({ has: page.getByLabel(/^Targets/) });
    await form.getByLabel(/^Targets/).fill('127.0.0.21');
    await form.getByRole('button', { name: /^Start scan/ }).click();
    await expect(page.getByText(/already running/i).first()).toBeVisible();
    await expect(page).toHaveURL(/\/discovery$/);
    // The running scan is visible in the scans list.
    await expect(page.getByRole('row').filter({ has: page.locator(`a[href="/discovery/scans/${running.id}"]`) })).toContainText('Running');
  } finally {
    await raw(api, 'POST', `/api/discovery/scans/${running.id}/cancel`);
  }
  expect((await waitForScan(api, running.id, 15_000)).status).toBe('cancelled');
});

test('delete a finished scan -> 204, its results are gone', async () => {
  const scans = await api.get('/api/discovery/scans');
  const victim = scans.items.find((s: any) => s.status === 'cancelled');
  expect(victim).toBeTruthy();
  await api.del(`/api/discovery/scans/${victim.id}`);
  expect((await raw(api, 'GET', `/api/discovery/scans/${victim.id}`)).status).toBe(404);
  expect((await raw(api, 'GET', `/api/discovery/scans/${victim.id}/results`)).status).toBe(404);
});
