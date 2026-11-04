import { describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import type { ConfigField, Sensor, Tree } from '../api/types';
import { backoffDelay } from '../api/ws';
import { toSeries } from './history';
import { applyLiveMessage, InvalidationBatcher } from './liveCache';
import { configFromRaw, rawFromConfig } from './sensorConfig';
import { snmpFormFrom, snmpFormToInput } from './snmp';
import { groupPath } from './tree';
import { diffThresholds } from '../components/ThresholdEditor';
import { formatValue } from './format';

const counts = { up: 1, warning: 0, down: 0, paused: 0, unknown: 0, total: 1 };
const tree: Tree = {
  groups: [
    {
      id: 1,
      name: 'Default',
      parent_id: null,
      status: 'up',
      status_counts: counts,
      children: [
        {
          id: 2,
          name: 'TLV',
          parent_id: 1,
          status: 'up',
          status_counts: counts,
          children: [],
          devices: [],
        },
      ],
      devices: [
        {
          id: 3,
          name: 'sw',
          host: '10.0.0.2',
          enabled: true,
          status: 'up',
          status_counts: counts,
          sensors: [
            {
              id: 7,
              name: 'Ping',
              type: 'ping',
              enabled: true,
              status: 'up',
              status_message: '',
              primary_channel: 'latency_ms',
              primary_value: 1,
              primary_unit: 'ms',
              last_check_at: null,
            },
          ],
        },
      ],
    },
  ],
};

describe('ws backoff', () => {
  it('follows 1s, 2s, 5s, then 10s', () => {
    expect([0, 1, 2, 3, 4, 10].map(backoffDelay)).toEqual([1000, 2000, 5000, 10000, 10000, 10000]);
  });
});

describe('live cache', () => {
  it('patches the tree and sensor on sensor.update', () => {
    const qc = new QueryClient();
    qc.setQueryData(['tree'], tree);
    qc.setQueryData(['sensor', 7], {
      id: 7,
      status: 'up',
      status_message: '',
      last_check_at: null,
      last_status_change_at: null,
      channels: [{ key: 'latency_ms', unit: 'ms', last_value: 1, status: 'up' }],
    } as unknown as Sensor);
    const batch = new InvalidationBatcher(qc, 10_000);
    applyLiveMessage(
      qc,
      {
        type: 'sensor.update',
        ts: '2026-11-04T12:00:00Z',
        data: {
          id: 7,
          device_id: 3,
          status: 'down',
          status_message: 'Host unreachable',
          last_check_at: '2026-11-04T12:00:00Z',
          status_changed: true,
          channels: [{ key: 'latency_ms', value: 600, status: 'down' }],
        },
      },
      batch,
    );
    const t = qc.getQueryData<Tree>(['tree'])!;
    const s = t.groups[0].devices[0].sensors[0];
    expect(s.status).toBe('down');
    expect(s.primary_value).toBe(600);
    const sensor = qc.getQueryData<Sensor>(['sensor', 7])!;
    expect(sensor.channels[0].last_value).toBe(600);
    expect(sensor.last_status_change_at).toBe('2026-11-04T12:00:00Z');
    batch.dispose();
  });

  it('invalidates on config.changed', () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, 'invalidateQueries');
    const batch = new InvalidationBatcher(qc, 10_000);
    applyLiveMessage(
      qc,
      { type: 'config.changed', ts: '', data: { entity: 'device', id: 3, action: 'updated' } },
      batch,
    );
    batch.flush();
    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(keys).toContain('["tree"]');
    expect(keys).toContain('["device",3]');
  });
});

describe('tree helpers', () => {
  it('finds the group path', () => {
    expect(groupPath(tree.groups, 2).map((g) => g.name)).toEqual(['Default', 'TLV']);
  });
});

