import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import i18n from '../i18n';
import { AccountPage } from './AccountPage';
import { ThemeProvider } from '../hooks/theme';
import { authRoutes, mockFetch, Wrap } from '../test/v13';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});
afterEach(() => vi.unstubAllGlobals());

const CHROME_WIN =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
const SAFARI_IPHONE =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1';
const now = new Date().toISOString().slice(0, 19) + 'Z';
const ITEMS = [
  {
    id: 'cur',
    created_at: now,
    last_seen_at: now,
    ip: '10.20.9.4',
    user_agent: CHROME_WIN,
    current: true,
  },
  {
    id: 'old',
    created_at: now,
    last_seen_at: now,
    ip: '10.20.88.17',
    user_agent: SAFARI_IPHONE,
    current: false,
  },
  {
    id: 'raw',
    created_at: now,
    last_seen_at: null,
    ip: null,
    user_agent: 'netmon-cli/1.0',
    current: false,
  },
];

function common(method: string, url: string) {
  const a = authRoutes(method, url);
  if (a) return a;
  if (url.startsWith('/api/tokens')) return { status: 200, body: { items: [], total: 0 } };
  if (url.startsWith('/api/chat-links')) return { status: 200, body: { items: [] } };
  if (url.startsWith('/api/features') || url.startsWith('/api/settings/public'))
    return { status: 200, body: {} };
  return undefined;
}

describe('[premium] signed-in browsers on the account page', () => {
  it('lists each session with browser · system, IP, "this browser" and a per-row sign out', async () => {
    let items = ITEMS;
    const calls = mockFetch((method, url) => {
      if (method === 'GET' && url.startsWith('/api/auth/sessions'))
        return { status: 200, body: { items } };
      if (method === 'DELETE' && url === '/api/auth/sessions/old') {
        items = ITEMS.filter((x) => x.id !== 'old');
        return { status: 204 };
      }
      return common(method, url);
    });
    render(
      <Wrap auth>
        <ThemeProvider>
          <AccountPage />
        </ThemeProvider>
      </Wrap>,
    );
    const rows = await screen.findAllByTestId('session-row');
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain('Chrome · Windows');
    expect(rows[0].textContent).toContain('10.20.9.4');
    expect(within(rows[0]).getByTestId('session-current').textContent).toBe('This browser');
    expect(rows[1].textContent).toContain('Safari · iPhone');
    expect(within(rows[1]).queryByTestId('session-current')).toBeNull();
    // an unknown User-Agent is shown as sent
    expect(rows[2].textContent).toContain('netmon-cli/1.0');
    fireEvent.click(within(rows[1]).getByRole('button', { name: 'Sign out Safari · iPhone' }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'DELETE' && c.url === '/api/auth/sessions/old')).toBe(
        true,
      ),
    );
    await waitFor(() => expect(screen.getAllByTestId('session-row')).toHaveLength(2));
    // the existing actions stay
    expect(screen.getByTestId('logout-here')).toBeTruthy();
    expect(screen.getByTestId('logout-everywhere')).toBeTruthy();
  });

  it('the current row signs out like "Sign out" (POST /api/auth/logout)', async () => {
    const calls = mockFetch((method, url) => {
      if (method === 'GET' && url.startsWith('/api/auth/sessions'))
        return { status: 200, body: { items: ITEMS.slice(0, 1) } };
      if (method === 'POST' && url.startsWith('/api/auth/logout')) return { status: 204 };
      return common(method, url);
    });
    render(
      <Wrap auth>
        <ThemeProvider>
          <AccountPage />
        </ThemeProvider>
      </Wrap>,
    );
    const row = await screen.findByTestId('session-row');
    fireEvent.click(within(row).getByTestId('session-signout'));
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.url.startsWith('/api/auth/logout'))).toBe(
        true,
      ),
    );
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false);
  });

  it('without the sessions API (404) the card stays text-only', async () => {
    mockFetch((method, url) => common(method, url));
    render(
      <Wrap auth>
        <ThemeProvider>
          <AccountPage />
        </ThemeProvider>
      </Wrap>,
    );
    expect(await screen.findByTestId('logout-here')).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId('account-sessions')).toBeTruthy());
    expect(screen.queryByTestId('session-list')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
