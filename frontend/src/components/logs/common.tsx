import { useMemo, type ReactNode } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import type { TopSource } from '../../api/types';
import { useTree } from '../../hooks/queries';
import {
  FACILITIES,
  FACILITY_CODES,
  SEVERITIES,
  SEVERITY_BADGE,
  severityTone,
  TIME_PRESETS,
  type LogFilter,
  type TimePreset,
} from '../../lib/logs';
import { allDevices, findGroup } from '../../lib/tree';
import { formatDateTime, formatLogTime } from '../../lib/format';
import { ShareBar } from '../kit';
import { cx, Input, Select, TAG, TAG_TONE } from '../ui';

export function SeverityBadge({ severity }: { severity: number }) {
  const { t } = useTranslation();
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1.5 whitespace-nowrap text-xs font-medium leading-none text-ink before:h-3.5 before:w-[3px] before:rounded-xs before:content-['']",
        SEVERITY_BADGE[severityTone(severity)],
      )}
      title={`${severity}`}
    >
      {t(`syslogSeverity.${severity}`)}
    </span>
  );
}

export function facilityLabel(n: number): string {
  return FACILITY_CODES[n] ?? String(n);
}

/** The attributed device (link) or the raw source IP. */
export function SourceCell({
  r,
}: {
  r: { device_id: number | null; device_name: string | null; source_ip: string };
}) {
  const { t } = useTranslation();
  if (r.device_id !== null)
    return (
      <Link
        to={`/devices/${r.device_id}`}
        onClick={(e) => e.stopPropagation()}
        className="font-display text-ink hover:underline"
        title={r.source_ip}
      >
        <bdi dir="auto">{r.device_name ?? `#${r.device_id}`}</bdi>
      </Link>
    );
  return (
    <span className="inline-flex items-center gap-1">
      <bdi dir="ltr" className="num">
        {r.source_ip}
      </bdi>
      <span className={cx(TAG, TAG_TONE.neutral)}>{t('logs.unknownShort')}</span>
    </span>
  );
}

/** Device ids of a group and its subgroups (client-side live filter). */
export function useGroupDevices(groupId: number | null): ReadonlySet<number> | null {
  const { data } = useTree();
  return useMemo(() => {
    if (groupId === null || !data) return null;
    const g = findGroup(data.groups, groupId);
    return new Set(g ? allDevices([g]).map((x) => x.device.id) : []);
  }, [data, groupId]);
}

export function TimeRangeSelect({
  filter,
  onChange,
}: {
  filter: LogFilter;
  onChange: (f: Partial<LogFilter>) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <div
        className="inline-flex gap-0.5 self-start rounded-[11px] bg-chip p-[3px]"
        role="group"
        aria-label={t('logs.range')}
      >
        {TIME_PRESETS.map((p: TimePreset) => (
          <button
            key={p}
            type="button"
            aria-pressed={filter.preset === p}
            onClick={() => onChange({ preset: p })}
            className={cx(
              'h-7 whitespace-nowrap rounded-[8px] px-3 text-[13px] font-medium transition-colors',
              filter.preset === p
                ? 'bg-surface text-ink shadow-[0_1px_3px_rgba(0,0,0,0.12)]'
                : 'text-ink-muted hover:text-ink',
            )}
          >
            {t(`logs.preset.${p}`)}
          </button>
        ))}
      </div>
      {filter.preset === 'custom' && (
        <>
          <Input
            type="datetime-local"
            value={filter.from}
            onChange={(e) => onChange({ from: e.target.value })}
            aria-label={t('logs.from')}
            className="w-auto"
            dir="ltr"
          />
          <span className="text-ink-subtle">–</span>
          <Input
            type="datetime-local"
            value={filter.to}
            onChange={(e) => onChange({ to: e.target.value })}
            aria-label={t('logs.to')}
            className="w-auto"
            dir="ltr"
          />
        </>
      )}
    </div>
  );
}

export function SeveritySelect({
  value,
  onChange,
  label,
  anyLabel,
}: {
  value: number | null;
  onChange: (v: number | null) => void;
  label: string;
  anyLabel?: string;
}) {
  const { t } = useTranslation();
  return (
    <Select
      value={value === null ? '' : String(value)}
      onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
      aria-label={label}
    >
      <option value="">{anyLabel ?? t('logs.anySeverity')}</option>
      {SEVERITIES.map((s) => (
        <option key={s} value={s}>
          {t('logs.andWorse', { sev: t(`syslogSeverity.${s}`) })}
        </option>
      ))}
    </Select>
  );
}

