import { expect, test } from '@playwright/test';
import { ADMIN, Api, apiAs, loginViaApi, loginViaUi } from '../lib/helpers';
import { raw } from '../lib/lab';
import {
  AD, LDAPS_PORT, LDAP_DEAD_PORT, LDAP_PLAIN_PORT, LDAP_WRONG_PORT, adBinds, adHang, adStart, adStop, clearAdBinds, featureOff, featureOn,
  ldapConfig, publicFeatures, shot2b3, tOr, tryLogin,
} from '../lib/lab2b3';

// [2b-3] 45 — Active Directory login against the fake AD (architecture.md §12.7, api.md [2b-3] Active Directory,
// fixture tests/sim/ldap/mnidom.yaml): configure, Test connection, Test user, three name forms, nested groups,
// denied users (generic error), local precedence, empty password never reaches AD, DC failover, wrong cert,
// local admin with the AD stopped / hanging, directory guard, ldap_managed refusals.
test.describe.configure({ mode: 'serial' });

let admin: Api;

test.beforeAll(async () => {
  admin = await apiAs();
  await adStart();
  await adHang(false);
});
test.afterAll(async () => {
  await adStart();
  await adHang(false);
});

test('configure while OFF: bind password write-only; Test connection works while the feature is OFF', async () => {
  expect((await publicFeatures(admin)).ldap).toBe(false);
  const s = await raw(admin, 'PUT', '/api/settings', { ldap: ldapConfig() });
  expect(s.status, s.text).toBe(200);
  expect(s.body.ldap).toMatchObject({ bind_password: null, bind_password_set: true, bind_password_unreadable: false, servers: [`localhost:${LDAPS_PORT}`] });
  expect(JSON.stringify(s.body)).not.toContain(AD.password);
  const t = await admin.post('/api/ldap/test', {});
  expect(t).toMatchObject({ ok: true, discovered_via_dns: false, service_bind: { ok: true }, base_dn: { ok: true } });
  expect(t.servers[0]).toMatchObject({ server: `localhost:${LDAPS_PORT}`, ok: true, security: 'ldaps', active_directory: true, error: null });
  expect(t.servers[0].tls).toMatchObject({ verified: true });
  expect(t.admin_group).toEqual({ dn: AD.adminGroup, found: true });
  expect(t.viewer_group).toEqual({ dn: AD.viewerGroup, found: true });
});

test('Test connection: StartTLS, wrong-name certificate, failover (first DC closed)', async () => {
  const st = await admin.post('/api/ldap/test', { ldap: { security: 'starttls', servers: [`localhost:${LDAP_PLAIN_PORT}`] } });
  expect(st.servers[0]).toMatchObject({ ok: true, security: 'starttls' });
  const wrong = await admin.post('/api/ldap/test', { ldap: { servers: [`localhost:${LDAP_WRONG_PORT}`] } });
  expect(wrong.ok).toBe(false);
  expect(wrong.servers[0].ok).toBe(false);
  expect(wrong.servers[0].error).toMatch(/certificate|hostname|verify/i);
  const fo = await admin.post('/api/ldap/test', { ldap: { servers: [`localhost:${LDAP_DEAD_PORT}`, `localhost:${LDAPS_PORT}`] } });
  expect(fo.servers.map((x: any) => x.ok)).toEqual([false, true]);
  // stored config unchanged by the test overrides
  expect((await admin.get('/api/settings')).ldap.servers).toEqual([`localhost:${LDAPS_PORT}`]);
});

test('Test user login: steps, role, reason; no user created', async () => {
  const ok = await admin.post('/api/ldap/test-user', { username: 'asmith', password: AD.password });
  expect(ok).toMatchObject({ ok: true, role: 'admin', reason: null });
  expect(ok.steps.map((s: any) => s.step)).toEqual(['connect', 'service_bind', 'search_user', 'account_state', 'user_bind', 'groups']);
  expect(ok.user).toMatchObject({ sam_account_name: 'asmith', disabled: false, locked: false });
  const no = await admin.post('/api/ldap/test-user', { username: 'nogroup', password: AD.password });
  expect(no).toMatchObject({ ok: false, role: null, reason: 'not_in_group' });
  expect((await admin.get('/api/users?page_size=1000')).items.some((u: any) => u.username === 'asmith')).toBe(false);
});

