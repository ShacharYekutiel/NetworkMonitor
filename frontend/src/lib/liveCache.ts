import type { QueryClient, QueryKey } from '@tanstack/react-query';
import { qk } from '../api/endpoints';
import type {
  Dashboard,
  Device,
  DiscoveryResult,
  DiscoveryScan,
  Group,
  Page,
  Sensor,
  Tree,
  TopologyGraph,
  WsMessage,
} from '../api/types';
import {
  applyScanProgress,
  applyScanProgressToPage,
  bufferResult,
  clearBuffered,
  mergeResult,
} from './discovery';
import { patchTreeDevice, patchTreeGroup, patchTreeSensor, patchTreeDeviceFlags } from './tree';

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
        // [2b-1] any status change clears the acknowledgement (sensor.ack follows as well).
        acknowledgement:
          d.acknowledged === false || (d.status_changed && d.acknowledged !== true)
            ? null
            : s.acknowledgement,
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
          ...(d.acknowledged !== undefined ? { acknowledged: d.acknowledged } : {}),
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
      qc.setQueriesData<TopologyGraph>({ queryKey: ['topology'] }, (old) =>
        patchTopologyNode(old, id, { status }),
      );
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
        case 'discovery_profile':
          batch.add(qk.discoveryProfiles);
          break;
        case 'maintenance_window':
          batch.add(['maintenanceWindows']);
          batch.add(['maintenanceWindow', id]);
          batch.add(['occurrences', id]);
          batch.add(qk.maintenanceActive);
          break;
        case 'topology_link':
          batch.add(['topology']);
          batch.add(['topologyLinks']);
          break;
      }
      // Device/group changes can move dependencies, names and the topology graph.
      if (entity === 'device' || entity === 'group') {
        batch.add(['topology']);
        batch.add(['dependencySuggestions']);
      }
      break;
    }
    case 'sensor.ack': {
      const { id, acknowledgement } = msg.data;
      const patch = (s: Sensor): Sensor => ({ ...s, acknowledgement });
      qc.setQueryData<Sensor>(qk.sensor(id), (old) => (old ? patch(old) : old));
      patchPages<Sensor>(qc, 'sensors', id, patch);
      qc.setQueryData<Tree>(qk.tree, (old) =>
        old ? patchTreeSensor(old, id, { acknowledged: acknowledgement !== null }) : old,
      );
      qc.setQueryData<Dashboard>(qk.dashboard, (old) =>
        old ? applyAckToDashboard(old, id, acknowledgement) : old,
      );
      batch.add(qk.dashboard);
      break;
    }
    case 'maintenance.changed': {
      const { entered_device_ids, left_device_ids } = msg.data;
      qc.setQueryData<Tree>(qk.tree, (old) => {
        if (!old) return old;
        let t = old;
        for (const id of entered_device_ids)
          t = patchTreeDeviceFlags(t, id, { in_maintenance: true });
        for (const id of left_device_ids)
          t = patchTreeDeviceFlags(t, id, { in_maintenance: false });
        return t;
      });
      // Per api.md: refetch tree/devices/sensors/dashboard (ends_at, window names, sensors flags).
      batch.add(qk.tree);
      batch.add(['devices']);
      batch.add(['device']);
      batch.add(['sensors']);
      batch.add(['sensor']);
      batch.add(qk.dashboard);
      batch.add(qk.maintenanceActive);
      batch.add(['maintenanceWindows']);
      batch.add(['topology']);
      break;
    }
    case 'topology.updated': {
      batch.add(['topology']);
      batch.add(['topologyLinks']);
      if (msg.data.reason === 'refresh') batch.add(['dependencySuggestions']);
      break;
    }
    case 'topology.refresh': {
      const r = msg.data.refresh;
      qc.setQueriesData<TopologyGraph>({ queryKey: ['topology'] }, (old) =>
        old ? { ...old, last_refresh: r, refresh_running: r.status === 'running' } : old,
      );
      if (r.status !== 'running') batch.add(['topology']);
      break;
    }
    case 'discovery.progress': {
      const d = msg.data;
      const known = qc.getQueryData<DiscoveryScan>(qk.discoveryScan(d.scan_id));
      qc.setQueryData<DiscoveryScan>(qk.discoveryScan(d.scan_id), (old) =>
        applyScanProgress(old, d, msg.ts),
      );
      qc.setQueriesData<Page<DiscoveryScan>>({ queryKey: ['discoveryScans'] }, (old) =>
        applyScanProgressToPage(old, d, msg.ts),
      );
      // A scan we have not seen yet (e.g. started by a schedule or another admin).
      if (!known) batch.add(['discoveryScans']);
      break;
    }
    case 'discovery.result': {
      const { scan_id, result } = msg.data;
      // Always buffer: if the results query has no data yet or a fetch is in flight, the row is
      // merged into that fetch (withBuffered) instead of being lost.
      bufferResult(result);
      qc.setQueryData<DiscoveryResult[]>(qk.discoveryResults(scan_id), (old) =>
        old ? mergeResult(old, result) : old,
      );
      break;
    }
    case 'discovery.finished': {
      const d = msg.data;
      qc.setQueryData<DiscoveryScan>(qk.discoveryScan(d.scan_id), (old) =>
        applyScanProgress(old, d, msg.ts),
      );
      qc.setQueriesData<Page<DiscoveryScan>>({ queryKey: ['discoveryScans'] }, (old) =>
        applyScanProgressToPage(old, d, msg.ts),
      );
      // Per api.md: refetch the scan and its results after discovery.finished (the refetch has
      // every row, so the live buffer is no longer needed).
      clearBuffered(d.scan_id);
      batch.add(qk.discoveryScan(d.scan_id));
      batch.add(qk.discoveryResults(d.scan_id));
      batch.add(['discoveryScans']);
      batch.add(qk.discoveryProfiles);
      break;
    }
    default:
      break;
  }
}

