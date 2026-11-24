import { beforeAll, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import i18n from '../../i18n';
import type { Settings } from '../../api/types';
import { Wrap } from '../../test/v13';
import { IngestSettingsCard } from './IngestSettings';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});

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

const show = (s: Partial<Settings>) =>
  render(
    <Wrap>
      <IngestSettingsCard settings={{ ...base, ...s } as Settings} />
    </Wrap>,
  );

describe('[security] trap community check warnings', () => {
  it('mode "any": warns that anyone can send traps, and explains how to switch', () => {
    show({ trap_community_check: 'any', trap_communities_count: 3 });
    const w = screen.getByTestId('trap-any-warning');
    expect(w.textContent).toContain('Traps are accepted with any community.');
    expect(w.textContent).toContain(
      'Anyone who can reach the trap port can send traps that will be stored.',
    );
    expect(w.textContent).toContain('Replace the stored list');
    expect(w.textContent).toContain('Only known communities');
    expect(screen.queryByTestId('trap-known-empty-warning')).toBeNull();
  });

  it('mode "known" with no stored communities: warns that v1/v2c traps are dropped', () => {
    show({ trap_community_check: 'known', trap_communities_count: 0 });
    const w = screen.getByTestId('trap-known-empty-warning');
    expect(w.textContent).toContain('every v1/v2c trap is dropped');
    expect(screen.queryByTestId('trap-any-warning')).toBeNull();
  });

  it('mode "known" with stored communities: no warning', () => {
    show({ trap_community_check: 'known', trap_communities_count: 2 });
    expect(screen.queryByTestId('trap-any-warning')).toBeNull();
    expect(screen.queryByTestId('trap-known-empty-warning')).toBeNull();
  });

  it('Hebrew texts exist for both warnings', async () => {
    await i18n.changeLanguage('he');
    try {
      show({ trap_community_check: 'any' });
      expect(screen.getByTestId('trap-any-warning').textContent).toContain(
        'כל מי שיכול להגיע לפורט המלכודות',
      );
    } finally {
      await i18n.changeLanguage('en');
    }
  });
});
