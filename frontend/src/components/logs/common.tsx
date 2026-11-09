import { useMemo } from 'react';
import { Link } from 'react-router-dom';
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
import { cx, Input, Select } from '../ui';

export function SeverityBadge({ severity }: { severity: number }) {
  const { t } = useTranslation();
  return (
    <span
      className={cx(
        'inline-block min-w-[3.5rem] rounded px-1.5 py-0.5 text-center text-[11px] font-semibold',
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
        className="text-sky-700 hover:underline dark:text-sky-400"
        title={r.source_ip}
      >
        <bdi dir="auto">{r.device_name ?? `#${r.device_id}`}</bdi>
      </Link>
    );
  return (
    <span className="inline-flex items-center gap-1">
      <bdi dir="ltr" className="font-mono text-xs">
        {r.source_ip}
      </bdi>
      <span className="rounded bg-slate-100 px-1 text-[10px] text-slate-500 dark:bg-slate-800">
        {t('logs.unknownShort')}
      </span>
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
        className="inline-flex rounded-md ring-1 ring-slate-300 dark:ring-slate-600"
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
              'px-2 py-1 text-xs first:rounded-s-md last:rounded-e-md',
              filter.preset === p
                ? 'bg-sky-600 text-white'
                : 'hover:bg-slate-100 dark:hover:bg-slate-800',
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
          <span className="text-slate-400">–</span>
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
            className="h-3.5 w-3.5 rounded border-slate-300 text-sky-600 dark:border-slate-600 dark:bg-slate-900"
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
      <div className="mb-1 text-xs font-medium text-slate-500">{title}</div>
      {items.length === 0 ? (
        <div className="text-xs text-slate-400">{t('common.none')}</div>
      ) : (
        <ul className="space-y-0.5">
          {items.map((x) => (
            <li key={x.key} className="relative flex items-center justify-between gap-2 text-xs">
              <span
                className="absolute inset-y-0 start-0 rounded bg-sky-100 dark:bg-sky-950/60"
                style={{ width: `${(x.count / max) * 100}%` }}
                aria-hidden="true"
              />
              <span className="relative min-w-0 truncate ps-1">{x.label}</span>
              <span className="relative pe-1 tabular-nums text-slate-600 dark:text-slate-300">
                {x.count}
              </span>
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
