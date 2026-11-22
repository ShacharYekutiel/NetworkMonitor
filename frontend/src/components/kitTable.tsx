// Shared data-table behaviour (kit): movable and resizable columns remembered per browser and per
// table, a pinned actions column at the end edge, and the "Columns" popover (show / move / reset).
// Logical properties only, so Hebrew mirrors the edges and the drag directions.
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useTranslation } from 'react-i18next';
import {
  dropColumn,
  dropSide,
  isMovable,
  isResizable,
  keyResize,
  loadLayout,
  minWidthOf,
  moveColumn,
  orderColumns,
  resizedWidth,
  saveLayout,
  tableMinWidth,
  type ColumnSpec,
  type TableLayout,
} from '../lib/tableLayout';
import { ChevronIcon, ColumnsIcon } from './icons';
import { createPortal } from 'react-dom';
import { cx } from './ui';

export type { ColumnSpec } from '../lib/tableLayout';

type Drag = { id: string; over: string | null; side: 'before' | 'after' };

export type ColumnLayout<C extends ColumnSpec = ColumnSpec> = {
  tableId: string;
  /** every column in display order (filter the hidden ones yourself) */
  columns: C[];
  widthOf: (id: string) => number | undefined;
  setWidth: (id: string, width: number, persist?: boolean) => void;
  resetWidth: (id: string) => void;
  move: (id: string, delta: number) => void;
  drop: (dragId: string, targetId: string, after: boolean) => void;
  reset: () => void;
  customized: boolean;
  drag: Drag | null;
  setDrag: (d: Drag | null) => void;
};

/** Column order and widths of one table, remembered in localStorage (`netmon.table.<id>`). */
export function useColumnLayout<C extends ColumnSpec>(
  tableId: string,
  specs: C[],
): ColumnLayout<C> {
  const [layout, setLayout] = useState<TableLayout>(() => loadLayout(tableId));
  const [drag, setDrag] = useState<Drag | null>(null);
  const latest = useRef(layout);
  latest.current = layout;
  const commit = useCallback(
    (next: TableLayout, persist = true) => {
      latest.current = next;
      setLayout(next);
      if (persist) saveLayout(tableId, next);
    },
    [tableId],
  );
  const columns = useMemo(() => orderColumns(specs, layout.order), [specs, layout.order]);
  const specOf = (id: string) => specs.find((c) => c.id === id);
  return {
    tableId,
    columns,
    widthOf: (id) => layout.widths[id] ?? specOf(id)?.width,
    setWidth: (id, width, persist = true) => {
      const c = specOf(id);
      if (!c || !isResizable(c)) return;
      const w = Math.max(minWidthOf(c), Math.round(width));
      commit({ ...latest.current, widths: { ...latest.current.widths, [id]: w } }, persist);
    },
    resetWidth: (id) => {
      const widths = { ...latest.current.widths };
      delete widths[id];
      commit({ ...latest.current, widths });
    },
    move: (id, delta) =>
      commit({ ...latest.current, order: moveColumn(specs, latest.current.order, id, delta) }),
    drop: (dragId, targetId, after) =>
      commit({
        ...latest.current,
        order: dropColumn(specs, latest.current.order, dragId, targetId, after),
      }),
    reset: () => commit({ order: [], widths: {} }),
    customized: layout.order.length > 0 || Object.keys(layout.widths).length > 0,
    drag,
    setDrag,
  };
}

const isRtl = (el: Element) => getComputedStyle(el).direction === 'rtl';

/**
 * The table: fixed layout (so a long text never pushes the other columns), the user's widths in a
 * colgroup, a minimum width so narrow screens scroll inside the card, never the page.
 */
