// [3b] Streaming an answer (ask / explain) with re-attach and Stop (architecture § 14.3.3).
import { useCallback, useEffect, useRef, useState } from 'react';
import { isApiError } from '../api/client';
import { endpoints3b, type StreamHandlers } from '../api/endpoints3b';
import type { StreamEvent } from '../api/types';
import { applyStreamEvent, EMPTY_LIVE, type LiveAnswer } from '../lib/assistant';

export type PreStreamError = {
  code: string;
  message: string;
  status: number;
  details: Record<string, unknown>[];
};

const MAX_REATTACH = 5;

export function useAnswerStream(opts: { onStart?: (conversationId: number) => void } = {}) {
  const [live, setLive] = useState<LiveAnswer>(EMPTY_LIVE);
  const [running, setRunning] = useState(false);
  const [preError, setPreError] = useState<PreStreamError | null>(null);
  const liveRef = useRef(live);
  const abortRef = useRef<AbortController | null>(null);
  const onStartRef = useRef(opts.onStart);
  onStartRef.current = opts.onStart;
  const genRef = useRef(0);

  useEffect(
    () => () => {
      // leaving the page does NOT cancel the run on the server (the paid answer is kept)
      abortRef.current?.abort();
    },
    [],
  );

  const run = useCallback(
    async (open: (h: StreamHandlers) => Promise<boolean>, initial: LiveAnswer = EMPTY_LIVE) => {
      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      const gen = ++genRef.current;
      liveRef.current = initial;
      setLive(initial);
      setPreError(null);
      setRunning(true);
      const onEvent = (ev: StreamEvent) => {
        if (gen !== genRef.current) return;
        const next = applyStreamEvent(liveRef.current, ev);
        liveRef.current = next;
        setLive(next);
        if (ev.event === 'start') onStartRef.current?.(ev.data.conversation_id);
      };
      try {
        let done = await open({ onEvent, signal: ctrl.signal });
        let tries = 0;
        // connection cut before `done`: re-attach with Last-Event-ID (the run continues server-side)
        while (!done && !ctrl.signal.aborted && liveRef.current.messageId && tries < MAX_REATTACH) {
          tries++;
          await new Promise((r) => setTimeout(r, Math.min(4000, 500 * tries)));
          if (ctrl.signal.aborted) break;
          try {
            done = await endpoints3b.attach(
              liveRef.current.messageId,
              liveRef.current.lastEventId,
              {
                onEvent,
                signal: ctrl.signal,
              },
            );
          } catch (e) {
            if (e instanceof DOMException && e.name === 'AbortError') throw e;
            if (isApiError(e) && e.status === 404) break;
          }
        }
        if (!done && !ctrl.signal.aborted && gen === genRef.current) {
          const n = {
            ...liveRef.current,
            error: liveRef.current.error ?? {
              code: 'stream_interrupted',
              message: 'The connection to the answer was lost.',
            },
          };
          liveRef.current = n;
          setLive(n);
        }
      } catch (e) {
        if (e instanceof DOMException && e.name === 'AbortError') return;
        if (gen !== genRef.current) return;
        if (isApiError(e))
          setPreError({
            code: e.code,
            message: e.message,
            status: e.status,
            details: (e.details ?? []) as Record<string, unknown>[],
          });
        else
          setPreError({
            code: 'network_error',
            message: e instanceof Error ? e.message : String(e),
            status: 0,
            details: [],
          });
      } finally {
        if (gen === genRef.current) setRunning(false);
      }
    },
    [],
  );

  const ask = useCallback(
    (conversationId: number | null, question: string) =>
      run((h) => endpoints3b.ask(conversationId, question, h)),
    [run],
  );
  const explain = useCallback(
    (sensorId: number, eventId: number | null, refresh = false) =>
      run((h) => endpoints3b.explain({ sensor_id: sensorId, event_id: eventId, refresh }, h)),
    [run],
  );
  /** Re-attach to a running (or finished) message: replays its buffered events. */
  const attach = useCallback(
    (messageId: number) =>
      run((h) => endpoints3b.attach(messageId, null, h), { ...EMPTY_LIVE, messageId }),
    [run],
  );
  /** Stop: cancels the run on the server (partial text is kept, status `cancelled`). */
  const stop = useCallback(async () => {
    const id = liveRef.current.messageId;
    if (!id) {
      abortRef.current?.abort();
      setRunning(false);
      return;
    }
    try {
      await endpoints3b.cancel(id);
    } catch {
      abortRef.current?.abort();
      setRunning(false);
    }
  }, []);
  /** Forget the live state (the stream keeps running on the server). */
  const reset = useCallback(() => {
    genRef.current++;
    abortRef.current?.abort();
    liveRef.current = EMPTY_LIVE;
    setLive(EMPTY_LIVE);
    setPreError(null);
    setRunning(false);
  }, []);

  return { live, running, preError, ask, explain, attach, stop, reset };
}
