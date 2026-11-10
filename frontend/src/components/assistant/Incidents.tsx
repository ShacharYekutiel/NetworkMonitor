import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { endpoints3b, qk3b, type IncidentQuery } from '../../api/endpoints3b';
import type { Incident, IncidentState } from '../../api/types';
import { useLiveMessages } from '../../hooks/live';
import { formatDateTime, formatRelative } from '../../lib/format';
import { IncidentIcon } from '../icons';
import { Pagination } from '../Pagination';
import { Card, cx, Empty, ErrorBox, Loading } from '../ui';
import { AiBadge, useAiFeature } from './common';

/** Deterministic title from its fields ("core-sw down — 14 devices unreachable"), he/en. */
export function useIncidentTitle() {
  const { t } = useTranslation();
  return (i: Incident) => {
    const f = i.title_fields;
    if (!f?.root) return i.title;
    return t('incident.titleLine', {
      root: f.root,
      rootStatus: t(`status.${f.root_status}`, { defaultValue: f.root_status }),
      members: t(`incident.unit.${f.unit}`, { count: f.count }),
      status: t(`status.${f.status}`, { defaultValue: f.status }),
    });
  };
}

const STATE_CLS: Record<IncidentState, string> = {
  open: 'bg-red-600 text-white',
  resolved: 'bg-green-600 text-white',
  stale: 'bg-slate-400 text-white',
};

export function IncidentStateBadge({ state }: { state: IncidentState }) {
  const { t } = useTranslation();
  return (
    <span className={cx('rounded px-1.5 text-[11px] font-medium', STATE_CLS[state])}>
      {t(`incident.state.${state}`)}
    </span>
  );
}

/** Counts strip: 1 down · 4 unreachable · 0 warning · 2 recovered. */
export function IncidentCounts({ i }: { i: Incident }) {
  const { t } = useTranslation();
  const parts: [string, number, string][] = [
    ['down', i.down, 'text-red-600'],
    ['unreachable', i.unreachable, 'text-red-500'],
    ['warning', i.warning, 'text-amber-600'],
    ['recovered', i.recovered, 'text-green-600'],
  ];
  return (
    <span className="flex flex-wrap gap-x-3 text-xs tabular-nums">
      {parts
        .filter(([, n]) => n > 0)
        .map(([k, n, cls]) => (
          <span key={k} className={cls}>
            {n} {t(k === 'recovered' ? 'incident.recovered' : `status.${k}`)}
          </span>
        ))}
    </span>
  );
}

/** One incident card (dashboard, Events → Incidents). */
export function IncidentCard({ i }: { i: Incident }) {
  const { t } = useTranslation();
  const title = useIncidentTitle();
  return (
    <li className="space-y-1 px-4 py-3" data-testid="incident-card" data-incident={i.id}>
      <div className="flex flex-wrap items-center gap-2">
        <IncidentIcon size={14} className="text-slate-500" />
        <Link
          to={`/incidents/${i.id}`}
          className="font-medium text-sky-700 hover:underline dark:text-sky-400"
          dir="auto"
        >
          {title(i)}
        </Link>
        <IncidentStateBadge state={i.state} />
        <span className="ms-auto text-xs text-slate-500" title={formatDateTime(i.opened_at)}>
          {formatRelative(i.opened_at)}
        </span>
      </div>
      <IncidentCounts i={i} />
      {i.ai && (
        <p
          className="text-sm text-slate-700 dark:text-slate-300"
          dir="auto"
          data-testid="incident-ai"
        >
          <AiBadge className="me-1" />
          {i.ai.summary}
        </p>
      )}
      {i.root_device && (
        <p className="text-xs text-slate-500">
          {t('incident.rootCandidate')}:{' '}
          <Link to={`/devices/${i.root_device.id}`} className="hover:underline">
            {i.root_device.name}
          </Link>
        </p>
      )}
    </li>
  );
}

/** Live list of incidents (WS `incident.update` refreshes it). */
export function IncidentList({
  query,
  emptyText,
  onPage,
}: {
  query: IncidentQuery;
  emptyText?: string;
  onPage?: (p: number) => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: qk3b.incidents(query),
    queryFn: () => endpoints3b.incidents(query),
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
  });
  useLiveMessages(['incident.update'], () => {
    void qc.invalidateQueries({ queryKey: ['incidents'] });
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} className="m-3" />;
  if (!q.data?.items.length) return <Empty>{emptyText ?? t('incident.none')}</Empty>;
  return (
    <>
      <ul className="divide-y divide-slate-100 dark:divide-slate-800" data-testid="incident-list">
        {q.data.items.map((i) => (
          <IncidentCard key={i.id} i={i} />
        ))}
      </ul>
      {onPage && (
        <Pagination
          page={q.data.page}
          pageSize={q.data.page_size}
          total={q.data.total}
          onPage={onPage}
        />
      )}
    </>
  );
}

/** Dashboard card: open incidents (only while `ai_correlation` is ON and there are some). */
export function DashboardIncidents() {
  const { t } = useTranslation();
  const on = useAiFeature('ai_correlation');
  const q = useQuery({
    queryKey: qk3b.incidents({ state: ['open'], page_size: 10 }),
    queryFn: () => endpoints3b.incidents({ state: ['open'], page_size: 10 }),
    enabled: on,
    refetchInterval: 60_000,
  });
  if (!on || !q.data?.items.length) return null;
  return (
    <Card
      title={
        <span className="inline-flex items-center gap-2">
          <IncidentIcon size={14} /> {t('incident.openTitle')}
          <span className="rounded bg-red-600 px-1.5 text-xs tabular-nums text-white">
            {q.data.total}
          </span>
        </span>
      }
      actions={
        <Link
          to="/events?tab=incidents"
          className="text-xs text-sky-700 hover:underline dark:text-sky-400"
        >
          {t('incident.all')}
        </Link>
      }
      bodyClassName="p-0"
    >
      <IncidentList query={{ state: ['open'], page_size: 10 }} />
    </Card>
  );
}
