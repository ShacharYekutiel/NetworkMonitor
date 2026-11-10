import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import i18n from '../i18n';
import { AuthProvider } from '../hooks/auth';
import { ThemeProvider } from '../hooks/theme';
import { LoginPage } from './Login';
import { KioskPage } from './KioskPage';
import { ShowUrlDialog } from '../components/features/NocSettings';
import type { KioskView } from '../api/types3';

type Route = (url: string, init?: RequestInit) => { status: number; body?: unknown } | undefined;

function mockFetch(route: Route) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const r = route(url, init) ?? {
      status: 404,
      body: { error: { code: 'not_found', message: 'nf', details: null } },
    };
    return new Response(r.body === undefined ? null : JSON.stringify(r.body), {
      status: r.status,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <MemoryRouter>
          <AuthProvider>{children}</AuthProvider>
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  );
}

const user = {
  id: 1,
  username: 'jdoe',
  email: null,
  role: 'admin',
  language: 'en',
  is_active: true,
  last_login_at: null,
  created_at: '',
  source: 'ldap',
};

function loginRoutes(sso: object, kerberosOk = true): Route {
  return (url) => {
    if (url.startsWith('/api/setup/status'))
      return { status: 200, body: { setup_required: false } };
    if (url.startsWith('/api/auth/me'))
      return {
        status: 401,
        body: { error: { code: 'unauthenticated', message: '', details: null } },
      };
    if (url.startsWith('/api/settings/public'))
      return {
        status: 200,
        body: { default_language: 'en', features: { sso_kerberos: true, sso_oidc: true }, sso },
      };
    if (url.startsWith('/api/auth/sso/kerberos'))
      return kerberosOk
        ? { status: 200, body: { user } }
        : { status: 401, body: { error: { code: 'sso_challenge', message: '', details: null } } };
    return undefined;
  };
}

beforeEach(async () => {
  await i18n.changeLanguage('en');
  sessionStorage.clear();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe('[3] login page SSO', () => {
  it('shows "Sign in with Microsoft" linking to the OIDC start', async () => {
    mockFetch(loginRoutes({ kerberos: null, oidc: { button_label: 'Sign in with Contoso' } }));
    render(wrap(<LoginPage />));
    const a = await screen.findByTestId('oidc-button');
    expect(a.getAttribute('href')).toBe('/api/auth/oidc/start?next=%2F');
    expect(a.textContent).toContain('Sign in with Contoso');
    expect(screen.queryByTestId('kerberos-button')).toBeNull();
  });
  it('tries Kerberos automatically, but not after a logout', async () => {
    const f = mockFetch(loginRoutes({ kerberos: { auto_login: true }, oidc: null }, false));
    render(wrap(<LoginPage />));
    await screen.findByTestId('kerberos-button');
    await waitFor(() =>
      expect(f.mock.calls.some(([u]) => String(u).startsWith('/api/auth/sso/kerberos'))).toBe(true),
    );
    // sso_challenge without a ticket: silently back to the form
    await waitFor(() => expect(screen.queryByTestId('kerberos-trying')).toBeNull());
  });
  it('does not try Kerberos when the logout flag is set', async () => {
    sessionStorage.setItem('netmon.sso.skip', '1');
    const f = mockFetch(loginRoutes({ kerberos: { auto_login: true }, oidc: null }));
    render(wrap(<LoginPage />));
    await screen.findByTestId('kerberos-button');
    expect(f.mock.calls.some(([u]) => String(u).startsWith('/api/auth/sso/kerberos'))).toBe(false);
  });
});

const counts = { up: 40, warning: 1, down: 2, paused: 0, unknown: 0, unreachable: 1, total: 44 };
const view: KioskView = {
  screen: { name: 'NOC wall', language: 'en', widgets: ['summary', 'down_devices'], refresh_s: 30 },
  generated_at: '2026-11-08T10:00:00Z',
  timezone: 'Asia/Jerusalem',
  summary: { device_counts: counts, sensor_counts: counts, acknowledged: 1, in_maintenance: 0 },
  down_devices: [
    {
      id: 3,
      name: 'core-sw',
      group: 'Core',
      host: null,
      status: 'down',
      since: '2026-11-08T09:00:00Z',
      acknowledged: false,
    },
  ],
  problems: null,
  recent_alerts: null,
  group_tiles: null,
  map: null,
};

class FakeWs {
  static last: FakeWs | null = null;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  constructor(public url: string) {
    FakeWs.last = this;
    setTimeout(() => this.onopen?.(), 0);
  }
  send(s: string) {
    this.sent.push(s);
  }
  close() {}
}

describe('[3] NOC kiosk page', () => {
  it('takes the token from the fragment, hides it, and renders the view', async () => {
    const token = `nmk_${'k'.repeat(43)}`;
    window.history.pushState(null, '', `/noc#${token}`);
    vi.stubGlobal('WebSocket', FakeWs);
    const f = mockFetch((url) =>
      url.startsWith('/api/kiosk/view') ? { status: 200, body: view } : undefined,
    );
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <KioskPage />
      </QueryClientProvider>,
    );
    expect(await screen.findByText('core-sw')).toBeTruthy();
    expect(screen.getByTestId('kiosk-kpis').textContent).toContain('40');
    expect(window.location.hash).toBe('');
    const [, init] = f.mock.calls.find(([u]) => String(u).startsWith('/api/kiosk/view'))!;
    expect((init?.headers as Record<string, string>)['X-Netmon-Kiosk']).toBe(token);
    expect(init?.credentials).toBe('omit');
    await waitFor(() =>
      expect(FakeWs.last?.sent[0]).toBe(JSON.stringify({ type: 'kiosk_auth', token })),
    );
    expect(FakeWs.last?.url).toMatch(/\/ws\/kiosk$/);
    window.history.pushState(null, '', '/');
  });
  it('shows "not available" on 404', async () => {
    window.history.pushState(null, '', `/noc#nmk_${'z'.repeat(43)}`);
    vi.stubGlobal('WebSocket', FakeWs);
    mockFetch(() => undefined);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <KioskPage />
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId('kiosk-unavailable')).toBeTruthy();
    window.history.pushState(null, '', '/');
  });
  it('explains a 429 instead of staying blank (office test: Docker Desktop source address)', async () => {
    window.history.pushState(null, '', `/noc#nmk_${'y'.repeat(43)}`);
    vi.stubGlobal('WebSocket', FakeWs);
    mockFetch(() => ({
      status: 429,
      body: { error: { code: 'rate_limited', message: 'Too many requests', details: null } },
    }));
    render(
      <QueryClientProvider client={new QueryClient()}>
        <KioskPage />
      </QueryClientProvider>,
    );
    const el = await screen.findByTestId('kiosk-error');
    expect(el.textContent).toMatch(/too many|יותר מדי/i);
    window.history.pushState(null, '', '/');
  });
});

describe('[3] kiosk "Show URL once" dialog', () => {
  it('builds the URL from the origin when the server has no public URL and draws a QR code', () => {
    const token = `nmk_${'q'.repeat(43)}`;
    render(
      <ShowUrlDialog
        resp={{ token, url: null, item: { name: 'Wall 1' } as never }}
        onClose={() => undefined}
      />,
    );
    const input = screen.getByDisplayValue(`${window.location.origin}/noc#${token}`);
    expect(input).toBeTruthy();
    expect(screen.getByTestId('qr').querySelector('path')?.getAttribute('d')).toMatch(/^M\d/);
  });
});
