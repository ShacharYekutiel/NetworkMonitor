import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { NavLink } from 'react-router';
import { useTranslation } from 'react-i18next';
import type { TreeDevice, TreeGroup, TreeSensor } from '../api/types';
import { useTree } from '../hooks/queries';
import { useLocalState } from '../hooks/useLocalState';
import { formatValue } from '../lib/format';
import {
  ancestors,
  AUTO_EXPAND_MS,
  dragItems,
  indexForMove,
  rangeKeys,
  refKey,
  selectedRefs,
  toggleKey,
  visibleOrder,
  type MoveRef,
} from '../lib/treeMove';
import {
  CheckIcon,
  ChevronIcon,
  FolderIcon,
  MoveIcon,
  SearchIcon,
  ServerIcon,
  WrenchIcon,
  XIcon,
} from './icons';
import { StatusDot } from './Status';
import { TreeMenuProvider, useTreeItemMenu, useTreeMenu } from './treemenu/TreeContextMenu';
import { useTreeMove } from './treemenu/TreeMove';
import { Button, cx, ErrorBox, Input, Spinner, Value } from './ui';
import { useLocalizeMessage } from '../hooks/localize';

type Expanded = Record<string, boolean>;

const rowBase =
  'group flex h-8 min-w-0 items-center gap-2 whitespace-nowrap rounded-[10px] px-2 text-[14px] text-ink hover:bg-surface-hover';
// The selected node (premium menu): the nav-active ground and semibold ink.
const activeRow = 'bg-nav-active font-semibold';
/** [3b] no iOS link callout on long press (it opens the tree context menu instead) */
const noCallout = { WebkitTouchCallout: 'none' } as const;

// [tree-move] a selected row, a row being dragged, and a group row under the drag (a target that
// accepts the drop is outlined; one that does not is dimmed and the pointer shows "no drop")
const moveRow =
  'data-[selected]:bg-accent-soft data-[selected]:shadow-[inset_0_0_0_1px_var(--accent-ink)] data-[dragging]:opacity-50';
const dropRow =
  'rounded-[10px] data-[drop=ok]:bg-accent-soft data-[drop=ok]:shadow-[inset_0_0_0_2px_var(--accent-ink)] data-[drop=no]:cursor-no-drop data-[drop=no]:opacity-50';

/** What the rows need from the tree: the visible row order (Shift-click) and expanding a group. */
type RowCtx = { order: string[]; expand: (key: string) => void };
const TreeRows = createContext<RowCtx>({ order: [], expand: () => undefined });

type Handlers = Record<string, unknown> & {
  onClick?: (e: React.MouseEvent<HTMLElement>) => void;
  onKeyDown?: (e: React.KeyboardEvent<HTMLElement>) => void;
};

/** The menu's handlers and the move handlers on one element (both run; the move's first). */
function merge(menu: Handlers, move: Handlers): Handlers {
  return {
    ...menu,
    ...move,
    onClick: (e: React.MouseEvent<HTMLElement>) => {
      move.onClick?.(e);
      menu.onClick?.(e);
    },
    onKeyDown: (e: React.KeyboardEvent<HTMLElement>) => {
      move.onKeyDown?.(e);
      if (!e.defaultPrevented) menu.onKeyDown?.(e);
    },
  };
}

/**
 * [tree-move] A group / device row as a drag source and a selectable item (admins only):
 * Ctrl/Cmd-click toggles it, Shift-click selects the visible rows from the last clicked one,
 * Ctrl/Cmd+Space toggles the focused row; a plain click clears the selection and opens the item.
 */
function useMoveSource(ref: MoveRef): Handlers {
  const mover = useTreeMove();
  const { order } = useContext(TreeRows);
  if (!mover?.canMove) return {};
  const key = refKey(ref);
  const sel = mover.selection;
  const dragging = !!mover.drag?.refs.some((r) => refKey(r) === key);
  return {
    draggable: true,
    'data-selected': sel.has(key) || undefined,
    'data-dragging': dragging || undefined,
    'aria-keyshortcuts': 'Shift+F10 Control+Space',
    onDragStart: (e: React.DragEvent<HTMLElement>) => mover.beginDrag(dragItems(sel, ref), e),
    onDragEnd: () => mover.endDrag(),
    onMouseDown: (e: React.MouseEvent<HTMLElement>) => {
      if (e.shiftKey) e.preventDefault(); // no text selection on Shift-click
    },
    onClick: (e: React.MouseEvent<HTMLElement>) => {
      if (e.button !== 0) return;
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        mover.setSelection(toggleKey(sel, key), key);
      } else if (e.shiftKey) {
        e.preventDefault();
        mover.setSelection(new Set(rangeKeys(order, mover.anchor, key)), mover.anchor ?? key);
      } else if (sel.size || mover.anchor !== key) {
        mover.setSelection(new Set(), key);
      }
    },
    onKeyDown: (e: React.KeyboardEvent<HTMLElement>) => {
      if (e.key === ' ' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        mover.setSelection(toggleKey(sel, key), key);
      }
    },
  };
}

