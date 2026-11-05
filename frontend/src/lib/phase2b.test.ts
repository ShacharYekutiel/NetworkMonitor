import { describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import type { Dashboard, Sensor, TopologyGraph, Tree } from '../api/types';
import { ApiError } from '../api/client';
import i18n from '../i18n';
import { parseUnreachable } from '../components/Dependency';
import { dependencyToInput } from '../components/Dependency';
import { errorMessage } from './errors';
import { sensorValueText } from './format';
import { eventMarkers } from './history';
import { applyAckToDashboard, applyLiveMessage, InvalidationBatcher } from './liveCache';
import {
  buildSensorConfig,
  mapEnumValue,
  rawFromConfig,
  rowsToValueMap,
  valueMapToRows,
  visibleConfigFields,
} from './sensorConfig';
import type { ConfigField } from '../api/types';

const counts = { up: 1, warning: 0, down: 0, paused: 0, unknown: 0, unreachable: 0, total: 1 };
const tree = (): Tree => ({
  groups: [
    {
      id: 1,
      name: 'Default',
      parent_id: null,
      status: 'up',
      status_counts: counts,
      children: [],
      devices: [
        {
          id: 3,
          name: 'ap-2',
          host: '10.0.1.20',
          enabled: true,
          status: 'down',
          status_counts: counts,
          in_maintenance: false,
          parent_device_id: 2,
          sensors: [
            {
              id: 7,
              name: 'Ping',
              type: 'ping',
              enabled: true,
              status: 'down',
              status_message: '',
              primary_channel: 'latency_ms',
              primary_value: null,
              primary_unit: 'ms',
              last_check_at: null,
              acknowledged: false,
            },
          ],
        },
      ],
    },
  ],
});
const sensor = (p: Partial<Sensor> = {}): Sensor =>
  ({
    id: 7,
    device_id: 3,
    name: 'Ping',
    status: 'down',
    acknowledgement: null,
    channels: [],
    ...p,
  }) as Sensor;
const ack = {
  by: 'admin',
  at: '2026-11-05T09:12:00Z',
  comment: 'Replacing PSU',
  status: 'down' as const,
};

describe('live cache (2b-1 messages)', () => {
  it('sensor.ack patches the sensor, the tree and the dashboard lists', () => {
    const qc = new QueryClient();
    qc.setQueryData(['tree'], tree());
    qc.setQueryData(['sensor', 7], sensor());
    qc.setQueryData<Dashboard>(['dashboard'], {
      problem_sensors: [sensor()],
      acknowledged_sensors: [],
      acknowledged_count: 0,
    } as unknown as Dashboard);
    const batch = new InvalidationBatcher(qc, 10_000);
    applyLiveMessage(
      qc,
      { type: 'sensor.ack', ts: '', data: { id: 7, device_id: 3, acknowledgement: ack } },
      batch,
    );
    expect(qc.getQueryData<Sensor>(['sensor', 7])!.acknowledgement).toEqual(ack);
    expect(qc.getQueryData<Tree>(['tree'])!.groups[0].devices[0].sensors[0].acknowledged).toBe(
      true,
    );
    const d = qc.getQueryData<Dashboard>(['dashboard'])!;
    expect(d.problem_sensors).toHaveLength(0);
    expect(d.acknowledged_sensors.map((s) => s.id)).toEqual([7]);
    expect(d.acknowledged_count).toBe(1);
    // auto-clear
    applyLiveMessage(
      qc,
      { type: 'sensor.ack', ts: '', data: { id: 7, device_id: 3, acknowledgement: null } },
      batch,
    );
    const d2 = qc.getQueryData<Dashboard>(['dashboard'])!;
    expect(d2.problem_sensors.map((s) => s.id)).toEqual([7]);
    expect(d2.acknowledged_count).toBe(0);
    batch.dispose();
  });

  it('sensor.update clears the acknowledgement on a status change', () => {
    const qc = new QueryClient();
    qc.setQueryData(['sensor', 7], sensor({ acknowledgement: ack }));
    const batch = new InvalidationBatcher(qc, 10_000);
    applyLiveMessage(
      qc,
      {
        type: 'sensor.update',
        ts: 't',
        data: {
          id: 7,
          device_id: 3,
          status: 'unreachable',
          status_message: 'Unreachable (parent branch-gw down): timeout',
          last_check_at: null,
          status_changed: true,
          channels: [],
          acknowledged: false,
        },
      },
      batch,
    );
    expect(qc.getQueryData<Sensor>(['sensor', 7])!.acknowledgement).toBeNull();
    expect(qc.getQueryData<Sensor>(['sensor', 7])!.status).toBe('unreachable');
    batch.dispose();
  });

  it('maintenance.changed flags tree devices and refetches', () => {
    const qc = new QueryClient();
    qc.setQueryData(['tree'], tree());
    const spy = vi.spyOn(qc, 'invalidateQueries');
    const batch = new InvalidationBatcher(qc, 10_000);
    applyLiveMessage(
      qc,
      {
        type: 'maintenance.changed',
        ts: '',
        data: { active_window_ids: [3], entered_device_ids: [3], left_device_ids: [] },
      },
      batch,
    );
    expect(qc.getQueryData<Tree>(['tree'])!.groups[0].devices[0].in_maintenance).toBe(true);
    batch.flush();
    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    for (const k of [
      '["tree"]',
      '["devices"]',
      '["sensors"]',
      '["dashboard"]',
      '["maintenanceActive"]',
      '["topology"]',
    ])
      expect(keys).toContain(k);
  });

  it('topology messages and the new config.changed entities', () => {
    const qc = new QueryClient();
    const g = { nodes: [], links: [], last_refresh: null, refresh_running: false } as TopologyGraph;
    qc.setQueryData(['topology', {}], g);
    const spy = vi.spyOn(qc, 'invalidateQueries');
    const batch = new InvalidationBatcher(qc, 10_000);
    const refresh = {
      id: 12,
      trigger: 'manual' as const,
      status: 'running' as const,
      started_at: 't',
      finished_at: null,
      devices_total: 3,
      devices_ok: 0,
      devices_failed: 0,
      links_found: 0,
      error: null,
    };
    applyLiveMessage(qc, { type: 'topology.refresh', ts: '', data: { refresh } }, batch);
    expect(qc.getQueryData<TopologyGraph>(['topology', {}])!.refresh_running).toBe(true);
    applyLiveMessage(
      qc,
      { type: 'topology.refresh', ts: '', data: { refresh: { ...refresh, status: 'completed' } } },
      batch,
    );
    expect(qc.getQueryData<TopologyGraph>(['topology', {}])!.refresh_running).toBe(false);
    applyLiveMessage(
      qc,
      { type: 'topology.updated', ts: '', data: { reason: 'link', refresh_id: null } },
      batch,
    );
    applyLiveMessage(
      qc,
      {
        type: 'config.changed',
        ts: '',
        data: { entity: 'maintenance_window', id: 3, action: 'updated' },
      },
      batch,
    );
    applyLiveMessage(
      qc,
      {
        type: 'config.changed',
        ts: '',
        data: { entity: 'topology_link', id: 4, action: 'deleted' },
      },
      batch,
    );
    batch.flush();
    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    for (const k of [
      '["topology"]',
      '["topologyLinks"]',
      '["maintenanceWindows"]',
      '["maintenanceWindow",3]',
      '["occurrences",3]',
    ])
      expect(keys).toContain(k);
  });

  it('device.status patches topology nodes', () => {
    const qc = new QueryClient();
    qc.setQueryData(['topology', {}], {
      nodes: [{ key: 'd:3', kind: 'device', device_id: 3, status: 'up' }],
      links: [],
    } as unknown as TopologyGraph);
    const batch = new InvalidationBatcher(qc, 10_000);
    applyLiveMessage(
      qc,
      {
        type: 'device.status',
        ts: '',
        data: { id: 3, status: 'unreachable', status_counts: counts },
      },
      batch,
    );
    const n = qc.getQueryData<TopologyGraph>(['topology', {}])!.nodes[0];
    expect(n.kind === 'device' && n.status).toBe('unreachable');
    batch.dispose();
  });

  it('applyAckToDashboard ignores unknown sensors', () => {
    const d = {
      problem_sensors: [],
      acknowledged_sensors: [],
      acknowledged_count: 0,
    } as unknown as Dashboard;
    expect(applyAckToDashboard(d, 99, ack)).toBe(d);
  });
});

describe('dependencies', () => {
  it('parses the unreachable message prefix', () => {
    expect(parseUnreachable('Unreachable (parent branch-gw down): Request timed out')).toEqual({
      parent: 'branch-gw',
      rest: 'Request timed out',
    });
    expect(parseUnreachable('Unreachable (parent core (A) down)')).toEqual({
      parent: 'core (A)',
      rest: '',
    });
    expect(parseUnreachable('Request timed out')).toBeNull();
  });

  it('normalises the dependency body (ids null unless device)', () => {
    expect(dependencyToInput({ mode: 'inherit', device_id: 3, sensor_id: 4 })).toEqual({
      mode: 'inherit',
      device_id: null,
      sensor_id: null,
    });
    expect(dependencyToInput({ mode: 'device', device_id: 2, sensor_id: 11 })).toEqual({
      mode: 'device',
      device_id: 2,
      sensor_id: 11,
    });
  });

  it('shows the dependency_cycle path with device names', async () => {
    await i18n.changeLanguage('en');
    const e = new ApiError(400, 'dependency_cycle', 'cycle', [{ device_ids: [3, 7, 3] }]);
    const names: Record<number, string> = { 3: 'sw-a', 7: 'sw-b' };
    expect(errorMessage(i18n.t, e, { deviceName: (id) => names[id] })).toBe(
      'This dependency would create a loop: sw-a → sw-b → sw-a.',
    );
    expect(errorMessage(i18n.t, e)).toContain('#3 → #7 → #3');
    expect(errorMessage(i18n.t, new ApiError(409, 'not_acknowledgeable', 'x'))).toBe(
      'Only down or warning sensors can be acknowledged.',
    );
  });
});

describe('snmp_custom enum value map', () => {
  const f = (
    key: string,
    type: ConfigField['type'],
    extra: Partial<ConfigField> = {},
  ): ConfigField => ({
    key,
    type,
    required: false,
    default: null,
    min: null,
    max: null,
    options: null,
    ...extra,
  });
  const fields: ConfigField[] = [
    f('oid', 'str', { required: true }),
    f('value_type', 'enum', { default: 'gauge', options: ['gauge', 'counter', 'string', 'enum'] }),
    f('unit', 'str', { default: '' }),
    f('multiplier', 'float', { default: 1 }),
    f('percent_of_oid', 'str_nullable'),
    f('expected', 'str_nullable'),
    f('value_map', 'value_map', { default: {} }),
    f('value_map_default', 'enum', { default: 'warning', options: ['up', 'warning', 'down'] }),
  ];

  it('round-trips the map through the form rows', () => {
    const config = {
      oid: '1.3.6.1.4.1.11.2.14.11.1.2.6.1.4',
      value_type: 'enum',
      value_map: { '4': { status: 'up', text: 'good' }, '2': { status: 'down', text: 'bad' } },
      value_map_default: 'warning',
    };
    const raw = rawFromConfig(fields, config);
    expect(visibleConfigFields('snmp_custom', fields, raw).map((x) => x.key)).toEqual([
      'oid',
      'value_type',
      'value_map',
      'value_map_default',
    ]);
    const { config: out, errors } = buildSensorConfig('snmp_custom', fields, raw);
    expect(errors).toEqual({});
    expect(out.value_map).toEqual({
      '2': { status: 'down', text: 'bad' },
      '4': { status: 'up', text: 'good' },
    });
    expect(out.value_map_default).toBe('warning');
    expect(out.percent_of_oid).toBeNull();
    expect(valueMapToRows(out.value_map).map((r) => r.value)).toEqual(['2', '4']);
  });

  it('validates the rows and requires a non-empty map for enum', () => {
    expect(rowsToValueMap([{ value: 'x', status: 'up', text: '' }]).error).toBe(
      'valueMap.err.integer',
    );
    expect(
      rowsToValueMap([
        { value: '1', status: 'up', text: '' },
        { value: '01', status: 'down', text: '' },
      ]).error,
    ).toBe('valueMap.err.duplicate');
    expect(rowsToValueMap([{ value: '1', status: 'up', text: 'x'.repeat(33) }]).error).toBe(
      'valueMap.err.text',
    );
    expect(rowsToValueMap([{ value: '', status: 'up', text: '' }])).toEqual({
      map: {},
      error: null,
    });
    const raw = rawFromConfig(fields, { oid: '1.3', value_type: 'enum', value_map: {} });
    expect(buildSensorConfig('snmp_custom', fields, raw).errors.value_map).toBe(
      'valueMap.err.empty',
    );
    // gauge does not show/require the map
    const g = rawFromConfig(fields, { oid: '1.3', value_type: 'gauge' });
    expect(buildSensorConfig('snmp_custom', fields, g).errors).toEqual({});
  });

  it('maps a raw value to its status and text', async () => {
    await i18n.changeLanguage('en');
    const config = {
      value_type: 'enum',
      value_map: { '2': { status: 'down', text: 'bad' } },
      value_map_default: 'warning',
    };
    expect(mapEnumValue(config, 2)).toEqual({ status: 'down', text: 'bad' });
    expect(mapEnumValue(config, 5)).toEqual({ status: 'warning', text: null });
    expect(mapEnumValue({ value_type: 'gauge' }, 2)).toBeNull();
    expect(sensorValueText({ type: 'snmp_custom', config }, 'value', 2, '')).toBe('bad (2)');
    expect(sensorValueText({ type: 'snmp_custom', config }, 'value', 5, '')).toBe(
      'unexpected value (5)',
    );
  });
});

describe('history event markers', () => {
  it('keeps status changes inside the range, incl. unreachable', () => {
    const ev = (time: string, n: string, kind = 'status_change') =>
      ({ kind, time, new_status: n, old_status: 'up' }) as never;
    const m = eventMarkers(
      [
        ev('2026-11-05T10:00:00Z', 'unreachable'),
        ev('2026-11-05T08:00:00Z', 'down'),
        ev('2026-11-04T00:00:00Z', 'down'),
        ev('2026-11-05T09:00:00Z', 'down', 'acknowledge'),
      ],
      '2026-11-05T07:00:00Z',
      '2026-11-05T11:00:00Z',
    );
    expect(m.map((x) => x.status)).toEqual(['down', 'unreachable']);
  });
});
