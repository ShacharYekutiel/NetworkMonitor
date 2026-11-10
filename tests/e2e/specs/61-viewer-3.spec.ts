// [3] Viewer permissions for the phase-3 features (docs/architecture.md §13.2.5, §13.11 spec 61; api.md "[3]").
// Backup metadata only (no content/diff/search/export), flows and new devices read-only, reports read/print
// only, no NOC or SSO settings, the ack page says "admins only". Turns on the features it needs and restores.
import { expect, test } from '@playwright/test';
import { VIEWER, apiAs, loginViaApi, type Api } from '../lib/helpers';
import { ensureViewer, raw } from '../lib/lab';
import { PHASE3, SSH, ensureDeviceAt, ensureGroupNamed, problemDevice, setFeatures, shot3, sshCreds, tx, waitBackup } from '../lib/lab3';

test.describe.configure({ mode: 'serial' });
test.skip(!PHASE3, 'phase-3 simulators not running');

let admin: Api;
let viewer: Api;
let dev: any;
let versionId = 0;

test.beforeAll(async () => {
  admin = await apiAs();
  await ensureViewer(admin);
  viewer = await apiAs(VIEWER);
  await setFeatures(admin, { config_backup: true, flows: true, new_device_alerts: true, reports: true, noc: true, chat_ack: true });
  const g = await ensureGroupNamed(admin, 'QA Backups 3', { ssh_credentials: sshCreds() });
  dev = await ensureDeviceAt(admin, g.id, SSH.cx.ip, 'qa3-sw-cx1', { config_backup_driver: 'aruba_cx' });
  await admin.post(`/api/devices/${dev.id}/config/backup`).catch(() => undefined);
  await waitBackup(admin, dev.id, (s) => ['ok', 'unchanged'].includes(s.last_result));
  versionId = (await admin.get(`/api/devices/${dev.id}/config/versions`)).items[0].id;
});

test('API: viewer reads metadata, every content/diff/search/export/write endpoint is 403', async () => {
  expect((await raw(viewer, 'GET', '/api/config-backups')).status).toBe(200);
  expect((await raw(viewer, 'GET', `/api/devices/${dev.id}/config`)).status).toBe(200);
  const v = await raw(viewer, 'GET', `/api/devices/${dev.id}/config/versions`);
  expect(v.status).toBe(200);
  expect(v.body.items[0]).not.toHaveProperty('content');
  const forbidden: [string, string, unknown?][] = [
    ['GET', `/api/devices/${dev.id}/config/versions/${versionId}`],
    ['GET', `/api/devices/${dev.id}/config/versions/${versionId}/download`],
    ['GET', `/api/devices/${dev.id}/config/diff?from=${versionId}`],
    ['GET', '/api/config-backups/search?q=hostname'],
    ['GET', '/api/config-backups/export.zip'],
    ['POST', '/api/config-backups/run', {}],
    ['POST', `/api/devices/${dev.id}/config/backup`],
    ['POST', `/api/devices/${dev.id}/ssh/test`, {}],
    ['POST', `/api/devices/${dev.id}/config/host-key/accept`, { fingerprint: 'SHA256:x' }],
    ['DELETE', `/api/devices/${dev.id}/config/host-key`],
    ['PATCH', '/api/flows/exporters/1', { state: 'ignored' }],
    ['DELETE', '/api/flows/exporters/1'],
    ['POST', '/api/new-devices/acknowledge', { macs: ['00:1b:21:aa:00:01'] }],
    ['POST', '/api/new-devices/allowlist', { prefix: '00:11:22' }],
    ['POST', '/api/new-devices/restart-learning'],
    ['POST', '/api/reports', { name: 'v' }],
    ['POST', '/api/reports/preview', {}],
    ['GET', '/api/kiosk-screens'],
    ['POST', '/api/kiosk-screens', { name: 'v' }],
    ['GET', '/api/sso/kerberos'],
    ['PUT', '/api/sso/oidc', {}],
    ['POST', '/api/sso/oidc/test'],
    ['GET', '/api/chat-ack/telegram/status'],
    ['POST', '/api/chat-ack/acknowledge', { token: 'a1.' + 'A'.repeat(38) }],
  ];
  for (const [m, p, b] of forbidden) {
    const r = await raw(viewer, m, p, b);
    expect(r.status, `${m} ${p}: ${r.text.slice(0, 120)}`).toBe(403);
  }
  for (const p of ['/api/flows/status', '/api/flows/exporters', '/api/flows/top/talkers', '/api/new-devices', '/api/new-devices/status',
    '/api/new-devices/allowlist', '/api/reports', '/api/reports/runs', '/api/chat-links']) {
    expect((await raw(viewer, 'GET', p)).status, `viewer GET ${p}`).toBe(200);
  }
  const flowStatus = await viewer.get('/api/flows/status');
  expect(flowStatus.storage_bytes, 'storage size is admins only').toBeNull();
  // a viewer may create a link code for their own Telegram account
  expect((await raw(viewer, 'POST', '/api/chat-links/codes', { provider: 'telegram' })).status).toBe(201);
});

