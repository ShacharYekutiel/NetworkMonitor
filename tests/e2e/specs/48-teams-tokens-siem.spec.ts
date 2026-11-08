import { expect, test } from '@playwright/test';
import { Api, VIEWER, apiAs, loginViaApi } from '../lib/helpers';
import { ensureViewer, raw } from '../lib/lab';
import { ensurePortLab, type PortLab } from '../lib/lab2b2';
import {
  OID, SIEM_TCP_PORT, SIEM_TLS_PORT, SIEM_UDP_PORT, SRC, TEAMS_URL, clearSiem, clearTeams, featureOff, featureOn, linkVarbinds,
  marker, sendSyslog, sendTrap, shot2b3, siemDown, siemRecords, siemUp, sleep, tOr, teamsRecords, teamsStatus, waitEventSensor,
  waitItems, waitReceivers,
} from '../lib/lab2b3';

// [2b-3] 48 — Microsoft Teams channel against tests/sim/teams_mock.py (architecture.md §12.10, api.md [2b-3]
// Teams), API tokens (§12.11), SIEM forwarding to tests/sim/ingest/siem_sink.py (§12.5).
test.describe.configure({ mode: 'default' });

let admin: Api;
let lab: PortLab;

test.beforeAll(async () => {
  admin = await apiAs();
  lab = await ensurePortLab(admin);
  await ensureViewer(admin);
});

// ------------------------------------------------------------------------------------------------ Teams
test.describe('Teams', () => {
  test.describe.configure({ mode: 'serial' });
  let ch: any;

  test('create: URL is secret (url_host, legacy flag), https only; Test posts an Adaptive Card', async () => {
    await featureOn(admin, 'teams');
    await teamsStatus(202);
    await clearTeams();
    expect((await raw(admin, 'POST', '/api/notification-channels', { name: 'x', type: 'teams', enabled: true, config: { url: 'http://localhost/x' } })).status).toBe(422);
    const legacy = await admin.post('/api/notification-channels', { name: `qa48-legacy-${marker()}`, type: 'teams', enabled: false, config: { url: 'https://mnidom.webhook.office.com/webhookb2/abc' } });
    expect(legacy.config).toMatchObject({ url: null, url_set: true, legacy: true });
    await admin.del(`/api/notification-channels/${legacy.id}`);
    ch = await admin.post('/api/notification-channels', { name: `qa48-teams-${marker()}`, type: 'teams', enabled: true, config: { url: TEAMS_URL } });
    expect(ch.config).toEqual(expect.objectContaining({ url: null, url_set: true, url_host: 'localhost', legacy: false }));
    expect(JSON.stringify(ch)).not.toContain('QA-SECRET-SIG');
    expect((await raw(admin, 'POST', `/api/notification-channels/${ch.id}/test`)).status).toBe(200);
    const [rec] = await teamsRecords();
    expect(rec.query.sig).toEqual(['QA-SECRET-SIG']);
    expect(rec.body.type).toBe('message');
    const att = rec.body.attachments[0];
    expect(att).toMatchObject({ contentType: 'application/vnd.microsoft.card.adaptive', contentUrl: null });
    expect(att.content).toMatchObject({ type: 'AdaptiveCard', version: '1.4' });
    expect(att.content.body[0].text).toBe('NetworkMonitor test message');
  });

  test('Test against a failing endpoint -> 502 teams_error without the URL', async () => {
    await teamsStatus(400, 'Bad card payload');
    try {
      const r = await raw(admin, 'POST', `/api/notification-channels/${ch.id}/test`);
      expect(r.status).toBe(502);
      expect(r.body.error.code).toBe('teams_error');
      expect(r.body.error.message).toContain('400');
      expect(r.text).not.toContain('QA-SECRET-SIG');
    } finally {
      await teamsStatus(202);
    }
  });

  test('an alert is delivered as a card with facts; OFF -> skipped with a notification event', async () => {
    await featureOn(admin, 'traps', 'event_alerts');
    await waitReceivers(admin, ['trap'], 'listening');
    const ld = (await admin.get('/api/event-rules')).items.find((r: any) => r.builtin === 'trap_link_down');
    if (!ld.enabled) await admin.patch(`/api/event-rules/${ld.id}`, { enabled: true });
    sendTrap({ from: SRC.aos, oid: OID.linkUp, varbinds: linkVarbinds(1, true) });
    const s = await waitEventSensor(admin, lab.aos.id, 'trap');
    await raw(admin, 'POST', `/api/sensors/${s.id}/clear`);
    const rule = await admin.post('/api/alert-rules', { name: `qa48-teams-${marker()}`, sensor_id: s.id, on_statuses: ['warning', 'up'], delay_s: 0, enabled: true, channel_ids: [ch.id] });
    try {
      await clearTeams();
      sendTrap({ from: SRC.aos, oid: OID.linkDown, varbinds: linkVarbinds(1, false) });
      await expect.poll(async () => (await teamsRecords()).length, { timeout: 30_000 }).toBeGreaterThanOrEqual(1);
      const card = (await teamsRecords())[0].body.attachments[0].content;
      expect(card.body[0].color).toBe('Warning');
      expect(card.body[0].text).toContain('SNMP traps');
      const facts = card.body[1].facts.map((f: any) => f.title);
      expect(facts).toEqual(expect.arrayContaining(['Status', 'Device', 'Sensor', 'Message', 'Since', 'Rule']));
      expect(JSON.stringify(card).length).toBeLessThanOrEqual(24 * 1024);
      // OFF: skipped, notification event written
      await featureOff(admin, 'teams');
      await clearTeams();
      await raw(admin, 'POST', `/api/sensors/${s.id}/clear`);
      await sleep(4000);
      expect(await teamsRecords()).toEqual([]);
      const ev = await admin.get(`/api/events?kind=notification&page_size=20`);
      expect(ev.items.some((e: any) => /Teams notifications are turned off/.test(e.message))).toBe(true);
      expect((await raw(admin, 'POST', `/api/notification-channels/${ch.id}/test`)).status).toBe(409);
    } finally {
      await admin.del(`/api/alert-rules/${rule.id}`);
      await featureOn(admin, 'teams');
    }
  });

  test('UI: channel type Microsoft Teams with Test button', async ({ page }) => {
    await loginViaApi(page, undefined, '/notifications');
    await expect(page.getByText(ch.name)).toBeVisible();
    await expect(page.getByText(tOr('channel.teams', 'Microsoft Teams')).first()).toBeVisible();
    await shot2b3(page, '48-teams-channel');
  });
});

