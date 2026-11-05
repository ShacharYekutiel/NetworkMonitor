import { describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import type { ConfigField, PortMac, WsMessage } from '../api/types';
import { buildUrl } from '../api/client';
import { mkPort } from './portFixtures';
import {
  COLUMNS_KEY,
  connectedView,
  DEFAULT_COLUMNS,
  filterPorts,
  formatSpeed,
  formatVlanList,
  healthText,
  isIpv4,
  loadColumns,
  macCountBadge,
  macQuery,
  normalizeColumns,
  parseSearchInput,
  poeView,
  portLink,
  portSettingsErrors,
  saveColumns,
  TILE_CLASS,
  tileKind,
  tileView,
  toggleColumn,
} from './ports';
import {
  applyLiveMessage,
  InvalidationBatcher,
  isArpSource,
  onPortsUpdated,
  refetchIfInvalidated,
} from './liveCache';
import { groupByVendor, groupSkipped, skipReasonKey, describeWhen } from './templates';
import { buildSensorConfig, visibleConfigFields } from './sensorConfig';
import { formatValue } from './format';

const mac = (m: string, extra: Partial<PortMac> = {}): PortMac => ({
  mac: m,
  vlan: 1,
  vendor: null,
  locally_administered: false,
  ip: null,
  ips: [],
  hostname: null,
  device_id: null,
  device_name: null,
  first_seen: '2026-11-04T08:00:00Z',
  last_seen: '2026-11-05T10:00:00Z',
  ...extra,
});

describe('tile state mapping (one case per legend row)', () => {
  const cases: [
    string,
    Parameters<typeof tileView>[0],
    ReturnType<typeof tileKind>,
    string | null,
  ][] = [
    [
      'admin down → gray, crossed icon',
      {
        state: 'admin_down',
        alert: false,
        connected_kind: 'none',
        trunk: false,
        uplink_reason: null,
        mac_count: 0,
      },
      'disabled',
      'disabled',
    ],
    [
      'admin down never alerts',
      {
        state: 'admin_down',
        alert: true,
        connected_kind: 'none',
        trunk: false,
        uplink_reason: null,
        mac_count: 0,
      },
      'disabled',
      'disabled',
    ],
    [
      'up + alert → orange with icon',
      {
        state: 'up',
        alert: true,
        connected_kind: 'ap',
        trunk: false,
        uplink_reason: null,
        mac_count: 1,
      },
      'alert',
      'ap',
    ],
    [
      'up → green, client',
      {
        state: 'up',
        alert: false,
        connected_kind: 'client',
        trunk: false,
        uplink_reason: null,
        mac_count: 1,
      },
      'up',
      'client',
    ],
    [
      'up → green, switch',
      {
        state: 'up',
        alert: false,
        connected_kind: 'switch',
        trunk: true,
        uplink_reason: 'neighbor',
        mac_count: 2,
      },
      'up',
      'switch',
    ],
    [
      'down + alert → peach, no icon',
      {
        state: 'down',
        alert: true,
        connected_kind: 'none',
        trunk: false,
        uplink_reason: null,
        mac_count: 0,
      },
      'emptyAlert',
      null,
    ],
    [
      'absent + alert → peach',
      {
        state: 'absent',
        alert: true,
        connected_kind: 'none',
        trunk: false,
        uplink_reason: null,
        mac_count: 0,
      },
      'emptyAlert',
      null,
    ],
    [
      'down → white with gray border',
      {
        state: 'down',
        alert: false,
        connected_kind: 'none',
        trunk: true,
        uplink_reason: null,
        mac_count: 0,
      },
      'empty',
      null,
    ],
    [
      'absent → empty',
      {
        state: 'absent',
        alert: false,
        connected_kind: 'none',
        trunk: false,
        uplink_reason: null,
        mac_count: 0,
      },
      'empty',
      null,
    ],
  ];
  it.each(cases)('%s', (_, p, kind, icon) => {
    const v = tileView(p);
    expect(v.kind).toBe(kind);
    expect(tileKind(p)).toBe(kind);
    expect(v.icon).toBe(icon);
    expect(v.className).toBe(TILE_CLASS[kind]);
  });

  it('uses the contract classes', () => {
    expect(TILE_CLASS.disabled).toBe(
      'bg-gray-200 text-gray-500 dark:bg-slate-700 dark:text-slate-400',
    );
    expect(TILE_CLASS.alert).toBe('bg-orange-500 text-white');
    expect(TILE_CLASS.up).toBe('bg-green-500 text-white');
    expect(TILE_CLASS.emptyAlert).toBe(
      'bg-orange-200 border border-orange-300 dark:bg-orange-900/40 dark:border-orange-700',
    );
    expect(TILE_CLASS.empty).toBe(
      'bg-white border border-gray-300 dark:bg-slate-900 dark:border-slate-600',
    );
  });

  it('trunk badge only on connected tiles; MAC count badge for mac_count uplinks', () => {
    const base = {
      state: 'up' as const,
      alert: false,
      connected_kind: 'client' as const,
      mac_count: 7,
    };
    expect(tileView({ ...base, trunk: true, uplink_reason: null }).trunk).toBe(true);
    expect(tileView({ ...base, state: 'down', trunk: true, uplink_reason: null }).trunk).toBe(
      false,
    );
    expect(tileView({ ...base, trunk: false, uplink_reason: 'mac_count' }).badge).toBe('7');
    expect(tileView({ ...base, trunk: false, uplink_reason: 'neighbor' }).badge).toBeNull();
    expect(
      tileView({ ...base, mac_count: 150, trunk: false, uplink_reason: 'mac_count' }).badge,
    ).toBe('99+');
    expect(macCountBadge(99)).toBe('99');
    expect(macCountBadge(100)).toBe('99+');
  });
});

describe('port texts', () => {
  it('speed', () => {
    expect(formatSpeed(100_000_000)).toBe('100M');
    expect(formatSpeed(1_000_000_000)).toBe('1G');
    expect(formatSpeed(2_500_000_000)).toBe('2.5G');
    expect(formatSpeed(10_000_000_000)).toBe('10G');
    expect(formatSpeed(0)).toBeNull();
    expect(formatSpeed(null)).toBeNull();
  });
  it('VLAN lists', () => {
    expect(formatVlanList([20, 10, 11, 12, 1, 30, 31])).toBe('1, 10-12, 20, 30, 31');
    expect(formatVlanList([])).toBe('');
  });
  it('connected column', () => {
    const neighbor = {
      link_id: 7,
      source: 'lldp' as const,
      kind: 'device' as const,
      device_id: 5,
      neighbor_id: null,
      name: 'access-1',
      port: 'Ten-GigabitEthernet1/0/49',
      device_class: 'switch' as const,
      status: 'up' as const,
      mgmt_address: '10.0.0.5',
    };
    expect(connectedView(mkPort(10, '10', { neighbor, is_uplink: true }))).toMatchObject({
      kind: 'neighbor',
      name: 'access-1',
      device_id: 5,
    });
    expect(connectedView(mkPort(9, '9', { is_uplink: true, mac_count: 7 }))).toEqual({
      kind: 'uplinkMacs',
      count: 7,
    });
    const m = mac('00:00:0c:00:00:03', {
      ip: '10.0.0.12',
      hostname: 'phone-12.lan',
      vendor: 'Cisco',
    });
    expect(connectedView(mkPort(2, '2', { first_mac: m, mac_count: 2 }))).toMatchObject({
      kind: 'mac',
      text: 'phone-12.lan',
      more: 1,
    });
    const v = mac('00:50:56:01:00:07', { vendor: 'VMware, Inc.' });
    expect(connectedView(mkPort(7, '7', { first_mac: v, mac_count: 1 }))).toMatchObject({
      text: 'VMware, Inc.',
      more: 0,
    });
    expect(connectedView(mkPort(6, '6'))).toEqual({ kind: 'none' });
  });
  it('PoE column', () => {
    expect(poeView(mkPort(1, '1'))).toBeNull();
    const poe = {
      admin_enabled: true,
      detection: 'delivering' as const,
      power_class: 3,
      power_w: null,
    };
    expect(poeView(mkPort(1, '1', { poe }))).toEqual({ kind: 'on', cls: 3 });
    expect(poeView(mkPort(1, '1', { poe: { ...poe, power_w: 5.4 } }))).toEqual({
      kind: 'watts',
      w: 5.4,
    });
    expect(poeView(mkPort(1, '1', { poe: { ...poe, detection: 'searching' } }))).toBeNull();
  });
  it('health chip formatting', () => {
    expect(healthText({ value: 7, unit: '%', text: '' })).toBe(formatValue(7, '%'));
    expect(healthText({ value: 43, unit: '°C', text: '' })).toBe('43 °C');
    expect(healthText({ value: null, unit: '', text: '0/1' })).toBe('0/1');
    expect(healthText({ value: null, unit: '', text: '' })).toBe('—');
  });
});

describe('filterPorts', () => {
  const ports = [
    mkPort(1, '1', {
      alias: 'Reception PC',
      first_mac: mac('00:50:56:01:00:01', {
        ip: '10.0.0.11',
        ips: ['10.0.0.11'],
        hostname: 'pc-reception.lan',
        vendor: 'VMware, Inc.',
      }),
      macs: [
        mac('00:50:56:01:00:01', {
          ip: '10.0.0.11',
          ips: ['10.0.0.11'],
          hostname: 'pc-reception.lan',
          vendor: 'VMware, Inc.',
        }),
      ],
      mac_count: 1,
    }),
    mkPort(2, '2', {
      macs: [
        mac('3c:52:82:00:00:02', { vendor: 'Hewlett Packard' }),
        mac('00:00:0c:00:00:03', { ip: '10.0.0.12', vendor: 'Cisco Systems, Inc' }),
      ],
      mac_count: 2,
    }),
    mkPort(7, '7', {
      pvid: 10,
      macs: [mac('00:50:56:01:00:07', { ip: '10.0.0.17' })],
      mac_count: 1,
    }),
    mkPort(10, '10', {
      is_uplink: true,
      trunk: true,
      mode: 'trunk',
      neighbor: {
        link_id: 7,
        source: 'lldp',
        kind: 'device',
        device_id: 5,
        neighbor_id: null,
        name: 'access-1',
        port: 'Ten-GigabitEthernet1/0/49',
        device_class: 'switch',
        status: 'up',
        mgmt_address: '10.0.0.5',
      },
    }),
    mkPort(12, 'DEFAULT_VLAN', { kind: 'vlan' }),
  ];
  const ids = (q: string, extra?: Parameters<typeof filterPorts>[2]) =>
    filterPorts(ports, q, extra).map((p) => p.if_index);

  it('empty query returns everything', () => {
    expect(ids('')).toEqual([1, 2, 7, 10, 12]);
    expect(ids('   ')).toEqual([1, 2, 7, 10, 12]);
  });
  it('finds a MAC in three notations (hex digits compared)', () => {
    expect(ids('00:50:56:01:00:07')).toEqual([7]);
    expect(ids('00-50-56-01-00-07')).toEqual([7]);
    expect(ids('0050.5601.0007')).toEqual([7]);
    expect(ids('005056010007')).toEqual([7]);
    expect(ids('00:50:56')).toEqual([1, 7]);
    // "1010" is not a MAC fragment (no MAC contains it as text either); "10" matches as text only
    // (IPs 10.0.0.x, VLAN 10, ifIndex 10)
    expect(ids('1010')).toEqual([]);
    expect(ids('10')).toEqual([1, 2, 7, 10]);
  });
  it('finds IP, hostname, vendor, alias and neighbour, case-insensitively', () => {
    expect(ids('10.0.0.12')).toEqual([2]);
    expect(ids('10.0.0.1')).toEqual([1, 2, 7]);
    expect(ids('PC-RECEPTION')).toEqual([1]);
    expect(ids('cisco')).toEqual([2]);
    expect(ids('reception pc')).toEqual([1]);
    expect(ids('ACCESS-1')).toEqual([10]);
    expect(ids('ten-gigabit')).toEqual([10]);
    expect(ids('default_vlan')).toEqual([12]);
  });
  it('an IP query is not compared as MAC hex digits', () => {
    expect(macQuery('10.0.0.1')).toBeNull();
    expect(macQuery('0050.5601.0007')).toBe('005056010007');
    expect(macQuery('abc')).toBeNull();
    // a VLAN or port number is not a MAC: ≥ 6 hex digits or a separator is required
    expect(macQuery('1010')).toBeNull();
    expect(macQuery('00505')).toBeNull();
    expect(macQuery('005056')).toBe('005056');
    expect(macQuery('00:50')).toBe('0050');
    expect(macQuery('0050.56')).toBe('005056');
    expect(macQuery('hello')).toBeNull();
  });
  it('searches translated column texts supplied by the UI', () => {
    expect(ids('Kein Link', (p) => (p.if_index === 7 ? ['Kein Link'] : []))).toEqual([7]);
    expect(ids('trunk')).toEqual([10]);
  });
});

describe('column chooser persistence', () => {
  const store = () => {
    const m = new Map<string, string>();
    return {
      m,
      getItem: (k: string) => m.get(k) ?? null,
      setItem: (k: string, v: string) => void m.set(k, v),
    };
  };
  it('defaults when nothing is stored', () => {
    expect(loadColumns(store())).toEqual(DEFAULT_COLUMNS);
  });
  it('saves and loads, in display order, dropping unknown columns', () => {
    const s = store();
    saveColumns(['mac', 'status', 'ip'], s);
    expect(JSON.parse(s.m.get(COLUMNS_KEY) as string)).toEqual(['status', 'mac', 'ip']);
    expect(loadColumns(s)).toEqual(['status', 'mac', 'ip']);
    s.m.set(COLUMNS_KEY, JSON.stringify(['bogus', 'vlan']));
    expect(loadColumns(s)).toEqual(['vlan']);
  });
  it('survives broken JSON and throwing storage', () => {
    const s = store();
    s.m.set(COLUMNS_KEY, '{not json');
    expect(loadColumns(s)).toEqual(DEFAULT_COLUMNS);
    s.m.set(COLUMNS_KEY, '"x"');
    expect(loadColumns(s)).toEqual(DEFAULT_COLUMNS);
    const bad = {
      getItem: () => {
        throw new Error('SecurityError');
      },
      setItem: () => {
        throw new Error('QuotaExceeded');
      },
    };
    expect(loadColumns(bad)).toEqual(DEFAULT_COLUMNS);
    expect(() => saveColumns(['status'], bad)).not.toThrow();
    expect(loadColumns(null)).toEqual(DEFAULT_COLUMNS);
  });
  it('uses window.localStorage by default', () => {
    localStorage.removeItem(COLUMNS_KEY);
    saveColumns(['speed', 'kind']);
    expect(localStorage.getItem(COLUMNS_KEY)).toBe('["speed","kind"]');
    expect(loadColumns()).toEqual(['speed', 'kind']);
    localStorage.removeItem(COLUMNS_KEY);
  });
  it('toggles columns', () => {
    expect(toggleColumn(['status', 'vlan'], 'alias', true)).toEqual(['status', 'vlan', 'alias']);
    expect(toggleColumn(['status', 'vlan'], 'status', false)).toEqual(['vlan']);
    expect(normalizeColumns('x')).toBeNull();
  });
});

describe('Find page input', () => {
  it('IPv4 → ip', () => {
    expect(parseSearchInput(' 10.0.0.12 ')).toEqual({ ip: '10.0.0.12' });
    expect(isIpv4('256.1.1.1')).toBe(false);
  });
  it('MACs in any notation and prefixes → q', () => {
    expect(parseSearchInput('00:50:56:01:00:07')).toEqual({ q: '00:50:56:01:00:07' });
    expect(parseSearchInput('0050.5601.0007')).toEqual({ q: '0050.5601.0007' });
    expect(parseSearchInput('00-50-56-01-00-07')).toEqual({ q: '00-50-56-01-00-07' });
    expect(parseSearchInput('005056')).toEqual({ q: '005056' });
  });
  it('invalid input → null (shown as validation_error)', () => {
    expect(parseSearchInput('zz')).toBeNull();
    expect(parseSearchInput('00505')).toBeNull();
    expect(parseSearchInput('0050560100071')).toBeNull();
    expect(parseSearchInput('')).toBeNull();
    expect(parseSearchInput('pc-reception')).toBeNull();
  });
  it('builds the API request and the port link', () => {
    expect(buildUrl('/search/mac', { q: '005056' })).toBe('/api/search/mac?q=005056');
    expect(buildUrl('/search/mac', { ip: '10.0.0.12' })).toBe('/api/search/mac?ip=10.0.0.12');
    expect(buildUrl('/devices/3/ports', { kind: ['physical', 'lag'] })).toBe(
      '/api/devices/3/ports?kind=physical&kind=lag',
    );
    expect(portLink({ device_id: 3, if_index: 7 })).toBe('/devices/3?tab=ports&port=7');
  });
});

describe('ports settings validation', () => {
  it('limits per data-model', () => {
    const ok = { interval: '10', threshold: '5', retention: '30' };
    expect(portSettingsErrors(ok)).toEqual({ interval: false, threshold: false, retention: false });
    expect(portSettingsErrors({ ...ok, interval: '0' }).interval).toBe(false);
    expect(portSettingsErrors({ ...ok, interval: '4' }).interval).toBe(true);
    expect(portSettingsErrors({ ...ok, interval: '1441' }).interval).toBe(true);
    expect(portSettingsErrors({ ...ok, threshold: '0' }).threshold).toBe(true);
    expect(portSettingsErrors({ ...ok, threshold: '1000' }).threshold).toBe(false);
    expect(portSettingsErrors({ ...ok, retention: '366' }).retention).toBe(true);
    expect(portSettingsErrors({ ...ok, retention: 'x' }).retention).toBe(true);
  });
});

describe('WS: ports.updated and templates.reloaded', () => {
  it('invalidates ports queries and notifies listeners', () => {
    const qc = new QueryClient();
    const batch = new InvalidationBatcher(qc);
    const add = vi.spyOn(batch, 'add');
    const seen: number[] = [];
    const off = onPortsUpdated((d) => seen.push(d.device_id));
    const msg: WsMessage = {
      type: 'ports.updated',
      ts: '2026-11-05T10:00:00Z',
      data: { device_id: 3, ok: true, collected_at: '2026-11-05T10:00:00Z' },
    };
    applyLiveMessage(qc, msg, batch);
    // Only that device's view and modals; never every ports view or MAC searches.
    expect(add.mock.calls.map((c) => c[0])).toEqual([
      ['device-ports', 3],
      ['port-detail', 3],
    ]);
    expect(seen).toEqual([3]);
    off();
    applyLiveMessage(qc, msg, batch);
    expect(seen).toEqual([3]);
    batch.dispose();
  });
  const updated = (device_id: number): WsMessage => ({
    type: 'ports.updated',
    ts: 'x',
    data: { device_id, ok: true, collected_at: 'x' },
  });
  const seed = (qc: QueryClient) => {
    qc.setQueryData(['device-ports', 1], { device_id: 1 });
    qc.setQueryData(['device-ports', 2], { device_id: 2 });
    qc.setQueryData(['port-detail', 1, 7, false], { if_index: 7 });
    qc.setQueryData(['mac-search', { q: '005056' }], { items: [] });
  };
  const invalidated = (qc: QueryClient, key: unknown[]) =>
    qc.getQueryCache().find({ queryKey: key, exact: true })?.state.isInvalidated;

  it('only the updated device is invalidated (an open view refetches, a closed one goes stale)', () => {
    const qc = new QueryClient();
    const batch = new InvalidationBatcher(qc, 0);
    seed(qc);
    applyLiveMessage(qc, updated(2), batch);
    batch.flush();
    expect(invalidated(qc, ['device-ports', 2])).toBe(true);
    expect(invalidated(qc, ['device-ports', 1])).toBe(false);
    expect(invalidated(qc, ['port-detail', 1, 7, false])).toBe(false);
    expect(invalidated(qc, ['mac-search', { q: '005056' }])).toBe(false);
    batch.dispose();
  });

  it('an ARP source marks every ports view stale without refetching', () => {
    const qc = new QueryClient();
    const batch = new InvalidationBatcher(qc, 0);
    seed(qc);
    const refetch = vi.spyOn(qc, 'refetchQueries');
    qc.setQueryData(['device', 9], { id: 9, arp_effective: true });
    applyLiveMessage(qc, updated(9), batch);
    expect(invalidated(qc, ['device-ports', 1])).toBe(true);
    expect(invalidated(qc, ['device-ports', 2])).toBe(true);
    expect(invalidated(qc, ['port-detail', 1, 7, false])).toBe(true);
    expect(invalidated(qc, ['mac-search', { q: '005056' }])).toBe(false);
    // lazy: refetchType 'none' never triggers a refetch
    expect(refetch).not.toHaveBeenCalled();
    batch.dispose();
  });

  it('knows ARP sources from any cached copy of the device', () => {
    const qc = new QueryClient();
    expect(isArpSource(qc, 4)).toBe(false);
    qc.setQueryData(['devices', {}], { items: [{ id: 4, arp_effective: true }], total: 1 });
    expect(isArpSource(qc, 4)).toBe(true);
    qc.setQueryData(['device-ports', 5], { device_id: 5, arp_effective: true });
    expect(isArpSource(qc, 5)).toBe(true);
    qc.setQueryData(['device', 6], { id: 6, arp_effective: false });
    expect(isArpSource(qc, 6)).toBe(false);
  });

  it('ports queries refetch on focus only after being marked stale', () => {
    expect(refetchIfInvalidated({ state: { isInvalidated: true } })).toBe(true);
    expect(refetchIfInvalidated({ state: { isInvalidated: false } })).toBe(false);
  });

  it('templates.reloaded invalidates the templates queries', () => {
    const qc = new QueryClient();
    const batch = new InvalidationBatcher(qc);
    const add = vi.spyOn(batch, 'add');
    applyLiveMessage(
      qc,
      { type: 'templates.reloaded', ts: 'x', data: { loaded_at: 'x', count: 19, error_count: 0 } },
      batch,
    );
    expect(add.mock.calls.map((c) => c[0])).toEqual([['deviceTemplates'], ['deviceTemplate']]);
    batch.dispose();
  });
});

describe('templates 2b-2', () => {
  it('not_applicable has its own text', () => {
    expect(skipReasonKey('not_applicable')).toBe('templates.skip.not_applicable');
    expect(skipReasonKey('whatever')).toBe('templates.skip.other');
    const g = groupSkipped([
      { name: 'a', type: 'snmp_custom', reason: 'exists', message: '' },
      {
        name: 'b',
        type: 'snmp_custom',
        reason: 'not_applicable',
        message: 'Condition not met: 1.2',
      },
    ]);
    expect(g.map((x) => x.reason)).toEqual(['not_applicable', 'exists']);
  });
  it('groups by vendor, generic last', () => {
    const tpl = (id: string, vendor: string | null) => ({
      id,
      vendor,
      name: { en: id, he: id },
      description: { en: '', he: '' },
      requires_snmp: true,
      priority: 50,
      match: [],
      entries: [],
    });
    const g = groupByVendor([
      tpl('a', 'hpe'),
      tpl('b', null),
      tpl('c', 'checkpoint'),
      tpl('d', 'generic'),
      tpl('e', 'hpe'),
    ]);
    expect(g.map((x) => [x.vendor, x.items.map((i) => i.id)])).toEqual([
      ['checkpoint', ['c']],
      ['hpe', ['a', 'e']],
      [null, ['b', 'd']],
    ]);
  });
  it('describes when conditions', () => {
    const t = ((k: string, o?: { list?: string }) => `${k}(${o?.list ?? ''})`) as never;
    expect(describeWhen([{ oid: '1.3.6.1.4.1.2620.1.5.2.0', in: [1], not_in: null }], t)).toEqual([
      '1.3.6.1.4.1.2620.1.5.2.0 templates.whenIn(1)',
    ]);
    expect(describeWhen(undefined, t)).toEqual([]);
  });
});

describe('sensor forms 2b-2', () => {
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
  const custom = [
    f('oid', 'str', { required: true }),
    f('value_type', 'enum', { default: 'gauge', options: ['gauge', 'counter', 'string', 'enum'] }),
    f('percent_of_oid', 'str_nullable'),
    f('percent_base', 'enum', { default: 'total', options: ['total', 'sum'] }),
  ];
  it('percent_base is shown and sent for gauges only', () => {
    const keys = (vt: string) =>
      visibleConfigFields('snmp_custom', custom, { value_type: vt }).map((x) => x.key);
    expect(keys('gauge')).toContain('percent_base');
    expect(keys('counter')).not.toContain('percent_base');
    const r = buildSensorConfig('snmp_custom', custom, {
      oid: '1.3.6',
      value_type: 'counter',
      percent_of_oid: '',
      percent_base: 'sum',
    });
    expect('percent_base' in r.config).toBe(false);
    expect(r.errors).toEqual({});
    const g = buildSensorConfig('snmp_custom', custom, {
      oid: '1.3.6',
      value_type: 'gauge',
      percent_of_oid: '',
      percent_base: 'total',
    });
    expect(g.config.percent_base).toBe('total');
  });
  it('"sum" without percent_of_oid is rejected client-side', () => {
    const r = buildSensorConfig('snmp_custom', custom, {
      oid: '1.3.6',
      value_type: 'gauge',
      percent_of_oid: '',
      percent_base: 'sum',
    });
    expect(r.errors.percent_base).toBe('validation.percentBaseSum');
    const ok = buildSensorConfig('snmp_custom', custom, {
      oid: '1.3.6',
      value_type: 'gauge',
      percent_of_oid: '1.3.7',
      percent_base: 'sum',
    });
    expect(ok.errors).toEqual({});
    expect(ok.config.percent_base).toBe('sum');
  });
  it('snmp_walk_count config', () => {
    const fields = [
      f('oid', 'str', { required: true }),
      f('count_values', 'int_list'),
      f('label', 'str', { default: '' }),
      f('max_rows', 'int', { default: 5000, min: 1, max: 10000 }),
    ];
    const r = buildSensorConfig('snmp_walk_count', fields, {
      oid: ' .1.3.6.1.4.1.14823.2.3.3.1.2.1.1.11 ',
      count_values: '1, 3',
      label: 'APs up',
      max_rows: '5000',
    });
    expect(r.errors).toEqual({});
    expect(r.config).toEqual({
      oid: '1.3.6.1.4.1.14823.2.3.3.1.2.1.1.11',
      count_values: [1, 3],
      label: 'APs up',
      max_rows: 5000,
    });
    expect(
      buildSensorConfig('snmp_walk_count', fields, {
        oid: '1.3',
        count_values: '',
        label: '',
        max_rows: '5000',
      }).config.count_values,
    ).toBeNull();
    expect(
      buildSensorConfig('snmp_walk_count', fields, {
        oid: '1.3',
        count_values: '1,x',
        label: '',
        max_rows: '0',
      }).errors,
    ).toEqual({
      count_values: 'validation.intList',
      max_rows: 'validation.range',
    });
  });
  it('row counts display without a unit', () => {
    expect(formatValue(12, '#')).toBe('12');
  });
});