test('UI: config tab without content/diff/download; no NOC / SSO settings; reports read-only', async ({ page }) => {
  await loginViaApi(page, VIEWER, `/devices/${dev.id}`);
  await page.getByRole('tab', { name: tx('devices.tab.config', /configuration/i) }).click();
  await expect(page.getByText(/aruba_cx|AOS-CX/i).first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText('hostname sw-cx1')).toHaveCount(0);
  await expect(page.getByRole('button', { name: tx('configBackup.diff', /^diff$/i) })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /download/i })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /back up now/i })).toHaveCount(0);
  await shot3(page, '61-viewer-config');
  await page.goto('/config-backups');
  await expect(page.getByRole('button', { name: tx('configBackup.backupAll', /back up all/i) })).toHaveCount(0);
  await expect(page.getByRole('searchbox')).toHaveCount(0);
  await page.goto('/settings?tab=sso');
  await expect(page.getByText(/keytab/i)).toHaveCount(0);
  await page.goto('/reports');
  await expect(page.getByRole('button', { name: /new report|create/i })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /run now/i })).toHaveCount(0);
  await page.goto('/new-devices');
  await expect(page.getByRole('button', { name: tx('newDevices.acknowledge', /acknowledge as known/i) })).toHaveCount(0);
});

test('ack page: viewers see the problem and "admins only"', async ({ page }) => {
  test.setTimeout(120_000);
  await admin.put('/api/settings', { public_base_url: process.env.BASE_URL });
  const p = await problemDevice(admin, '127.0.0.135', 'qa3-viewer-ack');
  try {
    const tg = await admin.post('/api/notification-channels', { name: 'qa3-viewer-hook', type: 'webhook', config: { url: `${process.env.WEBHOOK_URL}/qa3-viewer-ack` } });
    await admin.post('/api/alert-rules', { name: 'qa3 viewer ack', enabled: true, sensor_id: p.sensor.id, on_statuses: ['down'], delay_s: 0, channel_ids: [tg.id] });
    await p.down();
    let url = '';
    await expect.poll(async () => {
      const recs = (await (await fetch(`${process.env.WEBHOOK_URL}/records`)).json()) as any[];
      url = recs.filter((r) => r.path === '/qa3-viewer-ack').map((r) => r.body?.ack_url).find(Boolean) ?? '';
      return url;
    }, { timeout: 60_000, message: 'webhook payload ack_url' }).toMatch(/\/ack#a1\./);
    const r = await viewer.post('/api/chat-ack/resolve', { token: url.split('#')[1] });
    expect(r.can_acknowledge).toBe(false);
    await loginViaApi(page, VIEWER);
    await page.goto(url);
    await expect(page.getByText('qa3-viewer-ack').first()).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText(tx('chatAck.adminOnly', /admins? only|administrators/i)).first()).toBeVisible();
    await expect(page.getByRole('button', { name: tx('chatAck.acknowledge', /^acknowledge$/i) })).toHaveCount(0);
    await shot3(page, '61-viewer-ack');
  } finally {
    await p.ctl('ok');
    await p.stop();
  }
});
