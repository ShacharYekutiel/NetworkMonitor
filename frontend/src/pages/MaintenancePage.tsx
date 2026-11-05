// [2b-1] Maintenance windows: Active now / Upcoming / Recurring / Past, editor, occurrences.
import { useState } from 'react';
import { Link } from 'react-router-dom';
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
import {
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  cx,
  Empty,
  ErrorBox,
  Loading,
  Ltr,
  PageHeader,
} from '../components/ui';
import { useAuth } from '../hooks/auth';
import { errorMessage } from '../lib/errors';
import { formatInTz, groupWindows } from '../lib/maintenance';

function ScopeText({ w }: { w: MaintenanceWindow }) {
  const { t } = useTranslation();
  if (w.scope === 'global') return <span>{t('maintenance.scopes.global')}</span>;
  if (w.scope === 'group')
    return (
      <span>
        {t('maintenance.scopes.group')}:{' '}
        <Link
          className="text-sky-700 hover:underline dark:text-sky-400"
          to={`/groups/${w.group_id}`}
        >
          {w.group_name ?? `#${w.group_id}`}
        </Link>
      </span>
    );
  return (
    <span>
      {t('maintenance.scopes.device')}:{' '}
      <Link
        className="text-sky-700 hover:underline dark:text-sky-400"
        to={`/devices/${w.device_id}`}
      >
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
    <li className={cx('px-4 py-3', !w.enabled && 'opacity-60')}>
      <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          aria-label={t('maintenance.occurrences')}
          className="mt-0.5 rounded p-0.5 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800"
        >
          <ChevronIcon
            size={14}
            className={cx('transition-transform', open ? 'rotate-90' : 'rtl:rotate-180')}
          />
        </button>
        <div className="min-w-0 flex-1 basis-56">
          <div className="flex flex-wrap items-center gap-2">
            <span dir="auto" className="font-medium">
              {w.name}
            </span>
            {w.active_now && (
              <span className="inline-flex items-center gap-1 rounded bg-sky-600 px-1.5 py-0.5 text-xs font-medium text-white">
                <WrenchIcon size={11} /> {t('maintenance.activeBadge')}
              </span>
            )}
            {!w.enabled && (
              <span className="rounded bg-gray-400 px-1.5 py-0.5 text-xs font-medium text-white">
                {t('maintenance.disabled')}
              </span>
            )}
          </div>
          <div className="mt-0.5 text-xs text-slate-600 dark:text-slate-300">
            <ScopeText w={w} />
          </div>
          <div className="mt-0.5 text-xs text-slate-500">
            <bdi>{schedule(w)}</bdi>
          </div>
          {w.comment && (
            <div dir="auto" className="mt-0.5 text-xs italic text-slate-500">
              {w.comment}
            </div>
          )}
        </div>
        <div className="min-w-0 basis-48 text-xs text-slate-600 dark:text-slate-300">
          {w.occurrence_start ? (
            <>
              <div className="text-slate-500">
                {w.active_now
                  ? t('maintenance.currentOccurrence')
                  : t('maintenance.nextOccurrence')}
              </div>
              <bdi className="tabular-nums">
                {formatInTz(w.occurrence_start, tz, locale)} –{' '}
                {formatInTz(w.occurrence_end, tz, locale, { timeStyle: 'short' })}
              </bdi>
            </>
          ) : (
            <span className="text-slate-500">{t('maintenance.noneLeft')}</span>
          )}
        </div>
        {isAdmin && (
          <div className="flex items-center gap-1">
            <Checkbox
              label={<span className="sr-only">{t('common.enabled')}</span>}
              checked={w.enabled}
              disabled={toggle.isPending}
              onChange={() => toggle.mutate()}
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
          </div>
        )}
      </div>
      {open && (
        <div className="mt-2 ps-7">
          <div className="mb-1 text-xs font-semibold text-slate-600 dark:text-slate-300">
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

  const groups = groupWindows(list.data?.items ?? [], Date.now());
  const section = (
    key: 'active' | 'upcoming' | 'recurring' | 'past',
    items: MaintenanceWindow[],
  ) => (
    <Card
      key={key}
      title={
        <span className="inline-flex items-center gap-2">
          {t(`maintenance.sections.${key}`)}
          <span className="rounded bg-slate-200 px-1.5 text-xs tabular-nums dark:bg-slate-700">
            {items.length}
          </span>
        </span>
      }
      bodyClassName="p-0"
      actions={
        key === 'past' && (
          <Button size="sm" variant="ghost" onClick={() => setShowPast((v) => !v)}>
            {showPast ? t('maintenance.hidePast') : t('maintenance.showPast')}
          </Button>
        )
      }
    >
      {key === 'past' && !showPast ? null : items.length ? (
        <ul className="divide-y divide-slate-100 dark:divide-slate-800">
          {items.map((w) => (
            <WindowRow key={w.id} w={w} onEdit={setEdit} onDelete={setDel} />
          ))}
        </ul>
      ) : (
        <Empty>{t(`maintenance.empty.${key}`)}</Empty>
      )}
    </Card>
  );

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('maintenance.title')}
        subtitle={
          <>
            {t('maintenance.tzHeader')} <Ltr className="font-medium">{tz}</Ltr>
            {active.data && (
              <span className="ms-2">
                · {t('maintenance.coveredDevices', { count: active.data.device_ids.length })}
              </span>
            )}
          </>
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
        <>
          {section('active', groups.active)}
          {section('upcoming', groups.upcoming)}
          {section('recurring', groups.recurring)}
          {section('past', groups.past)}
        </>
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