/** Patches one device node of a topology graph (by device id). */
export function patchTopologyNode(
  g: TopologyGraph | undefined,
  deviceId: number,
  patch: { status?: Sensor['status']; in_maintenance?: boolean },
): TopologyGraph | undefined {
  if (!g || !g.nodes.some((n) => n.kind === 'device' && n.device_id === deviceId)) return g;
  return {
    ...g,
    nodes: g.nodes.map((n) =>
      n.kind === 'device' && n.device_id === deviceId ? { ...n, ...patch } : n,
    ),
  };
}

/**
 * Moves a sensor between the dashboard's problem and acknowledged lists in place
 * (the dashboard refetch that follows fixes ordering and limits).
 */
export function applyAckToDashboard(
  d: Dashboard,
  sensorId: number,
  ack: Sensor['acknowledgement'],
): Dashboard {
  const all = [...d.problem_sensors, ...d.acknowledged_sensors];
  const s = all.find((x) => x.id === sensorId);
  if (!s) return d;
  const next = { ...s, acknowledgement: ack };
  const problems = d.problem_sensors.filter((x) => x.id !== sensorId);
  const acked = d.acknowledged_sensors.filter((x) => x.id !== sensorId);
  const wasAcked = d.acknowledged_sensors.some((x) => x.id === sensorId);
  if (ack) {
    return {
      ...d,
      problem_sensors: problems,
      acknowledged_sensors: [...acked, next],
      acknowledged_count: d.acknowledged_count + (wasAcked ? 0 : 1),
    };
  }
  const stillProblem = next.status === 'down' || next.status === 'warning';
  return {
    ...d,
    problem_sensors: stillProblem ? [...problems, next] : problems,
    acknowledged_sensors: acked,
    acknowledged_count: Math.max(0, d.acknowledged_count - (wasAcked ? 1 : 0)),
  };
}