/**
 * [tree-move] A group row (or the top-level zone: `groupId` null) as a drop target. Hovering a
 * collapsed group for a moment during a drag opens it.
 */
function useDropTarget(key: string, groupId: number | null, collapsed: boolean): Handlers {
  const mover = useTreeMove();
  const { expand } = useContext(TreeRows);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const active = !!mover?.drag;
  useEffect(() => {
    if (!active && timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  }, [active]);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  if (!mover?.canMove) return {};
  const stop = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };
  const over = (e: React.DragEvent<HTMLElement>) => {
    if (!mover.drag) return; // another kind of drag (a file, a link from elsewhere)
    if (mover.dragOver(key, groupId) === 'ok') {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
    } else {
      e.dataTransfer.dropEffect = 'none';
    }
  };
  return {
    'data-drop-target': key,
    'data-drop': mover.drag && mover.drag.over === key ? (mover.drag.ok ? 'ok' : 'no') : undefined,
    onDragEnter: (e: React.DragEvent<HTMLElement>) => {
      over(e);
      if (mover.drag && collapsed && !timer.current)
        timer.current = setTimeout(() => {
          timer.current = null;
          expand(key);
        }, AUTO_EXPAND_MS);
    },
    onDragOver: over,
    onDragLeave: (e: React.DragEvent<HTMLElement>) => {
      if (e.relatedTarget instanceof Node && e.currentTarget.contains(e.relatedTarget)) return;
      stop();
      mover.dragLeave(key);
    },
    onDrop: (e: React.DragEvent<HTMLElement>) => {
      if (!mover.drag) return;
      e.preventDefault();
      stop();
      mover.drop(groupId);
    },
  };
}

function Toggle({
  open,
  onClick,
  hidden,
}: {
  open: boolean;
  onClick: () => void;
  hidden?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={open ? t('tree.collapse') : t('tree.expand')}
      aria-expanded={open}
      className={cx(
        'flex h-4 w-4 shrink-0 items-center justify-center rounded-sm text-ink-subtle hover:text-ink',
        hidden && 'invisible',
      )}
    >
      <ChevronIcon
        size={12}
        className={cx('transition-transform', open ? 'rotate-90' : 'rtl:rotate-180')}
      />
    </button>
  );
}

function matches(name: string, q: string) {
  return name.toLowerCase().includes(q);
}

function filterGroups(groups: TreeGroup[], q: string): TreeGroup[] {
  if (!q) return groups;
  const out: TreeGroup[] = [];
  for (const g of groups) {
    const children = filterGroups(g.children, q);
    const devices = g.devices
      .map((d) => {
        if (matches(d.name, q) || matches(d.host, q)) return d;
        const sensors = d.sensors.filter((s) => matches(s.name, q));
        return sensors.length ? { ...d, sensors } : null;
      })
      .filter((d): d is TreeDevice => d !== null);
    if (matches(g.name, q) || children.length || devices.length) {
      out.push({
        ...g,
        children,
        devices: matches(g.name, q) && !devices.length ? g.devices : devices,
      });
    }
  }
  return out;
}

function SensorRow({ s, d, depth }: { s: TreeSensor; d: TreeDevice; depth: number }) {
  const { t } = useTranslation();
  const menu = useTreeItemMenu({ kind: 'sensor', sensor: s, device: d });
  const localize = useLocalizeMessage();
  return (
    <NavLink
      {...menu}
      to={`/sensors/${s.id}`}
      data-tree-item="sensor"
      className={({ isActive }) => cx(rowBase, isActive && activeRow)}
      style={{ paddingInlineStart: `${depth * 14 + 26}px`, ...noCallout }}
      title={localize(s.status_message) || s.name}
    >
      <StatusDot status={s.status} />
      <span className="min-w-0 flex-1 truncate">{s.name}</span>
      {s.acknowledged && (
        <CheckIcon
          size={12}
          className="shrink-0 text-ack"
          aria-label={t('ack.acknowledged')}
          aria-hidden={false}
        />
      )}
      <span className="shrink-0 font-mono text-[11.5px] tabular-nums text-ink-subtle">
        <Value text={formatValue(s.primary_value, s.primary_unit)} />
      </span>
    </NavLink>
  );
}

