// Data tables: column order and widths chosen by the user (drag a header, drag its edge), kept per
// browser and per table. Pure helpers (unit-tested in tableLayout.test.ts); the hook and the header
// cells live in components/kitTable.tsx.

export type ColumnSpec = {
  id: string;
  /** default width in px; columns without one share the free space */
  width?: number;
  /** narrowest width in px (a column never vanishes) */
  minWidth?: number;
  /** kept at the start / end edge (not movable): the selection and the actions columns */
  pin?: 'start' | 'end';
  /** neither movable nor resizable (checkbox and actions columns) */
  fixed?: boolean;
};

export type TableLayout = {
  /** the movable columns in the user's order (ids unknown to the table are ignored) */
  order: string[];
  /** user widths in px */
  widths: Record<string, number>;
};

export const MIN_COLUMN_WIDTH = 56;
/** Width of a flexible column when the table's minimum width is computed. */
export const FLEX_COLUMN_WIDTH = 140;
export const EMPTY_LAYOUT: TableLayout = { order: [], widths: {} };

export const layoutKey = (tableId: string) => `netmon.table.${tableId}`;

type Store = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

function safeStorage(): Store | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

export function minWidthOf(c: ColumnSpec): number {
  return Math.max(c.minWidth ?? MIN_COLUMN_WIDTH, 24);
}

export function isMovable(c: ColumnSpec): boolean {
  return !c.fixed && !c.pin;
}

export function isResizable(c: ColumnSpec): boolean {
  return !c.fixed;
}

/** Reads a stored layout; anything malformed reads as "no choice". */
export function loadLayout(tableId: string, storage: Store | null = safeStorage()): TableLayout {
  try {
    const raw = storage?.getItem(layoutKey(tableId));
    if (!raw) return EMPTY_LAYOUT;
    const v = JSON.parse(raw) as Partial<TableLayout>;
    const order = Array.isArray(v.order) ? v.order.filter((x) => typeof x === 'string') : [];
    const widths: Record<string, number> = {};
    if (v.widths && typeof v.widths === 'object')
      for (const [k, w] of Object.entries(v.widths))
        if (typeof w === 'number' && Number.isFinite(w) && w > 0) widths[k] = Math.round(w);
    return { order, widths };
  } catch {
    return EMPTY_LAYOUT;
  }
}

export function saveLayout(
  tableId: string,
  layout: TableLayout,
  storage: Store | null = safeStorage(),
): void {
  try {
    if (!layout.order.length && !Object.keys(layout.widths).length)
      storage?.removeItem(layoutKey(tableId));
    else storage?.setItem(layoutKey(tableId), JSON.stringify(layout));
  } catch {
    /* storage blocked or full: the layout lasts for this page only */
  }
}

/**
 * The columns in display order: pinned-start columns, the movable ones in the user's order, the
 * pinned-end columns. A movable column missing from the stored order (new, or never moved) keeps
 * its default place relative to its neighbours.
 */
export function orderColumns<C extends ColumnSpec>(specs: C[], order: string[]): C[] {
  const start = specs.filter((c) => c.pin === 'start');
  const end = specs.filter((c) => c.pin === 'end');
  const middle = specs.filter((c) => c.pin !== 'start' && c.pin !== 'end');
  const byId = new Map(middle.map((c) => [c.id, c]));
  const known = order.filter((id, i) => byId.has(id) && order.indexOf(id) === i);
  const out: C[] = known.map((id) => byId.get(id)!);
  // insert the others after the nearest earlier default neighbour that is already placed
  middle.forEach((c, i) => {
    if (known.includes(c.id)) return;
    let at = 0;
    for (let j = i - 1; j >= 0; j--) {
      const k = out.findIndex((x) => x.id === middle[j].id);
      if (k >= 0) {
        at = k + 1;
        break;
      }
    }
    out.splice(at, 0, c);
  });
  return [...start, ...out, ...end];
}

/** Ids of the movable columns in display order. */
export function movableOrder(specs: ColumnSpec[], order: string[]): string[] {
  return orderColumns(specs, order)
    .filter(isMovable)
    .map((c) => c.id);
}

/** Moves a column one place towards the start (delta -1) or the end (+1) of the table. */
export function moveColumn(specs: ColumnSpec[], order: string[], id: string, delta: number) {
  const ids = movableOrder(specs, order);
  const i = ids.indexOf(id);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= ids.length) return ids;
  const next = [...ids];
  [next[i], next[j]] = [next[j], next[i]];
  return next;
}

/** Drops column `dragId` before / after `targetId`. */
export function dropColumn(
  specs: ColumnSpec[],
  order: string[],
  dragId: string,
  targetId: string,
  after: boolean,
): string[] {
  const ids = movableOrder(specs, order);
  if (dragId === targetId || !ids.includes(dragId) || !ids.includes(targetId)) return ids;
  const rest = ids.filter((x) => x !== dragId);
  const at = rest.indexOf(targetId) + (after ? 1 : 0);
  rest.splice(at, 0, dragId);
  return rest;
}

/**
 * Which side of the header under the pointer a dragged column goes to: the half towards the
 * table's start is "before". In RTL the start is on the right.
 */
export function dropSide(
  clientX: number,
  rect: { left: number; width: number },
  rtl: boolean,
): 'before' | 'after' {
  const leftHalf = clientX < rect.left + rect.width / 2;
  return leftHalf !== rtl ? 'before' : 'after';
}

/** New width while dragging the end edge: in RTL the end edge is on the left (mirrored). */
export function resizedWidth(
  startWidth: number,
  startX: number,
  clientX: number,
  rtl: boolean,
  min: number,
): number {
  const dx = (clientX - startX) * (rtl ? -1 : 1);
  return Math.max(min, Math.round(startWidth + dx));
}

/** Arrow keys on the resize handle: towards the end edge grows (Right in LTR, Left in RTL). */
export function keyResize(
  key: string,
  width: number,
  rtl: boolean,
  min: number,
  step = 16,
): number | null {
  if (key !== 'ArrowLeft' && key !== 'ArrowRight') return null;
  const grow = (key === 'ArrowRight') !== rtl;
  return Math.max(min, width + (grow ? step : -step));
}

/** The minimum width of the table: every visible column at least at its width / minimum. */
export function tableMinWidth(columns: ColumnSpec[], widths: Record<string, number>): number {
  return columns.reduce(
    (sum, c) => sum + (widths[c.id] ?? c.width ?? Math.max(minWidthOf(c), FLEX_COLUMN_WIDTH)),
    0,
  );
}
