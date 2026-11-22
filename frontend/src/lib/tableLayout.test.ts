import { describe, expect, it } from 'vitest';
import {
  dropColumn,
  dropSide,
  keyResize,
  layoutKey,
  loadLayout,
  moveColumn,
  orderColumns,
  resizedWidth,
  saveLayout,
  tableMinWidth,
  type ColumnSpec,
} from './tableLayout';

const specs: ColumnSpec[] = [
  { id: 'select', pin: 'start', fixed: true, width: 52 },
  { id: 'a', width: 100 },
  { id: 'b' },
  { id: 'c', width: 120 },
  { id: 'actions', pin: 'end', fixed: true, width: 110 },
];
const ids = (cs: ColumnSpec[]) => cs.map((c) => c.id);

function memStore() {
  const m = new Map<string, string>();
  return {
    m,
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
  };
}

describe('table layout: order', () => {
  it('keeps pinned columns at their edges and applies the user order to the others', () => {
    expect(ids(orderColumns(specs, []))).toEqual(['select', 'a', 'b', 'c', 'actions']);
    expect(ids(orderColumns(specs, ['c', 'a', 'b']))).toEqual(['select', 'c', 'a', 'b', 'actions']);
    // pinned / unknown ids in a stored order are ignored
    expect(ids(orderColumns(specs, ['actions', 'zz', 'b']))).toEqual([
      'select',
      'a',
      'b',
      'c',
      'actions',
    ]);
  });
  it('a column missing from the stored order keeps its default neighbour', () => {
    expect(ids(orderColumns(specs, ['c', 'a']))).toEqual(['select', 'c', 'a', 'b', 'actions']);
  });
  it('moves one place (keyboard) and never past the ends or the pinned columns', () => {
    expect(moveColumn(specs, [], 'b', -1)).toEqual(['b', 'a', 'c']);
    expect(moveColumn(specs, [], 'b', 1)).toEqual(['a', 'c', 'b']);
    expect(moveColumn(specs, [], 'a', -1)).toEqual(['a', 'b', 'c']);
    expect(moveColumn(specs, [], 'select', 1)).toEqual(['a', 'b', 'c']);
  });
  it('drops before / after a target', () => {
    expect(dropColumn(specs, [], 'c', 'a', false)).toEqual(['c', 'a', 'b']);
    expect(dropColumn(specs, [], 'a', 'c', true)).toEqual(['b', 'c', 'a']);
    expect(dropColumn(specs, [], 'a', 'actions', true)).toEqual(['a', 'b', 'c']);
  });
});

describe('table layout: RTL', () => {
  const rect = { left: 100, width: 100 };
  it('the half towards the start is "before": left in LTR, right in RTL', () => {
    expect(dropSide(120, rect, false)).toBe('before');
    expect(dropSide(180, rect, false)).toBe('after');
    expect(dropSide(120, rect, true)).toBe('after');
    expect(dropSide(180, rect, true)).toBe('before');
  });
  it('dragging the end edge grows to the right in LTR and to the left in RTL', () => {
    expect(resizedWidth(150, 500, 540, false, 56)).toBe(190);
    expect(resizedWidth(150, 500, 540, true, 56)).toBe(110);
    expect(resizedWidth(150, 500, 300, false, 56)).toBe(56); // min width: never vanishes
  });
  it('arrow keys mirror too', () => {
    expect(keyResize('ArrowRight', 100, false, 56)).toBe(116);
    expect(keyResize('ArrowLeft', 100, false, 56)).toBe(84);
    expect(keyResize('ArrowLeft', 100, true, 56)).toBe(116);
    expect(keyResize('ArrowRight', 60, true, 56)).toBe(56);
    expect(keyResize('Enter', 100, false, 56)).toBeNull();
  });
});

describe('table layout: persistence', () => {
  it('saves per table, reads back, ignores junk, and removes the key when reset', () => {
    const st = memStore();
    saveLayout('sensors', { order: ['c', 'a'], widths: { b: 210 } }, st);
    expect(st.m.has(layoutKey('sensors'))).toBe(true);
    expect(loadLayout('sensors', st)).toEqual({ order: ['c', 'a'], widths: { b: 210 } });
    expect(loadLayout('ports', st)).toEqual({ order: [], widths: {} });
    st.setItem(layoutKey('x'), '{oops');
    expect(loadLayout('x', st)).toEqual({ order: [], widths: {} });
    st.setItem(
      layoutKey('y'),
      JSON.stringify({ order: [1, 'a'], widths: { a: 'wide', b: -3, c: 99.6 } }),
    );
    expect(loadLayout('y', st)).toEqual({ order: ['a'], widths: { c: 100 } });
    saveLayout('sensors', { order: [], widths: {} }, st);
    expect(st.m.has(layoutKey('sensors'))).toBe(false);
  });
  it('blocked storage never throws', () => {
    const bad = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
      removeItem: () => {
        throw new Error('blocked');
      },
    };
    expect(loadLayout('t', bad)).toEqual({ order: [], widths: {} });
    expect(() => saveLayout('t', { order: ['a'], widths: {} }, bad)).not.toThrow();
  });
  it('minimum table width counts user widths, defaults and flexible columns', () => {
    expect(tableMinWidth(specs, {})).toBe(52 + 100 + 140 + 120 + 110);
    expect(tableMinWidth(specs, { b: 300 })).toBe(52 + 100 + 300 + 120 + 110);
  });
});