export function ColumnTable({
  layout,
  columns,
  children,
  className,
  colClass,
  testId,
  scrollTestId,
}: {
  layout: ColumnLayout;
  /** the visible columns, in order */
  columns: ColumnSpec[];
  children: ReactNode;
  className?: string;
  /** extra classes of a <col> (e.g. responsive hiding, mirrored from the cells) */
  colClass?: (id: string) => string | undefined;
  testId?: string;
  scrollTestId?: string;
}) {
  const widths: Record<string, number> = {};
  for (const c of columns) {
    const w = layout.widthOf(c.id);
    if (w !== undefined) widths[c.id] = w;
  }
  return (
    <div className="overflow-x-auto" data-testid={scrollTestId}>
      <table
        data-testid={testId}
        data-table={layout.tableId}
        className={cx(
          'w-full table-fixed text-sm [&_tbody>tr:hover>td]:bg-surface-hover [&_tbody>tr:last-child>td]:border-b-0',
          className,
        )}
        style={{ minWidth: tableMinWidth(columns, widths) }}
      >
        <colgroup>
          {columns.map((c) => (
            <col
              key={c.id}
              data-col={c.id}
              className={colClass?.(c.id)}
              style={widths[c.id] !== undefined ? { width: widths[c.id] } : undefined}
            />
          ))}
        </colgroup>
        {children}
      </table>
    </div>
  );
}

/** A long text cell: one line with an ellipsis (give it a `title` with the full text). */
export const CELL_TRUNCATE = 'overflow-hidden text-ellipsis whitespace-nowrap';
/**
 * The actions column: pinned at the end edge (sticky, opaque) so its buttons stay visible however
 * long the other cells are and however far the table scrolls.
 */
export const CELL_PINNED_END = 'sticky end-0 z-[1] bg-surface whitespace-nowrap text-end';

/**
 * A header cell: drag it to move the column (a line shows where it goes), drag its end edge to
 * resize (double-click the edge resets), or focus the edge and use the arrow keys.
 */
