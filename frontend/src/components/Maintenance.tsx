// [2b-1] Maintenance: badge, global banner, window editor modal, server time zone hook.
import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { MaintenanceWindow } from '../api/types';
import { MAINTENANCE_SCOPES } from '../api/types';
import { useAuth } from '../hooks/auth';
import { formatDuration } from '../lib/format';
import {
  formatInTz,
  maintenanceForm,
  maintenanceToInput,
  occurrences,
  weekdayNames,
  type MaintenanceErrors,
  type MaintenanceForm,
} from '../lib/maintenance';
import { DevicePicker } from './Dependency';
import { GroupSelect } from './EntityForms';
import { WrenchIcon } from './icons';
import { useToast } from './toast';
import {
  Button,
  Checkbox,
  cx,
  ErrorBox,
  Field,
  FieldGroup,
  Input,
  Ltr,
  Modal,
  Textarea,
} from './ui';

/** The server time zone (settings.timezone, public for viewers). */
export function useServerTimeZone(): string {
  const { data } = useQuery({
    queryKey: qk.publicSettings,
    queryFn: endpoints.publicSettings,
    staleTime: 300_000,
  });
  return data?.timezone || 'UTC';
}

export function useLocale(): string {
  const { i18n } = useTranslation();
  return i18n.language === 'he' ? 'he-IL' : 'en-GB';
}

