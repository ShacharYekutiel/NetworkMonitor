import { describe, expect, it } from 'vitest';
import type {
  DependencySuggestion,
  LinkEnd,
  TopologyGraph,
  TopologyLink,
  TopologyNode,
} from '../api/types';
import { STATUS_HEX } from './status';
import {
  buildGraph,
  defaultSelection,
  endState,
  linkColor,
  linkState,
  NEUTRAL_EDGE,
  suggestionRows,
  suggestionsToBulk,
  trafficLabel,
} from './topology';

const dev = (
  id: number,
  p: Partial<Extract<TopologyNode, { kind: 'device' }>> = {},
): TopologyNode => ({
  key: `d:${id}`,
  kind: 'device',
  device_id: id,
  name: `dev-${id}`,
  host: `10.0.0.${id}`,
  group_id: 1,
  status: 'up',
  in_maintenance: false,
  device_class: 'switch',
  x: null,
  y: null,
  in_filter: true,
  ...p,
});
const neighbor: TopologyNode = {
  key: 'n:3',
  kind: 'neighbor',
  neighbor_id: 3,
  name: 'printer-3f',
  chassis_id: '00:11:22:33:44:55',
  mgmt_address: '127.0.0.99',
  sys_descr: null,
  x: null,
  y: null,
  in_filter: true,
};
const end = (node: string, p: Partial<LinkEnd> = {}): LinkEnd => ({
  node,
  device_id: node.startsWith('d:') ? Number(node.slice(2)) : null,
  neighbor_id: node.startsWith('n:') ? Number(node.slice(2)) : null,
  if_index: null,
  port: '',
  sensor_id: null,
  sensor_status: null,
  oper_status: null,
  in_bps: null,
  out_bps: null,
  in_util_pct: null,
  out_util_pct: null,
  ...p,
});
const link = (id: number, a: LinkEnd, b: LinkEnd, p: Partial<TopologyLink> = {}): TopologyLink => ({
  id,
  source: 'lldp',
  a,
  b,
  stale: false,
  missed_refreshes: 0,
  first_seen: '2026-11-05T08:00:00Z',
  last_seen: '2026-11-05T09:00:00Z',
  ...p,
});

// Core (down) with two unreachable access switches; one stale link to an unmanaged neighbour.
const graph: TopologyGraph = {
  nodes: [
    dev(1, { status: 'down', name: 'core', x: 0, y: 0 }),
    dev(2, { status: 'unreachable', in_maintenance: true }),
    dev(5, { status: 'unreachable', in_filter: false, device_class: null }),
    neighbor,
  ],
  links: [
    link(
      10,
      end('d:1', {
        port: '1',
        sensor_id: 70,
        sensor_status: 'up',
        oper_status: 1,
        in_bps: 12_300_000,
        out_bps: 1_200_000,
      }),
      end('d:2', { port: 'GigabitEthernet1/0/1' }),
    ),
    link(
      11,
      end('d:1', { port: '2', sensor_id: 71, sensor_status: 'down', oper_status: 2 }),
      end('d:5', { port: '24', sensor_id: 90, sensor_status: 'up', oper_status: 1 }),
    ),
    link(12, end('d:5', { port: '3' }), end('n:3', { port: 'eth0' }), {
      stale: true,
      missed_refreshes: 3,
    }),
    link(13, end('d:1'), end('d:99')), // dangling (node not in the graph)
  ],
  last_refresh: null,
  refresh_running: false,
};

