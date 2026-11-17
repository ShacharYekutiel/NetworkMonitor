import { useEffect, useMemo, useRef, useState, type ComponentType } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import type { MapStatus, OverviewMap, OverviewMapNode } from '../../api/premium';
import { layoutMap, MAP_TILE, mapIcon, type MapIconKey } from '../../lib/homeMap';
import {
  BoltIcon,
  BranchIcon,
  DeviceIcon,
  FolderIcon,
  GlobeIcon,
  ServerStackIcon,
  ShieldIcon,
  ApIcon,
} from '../icons';
import { cx } from '../ui';

type IconC = ComponentType<{ size?: number; strokeWidth?: number; className?: string }>;
const ICON: Record<MapIconKey, IconC> = {
  internet: GlobeIcon,
  firewall: ShieldIcon,
  switch: DeviceIcon,
  server: ServerStackIcon,
  wifi: ApIcon,
  power: BoltIcon,
  branch: BranchIcon,
  group: FolderIcon,
};

/** Tile ring + soft halo + icon colour per state (prototype .hn.c-*). */
const TILE: Record<MapStatus, string> = {
  down: 'text-status-down shadow-[0_0_0_2px_var(--status-down),0_0_0_8px_color-mix(in_srgb,var(--status-down)_12%,transparent),var(--shadow-card)]',
  warning:
    'text-status-warning shadow-[0_0_0_2px_var(--status-warning),0_0_0_8px_color-mix(in_srgb,var(--status-warning)_12%,transparent),var(--shadow-card)]',
  unreachable:
    'text-status-unreachable shadow-[0_0_0_2px_var(--status-unreachable),var(--shadow-card)]',
  paused: 'text-status-paused shadow-[0_0_0_1px_var(--border-strong),var(--shadow-card)]',
  unknown:
    'text-ink shadow-[0_0_0_1px_color-mix(in_srgb,var(--ink)_7%,transparent),var(--shadow-card)]',
  up: 'text-ink shadow-[0_0_0_1px_color-mix(in_srgb,var(--ink)_7%,transparent),var(--shadow-card)]',
};
const DOT: Record<MapStatus, string> = {
  down: 'bg-status-down',
  warning: 'bg-status-warning',
  unreachable: 'bg-status-unreachable',
  paused: 'bg-status-paused',
  unknown: 'bg-status-unknown',
  up: 'bg-status-up',
};

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth);
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

function NodeTile({ n, x, y }: { n: OverviewMapNode; x: number; y: number }) {
  const { t } = useTranslation();
  const Icon = ICON[mapIcon(n)];
  const bad = n.status !== 'up' && n.status !== 'unknown' && !!n.problem;
  // The virtual root of the whole network has no name: the client labels it.
  const name = n.name || t('home.network');
  const latin = !/[\u0590-\u05FF]/.test(name);
  const body = (
    <>
      <span
        className={cx(
          'mb-[7px] grid h-16 w-16 place-items-center rounded-[20px] bg-surface transition-transform duration-[400ms] ease-premium group-hover:-translate-y-[3px]',
          TILE[n.status] ?? TILE.unknown,
        )}
      >
        <Icon size={28} strokeWidth={1.7} />
      </span>
      <b
        dir={latin ? 'ltr' : undefined}
        className="max-w-[150px] truncate text-sm font-semibold leading-5 [font-family:var(--font-display)]"
      >
        {name}
      </b>
      <small
        className={cx(
          'inline-flex max-w-[160px] items-center gap-[5px] truncate text-xs leading-4',
          bad ? 'text-ink' : 'text-ink-muted',
        )}
      >
        {bad && (
          <i
            aria-hidden="true"
            className={cx('h-1.5 w-1.5 shrink-0 rounded-full', DOT[n.status])}
          />
        )}
        <span className="truncate" dir="auto">
          {bad ? n.problem : n.subtitle}
        </span>
      </small>
    </>
  );
  const cls =
    'group absolute grid -translate-x-1/2 justify-items-center gap-[3px] text-ink no-underline focus-visible:outline-offset-4';
  const label = `${name}: ${(bad ? n.problem : n.subtitle) || ''} (${t(`status.${n.status}`)})`;
  const style = { left: x, top: y - MAP_TILE / 2 };
  if (n.ref)
    return (
      <Link
        to={n.ref.type === 'device' ? `/devices/${n.ref.id}` : `/groups/${n.ref.id}`}
        className={cls}
        style={style}
        aria-label={label}
        data-node={n.id}
      >
        {body}
      </Link>
    );
  return (
    <button
      type="button"
      className={cls}
      style={style}
      aria-label={label}
      data-node={n.id}
      onClick={() => document.getElementById('nm-problems')?.scrollIntoView({ behavior: 'smooth' })}
    >
      {body}
    </button>
  );
}