function DeviceRow({
  d,
  depth,
  expanded,
  toggle,
  forceOpen,
}: {
  d: TreeDevice;
  depth: number;
  expanded: Expanded;
  toggle: (k: string) => void;
  forceOpen: boolean;
}) {
  const { t } = useTranslation();
  const key = `d${d.id}`;
  const open = forceOpen || !!expanded[key];
  const menu = useTreeItemMenu({ kind: 'device', device: d });
  const move = useMoveSource({ kind: 'device', id: d.id });
  return (
    <div>
      <div className="flex items-center" style={{ paddingInlineStart: `${depth * 14}px` }}>
        <Toggle open={open} onClick={() => toggle(key)} hidden={!d.sensors.length} />
        <NavLink
          {...merge(menu, move)}
          to={`/devices/${d.id}`}
          data-tree-item="device"
          className={({ isActive }) => cx(rowBase, 'flex-1', isActive && activeRow, moveRow)}
          style={noCallout}
          title={d.host}
        >
          <StatusDot status={d.status} />
          <ServerIcon size={14} strokeWidth={1.75} className="shrink-0 text-ink-subtle" />
          <span className="min-w-0 flex-1 truncate">{d.name}</span>
          {d.in_maintenance && (
            <WrenchIcon
              size={12}
              className="shrink-0 text-accent-ink"
              aria-label={t('maintenance.badge')}
              aria-hidden={false}
            />
          )}
        </NavLink>
      </div>
      {open && d.sensors.map((s) => <SensorRow key={s.id} s={s} d={d} depth={depth} />)}
    </div>
  );
}

function GroupRow({
  g,
  depth,
  expanded,
  toggle,
  forceOpen,
}: {
  g: TreeGroup;
  depth: number;
  expanded: Expanded;
  toggle: (k: string) => void;
  forceOpen: boolean;
}) {
  const key = `g${g.id}`;
  // Groups are expanded by default; the map records explicit collapses.
  const open = forceOpen || expanded[key] !== false;
  const hasChildren = g.children.length > 0 || g.devices.length > 0;
  const menu = useTreeItemMenu({ kind: 'group', group: g });
  const move = useMoveSource({ kind: 'group', id: g.id });
  const drop = useDropTarget(key, g.id, hasChildren && !open);
  return (
    <div>
      <div
        {...drop}
        className={cx('flex items-center', dropRow)}
        style={{ paddingInlineStart: `${depth * 14}px` }}
      >
        <Toggle open={open} onClick={() => toggle(key)} hidden={!hasChildren} />
        <NavLink
          {...merge(menu, move)}
          to={`/groups/${g.id}`}
          data-tree-item="group"
          className={({ isActive }) =>
            cx(rowBase, 'flex-1 font-medium', isActive && activeRow, moveRow)
          }
          style={noCallout}
        >
          <StatusDot status={g.status} />
          <FolderIcon size={14} strokeWidth={1.75} className="shrink-0 text-ink-subtle" />
          <span className="min-w-0 flex-1 truncate">{g.name}</span>
          <span className="shrink-0 font-mono text-[11.5px] font-normal tabular-nums text-ink-subtle">
            {g.status_counts.total}
          </span>
        </NavLink>
      </div>
      {open && (
        <>
          {g.children.map((c) => (
            <GroupRow
              key={c.id}
              g={c}
              depth={depth + 1}
              expanded={expanded}
              toggle={toggle}
              forceOpen={forceOpen}
            />
          ))}
          {g.devices.map((d) => (
            <DeviceRow
              key={d.id}
              d={d}
              depth={depth + 1}
              expanded={expanded}
              toggle={toggle}
              forceOpen={forceOpen}
            />
          ))}
        </>
      )}
    </div>
  );
}

export function TreeNav() {
  // The shell provides the menu for every screen; a tree rendered on its own brings its own.
  if (useTreeMenu()) return <TreeNavInner />;
  return (
    <TreeMenuProvider>
      <TreeNavInner />
    </TreeMenuProvider>
  );
}

