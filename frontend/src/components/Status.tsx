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
        'inline-flex h-[22px] items-center gap-1 whitespace-nowrap rounded-full pe-2 ps-1.5 text-xs font-semibold',
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
            'num inline-flex h-5 min-w-[22px] items-center justify-center whitespace-nowrap rounded-full px-[7px] text-xs font-semibold',
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
  if (!counts.total) return <div className={cx(h, 'rounded-full bg-chip')} />;
  return (
    <div className={cx(h, 'flex gap-0.5 overflow-hidden rounded-full bg-chip')}>
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

function StatusTile({ status, value }: { status: Status | 'total'; value: number }) {
  const { t } = useTranslation();
  const hot = status !== 'total' && value > 0 && isTileHot(status);
  const zero = status !== 'total' && value === 0;
  return (
    <div
      className={cx(
        'grid min-w-0 justify-items-center gap-0.5 px-3 py-4',
        status === 'total' && 'col-span-2 xl:col-span-1',
      )}
    >
      {/* Value first in the DOM ("392 Up"), the label under it (prototype .counts). */}
      <div
        className={cx(
          'num text-[clamp(28px,3vw,38px)] font-medium leading-[1.05] tracking-[-0.03em]',
          hot ? statusClass(status, STATUS_TEXT) : zero ? 'text-ink-subtle' : 'text-ink',
        )}
      >
        {value}
      </div>
      <div className="flex min-w-0 items-center gap-1.5 text-sm text-ink-muted">
        {status !== 'total' && (
          <span className={cx('h-2 w-2 shrink-0 rounded-full', statusClass(status, STATUS_DOT))} />
        )}
        <span className="truncate">
          {status === 'total' ? t('dashboard.total') : t(`status.${status}`)}
        </span>
      </div>
    </div>
  );
}

/**
 * Counters in a row divided by hairlines (prototype .counts), worst first; numbers in their status
 * colour when non-zero. `total` adds a seventh counter.
 */
export function StatusTiles({ counts, total }: { counts: StatusCounts; total?: boolean }) {
  return (
    <div
      className={cx(
        'grid grid-cols-2 overflow-hidden rounded-xl bg-surface shadow-card sm:grid-cols-3 [&>div+div]:border-s [&>div+div]:border-line',
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
