import { Fragment, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import type { Port } from '../../api/types';
import {
  gapAfter,
  layoutFaceplate,
  type FaceplateBlock,
  type FaceplateCell,
} from '../../lib/faceplate';
import { tileView, type TileIcon, type TileKind } from '../../lib/ports';
import {
  ApIcon,
  BanIcon,
  CameraIcon,
  PCIcon,
  PhoneIcon,
  PrinterIcon,
  QuestionIcon,
  RouterIcon,
  ServerStackIcon,
  TrunkIcon,
  UplinkIcon,
} from '../icons';
import type { ConnectedKind, Port as PortT } from '../../api/types';
import { CONNECTED_KINDS } from '../../api/types';
import { cx } from '../ui';
import { usePortText } from './text';
import { lagColorByMember, lagGroups, portName } from '../../lib/lag';

type IconC = (p: { size?: number; className?: string; strokeWidth?: number }) => JSX.Element;
/** Port icons (prototype PT): what is connected; the crossed circle for an admin-down port. */
export const KIND_ICONS: Record<ConnectedKind | 'disabled', IconC> = {
  computer: PCIcon,
  phone: PhoneIcon,
  ap: ApIcon,
  printer: PrinterIcon,
  camera: CameraIcon,
  server: ServerStackIcon,
  switch: UplinkIcon,
  router: RouterIcon,
  unknown: QuestionIcon,
  disabled: BanIcon,
};

/**
 * Port colours of the switch front panel (BUILD.md § 5 table). The panel is a dark product shot in
 * both themes, so these are fixed: fill / 1.1px frame / icon.
 */
export type PanelColor = 'up' | 'bad' | 'warn' | 'off' | 'offBad' | 'disabled';
const PANEL: Record<PanelColor, string> = {
  up: 'bg-[#1d6b30] border-[#3fe066] text-[#eafff0]',
  bad: 'bg-[#7a1c17] border-[#ff5a50] text-[#ffecea]',
  warn: 'bg-[#7a4a08] border-[#ffaa2b] text-[#fff4e2]',
  off: 'bg-[#0b0b0d] border-[#3d3e42] text-[#6b6b70]',
  offBad: 'bg-[#0b0b0d] border-[#ff5a50] text-[#6b6b70]',
  disabled: 'bg-[#141416] border-[#2b2c30] text-[#6b6b70]',
};
export function panelColor(kind: TileKind, sensor?: PortT['sensor_status']): PanelColor {
  if (kind === 'up') return sensor === 'warning' ? 'warn' : 'up';
  if (kind === 'alert') return 'bad';
  if (kind === 'emptyAlert') return 'offBad';
  if (kind === 'disabled') return 'disabled';
  return 'off';
}

/** Port size: 28px (a 1000-wide panel at ~1300px, BUILD.md § 5). */
const TILE = 'h-7 w-7';

export function TileGlyph({
  kind,
  icon,
  trunk,
  badge,
  className,
  color,
}: {
  kind: TileKind;
  icon: TileIcon;
  trunk?: boolean;
  badge?: string | null;
  className?: string;
  color?: PanelColor;
}) {
  const c = color ?? panelColor(kind);
  const Icon = icon ? KIND_ICONS[icon] : null;
  const empty = c === 'off' || c === 'offBad';
  return (
    <span
      data-color={c}
      className={cx(
        'relative flex items-center justify-center rounded-[3.5px] border-[1.5px] p-[3px]',
        TILE,
        PANEL[c],
        className,
      )}
    >
      {Icon && <Icon size={18} strokeWidth={2.3} className="h-full w-full" />}
      {/* an empty socket: the latch, no icon */}
      {!Icon && empty && (
        <span
          aria-hidden="true"
          className="absolute inset-x-[6px] bottom-[3px] h-[3px] rounded-[1px] bg-[#2b2c30]"
        />
      )}
      {trunk && (
        <span className="absolute -end-1 -top-1 flex h-3 w-3 items-center justify-center rounded-full bg-[#1c1c1e] text-[#3fe066] ring-1 ring-[#3fe066]">
          <TrunkIcon size={8} strokeWidth={3} />
        </span>
      )}
      {badge && (
        <span className="absolute -bottom-1.5 -end-1.5 min-w-[14px] rounded-full bg-white px-0.5 text-center text-[8px] font-semibold leading-[12px] text-black ring-1 ring-[#111]">
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
  const kindName = portDeviceKindName(port, text);
  return (
    <button
      type="button"
      onClick={() => onSelect(port)}
      aria-label={text.ariaLabel(port)}
      // native tooltip "port · type" (BUILD.md § 5)
      title={[port.name || port.descr || String(port.if_index), kindName]
        .filter(Boolean)
        .join(' · ')}
      aria-pressed={selected}
      data-if-index={port.if_index}
      data-tile={v.kind}
      className={cx(
        'block rounded-[4px] transition-transform hover:scale-[1.06] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white',
        selected && 'outline outline-[1.6px] outline-offset-[2.4px] outline-white',
      )}
    >
      <TileGlyph
        kind={v.kind}
        icon={v.icon}
        trunk={v.trunk}
        badge={v.badge}
        color={panelColor(v.kind, port.sensor_status)}
      />
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

function portDeviceKindName(p: Port, text: ReturnType<typeof usePortText>): string {
  return p.state === 'up' ? text.kindLabel(p) : '';
}

function Label({ cell }: { cell: FaceplateCell }) {
  return (
    <span className="block h-3 text-center text-[9px] leading-3 tabular-nums text-[#6e6e73]">
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
        'shrink-0 rounded-[6px] px-1 py-0.5',
        block.uplink && 'bg-white/[0.05] ring-1 ring-inset ring-white/10',
      )}
      data-block={block.key}
      aria-label={block.uplink ? t('ports.uplinkGroup') : undefined}
      role="group"
    >
      <div className="flex">
        {cols.map((c, i) => (
          <Fragment key={i}>
            <div className="flex flex-col items-center gap-[2px] px-[1px]">
              <Label cell={c.top} />
              {cell(c.top)}
              {cell(c.bottom)}
              <Label cell={c.bottom} />
            </div>
            {gapAfter(block, i) && <div className="w-[7px] shrink-0" aria-hidden="true" />}
          </Fragment>
        ))}
      </div>
    </div>
  );
}

const EAR =
  'flex w-[22px] shrink-0 flex-col items-center justify-between self-stretch rounded-[4px] bg-[linear-gradient(90deg,#4a4b50,#2c2d31_50%,#4a4b50)] py-2';
function Screw() {
  return <span aria-hidden="true" className="h-[5px] w-[5px] rounded-full bg-[#5a5b60]" />;
}

/**
 * The switch front panel (BUILD.md § 5): a straight-on dark chassis per stack member with rack
 * ears, name and model on the left, the ports filled and framed by state with an icon of what is
 * connected, vents and SYS/PWR LEDs on the right. Never mirrored; scrolls inside its card.
 */
export function Faceplate({
  ports,
  selected,
  onSelect,
  name,
  model,
  status,
}: {
  ports: Port[];
  selected: number | null;
  onSelect: (p: Port) => void;
  /** device name and model printed on the chassis */
  name?: string;
  model?: string;
  status?: string;
}) {
  const { t } = useTranslation();
  const fp = useMemo(() => layoutFaceplate(ports), [ports]);
  const byIndex = useMemo(() => new Map(ports.map((p) => [p.if_index, p])), [ports]);
  const lags = useMemo(() => lagGroups(ports), [ports]);
  const lagColors = useMemo(() => lagColorByMember(lags), [lags]);
  if (!fp.members.length)
    return <p className="p-4 text-sm text-ink-muted">{t('ports.noPhysical')}</p>;
  const sys =
    status === 'down' ? 'bg-[#ff453a]' : status === 'warning' ? 'bg-[#ff9f0a]' : 'bg-[#32d74b]';
  return (
    <div className="space-y-3" data-testid="faceplate" data-mode={fp.mode}>
      {fp.mode === 'fallback' && (
        <p className="text-xs text-ink-muted">{t('ports.fallbackNote')}</p>
      )}
      {/* A physical panel is never mirrored; it scrolls inside its stage only. */}
      <div
        dir="ltr"
        className="overflow-x-auto rounded-[30px] px-[3%] py-9 stage-bg [scrollbar-width:thin]"
        data-testid="faceplate-scroll"
      >
        <div className="mx-auto flex w-max min-w-full flex-col gap-2.5">
          {fp.members.map((m) => (
            <div
              key={m.member}
              data-member={m.member}
              className="rounded-xl bg-[#0c0c0e] px-2.5 py-2 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_20px_40px_rgba(0,0,0,0.18)]"
            >
              <div className="flex items-stretch gap-0 rounded-[5px] bg-[linear-gradient(180deg,#3a3b3f,#2a2b2e_8%,#1b1c1e_90%,#111214)] shadow-[inset_0_1px_0_rgba(255,255,255,0.18)]">
                <span className={EAR} aria-hidden="true">
                  <Screw />
                  <Screw />
                </span>
                <div className="flex min-w-[150px] flex-col justify-center px-4 py-2 text-left">
                  <b className="max-w-[170px] truncate text-xs font-semibold uppercase tracking-[0.08em] text-[#d8d8dc] [font-family:var(--font-display)]">
                    {name ?? ''}
                  </b>
                  <span className="max-w-[170px] truncate text-[9.5px] tracking-[0.04em] text-[#8e8e93] [font-family:var(--font-display)]">
                    {[model, m.label !== null ? t('ports.member', { n: m.member }) : null]
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                </div>
                <div className="flex flex-1 items-center gap-2.5 py-1.5">
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
                <div className="flex shrink-0 items-center gap-6 px-5" aria-hidden="true">
                  <span className="flex gap-1">
                    {Array.from({ length: 9 }, (_, i) => (
                      <i key={i} className="h-6 w-[3px] rounded-full bg-[#101113]" />
                    ))}
                  </span>
                  <span className="grid gap-2 text-[8px] text-[#8e8e93] [font-family:var(--font-display)]">
                    <span className="flex items-center gap-1.5">
                      <i className={cx('h-[5px] w-[5px] rounded-full', sys)} />
                      SYS
                    </span>
                    <span className="flex items-center gap-1.5">
                      <i className="h-[5px] w-[5px] rounded-full bg-[#32d74b]" />
                      PWR
                    </span>
                  </span>
                </div>
                <span className={EAR} aria-hidden="true">
                  <Screw />
                  <Screw />
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>
      {lags.length > 0 && (
        <ul className="flex flex-wrap gap-2 text-[13px] text-ink-muted" data-testid="lag-legend">
          {lags.map((g) => (
            <li
              key={g.if_index}
              className="inline-flex h-8 items-center gap-2 rounded-full bg-surface px-3 shadow-[0_0_0_1px_var(--border),var(--shadow-card)]"
            >
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

const LEGEND_TILES: { kind: TileKind; key: string }[] = [
  { kind: 'up', key: 'connected' },
  { kind: 'alert', key: 'alert' },
  { kind: 'emptyAlert', key: 'emptyAlert' },
  { kind: 'empty', key: 'empty' },
  { kind: 'disabled', key: 'disabled' },
];

/** Under the panel (BUILD.md § 5): the icons of what is connected, then the port states.
 * `kinds` false (no port on the device has a detected kind) leaves the device-kind entries out. */
export function FaceplateLegend({ kinds = true }: { kinds?: boolean }) {
  const { t } = useTranslation();
  const item = 'inline-flex items-center gap-2 whitespace-nowrap';
  return (
    <div
      className="flex flex-wrap items-center justify-center gap-x-5 gap-y-2.5 text-[12.5px] text-ink-muted"
      data-testid="faceplate-legend"
    >
      {kinds && (
        <>
          {CONNECTED_KINDS.map((k) => {
            const Icon = KIND_ICONS[k];
            return (
              <span key={k} className={item} data-testid={`legend-kind-${k}`}>
                <Icon size={15} className="text-ink" />
                {t(`ports.connectedKind.${k}`)}
              </span>
            );
          })}
          <span className="h-3.5 w-px bg-line-strong" aria-hidden="true" />
        </>
      )}
      {/* [1.3.2] each state explains itself on hover (title) */}
      {LEGEND_TILES.map((l) => (
        <span
          key={l.key}
          className={cx(item, 'cursor-help')}
          title={t(`ports.legendHint.${l.key}`)}
          data-testid={`legend-${l.kind}`}
        >
          <TileGlyph
            kind={l.kind}
            icon={l.kind === 'disabled' ? 'disabled' : null}
            className="!h-[15px] !w-[15px] !rounded-[3px] !p-[1px] [&>span]:hidden"
          />
          {t(`ports.legend.${l.key}`)}
        </span>
      ))}
      <span className="h-3.5 w-px bg-line-strong" aria-hidden="true" />
      {/* the two port marks on their own, the size they have on the panel's tiles, never overlapping */}
      <span className={item} data-testid="legend-trunk">
        <span
          aria-hidden="true"
          className="flex h-4 w-4 items-center justify-center rounded-full bg-[#1c1c1e] text-[#3fe066] ring-1 ring-[#3fe066]"
        >
          <TrunkIcon size={10} strokeWidth={3} />
        </span>
        {t('ports.legend.trunk')}
      </span>
      <span className={item} data-testid="legend-mac-count">
        <span
          aria-hidden="true"
          className="num min-w-[18px] rounded-full bg-white px-1 text-center text-[10px] font-semibold leading-4 text-black ring-1 ring-[#111]"
        >
          12
        </span>
        {t('ports.legend.macCount')}
      </span>
    </div>
  );
}
