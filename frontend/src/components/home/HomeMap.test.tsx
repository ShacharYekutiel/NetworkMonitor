// [1.5.1] The home map's words follow a language switch without a reload: the server writes a
// group's "N devices" and the Internet node's name in the user's language at request time, and the
// map stays in the query cache, so the client renders those words itself.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import i18n from '../../i18n';
import { HomeMap } from './HomeMap';
import { LanguageSwitcher } from '../Controls';
import { AuthProvider } from '../../hooks/auth';
import { ToastProvider } from '../toast';
import type { OverviewMap, OverviewMapNode } from '../../api/premium';
import { adminUser, authRoutes, mockFetch } from '../../test/v13';

const counts = (devices: number) => ({
  devices,
  sensors: 0,
  down: 0,
  warning: 0,
  unreachable: 0,
});

function node(p: Partial<OverviewMapNode> & Pick<OverviewMapNode, 'id'>): OverviewMapNode {
  return {
    kind: 'group',
    role: 'group',
    name: p.id,
    subtitle: '',
    status: 'up',
    problem: null,
    counts: counts(0),
    ref: null,
    parent_id: null,
    ...p,
  };
}

// As the server sends it for a Hebrew user (subtitles and the Internet name in Hebrew).
const MAP: OverviewMap = {
  scope: { group_id: null, name: '' },
  configured: true,
  nodes: [
    node({
      id: 'internet',
      kind: 'internet',
      role: 'internet',
      name: 'אינטרנט',
      status: 'unknown',
    }),
    node({
      id: 'd1',
      kind: 'device',
      role: 'firewall',
      name: 'fw-1',
      subtitle: 'FortiGate 60F',
      parent_id: 'internet',
      counts: counts(1),
      ref: { type: 'device', id: 1 },
    }),
    node({
      id: 'g2',
      name: 'Branch',
      subtitle: '2 התקנים',
      parent_id: 'd1',
      counts: counts(2),
      ref: { type: 'group', id: 2 },
    }),
    node({ id: 'g3', name: 'Lab', subtitle: 'התקן אחד', parent_id: 'd1', counts: counts(1) }),
  ],
  links: [
    { from: 'internet', to: 'd1', status: 'up' },
    { from: 'd1', to: 'g2', status: 'up' },
    { from: 'd1', to: 'g3', status: 'up' },
  ],
};

const tile = (id: string) => document.querySelector(`[data-node="${id}"]`) as HTMLElement;

beforeEach(async () => {
  await i18n.changeLanguage('he');
});
afterEach(async () => {
  vi.unstubAllGlobals();
  await i18n.changeLanguage('en');
});

describe('HomeMap follows the UI language', () => {
  it('renders group device counts and the Internet name in the current language', async () => {
    render(
      <MemoryRouter>
        <HomeMap map={MAP} />
      </MemoryRouter>,
    );
    expect(tile('g2')).toHaveTextContent('2 התקנים');
    expect(tile('g3')).toHaveTextContent('התקן אחד');
    expect(tile('internet')).toHaveTextContent('אינטרנט');

    await act(async () => {
      await i18n.changeLanguage('en');
    });
    // Same map data (still the Hebrew response in the cache): every word switched.
    expect(tile('g2')).toHaveTextContent('2 devices');
    expect(tile('g2')).not.toHaveTextContent('התקנים');
    expect(tile('g3')).toHaveTextContent('1 device');
    expect(tile('internet')).toHaveTextContent('Internet');
    expect(tile('g2').getAttribute('aria-label')).toContain('2 devices');
    // A device keeps the server's model / vendor / host subtitle, which has no language.
    expect(tile('d1')).toHaveTextContent('FortiGate 60F');

    await act(async () => {
      await i18n.changeLanguage('he');
    });
    expect(tile('g2')).toHaveTextContent('2 התקנים');
    expect(tile('internet')).toHaveTextContent('אינטרנט');
  });
});

describe('LanguageSwitcher refetches server-language data', () => {
  it('invalidates the home map once the server stored the new language', async () => {
    let mapGets = 0;
    const calls = mockFetch((method, url, body) => {
      if (method === 'GET' && url.startsWith('/api/auth/me'))
        return { status: 200, body: { user: { ...adminUser, language: 'he' } } };
      if (method === 'PATCH' && url.startsWith('/api/auth/me'))
        return { status: 200, body: { user: { ...adminUser, ...(body as object) } } };
      if (method === 'GET' && url.startsWith('/api/overview/map')) {
        mapGets += 1;
        return { status: 200, body: MAP };
      }
      return authRoutes(method, url);
    });
    function MapProbe() {
      useQuery({ queryKey: ['overview-map', null], queryFn: () => fetch('/api/overview/map') });
      return null;
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <ToastProvider>
          <MemoryRouter>
            <AuthProvider>
              <LanguageSwitcher />
              <MapProbe />
            </AuthProvider>
          </MemoryRouter>
        </ToastProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(mapGets).toBe(1));
    await waitFor(() => expect(i18n.language).toBe('he'));
    fireEvent.click(screen.getByRole('button', { name: /שפה|language/i }));
    fireEvent.click(screen.getByRole('option', { name: /English/ }).querySelector('button')!);
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'PATCH' && c.url.startsWith('/api/auth/me'))).toBe(
        true,
      ),
    );
    await waitFor(() => expect(mapGets).toBe(2));
  });
});
