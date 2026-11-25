import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { LiveSocket, WsState } from '../../api/ws';
import type { CursorPage, WsMessage } from '../../api/types';
import { LiveSocketContext, WsStateContext } from '../../hooks/live';
import { withRepeat } from '../../lib/logs';
import { useLogFeed } from './useLogFeed';

type Row = { id: number; time: string; repeat_count: number; last_seen: string | null };

/**
 * Production finding (1.5.0, Logs → Syslog): the API returned a row with repeat_count=2 while the
 * list showed it without "×2". A displayed row's count was only ever updated by `syslog.repeats`
 * frames; frames sent while Live was off or the socket was reconnecting are not replayed, and
 * switching Live on / reconnecting did not reload the list, so the row stayed at ×1.
 */
function setup(opts: { live: boolean; ws: WsState }) {
  const server = {
    rows: [{ id: 7, time: '2026-11-25T10:00:00Z', repeat_count: 1, last_seen: null }] as Row[],
  };
  const fetchPage = vi.fn(async (): Promise<CursorPage<Row>> => ({
    items: server.rows.map((r) => ({ ...r })),
    next_cursor: null,
    limit: 100,
  }));
  const listeners = new Set<(m: WsMessage) => void>();
  const socket = {
    subscribe: (l: (m: WsMessage) => void) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    subscribeTopic: () => () => {},
  } as unknown as LiveSocket;
  const state = { ws: opts.ws };
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <LiveSocketContext.Provider value={socket}>
        <WsStateContext.Provider value={state.ws}>{children}</WsStateContext.Provider>
      </LiveSocketContext.Provider>
    </QueryClientProvider>
  );
  const hook = renderHook(
    ({ live }: { live: boolean }) =>
      useLogFeed<Row>({
        queryKey: ['syslog', 'test'],
        fetchPage,
        enabled: true,
        live,
        topic: 'syslog',
        matches: () => true,
      }),
    { wrapper, initialProps: { live: opts.live } },
  );
  return { hook, server, fetchPage, state };
}

describe('useLogFeed resync (syslog repeat counts of rows on screen)', () => {
  it('switching Live on reloads the list, so a row folded meanwhile shows its new count', async () => {
    const { hook, server, fetchPage } = setup({ live: false, ws: 'open' });
    await waitFor(() => expect(hook.result.current.items[0]?.repeat_count).toBe(1));
    // a second identical message is folded into row 7 while Live is off: no frame reaches us
    server.rows[0] = { ...server.rows[0], repeat_count: 2, last_seen: '2026-11-25T10:00:30Z' };
    hook.rerender({ live: true });
    await waitFor(() => expect(hook.result.current.items[0].repeat_count).toBe(2));
    expect(fetchPage).toHaveBeenCalledTimes(2);
  });

  it('the socket coming back while Live is on reloads the list', async () => {
    const { hook, server, fetchPage, state } = setup({ live: true, ws: 'open' });
    await waitFor(() => expect(hook.result.current.items).toHaveLength(1));
    expect(fetchPage).toHaveBeenCalledTimes(1); // already connected at mount: no extra load
    state.ws = 'closed';
    hook.rerender({ live: true });
    server.rows[0] = { ...server.rows[0], repeat_count: 4, last_seen: '2026-11-25T10:00:50Z' };
    state.ws = 'open';
    hook.rerender({ live: true });
    await waitFor(() => expect(hook.result.current.items[0].repeat_count).toBe(4));
    expect(fetchPage).toHaveBeenCalledTimes(2);
  });

  it('no reload while Live stays off (the list is a snapshot until Refresh)', async () => {
    const { hook, fetchPage, state } = setup({ live: false, ws: 'open' });
    await waitFor(() => expect(hook.result.current.items).toHaveLength(1));
    state.ws = 'closed';
    hook.rerender({ live: false });
    state.ws = 'open';
    await act(async () => hook.rerender({ live: false }));
    expect(fetchPage).toHaveBeenCalledTimes(1);
  });
});

describe('withRepeat', () => {
  const row = { id: 7, repeat_count: 4, last_seen: '2026-11-25T10:00:50Z' };
  it('a newer live count wins', () => {
    expect(withRepeat(row, { repeat_count: 5, last_seen: '2026-11-25T10:00:55Z' })).toEqual({
      id: 7,
      repeat_count: 5,
      last_seen: '2026-11-25T10:00:55Z',
    });
  });
  it('an older live value never hides a newer fetched count', () => {
    expect(withRepeat(row, { repeat_count: 2, last_seen: '2026-11-25T10:00:30Z' })).toBe(row);
    expect(withRepeat(row, undefined)).toBe(row);
  });
});
