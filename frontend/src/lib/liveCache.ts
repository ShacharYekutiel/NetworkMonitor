import type { QueryClient, QueryKey } from '@tanstack/react-query';
import { qk } from '../api/endpoints';
import type { Dashboard, Device, Group, Page, Sensor, Tree, WsMessage } from '../api/types';
import { patchTreeDevice, patchTreeGroup, patchTreeSensor } from './tree';

/** Collects invalidations and flushes them in one batch, so bursts of WS messages stay cheap. */
export class InvalidationBatcher {
  private keys = new Map<string, QueryKey>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  constructor(
    private readonly qc: QueryClient,
    private readonly delayMs = 400,
  ) {}

  add(key: QueryKey) {
    this.keys.set(JSON.stringify(key), key);
    if (!this.timer) this.timer = setTimeout(() => this.flush(), this.delayMs);
  }

  flush() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    const keys = [...this.keys.values()];
    this.keys.clear();
    for (const k of keys) void this.qc.invalidateQueries({ queryKey: k });
  }

  dispose() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.keys.clear();
  }
}

function patchPages<T extends { id: number }>(
  qc: QueryClient,
  prefix: string,
  id: number,
  fn: (item: T) => T,
) {
  qc.setQueriesData<Page<T>>({ queryKey: [prefix] }, (old) => {
    if (!old || !Array.isArray(old.items)) return old;
    if (!old.items.some((x) => x.id === id)) return old;
    return { ...old, items: old.items.map((x) => (x.id === id ? fn(x) : x)) };
  });
}

/** Applies one WebSocket message to the TanStack Query cache. */
export function applyLiveMessage(
  qc: QueryClient,
  msg: WsMessage,
  batch: InvalidationBatcher,
): void {
  switch (msg.type) {
    case 'sensor.update': {
      const d = msg.data;
      const patchSensor = (s: Sensor): Sensor => ({
        ...s,
        status: d.status,
        status_message: d.status_message,
        last_check_at: d.last_check_at,
        last_status_change_at: d.status_changed ? msg.ts : s.last_status_change_at,
        channels: s.channels.map((c) => {
          const u = d.channels.find((x) => x.key === c.key);
          return u ? { ...c, last_value: u.value, status: u.status } : c;
        }),
      });
      qc.setQueryData<Sensor>(qk.sensor(d.id), (old) => (old ? patchSensor(old) : old));
      patchPages<Sensor>(qc, 'sensors', d.id, patchSensor);
      qc.setQueryData<Tree>(qk.tree, (old) => {
        if (!old) return old;
        return patchTreeSensor(old, d.id, {
          status: d.status,
          status_message: d.status_message,
          last_check_at: d.last_check_at,
        });
      });
      // primary_value needs the primary channel key, which the tree node carries.
      qc.setQueryData<Tree>(qk.tree, (old) => {
        if (!old) return old;
        let pv: number | null | undefined;
        const visit = (gs: Tree['groups']) => {
          for (const g of gs) {
            for (const dev of g.devices)
              for (const s of dev.sensors)
                if (s.id === d.id) pv = d.channels.find((c) => c.key === s.primary_channel)?.value;
            visit(g.children);
          }
        };
        visit(old.groups);
        return pv === undefined ? old : patchTreeSensor(old, d.id, { primary_value: pv });
      });
      batch.add(['history', d.id]);
      if (d.status_changed) batch.add(qk.dashboard);
      break;
    }
    case 'event.created': {
      const ev = msg.data;
      if (ev.kind === 'status_change') {
        qc.setQueryData<Dashboard>(qk.dashboard, (old) =>
          old && !old.recent_events.some((e) => e.id === ev.id)
            ? { ...old, recent_events: [ev, ...old.recent_events].slice(0, 20) }
            : old,
        );
        batch.add(qk.dashboard);
      }
      batch.add(['events']);
      break;
    }
    case 'device.status': {
      const { id, status, status_counts } = msg.data;
      qc.setQueryData<Tree>(qk.tree, (old) =>
        old ? patchTreeDevice(old, id, { status, status_counts }) : old,
      );
      qc.setQueryData<Device>(qk.device(id), (old) =>
        old ? { ...old, status, status_counts } : old,
      );
      patchPages<Device>(qc, 'devices', id, (x) => ({ ...x, status, status_counts }));
      batch.add(qk.dashboard);
      break;
    }
    case 'group.status': {
      const { id, status, status_counts } = msg.data;
      qc.setQueryData<Tree>(qk.tree, (old) =>
        old ? patchTreeGroup(old, id, { status, status_counts }) : old,
      );
      qc.setQueryData<Group>(qk.group(id), (old) =>
        old ? { ...old, status, status_counts } : old,
      );
      patchPages<Group>(qc, 'groups', id, (x) => ({ ...x, status, status_counts }));
      break;
    }
    case 'config.changed': {
      const { entity, id } = msg.data;
      batch.add(qk.tree);
      batch.add(qk.dashboard);
      switch (entity) {
        case 'group':
          batch.add(['group', id]);
          batch.add(['groups']);
          break;
        case 'device':
          batch.add(['device', id]);
          batch.add(['devices']);
          batch.add(['sensors']);
          batch.add(['snmpInterfaces', id]);
          break;
        case 'sensor':
          batch.add(['sensor', id]);
          batch.add(['sensors']);
          batch.add(['snmpInterfaces']);
          batch.add(['alertRules']);
          break;
        case 'alert_rule':
          batch.add(['alertRules']);
          break;
        case 'notification_channel':
          batch.add(qk.channels);
          break;
      }
      break;
    }
    default:
      break;
  }
}