function TreeNavInner() {
  const { t } = useTranslation();
  const { data, isLoading, error } = useTree();
  const [expanded, setExpanded] = useLocalState<Expanded>('netmon.treeExpanded', {});
  const [q, setQ] = useState('');
  const query = q.trim().toLowerCase();
  const groups = useMemo(() => filterGroups(data?.groups ?? [], query), [data, query]);

  const toggle = (k: string) =>
    setExpanded((prev) => {
      const isGroup = k.startsWith('g');
      const open = isGroup ? prev[k] !== false : !!prev[k];
      return { ...prev, [k]: !open };
    });

  // [tree-move] rows in display order (Shift-click ranges), auto-expand while dragging, and
  // after a move the target group and its ancestors open so the moved items stay in view
  const mover = useTreeMove();
  const rows = useMemo<RowCtx>(
    () => ({
      order: visibleOrder(groups, (k) => !!query || expanded[k] !== false),
      expand: (k) => setExpanded((prev) => (prev[k] === false ? { ...prev, [k]: true } : prev)),
    }),
    [groups, query, expanded, setExpanded],
  );
  const setRevealer = mover?.setRevealer;
  useEffect(() => {
    if (!setRevealer || !data) return;
    const ix = indexForMove(data.groups);
    setRevealer((ids) =>
      setExpanded((prev) => {
        const next = { ...prev };
        for (const id of ids) for (const a of ancestors(ix, id)) delete next[`g${a}`];
        return next;
      }),
    );
    return () => setRevealer(null);
  }, [setRevealer, data, setExpanded]);
  const sel = mover?.canMove ? mover.selection : null;
  const dragGroupsOnly = !!mover?.drag && mover.drag.refs.every((r) => r.kind === 'group');

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="relative px-3 pb-2 pt-3">
        <SearchIcon
          size={15}
          className="pointer-events-none absolute start-[24px] top-[24px] text-ink-subtle"
        />
        <Input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t('tree.search')}
          className="h-9 ps-9 text-sm"
          aria-label={t('tree.search')}
        />
      </div>
      {sel && sel.size > 0 && mover && (
        <SelectionBar
          count={sel.size}
          onMove={() => mover.openPicker(selectedRefs(sel))}
          onClear={mover.clearSelection}
        />
      )}
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {isLoading && (
          <div className="flex justify-center p-4 text-ink-subtle">
            <Spinner />
          </div>
        )}
        {error && <ErrorBox error={error} className="m-2" />}
        {data && !groups.length && (
          <div className="p-3 text-xs text-ink-muted">
            {query ? t('tree.noMatch') : t('tree.empty')}
          </div>
        )}
        <TreeRows.Provider value={rows}>
          {groups.map((g) => (
            <GroupRow
              key={g.id}
              g={g}
              depth={0}
              expanded={expanded}
              toggle={toggle}
              forceOpen={!!query}
            />
          ))}
          {dragGroupsOnly && <TopLevelZone />}
        </TreeRows.Provider>
      </div>
    </div>
  );
}

/** [tree-move] "N selected · Move to… · ×" above the tree while rows are selected. */
function SelectionBar({
  count,
  onMove,
  onClear,
}: {
  count: number;
  onMove: () => void;
  onClear: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      role="toolbar"
      aria-label={t('treeMove.selection')}
      data-testid="tree-selection-bar"
      className="mx-3 mb-2 flex items-center gap-2 rounded-xl bg-accent-soft px-3 py-1.5 text-[13px]"
    >
      <span
        className="min-w-0 flex-1 truncate font-medium"
        aria-live="polite"
        title={t('treeMove.selectionHint')}
      >
        {t('treeMove.selected', { count })}
      </span>
      <Button size="sm" onClick={onMove} data-testid="tree-selection-move">
        <MoveIcon size={13} className="rtl:-scale-x-100" /> {t('treeMenu.action.moveTo')}
      </Button>
      <button
        type="button"
        onClick={onClear}
        aria-label={t('treeMove.clearSelection')}
        title={t('treeMove.clearSelection')}
        className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-ink-muted hover:bg-chip hover:text-ink"
      >
        <XIcon size={14} />
      </button>
    </div>
  );
}

/** [tree-move] The drop zone for the top level (shown while groups alone are dragged). */
function TopLevelZone() {
  const { t } = useTranslation();
  const drop = useDropTarget('root', null, false);
  return (
    <div
      {...drop}
      data-testid="tree-drop-root"
      className={cx(
        'mt-2 flex h-10 items-center justify-center border border-dashed border-line px-2 text-center text-[12.5px] text-ink-muted',
        dropRow,
      )}
    >
      {t('treeMove.dropTopLevel')}
    </div>
  );
}
