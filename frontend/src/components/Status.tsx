import { useTranslation } from 'react-i18next';
import type { Status, StatusCounts } from '../api/types';
import {
  COUNT_ORDER,
  STATUS_BADGE,
  STATUS_DOT,
  STATUS_SOFT,
  STATUS_TEXT,
  statusClass,
} from '../lib/status';
import { AlertIcon, CheckIcon, LinkIcon, PauseIcon, QuestionIcon, XIcon } from './icons';
import { cx } from './ui';

/** The status glyph (design system § Iconography): status is never shown by colour alone. */
const GLYPH: Record<Status, typeof CheckIcon> = {
  up: CheckIcon,
  warning: AlertIcon,
  down: XIcon,
  unreachable: LinkIcon,
  unknown: QuestionIcon,
  paused: PauseIcon,
};

export function StatusGlyph({
  status,
  size = 12,
  className,
}: {
  status: Status | string;
  size?: number;
  className?: string;
}) {
  const Icon = (GLYPH as Record<string, typeof CheckIcon>)[status] ?? QuestionIcon;
  return <Icon size={size} strokeWidth={2.5} className={cx('shrink-0', className)} />;
}

export function StatusDot({ status, className }: { status: Status | string; className?: string }) {
  const { t } = useTranslation();
  return (
    <span
      className={cx(
        'inline-block h-2 w-2 shrink-0 rounded-full',
        statusClass(status, STATUS_DOT),
        className,
      )}
      title={t(`status.${status}`)}
      aria-label={t(`status.${status}`)}
    />
  );
}

export function StatusBadge({
  status,
  labelled,
}: {
  status: Status | string;
  /** Also expose the word as aria-label (the page title's status, as StatusDot does). */
  labelled?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <span
      aria-label={labelled ? t(`status.${status}`) : undefined}
      className={cx(
        'inline-flex h-5 items-center gap-1 whitespace-nowrap rounded-sm pe-[7px] ps-[5px] text-xs font-semibold',
        statusClass(status, STATUS_SOFT),
      )}
    >
      <StatusGlyph status={status} />
      {t(`status.${status}`)}
    </span>
  );
}

const ORDER: Status[] = COUNT_ORDER;

/** Compact counters (only non-zero statuses), e.g. on a group row. */
export function StatusCountPills({ counts }: { counts: StatusCounts }) {
  const { t } = useTranslation();
  return (
    <span className="inline-flex flex-wrap gap-1">
      {ORDER.filter((s) => counts[s] > 0).map((s) => (
        <span
          key={s}
          title={t(`status.${s}`)}
          className={cx(
            'inline-flex h-[18px] min-w-[20px] items-center justify-center whitespace-nowrap rounded-sm px-[5px] font-mono text-[11px] font-bold tabular-nums',
            statusClass(s, STATUS_BADGE),
          )}
        >
          {counts[s]}
        </span>
      ))}
    </span>
  );
}

/** Proportional bar of a StatusCounts. */
export function StatusBar({ counts, thin }: { counts: StatusCounts; thin?: boolean }) {
  const { t } = useTranslation();
  const h = thin ? 'h-1.5' : 'h-2';
  if (!counts.total) return <div className={cx(h, 'rounded-sm bg-surface-sunken')} />;
  return (
    <div className={cx(h, 'flex gap-0.5 overflow-hidden rounded-sm bg-surface-sunken')}>
      {ORDER.map((s) =>
        counts[s] ? (
          <div
            key={s}
            className={statusClass(s, STATUS_DOT)}
            style={{ width: `${(counts[s] / counts.total) * 100}%` }}
            title={`${t(`status.${s}`)}: ${counts[s]}`}
          />
        ) : null,
      )}
    </div>
  );
}

/** Problem statuses take their soft ground when non-zero; up/paused/unknown stay plain. */
function isTileHot(s: Status): boolean {
  return s === 'down' || s === 'warning' || s === 'unreachable';
}

const STATUS_SOFT_BG: Record<Status, string> = {
  down: 'bg-status-down-soft',
  warning: 'bg-status-warning-soft',
  unreachable: 'bg-status-unreachable-soft',
  unknown: 'bg-status-unknown-soft',
  paused: 'bg-status-paused-soft',
  up: 'bg-status-up-soft',
};

function StatusTile({ status, value }: { status: Status | 'total'; value: number }) {
  const { t } = useTranslation();
  const hot = status !== 'total' && value > 0 && isTileHot(status);
  const zero = status !== 'total' && value === 0;
  return (
    <div
      className={cx(
        'min-w-0 px-4 pb-2.5 pt-3',
        hot ? STATUS_SOFT_BG[status as Status] : 'bg-surface',
        status === 'total' && 'col-span-2 xl:col-span-1',
      )}
    >
      {/* Value first in the DOM ("392 Up"), the label is shown above it. */}
      <div className="flex flex-col-reverse gap-2 rounded-lg">
        <div
          className={cx(
            'font-display text-[34px] font-semibold leading-9 tracking-[-0.01em] tabular-nums',
            hot ? statusClass(status, STATUS_TEXT) : zero ? 'text-ink-subtle' : 'text-ink',
          )}
        >
          {value}
        </div>
        <div className="flex min-w-0 items-center gap-[7px] text-[13px] font-medium leading-4 text-ink-muted">
          {status !== 'total' && (
            <span
              className={cx('h-2 w-2 shrink-0 rounded-full', statusClass(status, STATUS_DOT))}
            />
          )}
          <span className="truncate">
            {status === 'total' ? t('dashboard.total') : t(`status.${status}`)}
          </span>
        </div>
      </div>
    </div>
  );
}

/**
 * Counter tiles joined into one strip divided by hairlines (design system: StatusCounters),
 * worst first; problem tiles take their soft ground. `total` adds a seventh tile.
 */
export function StatusTiles({ counts, total }: { counts: StatusCounts; total?: boolean }) {
  return (
    // The 1px gaps show the border colour as hairlines.
    <div
      className={cx(
        'grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-line bg-line sm:grid-cols-3',
        total ? 'md:grid-cols-4 xl:grid-cols-7' : 'xl:grid-cols-6',
      )}
    >
      {ORDER.map((s) => (
        <StatusTile key={s} status={s} value={counts[s] ?? 0} />
      ))}
      {total && <StatusTile status="total" value={counts.total} />}
    </div>
  );
}
