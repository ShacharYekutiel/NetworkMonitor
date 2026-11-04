import { useTranslation } from 'react-i18next';
import type { Status, StatusCounts } from '../api/types';
import { STATUS_BADGE, STATUS_DOT, statusClass } from '../lib/status';
import { cx } from './ui';

export function StatusDot({ status, className }: { status: Status | string; className?: string }) {
  const { t } = useTranslation();
  return (
    <span
      className={cx(
        'inline-block h-2.5 w-2.5 shrink-0 rounded-full',
        statusClass(status, STATUS_DOT),
        className,
      )}
      title={t(`status.${status}`)}
      aria-label={t(`status.${status}`)}
    />
  );
}

export function StatusBadge({ status }: { status: Status | string }) {
  const { t } = useTranslation();
  return (
    <span
      className={cx(
        'inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium',
        statusClass(status, STATUS_BADGE),
      )}
    >
      {t(`status.${status}`)}
    </span>
  );
}

const ORDER: Status[] = ['down', 'warning', 'unknown', 'paused', 'up'];

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
            'rounded px-1.5 text-xs font-medium tabular-nums',
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
export function StatusBar({ counts }: { counts: StatusCounts }) {
  const { t } = useTranslation();
  if (!counts.total) return <div className="h-2 rounded bg-slate-200 dark:bg-slate-800" />;
  return (
    <div className="flex h-2 overflow-hidden rounded bg-slate-200 dark:bg-slate-800">
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
