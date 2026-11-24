import { describe, expect, it } from 'vitest';
import type { TreeGroup } from '../api/types';
import {
  ancestors,
  dragItems,
  dropCheck,
  indexForMove,
  moveBody,
  moveCounts,
  normalizeMove,
  parseKey,
  pickerOptions,
  rangeKeys,
  refKey,
  selectedRefs,
  toggleKey,
  visibleOrder,
  type MoveRef,
} from './treeMove';

const counts = { up: 0, warning: 0, down: 0, paused: 0, unknown: 0, unreachable: 0, total: 0 };
const dev = (id: number) => ({
  id,
  name: `d${id}`,
  host: '10.0.0.1',
  enabled: true,
  status: 'up' as const,
  status_counts: counts,
  in_maintenance: false,
  parent_device_id: null,
  sensors: [],
});
const grp = (
  id: number,
  parent: number | null,
  children: TreeGroup[] = [],
  devices: number[] = [],
): TreeGroup => ({
  id,
  name: `G${id}`,
  parent_id: parent,
  status: 'up',
  status_counts: counts,
  children,
  devices: devices.map(dev),
});

// 1 ─ 2 ─ 3 (d30) ; 1 has d10, d11 ; 4 (d40) ; top level: 1, 4
const TREE = [grp(1, null, [grp(2, 1, [grp(3, 2, [], [30])])], [10, 11]), grp(4, null, [], [40])];
const ix = indexForMove(TREE);
const G = (id: number): MoveRef => ({ kind: 'group', id });
const D = (id: number): MoveRef => ({ kind: 'device', id });

describe('tree move: keys and index', () => {
  it('round-trips keys', () => {
    expect(refKey(G(5))).toBe('g5');
    expect(refKey(D(7))).toBe('d7');
    expect(parseKey('g5')).toEqual(G(5));
    expect(parseKey('d7')).toEqual(D(7));
    expect(parseKey('s7')).toBeNull();
    expect(selectedRefs(new Set(['g1', 'd10', 'x']))).toEqual([G(1), D(10)]);
  });

  it('knows ancestors and where a device is', () => {
    expect(ancestors(ix, 3)).toEqual([3, 2, 1]);
    expect(ancestors(ix, null)).toEqual([]);
    expect(ix.devices.get(30)?.groupId).toBe(3);
    expect(moveCounts([G(1), D(2), D(3)])).toEqual({ devices: 2, groups: 1 });
  });
});

describe('tree move: what moves', () => {
  it('drops duplicates, unknown ids and items carried by a moved group', () => {
    expect(normalizeMove(ix, [G(1), G(3), D(30), D(10), D(40), D(40), D(999)])).toEqual([
      G(1),
      D(40),
    ]);
  });

  it('allows a valid drop and refuses the others', () => {
    expect(dropCheck(ix, [D(10), D(11)], 4)).toBe('ok');
    expect(dropCheck(ix, [G(2)], 4)).toBe('ok');
    expect(dropCheck(ix, [G(2)], null)).toBe('ok'); // a group to the top level
    expect(dropCheck(ix, [G(2)], 2)).toBe('self');
    expect(dropCheck(ix, [G(1)], 3)).toBe('cycle'); // into its own grandchild
    expect(dropCheck(ix, [D(10), G(4)], 4)).toBe('self');
    expect(dropCheck(ix, [D(10)], null)).toBe('root'); // devices need a group
    expect(dropCheck(ix, [D(10), D(11)], 1)).toBe('same');
    expect(dropCheck(ix, [G(1)], null)).toBe('same');
    expect(dropCheck(ix, [], 1)).toBe('empty');
    expect(dropCheck(ix, [D(10)], 999)).toBe('empty');
    // partly there already is fine (the others move)
    expect(dropCheck(ix, [D(10), D(40)], 1)).toBe('ok');
  });

  it('builds the request body without the no-ops', () => {
    expect(moveBody(ix, [D(10), D(40), G(2), D(30)], 1)).toEqual({
      device_ids: [40],
      group_ids: [],
      target_group_id: 1,
    });
    expect(moveBody(ix, [G(2), D(30)], null)).toEqual({
      device_ids: [],
      group_ids: [2],
      target_group_id: null,
    });
  });
});

describe('tree move: selection', () => {
  const allOpen = () => true;
  it('lists the visible rows in tree order (collapsed groups hide their rows)', () => {
    expect(visibleOrder(TREE, allOpen)).toEqual([
      'g1',
      'g2',
      'g3',
      'd30',
      'd10',
      'd11',
      'g4',
      'd40',
    ]);
    expect(visibleOrder(TREE, (k) => k !== 'g2')).toEqual(['g1', 'g2', 'd10', 'd11', 'g4', 'd40']);
  });

  it('toggles and selects ranges in both directions', () => {
    const order = visibleOrder(TREE, allOpen);
    expect([...toggleKey(new Set(['g1']), 'd10')]).toEqual(['g1', 'd10']);
    expect([...toggleKey(new Set(['g1', 'd10']), 'g1')]).toEqual(['d10']);
    expect(rangeKeys(order, 'd30', 'd11')).toEqual(['d30', 'd10', 'd11']);
    expect(rangeKeys(order, 'd11', 'd30')).toEqual(['d30', 'd10', 'd11']);
    expect(rangeKeys(order, null, 'g4')).toEqual(['g4']);
    expect(rangeKeys(order, 'gone', 'g4')).toEqual(['g4']);
    expect(rangeKeys(order, 'g1', 'hidden')).toEqual([]);
  });

  it('drags the whole selection only when the dragged row is part of it', () => {
    const sel = new Set(['d10', 'd11']);
    expect(dragItems(sel, D(10))).toEqual([D(10), D(11)]);
    expect(dragItems(sel, D(40))).toEqual([D(40)]);
    expect(dragItems(new Set(['d10']), D(10))).toEqual([D(10)]);
  });
});

describe('tree move: the group picker', () => {
  it('lists every group with its check, the top level only for groups', () => {
    const opts = pickerOptions(TREE, ix, [G(2)]);
    expect(opts.map((o) => [o.id, o.depth, o.check])).toEqual([
      [null, 0, 'ok'],
      [1, 0, 'same'],
      [2, 1, 'self'],
      [3, 2, 'cycle'],
      [4, 0, 'ok'],
    ]);
    expect(pickerOptions(TREE, ix, [D(10), G(2)]).some((o) => o.id === null)).toBe(false);
  });

  it('filters by name (flat)', () => {
    const opts = pickerOptions(TREE, ix, [D(10)], 'g3');
    expect(opts.map((o) => [o.id, o.depth, o.check])).toEqual([[3, 0, 'ok']]);
  });
});
