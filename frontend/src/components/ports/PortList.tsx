import { useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import type { Port } from '../../api/types';
import { formatDateTime, formatRelative } from '../../lib/format';
import {
  COLUMN_HIDE_CLASS,
  DEFAULT_COLUMNS,
  connectedHostView,
  connectedView,
  portDeviceKind,
  errorsDelta,
  filterPorts,
  formatSpeed,
  loadColumns,
  PHYSICAL_KINDS,
  PORT_COLUMNS,
  saveColumns,
  toggleColumn,
  type PortColumn,
} from '../../lib/ports';
import { AlertIcon, PortIcon, SearchIcon } from '../icons';
import { EmptyState, FilterChips } from '../kit';
import {
  CELL_TRUNCATE,
  ColumnHeader,
  ColumnsMenu,
  ColumnTable,
  useColumnLayout,
  type ColumnSpec,
} from '../kitTable';
import { Checkbox, cx, Input, TAG, TAG_TONE } from '../ui';
import { KIND_ICONS } from './Faceplate';
import { usePortText } from './text';
import { lagGroups, lagMemberNames, portName } from '../../lib/lag';

/** Table cells (prototype .tbl): 12.5px grey headers, hairline rows, the outer cells on the card's padding. */
const thBase =
  'whitespace-nowrap px-3.5 pb-2.5 pt-3 text-start text-[12.5px] font-medium text-ink-muted first:ps-5 last:pe-5';
const tdBase =
  'whitespace-nowrap border-t border-line px-3.5 py-3 align-middle first:ps-5 last:pe-5';
/** The sticky Port column needs an opaque background (rows scroll under it). */
const stickyCell = 'sticky start-0 z-10 bg-surface';

/** Default widths of the port columns (px); the user's widths win. */
const PORT_COL_WIDTH: Partial<Record<PortColumn, number>> = {
  status: 130,
  connected: 190,
  connIp: 130,
  connName: 190,
  vlan: 80,
  mode: 100,
  speed: 100,
  poe: 110,
  rxtx: 150,
  alias: 160,
  duplex: 90,
  mac: 160,
  ip: 130,
  hostname: 160,
  vendor: 150,
  errors: 80,
  lastChange: 120,
  ifIndex: 80,
  kind: 110,
};

/** COLUMN_HIDE_CLASS for the <col> elements (a hidden cell's column must go too). */
const COL_HIDE_CLASS: Partial<Record<PortColumn, string>> = {
  connIp: 'hidden sm:table-column',
  poe: 'hidden md:table-column',
  rxtx: 'hidden md:table-column',
};

type StateFilter = 'all' | 'up' | 'problem' | 'free';
const STATE_FILTERS: StateFilter[] = ['all', 'up', 'problem', 'free'];
function stateMatch(p: Port, f: StateFilter): boolean {
  if (f === 'up') return p.state === 'up';
  if (f === 'problem') return p.alert;
  if (f === 'free') return p.state !== 'up' && !p.alert;
  return true;
}

/** Status dot of a row (prototype ports table): connected green, problem red, free grey. */
function stateDot(p: Port): string {
  if (p.alert) return 'bg-status-down';
  if (p.state === 'up') return 'bg-status-up';
  return 'bg-status-paused';
}

/** LAG summary chips above the table (prototype .lagchip): members, speed, degraded in warning. */
function LagChips({ ports }: { ports: Port[] }) {
  const { t } = useTranslation();
  const groups = lagGroups(ports);
  if (!groups.length) return null;
  const byIndex = new Map(ports.map((p) => [p.if_index, p]));
  return (
    <div className="flex flex-wrap gap-2" data-testid="lag-summary">
      {groups.map((g) => {
        const row = byIndex.get(g.if_index);
        const up = g.members.filter((m) => m.state === 'up' && m.lag?.attached !== false);
        const down = row ? row.state !== 'up' : up.length === 0;
        const degraded = !down && up.length < g.members.length;
        const bps =
          row?.state === 'up' && row.speed_bps
            ? row.speed_bps
            : up.reduce((a, m) => a + (m.speed_bps ?? 0), 0);
        const speed = formatSpeed(bps);
        return (
          <span
            key={g.if_index}
            data-testid="lag-summary-chip"
            data-state={down ? 'down' : degraded ? 'degraded' : 'up'}
            className="inline-flex h-8 max-w-full items-center gap-2 rounded-full bg-surface px-3 text-[13px] font-medium shadow-[0_1px_2px_rgba(0,0,0,0.06),0_0_0_1px_var(--border)]"
          >
            <i
              aria-hidden="true"
              className={cx(
                'h-[7px] w-[7px] shrink-0 rounded-full',
                down ? 'bg-status-down' : degraded ? 'bg-status-warning' : 'bg-status-up',
              )}
            />
            <b className="font-display font-semibold">
              <bdi dir="ltr">{g.name}</bdi>
            </b>
            <bdi dir="ltr" className="truncate font-display">
              {g.members.map((m) => portName(m)).join(' + ')}
              {speed && !down ? ` · ${speed}` : ''}
            </bdi>
            {(down || degraded) && (
              <span className={down ? 'text-status-down' : 'text-status-warning'}>
                · {down ? t('ports.lagState.down') : t('ports.lagState.degraded')}
              </span>
            )}
          </span>
        );
      })}
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
      className="text-accent-ink hover:underline"
    >
      <bdi dir="auto">{text}</bdi>
    </Link>
  );
  // On narrow screens the IP column is hidden: show the IP under the name instead.
  const ipLine = (ip: string | null) =>
    ip ? (
      <div className={cx('text-xs text-ink-muted', showIp && 'sm:hidden')}>
        <bdi dir="ltr">{ip}</bdi>
      </div>
    ) : null;
  if (v.kind === 'neighbor')
    return (
      <div className="max-w-[16rem]" data-testid="port-conn-name">
        <span className="inline-flex items-center gap-1">
          <span className={cx(TAG, TAG_TONE.neutral)} title={t('ports.conn.neighborTitle')}>
            {t('ports.conn.neighbor')}
          </span>
          {v.device_id ? link(v.device_id, v.name) : <bdi dir="auto">{v.name}</bdi>}
        </span>
        {ipLine(v.ip)}
      </div>
    );
  if (v.kind === 'uplink')
    return <span className="text-ink-muted">{t('ports.macCount', { count: v.count })}</span>;
  if (v.kind === 'none') return <span className="text-ink-subtle">—</span>;
  const label = v.device_id ? (
    link(v.device_id, v.device_name ?? v.hostname ?? v.ip ?? v.mac)
  ) : v.hostname ? (
    <bdi dir="ltr">{v.hostname}</bdi>
  ) : v.vendor ? (
    <bdi dir="auto" className="text-ink-muted">
      {v.vendor}
    </bdi>
  ) : (
    <bdi dir="ltr" className="font-mono text-[13px]">
      {v.mac}
    </bdi>
  );
  return (
    <div className="max-w-[16rem]" data-testid="port-conn-name">
      <span className="inline-flex min-w-0 items-center gap-1">
        <span className="truncate">{label}</span>
        {v.more > 0 && (
          <span
            className={cx(TAG, TAG_TONE.neutral, 'shrink-0')}
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
  const [stateFilter, setStateFilter] = useState<StateFilter>('all');
  const [columns, setColumnsState] = useState<PortColumn[]>(() => loadColumns());
  const setColumns = (c: PortColumn[]) => {
    setColumnsState(c);
    saveColumns(c);
  };
  // Order and widths (netmon.table.ports); which columns show stays in netmon.ports.columns.
  const specs = useMemo<ColumnSpec[]>(
    () => [
      { id: 'port', pin: 'start', width: 150, minWidth: 90 },
      ...PORT_COLUMNS.map((c) => ({ id: c, width: PORT_COL_WIDTH[c], minWidth: 64 })),
    ],
    [],
  );
  const layout = useColumnLayout('ports', specs);
  const shown = layout.columns.filter(
    (c) => c.id === 'port' || columns.includes(c.id as PortColumn),
  );

  const base = useMemo(
    () => (physicalOnly ? ports.filter((p) => PHYSICAL_KINDS.includes(p.kind)) : ports),
    [ports, physicalOnly],
  );
  const rows = useMemo(
    () => filterPorts(base, query, text.searchTexts).filter((p) => stateMatch(p, stateFilter)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [base, query, stateFilter, t],
  );
  const counts = useMemo(() => {
    const c: Record<StateFilter, number> = { all: 0, up: 0, problem: 0, free: 0 };
    for (const p of base) for (const f of STATE_FILTERS) if (stateMatch(p, f)) c[f] += 1;
    return c;
  }, [base]);

  const connectedCell = (p: Port): ReactNode => {
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
  };
  const cell = (p: Port, c: PortColumn): ReactNode => {
    switch (c) {
      case 'status': {
        return (
          <span className="inline-flex items-center gap-[7px] whitespace-nowrap">
            <span aria-hidden="true" className={cx('h-2 w-2 shrink-0 rounded-full', stateDot(p))} />
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
        // [premium] the device type icon in front, "manual" after a user's choice
        const k = p.state === 'up' ? portDeviceKind(p) : null;
        const Icon = k ? KIND_ICONS[k] : null;
        return (
          <span className="inline-flex max-w-full items-center gap-1.5">
            {Icon && (
              <span title={t(`ports.connectedKind.${k}`)} className="inline-flex shrink-0">
                <Icon size={15} className="text-ink-muted" />
              </span>
            )}
            {connectedCell(p)}
            {p.connected_kind_override && (
              <small className="shrink-0 text-[11.5px] text-ink-subtle">
                {t('ports.kindChooser.manual')}
              </small>
            )}
          </span>
        );
      }
      case 'connIp': {
        const v = connectedHostView(p);
        const ip = v.kind === 'neighbor' || v.kind === 'host' ? v.ip : null;
        return ip ? (
          <bdi dir="ltr" data-testid="port-conn-ip">
            {ip}
          </bdi>
        ) : (
          <span className="text-ink-subtle">—</span>
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
          <bdi dir="ltr" className="font-mono text-[13px]">
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
    <div className="grid gap-3" data-testid="port-list">
      {/* prototype .ptools: LAG chips, the state chips, the filter, columns */}
      <LagChips ports={ports} />
      <div className="flex flex-wrap items-center gap-2">
        <FilterChips
          label={t('ports.stateFilter')}
          value={stateFilter}
          onChange={setStateFilter}
          testId="port-state-filter"
          options={STATE_FILTERS.map((f) => ({
            value: f,
            label: (
              <>
                {t(`ports.stateChip.${f}`)}
                <span className="num text-[12px] opacity-70">{counts[f]}</span>
              </>
            ),
          }))}
        />
        <div className="relative min-w-0 flex-1 basis-56">
          <SearchIcon
            size={15}
            className="pointer-events-none absolute start-3 top-1/2 -translate-y-1/2 text-ink-subtle"
          />
          <Input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('ports.filterPlaceholder')}
            aria-label={t('ports.filter')}
            dir={query ? 'auto' : undefined}
            className="h-[34px] !rounded-full !py-1.5 ps-9 text-[13.5px]"
          />
        </div>
        <Checkbox
          label={t('ports.physicalOnly')}
          checked={physicalOnly}
          onChange={setPhysicalOnly}
        />
        <ColumnsMenu
          layout={layout}
          label={t('ports.columns')}
          testId="column-chooser"
          labelOf={(id) => t(`ports.col.${id}`)}
          visible={(id) => columns.includes(id as PortColumn)}
          onVisible={(id, on) => setColumns(toggleColumn(columns, id as PortColumn, on))}
          onReset={() => setColumns(DEFAULT_COLUMNS)}
        />
      </div>
      <div className="mx-1 text-[12.5px] text-ink-subtle">
        {t('ports.shown', { shown: rows.length, total: ports.length })}
      </div>
      <div className="min-w-0 overflow-hidden rounded-xl bg-surface shadow-card">
        {rows.length === 0 ? (
          <EmptyState icon={PortIcon} title={t('ports.noMatch')} className="!py-10" />
        ) : (
          <ColumnTable
            layout={layout}
            columns={shown}
            scrollTestId="port-list-scroll"
            colClass={(id) => COL_HIDE_CLASS[id as PortColumn]}
          >
            <thead>
              <tr>
                {shown.map((c) =>
                  c.id === 'port' ? (
                    <ColumnHeader
                      key={c.id}
                      layout={layout}
                      col={c}
                      label={t('ports.col.port')}
                      className={cx(thBase, 'sticky start-0 z-20 bg-surface')}
                    />
                  ) : (
                    <ColumnHeader
                      key={c.id}
                      layout={layout}
                      col={c}
                      label={t(`ports.col.${c.id}`)}
                      className={cx(thBase, COLUMN_HIDE_CLASS[c.id as PortColumn])}
                    />
                  ),
                )}
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
                      'group cursor-pointer transition-colors',
                      sel ? 'bg-chip' : 'hover:bg-surface-hover',
                    )}
                  >
                    <td
                      className={cx(
                        tdBase,
                        stickyCell,
                        sel ? '!bg-chip' : 'group-hover:bg-surface-hover',
                      )}
                    >
                      <button
                        type="button"
                        className="text-start font-display font-semibold text-ink hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
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
                            TAG,
                            'ms-1.5 h-5 text-[11px] font-medium',
                            p.lag.attached === false
                              ? cx(TAG_TONE.neutral, 'line-through')
                              : TAG_TONE.neutral,
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
                          {t('ports.lag.members', {
                            list: lagMemberNames(p, ports).join(', '),
                          })}
                        </div>
                      )}
                      {p.alias && !columns.includes('alias') && (
                        <div className="max-w-[10rem] truncate text-xs font-normal text-ink-muted">
                          <bdi dir="auto">{p.alias}</bdi>
                        </div>
                      )}
                    </td>
                    {shown.map((col) => {
                      if (col.id === 'port') return null;
                      const c = col.id as PortColumn;
                      return (
                        <td
                          key={c}
                          className={cx(
                            tdBase,
                            CELL_TRUNCATE,
                            'tabular-nums',
                            COLUMN_HIDE_CLASS[c],
                          )}
                        >
                          {cell(p, c)}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </ColumnTable>
        )}
      </div>
    </div>
  );
}