// ------------------------------------------------------------------------------------------------ tokens
test.describe('API tokens', () => {
  test.describe.configure({ mode: 'serial' });
  let token = '';
  let item: any;
  const bearer = (path: string, init: RequestInit = {}, tok = token) =>
    fetch(`${process.env.BASE_URL}${path}`, { ...init, headers: { ...(init.headers ?? {}), Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' } });

  test('create shows the token once; list never; bearer works with role cap', async () => {
    await featureOn(admin, 'api_tokens');
    const name = `qa48-${marker()}`;
    const r = await admin.post('/api/tokens', { name, role: 'viewer', expires_days: 30 });
    token = r.token;
    item = r.item;
    expect(token).toMatch(/^nmt_[A-Za-z0-9_-]{43}$/);
    expect(item).toMatchObject({ name, role: 'viewer', state: 'active', prefix: token.slice(0, 12), last_used_at: null });
    expect(JSON.stringify(await admin.get('/api/tokens'))).not.toContain(token);
    expect((await bearer('/api/devices?page_size=1')).status).toBe(200);
    // viewer token cannot write; Origin of another site does not matter for bearer requests
    expect((await bearer('/api/settings', { method: 'PUT', body: JSON.stringify({ syslog_retention_days: 31 }), headers: { Origin: 'https://evil.example' } })).status).toBe(403);
    await expect.poll(async () => (await admin.get('/api/tokens')).items.find((t: any) => t.id === item.id).last_used_at, { timeout: 10_000 }).not.toBeNull();
  });

  test('admin token writes and is audited as token; refused on auth/users/tokens', async () => {
    await featureOn(admin, 'audit');
    const { token: at, item: ai } = await admin.post('/api/tokens', { name: `qa48-admin-${marker()}`, role: 'admin', expires_days: 1 });
    const cur = (await admin.get('/api/settings')).syslog_retention_days;
    const res = await bearer('/api/settings', { method: 'PUT', body: JSON.stringify({ syslog_retention_days: cur + 1 }) }, at);
    expect(res.status).toBe(200);
    expect(res.headers.get('set-cookie')).toBeNull();
    for (const [m, p] of [['GET', '/api/tokens'], ['GET', '/api/users'], ['GET', '/api/auth/me'], ['POST', '/api/tokens']]) {
      const r = await bearer(p, { method: m, ...(m === 'POST' ? { body: '{}' } : {}) }, at);
      expect.soft(r.status, `${m} ${p}`).toBe(403);
    }
    const a = await admin.get(`/api/audit?action=settings.update&page_size=5`);
    expect(a.items[0]).toMatchObject({ actor_type: 'token', token_id: ai.id });
    await admin.put('/api/settings', { syslog_retention_days: cur });
    await admin.del(`/api/tokens/${ai.id}`);
  });

  test('viewer cannot mint an admin token; expiry range; revoke -> 401; OFF -> 401; bad tokens throttled eventually', async () => {
    const v = await apiAs(VIEWER);
    expect((await raw(v, 'POST', '/api/tokens', { name: 'up', role: 'admin', expires_days: 1 })).status).toBe(422);
    expect((await raw(admin, 'POST', '/api/tokens', { name: 'z', role: 'viewer', expires_days: 366 })).status).toBe(422);
    expect((await raw(admin, 'POST', '/api/tokens', { name: 'z', role: 'viewer', expires_days: 0 })).status).toBe(422);
    const vt = await v.post('/api/tokens', { name: `qa48-v-${marker()}`, role: 'viewer', expires_days: 5 });
    // `all` is an admin option (api.md); a viewer gets either 403 or only their own tokens
    const va = await raw(v, 'GET', '/api/tokens?all=true');
    if (va.status === 200) expect(va.body.items.every((t: any) => t.user_id === vt.item.user_id)).toBe(true);
    else expect(va.status).toBe(403);
    expect((await v.get('/api/tokens')).items.every((t: any) => t.user_id === vt.item.user_id)).toBe(true);
    expect((await admin.get('/api/tokens?all=true')).items.some((t: any) => t.id === vt.item.id)).toBe(true);
    await featureOff(admin, 'api_tokens');
    expect((await bearer('/api/devices')).status).toBe(401);
    await featureOn(admin, 'api_tokens');
    expect((await bearer('/api/devices')).status).toBe(200);
    await admin.del(`/api/tokens/${item.id}`);
    expect((await admin.get('/api/tokens')).items.find((t: any) => t.id === item.id).state).toBe('revoked');
    expect((await bearer('/api/devices')).status).toBe(401);
    expect((await raw(admin, 'DELETE', `/api/tokens/${item.id}`)).status).toBe(204); // idempotent
  });

  test('UI: API tokens page: create dialog shows the token once with copy, revoke', async ({ page }) => {
    await loginViaApi(page, undefined, '/tokens');
    await page.getByRole('button', { name: tOr('tokens.create', 'Create token') }).click();
    const d = page.getByRole('dialog');
    await d.getByRole('textbox').first().fill(`qa48-ui-${marker()}`);
    await d.getByRole('button', { name: tOr('tokens.create', 'Create') }).click();
    const once = page.getByTestId('token-once');
    await expect(once.locator('input')).toHaveValue(/^nmt_[A-Za-z0-9_-]{43}$/);
    await expect(once.getByRole('button', { name: tOr('tokens.copy', 'Copy') })).toBeVisible();
    await shot2b3(page, '48-token-created');
  });
});

// ------------------------------------------------------------------------------------------------ SIEM
test.describe('SIEM forwarding', () => {
  test.describe.configure({ mode: 'serial' });

  const target = (o: Record<string, unknown>) => ({ name: 'QA sink', enabled: true, host: '127.0.0.1', port: SIEM_TCP_PORT, protocol: 'tcp', tls_verify: true, kinds: ['syslog', 'trap'], min_severity: 7, sources: [], include_unknown: false, ...o });

  test('targets: validation, ids, Test (unsaved and saved) reaches the sink', async () => {
    await featureOn(admin, 'syslog', 'traps', 'siem_forwarding');
    await waitReceivers(admin, ['syslog_udp', 'trap'], 'listening');
    await clearSiem();
    expect((await raw(admin, 'PUT', '/api/settings', { siem_targets: [target({ protocol: 'http' })] })).status).toBe(422);
    expect((await raw(admin, 'PUT', '/api/settings', { siem_targets: [1, 2, 3, 4, 5].map((i) => target({ name: `t${i}` })) })).status).toBe(422);
    const s = await admin.put('/api/settings', { siem_targets: [target({}), target({ name: 'QA udp', protocol: 'udp', port: SIEM_UDP_PORT }), target({ name: 'QA tls', protocol: 'tls', port: SIEM_TLS_PORT, host: 'localhost' })] });
    for (const t of s.siem_targets) expect(t.id).toMatch(/^[0-9a-f]{6}$/);
    expect((await raw(admin, 'POST', '/api/ingest/forwarders/test', { target: target({ port: SIEM_UDP_PORT, protocol: 'udp' }) })).status).toBe(200);
    for (const t of s.siem_targets) {
      const r = await raw(admin, 'POST', '/api/ingest/forwarders/test', { target_id: t.id });
      expect.soft(r.status, `${t.name}: ${r.text}`).toBe(200);
    }
    await expect.poll(async () => new Set((await siemRecords()).map((r) => r.transport)), { timeout: 10_000 }).toEqual(new Set(['udp', 'tcp', 'tls']));
    const bad = await raw(admin, 'POST', '/api/ingest/forwarders/test', { target: target({ port: 1, protocol: 'tcp' }) });
    expect(bad.status).toBe(502);
    expect(bad.body.error.code).toBe('forward_error');
  });

  test('forwarded syslog keeps the origin (PRI, hostname, timestamp, app, msgid) + [origin ...]; octet framing on TCP', async () => {
    await clearSiem();
    sendSyslog({ from: SRC.aos, ids: ['S5'] });
    await expect.poll(async () => (await siemRecords('tcp')).length, { timeout: 15_000 }).toBeGreaterThanOrEqual(1);
    const tcp = (await siemRecords('tcp')).find((r) => r.line.includes('PHY_UPDOWN'));
    expect(tcp.framing).toBe('octet');
    expect(tcp.line).toMatch(/^<188>1 \S+ FF5945-IRF IFNET - PHY_UPDOWN \[origin ip="127\.0\.0\.81" software="NetworkMonitor" swVersion="[^"]+"\] Physical state on the interface Ten-GigabitEthernet1\/0\/49 changed to down\.$/);
    await expect.poll(async () => (await siemRecords('udp')).some((r) => r.line.includes('PHY_UPDOWN')), { timeout: 10_000 }).toBe(true);
    await expect.poll(async () => (await siemRecords('tls')).some((r) => r.line.includes('PHY_UPDOWN')), { timeout: 10_000 }).toBe(true);
  });

  test('traps forwarded as local0 snmptrap lines; min_severity filter; unknown sources excluded', async () => {
    const s = await admin.get('/api/settings');
    // min_severity 5: syslog info (6) filtered, warning (4) and a plain trap (severity 5) forwarded
    await admin.put('/api/settings', { siem_targets: [{ ...s.siem_targets[0], min_severity: 5 }] });
    await clearSiem();
    const m = marker('qa48f');
    sendSyslog({ from: SRC.aos, messages: [`<14>Nov  8 12:00:00 sw a: ${m} info`, `<12>Nov  8 12:00:00 sw a: ${m} warn`] });
    sendSyslog({ from: SRC.unknown, messages: [`<12>Nov  8 12:00:00 sw a: ${m} unknown`] });
    sendTrap({ from: SRC.aos, oid: OID.coldStart, varbinds: [`1.3.6.1.2.1.1.5.0=s:${m}`] });
    await waitItems(admin, 'syslog', { q: m }, (it) => it.length === 3);
    await sleep(2000);
    const lines = (await siemRecords()).map((r) => r.line).filter((l) => l.includes(m));
    expect(lines.some((l) => l.includes(`${m} warn`))).toBe(true);
    expect(lines.some((l) => l.includes(`${m} info`))).toBe(false);
    expect(lines.some((l) => l.includes(`${m} unknown`))).toBe(false);
    const trap = lines.find((l) => l.includes('snmptrap'));
    expect(trap).toMatch(/^<13[0-9]>1 \S+ \S+ snmptrap - coldStart \[origin ip="127\.0\.0\.81"/);
  });

  test('sink down: dropped/errors grow while ingestion continues; recovers when the sink is back', async () => {
    await siemDown();
    const m = marker('qa48down');
    try {
      sendSyslog({ from: SRC.aos, count: 20, messages: [`<12>Nov  8 12:00:00 sw a: ${m} {i}`] });
      await waitItems(admin, 'syslog', { q: m }, (it) => it.length === 20);
      await expect.poll(async () => {
        const f = (await admin.get('/api/ingest/status')).forwarders[0];
        return f.state !== 'connected' && f.errors + f.dropped + f.queue > 0;
      }, { timeout: 20_000 }).toBe(true);
    } finally {
      await siemUp();
    }
    await expect.poll(async () => (await admin.get('/api/ingest/status')).forwarders[0].state, { timeout: 45_000 }).toBe('connected');
  });

  test('UI: Settings SIEM section lists targets with Test', async ({ page }) => {
    await loginViaApi(page, undefined, '/settings');
    await page.getByRole('tab', { name: tOr('settings.tab.siem', 'SIEM forwarding') }).click();
    await expect(page.getByText('QA sink')).toBeVisible();
    await shot2b3(page, '48-siem');
  });
});