describe('history series', () => {
  it('inserts gaps when t jumps more than 2 buckets', () => {
    const p = (t: string, v: number) => ({ t, avg: v, min: v - 1, max: v + 1 });
    const { avg, band } = toSeries(
      [p('2026-11-04T12:00:00Z', 5), p('2026-11-04T12:01:00Z', 6), p('2026-11-04T12:10:00Z', 7)],
      60,
    );
    expect(avg.map((x) => x[1])).toEqual([5, 6, null, 7]);
    expect(band[0][1]).toBe(2);
  });
});

describe('sensor config', () => {
  const fields: ConfigField[] = [
    { key: 'url', type: 'str', required: true, default: null, min: null, max: null, options: null },
    { key: 'timeout_s', type: 'int', required: false, default: 10, min: 1, max: 60, options: null },
    {
      key: 'expected_status',
      type: 'int_list',
      required: false,
      default: null,
      min: null,
      max: null,
      options: null,
    },
    {
      key: 'keyword',
      type: 'str_nullable',
      required: false,
      default: null,
      min: null,
      max: null,
      options: null,
    },
    {
      key: 'verify_tls',
      type: 'bool',
      required: false,
      default: true,
      min: null,
      max: null,
      options: null,
    },
    {
      key: 'headers',
      type: 'headers',
      required: false,
      default: {},
      min: null,
      max: null,
      options: null,
    },
  ];
  it('round-trips defaults and validates', () => {
    const raw = rawFromConfig(fields);
    expect(raw.timeout_s).toBe('10');
    expect(configFromRaw(fields, raw).errors).toEqual({ url: 'validation.required' });
    const ok = configFromRaw(fields, {
      ...raw,
      url: 'https://{host}/',
      expected_status: '200, 301',
      headers: 'X-Token: abc',
      timeout_s: '5',
    });
    expect(ok.errors).toEqual({});
    expect(ok.config).toEqual({
      url: 'https://{host}/',
      timeout_s: 5,
      expected_status: [200, 301],
      keyword: null,
      verify_tls: true,
      headers: { 'X-Token': 'abc' },
    });
    expect(configFromRaw(fields, { ...raw, url: 'x', timeout_s: '99' }).errors).toEqual({
      timeout_s: 'validation.range',
    });
  });
});

describe('snmp form', () => {
  it('maps inherit to null and keeps secrets when empty', () => {
    expect(snmpFormToInput(snmpFormFrom(null))).toBeNull();
    const f = { ...snmpFormFrom(null), inherit: false, version: 'v3' as const, v3_username: 'u' };
    const out = snmpFormToInput(f)!;
    expect(out.community).toBeNull();
    expect('v3_auth_key' in out).toBe(false);
    expect(snmpFormToInput({ ...f, v3_auth_key_clear: true })!.v3_auth_key).toBe('');
    expect(snmpFormToInput({ ...f, v3_priv_key: 'secret123' })!.v3_priv_key).toBe('secret123');
  });
});

describe('thresholds diff', () => {
  it('sends only changed keys, null for cleared limits', () => {
    const ch = [
      {
        key: 'latency_ms',
        unit: 'ms',
        warning_min: null,
        warning_max: 100,
        down_min: null,
        down_max: 500,
        last_value: 1,
        status: 'up' as const,
      },
    ];
    expect(
      diffThresholds(ch, {
        latency_ms: { warning_min: '', warning_max: '100', down_min: '', down_max: '500' },
      }),
    ).toEqual([]);
    expect(
      diffThresholds(ch, {
        latency_ms: { warning_min: '', warning_max: '', down_min: '', down_max: '800' },
      }),
    ).toEqual([{ key: 'latency_ms', warning_max: null, down_max: 800 }]);
    expect(
      diffThresholds(ch, {
        latency_ms: { warning_min: 'abc', warning_max: '', down_min: '', down_max: '' },
      }),
    ).toBe('invalid');
  });
});

describe('formatValue', () => {
  it('scales bit/s and bytes', () => {
    expect(formatValue(1_500_000, 'bit/s')).toBe('1.5 Mbit/s');
    expect(formatValue(2048, 'B')).toBe('2 KiB');
    expect(formatValue(null, 'ms')).toBe('—');
  });
});