export function ColumnHeader({
  layout,
  col,
  label,
  children,
  className,
}: {
  layout: ColumnLayout;
  col: ColumnSpec;
  /** the column's name (accessible names of the edge) */
  label: string;
  children?: ReactNode;
  className?: string;
}) {
  const { t } = useTranslation();
  const ref = useRef<HTMLTableCellElement>(null);
  const suppressClick = useRef(false);
  const movable = isMovable(col);
  const resizable = isResizable(col);
  const min = minWidthOf(col);
  const drag = layout.drag;
  const dropHere = drag && drag.over === col.id && drag.id !== col.id ? drag.side : undefined;

  const onPointerDown = (e: React.PointerEvent<HTMLTableCellElement>) => {
    if (!movable || e.button !== 0) return;
    const target = e.target as Element;
    if (target.closest('[data-col-resize], input, button, a, select, textarea')) return;
    const th = e.currentTarget;
    const table = th.closest('table');
    if (!table) return;
    const rtl = isRtl(th);
    const startX = e.clientX;
    const startY = e.clientY;
    let dragging = false;
    let last: Drag | null = null;
    const onMove = (ev: PointerEvent) => {
      if (!dragging && Math.hypot(ev.clientX - startX, ev.clientY - startY) < 6) return;
      dragging = true;
      const heads = Array.from(table.querySelectorAll<HTMLElement>('th[data-col-movable]'));
      let hit: HTMLElement | null = null;
      let best = Infinity;
      for (const h of heads) {
        const r = h.getBoundingClientRect();
        const d =
          ev.clientX < r.left
            ? r.left - ev.clientX
            : ev.clientX > r.right
              ? ev.clientX - r.right
              : 0;
        if (d < best) {
          best = d;
          hit = h;
        }
      }
      last = {
        id: col.id,
        over: hit?.dataset.col ?? null,
        side: hit ? dropSide(ev.clientX, hit.getBoundingClientRect(), rtl) : 'before',
      };
      layout.setDrag(last);
    };
    const onUp = () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
      layout.setDrag(null);
      if (dragging) {
        suppressClick.current = true; // a sort button under the pointer must not fire
        setTimeout(() => (suppressClick.current = false), 0);
        if (last?.over) layout.drop(col.id, last.over, last.side === 'after');
      }
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
  };

  const onResizeDown = (e: React.PointerEvent<HTMLSpanElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const th = ref.current;
    if (!th) return;
    const rtl = isRtl(th);
    const startX = e.clientX;
    const startW = th.getBoundingClientRect().width;
    let w = startW;
    const onMove = (ev: PointerEvent) => {
      w = resizedWidth(startW, startX, ev.clientX, rtl, min);
      layout.setWidth(col.id, w, false);
    };
    const onUp = () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
      if (w !== startW) layout.setWidth(col.id, w, true);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
  };

  const onResizeKey = (e: React.KeyboardEvent<HTMLSpanElement>) => {
    const th = ref.current;
    if (!th) return;
    if (e.key === 'Enter' || e.key === 'Delete') {
      e.preventDefault();
      layout.resetWidth(col.id);
      return;
    }
    const next = keyResize(
      e.key,
      layout.widthOf(col.id) ?? th.getBoundingClientRect().width,
      isRtl(th),
      min,
      e.shiftKey ? 48 : 16,
    );
    if (next === null) return;
    e.preventDefault();
    layout.setWidth(col.id, next);
  };

  const width = layout.widthOf(col.id);
  return (
    <th
      ref={ref}
      scope="col"
      // the visible name only (not the resize handle's label)
      aria-label={label}
      data-col={col.id}
      data-col-movable={movable || undefined}
      data-drop={dropHere}
      data-dragging={drag?.id === col.id || undefined}
      title={movable ? t('tableCols.dragHint') : undefined}
      onPointerDown={onPointerDown}
      onClickCapture={(e) => {
        if (suppressClick.current) {
          e.preventDefault();
          e.stopPropagation();
        }
      }}
      className={cx(
        'relative overflow-hidden text-ellipsis',
        movable && 'cursor-grab select-none active:cursor-grabbing',
        'data-[dragging]:opacity-50',
        // the drop line, on the side the column goes to (logical: mirrored in RTL)
        'before:pointer-events-none before:absolute before:inset-y-1.5 before:z-[2] before:w-[3px] before:rounded-full before:bg-accent before:opacity-0 before:content-[""]',
        'data-[drop=before]:before:start-0 data-[drop=before]:before:opacity-100',
        'data-[drop=after]:before:end-0 data-[drop=after]:before:opacity-100',
        className,
      )}
    >
      {children ?? label}
      {resizable && (
        <span
          role="separator"
          aria-orientation="vertical"
          aria-label={t('tableCols.resize', { name: label })}
          aria-valuemin={min}
          aria-valuenow={width !== undefined ? Math.round(width) : undefined}
          tabIndex={0}
          title={t('tableCols.resizeHint')}
          data-col-resize={col.id}
          data-testid="col-resize"
          onPointerDown={onResizeDown}
          onDoubleClick={(e) => {
            e.stopPropagation();
            layout.resetWidth(col.id);
          }}
          onKeyDown={onResizeKey}
          className="absolute inset-y-0 end-0 z-[3] w-2.5 cursor-col-resize touch-none after:absolute after:inset-y-2 after:end-[3px] after:w-0.5 after:rounded-full after:bg-line-strong after:opacity-0 after:transition-opacity after:content-[''] hover:after:opacity-100 focus-visible:outline-none focus-visible:after:bg-accent focus-visible:after:opacity-100"
        />
      )}
    </th>
  );
}

/**
 * "Columns" popover (prototype .popmenu): one row per column with an optional show / hide switch
 * and move up / down buttons (the keyboard way to reorder), then "Reset columns".
 */
