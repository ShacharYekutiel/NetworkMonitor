import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import i18n from '../../i18n';
import type { Settings } from '../../api/types';
import { mockFetch, Wrap } from '../../test/v13';
import { IngestSettingsCard } from './IngestSettings';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
afterEach(() => vi.unstubAllGlobals());

const base = {
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
} as unknown as Settings;

describe('[1.3] D. traps & syslog: trunk ports', () => {
  it('loads, validates and saves both fields with the form', async () => {
    const calls = mockFetch((method, url, body) =>
      method === 'PUT' && url === '/api/settings'
        ? { status: 200, body: { ...base, ...(body as object) } }
        : undefined,
    );
    render(
      <Wrap>
        <IngestSettingsCard settings={base} />
      </Wrap>,
    );
    const box = screen.getByTestId('trunk-scope');
    const include = within(box).getByLabelText('Include all trunk ports') as HTMLInputElement;
    expect(include.checked).toBe(true);
    const n = screen.getByTestId('trunk-min-tagged') as HTMLInputElement;
    expect(n.value).toBe('2');
    expect(box.textContent).toContain('voice VLAN');
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    fireEvent.change(n, { target: { value: '0' } });
    expect(save.disabled).toBe(true);
    expect(box.textContent).toContain('from 1 to 100');
    fireEvent.change(n, { target: { value: '101' } });
    expect(save.disabled).toBe(true);
    fireEvent.change(n, { target: { value: '3' } });
    fireEvent.click(include);
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));
    const body = calls.find((c) => c.method === 'PUT')!.body as Record<string, unknown>;
    expect(body.event_linkdown_include_trunks).toBe(false);
    expect(body.trunk_min_tagged_vlans).toBe(3);
    expect(body.event_linkdown_scope).toBe('important');
  });

  it('defaults (true, 2) when an older server omits the keys', () => {
    const old = { ...base } as Record<string, unknown>;
    delete old.event_linkdown_include_trunks;
    delete old.trunk_min_tagged_vlans;
    render(
      <Wrap>
        <IngestSettingsCard settings={old as unknown as Settings} />
      </Wrap>,
    );
    expect((screen.getByLabelText('Include all trunk ports') as HTMLInputElement).checked).toBe(
      true,
    );
    expect((screen.getByTestId('trunk-min-tagged') as HTMLInputElement).value).toBe('2');
  });

  it('Hebrew strings', async () => {
    await i18n.changeLanguage('he');
    render(
      <Wrap>
        <IngestSettingsCard settings={base} />
      </Wrap>,
    );
    expect(screen.getByText('כלול את כל פורטי ה-trunk')).toBeTruthy();
    await i18n.changeLanguage('en');
  });
});
