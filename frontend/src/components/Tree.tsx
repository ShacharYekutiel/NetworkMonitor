import { useMemo, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { TreeDevice, TreeGroup, TreeSensor } from '../api/types';
import { useTree } from '../hooks/queries';
import { useLocalState } from '../hooks/useLocalState';
import { formatValue } from '../lib/format';
import { CheckIcon, ChevronIcon, FolderIcon, SearchIcon, ServerIcon, WrenchIcon } from './icons';
import { StatusDot } from './Status';
import { cx, ErrorBox, Input, Spinner, Value } from './ui';

type Expanded = Record<string, boolean>;

const rowBase =
  'group flex min-w-0 items-center gap-1.5 rounded px-1.5 py-1 text-sm hover:bg-slate-200/70 dark:hover:bg-slate-800';
const activeRow = 'bg-sky-100 text-sky-900 dark:bg-sky-900/40 dark:text-sky-100';

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
        'flex h-4 w-4 shrink-0 items-center justify-center rounded text-slate-500 hover:text-slate-900 dark:hover:text-white',
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

function SensorRow({ s, depth }: { s: TreeSensor; depth: number }) {
  const { t } = useTranslation();
  return (
    <NavLink
      to={`/sensors/${s.id}`}
      className={({ isActive }) => cx(rowBase, isActive && activeRow)}
      style={{ paddingInlineStart: `${depth * 14 + 26}px` }}
      title={s.status_message || s.name}
    >
      <StatusDot status={s.status} />
      <span className="min-w-0 flex-1 truncate">{s.name}</span>
      {s.acknowledged && (
        <CheckIcon
          size={12}
          className="shrink-0 text-emerald-600 dark:text-emerald-400"
          aria-label={t('ack.acknowledged')}
          aria-hidden={false}
        />
      )}
      <span className="shrink-0 text-xs tabular-nums text-slate-500">
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
  return (
    <div>
      <div className="flex items-center" style={{ paddingInlineStart: `${depth * 14}px` }}>
        <Toggle open={open} onClick={() => toggle(key)} hidden={!d.sensors.length} />
        <NavLink
          to={`/devices/${d.id}`}
          className={({ isActive }) => cx(rowBase, 'flex-1', isActive && activeRow)}
          title={d.host}
        >
          <StatusDot status={d.status} />
          <ServerIcon size={14} className="shrink-0 text-slate-500" />
          <span className="min-w-0 flex-1 truncate">{d.name}</span>
          {d.in_maintenance && (
            <WrenchIcon
              size={12}
              className="shrink-0 text-sky-600 dark:text-sky-400"
              aria-label={t('maintenance.badge')}
              aria-hidden={false}
            />
          )}
        </NavLink>
      </div>
      {open && d.sensors.map((s) => <SensorRow key={s.id} s={s} depth={depth} />)}
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
  return (
    <div>
      <div className="flex items-center" style={{ paddingInlineStart: `${depth * 14}px` }}>
        <Toggle open={open} onClick={() => toggle(key)} hidden={!hasChildren} />
        <NavLink
          to={`/groups/${g.id}`}
          className={({ isActive }) => cx(rowBase, 'flex-1 font-medium', isActive && activeRow)}
        >
          <StatusDot status={g.status} />
          <FolderIcon size={14} className="shrink-0 text-slate-500" />
          <span className="min-w-0 flex-1 truncate">{g.name}</span>
          <span className="shrink-0 text-xs tabular-nums text-slate-400">
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

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="relative px-2 pb-2">
        <SearchIcon
          size={14}
          className="pointer-events-none absolute start-4 top-2.5 text-slate-400"
        />
        <Input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t('tree.search')}
          className="ps-7"
          aria-label={t('tree.search')}
        />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-1 pb-4">
        {isLoading && (
          <div className="flex justify-center p-4 text-slate-400">
            <Spinner />
          </div>
        )}
        {error && <ErrorBox error={error} className="m-2" />}
        {data && !groups.length && (
          <div className="p-3 text-xs text-slate-500">
            {query ? t('tree.noMatch') : t('tree.empty')}
          </div>
        )}
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
      </div>
    </div>
  );
}
