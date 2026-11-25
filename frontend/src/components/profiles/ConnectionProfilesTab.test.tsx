import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import i18n from '../../i18n';
import type { ConnectionProfile } from '../../api/types';
import { authRoutes, mockFetch, Wrap, type Call } from '../../test/v13';
import { ConnectionProfilesTab, filterProfiles, usageTotal } from './ConnectionProfilesTab';
import { testTarget } from './ProfileTestDialog';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
afterEach(() => vi.unstubAllGlobals());

const snmp = {
  id: 1,
  kind: 'snmp',
  name: 'Core v3',
  description: 'Core switches',
  credentials: {
    version: 'v3',
    port: 161,
    timeout_s: 2,
    retries: 1,
    community: null,
    community_set: false,
    v3_username: 'mon',
    v3_security_level: 'authPriv',
    v3_auth_protocol: 'SHA',
    v3_auth_key: null,
    v3_auth_key_set: true,
    v3_priv_protocol: 'AES128',
    v3_priv_key: null,
    v3_priv_key_set: true,
    credentials_unreadable: false,
  },
  usage: { groups: 2, devices: 1, discovery_profiles: 1, devices_effective: 14 },
  created_at: '',
  updated_at: '',
} as ConnectionProfile;
const ssh = {
  id: 2,
  kind: 'ssh',
  name: 'Backup',
  description: '',
  credentials: {
    username: 'backup',
    port: 22,
    password: null,
    password_set: true,
    private_key: null,
    private_key_set: false,
    passphrase: null,
    passphrase_set: false,
    enable_password: null,
    enable_password_set: false,
    legacy_algorithms: false,
    credentials_unreadable: false,
  },
  usage: { groups: 0, devices: 0, discovery_profiles: 0, devices_effective: 0 },
  created_at: '',
  updated_at: '',
} as ConnectionProfile;

function routes(
  extra?: (m: string, u: string, b: unknown) => { status: number; body?: unknown } | undefined,
): Call[] {
  return mockFetch((method, url, body) => {
    const a = authRoutes(method, url);
    if (a) return a;
    const x = extra?.(method, url, body);
    if (x) return x;
    if (method === 'GET' && url.startsWith('/api/connection-profiles?'))
      return { status: 200, body: { items: [snmp, ssh], total: 2, page: 1, page_size: 1000 } };
    if (method === 'GET' && url === '/api/connection-profiles/1/usage')
      return {
        status: 200,
        body: {
          // path = ancestor names, root first, without the group itself (empty at the top level)
          groups: [
            { id: 4, name: 'Core', path: ['HQ', 'Network'] },
            { id: 6, name: 'Lab', path: [] },
          ],
          devices: [{ id: 9, name: 'sw1', host: '10.0.0.9', group_id: 5, group_name: 'Edge' }],
          discovery_profiles: [{ id: 3, name: 'Nightly' }],
          devices_effective: 14,
          truncated: false,
        },
      };
    return undefined;
  });
}

describe('connection profiles helpers', () => {
  it('filters by kind and name', () => {
    expect(filterProfiles([snmp, ssh], 'all', '').map((p) => p.id)).toEqual([1, 2]);
    expect(filterProfiles([snmp, ssh], 'ssh', '').map((p) => p.id)).toEqual([2]);
    expect(filterProfiles([snmp, ssh], 'all', 'CORE').map((p) => p.id)).toEqual([1]);
    expect(usageTotal(snmp)).toBe(4);
    expect(usageTotal(ssh)).toBe(0);
  });

  it('test target: one device or one address', () => {
    expect(testTarget('device', null, '')).toBeNull();
    expect(testTarget('device', 3, '')).toEqual({ device_id: 3 });
    expect(testTarget('host', null, ' 10.0.0.5 ')).toEqual({ host: '10.0.0.5' });
    expect(testTarget('host', null, '10.0.0.0/24')).toBeNull();
    expect(testTarget('host', null, 'a b')).toBeNull();
  });
});

