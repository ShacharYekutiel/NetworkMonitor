// [2b-1] Maintenance windows: Active now / Upcoming / Recurring / Past, editor, occurrences.
import { useMemo, useState } from 'react';
import { Link } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { endpoints, qk } from '../api/endpoints';
import type { MaintenanceWindow } from '../api/types';
import { ChevronIcon, EditIcon, PlusIcon, TrashIcon, WrenchIcon } from '../components/icons';
import {
  MaintenanceWindowModal,
  OccurrenceList,
  useLocale,
  useScheduleText,
  useServerTimeZone,
} from '../components/Maintenance';
import { useToast } from '../components/toast';
import { EmptyState } from '../components/kit';
import {
  Button,
  ConfirmDialog,
  CountPill,
  cx,
  ErrorBox,
  GroupList,
  GroupTitle,
  ICON_BUTTON,
  Loading,
  Ltr,
  PageHeader,
  Switch,
  TAG,
  TAG_TONE,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { errorMessage } from '../lib/errors';
import {
  formatInTz,
  groupWindows,
  isoWeekday,
  occurrences,
  zonedParts,
  zonedToUtc,
} from '../lib/maintenance';

function ScopeText({ w }: { w: MaintenanceWindow }) {
  const { t } = useTranslation();
  if (w.scope === 'global') return <span>{t('maintenance.scopes.global')}</span>;
  if (w.scope === 'group')
    return (
      <span>
        {t('maintenance.scopes.group')}:{' '}
        <Link className="text-accent-ink hover:underline" to={`/groups/${w.group_id}`}>
          {w.group_name ?? `#${w.group_id}`}
        </Link>
      </span>
    );
  return (
    <span>
      {t('maintenance.scopes.device')}:{' '}
      <Link className="text-accent-ink hover:underline" to={`/devices/${w.device_id}`}>
        {w.device_name ?? `#${w.device_id}`}
      </Link>
    </span>
  );
}

function Occurrences({ id }: { id: number }) {
  const q = useQuery({
    queryKey: qk.occurrences(id, 14),
    queryFn: () => endpoints.occurrences(id, 14),
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  return <OccurrenceList items={(q.data?.items ?? []).slice(0, 10)} />;
}

const DAY_MS = 86_400_000;

type WeekItem = { w: MaintenanceWindow; start: number; end: number };

/** The seven local days (Monday first) of the week `offset` weeks from now, in the server zone. */
function weekDays(nowMs: number, tz: string, offset: number) {
  const p = zonedParts(nowMs, tz);
  const wd = isoWeekday(p.y, p.mo, p.d);
  // local calendar arithmetic on a UTC date (no DST): Monday of this week, then the offset
  const monday = Date.UTC(p.y, p.mo - 1, p.d) - (wd - 1) * DAY_MS + offset * 7 * DAY_MS;
  return Array.from({ length: 8 }, (_, i) => {
    const d = new Date(monday + i * DAY_MS);
    const y = d.getUTCFullYear();
    const mo = d.getUTCMonth() + 1;
    const day = d.getUTCDate();
    return {
      y,
      mo,
      d: day,
      start: zonedToUtc(y, mo, day, 0, 0, tz),
      today: y === p.y && mo === p.mo && day === p.d,
    };
  });
}

/**
 * Week view (prototype maintenance .week): seven day cards, the day number large (today in red),
 * the windows of that day as chips with their hours; a window active now is inverted (ink).
 * Occurrences are computed here from each enabled window's schedule (lib/maintenance), the same
 * rules as the server's `occurrences()`.
 */
function WeekView({
  windows,
  onOpen,
}: {
  windows: MaintenanceWindow[];
  onOpen?: (w: MaintenanceWindow) => void;
}) {
  const { t } = useTranslation();
  const tz = useServerTimeZone();
  const locale = useLocale();
  const [offset, setOffset] = useState(0);
  const now = Date.now();
  const days = useMemo(() => weekDays(now, tz, offset), [now, tz, offset]);
  const from = days[0].start;
  const to = days[7].start;
  const items = useMemo(() => {
    const out: WeekItem[] = [];
    for (const w of windows) {
      if (!w.enabled) continue;
      for (const o of occurrences(w, tz, from, to))
        out.push({ w, start: Date.parse(o.start), end: Date.parse(o.end) });
    }
    return out.sort((a, b) => a.start - b.start);
  }, [windows, tz, from, to]);
  const wdName = new Intl.DateTimeFormat(locale, { weekday: 'short', timeZone: 'UTC' });
  const time = (ms: number) =>
    formatInTz(new Date(ms).toISOString(), tz, locale, {
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    });
  const dm = new Intl.DateTimeFormat(locale, { day: 'numeric', month: 'short', timeZone: 'UTC' });
  const range = `${dm.format(Date.UTC(days[0].y, days[0].mo - 1, days[0].d))} – ${dm.format(
    Date.UTC(days[6].y, days[6].mo - 1, days[6].d),
  )}`;
  return (
    <section aria-labelledby="mw-week" data-testid="maintenance-week">
      <div className="mx-1.5 mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 id="mw-week" className="m-0 text-[22px] font-semibold tracking-[-0.01em]">
          {offset === 0 ? t('maintenance.week.this') : <bdi>{range}</bdi>}
        </h2>
        <div className="flex items-center gap-1.5">
          {offset !== 0 && (
            <Button size="sm" variant="ghost" onClick={() => setOffset(0)}>
              {t('maintenance.week.today')}
            </Button>
          )}
          <Button
            size="sm"
            className={ICON_BUTTON.sm}
            aria-label={t('maintenance.week.prev')}
            onClick={() => setOffset((o) => o - 1)}
          >
            <ChevronIcon size={16} className="rotate-180 rtl:rotate-0" />
          </Button>
          <Button
            size="sm"
            className={ICON_BUTTON.sm}
            aria-label={t('maintenance.week.next')}
            onClick={() => setOffset((o) => o + 1)}
          >
            <ChevronIcon size={16} className="rtl:rotate-180" />
          </Button>
        </div>
      </div>
      <ol className="m-0 grid list-none grid-cols-2 gap-2.5 p-0 sm:grid-cols-4 lg:grid-cols-7">
        {days.slice(0, 7).map((d, i) => {
          const end = days[i + 1].start;
          const mine = items.filter((it) => it.start < end && it.end > d.start);
          return (
            <li
              key={d.start}
              aria-current={d.today ? 'date' : undefined}
              className="grid min-h-[150px] content-start gap-2 rounded-[18px] bg-surface p-3 shadow-card"
            >
              <b className="text-[13px] font-semibold text-ink-muted">
                {wdName.format(Date.UTC(d.y, d.mo - 1, d.d))}
                <span
                  className={cx(
                    'num block text-[26px] leading-tight',
                    d.today ? 'text-status-down' : 'text-ink',
                  )}
                >
                  {d.d}
                </span>
              </b>
              {mine.map((it) => {
                const on = it.start <= now && now < it.end;
                const body = (
                  <>
                    <bdi dir="auto" className="truncate font-medium">
                      {it.w.name}
                    </bdi>
                    <span className="num opacity-70" dir="ltr">
                      {time(it.start)}–{time(it.end)}
                    </span>
                  </>
                );
                const cls = cx(
                  'grid min-w-0 gap-0.5 rounded-[12px] px-2.5 py-2 text-start text-[12.5px]',
                  on ? 'bg-accent text-accent-on' : 'bg-chip text-ink',
                );
                return onOpen ? (
                  <button
                    key={`${it.w.id}-${it.start}`}
                    type="button"
                    className={cx(cls, 'hover:opacity-90')}
                    onClick={() => onOpen(it.w)}
                  >
                    {body}
                  </button>
                ) : (
                  <div key={`${it.w.id}-${it.start}`} className={cls}>
                    {body}
                  </div>
                );
              })}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function windowDot(w: MaintenanceWindow): string {
  if (!w.enabled) return 'bg-ink-subtle';
  return w.active_now ? 'bg-status-up' : 'bg-status-paused';
}

function WindowRow({
  w,
  onEdit,
  onDelete,
}: {
  w: MaintenanceWindow;
  onEdit: (w: MaintenanceWindow) => void;
  onDelete: (w: MaintenanceWindow) => void;
}) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const schedule = useScheduleText();
  const tz = useServerTimeZone();
  const locale = useLocale();
  const [open, setOpen] = useState(false);
  const toggle = useMutation({
    mutationFn: () => endpoints.updateMaintenanceWindow(w.id, { enabled: !w.enabled }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['maintenanceWindows'] });
      void qc.invalidateQueries({ queryKey: qk.maintenanceActive });
    },
    onError: (e) => toast.push('error', errorMessage(t, e)),
  });
  return (
    <li>
      <div className="flex flex-wrap items-center gap-x-3.5 gap-y-2 px-5 py-4">
        <span
          aria-hidden="true"
          className={cx('h-2.5 w-2.5 shrink-0 rounded-full', windowDot(w))}
        />
        <div className={cx('grid min-w-0 flex-1 basis-64 gap-0.5', !w.enabled && 'opacity-60')}>
          <b className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[15.5px] font-medium">
            <bdi dir="auto">{w.name}</bdi>
            {w.active_now && (
              <span className={cx(TAG, TAG_TONE.up)}>{t('maintenance.activeBadge')}</span>
            )}
            {!w.enabled && (
              <span className={cx(TAG, TAG_TONE.neutral)}>{t('maintenance.disabled')}</span>
            )}
          </b>
          <span className="text-[13.5px] text-ink-muted">
            <ScopeText w={w} /> · <bdi>{schedule(w)}</bdi>
          </span>
          {/* a one-time window's schedule already is its occurrence; weekly ones show the next */}
          {(w.kind === 'weekly' || !w.occurrence_start || w.comment) && (
            <span className="text-[13px] text-ink-muted">
              {w.kind === 'weekly' && w.occurrence_start ? (
                <>
                  {w.active_now
                    ? t('maintenance.currentOccurrence')
                    : t('maintenance.nextOccurrence')}
                  :{' '}
                  <bdi className="num">
                    {formatInTz(w.occurrence_start, tz, locale)} –{' '}
                    {formatInTz(w.occurrence_end, tz, locale, { timeStyle: 'short' })}
                  </bdi>
                </>
              ) : (
                !w.occurrence_start && t('maintenance.noneLeft')
              )}
              {w.comment && (
                <>
                  {(w.kind === 'weekly' || !w.occurrence_start) && ' · '}
                  <bdi dir="auto">{w.comment}</bdi>
                </>
              )}
            </span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
            aria-label={t('maintenance.occurrences')}
            title={t('maintenance.occurrences')}
            className={ICON_BUTTON.sm}
          >
            <ChevronIcon
              size={16}
              className={cx('transition-transform', open ? 'rotate-90' : 'rtl:rotate-180')}
            />
          </Button>
          {isAdmin && (
            <>
              <Switch
                label={t('common.enabled')}
                checked={w.enabled}
                disabled={toggle.isPending}
                onChange={() => toggle.mutate()}
                className="mx-1.5"
              />
              <Button
                size="sm"
                variant="ghost"
                aria-label={t('common.edit')}
                onClick={() => onEdit(w)}
              >
                <EditIcon size={14} />
              </Button>
              <Button
                size="sm"
                variant="ghost"
                aria-label={t('common.delete')}
                onClick={() => onDelete(w)}
              >
                <TrashIcon size={14} />
              </Button>
            </>
          )}
        </div>
      </div>
      {open && (
        <div className="px-5 pb-4 ps-[44px]">
          <div className="mb-1.5 text-[12.5px] font-medium text-ink-muted">
            {t('maintenance.occurrences')}
          </div>
          <Occurrences id={w.id} />
        </div>
      )}
    </li>
  );
}

export function MaintenancePage() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const tz = useServerTimeZone();
  const [edit, setEdit] = useState<MaintenanceWindow | 'new' | null>(null);
  const [del, setDel] = useState<MaintenanceWindow | null>(null);
  const [showPast, setShowPast] = useState(false);
  const list = useQuery({
    queryKey: qk.maintenanceWindows({ include_past: true }),
    queryFn: () => endpoints.maintenanceWindows({ include_past: true }),
    refetchInterval: 60_000,
  });
  const active = useQuery({
    queryKey: qk.maintenanceActive,
    queryFn: endpoints.maintenanceActive,
    refetchInterval: 60_000,
  });
  const remove = useMutation({
    mutationFn: (w: MaintenanceWindow) => endpoints.deleteMaintenanceWindow(w.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['maintenanceWindows'] });
      void qc.invalidateQueries({ queryKey: qk.maintenanceActive });
      setDel(null);
    },
  });

  const all = list.data?.items ?? [];
  const groups = groupWindows(all, Date.now());
  const section = (key: 'active' | 'upcoming' | 'recurring' | 'past', items: MaintenanceWindow[]) =>
    items.length > 0 && (
      <section key={key} aria-labelledby={`mw-${key}`}>
        <GroupTitle
          id={`mw-${key}`}
          action={
            key === 'past' && (
              <Button size="sm" variant="ghost" onClick={() => setShowPast((v) => !v)}>
                {showPast ? t('maintenance.hidePast') : t('maintenance.showPast')}
              </Button>
            )
          }
        >
          <span className="inline-flex items-center gap-2">
            {t(`maintenance.sections.${key}`)}
            <CountPill n={items.length} />
          </span>
        </GroupTitle>
        {(key !== 'past' || showPast) && (
          <GroupList label={t(`maintenance.sections.${key}`)}>
            {items.map((w) => (
              <WindowRow key={w.id} w={w} onEdit={setEdit} onDelete={setDel} />
            ))}
          </GroupList>
        )}
      </section>
    );

  return (
    <div>
      <PageHeader
        title={t('maintenance.title')}
        subtitle={
          <>
            {t('maintenance.pausedNote')} · {t('maintenance.tzHeader')}{' '}
            <Ltr className="font-medium">{tz}</Ltr>
          </>
        }
        meta={
          active.data &&
          active.data.device_ids.length > 0 && (
            <span>{t('maintenance.coveredDevices', { count: active.data.device_ids.length })}</span>
          )
        }
        actions={
          isAdmin && (
            <Button variant="primary" onClick={() => setEdit('new')}>
              <PlusIcon size={14} /> {t('maintenance.add')}
            </Button>
          )
        }
      />
      {list.isLoading && <Loading />}
      <ErrorBox error={list.error} />
      {list.data && (
        <div className="grid gap-[26px]">
          <WeekView windows={all} onOpen={isAdmin ? setEdit : undefined} />
          {all.length === 0 ? (
            <div className="rounded-xl bg-surface shadow-card">
              <EmptyState icon={WrenchIcon} title={t('maintenance.emptyTitle')}>
                {t('maintenance.emptyText')}
              </EmptyState>
            </div>
          ) : (
            <>
              {section('active', groups.active)}
              {section('upcoming', groups.upcoming)}
              {section('recurring', groups.recurring)}
              {section('past', groups.past)}
            </>
          )}
        </div>
      )}
      <MaintenanceWindowModal
        open={edit !== null}
        onClose={() => setEdit(null)}
        window={edit && edit !== 'new' ? edit : undefined}
      />
      <ConfirmDialog
        open={!!del}
        title={t('maintenance.deleteTitle')}
        message={t('common.deleteConfirm', { name: del?.name })}
        onConfirm={() => del && remove.mutate(del)}
        onClose={() => setDel(null)}
        busy={remove.isPending}
        error={remove.error}
      />
    </div>
  );
}