/** Multi-select of facilities as a compact checkbox grid. */
export function FacilityPicker({
  value,
  onChange,
}: {
  value: number[];
  onChange: (v: number[]) => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      className="grid grid-cols-3 gap-x-3 gap-y-1 sm:grid-cols-4"
      role="group"
      aria-label={t('logs.facility')}
    >
      {FACILITIES.map((f) => (
        <label key={f} className="inline-flex items-center gap-1.5 text-xs">
          <input
            type="checkbox"
            className="h-3.5 w-3.5 rounded border-line-strong text-accent-ink"
            checked={value.includes(f)}
            onChange={(e) =>
              onChange(e.target.checked ? [...value, f] : value.filter((x) => x !== f))
            }
          />
          <span dir="ltr" title={t(`syslogFacility.${f}`)}>
            {facilityLabel(f)}
          </span>
        </label>
      ))}
    </div>
  );
}

export function TopList({
  title,
  items,
}: {
  title: string;
  items: { key: string; label: React.ReactNode; count: number }[];
}) {
  const { t } = useTranslation();
  const max = Math.max(1, ...items.map((x) => x.count));
  return (
    <div className="min-w-0">
      <h3 className="m-0 mb-2.5 text-[15px] font-semibold">{title}</h3>
      {items.length === 0 ? (
        <div className="text-[13px] text-ink-subtle">{t('common.none')}</div>
      ) : (
        // prototype .share: name and count, then a 6px ink bar on a chip track
        <ul className="m-0 grid list-none gap-2.5 p-0">
          {items.map((x) => (
            <li key={x.key} className="grid gap-1.5">
              <div className="flex items-baseline justify-between gap-2 text-[14px]">
                <span className="min-w-0 truncate font-display">{x.label}</span>
                <b className="num font-medium">{x.count.toLocaleString()}</b>
              </div>
              <ShareBar pct={(x.count / max) * 100} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function topSourceItems(list: TopSource[]) {
  return list.map((s) => ({
    key: `${s.device_id ?? 'x'}-${s.source_ip}`,
    label: s.device_name ? (
      <bdi dir="auto">{s.device_name}</bdi>
    ) : (
      <bdi dir="ltr">{s.source_ip}</bdi>
    ),
    count: s.count,
  }));
}

/** Dot of a syslog line (prototype logs rows): errors red, warnings amber, info green, debug grey. */
export function severityDot(sev: number): string {
  if (sev <= 3) return 'bg-status-down';
  if (sev === 4) return 'bg-status-warning';
  if (sev <= 6) return 'bg-status-up';
  return 'bg-status-paused';
}

/** The table of a log feed drawn as the prototype's grouped rows; header for screen readers only. */
export function LogTable({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <table className="w-full text-[14px] [&_tbody>tr:last-child>td]:border-b-0">
      <thead className="sr-only">
        <tr>
          {head.map((h, i) => (
            <th key={i}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>{children}</tbody>
    </table>
  );
}

/**
 * One log line (prototype .rowx): an optional status dot, the message in mono (LTR), a grey
 * subline, the time at the end. The whole row opens the detail.
 */
export function LogRow({
  dot,
  title,
  sub,
  time,
  onClick,
}: {
  dot?: string;
  title: ReactNode;
  sub: ReactNode;
  time: string;
  onClick: () => void;
}) {
  const cell = 'border-b border-line align-top transition-colors';
  return (
    <tr onClick={onClick} className="cursor-pointer hover:[&>td]:bg-surface-hover">
      {dot !== undefined && (
        <td className={cx(cell, 'w-0 py-0 pe-0 ps-5')}>
          <span
            aria-hidden="true"
            className={cx('mt-[19px] block h-2.5 w-2.5 rounded-full', dot)}
          />
        </td>
      )}
      <td className={cx(cell, 'px-3.5 py-3', dot === undefined && 'ps-5')}>
        <div className="grid min-w-0 gap-0.5">
          <div
            dir="ltr"
            className="line-clamp-2 break-all text-left font-display text-[14.5px] font-medium text-ink rtl:text-right"
          >
            {title}
          </div>
          <div className="min-w-0 break-words text-[13px] text-ink-muted">{sub}</div>
        </div>
      </td>
      <td
        className={cx(cell, 'w-0 whitespace-nowrap py-3 pe-5 ps-3 text-end')}
        title={formatDateTime(time)}
      >
        <span dir="ltr" className="num text-[13px] text-ink-muted">
          {formatLogTime(time)}
        </span>
      </td>
    </tr>
  );
}
