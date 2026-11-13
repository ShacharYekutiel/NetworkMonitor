// [1.3.1] Settings search on the Settings page: search → result → tab switch → highlighted anchor.
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import i18n from '../i18n';
import { mockFetch, Wrap } from '../test/v13';
import { SettingsPage } from '../pages/SettingsPage';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
afterEach(async () => {
  vi.unstubAllGlobals();
  await i18n.changeLanguage('en');
});

function settings(features: Record<string, boolean>) {
  return {
    retention_days: 30,
    event_retention_days: 30,
    default_interval_s: 60,
    default_language: 'en',
    public_base_url: null,
    timezone: 'UTC',
    topology_refresh_min: 30,
    port_inventory_min: 10,
    uplink_mac_threshold: 5,
    port_mac_retention_days: 30,
    reverse_dns: true,
    smtp: {
      host: '',
      port: 587,
      security: 'starttls',
      username: null,
      from_address: '',
      from_name: '',
      password_set: false,
    },
    trap_retention_days: 30,
    syslog_retention_days: 30,
    trap_community_check: 'any',
    trap_communities_count: 0,
    trap_v3_users: [],
    ingest_unknown_sources: 'store',
    ingest_ignored_sources: [],
    event_linkdown_scope: 'important',
    event_linkdown_include_trunks: true,
    trunk_min_tagged_vlans: 2,
    ...Object.fromEntries(Object.entries(features).map(([k, v]) => [`feature_${k}_enabled`, v])),
  };
}

function renderPage(features: Record<string, boolean>) {
  mockFetch((method, url) =>
    method === 'GET' && url === '/api/settings'
      ? { status: 200, body: settings(features) }
      : undefined,
  );
  render(
    <Wrap>
      <SettingsPage />
    </Wrap>,
  );
}

const activeTab = () =>
  screen.getAllByRole('tab').find((el) => el.getAttribute('aria-selected') === 'true')?.textContent;

describe('[1.3.1] settings search', () => {
  it('finds the trunk setting and opens it on the Traps & syslog tab', async () => {
    renderPage({ traps: true, syslog: true });
    await screen.findByText('Measurement retention (days)');
    const input = screen.getByTestId('settings-search');
    fireEvent.change(input, { target: { value: 'trunk' } });
    const first = screen
      .getAllByTestId('settings-search-result')
      .find((r) => r.textContent?.startsWith('Include all trunk ports'))!;
    expect(within(first).getByText('Include all trunk ports')).toBeTruthy();
    expect(first.textContent).toContain('Traps & syslog›Traps & syslog');
    expect(first.textContent).not.toContain('Requires');
    fireEvent.click(first);
    expect(activeTab()).toBe('Traps & syslog');
    await waitFor(() =>
      expect(
        document
          .querySelector('[data-anchor="setting-trunk-scope"]')
          ?.getAttribute('data-highlight'),
      ).toBe('true'),
    );
    expect(screen.queryByTestId('settings-search-results')).toBeNull();
  });

  it('Enter opens the first result, Esc clears', async () => {
    renderPage({ traps: true });
    await screen.findByText('Measurement retention (days)');
    const input = screen.getByTestId('settings-search') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'nothing-like-this' } });
    expect(screen.getByText('No settings found')).toBeTruthy();
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(input.value).toBe('');
    expect(screen.queryByTestId('settings-search-results')).toBeNull();
    fireEvent.change(input, { target: { value: 'smtp server' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() =>
      expect(
        document.querySelector('[data-anchor="settings-smtp"]')?.getAttribute('data-highlight'),
      ).toBe('true'),
    );
    expect(activeTab()).toBe('General');
  });

  it('a setting of a feature that is off is marked and leads to its switch', async () => {
    renderPage({});
    await screen.findByText('Measurement retention (days)');
    fireEvent.change(screen.getByTestId('settings-search'), { target: { value: 'trunk' } });
    const first = screen.getAllByTestId('settings-search-result')[0];
    expect(first.textContent).toContain('Trunk');
    expect(first.textContent).toContain('Requires the SNMP traps / Syslog feature');
    fireEvent.click(first);
    expect(activeTab()).toBe('Traps & syslog');
    await waitFor(() =>
      expect(document.querySelector('[data-feature="traps"]')?.getAttribute('data-highlight')).toBe(
        'true',
      ),
    );
    expect(document.querySelector('[data-anchor="setting-trunk-scope"]')).toBeNull();
  });

  it('Hebrew UI: finds by Hebrew and by English words', async () => {
    await i18n.changeLanguage('he');
    renderPage({ traps: true });
    await waitFor(() =>
      expect(document.querySelector('[data-anchor="settings-general"]')).toBeTruthy(),
    );
    const input = screen.getByTestId('settings-search');
    fireEvent.change(input, { target: { value: 'קישור' } });
    expect(screen.getAllByText('כלול את כל פורטי ה-trunk').length).toBeGreaterThan(0);
    fireEvent.change(input, { target: { value: 'trunk' } });
    const texts = screen.getAllByTestId('settings-search-result').map((r) => r.textContent);
    expect(texts.slice(0, 2).some((x) => x?.startsWith('כלול את כל פורטי ה-trunk'))).toBe(true);
  });
});
