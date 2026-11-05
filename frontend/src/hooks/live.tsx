import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { LiveSocket, type WsState } from '../api/ws';
import { qk } from '../api/endpoints';
import { applyLiveMessage, InvalidationBatcher } from '../lib/liveCache';
import { useAuth } from './auth';

const Ctx = createContext<WsState>('closed');

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
    };
  }, [authState, socket, qc]);

  return <Ctx.Provider value={wsState}>{children}</Ctx.Provider>;
}

export function useLiveState(): WsState {
  return useContext(Ctx);
}