test('UI: AD is configure-first: form shown while OFF, switch at the bottom unlocked by a successful Test connection', async ({ page }) => {
  expect((await publicFeatures(admin)).ldap).toBe(false);
  await loginViaApi(page, undefined, '/settings?tab=ldap');
  const form = page.getByTestId('ldap-settings');
  await expect(form).toBeVisible();
  await expect(form.getByPlaceholder('mnidom.net', { exact: true })).toHaveValue('mnidom.net');
  // nothing at the top: the switch is inside the form, next to Test connection
  const sw = form.getByTestId('feature-toggles').getByRole('switch', { name: tOr('features.name.ldap', 'Active Directory login') });
  await expect(sw).toBeDisabled();
  await expect(form.getByTestId('enable-hint')).toHaveText(tOr('features.enableNeedsTest', 'Test the connection to enable'));
  await shot2b3(page, '45-ad-off-locked');
  // an unsaved edit needs a save first
  await form.getByPlaceholder('MNIDOM', { exact: true }).fill('MNIDOMX');
  await expect(form.getByTestId('enable-hint')).toHaveText(tOr('features.enableNeedsSave', 'Save the settings and test the connection to enable'));
  await form.getByPlaceholder('MNIDOM', { exact: true }).fill('MNIDOM');
  await form.getByRole('button', { name: tOr('ldap.testConnection', 'Test connection') }).click();
  await expect(page.getByRole('table').filter({ hasText: `localhost:${LDAPS_PORT}` })).toBeVisible({ timeout: 15_000 });
  await expect(sw).toBeEnabled();
  await sw.click();
  await expect.poll(async () => (await publicFeatures(admin)).ldap, { timeout: 10_000 }).toBe(true);
  await expect(sw).toHaveAttribute('aria-checked', 'true');
  await expect(form).toBeVisible();
  await shot2b3(page, '45-ad-on');
  // OFF again from the same switch (the stored settings stay)
  await sw.click();
  await expect.poll(async () => (await publicFeatures(admin)).ldap, { timeout: 10_000 }).toBe(false);
  await expect(form.getByPlaceholder('mnidom.net', { exact: true })).toHaveValue('mnidom.net');
  await featureOff(admin, 'ldap');
});

test('LDAP users get 401 while the feature is OFF; ON publishes the domain hint', async () => {
  expect((await tryLogin('jdoe', AD.password)).status).toBe(401);
  await featureOn(admin, 'ldap');
  const pub = await admin.get('/api/settings/public');
  expect(pub.ldap).toEqual({ domain: 'mnidom.net', netbios_domain: 'MNIDOM' });
});

test('three name forms -> viewer; nested group (1 and 2 levels) -> admin; source ldap', async () => {
  for (const name of ['jdoe', 'MNIDOM\\jdoe', 'mnidom\\jdoe', 'John.Doe@mnidom.net', 'john.doe@MNIDOM.NET']) {
    const r = await tryLogin(name, AD.password);
    expect.soft(r.status, `${name}: ${JSON.stringify(r.body)}`).toBe(200);
    if (r.status === 200) expect.soft(r.body.user, name).toMatchObject({ username: 'jdoe', role: 'viewer', source: 'ldap', display_name: 'John Doe', email: 'john.doe@mnidom.net' });
  }
  const a = await tryLogin('asmith', AD.password);
  expect(a.body.user).toMatchObject({ username: 'asmith', role: 'admin', source: 'ldap' });
  const b = await tryLogin('bhelp', AD.password);
  expect(b.body.user).toMatchObject({ role: 'admin' });
  // only one app user per AD account (upsert by objectGUID)
  const users = (await admin.get('/api/users?page_size=1000')).items.filter((u: any) => u.username === 'jdoe');
  expect(users).toHaveLength(1);
  expect(users[0].ldap_synced_at).not.toBeNull();
  // a session made by an AD login works on the API
  expect((await raw(a.api, 'GET', '/api/auth/me')).body.username ?? (await raw(a.api, 'GET', '/api/auth/me')).body.user?.username).toBe('asmith');
});

