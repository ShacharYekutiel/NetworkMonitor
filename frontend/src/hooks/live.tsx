import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { LiveSocket, type WsState } from '../api/ws';
import type { WsMessage } from '../api/types';
import { qk } from '../api/endpoints';
import { applyLiveMessage, cancelArpRefetch, InvalidationBatcher } from '../lib/liveCache';
import { useAuth } from './auth';

const Ctx = createContext<WsState>('closed');
const SocketCtx = createContext<LiveSocket | null>(null);

export function LiveProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const { state: authState, expire, setupRequired } = useAuth();
  const [wsState, setWsState] = useState<WsState>('closed');

  const socket = useMemo(
    () =>
      new LiveSocket({
        onUnauthorized: () => expire(),
        onSetupRequired: () => setupRequired(),
        onReconnect: () => {
          void qc.invalidateQueries({ queryKey: qk.tree });
          void qc.invalidateQueries({ queryKey: qk.dashboard });
          // Discovery frames may have been missed while disconnected (api.md).
          void qc.invalidateQueries({ queryKey: ['discoveryScan'] });
          void qc.invalidateQueries({ queryKey: ['discoveryResults'] });
          void qc.invalidateQueries({ queryKey: ['discoveryScans'] });
          // [2b-1] maintenance coverage, acks and the topology may have changed meanwhile.
          void qc.invalidateQueries({ queryKey: qk.maintenanceActive });
          void qc.invalidateQueries({ queryKey: ['maintenanceWindows'] });
          void qc.invalidateQueries({ queryKey: ['topology'] });
          void qc.invalidateQueries({ queryKey: ['sensors'] });
          void qc.invalidateQueries({ queryKey: ['sensor'] });
          void qc.invalidateQueries({ queryKey: ['device'] });
          // [2b-2] port runs and template reloads may have happened meanwhile.
          void qc.invalidateQueries({ queryKey: ['device-ports'] });
          void qc.invalidateQueries({ queryKey: ['port-detail'] });
          void qc.invalidateQueries({ queryKey: qk.deviceTemplates });
          // [2b-3] feature toggles, rules, conditions and ingest state may have changed meanwhile.
          void qc.invalidateQueries({ queryKey: qk.publicSettings });
          void qc.invalidateQueries({ queryKey: ['event-rules'] });
          void qc.invalidateQueries({ queryKey: ['conditions'] });
          void qc.invalidateQueries({ queryKey: qk.ingestStatus });
        },
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [qc],
  );

  useEffect(() => {
    if (authState !== 'authenticated') return;
    const batch = new InvalidationBatcher(qc);
    const offMsg = socket.subscribe((m) => applyLiveMessage(qc, m, batch));
    const offState = socket.onState(setWsState);
    socket.start();
    return () => {
      offMsg();
      offState();
      socket.stop();
      batch.dispose();
      cancelArpRefetch(qc);
    };
  }, [authState, socket, qc]);

  return (
    <SocketCtx.Provider value={socket}>
      <Ctx.Provider value={wsState}>{children}</Ctx.Provider>
    </SocketCtx.Provider>
  );
}

/**
 * [2b-3] Receives every WS message of the given types while mounted; when `topic` is set the
 * server topic is subscribed for that time (reference-counted, re-sent after reconnects).
 */
export function useLiveMessages(
  types: WsMessage['type'][],
  handler: (msg: WsMessage) => void,
  topic?: string | null,
): void {
  const socket = useContext(SocketCtx);
  const ref = useRef(handler);
  ref.current = handler;
  const key = types.join(',');
  useEffect(() => {
    if (!socket) return;
    const wanted = new Set(key.split(','));
    const off = socket.subscribe((m) => {
      if (wanted.has(m.type)) ref.current(m);
    });
    const release = topic ? socket.subscribeTopic(topic) : null;
    return () => {
      off();
      release?.();
    };
  }, [socket, key, topic]);
}

export function useLiveState(): WsState {
  return useContext(Ctx);
}