export function ColumnsMenu({
  layout,
  labelOf,
  visible,
  onVisible,
  onReset,
  testId = 'columns-menu',
  buttonClassName,
  label,
}: {
  /** the button's name (default "Columns") */
  label?: string;
  layout: ColumnLayout;
  labelOf: (id: string) => string;
  /** with a visibility choice: which columns are shown */
  visible?: (id: string) => boolean;
  onVisible?: (id: string, on: boolean) => void;
  /** extra reset work (e.g. the default visible columns) */
  onReset?: () => void;
  testId?: string;
  buttonClassName?: string;
}) {
  const { t } = useTranslation();
  const movable = layout.columns.filter(isMovable);
  const listed = layout.columns.filter((c) => !c.fixed);
  const name = label ?? t('tableCols.columns');
  return (
    <FloatingPopover
      label={name}
      testId={testId}
      className={buttonClassName}
      button={
        <>
          <ColumnsIcon size={16} /> {name}
        </>
      }
    >
      <div className="-m-2 grid max-h-[60vh] gap-0.5 overflow-y-auto">
        <div className="px-2.5 pb-1 pt-1.5 text-xs font-semibold text-ink-subtle">{name}</div>
        <ul className="m-0 grid list-none gap-0.5 p-0" aria-label={name}>
          {listed.map((c) => {
            const name = labelOf(c.id);
            const i = movable.findIndex((x) => x.id === c.id);
            const canMove = i >= 0;
            const on = visible ? visible(c.id) : true;
            return (
              <li
                key={c.id}
                data-testid="columns-menu-row"
                data-col={c.id}
                className="flex min-h-[38px] items-center gap-1 rounded-[10px] pe-1 ps-2.5 text-sm hover:bg-chip"
              >
                {onVisible ? (
                  <label className="flex min-w-0 flex-1 cursor-pointer items-center gap-2.5">
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={(e) => onVisible(c.id, e.target.checked)}
                      className="relative h-[22px] w-9 shrink-0 cursor-pointer appearance-none rounded-full bg-chip shadow-[inset_0_0_0_1px_var(--border)] transition-colors duration-300 before:absolute before:start-0.5 before:top-0.5 before:h-[18px] before:w-[18px] before:rounded-full before:bg-white before:shadow-[0_1px_3px_rgba(0,0,0,0.25)] before:transition-[inset-inline-start] before:duration-300 before:content-[''] checked:bg-status-up checked:shadow-none checked:before:start-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
                    />
                    <span className="min-w-0 truncate">{name}</span>
                  </label>
                ) : (
                  <span className="min-w-0 flex-1 truncate">{name}</span>
                )}
                {canMove && (
                  <>
                    <button
                      type="button"
                      aria-label={t('tableCols.moveUp', { name })}
                      title={t('tableCols.moveUp', { name })}
                      disabled={i === 0}
                      onClick={() => layout.move(c.id, -1)}
                      className="grid h-7 w-7 shrink-0 place-items-center rounded-[8px] text-ink-muted hover:bg-surface hover:text-ink disabled:opacity-30"
                    >
                      <ChevronIcon size={14} className="-rotate-90" />
                    </button>
                    <button
                      type="button"
                      aria-label={t('tableCols.moveDown', { name })}
                      title={t('tableCols.moveDown', { name })}
                      disabled={i === movable.length - 1}
                      onClick={() => layout.move(c.id, 1)}
                      className="grid h-7 w-7 shrink-0 place-items-center rounded-[8px] text-ink-muted hover:bg-surface hover:text-ink disabled:opacity-30"
                    >
                      <ChevronIcon size={14} className="rotate-90" />
                    </button>
                  </>
                )}
              </li>
            );
          })}
        </ul>
        <div className="mt-1 border-t border-line px-1 pt-1.5">
          <button
            type="button"
            data-testid="columns-reset"
            onClick={() => {
              layout.reset();
              onReset?.();
            }}
            className="h-8 w-full rounded-[10px] px-2 text-start text-sm font-medium text-accent-ink hover:bg-chip"
          >
            {t('tableCols.reset')}
          </button>
        </div>
      </div>
    </FloatingPopover>
  );
}

/** One column of a DataTable: its spec, header and cell. */
export type DataColumn<R> = ColumnSpec & {
  label: string;
  cell: (row: R) => ReactNode;
  /** full text on hover of a truncated cell */
  title?: (row: R) => string | undefined;
  /** cell classes; text cells truncate unless `wrap` */
  className?: string;
  wrap?: boolean;
  dir?: 'auto' | 'ltr' | 'rtl';
  /** numbers: header and cells at the end edge */
  numeric?: boolean;
};

const DT_TH =
  'whitespace-nowrap border-b border-line px-3.5 pb-2.5 pt-3 text-start text-[12.5px] font-medium text-ink-muted first:ps-5 last:pe-5';
const DT_TD =
  'border-b border-line px-3.5 py-3 align-middle transition-colors first:ps-5 last:pe-5';

/**
 * A data table with the shared column behaviour (move, resize, remembered, pinned actions). The
 * "Columns" popover is `ColumnsMenu` with the same layout (`useDataTable`).
 */
