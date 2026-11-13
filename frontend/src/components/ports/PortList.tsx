import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { Port } from '../../api/types';
import { formatDateTime, formatRelative } from '../../lib/format';
import {
  COLUMN_HIDE_CLASS,
  connectedHostView,
  connectedView,
  errorsDelta,
  filterPorts,
  loadColumns,
  PHYSICAL_KINDS,
  PORT_COLUMNS,
  saveColumns,
  tileView,
  toggleColumn,
  type PortColumn,
} from '../../lib/ports';
import { AlertIcon, ColumnsIcon, SearchIcon } from '../icons';
import { Button, Checkbox, cx, Empty, Input } from '../ui';
import { TileGlyph } from './Faceplate';
import { usePortText } from './text';
import { lagMemberNames } from '../../lib/lag';

const thBase =
  'whitespace-nowrap border-b border-line px-3 py-2 text-start text-xs font-medium text-ink-muted';
const tdBase = 'whitespace-nowrap border-b border-line px-3 py-1.5 align-middle';
/** The sticky Port column needs an opaque background (rows scroll under it). */
const stickyCell = 'sticky start-0 z-10 bg-surface';

function ColumnChooser({
  columns,
  onChange,
}: {
  columns: PortColumn[];
  onChange: (c: PortColumn[]) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const k = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('mousedown', h);
    document.addEventListener('keydown', k);
    return () => {
      document.removeEventListener('mousedown', h);
      document.removeEventListener('keydown', k);
    };
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <Button
        size="sm"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-haspopup="true"
      >
        <ColumnsIcon size={14} /> {t('ports.columns')}
      </Button>
      {open && (
        <div
          className="absolute end-0 z-30 mt-1 w-56 rounded-md bg-surface p-2 shadow-pop ring-1 ring-line"
          data-testid="column-chooser"
        >
          <div className="grid grid-cols-1 gap-1">
            {PORT_COLUMNS.map((c) => (
              <Checkbox
                key={c}
                label={t(`ports.col.${c}`)}
                checked={columns.includes(c)}
                onChange={(v) => onChange(toggleColumn(columns, c, v))}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** [1.3] Name/neighbour cell: LLDP neighbour, else the first connected host (+N more). */
function ConnNameCell({ p, showIp }: { p: Port; showIp: boolean }) {
  const { t } = useTranslation();
  const v = connectedHostView(p);
  const link = (id: number, text: string) => (
    <Link
      to={`/devices/${id}`}
      onClick={(e) => e.stopPropagation()}
      className="text-sky-700 hover:underline dark:text-sky-400"
    >
      <bdi dir="auto">{text}</bdi>
    </Link>
  );
  // On narrow screens the IP column is hidden: show the IP under the name instead.
  const ipLine = (ip: string | null) =>
    ip ? (
      <div className={cx('text-xs text-slate-500', showIp && 'sm:hidden')}>
        <bdi dir="ltr">{ip}</bdi>
      </div>
    ) : null;
  if (v.kind === 'neighbor')
    return (
      <div className="max-w-[16rem]" data-testid="port-conn-name">
        <span className="inline-flex items-center gap-1">
          <span
            className="rounded bg-slate-100 px-1 text-[10px] font-semibold text-slate-600 dark:bg-slate-800 dark:text-slate-300"
            title={t('ports.conn.neighborTitle')}
          >
            {t('ports.conn.neighbor')}
          </span>
          {v.device_id ? link(v.device_id, v.name) : <bdi dir="auto">{v.name}</bdi>}
        </span>
        {ipLine(v.ip)}
      </div>
    );
  if (v.kind === 'uplink')
    return <span className="text-slate-500">{t('ports.macCount', { count: v.count })}</span>;
  if (v.kind === 'none') return <span className="text-slate-400">—</span>;
  const label = v.device_id ? (
    link(v.device_id, v.device_name ?? v.hostname ?? v.ip ?? v.mac)
  ) : v.hostname ? (
    <bdi dir="ltr">{v.hostname}</bdi>
  ) : v.vendor ? (
    <bdi dir="auto" className="text-slate-500">
      {v.vendor}
    </bdi>
  ) : (
    <bdi dir="ltr" className="font-mono text-xs">
      {v.mac}
    </bdi>
  );
  return (
    <div className="max-w-[16rem]" data-testid="port-conn-name">
      <span className="inline-flex min-w-0 items-center gap-1">
        <span className="truncate">{label}</span>
        {v.more > 0 && (
          <span
            className="shrink-0 rounded bg-slate-100 px-1 text-[10px] font-semibold text-slate-600 dark:bg-slate-800 dark:text-slate-300"
            title={t('ports.conn.moreTitle', { count: v.more })}
            data-testid="port-conn-more"
          >
            +{v.more}
          </span>
        )}
      </span>
      {ipLine(v.ip)}
    </div>
  );
}

export function PortList({
  ports,
  selected,
  onSelect,
}: {
  ports: Port[];
  selected: number | null;
  onSelect: (p: Port) => void;
}) {
  const { t } = useTranslation();
  const text = usePortText();
  const [query, setQuery] = useState('');
  const [physicalOnly, setPhysicalOnly] = useState(true);
  const [columns, setColumnsState] = useState<PortColumn[]>(() => loadColumns());
  const setColumns = (c: PortColumn[]) => {
    setColumnsState(c);
    saveColumns(c);
  };

  const rows = useMemo(() => {
    const base = physicalOnly ? ports.filter((p) => PHYSICAL_KINDS.includes(p.kind)) : ports;
    return filterPorts(base, query, text.searchTexts);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ports, query, physicalOnly, t]);

  const cell = (p: Port, c: PortColumn): ReactNode => {
    switch (c) {
      case 'status': {
        const v = tileView(p);
        return (
          <span className="inline-flex items-center gap-1.5">
            <TileGlyph kind={v.kind} icon={null} className="!h-3 !w-3 !rounded-sm" />
            {text.state(p)}
            {p.alert && (
              <span
                className="inline-flex items-center gap-0.5 text-status-warning"
                title={p.alert_reasons.map((r) => t(`ports.alertReason.${r}`)).join(', ')}
              >
                <AlertIcon size={13} />
                <span className="sr-only">{t('ports.alert')}</span>
              </span>
            )}
          </span>
        );
      }
      case 'connected': {
        const v = connectedView(p);
        if (v.kind === 'neighbor')
          return v.device_id ? (
            <Link
              to={`/devices/${v.device_id}`}
              onClick={(e) => e.stopPropagation()}
              className="text-accent-ink hover:underline"
            >
              <bdi dir="auto">{v.name}</bdi>
            </Link>
          ) : (
            <bdi dir="auto">{v.name}</bdi>
          );
        if (v.kind === 'uplinkMacs')
          return <span className="text-ink-muted">{t('ports.macCount', { count: v.count })}</span>;
        if (v.kind === 'mac')
          return (
            <span className="inline-flex max-w-[16rem] items-center gap-1">
              <bdi dir="ltr" className="truncate">
                {v.text}
              </bdi>
              {v.more > 0 && <span className="text-xs text-ink-muted">+{v.more}</span>}
            </span>
          );
        return <span className="text-ink-subtle">—</span>;
      }
      case 'connIp': {
        const v = connectedHostView(p);
        const ip = v.kind === 'neighbor' || v.kind === 'host' ? v.ip : null;
        return ip ? (
          <bdi dir="ltr" data-testid="port-conn-ip">
            {ip}
          </bdi>
        ) : (
          <span className="text-slate-400">—</span>
        );
      }
      case 'connName':
        return <ConnNameCell p={p} showIp={columns.includes('connIp')} />;
      case 'vlan':
        return (
          <span>
            {p.pvid ?? '—'}
            {p.vlans_from_lag && (
              <span className="ms-1 text-[10px] text-ink-muted" title={t('ports.lag.vlansFromLag')}>
                ({t('ports.lag.viaLag')})
              </span>
            )}
          </span>
        );
      case 'mode':
        return text.mode(p);
      case 'speed':
        return <bdi dir="ltr">{text.speed(p)}</bdi>;
      case 'poe':
        return <bdi dir="auto">{text.poe(p)}</bdi>;
      case 'rxtx':
        return <bdi dir="ltr">{text.rxtx(p)}</bdi>;
      case 'alias':
        return <bdi dir="auto">{p.alias || '—'}</bdi>;
      case 'duplex':
        return p.duplex ? t(`ports.duplex.${p.duplex}`) : '—';
      case 'mac':
        return (
          <bdi dir="ltr" className="font-mono text-xs">
            {p.first_mac?.mac ?? '—'}
          </bdi>
        );
      case 'ip':
        return <bdi dir="ltr">{p.first_mac?.ip ?? '—'}</bdi>;
      case 'hostname':
        return <bdi dir="ltr">{p.first_mac?.hostname ?? '—'}</bdi>;
      case 'vendor':
        return <bdi dir="auto">{p.first_mac?.vendor ?? '—'}</bdi>;
      case 'errors': {
        const e = errorsDelta(p);
        return e === null ? (
          '—'
        ) : (
          <span className={e > 0 ? 'text-status-warning' : undefined}>{e}</span>
        );
      }
      case 'lastChange':
        return p.last_change_at ? (
          <span title={formatDateTime(p.last_change_at)}>{formatRelative(p.last_change_at)}</span>
        ) : (
          '—'
        );
      case 'ifIndex':
        return p.if_index;
      case 'kind':
        return t(`ports.kind.${p.kind}`);
    }
  };

  return (
    <div className="space-y-2" data-testid="port-list">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-0 flex-1 basis-56">
          <SearchIcon
            size={14}
            className="pointer-events-none absolute start-2.5 top-1/2 -translate-y-1/2 text-ink-subtle"
          />
          <Input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('ports.filterPlaceholder')}
            aria-label={t('ports.filter')}
            dir={query ? 'auto' : undefined}
            className="ps-8"
          />
        </div>
        <Checkbox
          label={t('ports.physicalOnly')}
          checked={physicalOnly}
          onChange={setPhysicalOnly}
        />
        <ColumnChooser columns={columns} onChange={setColumns} />
      </div>
      <div className="text-xs text-ink-muted">
        {t('ports.shown', { shown: rows.length, total: ports.length })}
      </div>
      {rows.length === 0 ? (
        <Empty>{t('ports.noMatch')}</Empty>
      ) : (
        <div className="overflow-x-auto" data-testid="port-list-scroll">
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className={cx(thBase, stickyCell, 'z-20')}>{t('ports.col.port')}</th>
                {columns.map((c) => (
                  <th key={c} className={cx(thBase, COLUMN_HIDE_CLASS[c])}>
                    {t(`ports.col.${c}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => {
                const sel = selected === p.if_index;
                return (
                  <tr
                    key={p.if_index}
                    onClick={() => onSelect(p)}
                    className={cx(
                      'group cursor-pointer',
                      sel ? 'bg-accent-soft' : 'hover:bg-surface-hover',
                    )}
                  >
                    <td
                      className={cx(
                        tdBase,
                        stickyCell,
                        'font-medium',
                        sel ? '!bg-accent-soft' : 'group-hover:bg-surface-hover',
                      )}
                    >
                      <button
                        type="button"
                        className="text-start text-accent-ink hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                        onClick={(e) => {
                          e.stopPropagation();
                          onSelect(p);
                        }}
                      >
                        <bdi dir="ltr">{p.name || p.descr || p.if_index}</bdi>
                      </button>
                      {p.lag && (
                        <span
                          className={cx(
                            'ms-1.5 rounded px-1 text-[10px] font-semibold',
                            p.lag.attached === false
                              ? 'bg-surface-sunken text-ink-muted line-through'
                              : 'bg-accent-soft text-accent-ink',
                          )}
                          title={
                            p.lag.attached === false
                              ? t('ports.lag.notActive', { name: p.lag.name })
                              : t('ports.lag.memberOf', { name: p.lag.name })
                          }
                          data-testid="lag-chip"
                        >
                          <bdi dir="ltr">{p.lag.name}</bdi>
                        </span>
                      )}
                      {p.kind === 'lag' && !!p.lag_members?.length && (
                        <div
                          className="text-xs font-normal text-ink-muted"
                          data-testid="lag-members"
                        >
                          {t('ports.lag.members', { list: lagMemberNames(p, ports).join(', ') })}
                        </div>
                      )}
                      {p.alias && !columns.includes('alias') && (
                        <div className="max-w-[10rem] truncate text-xs font-normal text-ink-muted">
                          <bdi dir="auto">{p.alias}</bdi>
                        </div>
                      )}
                    </td>
                    {columns.map((c) => (
                      <td key={c} className={cx(tdBase, 'tabular-nums', COLUMN_HIDE_CLASS[c])}>
                        {cell(p, c)}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
