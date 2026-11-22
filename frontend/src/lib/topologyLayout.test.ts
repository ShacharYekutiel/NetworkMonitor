import { describe, expect, it } from 'vitest';
import {
  arrangeAll,
  completeLayout,
  estimateLabelWidth,
  groupCaptions,
  overlappingPairs,
  ROW_STEP,
  TIER_STEP,
  usablePositions,
  type GroupInfo,
  type LayoutLink,
  type LayoutNode,
  type Pos,
} from './topologyLayout';

const node = (key: string, p: Partial<LayoutNode> = {}): LayoutNode => ({
  key,
  name: key,
  kind: 'device',
  groupId: 1,
  deviceClass: null,
  pos: null,
  ...p,
});
const link = (a: string, b: string): LayoutLink => ({ a, b });

/** A seeded PRNG so the "random" graphs are the same on every run. */
function rng(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

/** A mixed graph: trees, a star, a ring, unlinked devices in several groups, long names. */
function mixed(n: number, seed = 1): { nodes: LayoutNode[]; links: LayoutLink[] } {
  const r = rng(seed);
  const nodes: LayoutNode[] = [];
  const links: LayoutLink[] = [];
  for (let i = 0; i < n; i++) {
    const long = r() < 0.15;
    nodes.push(
      node(`d:${i}`, {
        name: long ? `building-${i}-floor-${i % 7}-access-switch` : `sw-${i}`,
        groupId: 1 + Math.floor(r() * 5),
        deviceClass: r() < 0.1 ? 'router' : 'switch',
      }),
    );
  }
  // ~half of the nodes linked: a few trees + random extra links
  const linked = Math.floor(n / 2);
  for (let i = 1; i < linked; i++) {
    const parent = Math.floor(r() * i);
    if (r() < 0.9) links.push(link(`d:${parent}`, `d:${i}`));
  }
  for (let k = 0; k < n / 10; k++) {
    const a = Math.floor(r() * linked);
    const b = Math.floor(r() * linked);
    links.push(link(`d:${a}`, `d:${b}`));
  }
  return { nodes, links };
}

const groups = new Map<number, GroupInfo>([
  [1, { name: 'Head office', order: 0 }],
  [2, { name: 'Branch A', order: 1 }],
  [3, { name: 'Branch B', order: 2 }],
  [4, { name: 'Warehouse', order: 3 }],
  [5, { name: 'Lab', order: 4 }],
]);

describe('arrangeAll', () => {
  it('gives every node a position and no two footprints overlap (N up to 300, mixed)', () => {
    for (const n of [1, 2, 5, 30, 77, 150, 300]) {
      for (const seed of [1, 2, 3]) {
        const { nodes, links } = mixed(n, seed);
        for (const style of ['tiered', 'groups'] as const) {
          const pos = arrangeAll(nodes, links, style, { groups });
          expect(Object.keys(pos)).toHaveLength(n);
          expect(overlappingPairs(nodes, pos), `${n}/${seed}/${style}`).toEqual([]);
        }
      }
    }
  });

  it('30 unlinked devices: a grid per group, never stacked', () => {
    const nodes = Array.from({ length: 30 }, (_, i) =>
      node(`d:${i}`, { name: `sw-${i}`, groupId: 1 + (i % 3) }),
    );
    const pos = arrangeAll(nodes, [], 'tiered', { groups });
    const spots = new Set(Object.values(pos).map((p) => p.join(',')));
    expect(spots.size).toBe(30);
    expect(overlappingPairs(nodes, pos)).toEqual([]);
    // grouped: the devices of one group share their rows only with each other
    const rowsOf = (g: number) =>
      new Set(nodes.filter((n) => n.groupId === g).map((n) => pos[n.key][1]));
    for (const y of rowsOf(1)) expect(rowsOf(2).has(y) && rowsOf(3).has(y)).toBe(false);
    // the picture is wider than tall (the canvas aspect), not one long row or column
    const xs = Object.values(pos).map((p) => p[0]);
    const ys = Object.values(pos).map((p) => p[1]);
    const w = Math.max(...xs) - Math.min(...xs);
    const h = Math.max(...ys) - Math.min(...ys);
    expect(w).toBeGreaterThan(h);
    expect(w / h).toBeLessThan(6);
  });

  it('a core / distribution / access tree is tiered: core on top, access at the bottom', () => {
    const nodes = [node('core-1'), node('core-2')];
    const links = [link('core-1', 'core-2')];
    for (let d = 1; d <= 6; d++) {
      nodes.push(node(`dist-${d}`));
      links.push(link(d % 2 ? 'core-1' : 'core-2', `dist-${d}`));
      if (d % 3 === 1) links.push(link(d % 2 ? 'core-2' : 'core-1', `dist-${d}`));
      for (let a = 1; a <= 5; a++) {
        nodes.push(node(`acc-${d}-${a}`));
        links.push(link(`dist-${d}`, `acc-${d}-${a}`));
      }
    }
    const pos = arrangeAll(nodes, links);
    expect(overlappingPairs(nodes, pos)).toEqual([]);
    const y = (k: string) => pos[k][1];
    expect(y('core-1')).toBe(y('core-2'));
    for (let d = 1; d <= 6; d++) {
      expect(y(`dist-${d}`)).toBe(y('core-1') + TIER_STEP);
      for (let a = 1; a <= 5; a++) expect(y(`acc-${d}-${a}`)).toBeGreaterThan(y(`dist-${d}`));
    }
    // the cores are above the middle of their tier, not at an edge
    const xs = Object.values(pos).map((p) => p[0]);
    const mid = (Math.min(...xs) + Math.max(...xs)) / 2;
    const coreMid = (pos['core-1'][0] + pos['core-2'][0]) / 2;
    expect(Math.abs(coreMid - mid)).toBeLessThan((Math.max(...xs) - Math.min(...xs)) / 4);
  });

  it('a big star: the hub on top, the leaves wrap into rows under it', () => {
    const nodes = [node('hub', { deviceClass: 'switch' })];
    const links: LayoutLink[] = [];
    for (let i = 0; i < 60; i++) {
      nodes.push(node(`leaf-${i}`));
      links.push(link('hub', `leaf-${i}`));
    }
    const pos = arrangeAll(nodes, links);
    expect(overlappingPairs(nodes, pos)).toEqual([]);
    for (let i = 0; i < 60; i++) expect(pos[`leaf-${i}`][1]).toBeGreaterThan(pos.hub[1]);
    const rows = new Set(nodes.slice(1).map((n) => pos[n.key][1]));
    expect(rows.size).toBeGreaterThan(2);
    expect([...rows].every((y) => (y - pos.hub[1] - TIER_STEP) % ROW_STEP === 0)).toBe(true);
  });

  it('linked components above the unlinked grid, side by side', () => {
    const nodes = [node('a1'), node('a2'), node('b1'), node('b2'), node('x'), node('y')];
    const links = [link('a1', 'a2'), link('b1', 'b2')];
    const pos = arrangeAll(nodes, links);
    expect(overlappingPairs(nodes, pos)).toEqual([]);
    const linkedMaxY = Math.max(...['a1', 'a2', 'b1', 'b2'].map((k) => pos[k][1]));
    expect(pos.x[1]).toBeGreaterThan(linkedMaxY);
    expect(pos.y[1]).toBeGreaterThan(linkedMaxY);
    // the two components share a row
    expect(pos.a1[1]).toBe(pos.b1[1]);
  });

  it('is deterministic and independent of the input order', () => {
    const { nodes, links } = mixed(120, 7);
    const a = arrangeAll(nodes, links, 'tiered', { groups });
    const b = arrangeAll([...nodes].reverse(), [...links].reverse(), 'tiered', { groups });
    expect(b).toEqual(a);
    expect(arrangeAll(nodes, links, 'groups', { groups })).toEqual(
      arrangeAll([...nodes].reverse(), links, 'groups', { groups }),
    );
  });

  it('spacing follows the label width (long names never collide)', () => {
    const nodes = Array.from({ length: 12 }, (_, i) =>
      node(`d:${i}`, { name: `a-very-long-device-name-number-${i}-in-the-rack` }),
    );
    const pos = arrangeAll(nodes, []);
    const row = nodes.map((n) => pos[n.key]).filter((p) => p[1] === pos['d:0'][1]);
    row.sort((p, q) => p[0] - q[0]);
    for (let i = 1; i < row.length; i++)
      expect(row[i][0] - row[i - 1][0]).toBeGreaterThanOrEqual(estimateLabelWidth(nodes[0].name));
  });

  it('"by group" keeps each group together', () => {
    const { nodes, links } = mixed(60, 4);
    const pos = arrangeAll(nodes, links, 'groups', { groups });
    expect(overlappingPairs(nodes, pos)).toEqual([]);
    // the boxes of two groups do not intersect
    const box = (g: number) => {
      const ps = nodes.filter((n) => n.groupId === g).map((n) => pos[n.key]);
      return [
        Math.min(...ps.map((p) => p[0])),
        Math.min(...ps.map((p) => p[1])),
        Math.max(...ps.map((p) => p[0])),
        Math.max(...ps.map((p) => p[1])),
      ];
    };
    for (let g = 1; g <= 5; g++)
      for (let h = g + 1; h <= 5; h++) {
        const [a, b] = [box(g), box(h)];
        const disjoint = a[2] < b[0] || b[2] < a[0] || a[3] < b[1] || b[3] < a[1];
        expect(disjoint, `${g} vs ${h}`).toBe(true);
      }
  });
});

describe('completeLayout', () => {
  it('no saved positions: the default arrangement for everybody', () => {
    const { nodes, links } = mixed(40);
    expect(completeLayout(nodes, links)).toEqual(arrangeAll(nodes, links));
  });

  it('keeps saved positions and places a new linked node near its neighbour, without overlap', () => {
    const { nodes, links } = mixed(80, 5);
    const saved = arrangeAll(nodes, links);
    const withSaved = nodes.map((n) => ({ ...n, pos: saved[n.key] }));
    const fresh = node('d:new', { name: 'new-switch' });
    const all = [...withSaved, fresh];
    const allLinks = [...links, link('d:3', 'd:new')];
    const added = completeLayout(all, allLinks);
    expect(Object.keys(added)).toEqual(['d:new']);
    const merged: Pos = { ...saved, ...added };
    expect(overlappingPairs(all, merged)).toEqual([]);
    const d = Math.hypot(added['d:new'][0] - saved['d:3'][0], added['d:new'][1] - saved['d:3'][1]);
    expect(d).toBeLessThan(3 * TIER_STEP);
    // nothing else moved
    expect(
      completeLayout(
        all.map((n) => ({ ...n, pos: merged[n.key] })),
        allLinks,
      ),
    ).toEqual({});
  });

  it('a new unlinked device joins its group grid', () => {
    const nodes = Array.from({ length: 12 }, (_, i) =>
      node(`d:${i}`, { name: `sw-${i}`, groupId: i < 6 ? 1 : 2 }),
    );
    const saved = arrangeAll(nodes, [], 'tiered', { groups });
    const all = [
      ...nodes.map((n) => ({ ...n, pos: saved[n.key] })),
      node('d:99', { name: 'sw-99', groupId: 2 }),
    ];
    const added = completeLayout(all, [], { groups });
    const merged = { ...saved, ...added };
    expect(overlappingPairs(all, merged)).toEqual([]);
    const g2 = nodes.filter((n) => n.groupId === 2).map((n) => saved[n.key]);
    const nearest = Math.min(
      ...g2.map((p) => Math.hypot(p[0] - added['d:99'][0], p[1] - added['d:99'][1])),
    );
    expect(nearest).toBeLessThanOrEqual(ROW_STEP * 1.6);
  });

  it('adding one device does not move the others; the same input gives the same spot', () => {
    const { nodes, links } = mixed(150, 9);
    const saved = arrangeAll(nodes, links);
    const all = [...nodes.map((n) => ({ ...n, pos: saved[n.key] })), node('d:x', { groupId: 3 })];
    const a = completeLayout(all, links);
    const b = completeLayout([...all].reverse(), [...links].reverse());
    expect(a).toEqual(b);
    expect(Object.keys(a)).toEqual(['d:x']);
  });

  it('nodes saved on the same spot (0/0) are spread; the first by name keeps it', () => {
    const nodes = [
      node('a', { pos: [0, 0] }),
      node('b', { pos: [0, 0] }),
      node('c', { pos: [0.5, -0.5] }),
      node('d', { pos: [400, 0] }),
      node('e', { pos: [NaN, 3] }),
    ];
    expect(usablePositions(nodes)).toEqual({ a: [0, 0], d: [400, 0] });
    const added = completeLayout(nodes, [link('a', 'b')]);
    expect(Object.keys(added).sort()).toEqual(['b', 'c', 'e']);
    expect(overlappingPairs(nodes, { a: [0, 0], d: [400, 0], ...added })).toEqual([]);
  });

  it('many new nodes next to a few saved ones: arranged as a block beside them', () => {
    const { nodes, links } = mixed(100, 11);
    const all = nodes.map((n, i) => (i < 5 ? { ...n, pos: [i * 400, 0] as [number, number] } : n));
    const added = completeLayout(all, links, { groups });
    expect(Object.keys(added)).toHaveLength(95);
    const merged: Pos = { ...usablePositions(all), ...added };
    expect(overlappingPairs(all, merged)).toEqual([]);
  });

  it('a saved layout with 300 mixed nodes and 40 new ones stays overlap-free', () => {
    const { nodes, links } = mixed(300, 13);
    const saved = arrangeAll(nodes.slice(0, 260), links);
    const all = nodes.map((n) => ({ ...n, pos: saved[n.key] ?? null }));
    const added = completeLayout(all, links, { groups });
    expect(Object.keys(added)).toHaveLength(40);
    for (const k of Object.keys(saved)) expect(added[k]).toBeUndefined();
    expect(overlappingPairs(all, { ...saved, ...added })).toEqual([]);
  });
});

describe('groupCaptions', () => {
  it('a caption above each group grid of the default arrangement; none once a group is mixed', () => {
    const nodes = Array.from({ length: 9 }, (_, i) =>
      node(`d:${i}`, { name: `sw-${i}`, groupId: 1 + (i % 3) }),
    );
    const pos = arrangeAll(nodes, [], 'tiered', { groups });
    const caps = groupCaptions(nodes, [], pos, { groups });
    expect(caps.map((c) => c.name)).toEqual(['Head office', 'Branch A', 'Branch B']);
    for (const c of caps) {
      const ys = nodes.filter((n) => n.groupId === c.groupId).map((n) => pos[n.key][1]);
      expect(c.y).toBeLessThan(Math.min(...ys));
    }
    // move a Branch A device into the middle of the Head office grid
    const ho = nodes.filter((n) => n.groupId === 1).map((n) => pos[n.key]);
    const moved = { ...pos, 'd:1': [(ho[0][0] + ho[1][0]) / 2, ho[0][1]] as [number, number] };
    const after = groupCaptions(nodes, [], moved, { groups }).map((c) => c.name);
    expect(after).not.toContain('Head office');
    expect(groupCaptions(nodes, [], pos)).toEqual([]);
  });
});
