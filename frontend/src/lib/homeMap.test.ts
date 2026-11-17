import { describe, expect, it } from 'vitest';
import type { OverviewMapNode } from '../api/premium';
import { edgePath, elapsed, layoutMap, MAP_LABEL, MAP_TILE, mapIcon } from './homeMap';

const node = (
  id: string,
  parent: string | null,
  extra: Partial<OverviewMapNode> = {},
): OverviewMapNode => ({
  id,
  kind: 'group',
  role: 'group',
  name: id,
  subtitle: '',
  status: 'up',
  problem: null,
  counts: { devices: 0, sensors: 0, down: 0, warning: 0, unreachable: 0 },
  ref: null,
  parent_id: parent,
  ...extra,
});

describe('home map layout', () => {
  it('stacks a chain and spreads the leaves in one row under it', () => {
    const nodes = [
      node('internet', null, { kind: 'internet', role: 'internet' }),
      node('d1', 'internet', { kind: 'device', role: 'firewall' }),
      node('d2', 'd1', { kind: 'device', role: 'core' }),
      node('g1', 'd2'),
      node('g2', 'd2'),
      node('g3', 'd2'),
    ];
    const l = layoutMap(nodes, [{ from: 'd2', to: 'g2', status: 'down' }], { minWidth: 900 });
    const at = Object.fromEntries(l.nodes.map((p) => [p.node.id, p]));
    expect(at.internet.x).toBe(at.d1.x);
    expect(at.d1.x).toBe(at.d2.x);
    expect(at.d2.x).toBe(at.g2.x); // centred over its children
    expect(at.g1.y).toBe(at.g3.y);
    expect(at.g1.y).toBeGreaterThan(at.d2.y);
    expect(at.g1.x).toBeLessThan(at.g2.x);
    expect(l.width).toBe(900);
    expect(l.edges).toHaveLength(5);
    // the broken link is drawn first, the healthy ones over it
    expect(l.edges[0]).toMatchObject({ status: 'down' });
  });

  it('grows wider than the hero with many leaves, and survives cycles and orphans', () => {
    const leaves = Array.from({ length: 20 }, (_, i) => node(`g${i}`, 'root'));
    const l = layoutMap([node('root', null), ...leaves], [], { minWidth: 600, colWidth: 100 });
    expect(l.width).toBe(2000);
    const cyc = layoutMap([node('a', 'b'), node('b', 'a'), node('c', 'missing')], [], {
      minWidth: 300,
    });
    expect(cyc.nodes).toHaveLength(3);
  });

  it('draws straight lines for a chain and rounded buses otherwise', () => {
    expect(edgePath({ x: 10, y: 0 }, { x: 10, y: 200 })).toMatch(/^M10 \S+V\S+$/);
    expect(edgePath({ x: 10, y: 0 }, { x: 110, y: 200 })).toContain('Q');
  });

  it('starts a link below the whole label block of its parent (name + caption)', () => {
    const start = (d: string) => Number(/^M\S+ (\S+)V/.exec(d)![1]);
    for (const b of [
      { x: 10, y: 160 },
      { x: 140, y: 160 },
    ])
      expect(start(edgePath({ x: 10, y: 0 }, b))).toBeGreaterThan(MAP_TILE / 2 + MAP_LABEL);
    // the bus of a row of children still sits between the parent's caption and the child tiles
    const l = layoutMap([node('r', null), node('a', 'r'), node('b', 'r')], [], { minWidth: 400 });
    for (const e of l.edges) {
      const ys = [...e.d.matchAll(/[MVQ ]-?[\d.]+ (-?[\d.]+)/g)].map((m) => Number(m[1]));
      expect(Math.min(...ys)).toBeGreaterThan(e.from.y + MAP_TILE / 2 + MAP_LABEL);
    }
  });

  it('picks icons by role and by group name', () => {
    expect(mapIcon(node('internet', null, { kind: 'internet', role: 'internet' }))).toBe(
      'internet',
    );
    expect(mapIcon(node('d', null, { kind: 'device', role: 'firewall' }))).toBe('firewall');
    expect(mapIcon(node('g', null, { name: 'Wi-Fi' }))).toBe('wifi');
    expect(mapIcon(node('g', null, { name: 'סניפים' }))).toBe('branch');
    expect(mapIcon(node('g', null, { name: 'Default' }))).toBe('group');
  });

  it('formats the time in a state as a clock', () => {
    const now = Date.parse('2026-11-16T12:00:00Z');
    expect(elapsed('2026-11-16T11:55:58Z', now)).toEqual({ clock: '4:02' });
    expect(elapsed('2026-11-16T10:56:49Z', now)).toEqual({ clock: '1:03:11' });
    expect(elapsed('2026-11-10T12:00:00Z', now)).toEqual({ days: 6 });
    expect(elapsed(null, now)).toBeNull();
  });
});