describe('topology graph builder', () => {
  it('builds nodes from the API (status, maintenance, ghost neighbours, filter)', () => {
    const b = buildGraph(graph, { dark: false, showTraffic: false });
    expect(b.nodes.map((n) => n.id)).toEqual(['d:1', 'd:2', 'd:5', 'n:3']);
    const [core, child, outside, ghost] = b.nodes;
    expect(core).toMatchObject({
      kind: 'device',
      refId: 1,
      status: 'down',
      x: 0,
      y: 0,
      fixed: true,
      ghost: false,
    });
    expect(child).toMatchObject({ status: 'unreachable', maintenance: true });
    expect(child.x).toBeUndefined();
    expect(outside.itemStyle.opacity).toBeLessThan(1);
    expect(ghost).toMatchObject({ kind: 'neighbor', refId: 3, ghost: true, status: null });
    // Symbols are data-URL SVGs coloured by status; the wrench badge only for maintenance.
    const svg = (s: string) => decodeURIComponent(s.split(',').slice(1).join(','));
    expect(core.symbol.startsWith('image://data:image/svg+xml')).toBe(true);
    expect(svg(core.symbol)).toContain(STATUS_HEX.down);
    expect(svg(child.symbol)).toContain(STATUS_HEX.unreachable);
    expect(svg(child.symbol)).toContain('#0ea5e9');
    expect(svg(core.symbol)).not.toContain('#0ea5e9');
    expect(svg(ghost.symbol)).toContain('stroke-dasharray');
    expect(b.needsLayout).toBe(true);
    // degree drives nothing but is exposed as value
    expect(core.value).toBe(2);
  });

  it('builds edges with colours from the interface sensors, stale dashed, dangling dropped', () => {
    const b = buildGraph(graph, { dark: false, showTraffic: false });
    expect(b.links.map((l) => l.linkId)).toEqual([10, 11, 12]);
    const [l10, l11, l12] = b.links;
    expect(l10).toMatchObject({ source: 'd:1', target: 'd:2', state: 'up', stale: false });
    expect(l10.lineStyle).toMatchObject({ color: STATUS_HEX.up, type: 'solid' });
    // worst of the two ends: oper_status 2 on one side → down
    expect(l11.state).toBe('down');
    expect(l11.lineStyle.color).toBe(STATUS_HEX.down);
    // no interface sensor → neutral; stale → dashed + 40 % opacity
    expect(l12.state).toBeNull();
    expect(l12.lineStyle).toEqual({
      color: NEUTRAL_EDGE.light,
      width: 2.5,
      type: 'dashed',
      opacity: 0.4,
    });
    expect(buildGraph(graph, { dark: true, showTraffic: false }).links[2].lineStyle.color).toBe(
      NEUTRAL_EDGE.dark,
    );
  });

  it('shows the bps label only with "Show traffic"', () => {
    expect(buildGraph(graph, { dark: false, showTraffic: false }).links[0].label.show).toBe(false);
    const on = buildGraph(graph, { dark: false, showTraffic: true });
    expect(on.links[0].label.show).toBe(true);
    expect(on.links[0].label.formatter).toContain('12.3 Mbit/s');
    expect(on.links[0].label.formatter).toContain('1.2 Mbit/s');
    expect(on.links[1].label.show).toBe(false); // no rates
    expect(trafficLabel(graph.links[2])).toBe('');
  });

  it('local positions override the API and finish the layout', () => {
    const positions = { 'd:2': [10, 20], 'd:5': [30, 40], 'n:3': [50, 60] } as Record<
      string,
      [number, number]
    >;
    const b = buildGraph(graph, { dark: false, showTraffic: false, positions });
    expect(b.needsLayout).toBe(false);
    expect(b.nodes[1]).toMatchObject({ x: 10, y: 20, fixed: true });
    const all = buildGraph(
      { nodes: graph.nodes.map((n) => ({ ...n, x: 1, y: 2 })) as TopologyNode[], links: [] },
      { dark: false, showTraffic: false },
    );
    expect(all.needsLayout).toBe(false);
  });

  it('derives link end states', () => {
    expect(endState(end('d:1'))).toBeNull();
    expect(endState(end('d:1', { sensor_id: 1, oper_status: 1 }))).toBe('up');
    expect(endState(end('d:1', { sensor_id: 1, oper_status: 7 }))).toBe('down');
    expect(endState(end('d:1', { sensor_id: 1, oper_status: null, sensor_status: null }))).toBe(
      'unknown',
    );
    expect(endState(end('d:1', { sensor_id: 1, sensor_status: 'paused', oper_status: 1 }))).toBe(
      'paused',
    );
    expect(linkState(graph.links[2])).toBeNull();
    expect(linkColor(graph.links[0], true)).toBe(STATUS_HEX.up);
  });
});

describe('dependency suggestion mapping', () => {
  const cur = (p: Partial<DependencySuggestion['current']> = {}) => ({
    mode: 'inherit' as const,
    device_id: null,
    device_name: null,
    sensor_id: null,
    sensor_name: null,
    ...p,
  });
  const items: DependencySuggestion[] = [
    {
      device_id: 5,
      device_name: 'sw-floor2',
      suggested_parent_id: 1,
      suggested_parent_name: 'core-sw',
      current: cur(),
      effective_parent_id: null,
      hops: 1,
    },
    {
      device_id: 6,
      device_name: 'sw-floor3',
      suggested_parent_id: 1,
      suggested_parent_name: 'core-sw',
      current: cur({ mode: 'device', device_id: 1, device_name: 'core-sw' }),
      effective_parent_id: 1,
      hops: 1,
    },
    {
      device_id: 7,
      device_name: 'ap-2',
      suggested_parent_id: 5,
      suggested_parent_name: 'sw-floor2',
      current: cur(),
      effective_parent_id: 5,
      hops: 2,
    },
    {
      device_id: 8,
      device_name: 'lonely',
      suggested_parent_id: null,
      suggested_parent_name: null,
      current: cur({ mode: 'none' }),
      effective_parent_id: null,
      hops: 3,
    },
  ];

  it('flags rows that would change and preselects the useful ones', () => {
    const rows = suggestionRows(items);
    expect(rows.map((r) => [r.device_id, r.changes, r.alreadyEffective])).toEqual([
      [5, true, false],
      [6, false, true],
      [7, true, true], // inherited parent already matches: storing it explicitly is optional
      [8, false, false],
    ]);
    expect([...defaultSelection(rows)]).toEqual([5]);
  });

  it('maps the selection to the bulk endpoint body', () => {
    const rows = suggestionRows(items);
    expect(suggestionsToBulk(rows, new Set([5, 6, 7, 8]))).toEqual([
      { device_id: 5, dependency: { mode: 'device', device_id: 1, sensor_id: null } },
      { device_id: 7, dependency: { mode: 'device', device_id: 5, sensor_id: null } },
    ]);
    expect(suggestionsToBulk(rows, new Set())).toEqual([]);
  });
});
