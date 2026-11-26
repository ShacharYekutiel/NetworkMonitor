import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useInfiniteQuery } from '@tanstack/react-query';
import type { CursorPage, WsMessage } from '../../api/types';
import { useLiveMessages, useLiveState } from '../../hooks/live';
import { mergeNewest } from '../../lib/logs';

const LIVE_MAX = 2000;

/**
 * [2b-3] Cursor-paged records ("Load more" = next_cursor) plus an optional live tail: records
 * from `syslog.batch` / `trap.batch` that match the filter are prepended; while the list is
 * scrolled away from the top they are held back ("N new — show").
 *
 * Whenever the live tail (re)connects — Live switched on, or the socket came back after a drop —
 * the loaded pages are fetched again: frames sent while it was not connected (new records and,
 * for syslog, the `syslog.repeats` counts of rows already on screen) are not replayed, so without
 * this a row stayed at "×1" although the server had folded more messages into it.
 */
export function useLogFeed<T extends { id: number; time: string }>(opts: {
  queryKey: readonly unknown[];
  fetchPage: (cursor: string | null) => Promise<CursorPage<T>>;
  enabled: boolean;
  live: boolean;
  topic: 'syslog' | 'trap';
  matches: (r: T) => boolean;
}) {
  const { queryKey, fetchPage, enabled, live, topic, matches } = opts;
  const q = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam }) => fetchPage(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
  const [liveItems, setLiveItems] = useState<T[]>([]);
  const [pending, setPending] = useState<T[]>([]);
  const [atTop, setAtTop] = useState(true);
  const atTopRef = useRef(true);
  atTopRef.current = atTop;
  const matchRef = useRef(matches);
  matchRef.current = matches;
  const keyStr = JSON.stringify(queryKey);

  useEffect(() => {
    setLiveItems([]);
    setPending([]);
  }, [keyStr]);

  const onMsg = useCallback((m: WsMessage) => {
    if (m.type !== 'syslog.batch' && m.type !== 'trap.batch') return;
    const items = (m.data.items as unknown as T[]).filter((x) => matchRef.current(x));
    if (!items.length) return;
    if (atTopRef.current) setLiveItems((prev) => mergeNewest(items, prev, LIVE_MAX));
    else setPending((prev) => mergeNewest(items, prev, LIVE_MAX));
  }, []);
  useLiveMessages(
    [topic === 'syslog' ? 'syslog.batch' : 'trap.batch'],
    onMsg,
    live && enabled ? topic : null,
  );

  const wsState = useLiveState();
  const connected = live && enabled && wsState === 'open';
  const wasConnected = useRef(connected);
  const hasData = !!q.data;
  const { refetch } = q;
  useEffect(() => {
    const was = wasConnected.current;
    wasConnected.current = connected;
    if (connected && !was && hasData) void refetch();
  }, [connected, hasData, refetch]);

  const showPending = useCallback(() => {
    setLiveItems((prev) => mergeNewest(pending, prev, LIVE_MAX));
    setPending([]);
  }, [pending]);

  // Back at the top: the held-back records join the list.
  useEffect(() => {
    if (atTop && pending.length) showPending();
  }, [atTop, pending.length, showPending]);

  const paged = useMemo(() => q.data?.pages.flatMap((p) => p.items) ?? [], [q.data]);
  const items = useMemo(
    () => (liveItems.length ? mergeNewest(liveItems, paged, Number.MAX_SAFE_INTEGER) : paged),
    [liveItems, paged],
  );

  /** Pass to the scroll container's onScroll. */
  const onScroll = useCallback((el: HTMLElement) => {
    const top = el.scrollTop < 24;
    if (top !== atTopRef.current) setAtTop(top);
  }, []);

  return {
    items,
    pendingCount: pending.length,
    showPending,
    onScroll,
    atTop,
    isLoading: q.isLoading,
    error: q.error,
    hasMore: !!q.hasNextPage,
    loadMore: () => void q.fetchNextPage(),
    loadingMore: q.isFetchingNextPage,
    refetch: () => void q.refetch(),
  };
}
