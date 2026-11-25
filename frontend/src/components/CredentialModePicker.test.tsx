import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import i18n from '../i18n';
import type { CredentialMode } from '../api/types';
import type { KindChoice } from '../lib/credentials';
import { authRoutes, mockFetch, Wrap, type Call } from '../test/v13';
import { CredentialModePicker } from './CredentialModePicker';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
afterEach(() => vi.unstubAllGlobals());

const profile = (id: number, name: string) => ({
  id,
  kind: 'snmp',
  name,
  description: `${name} description`,
  credentials: { version: 'v2c', community_set: true, credentials_unreadable: false },
  usage: { groups: 0, devices: 0, discovery_profiles: 0, devices_effective: 0 },
  created_at: '',
  updated_at: '',
});

function routes(): Call[] {
  return mockFetch((method, url) => {
    const a = authRoutes(method, url);
    if (a) return a;
    if (method === 'GET' && url.startsWith('/api/connection-profiles'))
      return {
        status: 200,
        body: {
          items: [profile(1, 'Core v2'), profile(2, 'Edge')],
          total: 2,
          page: 1,
          page_size: 1000,
        },
      };
    if (method === 'POST' && url === '/api/devices/7/credentials/snmp/copy-from-profile')
      return { status: 200, body: { id: 7, snmp_mode: 'manual' } };
    return undefined;
  });
}

function Harness({
  initialMode,
  savedManual = false,
  isNew = false,
  start,
  onChange,
}: {
  initialMode: CredentialMode;
  savedManual?: boolean;
  isNew?: boolean;
  start?: Partial<KindChoice>;
  onChange?: (c: KindChoice) => void;
}) {
  const [v, setV] = useState<KindChoice>({
    mode: initialMode,
    profileId: null,
    manualTouched: false,
    restore: savedManual,
    ...start,
  });
  return (
    <CredentialModePicker
      kind="snmp"
      forGroup={false}
      nodeName="sw1"
      node={isNew ? undefined : { type: 'device', id: 7 }}
      initialMode={initialMode}
      savedManual={savedManual}
      origin={{
        source: 'group',
        group_id: 2,
        group_name: 'TLV',
        mode: 'profile',
        profile_id: 1,
        profile_name: 'Core v2',
      }}
      value={v}
      onChange={(c) => {
        setV(c);
        onChange?.(c);
      }}
      manual={<div data-testid="manual-fields" />}
      onCopied={() => undefined}
    />
  );
}

const radio = (name: string) => screen.getByRole('radio', { name });

describe('CredentialModePicker', () => {
  it('shows the three modes, the origin line and the inherit hint', async () => {
    routes();
    render(
      <Wrap auth>
        <Harness initialMode="inherit" />
      </Wrap>,
    );
    expect(
      await screen.findByRole('radiogroup', { name: 'SNMP credentials of this device' }),
    ).toBeInTheDocument();
    expect(radio('Inherit')).toHaveAttribute('aria-checked', 'true');
    expect(radio('Profile')).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByTestId('cred-origin-snmp')).toHaveTextContent(
      'Inherited from group TLV · profile Core v2',
    );
    expect(screen.getByText(/Use the group's settings/)).toBeInTheDocument();
    expect(screen.queryByTestId('manual-fields')).toBeNull();
  });

  it('profile mode lists the profiles of the kind and requires one', async () => {
    routes();
    render(
      <Wrap auth>
        <Harness initialMode="inherit" />
      </Wrap>,
    );
    fireEvent.click(await screen.findByRole('radio', { name: 'Profile' }));
    const select = screen.getByTestId('cred-profile-snmp');
    expect(screen.getByText(/Choose a connection profile/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('option', { name: 'Edge' })).toBeInTheDocument());
    fireEvent.change(select, { target: { value: '2' } });
    expect(screen.getByText('Edge description')).toBeInTheDocument();
    expect(screen.queryByText(/Choose a connection profile/)).toBeNull();
  });

  it('arrow keys move between the modes', async () => {
    routes();
    render(
      <Wrap auth>
        <Harness initialMode="inherit" />
      </Wrap>,
    );
    const inherit = await screen.findByRole('radio', { name: 'Inherit' });
    fireEvent.keyDown(inherit, { key: 'ArrowRight' });
    expect(radio('Profile')).toHaveAttribute('aria-checked', 'true');
    expect(radio('Profile')).toHaveAttribute('tabindex', '0');
    expect(radio('Inherit')).toHaveAttribute('tabindex', '-1');
  });

  it('manual → profile: no confirmation, a note that the manual settings are kept', async () => {
    routes();
    render(
      <Wrap auth>
        <Harness initialMode="manual" />
      </Wrap>,
    );
    expect(await screen.findByTestId('manual-fields')).toBeInTheDocument();
    fireEvent.click(radio('Profile'));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText(/are kept and come back/)).toBeInTheDocument();
  });

  it('manual → inherit asks first; cancel keeps manual', async () => {
    routes();
    render(
      <Wrap auth>
        <Harness initialMode="manual" />
      </Wrap>,
    );
    fireEvent.click(await screen.findByRole('radio', { name: 'Inherit' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent(
      'Switching to Inherit deletes the SNMP credentials stored on sw1',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(radio('Manual')).toHaveAttribute('aria-checked', 'true');
    fireEvent.click(radio('Inherit'));
    fireEvent.click(screen.getByRole('button', { name: 'Switch to Inherit' }));
    expect(radio('Inherit')).toHaveAttribute('aria-checked', 'true');
  });

  it('profile with dormant data → manual offers restore or new settings', async () => {
    routes();
    render(
      <Wrap auth>
        <Harness initialMode="profile" savedManual start={{ profileId: 1 }} />
      </Wrap>,
    );
    fireEvent.click(await screen.findByRole('radio', { name: 'Manual' }));
    expect(radio('Restore the saved manual settings')).toHaveAttribute('aria-checked', 'true');
    expect(screen.queryByTestId('manual-fields')).toBeNull();
    fireEvent.click(radio('Enter new manual settings'));
    expect(screen.getByTestId('manual-fields')).toBeInTheDocument();
  });

  it('profile without dormant data → inherit needs no confirmation', async () => {
    routes();
    render(
      <Wrap auth>
        <Harness initialMode="profile" start={{ profileId: 1 }} />
      </Wrap>,
    );
    fireEvent.click(await screen.findByRole('radio', { name: 'Inherit' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('copy into manual posts the selected profile; asks first when dormant data exists', async () => {
    const calls = routes();
    render(
      <Wrap auth>
        <Harness initialMode="profile" savedManual start={{ profileId: 2 }} />
      </Wrap>,
    );
    fireEvent.click(await screen.findByTestId('cred-copy-snmp'));
    expect(screen.getByRole('dialog')).toHaveTextContent('Replace the manual settings?');
    fireEvent.click(screen.getByRole('button', { name: 'Copy and replace' }));
    await waitFor(() =>
      expect(calls.find((c) => c.url.endsWith('/copy-from-profile'))?.body).toEqual({
        profile_id: 2,
      }),
    );
  });

  it('a new node has no copy button', async () => {
    routes();
    render(
      <Wrap auth>
        <Harness initialMode="inherit" isNew start={{ mode: 'profile' }} />
      </Wrap>,
    );
    expect(await screen.findByText(/available after the first save/)).toBeInTheDocument();
    expect(screen.queryByTestId('cred-copy-snmp')).toBeNull();
  });
});