/**
 * The home hero (BUILD.md § 4): the network as a tree of quiet 64px tiles, problems in words with
 * a status ring, hairline links with slow green dots where healthy, red dashes where broken.
 */
export function HomeMap({ map, onSetup }: { map: OverviewMap; onSetup?: () => void }) {
  const { t } = useTranslation();
  const [ref, width] = useWidth<HTMLDivElement>();
  // Not set up yet (BUILD.md § 6b): only the main groups in a row, no lines, a quiet hint.
  const flat = !map.configured;
  const layout = useMemo(() => {
    const minWidth = Math.max(320, Math.min(width, 1040));
    if (!flat) return layoutMap(map.nodes, map.links, { minWidth });
    const parents = new Set(map.nodes.map((n) => n.parent_id));
    const leaves = map.nodes
      .filter((n) => !parents.has(n.id) && n.kind !== 'internet')
      .map((n) => ({ ...n, parent_id: null }));
    return layoutMap(leaves, [], { minWidth, top: 150 });
  }, [map, width, flat]);
  return (
    <div
      ref={ref}
      role="figure"
      aria-label={t('home.map.label')}
      data-testid="home-map"
      className="relative overflow-x-auto [scrollbar-width:thin]"
    >
      <div className="relative mx-auto" style={{ width: layout.width, height: layout.height }}>
        {flat && (
          <div className="absolute inset-x-0 top-2 mx-auto grid max-w-[460px] justify-items-center gap-2 text-center text-ink-muted">
            <b className="text-[17px] font-semibold text-ink">{t('home.map.notSet')}</b>
            <span className="text-[14.5px]">{t('home.map.notSetText')}</span>
            {onSetup && (
              <button
                type="button"
                onClick={onSetup}
                className="font-medium text-ink underline decoration-line-strong underline-offset-[3px]"
              >
                {t('home.map.setup')}
              </button>
            )}
          </div>
        )}
        <svg
          aria-hidden="true"
          className="absolute inset-0 overflow-visible"
          width={layout.width}
          height={layout.height}
        >
          {layout.edges.map((e, i) => (
            <g key={i}>
              <path
                d={e.d}
                fill="none"
                strokeWidth={e.status === 'up' || e.status === 'unknown' ? 1.5 : 1.75}
                strokeDasharray={e.status === 'down' || e.status === 'warning' ? '5 5' : undefined}
                className={cx(
                  e.status === 'down'
                    ? 'stroke-status-down opacity-80'
                    : e.status === 'warning'
                      ? 'stroke-status-warning opacity-80'
                      : 'stroke-[color-mix(in_srgb,var(--ink)_16%,transparent)]',
                )}
              />
              {e.status === 'up' && (
                <path
                  d={e.d}
                  fill="none"
                  strokeWidth={2}
                  strokeLinecap="round"
                  strokeDasharray="1.5 14"
                  className="stroke-status-up opacity-70 motion-safe:animate-[nm-flow_1.8s_linear_infinite]"
                />
              )}
            </g>
          ))}
        </svg>
        {layout.nodes.map((p) => (
          <NodeTile key={p.node.id} n={p.node} x={p.x} y={p.y} />
        ))}
      </div>
    </div>
  );
}