describe('ConnectionProfilesTab', () => {
  it('lists profiles with kind, usage and affected devices; never a secret', async () => {
    routes();
    render(
      <Wrap auth>
        <ConnectionProfilesTab />
      </Wrap>,
    );
    const row = await screen.findByTestId('profile-row-1');
    expect(row).toHaveTextContent('Core v3');
    expect(row).toHaveTextContent('Core switches');
    expect(row).toHaveTextContent('SNMP');
    expect(row).toHaveTextContent('2 groups · 1 device · 1 discovery profile');
    expect(row).toHaveTextContent('14');
    expect(screen.getByTestId('profile-row-2')).toHaveTextContent('Not used');
  });

  it('deleting a used profile shows where it is used instead', async () => {
    const calls = routes();
    render(
      <Wrap auth>
        <ConnectionProfilesTab />
      </Wrap>,
    );
    const row = await screen.findByTestId('profile-row-1');
    fireEvent.click(within(row).getByRole('button', { name: 'Delete Core v3' }));
    const dlg = await screen.findByTestId('profile-usage-dialog');
    expect(dlg).toHaveTextContent('Core v3 is in use');
    await waitFor(() => expect(dlg).toHaveTextContent('HQ / Network / Core'));
    expect(within(dlg).getByRole('link', { name: 'sw1' })).toHaveAttribute('href', '/devices/9');
    expect(within(dlg).getByRole('link', { name: 'Core' })).toHaveAttribute('href', '/groups/4');
    // a top-level group: its name alone, no leading separator
    const lab = within(dlg).getByRole('link', { name: 'Lab' });
    expect(lab).toHaveAttribute('href', '/groups/6');
    expect(lab.closest('li')!.textContent).toBe('Lab');
    expect(within(dlg).getByRole('link', { name: 'Core' }).closest('li')!.textContent).toBe(
      'HQ / Network / Core',
    );
    expect(dlg).toHaveTextContent('Nightly');
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false);
  });

  it('deletes an unused profile after confirming; a 409 opens the usage dialog', async () => {
    const calls = routes((m, u) =>
      m === 'DELETE' && u === '/api/connection-profiles/2'
        ? {
            status: 409,
            body: {
              error: {
                code: 'profile_in_use',
                message: 'in use',
                details: [{ groups: 1, devices: 0, discovery_profiles: 0 }],
              },
            },
          }
        : undefined,
    );
    render(
      <Wrap auth>
        <ConnectionProfilesTab />
      </Wrap>,
    );
    const row = await screen.findByTestId('profile-row-2');
    fireEvent.click(within(row).getByRole('button', { name: 'Delete Backup' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    expect(await screen.findByTestId('profile-usage-dialog')).toHaveTextContent('Backup is in use');
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(1);
  });

  it('creates an SSH profile; editing sends no credentials unless changed', async () => {
    const calls = routes((m, u) => {
      if (m === 'POST' && u === '/api/connection-profiles') return { status: 201, body: ssh };
      if (m === 'PATCH' && u === '/api/connection-profiles/1') return { status: 200, body: snmp };
      return undefined;
    });
    render(
      <Wrap auth>
        <ConnectionProfilesTab />
      </Wrap>,
    );
    await screen.findByTestId('profile-row-1');
    fireEvent.click(screen.getByRole('button', { name: 'Add profile' }));
    const form = await screen.findByTestId('profile-form');
    fireEvent.click(within(form).getByRole('radio', { name: 'SSH' }));
    fireEvent.change(within(form).getByLabelText(/^Name/), { target: { value: 'Lab' } });
    fireEvent.change(within(form).getByLabelText(/^Username/), { target: { value: 'admin' } });
    fireEvent.change(within(form).getByLabelText(/^Password/), { target: { value: 's3cret' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.body).toMatchObject({
      kind: 'ssh',
      name: 'Lab',
      credentials: { username: 'admin', port: 22, password: 's3cret' },
    });

    await waitFor(() => expect(screen.queryByTestId('profile-form')).toBeNull());
    fireEvent.click(
      within(screen.getByTestId('profile-row-1')).getByRole('button', { name: 'Edit Core v3' }),
    );
    const edit = await screen.findByTestId('profile-form');
    expect(edit).toHaveTextContent('Saving applies at once to 14 devices');
    expect(edit).toHaveTextContent('The kind cannot be changed');
    fireEvent.change(within(edit).getByLabelText(/^Description/), { target: { value: 'x' } });
    fireEvent.click(within(edit).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({
      name: 'Core v3',
      description: 'x',
    });
  });
});

describe('"used by" counts', () => {
  const text = () =>
    i18n.t('profiles.usedByText', {
      groups: i18n.t('profiles.usedGroups', { count: 1 }),
      devices: i18n.t('profiles.usedDevices', { count: 2 }),
      discovery: i18n.t('profiles.usedDiscovery', { count: 5 }),
    });
  afterEach(async () => {
    await i18n.changeLanguage('en');
  });
  it('pluralises every count (en one/other, he one/two/other)', async () => {
    await i18n.changeLanguage('en');
    expect(text()).toBe('1 group · 2 devices · 5 discovery profiles');
    expect(i18n.t('profiles.usedDiscovery', { count: 1 })).toBe('1 discovery profile');
    expect(i18n.t('profiles.usedGroups', { count: 0 })).toBe('0 groups');
    await i18n.changeLanguage('he');
    expect(text()).toBe('קבוצה אחת · 2 התקנים · 5 פרופילי גילוי');
    expect(i18n.t('profiles.usedDiscovery', { count: 1 })).toBe('פרופיל גילוי אחד');
    expect(i18n.t('profiles.usedGroups', { count: 2 })).toBe('2 קבוצות');
  });
});