export function DataTable<R>({
  layout,
  columns,
  rows,
  rowKey,
  rowProps,
  testId,
}: {
  layout: ColumnLayout;
  /** the visible columns in order (`layout.columns` of `useDataTable`) */
  columns: DataColumn<R>[];
  rows: R[];
  rowKey: (row: R) => string | number;
  rowProps?: (row: R) => React.HTMLAttributes<HTMLTableRowElement> & Record<string, unknown>;
  testId?: string;
}) {
  return (
    <ColumnTable layout={layout} columns={columns} testId={testId}>
      <thead>
        <tr>
          {columns.map((c) =>
            c.fixed || c.pin === 'end' ? (
              <th
                key={c.id}
                scope="col"
                data-col={c.id}
                className={cx(DT_TH, c.pin === 'end' && CELL_PINNED_END)}
              >
                <span className={c.pin === 'end' ? 'sr-only' : undefined}>{c.label}</span>
              </th>
            ) : (
              <ColumnHeader
                key={c.id}
                layout={layout}
                col={c}
                label={c.label}
                className={cx(DT_TH, c.numeric && 'text-end')}
              />
            ),
          )}
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={rowKey(r)} {...rowProps?.(r)}>
            {columns.map((c) => (
              <td
                key={c.id}
                dir={c.dir}
                title={c.title?.(r)}
                className={cx(
                  DT_TD,
                  c.pin === 'end' ? CELL_PINNED_END : c.wrap ? 'break-words' : CELL_TRUNCATE,
                  c.numeric && 'num text-end tabular-nums',
                  c.className,
                )}
              >
                {c.cell(r)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </ColumnTable>
  );
}

/** Layout of a DataTable: the columns in the user's order (all of them; filter hidden ones). */
export function useDataTable<R>(tableId: string, columns: DataColumn<R>[]) {
  const layout = useColumnLayout(tableId, columns);
  return layout as ColumnLayout<DataColumn<R>>;
}

/** The header text of a column (for ColumnsMenu `labelOf`). */
export function labelOf(columns: { id: string; label: string }[], id: string): string {
  return columns.find((c) => c.id === id)?.label ?? id;
}

/**
 * The kit Popover's look, but the card floats over the page (portal, fixed): a cell or a card
 * with `overflow: hidden` never clips it. Escape / outside click close; focus returns to the button.
 */
function FloatingPopover({
  button,
  label,
  children,
  className,
  testId,
}: {
  button: ReactNode;
  label: string;
  children: ReactNode;
  className?: string;
  testId?: string;
}) {
  const [open, setOpen] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  const card = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const id = useId();
  useLayoutEffect(() => {
    if (!open || !btn.current) return;
    const place = () => {
      const r = btn.current!.getBoundingClientRect();
      const w = card.current?.offsetWidth ?? 360;
      const rtl = isRtl(btn.current!);
      // aligned to the button's end edge, kept inside the viewport
      const left = rtl ? r.left : r.right - w;
      setPos({
        top: r.bottom + 8,
        left: Math.max(8, Math.min(left, window.innerWidth - w - 8)),
      });
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!card.current?.contains(t) && !btn.current?.contains(t)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false);
        btn.current?.focus();
      }
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);
  return (
    <>
      <button
        ref={btn}
        type="button"
        aria-label={label}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((o) => !o)}
        className={cx(
          'inline-flex h-[34px] items-center gap-2 whitespace-nowrap rounded-full bg-chip px-3.5 text-[13.5px] font-medium text-ink transition-colors hover:bg-surface-hover',
          className,
        )}
      >
        {button}
      </button>
      {open &&
        createPortal(
          <div
            ref={card}
            id={id}
            data-testid={testId}
            className={cx(
              'fixed z-[65] w-[min(360px,90vw)] rounded-[20px] bg-surface p-4 text-sm text-ink shadow-[0_18px_60px_rgba(0,0,0,0.18),0_0_0_1px_var(--border)] motion-safe:animate-[nm-pop-in_.3s_cubic-bezier(.32,.72,0,1)]',
              !pos && 'invisible',
            )}
            style={pos ?? undefined}
          >
            {children}
          </div>,
          document.body,
        )}
    </>
  );
}
