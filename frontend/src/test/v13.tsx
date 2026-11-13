// [1.3] Test helpers (Vitest only; never imported by the app).
import type { ReactNode } from 'react';
import { vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '../hooks/auth';
import { ToastProvider } from '../components/toast';
import type { Sensor } from '../api/types';

export type Call = { method: string; url: string; body: unknown };
export type Route = (
  method: string,
  url: string,
  body: unknown,
) => { status: number; body?: unknown } | undefined;

/** Stubs global fetch; unknown routes → 404 envelope. Returns the recorded calls. */
export function mockFetch(route: Route): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ method, url, body });
      const r = route(method, url, body) ?? {
        status: 404,
        body: { error: { code: 'not_found', message: 'nf', details: null } },
      };
      return new Response(r.body === undefined ? null : JSON.stringify(r.body), {
        status: r.status,
        headers: { 'Content-Type': 'application/json' },
      });
    }),
  );
  return calls;
}

export const adminUser = {
  id: 1,
  username: 'admin',
  email: null,
  role: 'admin',
  language: 'en',
  is_active: true,
  last_login_at: null,
  created_at: '',
};

/** Routes of an authenticated admin session (AuthProvider boot). */
export function authRoutes(method: string, url: string) {
  if (method === 'GET' && url.startsWith('/api/setup/status'))
    return { status: 200, body: { setup_required: false } };
  if (method === 'GET' && url.startsWith('/api/auth/me'))
    return { status: 200, body: { user: adminUser } };
  return undefined;
}

export function Wrap({ children, auth }: { children: ReactNode; auth?: boolean }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter>{auth ? <AuthProvider>{children}</AuthProvider> : children}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}

export function mkSensor(id: number, name: string, extra: Partial<Sensor> = {}): Sensor {
  return {
    id,
    device_id: 7,
    device_name: 'sw1',
    group_id: 1,
    name,
    type: 'ping',
    config: {},
    interval_s: 60,
    enabled: true,
    status: 'up',
    status_message: '',
    last_check_at: null,
    last_status_change_at: null,
    primary_channel: 'value',
    channels: [],
    sort_order: 0,
    acknowledgement: null,
    in_maintenance: false,
    created_at: '',
    updated_at: '',
    ...extra,
  };
}
