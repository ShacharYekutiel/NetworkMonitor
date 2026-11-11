import { useTranslation } from 'react-i18next';
import type { Status, StatusCounts } from '../api/types';
import { COUNT_ORDER, STATUS_BADGE, STATUS_DOT, STATUS_SOFT, statusClass } from '../lib/status';
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

export function StatusBadge({ status }: { status: Status | string }) {
  const { t } = useTranslation();
  return (
    <span
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
export function StatusBar({ counts }: { counts: StatusCounts }) {
  const { t } = useTranslation();
  if (!counts.total) return <div className="h-2 rounded-sm bg-surface-sunken" />;
  return (
    <div className="flex h-2 gap-0.5 overflow-hidden rounded-sm bg-surface-sunken">
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
