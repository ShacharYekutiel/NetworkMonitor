// [if-noise] Pure helpers: debounce hold, form checks, criteria, apply-template port filters,
// preview counts and the device SNMP source line.
import { describe, expect, it } from 'vitest';
import type { ApplyInterface } from '../api/types';
import { patternError } from '../components/settings/NoiseSettings';
import {
  EMPTY_FILTER,
  filterActive,
  filterErrors,
  filteredPick,
  hasPortData,
  holdSeconds,
  isHiddenEvent,
  legacyCriteria,
  noiseFormErrors,
  normalizeCriteria,
  parseVlans,
  portPasses,
  portVlans,
  previewCounts,
  snmpSourceInfo,
} from './noise';
import { coveredPorts } from './templates';

function port(p: Partial<ApplyInterface> & { if_index: number }): ApplyInterface {
  return {
    name: `Gi1/0/${p.if_index}`,
    alias: '',
    oper_status: 'up',
    speed_bps: 1e9,
    kind: 'physical',
    important: false,
    reasons: [],
    has_sensor: false,
    sensor_id: null,
    mode: 'access',
    pvid: 10,
    untagged_vlans: [10],
    tagged_vlans: [],
    has_neighbor: false,
    ...p,
  };
}

const PORTS: ApplyInterface[] = [
  port({ if_index: 1, alias: 'PC room 12' }),
  port({ if_index: 2, alias: 'printer', pvid: 20, untagged_vlans: [20] }),
  port({
    if_index: 3,
    alias: 'uplink core',
    mode: 'trunk',
    pvid: 1,
    untagged_vlans: [1],
    tagged_vlans: [10, 20, 30],
    has_neighbor: true,
    important: true,
    reasons: ['uplink', 'trunk'],
  }),
  port({
    if_index: 4,
    alias: 'AP lobby',
    mode: 'trunk',
    tagged_vlans: [10, 99],
    has_neighbor: true,
  }),
];

describe('[if-noise] debounce settings', () => {
  it('hold = (polls + ½) intervals, at least the minimum', () => {
    expect(holdSeconds(60, 1, 0)).toBe(90);
    expect(holdSeconds(60, 2, 0)).toBe(150);
    expect(holdSeconds(30, 1, 300)).toBe(300);
    expect(holdSeconds(60, 0, 0)).toBe(90); // polls < 1 counts as 1
  });

  it('form ranges', () => {
    const ok = {
      debounce: true,
      polls: '1',
      minS: '0',
      flap: true,
      threshold: '5',
      windowMin: '10',
      quietMin: '15',
    };
    expect(Object.values(noiseFormErrors(ok)).some(Boolean)).toBe(false);
    expect(noiseFormErrors({ ...ok, polls: '11' }).polls).toBe(true);
    expect(noiseFormErrors({ ...ok, polls: '0' }).polls).toBe(true);
    expect(noiseFormErrors({ ...ok, minS: '3601' }).minS).toBe(true);
    expect(noiseFormErrors({ ...ok, threshold: '2' }).threshold).toBe(true);
    expect(noiseFormErrors({ ...ok, quietMin: '1441' }).quiet).toBe(true);
  });

  it('hidden rows', () => {
    expect(isHiddenEvent({ noise: null })).toBe(false);
    expect(isHiddenEvent({})).toBe(false);
    expect(isHiddenEvent({ noise: 'debounced' })).toBe(true);
  });
});

describe('[if-noise] important-port criteria', () => {
  it('legacy meaning and canonical order', () => {
    expect(legacyCriteria(['uplink', 'critical_sensor'])).toBe(true);
    expect(legacyCriteria(['uplink'])).toBe(false);
    expect(legacyCriteria(undefined)).toBe(false);
    expect(normalizeCriteria(['critical_sensor', 'uplink', 'uplink', 'irf'])).toEqual([
      'uplink',
      'irf',
      'critical_sensor',
    ]);
  });

  it('description pattern check mirrors the server (RE2: no backreferences / lookaround)', () => {
    expect(patternError('')).toBe(false);
    expect(patternError('uplink|core')).toBe(false);
    expect(patternError('(a)\\1')).toBe(true);
    expect(patternError('foo(?=bar)')).toBe(true);
    expect(patternError('([')).toBe(true);
    expect(patternError('x'.repeat(201))).toBe(true);
  });
});