/** "02:00", or "11 Nov, 02:00" when the instant is not within the next 24 hours. */
export function useShortTime() {
  const tz = useServerTimeZone();
  const locale = useLocale();
  return (iso: string | null | undefined) => {
    if (!iso) return '—';
    const ms = Date.parse(iso);
    const soon = Math.abs(ms - Date.now()) < 86_400_000;
    return formatInTz(
      iso,
      tz,
      locale,
      soon
        ? { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
        : { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' },
    );
  };
}

/** Wrench badge "In maintenance until 02:00" (or just the icon when compact). */
export function MaintenanceBadge({
  endsAt,
  name,
  compact,
}: {
  endsAt?: string | null;
  name?: string | null;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  const short = useShortTime();
  const text = endsAt
    ? t('maintenance.badgeUntil', { time: short(endsAt) })
    : t('maintenance.badge');
  const title = name ? `${text} — ${name}` : text;
  if (compact)
    return (
      <span title={title} aria-label={title} className="inline-flex shrink-0 text-sky-600 dark:text-sky-400">
        <WrenchIcon size={13} />
      </span>
    );
  return (
    <span
      title={title}
      className="inline-flex items-center gap-1 whitespace-nowrap rounded bg-sky-100 px-1.5 py-0.5 text-xs font-medium text-sky-800 ring-1 ring-inset ring-sky-200 dark:bg-sky-950 dark:text-sky-200 dark:ring-sky-800"
    >
      <WrenchIcon size={12} />
      <span dir="auto">{text}</span>
    </span>
  );
}

/** Banner in the layout while a global-scope window is active. */
export function GlobalMaintenanceBanner() {
  const { t } = useTranslation();
  const short = useShortTime();
  const { data } = useQuery({
    queryKey: qk.maintenanceActive,
    queryFn: endpoints.maintenanceActive,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  const global = data?.windows.filter((w) => w.scope === 'global') ?? [];
  if (!global.length) return null;
  const w = global[0];
  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-sky-200 bg-sky-50 px-4 py-1.5 text-sm text-sky-900 dark:border-sky-900 dark:bg-sky-950 dark:text-sky-100"
    >
      <WrenchIcon size={14} />
      <span dir="auto">
        {t('maintenance.globalBanner', { name: w.name, time: short(w.occurrence_end) })}
      </span>
      <Link to="/maintenance" className="text-xs underline">
        {t('maintenance.view')}
      </Link>
    </div>
  );
}

/** Weekday chips in the locale's week order (Sunday first in Hebrew), values ISO 1..7. */
export function WeekdayChips({
  value,
  onChange,
}: {
  value: number[];
  onChange: (v: number[]) => void;
}) {
  const locale = useLocale();
  const names = weekdayNames(locale);
  const order = locale.startsWith('he') ? [7, 1, 2, 3, 4, 5, 6] : [1, 2, 3, 4, 5, 6, 7];
  return (
    <div className="flex flex-wrap gap-1">
      {order.map((d) => {
        const on = value.includes(d);
        return (
          <button
            key={d}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(on ? value.filter((x) => x !== d) : [...value, d])}
            className={cx(
              'min-w-[2.75rem] rounded-md px-2 py-1 text-xs font-medium ring-1 ring-inset',
              on
                ? 'bg-sky-600 text-white ring-sky-600'
                : 'bg-white text-slate-700 ring-slate-300 hover:bg-slate-50 dark:bg-slate-900 dark:text-slate-200 dark:ring-slate-600 dark:hover:bg-slate-800',
            )}
          >
            {names[d]}
          </button>
        );
      })}
    </div>
  );
}

/** Human schedule: "Every Sun, Sat 02:00 · 1h 30m" or "11 Nov 22:00 – 11 Nov 02:00". */
export function useScheduleText() {
  const { t } = useTranslation();
  const locale = useLocale();
  const tz = useServerTimeZone();
  const names = weekdayNames(locale);
  return (w: Pick<MaintenanceWindow, 'kind' | 'weekdays' | 'start_time' | 'duration_min' | 'starts_at' | 'ends_at'>) => {
    if (w.kind === 'weekly') {
      const days = [...(w.weekdays ?? [])].sort((a, b) => a - b);
      const dayText = days.length === 7 ? t('maintenance.everyDay') : days.map((d) => names[d]).join(', ');
      return t('maintenance.scheduleWeekly', {
        days: dayText,
        time: w.start_time ?? '',
        duration: formatDuration((w.duration_min ?? 0) * 60),
      });
    }
    return `${formatInTz(w.starts_at, tz, locale)} – ${formatInTz(w.ends_at, tz, locale)}`;
  };
}

function Err({ k }: { k?: string }) {
  const { t } = useTranslation();
  return k ? <span className="text-red-600">{t(k)}</span> : null;
}

export function OccurrenceList({ items }: { items: { start: string; end: string }[] }) {
  const { t } = useTranslation();
  const tz = useServerTimeZone();
  const locale = useLocale();
  if (!items.length) return <p className="text-xs text-slate-500">{t('maintenance.noOccurrences')}</p>;
  return (
    <ul className="space-y-0.5 text-xs tabular-nums text-slate-600 dark:text-slate-300">
      {items.map((o) => (
        <li key={o.start} className="flex flex-wrap gap-x-1">
          <bdi>{formatInTz(o.start, tz, locale, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })}</bdi>
          <span className="text-slate-400">–</span>
          <bdi>{formatInTz(o.end, tz, locale, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })}</bdi>
        </li>
      ))}
    </ul>
  );
}

export function MaintenanceWindowModal({
  open,
  onClose,
  window: w,
  defaults,
}: {
  open: boolean;
  onClose: () => void;
  window?: MaintenanceWindow;
  defaults?: Partial<MaintenanceForm>;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useToast();
  const tz = useServerTimeZone();
  const [f, setF] = useState<MaintenanceForm>(() => maintenanceForm(null, tz));
  const [errors, setErrors] = useState<MaintenanceErrors>({});

  useEffect(() => {
    if (!open) return;
    setF(maintenanceForm(w ?? null, tz, w ? {} : defaults));
    setErrors({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, w, tz]);

  const set = <K extends keyof MaintenanceForm>(k: K, v: MaintenanceForm[K]) =>
    setF((x) => ({ ...x, [k]: v }));

  const m = useMutation({
    mutationFn: (body: ReturnType<typeof maintenanceToInput>['body']) =>
      w ? endpoints.updateMaintenanceWindow(w.id, body) : endpoints.createMaintenanceWindow(body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['maintenanceWindows'] });
      void qc.invalidateQueries({ queryKey: qk.maintenanceActive });
      if (w) void qc.invalidateQueries({ queryKey: ['occurrences', w.id] });
      toast.push('success', t('common.saved'));
      onClose();
    },
  });

  const preview = useMemo(() => {
    const { body, errors: e } = maintenanceToInput(f, tz);
    if (e.starts || e.ends || e.weekdays || e.start_time || e.duration) return [];
    const now = Date.now();
    return occurrences(
      {
        kind: f.kind,
        starts_at: body.starts_at,
        ends_at: body.ends_at,
        weekdays: body.weekdays,
        start_time: body.start_time,
        duration_min: body.duration_min,
      },
      tz,
      now,
      now + 14 * 86_400_000,
      8,
    );
  }, [f, tz]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const r = maintenanceToInput(f, tz);
    setErrors(r.errors);
    if (Object.keys(r.errors).length) return;
    m.mutate(r.body);
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={w ? t('maintenance.edit') : t('maintenance.add')}
      wide
    >
      <form onSubmit={submit} className="space-y-4" noValidate>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={t('common.name')} required hint={<Err k={errors.name} />}>
            <Input
              value={f.name}
              onChange={(e) => set('name', e.target.value)}
              maxLength={128}
              autoFocus
              dir="auto"
            />
          </Field>
          <div className="flex items-end pb-1.5">
            <Checkbox label={t('common.enabled')} checked={f.enabled} onChange={(v) => set('enabled', v)} />
          </div>
          <FieldGroup label={t('maintenance.scope')} className="sm:col-span-2">
            <div className="flex flex-wrap gap-x-5 gap-y-1">
              {MAINTENANCE_SCOPES.map((s) => (
                <label key={s} className="inline-flex items-center gap-2 text-sm">
                  <input
                    type="radio"
                    name="mw-scope"
                    checked={f.scope === s}
                    onChange={() => set('scope', s)}
                  />
                  {t(`maintenance.scopes.${s}`)}
                </label>
              ))}
            </div>
          </FieldGroup>
          {f.scope === 'group' && (
            <Field label={t('maintenance.group')} required hint={<Err k={errors.target} />} className="sm:col-span-2">
              <GroupSelect value={f.group_id} onChange={(v) => set('group_id', v)} allowRoot />
            </Field>
          )}
          {f.scope === 'device' && (
            <Field label={t('maintenance.device')} required hint={<Err k={errors.target} />} className="sm:col-span-2">
              <DevicePicker value={f.device_id} onChange={(v) => set('device_id', v)} />
            </Field>
          )}
          <FieldGroup label={t('maintenance.kind')} className="sm:col-span-2">
            <div className="inline-flex rounded-md ring-1 ring-slate-300 dark:ring-slate-600" role="group">
              {(['once', 'weekly'] as const).map((k) => (
                <button
                  key={k}
                  type="button"
                  aria-pressed={f.kind === k}
                  onClick={() => set('kind', k)}
                  className={cx(
                    'px-3 py-1 text-sm first:rounded-s-md last:rounded-e-md',
                    f.kind === k
                      ? 'bg-sky-600 text-white'
                      : 'hover:bg-slate-100 dark:hover:bg-slate-800',
                  )}
                >
                  {t(`maintenance.kinds.${k}`)}
                </button>
              ))}
            </div>
          </FieldGroup>
          {f.kind === 'once' ? (
            <>
              <Field label={t('maintenance.starts')} required hint={<Err k={errors.starts} />}>
                <Input
                  type="datetime-local"
                  value={f.starts_local}
                  onChange={(e) => set('starts_local', e.target.value)}
                  dir="ltr"
                />
              </Field>
              <Field label={t('maintenance.ends')} required hint={<Err k={errors.ends} />}>
                <Input
                  type="datetime-local"
                  value={f.ends_local}
                  onChange={(e) => set('ends_local', e.target.value)}
                  dir="ltr"
                />
              </Field>
            </>
          ) : (
            <>
              <FieldGroup
                label={t('maintenance.weekdays')}
                required
                hint={<Err k={errors.weekdays} />}
                className="sm:col-span-2"
              >
                <WeekdayChips value={f.weekdays} onChange={(v) => set('weekdays', v)} />
              </FieldGroup>
              <Field label={t('maintenance.startTime')} required hint={<Err k={errors.start_time} />}>
                <Input
                  type="time"
                  value={f.start_time}
                  onChange={(e) => set('start_time', e.target.value)}
                  dir="ltr"
                />
              </Field>
              <Field
                label={t('maintenance.durationMin')}
                required
                hint={
                  errors.duration ? (
                    <Err k={errors.duration} />
                  ) : (
                    /^\d+$/.test(f.duration_min) && formatDuration(Number(f.duration_min) * 60)
                  )
                }
              >
                <Input
                  type="number"
                  min={1}
                  max={10080}
                  value={f.duration_min}
                  onChange={(e) => set('duration_min', e.target.value)}
                  dir="ltr"
                />
              </Field>
            </>
          )}
          <p className="text-xs text-slate-500 sm:col-span-2">
            {t('maintenance.tzNote')} <Ltr className="font-medium">{tz}</Ltr>
          </p>
          <Field label={t('maintenance.comment')} className="sm:col-span-2">
            <Textarea
              rows={2}
              maxLength={1000}
              value={f.comment}
              onChange={(e) => set('comment', e.target.value)}
              dir="auto"
            />
          </Field>
        </div>
        <div className="rounded-md bg-slate-50 p-3 ring-1 ring-inset ring-slate-200 dark:bg-slate-800/60 dark:ring-slate-700">
          <div className="mb-1 text-xs font-semibold text-slate-600 dark:text-slate-300">
            {t('maintenance.preview')}
          </div>
          <OccurrenceList items={preview} />
        </div>
        <ErrorBox error={m.error} />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" variant="primary" busy={m.isPending}>
            {t('common.save')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/** Admin-only "Add maintenance window" button with a prefilled scope. */
export function AddMaintenanceButton({ defaults }: { defaults: Partial<MaintenanceForm> }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const [open, setOpen] = useState(false);
  if (!isAdmin) return null;
  return (
    <>
      <Button onClick={() => setOpen(true)}>
        <WrenchIcon size={14} /> {t('maintenance.addShort')}
      </Button>
      <MaintenanceWindowModal open={open} onClose={() => setOpen(false)} defaults={defaults} />
    </>
  );
}