test('denied users and bad passwords -> 401 invalid_credentials (generic), OTHER\\jdoe not a directory name', async () => {
  for (const [u, p] of [['nogroup', AD.password], ['olduser', AD.password], ['lockeduser', AD.password], ['expired', AD.password], ['jdoe', 'wrong-pass'], ['nobody', AD.password], ['OTHER\\jdoe', AD.password], ['*)(sAMAccountName=*', AD.password]]) {
    const r = await tryLogin(u, p);
    expect.soft(r.status, `${u}`).toBe(401);
    expect.soft(r.body?.error?.code, `${u}`).toBe('invalid_credentials');
  }
});

test('empty password: 401 and never sent to AD (no bind recorded)', async () => {
  await clearAdBinds();
  const r = await tryLogin('jdoe', '');
  expect(r.status).toBe(401);
  const binds = await adBinds();
  expect(binds.filter((b) => b.password_empty)).toEqual([]);
  expect(binds.filter((b) => /jdoe/i.test(b.name))).toEqual([]);
});

test('local account wins over AD (AD not contacted); creating a local user with an LDAP user name -> 409', async () => {
  const lu = await admin.post('/api/users', { username: 'nogroup', password: 'Local-pass-1', email: null, role: 'viewer', language: 'en' });
  try {
    await clearAdBinds();
    expect((await tryLogin('nogroup', AD.password)).status).toBe(401);
    const ok = await tryLogin('nogroup', 'Local-pass-1');
    expect(ok.body.user).toMatchObject({ source: 'local' });
    expect((await adBinds()).filter((b) => /nogroup/i.test(b.name))).toEqual([]);
  } finally {
    await admin.del(`/api/users/${lu.id}`);
  }
  const c = await raw(admin, 'POST', '/api/users', { username: 'jdoe', password: 'Local-pass-1', email: null, role: 'viewer', language: 'en' });
  expect(c.status).toBe(409);
});

test('ldap_managed: password/role/email refused; deactivate blocks login; language allowed', async () => {
  const j = await tryLogin('jdoe', AD.password);
  expect((await raw(j.api, 'POST', '/api/auth/change-password', { current_password: AD.password, new_password: 'Another-pass-9' })).body?.error?.code).toBe('ldap_managed');
  expect((await raw(j.api, 'PATCH', '/api/auth/me', { email: 'x@y.z' })).body?.error?.code).toBe('ldap_managed');
  expect((await raw(j.api, 'PATCH', '/api/auth/me', { language: 'he' })).status).toBe(200);
  const u = (await admin.get('/api/users?page_size=1000')).items.find((x: any) => x.username === 'jdoe');
  expect((await raw(admin, 'PATCH', `/api/users/${u.id}`, { role: 'admin' })).body?.error?.code).toBe('ldap_managed');
  expect((await raw(admin, 'PATCH', `/api/users/${u.id}`, { password: 'Another-pass-9' })).body?.error?.code).toBe('ldap_managed');
  await admin.patch(`/api/users/${u.id}`, { is_active: false, language: 'en' });
  expect((await tryLogin('jdoe', AD.password)).status).toBe(401);
  await admin.patch(`/api/users/${u.id}`, { is_active: true });
  expect((await tryLogin('jdoe', AD.password)).status).toBe(200);
});

test('last_admin counts local admins only (LDAP admins do not count)', async () => {
  const users = (await admin.get('/api/users?page_size=1000')).items;
  const localAdmins = users.filter((u: any) => u.role === 'admin' && u.is_active && (u.source ?? 'local') === 'local');
  test.skip(localAdmins.length !== 1, 'other local admins exist');
  const r = await raw(admin, 'PATCH', `/api/users/${localAdmins[0].id}`, { role: 'viewer' });
  expect(r.body?.error?.code).toBe('last_admin');
});

