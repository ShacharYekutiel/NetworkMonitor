import { Fragment, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import type { Port } from '../../api/types';
import {
  gapAfter,
  layoutFaceplate,
  type FaceplateBlock,
  type FaceplateCell,
} from '../../lib/faceplate';
import { TILE_CLASS, tileView, type TileIcon, type TileKind } from '../../lib/ports';
import { BanIcon, DesktopIcon, SwitchIcon, TrunkIcon, WifiIcon } from '../icons';
import { cx } from '../ui';
import { usePortText } from './text';
import { lagColorByMember, lagGroups, portName } from '../../lib/lag';

const ICONS: Record<
  Exclude<TileIcon, null>,
  (p: { size?: number; className?: string }) => JSX.Element
> = {
  client: DesktopIcon,
  ap: WifiIcon,
  switch: SwitchIcon,
  disabled: BanIcon,
};

/** Tile size: 28×28 px, 22×22 below 640 px (architecture.md § 11.5). */
const TILE = 'h-[22px] w-[22px] sm:h-7 sm:w-7';

export function TileGlyph({
  kind,
  icon,
  trunk,
  badge,
  className,
}: {
  kind: TileKind;
  icon: TileIcon;
  trunk?: boolean;
  badge?: string | null;
  className?: string;
}) {
  const Icon = icon ? ICONS[icon] : null;
  return (
    <span
      className={cx(
        'relative flex items-center justify-center rounded-[4px]',
        TILE,
        TILE_CLASS[kind],
        className,
      )}
    >
      {Icon && <Icon size={14} className="h-3 w-3 sm:h-3.5 sm:w-3.5" />}
      {trunk && (
        <span className="absolute -end-1 -top-1 flex h-3 w-3 items-center justify-center rounded-full bg-white text-green-700 ring-1 ring-green-600 dark:bg-slate-900 dark:text-green-400">
          <TrunkIcon size={8} strokeWidth={3} />
        </span>
      )}
      {badge && (
        <span className="absolute -bottom-1 -end-1.5 min-w-[14px] rounded-full bg-slate-800 px-0.5 text-center text-[8px] font-semibold leading-[12px] text-white ring-1 ring-white dark:bg-slate-100 dark:text-slate-900 dark:ring-slate-900">
          {badge}
        </span>
      )}
    </span>
  );
}

function Tile({
  port,
  selected,
  onSelect,
  lagColor,
}: {
  port: Port;
  selected: boolean;
  onSelect: (p: Port) => void;
  /** [2b-3] colour of the port's LAG (bar under the tile) */
  lagColor?: string;
}) {
  const text = usePortText();
  const v = tileView(port);
  return (
    <button
      type="button"
      onClick={() => onSelect(port)}
      aria-label={text.ariaLabel(port)}
      // The real name: labels in the fallback and "other" blocks are positions.
      title={port.name || port.descr || String(port.if_index)}
      aria-pressed={selected}
      data-if-index={port.if_index}
      data-tile={v.kind}
      className={cx(
        'block rounded-[4px] focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 focus-visible:ring-offset-1',
        selected && 'ring-2 ring-sky-500 ring-offset-1 dark:ring-offset-slate-900',
      )}
    >
      <TileGlyph kind={v.kind} icon={v.icon} trunk={v.trunk} badge={v.badge} />
      {lagColor && (
        <span
          className="mt-[2px] block h-[3px] rounded-sm"
          style={{ background: lagColor }}
          data-lag={port.lag?.if_index}
          aria-hidden="true"
        />
      )}
    </button>
  );
}

function Label({ cell }: { cell: FaceplateCell }) {
  return (
    <span className="block h-3.5 text-center text-[10px] leading-[14px] tabular-nums text-slate-500 dark:text-slate-400">
      {cell?.label ?? ''}
    </span>
  );
}

function Block({
  block,
  byIndex,
  selected,
  onSelect,
  lagColors,
}: {
  block: FaceplateBlock;
  byIndex: Map<number, Port>;
  selected: number | null;
  onSelect: (p: Port) => void;
  lagColors: Map<number, string>;
}) {
  const { t } = useTranslation();
  const cols = block.top.map((top, i) => ({ top, bottom: block.bottom[i] }));
  const cell = (c: FaceplateCell) => {
    const p = c ? byIndex.get(c.if_index) : undefined;
    return p ? (
      <Tile
        port={p}
        selected={selected === p.if_index}
        onSelect={onSelect}
        lagColor={lagColors.get(p.if_index)}
      />
    ) : (
      <span className={cx('block', TILE)} />
    );
  };
  return (
    <div
      className={cx(
        'shrink-0 rounded-md px-1.5 py-1',
        block.uplink &&
          'bg-slate-200/70 ring-1 ring-inset ring-slate-300 dark:bg-slate-800 dark:ring-slate-700',
      )}
      data-block={block.key}
      aria-label={block.uplink ? t('ports.uplinkGroup') : undefined}
      role="group"
    >
      <div className="flex">
        {cols.map((c, i) => (
          <Fragment key={i}>
            <div className="flex flex-col items-center gap-1 px-[2px]">
              <Label cell={c.top} />
              {cell(c.top)}
              {cell(c.bottom)}
              <Label cell={c.bottom} />
            </div>
            {gapAfter(block, i) && <div className="w-2 shrink-0 sm:w-3" aria-hidden="true" />}
          </Fragment>
        ))}
      </div>
    </div>
  );
}

export function Faceplate({
  ports,
  selected,
  onSelect,
}: {
  ports: Port[];
  selected: number | null;
  onSelect: (p: Port) => void;
}) {
  const { t } = useTranslation();
  const fp = useMemo(() => layoutFaceplate(ports), [ports]);
  const byIndex = useMemo(() => new Map(ports.map((p) => [p.if_index, p])), [ports]);
  const lags = useMemo(() => lagGroups(ports), [ports]);
  const lagColors = useMemo(() => lagColorByMember(lags), [lags]);
  if (!fp.members.length)
    return <p className="p-4 text-sm text-slate-500">{t('ports.noPhysical')}</p>;
  return (
    <div className="space-y-2" data-testid="faceplate" data-mode={fp.mode}>
      {fp.mode === 'fallback' && (
        <p className="text-xs text-slate-500">{t('ports.fallbackNote')}</p>
      )}
      {/* A physical panel is never mirrored; it scrolls inside its card only. */}
      <div dir="ltr" className="flex overflow-x-auto pb-1" data-testid="faceplate-scroll">
        <div className="flex min-w-max shrink-0 flex-col gap-2 rounded-lg rtl:ml-auto bg-slate-100 p-2 ring-1 ring-inset ring-slate-200 dark:bg-slate-950/60 dark:ring-slate-800">
          {fp.members.map((m) => (
            <div key={m.member}>
              {m.label !== null && (
                <div className="mb-0.5 ps-1 text-left text-[11px] font-semibold text-slate-600 dark:text-slate-300">
                  {t('ports.member', { n: m.member })}
                </div>
              )}
              <div className="flex items-start gap-3">
                {m.blocks.map((b) => (
                  <Block
                    key={b.key}
                    block={b}
                    byIndex={byIndex}
                    selected={selected}
                    onSelect={onSelect}
                    lagColors={lagColors}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
      {lags.length > 0 && (
        <ul
          className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600 dark:text-slate-300"
          data-testid="lag-legend"
        >
          {lags.map((g) => (
            <li key={g.if_index} className="inline-flex items-center gap-1.5">
              <span
                className="inline-block h-[3px] w-5 rounded-sm"
                style={{ background: g.color }}
                aria-hidden="true"
              />
              <bdi dir="ltr">
                {g.name}: {g.members.map(portName).join(', ')}
              </bdi>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const LEGEND_TILES: { kind: TileKind; icon: TileIcon; key: string }[] = [
  { kind: 'empty', icon: null, key: 'empty' },
  { kind: 'up', icon: 'client', key: 'connected' },
  { kind: 'alert', icon: 'client', key: 'alert' },
  { kind: 'emptyAlert', icon: null, key: 'emptyAlert' },
  { kind: 'disabled', icon: 'disabled', key: 'disabled' },
];
const LEGEND_ICONS: { icon: Exclude<TileIcon, null | 'disabled'>; key: string }[] = [
  { icon: 'client', key: 'client' },
  { icon: 'ap', key: 'ap' },
  { icon: 'switch', key: 'switch' },
];

/** The five tiles and the three icons (plus trunk and MAC count markers). */
export function FaceplateLegend() {
  const { t } = useTranslation();
  const item = 'inline-flex items-center gap-1.5';
  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-slate-600 dark:text-slate-300"
      data-testid="faceplate-legend"
    >
      {/* [1.3.2] each tile explains itself on hover (title) */}
      {LEGEND_TILES.map((l) => (
        <span
          key={l.key}
          className={cx(item, 'cursor-help')}
          title={t(`ports.legendHint.${l.key}`)}
          data-testid={`legend-${l.kind}`}
        >
          <TileGlyph kind={l.kind} icon={l.icon} className="!h-5 !w-5" />
          {t(`ports.legend.${l.key}`)}
        </span>
      ))}
      <span className="hidden h-4 w-px bg-slate-300 sm:inline-block dark:bg-slate-700" />
      {LEGEND_ICONS.map((l) => {
        const Icon = ICONS[l.icon];
        return (
          <span key={l.key} className={item}>
            <Icon size={14} className="text-green-600 dark:text-green-500" />
            {t(`ports.legend.${l.key}`)}
          </span>
        );
      })}
      <span className={item}>
        <TileGlyph kind="up" icon="switch" trunk className="!h-5 !w-5 me-1" />
        {t('ports.legend.trunk')}
      </span>
      <span className={item}>
        <TileGlyph kind="up" icon="client" badge="12" className="!h-5 !w-5 me-1.5" />
        {t('ports.legend.macCount')}
      </span>
    </div>
  );
}