describe('[if-noise] apply-template port filter', () => {
  it('parses VLAN lists and ranges', () => {
    expect(parseVlans('')).toEqual([]);
    expect(parseVlans('10, 20 30;40')).toEqual([10, 20, 30, 40]);
    expect(parseVlans('100-102,101')).toEqual([100, 101, 102]);
    expect(parseVlans('0')).toBeNull();
    expect(parseVlans('4095')).toBeNull();
    expect(parseVlans('20-10')).toBeNull();
    expect(parseVlans('ten')).toBeNull();
  });

  it('port VLANs = untagged + tagged + PVID', () => {
    expect(portVlans(PORTS[2])).toEqual([1, 10, 20, 30]);
    expect(portVlans(port({ if_index: 9, pvid: null, untagged_vlans: null }))).toEqual([]);
  });

  it('every condition', () => {
    const f = (patch: Partial<typeof EMPTY_FILTER>) =>
      filteredPick(PORTS, { ...EMPTY_FILTER, ...patch });
    expect(f({})).toEqual([1, 2, 3, 4]); // no filter: all ports
    expect(f({ includeVlans: '20' })).toEqual([2, 3]);
    expect(f({ excludeVlans: '10' })).toEqual([2]);
    expect(f({ mode: 'access' })).toEqual([1, 2]);
    expect(f({ mode: 'trunk' })).toEqual([3, 4]);
    expect(f({ neighbor: 'with' })).toEqual([3, 4]);
    expect(f({ neighbor: 'without' })).toEqual([1, 2]);
    expect(f({ descrInclude: 'UPLINK|ap ' })).toEqual([3, 4]);
    expect(f({ descrExclude: 'pc|printer' })).toEqual([3, 4]);
    // "leave out access ports in the user VLAN": not access, or not VLAN 10
    expect(f({ mode: 'trunk', excludeVlans: '99' })).toEqual([3]);
  });

  it('a port without data fails positive conditions, passes exclusions', () => {
    const bare = port({
      if_index: 7,
      mode: null,
      pvid: null,
      untagged_vlans: null,
      tagged_vlans: null,
      has_neighbor: null,
    });
    expect(portPasses(bare, { ...EMPTY_FILTER, includeVlans: '10' })).toBe(false);
    expect(portPasses(bare, { ...EMPTY_FILTER, excludeVlans: '10' })).toBe(true);
    expect(portPasses(bare, { ...EMPTY_FILTER, mode: 'access' })).toBe(false);
    expect(portPasses(bare, { ...EMPTY_FILTER, neighbor: 'without' })).toBe(false);
    expect(hasPortData([bare])).toBe(false);
    expect(hasPortData(PORTS)).toBe(true);
  });

  it('errors and activity', () => {
    expect(filterActive(EMPTY_FILTER)).toBe(false);
    expect(filterActive({ ...EMPTY_FILTER, mode: 'trunk' })).toBe(true);
    const e = filterErrors({ ...EMPTY_FILTER, includeVlans: 'x', descrExclude: '(' });
    expect(e).toEqual({
      includeVlans: true,
      excludeVlans: false,
      descrInclude: false,
      descrExclude: true,
    });
  });

  it('preview counts for every choice', () => {
    const picked = new Set([1, 3]);
    expect(previewCounts(PORTS, coveredPorts(PORTS, 'all', picked))).toEqual({ will: 4, wont: 0 });
    expect(previewCounts(PORTS, coveredPorts(PORTS, 'important', picked))).toEqual({
      will: 1,
      wont: 3,
    });
    expect(previewCounts(PORTS, coveredPorts(PORTS, 'choose', picked))).toEqual({
      will: 2,
      wont: 2,
    });
    expect(previewCounts(PORTS, coveredPorts(PORTS, 'none', picked))).toEqual({
      will: 0,
      wont: 4,
    });
  });
});

describe('[if-noise] device SNMP source', () => {
  const types = [
    { type: 'ping' as const, requires_snmp: false },
    { type: 'snmp_interface' as const, requires_snmp: true },
  ];
  it('names the group and warns about the built-in default only with SNMP sensors', () => {
    expect(
      snmpSourceInfo(
        { effective_snmp_source: 'group', effective_snmp_group_id: 4 },
        [{ type: 'snmp_interface' }],
        types,
      ),
    ).toEqual({ source: 'group', groupId: 4, warn: false });
    expect(
      snmpSourceInfo(
        { effective_snmp_source: 'default', effective_snmp_group_id: null },
        [{ type: 'snmp_interface' }],
        types,
      ).warn,
    ).toBe(true);
    expect(
      snmpSourceInfo(
        { effective_snmp_source: 'default', effective_snmp_group_id: null },
        [{ type: 'ping' }],
        types,
      ).warn,
    ).toBe(false);
    expect(
      snmpSourceInfo(
        { effective_snmp_source: 'default', effective_snmp_group_id: null },
        [],
        undefined,
      ).warn,
    ).toBe(false);
  });
});