test('DC failover: first server closed -> login through the second', async () => {
  await admin.put('/api/settings', { ldap: { servers: [`localhost:${LDAP_DEAD_PORT}`, `localhost:${LDAPS_PORT}`] } });
  try {
    expect((await tryLogin('jdoe', AD.password)).status).toBe(200);
  } finally {
    await admin.put('/api/settings', { ldap: { servers: [`localhost:${LDAPS_PORT}`] } });
  }
});

test('wrong DC certificate fails closed -> 503 directory_unavailable', async () => {
  await admin.put('/api/settings', { ldap: { servers: [`localhost:${LDAP_WRONG_PORT}`] } });
  try {
    const r = await tryLogin('jdoe', AD.password);
    expect(r.status).toBe(503);
    expect(r.body.error.code).toBe('directory_unavailable');
  } finally {
    await admin.put('/api/settings', { ldap: { servers: [`localhost:${LDAPS_PORT}`] } });
  }
});

test('AD stopped: local admin logs in < 1 s, AD user -> 503', async () => {
  await adStop();
  try {
    const a = await tryLogin(ADMIN.username, ADMIN.password);
    expect(a.status).toBe(200);
    expect(a.ms).toBeLessThan(1000);
    const j = await tryLogin('jdoe', AD.password);
    expect(j.status).toBe(503);
  } finally {
    await adStart();
  }
});

test('AD hangs: AD user -> 503 within the budget while a local admin login completes in < 1 s', async () => {
  await adHang(true);
  try {
    const t0 = Date.now();
    const pending = tryLogin('asmith', AD.password);
    await new Promise((r) => setTimeout(r, 300));
    const a = await tryLogin(ADMIN.username, ADMIN.password);
    expect(a.status).toBe(200);
    expect(a.ms).toBeLessThan(1000);
    const j = await pending;
    expect(j.status).toBe(503);
    // timeout_s 3 -> budget min(3*3+2, 30) = 11 s
    expect(Date.now() - t0).toBeLessThan(13_000);
  } finally {
    await adHang(false);
  }
});

test('directory guard: 5 failed AD logins for one username -> 429 (protects AD lockout)', async () => {
  let codes: number[] = [];
  // bhelp has no earlier failures in this spec (every failure, any reason, counts toward the guard)
  for (let i = 0; i < 6; i++) codes.push((await tryLogin('bhelp', `wrong-${i}`)).status);
  expect(codes.slice(0, 5)).toEqual([401, 401, 401, 401, 401]);
  expect(codes[5]).toBe(429);
  codes = [(await tryLogin('bhelp', AD.password)).status];
  expect(codes[0]).toBe(429);
});

test('UI: login hint, AD login, Settings -> Active Directory test table, profile without password change', async ({ page }) => {
  await page.goto('/login');
  await expect(page.getByText(/MNIDOM\\user or user@mnidom\.net/)).toBeVisible();
  await shot2b3(page, '45-login-hint');
  await loginViaUi(page, { username: 'MNIDOM\\jdoe', password: AD.password });
  await expect(page.getByRole('navigation').first()).toBeVisible();
  await expect(page).not.toHaveURL(/\/login/);
  await page.goto('/account');
  await expect(page.getByLabel(tOr('account.newPassword', 'New password'))).toHaveCount(0);
  await page.context().clearCookies();
  await loginViaUi(page);
  await expect(page.getByRole('navigation').first()).toBeVisible();
  await page.goto('/settings');
  await page.getByRole('tab', { name: tOr('settings.tab.ldap', 'Active Directory') }).click();
  // the AD switch lives at the bottom of its form, next to Test connection (and is on here)
  const adSwitch = page.getByTestId('ldap-settings').getByTestId('feature-toggles').getByRole('switch', { name: tOr('features.name.ldap', 'Active Directory login') });
  await expect(adSwitch).toHaveAttribute('aria-checked', 'true');
  await page.getByRole('button', { name: tOr('ldap.testConnection', 'Test connection') }).click();
  await expect(page.getByRole('table').filter({ hasText: `localhost:${LDAPS_PORT}` })).toBeVisible({ timeout: 15_000 });
  await shot2b3(page, '45-ad-settings-test');
  await page.goto('/users');
  await expect(page.getByRole('row').filter({ hasText: 'asmith' })).toContainText(tOr('user.source.ldap', 'AD'));
});
